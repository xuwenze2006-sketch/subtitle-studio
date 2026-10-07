"""Real Windows hard-exit recovery using only isolated, fake paid requests."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch
import venv


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = {'output': 'local fixture response', 'usage': {'cost': 0.5}}
STAGES = ('reserved', 'submitting', 'sent', 'raw_only', 'received', 'success')

# Hooks only stop at a precise boundary; every ledger mutation, file write,
# lock acquisition and retry below uses the real product implementation.
WORKER = r'''
import json, os, socket, sys, time
from pathlib import Path

def no_network(*args, **kwargs):
    raise AssertionError('This crash fixture must never access the network')
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network

from subtitle_pipeline import cloud_budget as cloud

root, mode, stage, name = Path(sys.argv[1]), *sys.argv[2:]
raw = root / 'raw.json'
payload = {'output': 'local fixture response', 'usage': {'cost': 0.5}}
body = json.dumps(payload, sort_keys=True).encode('utf-8')

def durable_json(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('wb') as output:
        output.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)

def pause(boundary):
    if mode != 'crash' or stage != boundary:
        return
    durable_json(root / 'ready.json', {'pid': os.getpid(), 'stage': boundary})
    # The supervisor kills this exact Popen handle. A timeout is a fixture
    # failure, never permission to continue a paid-request simulation.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(0.02)
    raise TimeoutError('Crash-stage supervisor did not terminate its worker')

class BeforeSend:
    def is_set(self):
        ledger_path = root / 'budget.json'
        if ledger_path.exists():
            record = json.loads(ledger_path.read_bytes())['requests']['stable-request']
            if record['status'] == 'reserved':
                pause('reserved')
        return False

def send():
    pause('submitting')
    # This is the only fake send. The counter is durable before acknowledging
    # this stage, so a later missing response cannot erase evidence of a send.
    with (root / 'sends.jsonl').open('ab') as output:
        output.write((json.dumps({'pid': os.getpid(), 'worker': name}) + '\n').encode())
        output.flush()
        os.fsync(output.fileno())
    pause('sent')
    return cloud.HttpResponse(200, {}, body)

ledger = cloud.BudgetLedger(root / 'budget.json')
if mode == 'crash':
    original_write = cloud._write_bytes
    def write_then_pause(path, content, replace=True):
        original_write(path, content, replace=replace)
        if Path(path) == raw:
            pause('raw_only')
    cloud._write_bytes = write_then_pause
    original_finish = ledger._finish_local
    def finish_after_pause(request_id, record, actual_cost):
        pause('received')
        return original_finish(request_id, record, actual_cost)
    ledger._finish_local = finish_after_pause

try:
    value = ledger.execute('stable-request', 'local-fixture', 2, raw, send,
                           stop_event=BeforeSend(), actual_cost=lambda data: data['usage']['cost'])
    pause('success')
    # A caller's local conversion can be repeated from the durable response.
    durable_json(root / (name + '-converted.json'), {'text': value['output']})
    outcome = {'kind': 'success', 'payload': value}
except cloud.CloudRequestError as error:
    outcome = {'kind': type(error).__name__, 'message': str(error)}
outcome.update(pid=os.getpid(), summary=ledger.summary())
durable_json(root / (name + '-result.json'), outcome)
'''


def _kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    return kernel


def _wait_native(child, milliseconds):
    """Do not use Popen's possibly cached returncode as an exit barrier."""
    kernel = _kernel()
    status = kernel.WaitForSingleObject(int(child._handle), milliseconds)
    if status == 258:
        return False
    if status != 0:
        raise ctypes.WinError(ctypes.get_last_error())
    code = wintypes.DWORD()
    if not kernel.GetExitCodeProcess(int(child._handle), ctypes.byref(code)):
        raise ctypes.WinError(ctypes.get_last_error())
    child.returncode = code.value
    return True


def _kill_and_reap(child):
    if not _wait_native(child, 0):
        if not _kernel().TerminateProcess(int(child._handle), 97):
            error = ctypes.WinError(ctypes.get_last_error())
            if not _wait_native(child, 0):
                raise error
        if not _wait_native(child, 5000):
            raise TimeoutError('Owned fixture process did not become signaled')


