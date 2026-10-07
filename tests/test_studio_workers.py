"""Real Windows Studio worker ownership, independent of browser activity."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


WORKER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from subtitle_pipeline.windows_job import owned_process
mode, directory = sys.argv[1:]
directory = Path(directory)
def ready(nested_pid=None):
    pending = directory / 'worker.writing'
    pending.write_text(json.dumps({'worker_pid': os.getpid(), 'nested_pid': nested_pid}), encoding='utf-8')
    pending.replace(directory / 'worker.json')
def hold_until_released():
    deadline = time.monotonic() + 12
    while not (directory / 'release').exists() and time.monotonic() < deadline:
        time.sleep(.01)
if mode == 'nested':
    nested = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    with owned_process(nested):
        ready(nested.pid)
        hold_until_released()
else:
    ready()
    hold_until_released()
'''


PARENT = r'''
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from subtitle_pipeline import studio
mode, directory, worker_code = sys.argv[1:]
directory = Path(directory)
def forbidden(*args, **kwargs):
    raise AssertionError('Network and credentials are outside this test')
socket.create_connection = forbidden
socket.socket.connect = forbidden
studio.account_environment = forbidden
studio.encrypted_names = forbidden
original = studio.owned_process
@contextmanager
def observed(child):
    # Keep the actual owner and publish readiness only after successful binding.
    with original(child) as owned:
        deadline = time.monotonic() + 5
        while not (directory / 'worker.json').exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('Worker did not become ready')
            time.sleep(.01)
        data = json.loads((directory / 'worker.json').read_text(encoding='utf-8'))
        pending = directory / 'ready.writing'
        pending.write_text(json.dumps(data), encoding='utf-8')
        pending.replace(directory / 'ready.json')
        yield owned
studio.owned_process = observed
def start(_payload, **_kwargs):
    return subprocess.Popen([sys.executable, '-X', 'utf8', '-c', worker_code, mode, str(directory)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
studio.start_process = start
campaign = directory / 'campaign'
campaign.mkdir()
controller = studio.StudioController(campaign=str(campaign), state_path=directory / 'state.json')
controller.job.update(busy=True, action='prepare', status='starting')
controller._cancel_marker = directory / 'stop'
controller._execute('prepare', ['synthetic-command-never-executed'])
'''


@unittest.skipUnless(os.name == 'nt', 'Windows worker lifetime ownership')
class StudioWorkerTests(unittest.TestCase):
    def setUp(self):
        from subtitle_pipeline import studio
        self.studio = studio
        self.root = Path(__file__).resolve().parents[1]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def reap_handle(self, handle):
        if self.kernel.WaitForSingleObject(handle, 0) == 258:
            self.assertTrue(self.kernel.TerminateProcess(handle, 92))
        self.assertEqual(self.kernel.WaitForSingleObject(handle, 3000), 0)

    def reap_popen(self, child):
        self.reap_handle(int(child._handle))
        child.returncode = None
        child.wait(timeout=0)
        if child.stdout is not None:
            child.stdout.close()

    def assert_hard_exit(self, mode):
        with tempfile.TemporaryDirectory(prefix='studio-worker-owner-') as directory:
            marker = Path(directory) / 'ready.json'
            parent = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', PARENT,
                mode, directory, WORKER], cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            handles = {}
            def hold_worker_handles():
                record = Path(directory) / 'worker.json'
                if not record.is_file():
                    return
                data = json.loads(record.read_text(encoding='utf-8'))
                for label in ('worker', 'nested'):
                    pid = data[label + '_pid']
                    if pid is None or label in handles:
                        continue
                    handle = self.kernel.OpenProcess(0x100000 | 0x1, False, pid)
                    self.assertTrue(handle, ctypes.WinError(ctypes.get_last_error()))
                    handles[label] = handle
            try:
                deadline = time.monotonic() + 7
                while not marker.is_file() and parent.poll() is None and time.monotonic() < deadline:
                    hold_worker_handles()
                    time.sleep(.01)
                if not marker.is_file():
                    hold_worker_handles()
                    (Path(directory) / 'release').touch()
                    self.reap_handle(int(parent._handle))
                    _output, error = parent.communicate(timeout=3)
                    self.fail('Studio did not bind its worker: ' + error.decode('utf-8'))
                hold_worker_handles()
                self.assertEqual(set(handles), {'worker', 'nested'} if mode == 'nested' else {'worker'})
                for handle in handles.values():
                    self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 258)
                parent.kill()
                parent.wait(timeout=3)
                for handle in handles.values():
                    self.assertEqual(self.kernel.WaitForSingleObject(handle, 2000), 0,
                                     'A bound worker subtree survived forced Studio exit')
            finally:
                # Even a failure before readiness lets the synthetic worker
                # finish cooperatively; only retained handles are force-reaped.
                (Path(directory) / 'release').touch()
                self.reap_handle(int(parent._handle))
                parent.communicate(timeout=3)
                for handle in handles.values():
                    try:
                        self.reap_handle(handle)
                    finally:
                        self.kernel.CloseHandle(handle)

    def test_forced_studio_exit_stops_silent_worker(self):
        self.assert_hard_exit('direct')

    def test_forced_studio_exit_stops_worker_and_owned_nested_child(self):
        self.assert_hard_exit('nested')

    def make_controller(self, directory):
        folder = Path(directory)
        campaign = folder / 'campaign'
        campaign.mkdir()
        (campaign / 'campaign.json').write_text(json.dumps({'status': 'prepared'}), encoding='utf-8')
        app = self.studio.StudioController(campaign=str(campaign), state_path=folder / 'state.json')
        app.job.update(busy=True, action='prepare', status='starting')
        app._cancel_marker = folder / 'stop'
        return app

    def test_worker_finishes_without_browser_polling_and_keeps_host_busy(self):
        children = []
        launched = threading.Event()
        release = None
        code = ('from pathlib import Path; import sys,time; '
                'release=Path(sys.argv[1]); deadline=time.monotonic()+10\n'
                'while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
                'assert release.exists(), "Test did not release worker"')
        def start(_payload, **_kwargs):
            child = subprocess.Popen([sys.executable, '-c', code, str(release)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(child)
            launched.set()
            return child
        with tempfile.TemporaryDirectory(prefix='studio-worker-no-browser-') as directory, \
                patch.object(self.studio, 'account_environment', return_value={}), \
                patch.object(self.studio, 'encrypted_names', return_value=set()), \
                patch.object(self.studio, 'start_process', side_effect=start):
            release = Path(directory) / 'release'
            app = self.make_controller(directory)
            thread = threading.Thread(target=app._execute, args=('prepare', []))
            app.worker = thread
            thread.start()
            try:
                self.assertTrue(launched.wait(2))
                self.assertTrue(app.job['busy'])
                self.assertFalse(app.idle_ready())
                self.assertFalse(app.stop_event.is_set())
                # No HTTP/browser request, heartbeat or explicit stop is sent.
                release.touch()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertFalse(app.job['busy'])
                self.assertTrue(app.idle_ready())
                self.assertFalse(app.stop_event.is_set())
                self.assertEqual(children[0].returncode, 0)
                self.assertEqual(app.job['status'], 'prepared')
                self.assertTrue(children[0].stdout.closed)
            finally:
                release.touch()
                for child in children:
                    self.reap_popen(child)
                thread.join(3)

    def test_job_setup_failure_reaps_real_worker_and_closes_pipe(self):
        from subtitle_pipeline import windows_job
        children = []
        def start(_payload, **_kwargs):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory(prefix='studio-worker-setup-fail-') as directory, \
                patch.object(self.studio, 'account_environment', return_value={}), \
                patch.object(self.studio, 'encrypted_names', return_value=set()), \
                patch.object(self.studio, 'start_process', side_effect=start), \
                patch.object(windows_job, '_create_job', side_effect=OSError('injected Job creation failure')):
            app = self.make_controller(directory)
            try:
                app._execute('prepare', [])
                self.assertEqual(len(children), 1)
                self.assertEqual(self.kernel.WaitForSingleObject(int(children[0]._handle), 0), 0)
                self.assertIsNotNone(children[0].returncode)
                self.assertTrue(children[0].stdout.closed)
                self.assertFalse(app.job['busy'])
                self.assertTrue(app.idle_ready())
                self.assertEqual(app.job['status'], 'needs_attention')
            finally:
                for child in children:
                    self.reap_popen(child)


if __name__ == '__main__':
    unittest.main()
