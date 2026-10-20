"""Ordinary persistence recovery uses synthetic data and owned temporary files."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import atomic_io, credential_store as store, translate


@contextmanager
def reader_without_delete_sharing(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def close():
        nonlocal handle
        if handle is not None:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None

    try:
        yield close
    finally:
        close()


@unittest.skipUnless(sys.platform == 'win32', 'Requires Windows DPAPI and sharing semantics')
class CredentialAtomicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='credential-atomic-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'credentials.json'
        store.save_secrets({'DEEPSEEK_API_KEY': 'fake-existing'}, self.path)
        self.before = self.path.read_bytes()

    def test_real_reader_conflict_saves_once_after_handle_closes(self):
        replace = os.replace
        attempts, conflicts, prepared = [], [], []
        with reader_without_delete_sharing(self.path) as close_reader:
            def publish(source, destination):
                self.assertEqual(Path(destination), self.path)
                attempts.append(Path(source))
                prepared.append(Path(source).read_bytes())
                try:
                    return replace(source, destination)
                except PermissionError as error:
                    conflicts.append(error.winerror)
                    self.assertEqual(self.path.read_bytes(), self.before)
                    close_reader()
                    raise

            with patch.object(store.os, 'replace', side_effect=publish), \
                    patch.object(store, '_protect', wraps=store._protect) as protect, \
                    patch.object(store.os, 'fsync', wraps=store.os.fsync) as sync:
                try:
                    store.save_secrets({'DASHSCOPE_API_KEY': 'fake-new'}, self.path)
                except store.CredentialStoreError:
                    self.fail('A released reader lock must not make credential saving fail')

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(prepared[0], prepared[1])
        self.assertEqual(len(conflicts), 1)
        self.assertIn(conflicts[0], (5, 32, 33))
        self.assertEqual(protect.call_count, 1)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(store.read_saved_secrets(self.path), {
            'DEEPSEEK_API_KEY': 'fake-existing', 'DASHSCOPE_API_KEY': 'fake-new',
        })
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_persistent_sharing_failure_preserves_old_ciphertext_without_reprotecting(self):
        failure = PermissionError('synthetic persistent sharing conflict')
        failure.winerror = 32
        sources, prepared, pauses = [], [], []

        def publish(source, destination):
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(self.path.read_bytes(), self.before)
            sources.append(Path(source))
            prepared.append(Path(source).read_bytes())
            raise failure

        with patch.object(store.os, 'replace', side_effect=publish), \
                patch.object(store, '_protect', wraps=store._protect) as protect, \
                patch.object(atomic_io.time, 'sleep', side_effect=pauses.append):
            with self.assertRaises(store.CredentialStoreError):
                store.save_secrets({'DASHSCOPE_API_KEY': 'fake-new'}, self.path)

        self.assertEqual(len(sources), 6)
        self.assertEqual(len(set(sources)), 1)
        self.assertTrue(all(content == prepared[0] for content in prepared))
        self.assertEqual(protect.call_count, 1)
        self.assertAlmostEqual(sum(pauses), .62)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(store.read_saved_secrets(self.path), {'DEEPSEEK_API_KEY': 'fake-existing'})
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_nonretryable_publish_failure_survives_failed_owned_temporary_cleanup(self):
        primary = OSError('synthetic publication failure')
        cleanup = PermissionError('synthetic write-protected cleanup')
        cleanup.winerror = 19
        neighbor = self.root / 'another-writer.tmp'
        neighbor.write_bytes(b'must remain')
        temporary = []

        def fail_cleanup(path, *, missing_ok=False):
            self.assertNotEqual(path, neighbor)
            self.assertEqual(path.parent, self.root)
            self.assertTrue(path.name.startswith(self.path.name + '.'))
            temporary.append(path)
            raise cleanup

        with patch.object(store.os, 'replace', side_effect=primary) as publish, \
                patch.object(Path, 'unlink', fail_cleanup), \
                patch.object(atomic_io.time, 'sleep', side_effect=AssertionError('must not retry')):
            with self.assertRaises(OSError) as raised:
                store._write_unlocked(self.path, {'DEEPSEEK_API_KEY': 'fake-new'})

        self.assertIs(raised.exception, primary)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(len(temporary), 1)
        self.assertIn(str(temporary[0]), '\n'.join(primary.__notes__))
        self.assertTrue(temporary[0].is_file())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(neighbor.read_bytes(), b'must remain')


class TranslationAtomicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='translation-atomic-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'cache.json'
        self.before = b'{"version":1,"entries":{}}'
        self.path.write_bytes(self.before)

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows sharing semantics')
    def test_real_reader_conflict_persists_returned_translation_without_repeating_request(self):
        replace = os.replace
        sources, prepared, conflicts = [], [], []
        with reader_without_delete_sharing(self.path) as close_reader:
            def publish(source, destination):
                self.assertEqual(Path(destination), self.path)
                sources.append(Path(source))
                prepared.append(Path(source).read_bytes())
                try:
                    return replace(source, destination)
                except PermissionError as error:
                    conflicts.append(error.winerror)
                    self.assertEqual(self.path.read_bytes(), self.before)
                    close_reader()
                    raise

            with patch.object(translate.os, 'replace', side_effect=publish), \
                    patch.object(translate, '_request_json', return_value=[[["你好", "hello", None, None]], None, 'en']) as request, \
                    patch.object(translate, '_wait'), \
                    patch.object(translate.os, 'fsync', wraps=translate.os.fsync) as sync:
                try:
                    result = translate.translate_texts(['hello'], 'en', 'zh-CN', self.path)
                except translate.TranslationError:
                    self.fail('A released reader lock must not discard a returned translation')

        self.assertEqual(result, ['你好'])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0], sources[1])
        self.assertEqual(prepared[0], prepared[1])
        self.assertEqual(len(conflicts), 1)
        self.assertIn(conflicts[0], (5, 32, 33))
        with patch.object(translate, '_request_json', side_effect=AssertionError('must reuse saved translation')):
            self.assertEqual(translate.translate_texts(['hello'], 'en', 'zh-CN', self.path), ['你好'])
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Requires Windows sharing retry policy')
    def test_persistent_sharing_failure_keeps_old_cache_without_repeating_request(self):
        failure = PermissionError('synthetic persistent sharing conflict')
        failure.winerror = 32
        sources, prepared, pauses = [], [], []

        def publish(source, destination):
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(self.path.read_bytes(), self.before)
            sources.append(Path(source))
            prepared.append(Path(source).read_bytes())
            raise failure

        with patch.object(translate.os, 'replace', side_effect=publish), \
                patch.object(translate, '_request_json', return_value=[[["你好", "hello", None, None]], None, 'en']) as request, \
                patch.object(translate, '_wait'), \
                patch.object(atomic_io.time, 'sleep', side_effect=pauses.append):
            with self.assertRaises(translate.TranslationError) as raised:
                translate.translate_texts(['hello'], 'en', 'zh-CN', self.path)

        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(sources), 6)
        self.assertEqual(len(set(sources)), 1)
        self.assertTrue(all(content == prepared[0] for content in prepared))
        self.assertAlmostEqual(sum(pauses), .62)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_nonretryable_cache_failure_survives_failed_owned_temporary_cleanup(self):
        primary = OSError('synthetic cache publication failure')
        cleanup = PermissionError('synthetic write-protected cleanup')
        cleanup.winerror = 19
        neighbor = self.root / 'another-writer.tmp'
        neighbor.write_bytes(b'must remain')
        temporary = []

        def fail_cleanup(path, *, missing_ok=False):
            self.assertNotEqual(path, neighbor)
            self.assertEqual(path.parent, self.root)
            self.assertTrue(path.name.startswith(self.path.name + '.'))
            temporary.append(path)
            raise cleanup

        with patch.object(translate.os, 'replace', side_effect=primary) as publish, \
                patch.object(Path, 'unlink', fail_cleanup), \
                patch.object(atomic_io.time, 'sleep', side_effect=AssertionError('must not retry')):
            try:
                translate._save_cache(self.path, {'version': 1, 'entries': {}})
            except OSError:
                self.fail('Temporary cleanup must not replace the cache publication error')
            except translate.TranslationError as error:
                raised = error
            else:
                self.fail('A failed cache publication must remain visible')

        self.assertIs(raised.__cause__, primary)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(len(temporary), 1)
        self.assertIn(str(temporary[0]), '\n'.join(raised.__notes__))
        self.assertTrue(temporary[0].is_file())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(neighbor.read_bytes(), b'must remain')


if __name__ == '__main__':
    unittest.main()
