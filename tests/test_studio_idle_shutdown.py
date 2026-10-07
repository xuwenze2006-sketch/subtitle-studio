"""Atomic idle-exit admission checks with isolated projects and worker doubles."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import studio


class StudioIdleShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-idle-shutdown-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.wav'
        self.source.write_bytes(b'isolated source')
        self.app = studio.StudioController(source=str(self.source),
            campaign=str(self.root / 'project'), state_path=self.root / 'studio.json')
        self.server = SimpleNamespace(last_touch=0.0)
        for guard in (patch.object(studio, 'account_environment', return_value={}),
                      patch.object(studio, 'encrypted_names', return_value=set()),
                      patch.object(studio, 'engine_available', return_value=True),
                      patch.object(studio.time, 'monotonic', return_value=1000.0)):
            guard.start()
            self.addCleanup(guard.stop)

    def test_actual_idle_loop_rejects_new_run_before_shutdown_starts(self):
        # Execute main's real nested loop; the shutdown boundary attempts a
        # queued request after the idle decision but before the host exits.
        module = ast.parse(Path(studio.__file__).read_text(encoding='utf-8'))
        main = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        loop = next(node for node in main.body if isinstance(node, ast.FunctionDef) and node.name == 'idle_shutdown')
        outcomes = []
        def shutdown():
            try:
                self.app.start({'action': 'local'})
                outcomes.append('admitted')
            except RuntimeError as error:
                outcomes.append(str(error))
        self.server.shutdown = shutdown
        environment = {'controller': self.app, 'server': self.server,
            'time': SimpleNamespace(sleep=lambda _: None, monotonic=lambda: 1000.0)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                     '<actual-idle-loop>', 'exec'), environment)
        with patch.object(studio.threading, 'Thread', return_value=Mock()):
            environment['idle_shutdown']()
        self.assertEqual(len(outcomes), 1)
        self.assertIn('重新打开', outcomes[0])
        self.assertFalse(self.app.job['busy'])
        self.assertFalse(self.app.state_path.exists())

    def test_admitted_task_wins_controller_lock_and_blocks_shutdown(self):
        state = self.app.state()
        entered, release, worker_exit = (threading.Event() for _ in range(3))
        def validate():
            entered.set()
            if not release.wait(3): raise AssertionError('admission not released')
            return state
        def run(*args, **kwargs):
            if not worker_exit.wait(3): raise AssertionError('worker not released')
            return {'status': 'complete'}
        with patch.object(self.app, 'state', side_effect=validate), \
                patch.object(studio, 'run_pipeline', side_effect=run), ThreadPoolExecutor(max_workers=2) as pool:
            started = pool.submit(self.app.start, {'action': 'local'})
            try:
                self.assertTrue(entered.wait(2))
                shutdown = pool.submit(self.app.claim_idle_shutdown, self.server)
                release.set()
                self.assertTrue(started.result(timeout=2)['started'])
                self.assertFalse(shutdown.result(timeout=2))
                self.assertTrue(self.app.job['busy'])
                self.assertTrue(self.app.touch(self.server))
            finally:
                release.set()
                worker_exit.set()
                if self.app.worker is not None: self.app.worker.join(3)

    def test_shutdown_wins_controller_lock_and_rejects_already_queued_start(self):
        queued = threading.Event()
        def start():
            queued.set()
            return self.app.start({'action': 'local'})
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.app.lock:
                started = pool.submit(start)
                self.assertTrue(queued.wait(2))
                self.assertTrue(self.app.claim_idle_shutdown(self.server))
            with self.assertRaisesRegex(RuntimeError, '重新打开'):
                started.result(timeout=2)
        self.assertFalse(self.app.job['busy'])
        self.assertFalse(self.app.touch(self.server))
        self.assertEqual(self.server.last_touch, 0.0)
        self.assertIsNone(self.app.worker)

    def test_recent_authenticated_touch_prevents_idle_shutdown(self):
        self.assertTrue(self.app.touch(self.server))
        self.assertEqual(self.server.last_touch, 1000.0)
        self.assertFalse(self.app.claim_idle_shutdown(self.server))

    def test_pending_final_save_prevents_shutdown_until_drained(self):
        with self.app.lock:
            self.app._queue_persist_locked()
        self.assertFalse(self.app.claim_idle_shutdown(self.server))
        self.app._drain_persistence()
        self.assertTrue(self.app.claim_idle_shutdown(self.server))


if __name__ == '__main__': unittest.main()
