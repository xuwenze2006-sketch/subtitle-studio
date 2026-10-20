"""Real tiny-file publication, recovery and native old-handle protection."""
import ctypes
from ctypes import wintypes
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


class OutputPublicationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.output_publication'),
                             'content-preserving publication helper is not implemented')
        self.p = importlib.import_module('subtitle_pipeline.output_publication')
        temporary = tempfile.TemporaryDirectory(prefix='output-publication-')
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.destination = self.project / '原文.srt'
        self.old = b'machine-old'
        self.new = 'machine-new'
        self.destination.write_bytes(self.old)
        self.expected = hashlib.sha256(self.old).hexdigest()

    def prepare(self, relative='原文.srt', expected=None):
        return self.p.prepare_publication(self.project, relative, self.new,
                                          self.expected if expected is None else expected)

    def paths(self, record):
        attempt = self.project / record['attempt']
        return attempt / 'replacement.srt', attempt / 'previous.srt'

    def test_changed_destination_after_journal_has_recoverable_original_object(self):
        record = self.prepare()
        self.destination.write_bytes(b'user-save-after-plan')
        result = self.p.publish_record(self.project, record)
        self.assertEqual(result['status'], 'preserved')
        self.assertEqual(self.destination.read_bytes(), b'machine-new')
        self.assertEqual((self.project / result['capture']).read_bytes(), b'user-save-after-plan')
        self.assertNotEqual(result['status'], 'clean')

    @unittest.skipUnless(os.name == 'nt', 'native exclusive cleanup')
    def test_normal_machine_generations_leave_no_captures_or_attempt_directories(self):
        for index in range(12):
            current = hashlib.sha256(self.destination.read_bytes()).hexdigest()
            record = self.p.prepare_publication(self.project, '原文.srt', f'machine-{index}', current)
            result = self.p.publish_record(self.project, record)
            self.assertEqual((result['status'], result['published']), ('clean', True))
            self.assertEqual(self.destination.read_text(), f'machine-{index}')
        self.assertEqual(list((self.project / '用户修改备份').iterdir()), [])

    def test_originally_absent_destination_collision_never_overwrites_user_file(self):
        self.destination.unlink()
        record = self.p.prepare_publication(self.project, '原文.srt', self.new, None)
        self.destination.write_bytes(b'concurrent-created')
        result = self.p.publish_record(self.project, record)
        self.assertEqual((result['status'], result['published']), ('preserved', False))
        self.assertEqual(self.destination.read_bytes(), b'concurrent-created')

    def test_new_missing_destination_publishes_without_capture(self):
        self.destination.unlink()
        record = self.p.prepare_publication(self.project, '原文.srt', self.new, None)
        result = self.p.publish_record(self.project, record)
        self.assertEqual((result['status'], result['published']), ('clean', True))
        self.assertEqual(self.destination.read_bytes(), b'machine-new')
        self.assertFalse((self.project / record['attempt']).exists())

    def test_recovery_after_replace_with_changed_capture_keeps_conflict(self):
        record = self.prepare()
        prepared, capture = self.paths(record)
        self.destination.write_bytes(b'human-captured')
        os.rename(self.destination, capture)
        os.rename(prepared, self.destination)
        for _ in range(2):
            result = self.p.recover_record(self.project, record)
            self.assertEqual(result['status'], 'preserved')
            self.assertEqual(capture.read_bytes(), b'human-captured')

    @unittest.skipUnless(os.name == 'nt', 'native exclusive cleanup')
    def test_crash_after_clean_capture_removal_recovers_published_digest(self):
        record = self.prepare()
        self.p.publish_record(self.project, record)
        result = self.p.recover_record(self.project, record)
        self.assertEqual((result['status'], result['published']), ('clean', True))

    def test_missing_destination_with_capture_restores_without_marking_published(self):
        record = self.prepare()
        prepared, capture = self.paths(record)
        os.rename(self.destination, capture)
        result = self.p.recover_record(self.project, record)
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertEqual((result['status'], result['published']), ('preserved', False))
        self.assertTrue(prepared.exists())

    def test_restore_collision_preserves_both_file_objects(self):
        record = self.prepare()
        _, capture = self.paths(record)
        os.rename(self.destination, capture)
        real = self.p._publish_missing
        def collide(source, destination):
            Path(destination).write_bytes(b'new-user-file')
            return real(source, destination)
        with patch.object(self.p, '_publish_missing', side_effect=collide):
            result = self.p.recover_record(self.project, record)
        self.assertEqual(result['status'], 'preserved')
        self.assertEqual(self.destination.read_bytes(), b'new-user-file')
        self.assertEqual(capture.read_bytes(), self.old)

    @unittest.skipUnless(os.name == 'nt', 'native ReplaceFile partial result')
    def test_partial_1177_is_not_retried_or_discarded(self):
        record = self.prepare()
        _, capture = self.paths(record)
        failure = OSError('simulated ReplaceFile partial result')
        failure.winerror = 1177
        def partial(destination, prepared, old):
            os.rename(destination, old)
            raise failure
        with patch.object(self.p, '_replace_windows', side_effect=partial) as operation:
            with self.assertRaises(OSError) as caught:
                self.p.publish_record(self.project, record)
        self.assertIs(caught.exception, failure)
        self.assertEqual(operation.call_count, 1)
        self.assertTrue(capture.exists())
        self.assertFalse(self.destination.exists())
        recovered = self.p.recover_record(self.project, record)
        self.assertEqual(recovered['status'], 'preserved')
        self.assertEqual(self.destination.read_bytes(), self.old)

    @unittest.skipUnless(os.name == 'nt', 'native sharing failure retry')
    def test_sharing_retry_captures_an_editor_save_made_between_attempts(self):
        record = self.prepare()
        original = self.p._replace_windows
        failed = False
        def sharing_once(destination, prepared, capture):
            nonlocal failed
            if not failed:
                failed = True
                self.destination.write_bytes(b'human-on-retry')
                error = PermissionError('synthetic short sharing failure')
                error.winerror = 32
                raise error
            return original(destination, prepared, capture)
        with patch.object(self.p, '_replace_windows', side_effect=sharing_once):
            result = self.p.publish_record(self.project, record)
        self.assertEqual(result['status'], 'preserved')
        self.assertEqual((self.project / result['capture']).read_bytes(), b'human-on-retry')

    def test_cancel_before_publication_leaves_original_and_prepared_checkpoint(self):
        from subtitle_pipeline.runner import Cancelled
        record = self.prepare()
        stop = threading.Event(); stop.set()
        with self.assertRaises(Cancelled):
            self.p.publish_record(self.project, record, stop=stop)
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertTrue(self.paths(record)[0].exists())

    def test_automatic_update_destination_gets_same_human_protection(self):
        automatic = self.project / '自动更新' / '原文.srt'
        automatic.parent.mkdir()
        automatic.write_bytes(self.old)
        record = self.prepare('自动更新/原文.srt')
        automatic.write_bytes(b'edited-auto-update')
        result = self.p.publish_record(self.project, record)
        self.assertEqual(result['status'], 'preserved')
        self.assertEqual((self.project / result['capture']).read_bytes(), b'edited-auto-update')

    def test_malformed_records_and_changed_prepared_file_touch_no_destination(self):
        record = self.prepare()
        for change in ({'attempt':'../outside'}, {'relative':'../outside'},
                       {'relative':'C:/outside'}, {'expected_old':'bad'},
                       {'version':True}, {'digest':'0'*63}):
            bad = {**record, **change}
            with self.assertRaises(ValueError):
                self.p.publish_record(self.project, bad)
            self.assertEqual(self.destination.read_bytes(), self.old)
        prepared, _ = self.paths(record)
        prepared.write_bytes(b'prepared-was-modified')
        with self.assertRaises(ValueError):
            self.p.publish_record(self.project, record)
        self.assertEqual(prepared.read_bytes(), b'prepared-was-modified')
        self.assertEqual(self.destination.read_bytes(), self.old)

    def test_portable_fallback_keeps_actual_old_object_and_reports_difference(self):
        record = self.prepare()
        old_id = self.destination.stat().st_ino
        with patch.object(self.p, '_WINDOWS', False):
            result = self.p.publish_record(self.project, record)
        self.assertEqual((result['status'], result['reason']), ('preserved', 'portable_retained'))
        capture = self.project / result['capture']
        self.assertEqual(capture.stat().st_ino, old_id)
        self.assertEqual(self.destination.read_bytes(), b'machine-new')

    def test_prepare_fsync_failure_cleans_only_its_unrecorded_attempt(self):
        failure = OSError('fixture flush failure')
        with patch.object(self.p.os, 'fsync', side_effect=failure):
            with self.assertRaises(OSError) as caught:
                self.prepare()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertEqual(list((self.project / '用户修改备份').iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'native junction ownership boundary')
    def test_backup_parent_junction_inside_project_cannot_be_used_as_owned_storage(self):
        import _winapi
        victim = self.project / 'personal-folder'
        victim.mkdir()
        (victim / 'sentinel.txt').write_bytes(b'user-data')
        _winapi.CreateJunction(str(victim), str(self.project / '用户修改备份'))
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual([p.name for p in victim.iterdir()], ['sentinel.txt'])
        self.assertEqual((victim / 'sentinel.txt').read_bytes(), b'user-data')

    @unittest.skipUnless(os.name == 'nt', 'native junction ownership boundary')
    def test_attempt_junction_cannot_authorize_removal_of_personal_files(self):
        import _winapi
        record = self.prepare()
        prepared, capture = self.paths(record)
        prepared.unlink()
        prepared.parent.rmdir()
        victim = self.project / 'personal-folder'
        victim.mkdir()
        (victim / 'replacement.srt').write_bytes(b'machine-new')
        (victim / 'previous.srt').write_bytes(self.old)
        _winapi.CreateJunction(str(victim), str(prepared.parent))
        with self.assertRaises(ValueError):
            self.p.publish_record(self.project, record)
        self.assertEqual((victim / 'replacement.srt').read_bytes(), b'machine-new')
        self.assertEqual((victim / 'previous.srt').read_bytes(), self.old)

    @unittest.skipUnless(os.name == 'nt', 'native descriptor transfer')
    def test_stream_wrap_failure_closes_native_capture_descriptor(self):
        record = self.prepare()
        failure = OSError('fixture stream construction failure')
        descriptors = []
        def failed_stream(descriptor, *args, **kwargs):
            descriptors.append(descriptor)
            raise failure
        with patch.object(self.p.os, 'fdopen', side_effect=failed_stream):
            with self.assertRaises(OSError) as caught:
                self.p.publish_record(self.project, record)
        self.assertIs(caught.exception, failure)
        closed = True
        for descriptor in descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            closed = False
            os.close(descriptor)
        self.assertTrue(closed, 'native capture descriptor leaked when stream construction failed')
        self.assertEqual(self.paths(record)[1].read_bytes(), self.old)

    def test_discard_prepared_cannot_delete_captured_user_generation(self):
        record = self.prepare()
        self.p.discard_prepared(self.project, record)
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertFalse((self.project / record['attempt']).exists())
        record = self.prepare()
        prepared, capture = self.paths(record)
        os.rename(self.destination, capture)
        with self.assertRaises(ValueError):
            self.p.discard_prepared(self.project, record)
        self.assertEqual(capture.read_bytes(), self.old)
        self.assertTrue(prepared.exists())

    @unittest.skipUnless(os.name == 'nt', 'native hardlink entry replacement')
    def test_replacing_output_hardlink_does_not_modify_source_object(self):
        source = self.project / 'protected-source'
        source.write_bytes(self.old)
        self.destination.unlink()
        os.link(source, self.destination)
        old_id = source.stat().st_ino
        result = self.p.publish_record(self.project, self.prepare())
        self.assertEqual(result['status'], 'clean')
        self.assertEqual(source.read_bytes(), self.old)
        self.assertEqual(source.stat().st_ino, old_id)
        self.assertNotEqual(self.destination.stat().st_ino, old_id)

    def test_recovering_unpublished_preparation_removes_only_owned_empty_attempt(self):
        record = self.prepare()
        result = self.p.recover_record(self.project, record)
        self.assertEqual((result['status'], result['published']), ('clean', False))
        self.assertEqual(self.destination.read_bytes(), self.old)
        self.assertFalse((self.project / record['attempt']).exists())

    @unittest.skipUnless(os.name == 'nt', 'native writable memory mapping')
    def test_writable_mapping_without_user_handles_retains_late_save(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                                       ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileMappingW.argtypes = [wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
                                              wintypes.DWORD,wintypes.DWORD,wintypes.LPCWSTR]
        kernel.CreateFileMappingW.restype = wintypes.HANDLE
        kernel.MapViewOfFile.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
                                         wintypes.DWORD,ctypes.c_size_t]
        kernel.MapViewOfFile.restype = ctypes.c_void_p
        kernel.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.destination),0x80000000|0x40000000,7,None,3,0,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        mapping = kernel.CreateFileMappingW(handle,None,4,0,0,None)
        self.assertTrue(mapping)
        view = kernel.MapViewOfFile(mapping,2,0,0,0)
        self.assertTrue(view)
        kernel.CloseHandle(mapping)
        kernel.CloseHandle(handle)
        try:
            record = self.prepare()
            result = self.p.publish_record(self.project, record)
            self.assertEqual(result['status'], 'busy')
            ctypes.memmove(view,b'late-human!',11)
            self.assertEqual((self.project / result['capture']).read_bytes(), b'late-human!')
        finally:
            kernel.UnmapViewOfFile(view)
        result = self.p.recover_record(self.project, record)
        self.assertEqual(result['status'], 'preserved')
        self.assertEqual((self.project / result['capture']).read_bytes(), b'late-human!')

    @unittest.skipUnless(os.name == 'nt', 'native Windows file handle sharing')
    def test_old_writable_handle_can_write_into_retained_capture_after_replace(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                                       ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WriteFile.argtypes = [wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
        handle = kernel.CreateFileW(str(self.destination),0x40000000,7,None,3,0,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        try:
            record = self.prepare()
            result = self.p.publish_record(self.project, record)
            self.assertEqual(result['status'], 'busy')
            count = wintypes.DWORD()
            self.assertTrue(kernel.WriteFile(handle,b'late-human!',11,ctypes.byref(count),None))
            self.assertEqual((self.project / result['capture']).read_bytes(), b'late-human!')
        finally:
            kernel.CloseHandle(handle)
        recovered = self.p.recover_record(self.project, record)
        self.assertEqual(recovered['status'], 'preserved')
        self.assertEqual((self.project / recovered['capture']).read_bytes(), b'late-human!')


if __name__ == '__main__':
    unittest.main()
