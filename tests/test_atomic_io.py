"""Offline checks for bounded Windows publication retries."""

import importlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


try:
    atomic = importlib.import_module('subtitle_pipeline.atomic_io')
except ModuleNotFoundError:
    atomic = None


class AtomicReplaceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(atomic, 'shared atomic replacement helper is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'synced.tmp'
        self.target = self.root / 'state.json'
        self.source.write_bytes(b'replacement')
        self.target.write_bytes(b'original')

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_transient_sharing_errors_publish_the_same_complete_file(self):
        replace = atomic.os.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                self.source.write_bytes(b'replacement')
                self.target.write_bytes(b'original')
                sources, pauses = [], []
                error = PermissionError('transient sharing conflict')
                error.winerror = code
                def conflicting_replace(source, target):
                    sources.append(Path(source))
                    if len(sources) < 3:
                        self.assertEqual(self.target.read_bytes(), b'original')
                        self.assertEqual(Path(source).read_bytes(), b'replacement')
                        raise error
                    return replace(source, target)
                with patch.object(atomic.os, 'replace', side_effect=conflicting_replace), \
                     patch.object(atomic.time, 'sleep', side_effect=pauses.append):
                    atomic.replace_with_retry(self.source, self.target)
                self.assertEqual(sources, [self.source] * 3)
                self.assertEqual(pauses, [.02, .04])
                self.assertEqual(self.target.read_bytes(), b'replacement')
                self.assertFalse(self.source.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_persistent_sharing_error_is_bounded_and_preserves_both_files(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                error = PermissionError('persistent sharing conflict')
                error.winerror = code
                pauses = []
                with patch.object(atomic.os, 'replace', side_effect=error) as replace, \
                     patch.object(atomic.time, 'sleep', side_effect=pauses.append):
                    with self.assertRaises(PermissionError) as caught:
                        atomic.replace_with_retry(self.source, self.target)
                self.assertIs(caught.exception, error)
                self.assertEqual(replace.call_count, 6)
                self.assertEqual(pauses, [.02, .04, .08, .16, .32])
                self.assertEqual(self.target.read_bytes(), b'original')
                self.assertEqual(self.source.read_bytes(), b'replacement')

    def test_other_errors_remain_visible_without_retry(self):
        protected = PermissionError('write protected')
        protected.winerror = 19
        for error in (protected, PermissionError('no Windows sharing code'), OSError('disk error')):
            with self.subTest(error=repr(error)), \
                 patch.object(atomic.os, 'replace', side_effect=error) as replace, \
                 patch.object(atomic.time, 'sleep', side_effect=AssertionError('must not retry')):
                with self.assertRaises(type(error)) as caught:
                    atomic.replace_with_retry(self.source, self.target)
                self.assertIs(caught.exception, error)
                self.assertEqual(replace.call_count, 1)
                self.assertEqual(self.target.read_bytes(), b'original')
                self.assertEqual(self.source.read_bytes(), b'replacement')

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_real_reader_sharing_conflict_recovers_after_handle_closes(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(self.target), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        replace, conflicts = atomic.os.replace, []
        def replace_with_reader(source, target):
            nonlocal handle
            try:
                return replace(source, target)
            except PermissionError as error:
                conflicts.append(error.winerror)
                self.assertEqual(self.target.read_bytes(), b'original')
                self.assertEqual(Path(source).read_bytes(), b'replacement')
                kernel.CloseHandle(handle)
                handle = None
                raise
        try:
            with patch.object(atomic.os, 'replace', side_effect=replace_with_reader):
                atomic.replace_with_retry(self.source, self.target)
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
        self.assertEqual(len(conflicts), 1)
        self.assertIn(conflicts[0], (5, 32, 33))
        self.assertEqual(self.target.read_bytes(), b'replacement')
        self.assertFalse(self.source.exists())


class TemporaryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='atomic-cleanup-test-')
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.remove_fixture)
        self.temporary = self.root / 'owned-exact.tmp'
        self.temporary.write_bytes(b'prepared temporary content')
        self.neighbor = self.root / 'another-writer.tmp'
        self.neighbor.write_bytes(b'must stay untouched')

    def remove_fixture(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith('atomic-cleanup-test-'))
        self.temp.cleanup()

    def cleanup(self, path):
        helper = getattr(atomic, 'cleanup_temporary', None)
        self.assertIsNotNone(helper, 'Shared temporary cleanup helper is missing')
        return helper(path)

    def test_removes_only_exact_owned_file_and_missing_file_is_success(self):
        self.cleanup(self.temporary)
        self.assertFalse(self.temporary.exists())
        self.assertEqual(self.neighbor.read_bytes(), b'must stay untouched')
        with patch.object(atomic.time, 'sleep', side_effect=AssertionError('missing file must not retry')):
            self.cleanup(self.temporary)

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_transient_sharing_failures_retry_same_temporary_only(self):
        unlink = Path.unlink
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                self.temporary.write_bytes(b'prepared temporary content')
                failure = PermissionError('temporary held by reader')
                failure.winerror = code
                attempts, pauses = [], []
                def delete(path, *, missing_ok=False):
                    attempts.append((path, missing_ok))
                    if len(attempts) < 3:
                        raise failure
                    return unlink(path, missing_ok=missing_ok)
                with patch.object(Path, 'unlink', delete), patch.object(atomic.time, 'sleep', side_effect=pauses.append):
                    self.cleanup(self.temporary)
                self.assertEqual(attempts, [(self.temporary, True)] * 3)
                self.assertEqual(pauses, [.02, .04])
                self.assertFalse(self.temporary.exists())
                self.assertEqual(self.neighbor.read_bytes(), b'must stay untouched')

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_persistent_failure_without_primary_raises_same_error_after_six_attempts(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                failure = PermissionError('still held by another reader')
                failure.winerror = code
                pauses = []
                with patch.object(Path, 'unlink', side_effect=failure) as delete, \
                        patch.object(atomic.time, 'sleep', side_effect=pauses.append):
                    with self.assertRaises(PermissionError) as raised:
                        self.cleanup(self.temporary)
                self.assertIs(raised.exception, failure)
                self.assertEqual(delete.call_count, 6)
                self.assertEqual(pauses, [.02, .04, .08, .16, .32])
                self.assertAlmostEqual(sum(pauses), .62)
                self.assertTrue(self.temporary.is_file())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_failed_finally_cleanup_keeps_primary_exception_and_notes_exact_path(self):
        primary = ValueError('original publication failure')
        failure = PermissionError('reader still owns temporary')
        failure.winerror = 32
        def caller():
            try:
                raise primary
            finally:
                self.cleanup(self.temporary)
        with patch.object(Path, 'unlink', side_effect=failure) as delete, \
                patch.object(atomic.time, 'sleep') as sleep:
            with self.assertRaises(ValueError) as raised:
                caller()
        self.assertIs(raised.exception, primary)
        self.assertEqual(delete.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        note = '\n'.join(primary.__notes__)
        self.assertIn(str(self.temporary), note)
        self.assertIn('待清理', note)
        self.assertIn('PermissionError', note)
        self.assertIn('reader still owns temporary', note)
        self.assertTrue(self.temporary.is_file())

    def test_other_filesystem_failures_are_not_retried_and_preserve_an_active_interrupt(self):
        protected = PermissionError('write protected')
        protected.winerror = 19
        for failure in (protected, PermissionError('no Windows sharing code'), OSError('disk error')):
            with self.subTest(error=repr(failure)), patch.object(Path, 'unlink', side_effect=failure) as delete, \
                    patch.object(atomic.time, 'sleep', side_effect=AssertionError('must not retry')):
                primary = KeyboardInterrupt('already stopping')
                try:
                    raise primary
                except KeyboardInterrupt:
                    self.cleanup(self.temporary)
                self.assertEqual(delete.call_count, 1)
                self.assertIn(str(failure), '\n'.join(primary.__notes__))
                self.assertIn(str(self.temporary), '\n'.join(primary.__notes__))

    def test_nonretryable_filesystem_failure_without_primary_is_not_suppressed(self):
        failure = OSError('disk error')
        with patch.object(Path, 'unlink', side_effect=failure) as delete, \
                patch.object(atomic.time, 'sleep', side_effect=AssertionError('must not retry')):
            with self.assertRaises(OSError) as raised:
                self.cleanup(self.temporary)
        self.assertIs(raised.exception, failure)
        self.assertEqual(delete.call_count, 1)

    def test_windows_error_code_on_other_platform_does_not_trigger_retries(self):
        failure = PermissionError('simulated sharing error outside Windows')
        failure.winerror = 32
        with patch.object(atomic, 'os', SimpleNamespace(name='posix')), \
                patch.object(Path, 'unlink', side_effect=failure) as delete, \
                patch.object(atomic.time, 'sleep', side_effect=AssertionError('must not retry')):
            with self.assertRaises(PermissionError) as raised:
                self.cleanup(self.temporary)
        self.assertIs(raised.exception, failure)
        self.assertEqual(delete.call_count, 1)

    def test_fresh_interrupt_and_programming_error_during_cleanup_are_not_swallowed(self):
        for failure in (KeyboardInterrupt('new interrupt'), RuntimeError('unexpected cleanup bug')):
            with self.subTest(error=repr(failure)), patch.object(Path, 'unlink', side_effect=failure):
                primary = ValueError('original publication failure')
                try:
                    raise primary
                except ValueError:
                    with self.assertRaises(type(failure)) as raised:
                        self.cleanup(self.temporary)
                self.assertIs(raised.exception, failure)

    def test_successful_cleanup_does_not_add_failure_note_to_primary(self):
        primary = ValueError('original publication failure')
        with self.assertRaises(ValueError) as raised:
            try:
                raise primary
            finally:
                self.cleanup(self.temporary)
        self.assertIs(raised.exception, primary)
        self.assertFalse(hasattr(primary, '__notes__'))
        self.assertFalse(self.temporary.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_real_reader_handle_is_closed_before_retrying_owned_temporary_delete(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(self.temporary), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        pauses = []
        def close_reader(delay):
            nonlocal handle
            pauses.append(delay)
            self.assertTrue(self.temporary.is_file())
            kernel.CloseHandle(handle)
            handle = None
        try:
            with patch.object(atomic.time, 'sleep', side_effect=close_reader):
                self.cleanup(self.temporary)
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
        self.assertEqual(pauses, [.02])
        self.assertFalse(self.temporary.exists())
        self.assertEqual(self.neighbor.read_bytes(), b'must stay untouched')


if __name__ == '__main__':
    unittest.main()
