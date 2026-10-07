"""Owned subprocess lifetime, including a real hard-killed Windows parent."""
import ctypes
from ctypes import wintypes
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


class _JobAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in
                ('user', 'kernel', 'period_user', 'period_kernel')] + [
                (name, wintypes.DWORD) for name in
                ('page_faults', 'total', 'active', 'terminated')]


class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.windows_job'),
                             'owned Windows process job is not implemented')
        self.jobs=importlib.import_module('subtitle_pipeline.windows_job')
        self.children=[]
        self.addCleanup(self.reap_children)
        if os.name=='nt':
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.GetHandleInformation.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            self.kernel.GetHandleInformation.restype=wintypes.BOOL
            self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            self.kernel.OpenProcess.restype=wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype=wintypes.DWORD
            self.kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
            self.kernel.TerminateProcess.restype=wintypes.BOOL
            self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            self.kernel.CloseHandle.restype=wintypes.BOOL

    def child(self,program='import time; time.sleep(20)',*args):
        child=subprocess.Popen([sys.executable,'-X','utf8','-c',program,*map(str,args)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.children.append(child)
        return child

    def reap_children(self):
        for child in self.children:
            if os.name=='nt':
                if self.kernel.WaitForSingleObject(int(child._handle),0)==258:
                    self.kernel.TerminateProcess(int(child._handle),93)
                self.assertEqual(self.kernel.WaitForSingleObject(int(child._handle),3000),0)
                child.returncode=None
                child.wait(timeout=0)
            else:
                if child.poll() is None:child.kill()
                child.wait(timeout=3)

    def assert_closed(self,handle):
        flags=wintypes.DWORD()
        self.assertFalse(self.kernel.GetHandleInformation(handle,ctypes.byref(flags)))
        self.assertEqual(ctypes.get_last_error(),6)

    def test_non_windows_is_noop_and_never_touches_the_passed_child(self):
        child=Mock()
        with patch.object(self.jobs.os,'name','posix'), \
                patch.object(self.jobs,'_create_job',side_effect=AssertionError('Windows API on another platform')):
            with self.jobs.owned_process(child) as returned:
                self.assertIs(returned,child)
        child.assert_not_called()
        self.assertEqual(child.mock_calls,[])

    @unittest.skipUnless(os.name=='nt','Native worker exit observation')
    def test_exit_observation_does_not_trust_cached_code_or_terminate_child(self):
        self.assertTrue(hasattr(self.jobs,'wait_for_process_exit'))
        child=Mock(_handle=123,returncode=0)
        with patch.object(self.jobs,'_wait_process',return_value=False) as wait, \
                patch.object(self.jobs,'_get_exit_code') as code, \
                patch.object(self.jobs,'_terminate_process') as terminate:
            self.assertFalse(self.jobs.wait_for_process_exit(child,250))
        wait.assert_called_once_with(child,250)
        code.assert_not_called();terminate.assert_not_called();child.wait.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Native worker exit observation')
    def test_exit_observation_refreshes_actual_code_only_after_signal(self):
        self.assertTrue(hasattr(self.jobs,'wait_for_process_exit'))
        child=Mock(_handle=123,returncode=0)
        with patch.object(self.jobs,'_wait_process',return_value=True), \
                patch.object(self.jobs,'_get_exit_code',return_value=9):
            self.assertTrue(self.jobs.wait_for_process_exit(child,0))
        self.assertEqual(child.returncode,9)

    def test_portable_exit_observation_reports_timeout_without_killing(self):
        self.assertTrue(hasattr(self.jobs,'wait_for_process_exit'))
        child=Mock(args=['synthetic-worker'])
        with patch.object(self.jobs.os,'name','posix'):
            child.wait.side_effect=subprocess.TimeoutExpired(child.args,.25)
            self.assertFalse(self.jobs.wait_for_process_exit(child,250))
            child.wait.assert_called_once_with(timeout=.25)
            child.wait.side_effect=None;child.wait.return_value=7
            self.assertTrue(self.jobs.wait_for_process_exit(child,0))
        child.kill.assert_not_called();child.terminate.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_normal_context_closes_noninheritable_job_and_reaps_owned_child(self):
        child=self.child()
        handles=[]
        create=self.jobs._create_job
        def capture_job():
            handle=create();handles.append(handle)
            flags=wintypes.DWORD()
            self.assertTrue(self.kernel.GetHandleInformation(handle,ctypes.byref(flags)))
            self.assertEqual(flags.value & 1,0,'Job handle must not be inherited')
            return handle
        with patch.object(self.jobs,'_create_job',side_effect=capture_job):
            with self.jobs.owned_process(child) as returned:
                self.assertIs(returned,child)
                self.assertIsNone(child.poll())
        self.assertIsNotNone(child.returncode)
        self.assertEqual(len(handles),1)
        self.assert_closed(handles[0])

    @unittest.skipUnless(os.name=='nt','Windows Job process-group exit barrier')
    def test_context_waits_for_all_job_members_before_closing_its_handle(self):
        child=self.child()
        query=self.kernel.QueryInformationJobObject
        query.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,wintypes.LPVOID]
        query.restype=wintypes.BOOL
        active=[]
        close=self.jobs._close_job
        def observed_close(handle):
            info=_JobAccounting()
            succeeded=query(handle,1,ctypes.byref(info),ctypes.sizeof(info),None)
            if succeeded:active.append(info.active)
            close(handle)
            self.assertTrue(succeeded,'could not observe the owned Job before closing')
        with patch.object(self.jobs,'_close_job',side_effect=observed_close):
            with self.jobs.owned_process(child):pass
        self.assertEqual(active,[0],'owned Job members must exit before its handle is released')

    @unittest.skipUnless(os.name=='nt','Windows Job process-group exit errors')
    def test_job_query_failure_preserves_cancellation_and_still_closes_job(self):
        from subtitle_pipeline.runner import Cancelled
        child=self.child()
        primary=Cancelled('original stop')
        handles=[]
        close=self.jobs._close_job
        def observed_close(handle):
            handles.append(handle)
            return close(handle)
        def failed(*args):
            ctypes.set_last_error(6)
            return 0
        with patch.object(self.jobs._kernel32,'QueryInformationJobObject',side_effect=failed), \
                patch.object(self.jobs,'_close_job',side_effect=observed_close):
            with self.assertRaises(Cancelled) as raised:
                with self.jobs.owned_process(child):raise primary
        self.assertIs(raised.exception,primary)
        self.assertTrue(any('QueryInformationJobObject' in note for note in getattr(primary,'__notes__',[])))
        self.assertEqual(len(handles),1)
        self.assert_closed(handles[0])
        self.assertEqual(self.kernel.WaitForSingleObject(int(child._handle),0),0)

    @unittest.skipUnless(os.name=='nt','Windows native Job member exit barrier')
    def test_inactive_job_still_waits_for_native_member_handles_with_one_deadline(self):
        with patch.object(self.jobs._kernel32,'TerminateJobObject',return_value=1), \
                patch.object(self.jobs,'_job_active_processes',return_value=0), \
                patch.object(self.jobs._kernel32,'WaitForSingleObject',side_effect=[0,258]) as wait:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                self.jobs._terminate_job_and_wait(1,[11,12],timeout=.04)
        self.assertEqual(raised.exception.timeout,.04)
        self.assertEqual([call.args[0] for call in wait.call_args_list],[11,12])
        self.assertTrue(all(0<=call.args[1]<=40 for call in wait.call_args_list))
        self.assertLessEqual(wait.call_args_list[1].args[1],wait.call_args_list[0].args[1])

    @unittest.skipUnless(os.name=='nt','Windows venv launcher descendant exit barrier')
    def test_actual_python_process_is_signaled_when_owned_launcher_scope_exits(self):
        program='from pathlib import Path;import os,sys,time;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(20)'
        with tempfile.TemporaryDirectory() as directory:
            ready=Path(directory)/'ready.txt'
            child=self.child(program,ready)
            handle=None
            try:
                with self.jobs.owned_process(child):
                    deadline=time.monotonic()+5
                    while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue(ready.exists(),'Python descendant did not become ready')
                    handle=self.kernel.OpenProcess(0x00100000,False,int(ready.read_text()))
                    self.assertTrue(handle,'could not retain the exact Python process handle')
                    self.assertEqual(self.kernel.WaitForSingleObject(handle,0),258)
                self.assertEqual(self.kernel.WaitForSingleObject(handle,0),0,
                                 'actual Python process must be signaled before context returns')
            finally:
                if handle:
                    self.kernel.WaitForSingleObject(handle,3000)
                    self.kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_setup_failure_terminates_child_and_closes_any_created_job(self):
        for stage in ('_create_job','_configure_job','_assign_job'):
            with self.subTest(stage=stage):
                child=self.child()
                primary=OSError('injected '+stage+' failure')
                handles=[]
                create=self.jobs._create_job
                def capture_job():
                    handle=create();handles.append(handle);return handle
                from contextlib import ExitStack
                with ExitStack() as stack:
                    if stage!='_create_job':stack.enter_context(patch.object(self.jobs,'_create_job',side_effect=capture_job))
                    stack.enter_context(patch.object(self.jobs,stage,side_effect=primary))
                    with self.assertRaises(OSError) as raised:
                        with self.jobs.owned_process(child):
                            self.fail('unprotected child was yielded after setup failure')
                self.assertIs(raised.exception,primary)
                self.assertIsNotNone(child.returncode)
                for handle in handles:self.assert_closed(handle)

    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_child_exit_code_survives_normal_job_close(self):
        child=self.child('raise SystemExit(7)')
        with self.jobs.owned_process(child):
            self.assertEqual(child.wait(timeout=3),7)
        self.assertEqual(child.returncode,7)

    @unittest.skipUnless(os.name=='nt','Windows short-lived child assignment')
    def test_child_already_exited_before_job_attachment_keeps_its_actual_exit_code(self):
        for code in (0,7):
            with self.subTest(code=code):
                child=self.child(f'raise SystemExit({code})')
                self.assertEqual(child.wait(timeout=3),code)
                with self.jobs.owned_process(child) as returned:
                    self.assertIs(returned,child)
                    self.assertEqual(child.returncode,code)

    @unittest.skipUnless(os.name=='nt','Windows short-lived child assignment')
    def test_assignment_failure_only_allows_already_reaped_child_and_closes_job(self):
        child=self.child('raise SystemExit(9)')
        child.wait(timeout=3)
        handles=[]
        close=self.jobs._close_job
        def tracked_close(handle):handles.append(handle);return close(handle)
        with patch.object(self.jobs,'_assign_job',side_effect=OSError('process exited before assign')), \
                patch.object(self.jobs,'_close_job',side_effect=tracked_close):
            with self.jobs.owned_process(child):self.assertEqual(child.returncode,9)
        self.assertEqual(len(handles),1)
        self.assert_closed(handles[0])

    @unittest.skipUnless(os.name=='nt','Windows short-lived child assignment')
    def test_already_exited_child_does_not_hide_create_or_configuration_errors(self):
        for stage in ('_create_job','_configure_job'):
            with self.subTest(stage=stage):
                child=self.child('raise SystemExit(0)')
                child.wait(timeout=3)
                primary=OSError('injected '+stage+' failure')
                with patch.object(self.jobs,stage,side_effect=primary):
                    with self.assertRaises(OSError) as raised:
                        with self.jobs.owned_process(child):pass
                self.assertIs(raised.exception,primary)

    @unittest.skipUnless(os.name=='nt','Windows child exit confirmation')
    def test_unknown_poll_result_never_allows_failed_attachment(self):
        for uncertain in (OSError('poll unavailable'),'unknown'):
            with self.subTest(poll=uncertain):
                child=self.child()
                primary=OSError('injected attach failure')
                with patch.object(self.jobs,'_assign_job',side_effect=primary), \
                        patch.object(child,'poll',side_effect=[uncertain,None]):
                    with self.assertRaises(OSError) as raised:
                        with self.jobs.owned_process(child):
                            self.fail('unknown process state cannot bypass attachment failure')
                self.assertIs(raised.exception,primary)
                self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(os.name=='nt','Windows cached returncode is not an exit barrier')
    def test_cached_exit_code_cannot_allow_a_still_running_child_after_assign_failure(self):
        child=self.child()
        child.returncode=0
        primary=OSError('injected attach failure')
        with patch.object(self.jobs,'_assign_job',side_effect=primary):
            with self.assertRaises(OSError) as raised:
                with self.jobs.owned_process(child):
                    self.fail('cached returncode cannot confirm native process termination')
        self.assertIs(raised.exception,primary)
        self.assertEqual(self.kernel.WaitForSingleObject(int(child._handle),0),0)

    @unittest.skipUnless(os.name=='nt','Windows cached returncode is not an exit barrier')
    def test_cached_zero_must_still_wait_for_native_process_signal_and_refresh_code(self):
        self.assertTrue(hasattr(self.jobs,'_wait_process'))
        child=Mock(args=['synthetic-child'],_handle=123,returncode=0)
        with patch.object(self.jobs,'_wait_process',side_effect=[False,True]) as wait, \
                patch.object(self.jobs,'_terminate_process') as terminate, \
                patch.object(self.jobs,'_get_exit_code',return_value=7):
            self.jobs._terminate_and_reap(child)
        self.assertEqual([call.args[1] for call in wait.call_args_list],[0,1000])
        terminate.assert_called_once_with(child)
        child.wait.assert_not_called()
        self.assertEqual(child.returncode,7)

    @unittest.skipUnless(os.name=='nt','Windows bounded native exit barrier')
    def test_native_wait_uses_bounded_escalation_without_cached_popen_wait(self):
        self.assertTrue(hasattr(self.jobs,'_wait_process'))
        for ended in (True,False):
            with self.subTest(ended=ended):
                child=Mock(args=['synthetic-child'],_handle=123,returncode=0)
                with patch.object(self.jobs,'_wait_process',side_effect=[False,False,ended]) as wait, \
                        patch.object(self.jobs,'_terminate_process') as terminate, \
                        patch.object(self.jobs,'_get_exit_code',return_value=1) as exit_code:
                    if ended:self.jobs._terminate_and_reap(child)
                    else:
                        with self.assertRaises(subprocess.TimeoutExpired) as raised:
                            self.jobs._terminate_and_reap(child)
                        self.assertEqual(raised.exception.timeout,4)
                self.assertEqual([call.args[1] for call in wait.call_args_list],[0,1000,3000])
                self.assertEqual(terminate.call_count,2)
                child.wait.assert_not_called()
                self.assertEqual(child.returncode,1 if ended else None)
                self.assertEqual(exit_code.call_count,1 if ended else 0)

    @unittest.skipUnless(os.name=='nt','Windows wait errors')
    def test_wait_failed_keeps_native_error_code(self):
        self.assertTrue(hasattr(self.jobs,'_wait_process'))
        def failed(*args):ctypes.set_last_error(6);return 0xffffffff
        with patch.object(self.jobs._kernel32,'WaitForSingleObject',side_effect=failed):
            with self.assertRaises(OSError) as raised:
                self.jobs._wait_process(Mock(_handle=123),0)
        self.assertEqual(raised.exception.winerror,6)

    @unittest.skipUnless(os.name=='nt','Windows wait errors')
    def test_failed_initial_native_wait_still_attempts_to_stop_owned_child(self):
        child=Mock(args=['synthetic-child'],_handle=123,returncode=0)
        primary=OSError('injected initial wait failure')
        with patch.object(self.jobs,'_wait_process',side_effect=primary), \
                patch.object(self.jobs,'_terminate_process') as terminate:
            with self.assertRaises(OSError) as raised:
                self.jobs._terminate_and_reap(child)
        self.assertIs(raised.exception,primary)
        self.assertIsNone(child.returncode)
        terminate.assert_called_once_with(child)

    @unittest.skipUnless(os.name=='nt','Windows wait errors')
    def test_native_wait_failure_does_not_hide_the_original_cancellation(self):
        self.assertTrue(hasattr(self.jobs,'_wait_process'))
        from subtitle_pipeline.runner import Cancelled
        child=self.child()
        primary=Cancelled('original stop')
        with patch.object(self.jobs,'_wait_process',side_effect=OSError('injected native wait failure')):
            with self.assertRaises(Cancelled) as raised:
                with self.jobs.owned_process(child):raise primary
        self.assertIs(raised.exception,primary)
        self.assertTrue(any('native wait failure' in note for note in primary.__notes__))

    @unittest.skipUnless(os.name=='nt','Windows file handle release after process termination')
    def test_real_runner_cancel_closes_parent_log_and_allows_immediate_delete(self):
        from subtitle_pipeline import runner
        with tempfile.TemporaryDirectory() as directory:
            for index in range(5):
                observed={}
                real_popen=subprocess.Popen
                def track(*args,**kwargs):
                    observed['stream']=kwargs['stdout']
                    observed['fd']=kwargs['stdout'].fileno()
                    child=real_popen(*args,**kwargs)
                    observed['child']=child;self.children.append(child)
                    return child
                stop=threading.Event()
                timer=threading.Timer(.05,stop.set)
                log=Path(directory)/f'cancel-{index}.log'
                timer.start()
                try:
                    with patch.object(runner.subprocess,'Popen',side_effect=track):
                        with self.assertRaises(runner.Cancelled):
                            runner.run_process([sys.executable,'-c','import time; time.sleep(20)'],log,stop)
                    self.assertTrue(observed['stream'].closed)
                    with self.assertRaises(OSError):os.fstat(observed['fd'])
                    log.unlink()
                    self.assertEqual(self.kernel.WaitForSingleObject(int(observed['child']._handle),0),0)
                finally:
                    timer.cancel();timer.join()
                    if 'child' in observed:
                        self.assertEqual(self.kernel.WaitForSingleObject(int(observed['child']._handle),3000),0)

    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_original_body_error_is_preserved_if_job_close_reports_failure(self):
        child=self.child()
        primary=ValueError('original body failure')
        close=self.jobs._close_job
        def close_then_fail(handle):
            close(handle)
            raise OSError('injected close failure')
        with patch.object(self.jobs,'_close_job',side_effect=close_then_fail):
            with self.assertRaises(ValueError) as raised:
                with self.jobs.owned_process(child):raise primary
        self.assertIs(raised.exception,primary)
        self.assertTrue(any('close failure' in note for note in primary.__notes__))
        self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_close_failure_without_primary_error_is_not_silenced(self):
        child=self.child()
        primary=OSError('injected close failure')
        close=self.jobs._close_job
        def close_then_fail(handle):close(handle);raise primary
        with patch.object(self.jobs,'_close_job',side_effect=close_then_fail):
            with self.assertRaises(OSError) as raised:
                with self.jobs.owned_process(child):pass
        self.assertIs(raised.exception,primary)
        self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_setup_error_is_preserved_even_when_job_cleanup_also_fails(self):
        child=self.child()
        primary=OSError('injected attach failure')
        close=self.jobs._close_job
        def close_then_fail(handle):close(handle);raise OSError('injected cleanup failure')
        with patch.object(self.jobs,'_assign_job',side_effect=primary), \
                patch.object(self.jobs,'_close_job',side_effect=close_then_fail):
            with self.assertRaises(OSError) as raised:
                with self.jobs.owned_process(child):pass
        self.assertIs(raised.exception,primary)
        self.assertTrue(any('cleanup failure' in note for note in primary.__notes__))
        self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(os.name=='nt','Windows hard-exit process lifetime')
    def test_hard_killing_attached_parent_reaps_its_child(self):
        program='''
from pathlib import Path
import json,os,subprocess,sys,time
from subtitle_pipeline.windows_job import owned_process
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW)
with owned_process(child):
    ready=Path(sys.argv[1])
    temporary=ready.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid':child.pid}),encoding='utf-8')
    temporary.replace(ready)
    time.sleep(20)
'''
        with tempfile.TemporaryDirectory() as directory:
            ready=Path(directory)/'ready.json'
            parent=self.child(program,ready)
            handle=None
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and parent.poll() is None and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(),'parent did not finish attaching child to Job')
                child_pid=json.loads(ready.read_text(encoding='utf-8'))['pid']
                handle=self.kernel.OpenProcess(0x00100000|0x0001,False,child_pid)
                self.assertTrue(handle,'could not retain exact test child handle')
                self.assertEqual(self.kernel.WaitForSingleObject(handle,0),258)
                parent.kill();parent.wait(timeout=3)
                self.assertEqual(self.kernel.WaitForSingleObject(handle,3000),0,
                                 'attached child survived hard-killed parent')
            finally:
                if parent.poll() is None:parent.kill()
                parent.wait(timeout=3)
                if handle:
                    if self.kernel.WaitForSingleObject(handle,0)==258:
                        self.kernel.TerminateProcess(handle,92)
                    self.assertEqual(self.kernel.WaitForSingleObject(handle,3000),0)
                    self.kernel.CloseHandle(handle)


if __name__=='__main__':unittest.main()