def _sends(root):
    path = root / 'sends.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def _snapshot(root):
    raw = root / 'raw.json'
    return {
        'ledger': json.loads((root / 'budget.json').read_bytes()),
        'sends': _sends(root),
        'raw_sha256': hashlib.sha256(raw.read_bytes()).hexdigest() if raw.exists() else None,
        'raw_payload': json.loads(raw.read_bytes()) if raw.exists() else None,
    }


def _cleanup_fixture_processes(children, streams):
    """Attempt all cleanup without losing the assertion that triggered it."""
    primary = sys.exc_info()[1]
    errors, confirmed = [], []
    for index, child in enumerate(children):
        try:
            _kill_and_reap(child)
        except BaseException as error:
            errors.append((f'process[{index}] reap', error))
        else:
            # Only this invocation's successful native wait authorizes closing
            # the handle. A cached returncode on a failed reap proves nothing.
            confirmed.append((index, child))
    for index, stream in enumerate(streams):
        try:
            stream.close()
        except BaseException as error:
            errors.append((f'stream[{index}] close', error))
    for index, child in confirmed:
        try:
            child._handle.Close()
        except BaseException as error:
            errors.append((f'process[{index}] handle close', error))
    if errors:
        target = primary if primary is not None else errors[0][1]
        for operation, error in errors:
            if error is not target:
                target.add_note(f'Fixture cleanup failed at {operation}: {type(error).__name__}: {error}')
        if primary is None:
            raise target


def _spawn_fixture_python(program, *arguments, **options):
    executable = sys.executable
    base = getattr(sys, '_base_executable', None) or executable
    if os.name == 'nt' and os.path.normcase(base) != os.path.normcase(executable):
        # Match multiprocessing.popen_spawn_win32: own the interpreter rather
        # than the venv redirector, while preserving the venv's import paths.
        environment = options.get('env')
        options['env'] = dict(os.environ if environment is None else environment)
        options['env']['__PYVENV_LAUNCHER__'] = executable
        executable = base
    return subprocess.Popen([executable, '-c', program, *map(str, arguments)], **options)


