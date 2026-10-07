"""Acquisition failures survive cleanup of a genuinely owned native guard."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_budget, runner as r


class ProjectLockAcquisitionErrors(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='project-acquisition-')
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.marker = self.project / 'run.lock'
        self.guard = self.project / 'run.guard.lock'
        self.guard.write_bytes(b'\0')
        self.guard_identity = (self.guard.stat().st_dev, self.guard.stat().st_ino)
        self.key = os.path.normcase(str(self.guard.resolve()))
        self.events = []
        self.streams = []
        self.addCleanup(self.cleanup_registry)
        guards = ExitStack()
        self.addCleanup(guards.close)
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guards.enter_context(patch(name, side_effect=AssertionError('No network in acquisition tests')))

    def cleanup_registry(self):
        for stream in self.streams:
            if not stream.closed:
                stream.close()
        with cloud_budget._THREAD_LOCKS_GUARD:
            mutex = cloud_budget._THREAD_LOCKS.get(self.key)
            if mutex is not None:
                self.assertFalse(mutex.locked(), 'Actual owned mutex must be released')
                del cloud_budget._THREAD_LOCKS[self.key]

    @contextmanager
    def cleanup_fault(self, error):
        original_open = Path.open
        if os.name == 'nt':
            import msvcrt
            module, name, unlock = msvcrt, 'locking', msvcrt.LK_UNLCK
        else:
            import fcntl
            module, name, unlock = fcntl, 'flock', fcntl.LOCK_UN
        native = getattr(module, name)

        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == self.guard and args and args[0] == 'a+b':
                self.streams.append(stream)
            return stream

        def locking(fd, mode, *args):
            if mode == unlock:
                self.events.append('native_unlock_attempt')
                raise error
            return native(fd, mode, *args)

        with patch.object(Path, 'open', new=opened), patch.object(module, name, side_effect=locking):
            yield

    def assert_released(self):
        self.assertEqual(self.events, ['native_unlock_attempt'])
        self.assertTrue(self.streams and all(stream.closed for stream in self.streams))
        with cloud_budget._file_lock(self.guard, blocking=False) as acquired:
            self.assertTrue(acquired)
        self.assertEqual((self.guard.stat().st_dev, self.guard.stat().st_ino), self.guard_identity)

    def test_marker_acquisition_error_remains_primary_with_cleanup_note(self):
        primary = PermissionError('marker write denied')
        cleanup = OSError('unlock failed before actual close')
        cleanup.add_note('nested native diagnostic')
        lock = r.ProjectLock(self.project)
        with patch.object(lock, '_acquire', side_effect=primary), self.cleanup_fault(cleanup):
            with self.assertRaises(BaseException) as caught:
                lock.__enter__()
        self.assert_released()
        self.assertIs(caught.exception, primary)
        notes = '\n'.join(primary.__notes__)
        for value in (str(self.guard), str(cleanup), 'nested native diagnostic'):
            self.assertIn(value, notes)
        self.assertFalse(lock.owned)
        self.assertIsNone(lock._guard)
        self.assertFalse(self.marker.exists())
        with lock:
            self.assertTrue(lock.owned)
        self.assertFalse(self.marker.exists())

    def test_corrupt_existing_marker_error_and_original_bytes_are_preserved(self):
        original = b'{broken marker'
        self.marker.write_bytes(original)
        cleanup = OSError('secondary native unlock')
        with self.cleanup_fault(cleanup):
            with self.assertRaises(BaseException) as caught:
                with r.ProjectLock(self.project):
                    self.fail('Corrupt marker must not permit entry')
        self.assert_released()
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIn('项目锁无法读取', str(caught.exception))
        self.assertIn(str(cleanup), '\n'.join(caught.exception.__notes__))
        self.assertEqual(self.marker.read_bytes(), original)

    def test_alive_existing_marker_error_and_original_bytes_are_preserved(self):
        original = json.dumps({'pid': os.getpid(), 'created': 1}).encode()
        self.marker.write_bytes(original)
        cleanup = OSError('secondary native unlock')
        with self.cleanup_fault(cleanup):
            with self.assertRaises(BaseException) as caught:
                with r.ProjectLock(self.project):
                    self.fail('Live marker must not permit entry')
        self.assert_released()
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIn('另一个窗口运行', str(caught.exception))
        self.assertEqual(self.marker.read_bytes(), original)

    def test_actual_acquisition_interrupt_is_not_masked_by_ordinary_cleanup(self):
        primary = KeyboardInterrupt('interrupted marker acquisition')
        cleanup = OSError('ordinary native cleanup')
        with patch.object(r.ProjectLock, '_acquire', side_effect=primary), self.cleanup_fault(cleanup):
            with self.assertRaises(BaseException) as caught:
                r.ProjectLock(self.project).__enter__()
        self.assert_released()
        self.assertIs(caught.exception, primary)
        self.assertIn(str(cleanup), '\n'.join(primary.__notes__))

    def test_new_cleanup_interrupt_keeps_priority_over_acquisition_failure(self):
        primary = PermissionError('older acquisition failure')
        cleanup = KeyboardInterrupt('new native unlock interrupt')
        with patch.object(r.ProjectLock, '_acquire', side_effect=primary), self.cleanup_fault(cleanup):
            with self.assertRaises(BaseException) as caught:
                r.ProjectLock(self.project).__enter__()
        self.assert_released()
        self.assertIs(caught.exception, cleanup)

    def test_successful_cleanup_preserves_exact_acquisition_error_without_notes(self):
        primary = PermissionError('marker write denied without cleanup error')
        lock = r.ProjectLock(self.project)
        with patch.object(lock, '_acquire', side_effect=primary):
            with self.assertRaises(BaseException) as caught:
                lock.__enter__()
        self.assertIs(caught.exception, primary)
        self.assertFalse(getattr(primary, '__notes__', ()))
        with cloud_budget._file_lock(self.guard, blocking=False) as acquired:
            self.assertTrue(acquired)
        self.assertFalse(lock.owned)
        self.assertIsNone(lock._guard)


if __name__ == '__main__':
    unittest.main()
