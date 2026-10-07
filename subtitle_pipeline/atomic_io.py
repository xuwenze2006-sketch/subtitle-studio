"""Bounded atomic publication and owned temporary-file cleanup."""

import os
from pathlib import Path
import sys
import time


def _retry_windows_permission_error(operation):
    for attempt in range(6):
        try:
            return operation()
        except PermissionError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 5:
                raise
            # Readers may briefly omit FILE_SHARE_DELETE. Only retry this
            # operation, for 0.62 s total; never repeat file writes or paid work.
            time.sleep(.02 * 2 ** attempt)


def replace_with_retry(source, destination):
    """Replace using the same prepared file; leave preparation/cleanup to callers."""
    return _retry_windows_permission_error(lambda: os.replace(source, destination))


def cleanup_temporary(path):
    """Delete exactly one caller-owned temporary, preserving an active failure.

    A failed cleanup in a caller's ``finally`` adds the remaining path to the
    original exception. Without an active exception, the cleanup error escapes.
    Only filesystem errors are handled; new interrupts and programming errors
    still propagate. No directory traversal or symlink resolution is performed.
    """
    primary_error = sys.exc_info()[1]
    temporary = Path(path).absolute()
    try:
        _retry_windows_permission_error(lambda: temporary.unlink(missing_ok=True))
    except OSError as cleanup_error:
        if primary_error is None:
            raise
        primary_error.add_note(
            f'临时文件清理失败，待清理路径：{temporary}；'
            f'{type(cleanup_error).__name__}: {cleanup_error}'
        )
