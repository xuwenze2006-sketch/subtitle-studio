"""Stop I/O races with real temporary markers and synthetic worker processes."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import io
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio


class StudioStopConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-stop-concurrency-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.wav'; self.source.write_bytes(b'synthetic source')
        self.project = self.root/'project'; self.project.mkdir()
        (self.project/'campaign.json').write_text('{"status":"prepared"}', encoding='utf-8')
        self.worker_exit = threading.Event()
        self.worker_waiting = threading.Event()
        self.addCleanup(self.worker_exit.set)
        def blocked(*args, **kwargs):
            raise AssertionError('No network allowed in stop concurrency tests')
        for guard in (patch.object(studio, 'account_environment', return_value={}),
                      patch.object(studio, 'encrypted_names', return_value=set()),
                      patch.object(studio, 'owned_process', side_effect=nullcontext),
                      patch.object(studio, 'wait_for_process_exit', return_value=True),
                      patch.object(socket.socket, 'connect', side_effect=blocked),
                      patch.object(socket.socket, 'connect_ex', side_effect=blocked),
                      patch.object(socket, 'create_connection', side_effect=blocked)):
            guard.start(); self.addCleanup(guard.stop)
        self.app = studio.StudioController(source=str(self.source), campaign=str(self.project),
                                           state_path=self.root/'studio.json')
        self.addCleanup(self.finish_worker)
        self.arm_old_run()

    def arm_old_run(self, action='prepare'):
        self.app.job = {**self.app._idle_job(), 'busy': True, 'action': action,
                        'status': 'running', 'message': 'synthetic old run'}
        self.app.stop_event = threading.Event()
        self.marker = self.root/'stops'/'old-unique-run.stop'
        self.app._cancel_marker = self.marker if action != 'local' else None

    def finish_worker(self):
        self.worker_exit.set()
        if self.app.worker is not None:
            self.app.worker.join(3)
            self.assertFalse(self.app.worker.is_alive())

    @contextmanager
    def delayed_marker(self, phase='write', error=None, error_after_io=False):
        entered, release = threading.Event(), threading.Event()
        original_write, original_mkdir = Path.write_text, Path.mkdir
        writes = []
        def gate():
            entered.set()
            if not release.wait(4): raise AssertionError('stop I/O was not released')
        def write(path, *args, **kwargs):
            if path == self.marker:
                writes.append(path)
                if phase == 'write' and len(writes) == 1: gate()
                if error is not None and not error_after_io: raise error
                value = original_write(path, *args, **kwargs)
                if error is not None: raise error
                return value
            return original_write(path, *args, **kwargs)
        def mkdir(path, *args, **kwargs):
            if path == self.marker.parent and phase == 'mkdir': gate()
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, 'write_text', write), patch.object(Path, 'mkdir', mkdir):
            try: yield entered, release, writes
            finally: release.set()

    def assert_ready(self, future, message):
        done, _ = wait((future,), timeout=.3)
        self.assertEqual(len(done), 1, message)
        return future.result(timeout=2)

    def finish_while_marker_pending(self, pool, message):
        # Finishing releases the live lock before draining the summary to disk.
        # Observe that boundary and responsiveness, not disk latency, while the
        # stop marker writer remains blocked. The caller still joins the real
        # final save after releasing the marker.
        entered = threading.Event()
        drain = self.app._drain_persistence
        def observed_drain():
            entered.set()
            return drain()
        with patch.object(self.app, '_drain_persistence', side_effect=observed_drain):
            finished = pool.submit(self.app._finish_job)
            self.assertTrue(entered.wait(.3), message)
            # Stop writers and a deliberately slow save may occupy every slot
            # in the scenario pool. Probe the live lock on a separate thread.
            controls = ThreadPoolExecutor(max_workers=1)
            try:
                self.assertTrue(self.assert_ready(
                    controls.submit(self.app.touch, SimpleNamespace(last_touch=0.0)), message))
            finally:
                controls.shutdown(wait=False)
            self.assertFalse(self.app.job['busy'])
        return finished

    def start_new_cloud_run(self):
        def child_wait():
            self.worker_waiting.set()
            if not self.worker_exit.wait(4): raise AssertionError('fake child exit was not released')
            return 0
        child = SimpleNamespace(stdout=io.StringIO(''), wait=child_wait, returncode=0)
        with patch.object(studio.StudioController, '_start_payload', return_value=('prepare', [], [])), \
                patch.object(studio, 'start_process', return_value=child):
            self.assertEqual(self.app.start({'action': 'prepare'}), {'started': True, 'action': 'prepare'})
            self.assertTrue(self.worker_waiting.wait(2))

    def test_slow_marker_mkdir_and_write_leave_controls_responsive(self):
        # Retaining the live lock around either filesystem operation breaks this.
        for phase in ('mkdir', 'write'):
            with self.subTest(phase=phase):
                self.arm_old_run()
                with self.delayed_marker(phase) as (entered, release, _), \
                        ThreadPoolExecutor(max_workers=1) as stops, ThreadPoolExecutor(max_workers=3) as controls:
                    pending = stops.submit(self.app.stop)
                    try:
                        self.assertTrue(entered.wait(2))
                        progress = controls.submit(self.app.progress)
                        touch = controls.submit(self.app.touch, SimpleNamespace(last_touch=0.0))
                        advanced = controls.submit(self.app._progress, {'recognized': 7})
                        done, _ = wait((progress, touch, advanced), timeout=.3)
                        self.assertEqual(len(done), 3, 'stop marker I/O retained the live controller lock')
                        self.assertTrue(touch.result())
                        self.assertEqual(self.app.job['recognized'], 7)
                        self.assertFalse(self.app.stop_event.is_set())
                        self.assertTrue(self.app.job['busy'])
                    finally: release.set()
                    self.assertEqual(pending.result(timeout=2), {'stopping': True})
                self.assertTrue(self.app.stop_event.is_set())
                self.assertTrue(self.marker.is_file())
                self.app._finish_job()
                self.assertFalse(self.marker.exists())

    def test_old_stop_cannot_cancel_new_run_and_cleans_its_late_marker(self):
        old_job, old_event = self.app.job, self.app.stop_event
        with self.delayed_marker() as (entered, release, _), ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(self.app.stop)
            try:
                self.assertTrue(entered.wait(2))
                finished = self.finish_while_marker_pending(pool, 'finished job waited for old stop I/O')
                self.start_new_cloud_run()
                new_job, new_event, new_marker = self.app.job, self.app.stop_event, self.app._cancel_marker
                before = deepcopy(new_job)
                release.set()
                self.assertEqual(pending.result(timeout=2), {'stopping': True})
                finished.result(timeout=2)
                self.assertIs(self.app.job, new_job)
                self.assertEqual(new_job, before)
                self.assertFalse(new_event.is_set())
                self.assertFalse(old_event.is_set())
                self.assertFalse(old_job['busy'])
                self.assertFalse(self.marker.exists())
                self.assertFalse(new_marker.exists())
            finally: release.set(); self.finish_worker()

    def test_new_run_stop_does_not_wait_for_old_run_marker_io(self):
        old_event = self.app.stop_event
        with self.delayed_marker() as (entered, release, _), ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(self.app.stop)
            try:
                self.assertTrue(entered.wait(2))
                finished = self.finish_while_marker_pending(pool, 'finish waited for old marker write')
                self.start_new_cloud_run()
                new_event, new_marker = self.app.stop_event, self.app._cancel_marker
                result = self.assert_ready(pool.submit(self.app.stop), 'new stop waited for old run I/O lock')
                self.assertEqual(result, {'stopping': True})
                self.assertTrue(new_event.is_set())
                self.assertTrue(new_marker.is_file())
                release.set()
                self.assertEqual(pending.result(timeout=2), {'stopping': True})
                finished.result(timeout=2)
                self.assertFalse(old_event.is_set())
                self.assertFalse(self.marker.exists())
                self.assertTrue(new_marker.is_file(), 'old cleanup deleted the new run marker')
            finally: release.set(); self.finish_worker()

    def test_duplicate_stop_writers_are_serialized_without_holding_live_lock(self):
        with self.delayed_marker() as (entered, release, writes), ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.app.stop)
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(self.app.stop)
                self.assertTrue(self.assert_ready(pool.submit(self.app.touch, SimpleNamespace(last_touch=0.0)),
                                                 'duplicate stop waited with the live lock held'))
                done, _ = wait((second,), timeout=.1)
                self.assertEqual(len(done), 0, 'duplicate marker writer ran before the first writer settled')
                self.assertEqual(len(writes), 1)
            finally: release.set()
            self.assertEqual(first.result(timeout=2), {'stopping': True})
            self.assertEqual(second.result(timeout=2), {'stopping': True})
        self.assertTrue(self.app.stop_event.is_set())
        self.assertTrue(self.marker.exists())
        self.app._finish_job()
        self.assertFalse(self.marker.exists())

    def test_waiting_duplicate_does_not_recreate_finished_run_marker(self):
        with self.delayed_marker() as (entered, release, writes), ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.app.stop)
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(self.app.stop)
                finished = self.finish_while_marker_pending(pool, 'finish waited for pending stop')
            finally: release.set()
            self.assertEqual(first.result(timeout=2), {'stopping': True})
            self.assertEqual(second.result(timeout=2), {'stopping': True})
            finished.result(timeout=2)
        self.assertEqual(len(writes), 1, 'stale duplicate wrote after the final marker cleanup')
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.app.stop_event.is_set())
        self.assertFalse(self.app.job['busy'])

    def test_failed_write_keeps_event_unset_and_natural_completion_prepared(self):
        primary = PermissionError('synthetic marker unavailable')
        with self.delayed_marker(error=primary) as (entered, release, _), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.app.stop)
            self.assertTrue(entered.wait(2)); release.set()
            with self.assertRaises(PermissionError) as caught: pending.result(timeout=2)
        self.assertIs(caught.exception, primary)
        self.assertFalse(self.app.stop_event.is_set())
        self.assertTrue(self.app.job['busy'])
        child = SimpleNamespace(stdout=io.StringIO(''), wait=lambda: 0, returncode=0)
        with patch.object(studio, 'start_process', return_value=child):
            self.app._execute('prepare', [])
        self.assertEqual(self.app.job['status'], 'prepared')
        self.assertFalse(self.app.job['busy'])

    def test_failed_duplicate_does_not_retract_successful_stop(self):
        self.assertEqual(self.app.stop(), {'stopping': True})
        original = Path.write_text
        def failed(path, *args, **kwargs):
            if path == self.marker: raise PermissionError('synthetic later failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'write_text', failed), self.assertRaises(PermissionError):
            self.app.stop()
        self.assertTrue(self.app.stop_event.is_set())
        self.assertTrue(self.marker.is_file())
        self.assertTrue(self.app.job['busy'])

    def test_stale_cleanup_failure_preserves_primary_write_error_and_new_run(self):
        primary = PermissionError('synthetic primary marker write failure')
        original_unlink = Path.unlink
        cleanup_fails = threading.Event()
        def unlink(path, *args, **kwargs):
            if path == self.marker and cleanup_fails.is_set():
                raise OSError('synthetic stale marker cleanup failure')
            return original_unlink(path, *args, **kwargs)
        with self.delayed_marker(error=primary, error_after_io=True) as (entered, release, _), \
                patch.object(Path, 'unlink', unlink), ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.app.stop)
            try:
                self.assertTrue(entered.wait(2))
                finished = self.finish_while_marker_pending(pool, 'finish waited for failed stop write')
                self.start_new_cloud_run()
                new_event, before = self.app.stop_event, deepcopy(self.app.job)
                cleanup_fails.set(); release.set()
                with self.assertRaises(PermissionError) as caught: pending.result(timeout=2)
                finished.result(timeout=2)
                self.assertIs(caught.exception, primary)
                self.assertFalse(new_event.is_set())
                self.assertEqual(self.app.job, before)
                self.assertTrue(self.marker.is_file())
            finally: cleanup_fails.clear(); release.set(); self.finish_worker()


if __name__ == '__main__':
    unittest.main()
