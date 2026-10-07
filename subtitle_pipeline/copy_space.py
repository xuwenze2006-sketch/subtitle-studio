"""Space preflight for one complete copy of an already encoded output."""

import errno
from pathlib import Path
import shutil


def require_copy_space(partial, final):
    """Reject known insufficient destination space without reserving or writing.

    Resolve the actual destination directory so junctions/mounts use their own
    volume. The encoded file already occupies its space; publication needs one
    additional copy even on the same volume. Metadata errors propagate intact.
    This is a snapshot, not a reservation: concurrent writes and allocation
    overhead can still cause copying to fail, so recovery remains necessary.
    """
    required = Path(partial).stat().st_size
    if type(required) is not int or required < 0:
        raise ValueError('已编码文件的大小无效，无法检查复制空间')
    destination = Path(final).parent.resolve(strict=True)
    available = shutil.disk_usage(destination).free
    if type(available) is not int or available < 0:
        raise ValueError('目标磁盘的可用空间统计无效，无法确认复制空间')
    if available < required:
        raise OSError(errno.ENOSPC,
                      f'目标磁盘空间不足：保存成片副本需要 {required:,} 字节，'
                      f'当前可用 {available:,} 字节；编码结果已保留', str(destination))