def run_crash_case(root, stage):
    """Use only the supplied, newly created fixture directory and held handles."""
    if stage not in STAGES or any(root.iterdir()):
        raise ValueError('A known stage and an empty fixture directory are required')
    children, streams = [], []
    def spawn(mode, name):
        stream = (root / (name + '.log')).open('wb')
        streams.append(stream)
        child = _spawn_fixture_python(WORKER, root, mode, stage, name,
                                      cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                                      stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(child)
        return child
    def retry(name):
        child = spawn('retry', name)
        if not _wait_native(child, 8000):
            raise TimeoutError(f'{name} could not acquire/reject the real process locks')
        if child.returncode != 0:
            raise AssertionError((root / (name + '.log')).read_text(encoding='utf-8'))
        return json.loads((root / (name + '-result.json')).read_bytes())
    try:
        owner = spawn('crash', 'owner')
        deadline = time.monotonic() + 8
        while not (root / 'ready.json').exists():
            if _wait_native(owner, 0):
                raise AssertionError('Fixture exited before readiness: ' + (root / 'owner.log').read_text(encoding='utf-8'))
            if time.monotonic() >= deadline:
                raise TimeoutError(f'No readiness at {stage}')
            time.sleep(0.01)
        ready = json.loads((root / 'ready.json').read_bytes())
        if ready != {'pid': owner.pid, 'stage': stage} or _wait_native(owner, 0):
            raise AssertionError('Ready file did not identify the live, owned process')
        before = _snapshot(root)
        # execute() still owns the request lock at all earlier stages. The
        # success pause deliberately occurs outside execute(), after release.
        contender = retry('contender') if stage != 'success' else None
        live = _snapshot(root)
        _kill_and_reap(owner)
        owner_exit_code = owner.returncode
        resumed = retry('resumed')
        repeated = retry('repeated')
        after = _snapshot(root)
        converted = root / 'resumed-converted.json'
        return dict(stage=stage, ready=ready, owner_exit_code=owner_exit_code,
                    before=before, live=live, contender=contender,
                    resumed=resumed, repeated=repeated, after=after,
                    owner_converted=(root / 'owner-converted.json').exists(),
                    resumed_converted=json.loads(converted.read_bytes()) if converted.exists() else None)
    finally:
        # No PID lookup or taskkill: these handles refer only to our own Popen
        # children even if Windows later reuses their numeric PIDs.
        _cleanup_fixture_processes(children, streams)


class CloudBudgetCrashCleanupTests(unittest.TestCase):
    def resources(self):
        children = [Mock(name='first-child', returncode=0), Mock(name='second-child', returncode=0)]
        streams = [Mock(name='first-log'), Mock(name='second-log')]
        return children, streams

    def test_cleanup_attempts_every_resource_and_raises_first_failure_with_later_notes(self):
        children, streams = self.resources()
        first_error = PermissionError('first process native wait failed')
        streams[0].close.side_effect = OSError('first log close failed')
        children[1]._handle.Close.side_effect = OSError('second handle close failed')
        with patch(__name__ + '._kill_and_reap', side_effect=[first_error, None]) as reap:
            with self.assertRaises(OSError) as raised:
                _cleanup_fixture_processes(children, streams)
        self.assertIs(raised.exception, first_error)
        self.assertEqual(reap.call_args_list, [call(children[0]), call(children[1])])
        for stream in streams:
            stream.close.assert_called_once_with()
        children[0]._handle.Close.assert_not_called()
        children[1]._handle.Close.assert_called_once_with()
        notes = '\n'.join(first_error.__notes__)
        self.assertIn('first log close failed', notes)
        self.assertIn('second handle close failed', notes)

    def test_cleanup_preserves_primary_exception_identity_and_adds_all_failures(self):
        children, streams = self.resources()
        primary = AssertionError('original ledger assertion failed')
        cleanup_error = PermissionError('native wait failed')
        streams[0].close.side_effect = OSError('log close failed')
        children[1]._handle.Close.side_effect = OSError('handle close failed')
        with patch(__name__ + '._kill_and_reap', side_effect=[cleanup_error, None]) as reap:
            try:
                try:
                    raise primary
                finally:
                    _cleanup_fixture_processes(children, streams)
            except BaseException as caught:
                self.assertIs(caught, primary)
            else:
                self.fail('The primary test failure was swallowed')
        self.assertEqual(reap.call_args_list, [call(children[0]), call(children[1])])
        for stream in streams:
            stream.close.assert_called_once_with()
        notes = '\n'.join(primary.__notes__)
        for message in ('native wait failed', 'log close failed', 'handle close failed'):
            self.assertIn(message, notes)
        children[0]._handle.Close.assert_not_called()
        children[1]._handle.Close.assert_called_once_with()

    def test_cached_zero_after_failed_native_reap_does_not_authorize_handle_close(self):
        children, streams = self.resources()
        first_error = TimeoutError('native process is still unconfirmed')
        with patch(__name__ + '._kill_and_reap', side_effect=[first_error, None]):
            with self.assertRaises(TimeoutError) as raised:
                _cleanup_fixture_processes(children, streams)
        self.assertIs(raised.exception, first_error)
        children[0]._handle.Close.assert_not_called()
        children[1]._handle.Close.assert_called_once_with()


@unittest.skipUnless(os.name == 'nt', 'Real Windows process termination and lock recovery')
class CloudBudgetCrashTests(unittest.TestCase):
    def test_venv_worker_handle_identifies_python_and_keeps_venv_imports(self):
        with tempfile.TemporaryDirectory(prefix='budget-crash-venv-') as temporary:
            environment = Path(temporary) / 'venv'
            venv.EnvBuilder(with_pip=False).create(environment)
            executable = environment / 'Scripts' / 'python.exe'
            module = environment / 'Lib' / 'site-packages' / 'crash_fixture_probe.py'
            module.write_text("MARKER = 'only in this temporary venv'\n", encoding='utf-8')
            supervisor = r'''
import json, subprocess
from tests.test_cloud_budget_crash import ROOT, _spawn_fixture_python
program = "import json, os, sys, crash_fixture_probe; print(json.dumps({'pid': os.getpid(), 'prefix': sys.prefix, 'executable': sys.executable, 'marker': crash_fixture_probe.MARKER}))"
with _spawn_fixture_python(program, cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NO_WINDOW) as worker:
    output, error = worker.communicate(timeout=10)
    if worker.returncode:
        raise AssertionError(error.decode('utf-8', errors='replace'))
    print(json.dumps({'handle_pid': worker.pid, 'worker': json.loads(output)}))
'''
            result = subprocess.run([str(executable), '-c', supervisor], cwd=ROOT,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence['handle_pid'], evidence['worker']['pid'])
            self.assertEqual(Path(evidence['worker']['prefix']).resolve(), environment.resolve())
            self.assertEqual(Path(evidence['worker']['executable']).resolve(), executable.resolve())
            self.assertEqual(evidence['worker']['marker'], 'only in this temporary venv')

    def check_stage(self, stage, before_status, initial_sends, outcome):
        with tempfile.TemporaryDirectory(prefix='budget-crash-fixture-') as temporary:
            evidence = run_crash_case(Path(temporary), stage)
        report_directory = getattr(self, 'report_directory', None)
        if report_directory is not None:
            (report_directory / (stage + '.json')).write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertEqual(evidence['owner_exit_code'], 97)
        self.assertFalse(evidence['owner_converted'])
        before = evidence['before']
        before_record = before['ledger']['requests']['stable-request']
        self.assertEqual(before_record['status'], before_status)
        self.assertEqual(len(before_record['attempts']), 0 if stage == 'reserved' else 1)
        self.assertEqual(len(before['sends']), initial_sends)
        self.assertEqual(evidence['live'], before, 'A competing process changed the live request')
        if stage != 'success':
            self.assertEqual(evidence['contender']['kind'], 'SubmissionUnknown')
            self.assertEqual(evidence['contender']['summary']['reserved_cny'], 2)
            self.assertEqual(evidence['contender']['summary']['spent_cny'], 0)
        if stage in ('raw_only', 'received', 'success'):
            self.assertEqual(before['raw_payload'], PAYLOAD)
            self.assertEqual(before['raw_sha256'], evidence['after']['raw_sha256'])
            if stage != 'raw_only':
                self.assertEqual(before_record['raw_sha256'], before['raw_sha256'])
            else:
                self.assertNotIn('raw_sha256', before_record)
        else:
            self.assertIsNone(before['raw_sha256'])
        for result in (evidence['resumed'], evidence['repeated']):
            self.assertNotEqual(result['pid'], evidence['ready']['pid'])
            self.assertEqual(result['kind'], outcome)
            summary = result['summary']
            record = summary['requests']['stable-request']
            self.assertEqual(len(record['attempts']), 1)
            if outcome == 'success':
                self.assertEqual(result['payload'], PAYLOAD)
                self.assertEqual(record['status'], 'success')
                self.assertEqual(summary['spent_cny'], 0.5)
                self.assertEqual(summary['reserved_cny'], 0)
            else:
                self.assertEqual(record['status'], 'unknown')
                self.assertEqual(record['reason'], 'interrupted_submission')
                self.assertEqual(summary['reserved_cny'], 2)
                self.assertEqual(summary['spent_cny'], 0)
        self.assertEqual(len(evidence['after']['sends']), 1 if outcome == 'success' else initial_sends)
        self.assertEqual(evidence['resumed_converted'], {'text': 'local fixture response'} if outcome == 'success' else None)
        if outcome == 'success':
            self.assertEqual(evidence['after']['raw_payload'], PAYLOAD)

    def test_reserved_without_begin_can_resume_its_first_send_after_hard_exit(self):
        self.check_stage('reserved', 'reserved', 0, 'success')

    def test_begin_before_send_is_conservatively_unknown_after_hard_exit(self):
        self.check_stage('submitting', 'submitting', 0, 'SubmissionUnknown')

    def test_sent_without_durable_response_never_sends_again_after_hard_exit(self):
        self.check_stage('sent', 'submitting', 1, 'SubmissionUnknown')

    def test_raw_without_durable_receipt_never_sends_again_after_hard_exit(self):
        self.check_stage('raw_only', 'submitting', 1, 'SubmissionUnknown')

    def test_received_response_replays_locally_after_hard_exit_before_conversion(self):
        self.check_stage('received', 'received', 1, 'success')

    def test_success_replays_locally_after_hard_exit_before_caller_conversion(self):
        self.check_stage('success', 'success', 1, 'success')


if __name__ == '__main__':
    unittest.main()
