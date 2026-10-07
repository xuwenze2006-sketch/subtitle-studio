import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class LocalCaptureTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.local_process'),
                             'cancellable local process capture is not implemented')
        self.module = importlib.import_module('subtitle_pipeline.local_process')

    def command(self, program, *args):
        return [sys.executable, '-X', 'utf8', '-c', program, *map(str, args)]

    def track_children(self):
        children = []
        original = subprocess.Popen
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        guard = patch.object(self.module.subprocess, 'Popen', side_effect=start)
        guard.start()
        self.addCleanup(guard.stop)
        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
        self.addCleanup(cleanup)
        return children

    def assert_reaped(self, children):
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(children[0].wait(timeout=0.1), children[0].returncode)
        if os.name != 'nt':
            with self.assertRaises(ChildProcessError):
                os.waitpid(children[0].pid, os.WNOHANG)

    def test_utf8_output_closed_stdin_and_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.module.capture_process(self.command(
                "import os,sys; print('字幕进度'); print(os.getcwd()); "
                "print('stdin='+repr(sys.stdin.read())); print('音轨检查',file=sys.stderr)"),
                cwd=Path(directory), timeout=3)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ['字幕进度', directory, "stdin=''"])
        self.assertEqual(result.stderr.strip(), '音轨检查')

    def test_large_stdout_and_stderr_do_not_deadlock(self):
        result = self.module.capture_process(self.command(
            "import sys; sys.stdout.write('a'*100000); sys.stdout.flush(); "
            "sys.stderr.write('b'*100000); sys.stderr.flush()"),
            timeout=3, max_output_bytes=256000)
        self.assertEqual(result.stdout, 'a' * 100000)
        self.assertEqual(result.stderr, 'b' * 100000)

    def test_nonzero_exit_preserves_return_code_and_decoded_diagnostics(self):
        children = self.track_children()
        args = self.command("import sys; print('标准输出'); print('诊断错误',file=sys.stderr); sys.exit(7)")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.module.capture_process(args, timeout=3)
        self.assertEqual(raised.exception.returncode, 7)
        self.assertEqual(raised.exception.stdout.strip(), '标准输出')
        self.assertEqual(raised.exception.stderr.strip(), '诊断错误')
        self.assert_reaped(children)

    def test_timeout_terminates_and_reaps_child(self):
        children = self.track_children()
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            self.module.capture_process(self.command(
                "import sys,time; print('已开始',flush=True); "
                "print('等待',file=sys.stderr,flush=True); time.sleep(10)"), timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsInstance(raised.exception.stdout, str)
        self.assertIsInstance(raised.exception.stderr, str)
        self.assert_reaped(children)

    def test_stop_after_child_starts_terminates_and_reaps_child(self):
        from subtitle_pipeline.runner import Cancelled
        children = self.track_children()
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready.txt'
            watcher_error = []
            def cancel_when_ready():
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not ready.exists():
                    watcher_error.append('child did not start')
                stop.set()
            watcher = threading.Thread(target=cancel_when_ready)
            watcher.start()
            try:
                started = time.monotonic()
                with self.assertRaises(Cancelled):
                    self.module.capture_process(self.command(
                        "from pathlib import Path; import sys,time; "
                        "Path(sys.argv[1]).write_text('ready'); time.sleep(10)", ready),
                        stop=stop, timeout=5)
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(watcher_error, [])
            finally:
                watcher.join(timeout=4)
        self.assert_reaped(children)

    def test_already_stopped_does_not_start_a_child(self):
        from subtitle_pipeline.runner import Cancelled
        stop = threading.Event()
        stop.set()
        children = self.track_children()
        with self.assertRaises(Cancelled):
            self.module.capture_process(self.command("raise SystemExit(0)"), stop=stop)
        self.assertEqual(children, [])

    def test_child_that_ignores_terminate_is_killed_and_reaped_on_timeout(self):
        children = []
        original = subprocess.Popen
        def ignore_terminate(*args, **kwargs):
            child = original(*args, **kwargs)
            # Simulate the OS accepting a graceful termination request while
            # this real child remains alive, exercising the forced-kill path.
            child.terminate = lambda: None
            children.append(child)
            return child
        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
        self.addCleanup(cleanup)
        with patch.object(self.module.subprocess, 'Popen', side_effect=ignore_terminate):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                self.module.capture_process(self.command('import time; time.sleep(10)'), timeout=0.2)
            self.assertLess(time.monotonic() - started, 3)
        self.assert_reaped(children)

    def test_output_limit_counts_both_streams_and_reaps_running_child(self):
        children = self.track_children()
        started = time.monotonic()
        with self.assertRaises(self.module.OutputLimitExceeded):
            self.module.capture_process(self.command(
                "import sys,time; sys.stdout.write('a'*40000); sys.stdout.flush(); "
                "sys.stderr.write('b'*40000); sys.stderr.flush(); time.sleep(10)"),
                timeout=5, max_output_bytes=65536)
        self.assertLess(time.monotonic() - started, 3)
        self.assert_reaped(children)

    def test_output_limit_also_checks_child_that_exited_between_polls(self):
        with self.assertRaises(self.module.OutputLimitExceeded):
            self.module.capture_process(self.command("import sys; sys.stdout.write('x'*65537)"),
                                        timeout=3, max_output_bytes=65536)

    def test_invalid_limits_do_not_start_children(self):
        children = self.track_children()
        for options in ({'timeout': 0}, {'timeout': float('nan')}, {'timeout': float('inf')},
                        {'max_output_bytes': 0}, {'max_output_bytes': True},
                        {'max_output_bytes': 1.5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.module.capture_process(self.command(''), **options)
        self.assertEqual(children, [])


if __name__ == '__main__':
    unittest.main()
