"""Actual media-call integration must bind children before crash readiness."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


PARENT = r'''
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
from subtitle_pipeline import local_process, runner
mode, marker = sys.argv[1:]
module = runner if mode == 'runner' else local_process
original = module.owned_process
@contextmanager
def observed(child):
    with original(child):
        ready = Path(marker).with_suffix('.pending')
        ready.write_text(json.dumps({'pid': child.pid}), encoding='utf-8')
        ready.replace(marker)
        yield child
module.owned_process = observed
args = [sys.executable, '-c', 'import time; time.sleep(20)']
if mode == 'runner':
    runner.run_process(args, Path(marker).with_suffix('.log'), threading.Event())
else:
    local_process.capture_process(args, timeout=25)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows process lifetime binding')
class OwnedMediaProcessTests(unittest.TestCase):
    def setUp(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def verify_hard_exit(self, mode):
        with tempfile.TemporaryDirectory(prefix='owned-media-test-') as directory:
            marker = Path(directory) / 'ready.json'
            parent = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', PARENT, mode, str(marker)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            handle = None
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                if not marker.exists():
                    if parent.poll() is None:
                        parent.kill()
                    _out, diagnostic = parent.communicate(timeout=3)
                    self.fail('Media caller did not establish lifetime ownership: ' + diagnostic.decode('utf-8'))
                # A marker is written inside the attached context. Keep the
                # exact child handle so PID reuse cannot affect probe cleanup.
                data = json.loads(marker.read_text(encoding='utf-8'))
                handle = self.kernel.OpenProcess(0x100000 | 0x1, False, data['pid'])
                self.assertTrue(handle, ctypes.WinError(ctypes.get_last_error()))
                self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 258)
                parent.kill()
                parent.wait(timeout=3)
                self.assertEqual(self.kernel.WaitForSingleObject(handle, 2000), 0,
                                 'Bound child survived forced exit of its media caller')
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.communicate(timeout=3)
                if handle:
                    try:
                        if self.kernel.WaitForSingleObject(handle, 0) == 258:
                            self.kernel.TerminateProcess(handle, 1)
                            self.kernel.WaitForSingleObject(handle, 3000)
                    finally:
                        self.kernel.CloseHandle(handle)

    def test_runner_child_is_reaped_after_parent_hard_exit(self):
        self.verify_hard_exit('runner')

    def test_capture_child_is_reaped_after_parent_hard_exit(self):
        self.verify_hard_exit('capture')

    def test_media_cleanup_error_cannot_replace_cancelled_result(self):
        from subtitle_pipeline import local_process, runner
        children = []
        original = subprocess.Popen
        denied = PermissionError('injected termination access error')
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            child.terminate = lambda: (_ for _ in ()).throw(denied)
            children.append(child)
            return child
        class StopAfterStart:
            def __init__(self): self.checks = 0
            def is_set(self):
                self.checks += 1
                return self.checks > 1
            def wait(self, timeout): return True
        with tempfile.TemporaryDirectory(prefix='owned-cancel-test-') as directory:
            try:
                for mode in ('runner', 'capture'):
                    with self.subTest(mode=mode), patch.object(runner.subprocess, 'Popen', side_effect=start):
                        with self.assertRaises(runner.Cancelled):
                            args = [sys.executable, '-c', 'import time; time.sleep(20)']
                            if mode == 'runner':
                                runner.run_process(args, Path(directory) / 'cancel.log', StopAfterStart())
                            else:
                                local_process.capture_process(args, stop=StopAfterStart(), timeout=3)
                        self.assertIsNotNone(children[-1].returncode)
                self.assertEqual(len(children), 2)
            finally:
                for child in children:
                    if child.poll() is None: child.kill()
                    child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
