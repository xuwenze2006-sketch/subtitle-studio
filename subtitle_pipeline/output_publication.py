"""One-output publication that retains the actual displaced file object.

Callers persist the returned preparation record before calling publish_record.
Only status=clean and published=True proves completion. Captured conflicts are
not restored over an existing destination or automatically discarded.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import time
import uuid

from .integrity import HumanEditConflict
from .atomic_io import cleanup_temporary

_WINDOWS = os.name == 'nt'
_FIELDS = {'version', 'relative', 'digest', 'expected_old', 'attempt'}


def _valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _relative(value):
    if not isinstance(value, str) or not value or PureWindowsPath(value).drive:
        raise HumanEditConflict('字幕发布记录的路径无效，未修改文件')
    value = PurePosixPath(value.replace('\\', '/'))
    if value.is_absolute() or not value.parts or any(p in ('.', '..') for p in value.parts):
        raise HumanEditConflict('字幕发布记录的路径无效，未修改文件')
    return Path(*value.parts)


def _safe_path(project, relative):
    project = Path(project).resolve()
    path = project / _relative(relative)
    if not path.parent.resolve().is_relative_to(project):
        raise HumanEditConflict('字幕发布路径越出项目，未修改文件')
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return path
    if path.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400 or path.is_dir():
        raise HumanEditConflict('字幕发布路径是链接或目录，请保留文件后核对')
    return path


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_record(project, record):
    """Validate the whole record without changing files; useful before a batch."""
    if (not isinstance(record, dict) or set(record) != _FIELDS
            or type(record.get('version')) is not int or record['version'] != 1
            or not _valid_digest(record.get('digest'))
            or record.get('expected_old') is not None and not _valid_digest(record['expected_old'])):
        raise HumanEditConflict('字幕发布恢复记录无效，请保留文件后核对')
    attempt = _relative(record.get('attempt'))
    if (len(attempt.parts) != 2 or attempt.parts[0] != '用户修改备份'
            or re.fullmatch(r'发布恢复-[0-9a-f]{32}', attempt.parts[1]) is None):
        raise HumanEditConflict('字幕发布恢复目录无效，未修改文件')
    for directory in (Path(project).resolve() / attempt.parts[0],
                      Path(project).resolve() / attempt):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if (directory.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
                or not directory.is_dir()):
            raise HumanEditConflict('字幕恢复目录包含链接或其他文件，不能作为本次临时目录清理')
    destination = _safe_path(project, record.get('relative'))
    if _relative(record['relative']).parts[0] == '用户修改备份':
        raise HumanEditConflict('字幕输出不能覆盖发布恢复目录')
    _safe_path(project, str(attempt / 'previous.srt'))
    prepared = _safe_path(project, str(attempt / 'replacement.srt'))
    if prepared.exists() and _hash(prepared) != record['digest']:
        raise HumanEditConflict('待发布字幕已被另外修改，请保留恢复目录后核对')
    return destination


def _paths(project, record):
    destination = validate_record(project, record)
    attempt = Path(project).resolve() / _relative(record['attempt'])
    return destination, attempt / 'replacement.srt', attempt / 'previous.srt'


def prepare_publication(project, relative, text, expected_old):
    """Prepare one fsynced text file; persist this JSON record before publishing."""
    if not isinstance(text, str) or expected_old is not None and not _valid_digest(expected_old):
        raise ValueError('字幕发布正文或原文件校验值无效')
    destination = _safe_path(project, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = {'version': 1, 'relative': relative,
              'digest': hashlib.sha256(text.encode('utf-8')).hexdigest(),
              'expected_old': expected_old,
              'attempt': '用户修改备份/发布恢复-' + uuid.uuid4().hex}
    validate_record(project, record)
    attempt = Path(project).resolve() / record['attempt']
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.mkdir()
    prepared = attempt / 'replacement.srt'
    try:
        with prepared.open('xb') as stream:
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException as error:
        cleanup_temporary(prepared)
        try:
            attempt.rmdir()
        except OSError as cleanup_error:
            error.add_note(f'未发布字幕临时目录保留：{attempt}；{cleanup_error}')
        raise
    return record


def _result(project, record, status, published, reason):
    capture = Path(project).resolve() / _relative(record['attempt']) / 'previous.srt'
    return {'status': status, 'published': published, 'relative': record['relative'],
            'digest': record['digest'], 'capture': capture.relative_to(Path(project).resolve()).as_posix()
            if capture.exists() else None, 'reason': reason}


def _publish_missing(source, destination):
    """Publish without replacement, including a concurrent destination creator."""
    if _WINDOWS:
        os.rename(source, destination)
    else:
        os.link(source, destination, follow_symlinks=False)
        source.unlink()


def _replace_windows(destination, prepared, capture):
    import ctypes
    from ctypes import wintypes
    replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    replace.restype = wintypes.BOOL
    if not replace(str(destination), str(prepared), str(capture), 0, None, None):
        # Includes partial failure 1177: the journal and all files remain owned
        # by the caller. Never repeat an uncertain multi-file native operation.
        raise ctypes.WinError(ctypes.get_last_error())


def _inspect_capture_windows(capture, expected):
    """Delete only a verified machine generation under this exclusive handle."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(capture), 0x80000000 | 0x10000, 0, None, 3, 0x200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        if error in (5, 32, 33):
            return 'busy'
        raise ctypes.WinError(error or 6)
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException as error:
        try:
            os.close(descriptor)
        except OSError as close_error:
            error.add_note(f'字幕恢复文件描述符关闭失败：{close_error}')
        raise
    with stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            return 'preserved'
        delete = kernel.SetFileInformationByHandle
        delete.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        delete.restype = wintypes.BOOL
        disposition = ctypes.c_ubyte(1)
        if not delete(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            # A mapped/ACL/driver restriction is not permission to unlink by
            # pathname after releasing the guard. Keep the captured generation.
            return 'busy'
    return 'clean'


def _cleanup_clean(prepared):
    # The prepared file is owned, content-validated, and never an old editor's
    # displaced object. Never recursively clean an attempt directory.
    prepared.unlink(missing_ok=True)
    try:
        prepared.parent.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        if not any(prepared.parent.iterdir()):
            raise


def discard_prepared(project, record):
    """Discard an unstarted owned preparation, never a displaced generation."""
    _, prepared, capture = _paths(project, record)
    if capture.exists():
        raise HumanEditConflict('字幕发布已经保留原文件，不能作为未开始任务清理')
    _cleanup_clean(prepared)


def recover_record(project, record, stop=None):
    """Recover facts without clobbering an independently created destination.

    Recovery remains available after cancellation so it can restore a missing
    directory entry. The caller retains its journal on preserved/busy results.
    """
    destination, prepared, capture = _paths(project, record)
    current = _hash(destination) if destination.exists() else None
    if capture.exists() and current is None:
        try:
            _publish_missing(capture, destination)
        except FileExistsError:
            return _result(project, record, 'preserved', False, 'restore_collision')
        return _result(project, record, 'preserved', False, 'restored_missing_destination')
    if current != record['digest']:
        if capture.exists():
            return _result(project, record, 'preserved', False, 'destination_changed')
        if current != record['expected_old']:
            if not prepared.parent.exists():
                # A human may archive the whole named recovery directory after
                # reviewing its files. This does not prove publication; normal
                # edit reconciliation must examine the destination afterward.
                return _result(project, record, 'clean', False, 'recovery_archived')
            return _result(project, record, 'preserved', False, 'destination_changed')
        _cleanup_clean(prepared)
        return _result(project, record, 'clean', False, 'not_published')
    if capture.exists():
        if not _WINDOWS:
            return _result(project, record, 'preserved', True, 'portable_retained')
        status = _inspect_capture_windows(capture, record['expected_old'])
        if status != 'clean':
            return _result(project, record, status, True,
                           'capture_busy' if status == 'busy' else 'capture_changed')
    _cleanup_clean(prepared)
    return _result(project, record, 'clean', True, 'published')


def publish_record(project, record, stop=None):
    """Publish once. Partial native failures escape with recovery files intact."""
    destination, prepared, capture = _paths(project, record)
    if stop is not None and stop.is_set():
        from .runner import Cancelled
        raise Cancelled('已停止字幕发布；准备文件和恢复记录保留')
    if capture.exists() or not prepared.exists():
        return recover_record(project, record, stop)
    if destination.exists() and _hash(destination) == record['digest']:
        return recover_record(project, record, stop)
    if record['expected_old'] is None:
        try:
            _publish_missing(prepared, destination)
        except FileExistsError:
            return _result(project, record, 'preserved', False, 'destination_created')
    elif not destination.exists():
        return _result(project, record, 'preserved', False, 'destination_deleted')
    elif _WINDOWS:
        for attempt in range(6):
            try:
                _replace_windows(destination, prepared, capture)
                break
            except PermissionError as error:
                if (getattr(error, 'winerror', None) not in (5, 32, 33)
                        or attempt == 5 or capture.exists()):
                    raise
                delay = .02 * 2 ** attempt
                if stop is not None:
                    if stop.wait(delay):
                        from .runner import Cancelled
                        raise Cancelled('已停止字幕发布重试，准备文件和原字幕保留') from error
                else:
                    time.sleep(delay)
                validate_record(project, record)
    else:
        # The capture directory is unique and owned by this record. Rename
        # retains the old object, and no-clobber publication protects a creator
        # that wins the intentionally visible gap. POSIX old fds stay writable.
        os.rename(destination, capture)
        try:
            _publish_missing(prepared, destination)
        except FileExistsError:
            return _result(project, record, 'preserved', False, 'destination_created')
    return recover_record(project, record, stop)
