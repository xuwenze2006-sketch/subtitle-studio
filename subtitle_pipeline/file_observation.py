"""Observe a file generation without reading its contents or owning task state."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def stable_read_guard(path):
    """Deny Windows writers/deletion for the caller's entire read lifetime.

    ChangeTime can repeat for fast writes, so tokens alone cannot protect a
    content read. Existing readers remain compatible; a preexisting writer or
    delete-access handle prevents protection and must fail before reading.
    The owned handle is released on exit. Callers consuming the path again
    after hashing must hold this guard through their last dependent read.
    """
    if os.name!='nt':
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING; NULL security attributes
    # create an owned, non-inheritable handle. No writer/delete sharing.
    handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
    if handle in (None,ctypes.c_void_p(-1).value):
        code=ctypes.get_last_error()
        if code in (32,33):
            raise ctypes.WinError(code,'输入文件正被其他程序写入或占用，请关闭编辑器或等待写入完成后重试')
        raise ctypes.WinError(code)
    primary=None
    try:
        yield
    except BaseException as error:
        primary=error
        raise
    finally:
        if not kernel.CloseHandle(handle):
            code=ctypes.get_last_error()
            error=ctypes.WinError(code,f'关闭输入文件保护句柄失败（CloseHandle）：{ctypes.FormatError(code).strip()}')
            if primary is None:raise error
            primary.add_note(str(error))


def windows_file_change_time(descriptor):
    """Read NTFS metadata change time; Windows stat ctime is creation time."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class FileBasicInfo(ctypes.Structure):
        _fields_=[('CreationTime',ctypes.c_longlong),('LastAccessTime',ctypes.c_longlong),
                  ('LastWriteTime',ctypes.c_longlong),('ChangeTime',ctypes.c_longlong),
                  ('FileAttributes',wintypes.DWORD)]
    query=ctypes.WinDLL('kernel32',use_last_error=True).GetFileInformationByHandleEx
    query.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    query.restype=wintypes.BOOL
    info=FileBasicInfo()
    if not query(msvcrt.get_osfhandle(descriptor),0,ctypes.byref(info),ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.ChangeTime<=0:
        raise OSError('无法读取文件的有效更改时间，未确认文件身份')
    return info.ChangeTime


def file_token(path, *, change_time=None):
    """Return stat identity plus Windows ChangeTime, failing on torn observations."""
    path=Path(path)
    def stamp(value):
        return value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns
    before=stamp(path.stat())
    if os.name!='nt':return before
    descriptor=os.open(path,os.O_RDONLY|os.O_BINARY)
    try:
        opened=stamp(os.fstat(descriptor))
        changed=(change_time or windows_file_change_time)(descriptor)
        if (opened[:4]!=before[:4] or stamp(os.fstat(descriptor))!=opened
                or stamp(path.stat())!=before):
            raise ValueError('校验期间文件发生变化，未确认文件身份')
        return (*before,changed)
    finally:
        os.close(descriptor)
