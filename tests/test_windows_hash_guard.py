"""Real Windows sharing controls for a single protected input hash."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import mmap
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import file_observation as fo
from subtitle_pipeline import runner as r
from tests.test_runner_identity_recovery import observe_reads


class PortableHashGuardTests(unittest.TestCase):
    def test_other_platforms_keep_their_existing_file_open_behavior(self):
        with patch.object(fo.os, 'name', 'posix'), \
             patch.object(ctypes, 'WinDLL', create=True,
                          side_effect=AssertionError('native Windows API reached')):
            # The no-op guard itself must not inspect/open a nonexistent path.
            with fo.stable_read_guard('nonexistent-input-for-noop-control'):
                pass


@unittest.skipUnless(os.name == 'nt', 'Windows native file sharing')
class WindowsHashGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='windows-hash-guard-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source.wav'
        self.content = b'original synthetic audio'
        self.source.write_bytes(self.content)

    @contextmanager
    def native_access(self, access):
        """Open without changing bytes, allowing every other sharing mode."""
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(self.source), access, 7, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield handle
        finally:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def assert_released(self):
        self.source.write_bytes(b'after guard release')
        replacement = self.root / 'replacement.wav'
        replacement.write_bytes(self.content)
        replacement.replace(self.source)
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_preexisting_write_or_delete_access_refused_before_content_read(self):
        # Even a handle that has not written anything makes protection impossible.
        for access in (0x40000000, 0x00010000):  # GENERIC_WRITE, DELETE
            for stop in (None, threading.Event()):
                with self.subTest(access=access, cancellable=stop is not None):
                    self.source.write_bytes(self.content)
                    before = fo.file_token(self.source)
                    with self.native_access(access), observe_reads(self.source) as reads:
                        with self.assertRaisesRegex(OSError, '关闭.*重试') as error:
                            r.file_identity(self.source, stop)
                    self.assertEqual(error.exception.winerror, 32)
                    self.assertEqual(reads, [])
                    self.assertEqual(fo.file_token(self.source), before)
                    self.assert_released()

    def test_existing_and_new_ordinary_readers_allowed_during_one_content_read(self):
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None):
                def read_alongside():
                    with open(self.source, 'rb') as peer:
                        self.assertEqual(peer.read(), self.content)
                with self.source.open('rb') as existing, \
                     observe_reads(self.source, after_read=read_alongside) as reads:
                    identity = r.file_identity(self.source, stop)
                    self.assertEqual(existing.read(), self.content)
                self.assertEqual(identity['sha256'], hashlib.sha256(self.content).hexdigest())
                self.assertEqual(len(reads), 1)
                self.assertTrue(reads[0]['eof'])
                self.assertEqual(reads[0]['bytes'], len(self.content))
                self.assert_released()

    def test_writable_mapping_refused_after_original_python_stream_is_closed(self):
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None):
                with self.source.open('r+b') as stream:
                    view = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE)
                self.assertTrue(stream.closed)
                try:
                    with observe_reads(self.source) as reads, \
                         self.assertRaisesRegex(OSError, '关闭.*重试') as raised:
                        r.file_identity(self.source, stop)
                    self.assertEqual(raised.exception.winerror, 32)
                    self.assertEqual(reads, [])
                    # This is a live writable view, rather than an inert fixture.
                    view[:] = b'Z' * len(self.content)
                    self.assertEqual(self.source.read_bytes(), b'Z' * len(self.content))
                finally:
                    view.close()
                self.assert_released()

    def test_native_writable_view_refused_after_all_user_handles_are_closed(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileMappingW.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
        kernel.CreateFileMappingW.restype = wintypes.HANDLE
        kernel.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        kernel.MapViewOfFile.restype = wintypes.LPVOID
        kernel.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
        kernel.UnmapViewOfFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None):
                view = None
                try:
                    with self.native_access(0xc0000000) as writer:  # READ | WRITE
                        mapping = kernel.CreateFileMappingW(writer, None, 4, 0, 0, None)
                        if not mapping:
                            raise ctypes.WinError(ctypes.get_last_error())
                        try:
                            view = kernel.MapViewOfFile(mapping, 2, 0, 0, 0)  # FILE_MAP_WRITE
                            if not view:
                                raise ctypes.WinError(ctypes.get_last_error())
                        finally:
                            if not kernel.CloseHandle(mapping):
                                raise ctypes.WinError(ctypes.get_last_error())
                    # Python mmap may keep its own duplicate file handle. Here only
                    # the native view remains; both file and mapping handles closed.
                    with observe_reads(self.source) as reads, \
                         self.assertRaisesRegex(OSError, '关闭.*重试') as raised:
                        r.file_identity(self.source, stop)
                    self.assertEqual(raised.exception.winerror, 32)
                    self.assertEqual(reads, [])
                    ctypes.memmove(view, b'Y' * len(self.content), len(self.content))
                    self.assertEqual(self.source.read_bytes(), b'Y' * len(self.content))
                finally:
                    if view and not kernel.UnmapViewOfFile(view):
                        raise ctypes.WinError(ctypes.get_last_error())
                self.assert_released()

    def test_hardlink_writers_are_denied_for_the_same_file(self):
        alias = self.root / 'alias.wav'
        os.link(self.source, alias)
        for stop in (None, threading.Event()):
            with self.subTest(cancellable=stop is not None):
                self.source.write_bytes(self.content)
                before = fo.file_token(self.source)
                attempts = []
                def write_alias():
                    with self.assertRaises(PermissionError):
                        alias.write_bytes(b'Z' * len(self.content))
                    attempts.append(True)
                with observe_reads(self.source, after_close=write_alias):
                    identity = r.file_identity(self.source, stop)
                self.assertEqual(attempts, [True])
                self.assertEqual(identity['sha256'], hashlib.sha256(self.content).hexdigest())
                self.assertEqual(fo.file_token(self.source), before)
                self.assertEqual(self.source.read_bytes(), self.content)
                with alias.open('r+b'), observe_reads(self.source) as reads, \
                     self.assertRaisesRegex(OSError, '关闭.*重试'):
                    r.file_identity(self.source, stop)
                self.assertEqual(reads, [])
                alias.write_bytes(self.content)  # No guard leaks after success/failure.

    def test_read_exception_releases_guard(self):
        with patch.object(r, 'sha256', side_effect=OSError('synthetic read failure')), \
             self.assertRaisesRegex(OSError, 'synthetic read failure'):
            r.file_identity(self.source)
        self.assert_released()

    def test_native_observation_exception_releases_guard_before_and_after_read(self):
        original = r.file_token
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                calls = 0
                def observe(path):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError('synthetic observation failure')
                    return original(path)
                with patch.object(r, 'file_token', side_effect=observe), \
                     self.assertRaisesRegex(OSError, 'synthetic observation failure'):
                    r.file_identity(self.source)
                self.assertEqual(calls, fail_at)
                self.assert_released()

    def test_cancel_during_read_releases_guard_and_content_stream(self):
        self.source.write_bytes(b'x' * (2 * 1024 * 1024 + 7))
        stop = threading.Event()
        with observe_reads(self.source, after_read=stop.set) as reads, \
             self.assertRaises(r.Cancelled):
            r.file_identity(self.source, stop)
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['reads'], 1)
        self.assertTrue(reads[0]['stream'].closed)
        self.assert_released()

    def test_invalid_native_handle_is_never_yielded_or_closed(self):
        for code in (32, 33, 5):
            with self.subTest(winerror=code):
                kernel = SimpleNamespace(CreateFileW=Mock(return_value=ctypes.c_void_p(-1).value),
                                         CloseHandle=Mock(return_value=True))
                ctypes.set_last_error(code)
                with patch.object(ctypes, 'WinDLL', return_value=kernel), \
                     self.assertRaises(OSError) as raised:
                    with fo.stable_read_guard(self.source):
                        self.fail('invalid handle yielded an unprotected file')
                self.assertEqual(raised.exception.winerror, code)
                if code in (32, 33):
                    self.assertRegex(str(raised.exception), '关闭.*重试')
                kernel.CreateFileW.assert_called_once_with(str(self.source), 0x80000000,
                                                           1, None, 3, 0, None)
                kernel.CloseHandle.assert_not_called()
                self.assertIs(kernel.CreateFileW.restype, wintypes.HANDLE)

    def test_owned_handle_close_failure_preserves_primary_error(self):
        kernel = SimpleNamespace(CreateFileW=Mock(return_value=123),
                                 CloseHandle=Mock(return_value=False))
        ctypes.set_last_error(6)
        original = RuntimeError('primary failure')
        with patch.object(ctypes, 'WinDLL', return_value=kernel):
            with self.assertRaises(RuntimeError) as raised:
                with fo.stable_read_guard(self.source):
                    raise original
        self.assertIs(raised.exception, original)
        self.assertTrue(any('CloseHandle' in note for note in original.__notes__))
        kernel.CloseHandle.assert_called_once_with(123)

    def test_owned_handle_close_failure_is_visible_without_primary_error(self):
        kernel = SimpleNamespace(CreateFileW=Mock(return_value=123),
                                 CloseHandle=Mock(return_value=False))
        ctypes.set_last_error(6)
        with patch.object(ctypes, 'WinDLL', return_value=kernel), \
             self.assertRaisesRegex(OSError, 'CloseHandle'):
            with fo.stable_read_guard(self.source):
                pass
        kernel.CloseHandle.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
