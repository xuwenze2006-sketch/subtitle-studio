"""Bind an owned Windows child to its parent's lifetime after Popen returns.

This does not eliminate the interval between Popen and AssignProcessToJobObject.
Non-Windows callers retain their existing process-lifetime behavior.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import subprocess
import time


class _BasicLimits(ctypes.Structure):
    _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),
              ('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',wintypes.DWORD),
              ('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
              ('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),
              ('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_=[(name,ctypes.c_ulonglong) for name in
              ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
               'ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_=[('BasicLimitInformation',_BasicLimits),('IoInfo',_IOCounters),
              ('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
              ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]


class _BasicAccounting(ctypes.Structure):
    _fields_=[(name,ctypes.c_longlong) for name in
              ('TotalUserTime','TotalKernelTime','ThisPeriodTotalUserTime','ThisPeriodTotalKernelTime')]+[
              (name,wintypes.DWORD) for name in
              ('TotalPageFaultCount','TotalProcesses','ActiveProcesses','TotalTerminatedProcesses')]


if os.name=='nt':
    _kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes=[wintypes.LPVOID,wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype=wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype=wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype=wintypes.BOOL
    _kernel32.CloseHandle.argtypes=[wintypes.HANDLE]
    _kernel32.CloseHandle.restype=wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype=wintypes.DWORD
    _kernel32.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype=wintypes.BOOL
    _kernel32.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    _kernel32.TerminateProcess.restype=wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
    _kernel32.TerminateJobObject.restype=wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,
                                                wintypes.DWORD,wintypes.LPVOID]
    _kernel32.QueryInformationJobObject.restype=wintypes.BOOL
    _kernel32.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    _kernel32.OpenProcess.restype=wintypes.HANDLE
    _kernel32.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
    _kernel32.IsProcessInJob.restype=wintypes.BOOL


def _win_error(operation):
    code=ctypes.get_last_error()
    return ctypes.WinError(code,f'Windows 子进程保护失败：{operation}；{ctypes.FormatError(code).strip()}')


def _create_job():
    # NULL security attributes create a non-inheritable, unnamed handle.
    job=_kernel32.CreateJobObjectW(None,None)
    if not job:raise _win_error('CreateJobObjectW')
    return job


def _configure_job(job):
    limits=_ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags=0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)):
        raise _win_error('SetInformationJobObject')


def _assign_job(job,child):
    # Popen retains ownership of its process handle. Never close it here.
    if not _kernel32.AssignProcessToJobObject(job,int(child._handle)):
        raise _win_error('AssignProcessToJobObject')


def _close_job(job):
    if not _kernel32.CloseHandle(job):raise _win_error('CloseHandle(Job)')


def _job_active_processes(job):
    accounting=_BasicAccounting()
    if not _kernel32.QueryInformationJobObject(job,1,ctypes.byref(accounting),ctypes.sizeof(accounting),None):
        raise _win_error('QueryInformationJobObject')
    return accounting.ActiveProcesses


def _job_process_ids(job):
    capacity=16
    while capacity<=4096:
        class ProcessIds(ctypes.Structure):
            _fields_=[('assigned',wintypes.DWORD),('count',wintypes.DWORD),
                      ('ids',ctypes.c_size_t*capacity)]
        members=ProcessIds()
        ok=_kernel32.QueryInformationJobObject(job,3,ctypes.byref(members),ctypes.sizeof(members),None)
        if ok and members.count>=members.assigned:return list(members.ids[:members.count])
        if not ok and ctypes.get_last_error()!=234:raise _win_error('QueryInformationJobObject(ProcessIds)')
        capacity=max(capacity*2,members.assigned)
    raise OSError('Windows 自有子进程组过大，无法确认全部进程退出')


def _retain_job_processes(job):
    handles=[]
    try:
        for pid in _job_process_ids(job):
            handle=_kernel32.OpenProcess(0x00100000|0x1000,False,pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if not handle:
                if ctypes.get_last_error()==87:continue  # Already exited before OpenProcess.
                raise _win_error('OpenProcess(Job member)')
            handles.append(handle)
            belongs=wintypes.BOOL()
            if not _kernel32.IsProcessInJob(handle,job,ctypes.byref(belongs)):
                raise _win_error('IsProcessInJob')
            if not belongs.value:
                # The PID may have been reused between enumeration and open.
                if not _kernel32.CloseHandle(handle):raise _win_error('CloseHandle(Job member)')
                handles.pop()
        return handles
    except BaseException as error:
        for handle in handles:
            if not _kernel32.CloseHandle(handle):error.add_note(str(_win_error('CloseHandle(Job member)')))
        raise


def _terminate_job_and_wait(job,handles=(),timeout=4):
    # Closing a kill-on-close Job initiates asynchronous termination. A venv
    # launcher can exit before its Python descendants release inherited logs.
    # ActiveProcesses can reach zero before process handles become signaled;
    # retained member handles provide the final native resource-release barrier.
    if not _kernel32.TerminateJobObject(job,1):raise _win_error('TerminateJobObject')
    deadline=time.monotonic()+timeout
    while _job_active_processes(job):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise subprocess.TimeoutExpired('Windows owned process Job',timeout)
        time.sleep(min(.01,remaining))
    for handle in handles:
        remaining=max(0,int((deadline-time.monotonic())*1000))
        result=_kernel32.WaitForSingleObject(handle,remaining)
        if result==0:continue
        if result==258:raise subprocess.TimeoutExpired('Windows owned process Job',timeout)
        if result==0xffffffff:raise _win_error('WaitForSingleObject(Job member)')
        raise OSError(f'Windows 子进程组等待返回未知状态：{result}')


def _wait_process(child,milliseconds):
    result=_kernel32.WaitForSingleObject(int(child._handle),milliseconds)
    if result==0:return True  # WAIT_OBJECT_0
    if result==258:return False  # WAIT_TIMEOUT
    if result==0xffffffff:raise _win_error('WaitForSingleObject(Process)')
    raise OSError(f'Windows 进程等待返回未知状态：{result}')


def _get_exit_code(child):
    code=wintypes.DWORD()
    if not _kernel32.GetExitCodeProcess(int(child._handle),ctypes.byref(code)):
        raise _win_error('GetExitCodeProcess')
    return code.value


def _terminate_process(child):
    if not _kernel32.TerminateProcess(int(child._handle),1):
        raise _win_error('TerminateProcess')


def wait_for_process_exit(child,milliseconds=0):
    """Observe exit without killing; Windows needs the native signal barrier."""
    if os.name!='nt':
        try:child.wait(timeout=milliseconds/1000)
        except subprocess.TimeoutExpired:return False
        return True
    if not _wait_process(child,milliseconds):return False
    child.returncode=_get_exit_code(child)
    return True


def _terminate_and_reap(child):
    # Job termination can expose an exit code before the process is signaled.
    # Popen.wait/terminate may then skip their native calls due to cached state.
    try:signaled=_wait_process(child,0)
    except OSError as wait_error:
        child.returncode=None
        try:_terminate_process(child)
        except OSError as terminate_error:
            wait_error.add_note(f'无法停止自有子进程：{terminate_error}')
        raise
    if not signaled:
        child.returncode=None
        try:_terminate_process(child)
        except OSError:pass  # A successful Job close may already have killed it.
        if not _wait_process(child,1000):
            try:_terminate_process(child)
            except OSError:pass
            if not _wait_process(child,3000):
                raise subprocess.TimeoutExpired(child.args,4)
    child.returncode=_get_exit_code(child)


@contextmanager
def owned_process(child):
    """Own one Popen child with Windows kill-on-close protection.

    Setup failures never yield an unprotected running child. They close any
    created Job and terminate/reap this child before propagating the original
    error. Cleanup errors are attached to an existing exception as notes.
    """
    if os.name!='nt':
        yield child
        return
    job=None
    primary=None
    try:
        job=_create_job()
        _configure_job(job)
        try:
            _assign_job(job,child)
        except OSError as assign_error:
            # A very short child may have completed before assignment. This
            # exception covers only confirmed exits, not unknown/live children
            # or descendants that an exited child started before attachment.
            try:returncode=child.poll()
            except Exception as poll_error:
                assign_error.add_note(f'未能确认子进程已退出：{poll_error}')
                raise assign_error
            if type(returncode) is not int:
                raise
            try:
                signaled=_wait_process(child,0)
                if signaled:child.returncode=_get_exit_code(child)
            except OSError as wait_error:
                assign_error.add_note(f'未能确认子进程原生退出状态：{wait_error}')
                raise assign_error
            if not signaled:
                raise
        yield child
    except BaseException as error:
        # sys.exc_info() can refer to a caller's already-handled encoder error
        # while this scope succeeds (for example during CPU fallback).
        primary=error
        raise
    finally:
        errors=[]
        if job is not None:
            handles=[]
            try:handles=_retain_job_processes(job)
            except BaseException as error:errors.append(error)
            try:_terminate_job_and_wait(job,handles)
            except BaseException as error:errors.append(error)
            try:_close_job(job)
            except BaseException as error:errors.append(error)
            for handle in handles:
                if not _kernel32.CloseHandle(handle):errors.append(_win_error('CloseHandle(Job member)'))
        try:_terminate_and_reap(child)
        except BaseException as error:errors.append(error)
        if errors:
            if primary is not None:
                for error in errors:primary.add_note(f'Windows 子进程清理失败：{error}')
            else:
                for error in errors[1:]:errors[0].add_note(f'Windows 子进程清理失败：{error}')
                raise errors[0]
