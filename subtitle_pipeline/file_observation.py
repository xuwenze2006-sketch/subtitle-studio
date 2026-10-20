"""Observe a file generation without reading its contents or owning task state."""
import os
from pathlib import Path


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
