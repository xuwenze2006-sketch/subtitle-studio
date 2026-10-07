"""Owned native locks release local mutexes even when cleanup fails."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_budget as cloud


class FileLockReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='owned-lock-release-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'guard.lock'
        self.path.write_bytes(b'\0')
        self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self.key = os.path.normcase(str(self.path.resolve()))
        self.events = []
        self.streams = []
        self.release_error = None
        self.release_after = True
        owner = self

        class OwnedMutex:
            def __init__(self): self.actual = threading.Lock()
            def acquire(self, **kwargs): return self.actual.acquire(**kwargs)
            def locked(self): return self.actual.locked()
            def release(self):
                owner.events.append('local_release')
                if owner.release_error is not None and not owner.release_after:
                    raise owner.release_error
                self.actual.release()
                if owner.release_error is not None:
                    raise owner.release_error

        self.local = OwnedMutex()
        with cloud._THREAD_LOCKS_GUARD:
            self.assertNotIn(self.key, cloud._THREAD_LOCKS)
            cloud._THREAD_LOCKS[self.key] = self.local
        self.addCleanup(self.cleanup_owned)
        if os.name == 'nt':
            import msvcrt
            self.native_module = msvcrt
            self.native_name = 'locking'
            self.acquire_mode, self.unlock_mode = msvcrt.LK_NBLCK, msvcrt.LK_UNLCK
            self.native_args = (1,)
        else:
            import fcntl
            self.native_module = fcntl
            self.native_name = 'flock'
            self.acquire_mode, self.unlock_mode = fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN
            self.native_args = ()
        self.native = getattr(self.native_module, self.native_name)
        guards = ExitStack()
        self.addCleanup(guards.close)
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guards.enter_context(patch(name, side_effect=AssertionError('No network in owned lock tests')))

    def cleanup_owned(self):
        errors = []
        for stream in self.streams:
            try:
                if not stream.closed: stream.close()
            except Exception as error:
                errors.append(error)
        try:
            with cloud._THREAD_LOCKS_GUARD:
                if cloud._THREAD_LOCKS.get(self.key) is not self.local:
                    raise AssertionError('Owned local lock identity changed')
                if self.local.actual.locked(): self.local.actual.release()
                del cloud._THREAD_LOCKS[self.key]
        except Exception as error:
            errors.append(error)
        if errors:
            for error in errors[1:]: errors[0].add_note(f'Owned cleanup: {error!r}')
            raise errors[0]

    @contextmanager
    def faults(self, *, unlock=None, close=None, close_after=True, release=None,
               release_after=True, open_error=None, os_busy=False):
        original_open = Path.open
        owner = self
        self.release_error, self.release_after = release, release_after

        class Handle:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def close(self):
                owner.events.append('close')
                if close is not None and not close_after: raise close
                self.stream.close()
                if close is not None: raise close

        def opened(path, *args, **kwargs):
            if path == self.path:
                if open_error is not None: raise open_error
                stream = original_open(path, *args, **kwargs)
                self.streams.append(stream)
                return Handle(stream)
            return original_open(path, *args, **kwargs)

        def native(fd, mode, *args):
            if mode == self.unlock_mode:
                self.events.append('native_unlock')
                if unlock is not None: raise unlock
            elif os_busy:
                raise BlockingIOError('owned native contention')
            return self.native(fd, mode, *args)

        try:
            with patch.object(Path, 'open', new=opened), \
                    patch.object(self.native_module, self.native_name, side_effect=native):
                yield
        finally:
            self.release_error = None
            self.release_after = True

    def notes(self, error, *parts):
        notes = '\n'.join(getattr(error, '__notes__', ()))
        for part in (str(self.path), *parts): self.assertIn(part, notes)

    def run_lock(self, body=None, *, acquired=True):
        with cloud._file_lock(self.path, blocking=False) as actual:
            self.assertIs(actual, acquired)
            if body is not None: raise body

    def assert_reacquirable(self):
        self.assertFalse(self.local.locked())
        with self.path.open('a+b') as stream:
            stream.seek(0)
            self.native(stream.fileno(), self.acquire_mode, *self.native_args)
            try:
                self.assertFalse(self.local.locked())
            finally:
                stream.seek(0)
                self.native(stream.fileno(), self.unlock_mode, *self.native_args)
        with cloud._file_lock(self.path, blocking=False) as acquired:
            self.assertTrue(acquired)
        self.assertEqual((self.path.stat().st_dev, self.path.stat().st_ino), self.identity)

    def test_native_unlock_failure_still_closes_and_releases_real_guard(self):
        failure = OSError('native unlock failed')
        with self.faults(unlock=failure):
            with self.assertRaises(OSError) as caught: self.run_lock()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ['native_unlock', 'close', 'local_release'])
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.notes(failure, 'unlock')
        self.assert_reacquirable()

    def test_close_after_actual_close_failure_releases_local_and_preserves_inode(self):
        failure = OSError('closed then failed')
        with self.faults(close=failure):
            with self.assertRaises(OSError) as caught: self.run_lock()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ['native_unlock', 'close', 'local_release'])
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.notes(failure, 'close')
        self.assert_reacquirable()

    def test_actual_body_error_survives_all_ordinary_cleanup_errors(self):
        primary = RuntimeError('body primary')
        errors = [OSError('unlock secondary'), OSError('close secondary'), OSError('local secondary')]
        errors[0].add_note('original nested diagnostic')
        with self.faults(unlock=errors[0], close=errors[1], release=errors[2]):
            with self.assertRaises(RuntimeError) as caught: self.run_lock(primary)
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.events, ['native_unlock', 'close', 'local_release'])
        self.notes(primary, *(str(error) for error in errors), 'original nested diagnostic')
        self.assert_reacquirable()

    def test_no_body_first_cleanup_error_is_primary_with_later_notes(self):
        errors = [OSError('first unlock'), OSError('later close'), OSError('later local')]
        with self.faults(unlock=errors[0], close=errors[1], release=errors[2]):
            with self.assertRaises(OSError) as caught: self.run_lock()
        self.assertIs(caught.exception, errors[0])
        self.notes(caught.exception, 'unlock', str(errors[1]), str(errors[2]))
        self.assert_reacquirable()

    def test_local_release_error_is_not_hidden_after_other_cleanup_success(self):
        failure = OSError('local release reported failure')
        with self.faults(release=failure):
            with self.assertRaises(OSError) as caught: self.run_lock()
        self.assertIs(caught.exception, failure)
        self.notes(failure, 'local')
        self.assert_reacquirable()

    def test_unrelated_caller_except_does_not_hide_cleanup_error(self):
        unrelated = RuntimeError('outside context')
        failure = OSError('own cleanup')
        try: raise unrelated
        except RuntimeError:
            with self.faults(close=failure):
                with self.assertRaises(OSError) as caught: self.run_lock()
        self.assertIs(caught.exception, failure)
        self.assertFalse(getattr(unrelated, '__notes__', ()))
        self.assert_reacquirable()

    def test_pre_yield_open_error_is_primary_and_local_release_is_attempted(self):
        primary = PermissionError('owned open failure')
        secondary = OSError('local cleanup failure')
        with self.faults(open_error=primary, release=secondary):
            with self.assertRaises(PermissionError) as caught: self.run_lock()
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.events, ['local_release'])
        self.notes(primary, str(secondary))
        self.assert_reacquirable()

    def test_nonblocking_native_contention_does_not_unlock_unowned_guard(self):
        failure = OSError('close after unavailable guard')
        with self.faults(os_busy=True, close=failure):
            with self.assertRaises(OSError) as caught: self.run_lock(acquired=False)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.events, ['close', 'local_release'])
        self.assert_reacquirable()

    def test_real_native_contention_returns_false_without_releasing_foreign_guard(self):
        with self.path.open('a+b') as held:
            held.seek(0)
            self.native(held.fileno(), self.acquire_mode, *self.native_args)
            try:
                with self.faults(): self.run_lock(acquired=False)
                self.assertEqual(self.events, ['close', 'local_release'])
                self.assertFalse(self.local.locked())
                with self.path.open('a+b') as other:
                    other.seek(0)
                    with self.assertRaises(OSError):
                        self.native(other.fileno(), self.acquire_mode, *self.native_args)
            finally:
                held.seek(0)
                self.native(held.fileno(), self.unlock_mode, *self.native_args)
        self.assert_reacquirable()

    def test_local_contention_does_not_release_someone_elses_mutex(self):
        self.assertTrue(self.local.actual.acquire(blocking=False))
        failure = RuntimeError('body while not acquired')
        with patch.object(Path, 'open', side_effect=AssertionError('must not open unowned guard')):
            with self.assertRaises(RuntimeError) as caught: self.run_lock(failure, acquired=False)
        self.assertIs(caught.exception, failure)
        self.assertTrue(self.local.locked())
        self.assertEqual(self.events, [])
        self.local.actual.release()
        self.assert_reacquirable()

    def test_unknown_close_with_failed_unlock_releases_local_without_claiming_os_release(self):
        unlock = OSError('unlock did not happen')
        close = OSError('close did not happen')
        with self.faults(unlock=unlock, close=close, close_after=False):
            with self.assertRaises(OSError) as caught: self.run_lock()
        self.assertIs(caught.exception, unlock)
        self.notes(unlock, str(close))
        self.assertFalse(self.local.locked())
        self.assertTrue(any(not stream.closed for stream in self.streams))
        with self.path.open('a+b') as other:
            other.seek(0)
            with self.assertRaises(OSError):
                self.native(other.fileno(), self.acquire_mode, *self.native_args)
        for stream in self.streams:
            stream.close()
        self.assert_reacquirable()

    def test_new_cleanup_interrupt_has_priority_and_remaining_steps_are_attempted(self):
        for stage in ('unlock', 'close', 'release'):
            with self.subTest(stage=stage):
                self.events.clear()
                body = RuntimeError('older body')
                interruption = KeyboardInterrupt('new ' + stage)
                errors = {'unlock': OSError('ordinary unlock'), 'close': OSError('ordinary close'),
                          'release': OSError('ordinary local')}
                errors[stage] = interruption
                with self.faults(**errors):
                    with self.assertRaises(KeyboardInterrupt) as caught: self.run_lock(body)
                self.assertIs(caught.exception, interruption)
                self.assertEqual(self.events, ['native_unlock', 'close', 'local_release'])
                self.notes(interruption, *(str(error) for error in errors.values() if error is not interruption))
                self.assert_reacquirable()

    def test_newer_cleanup_interrupt_beats_earlier_interrupt(self):
        first = KeyboardInterrupt('earlier unlock interrupt')
        last = SystemExit('later close interrupt')
        with self.faults(unlock=first, close=last):
            with self.assertRaises(SystemExit) as caught: self.run_lock()
        self.assertIs(caught.exception, last)
        self.notes(last, str(first))
        self.assertEqual(self.events, ['native_unlock', 'close', 'local_release'])
        self.assert_reacquirable()

    def test_body_interrupt_is_not_replaced_by_ordinary_cleanup_error(self):
        body = KeyboardInterrupt('body interrupt')
        secondary = OSError('cleanup secondary')
        with self.faults(unlock=secondary):
            with self.assertRaises(KeyboardInterrupt) as caught: self.run_lock(body)
        self.assertIs(caught.exception, body)
        self.notes(body, str(secondary))
        self.assert_reacquirable()

    def test_normal_body_failure_and_release_preserve_exact_error_without_notes(self):
        body = RuntimeError('unchanged body')
        with self.faults():
            with self.assertRaises(RuntimeError) as caught: self.run_lock(body)
        self.assertIs(caught.exception, body)
        self.assertFalse(getattr(body, '__notes__', ()))
        self.assertEqual(self.events, ['native_unlock', 'close', 'local_release'])
        self.assert_reacquirable()


if __name__ == '__main__':
    unittest.main()
