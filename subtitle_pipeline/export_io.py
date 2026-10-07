"""Bounded, cancellable file I/O for local export preparation and publication."""

import hashlib
import os
from pathlib import Path
import shutil

from .runner import Cancelled


def _check_cancel(stop):
    if stop.is_set():raise Cancelled('已停止导出文件处理，已有结果保留')


def _retry_windows_file_operation(operation,stop,*,check_initial_cancel):
    for attempt in range(6):
        if check_initial_cancel or attempt:
            _check_cancel(stop)
        try:
            return operation()
        except OSError as error:
            if os.name!='nt' or getattr(error,'winerror',None) not in (5,32,33) or attempt==5:
                raise
            if stop.wait(.02*2**attempt):
                raise Cancelled('已停止导出文件处理，已有结果保留') from error


def rename_with_retry(source,destination,stop):
    """Retry brief Windows sharing conflicts without replacing an existing file."""
    return _retry_windows_file_operation(lambda:os.rename(source,destination),stop,
                                         check_initial_cancel=True)


def unlink_with_retry(path,stop):
    """Remove only the owned temporary file, even when already cancelled."""
    return _retry_windows_file_operation(lambda:Path(path).unlink(missing_ok=True),stop,
                                         check_initial_cancel=False)


def _validate_chunk_size(chunk_size):
    if type(chunk_size) is not int or chunk_size<=0:
        raise ValueError('文件处理块大小必须是正整数')


class _CancellableReader:
    def __init__(self,source,stop,digest=None):
        self.source=source
        self.stop=stop
        self.digest=digest

    def read(self,size):
        _check_cancel(self.stop)
        data=self.source.read(size)
        _check_cancel(self.stop)
        if self.digest is not None:self.digest.update(data)
        return data


class _CheckedWriter:
    def __init__(self,destination):self.destination=destination

    def write(self,data):
        written=self.destination.write(data)
        if written!=len(data):raise OSError('导出文件写入不完整，未确认复制成功')
        return written


def cancellable_sha256(path,stop,*,chunk_size=1024*1024):
    """Hash a file with a cancellation check before and after each bounded read."""
    _validate_chunk_size(chunk_size)
    _check_cancel(stop)
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        reader=_CancellableReader(source,stop,digest)
        while reader.read(chunk_size):pass
    _check_cancel(stop)
    return digest.hexdigest()


def copy_and_hash(source_file,destination_file,stop,*,expected_hash=None,chunk_size=1024*1024):
    """Copy from current positions; callers retain file ownership and publication."""
    _validate_chunk_size(chunk_size)
    _check_cancel(stop)
    digest=hashlib.sha256()
    shutil.copyfileobj(_CancellableReader(source_file,stop,digest),_CheckedWriter(destination_file),chunk_size)
    _check_cancel(stop)
    actual=digest.hexdigest()
    if expected_hash is not None and actual!=expected_hash:
        raise ValueError('导出文件复制校验不一致，未确认复制成功')
    return actual


def cancellable_copy(source_file,destination_file,stop,*,chunk_size=1024*1024):
    """Copy without hashing when the caller already has a trusted source digest."""
    _validate_chunk_size(chunk_size)
    _check_cancel(stop)
    shutil.copyfileobj(_CancellableReader(source_file,stop),_CheckedWriter(destination_file),chunk_size)
    _check_cancel(stop)
