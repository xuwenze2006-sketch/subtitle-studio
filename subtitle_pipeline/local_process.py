"""Cancellable, bounded capture of local media utility output."""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import time

from .runner import Cancelled
from .windows_job import owned_process


class OutputLimitExceeded(RuntimeError):
    """The combined stdout and stderr of a local child exceeded its allowance."""

    def __init__(self, limit):
        self.limit = limit
        super().__init__(f'本地进程输出超过 {limit} 字节上限，已停止进程')


def _reap(child):
    if child.poll() is None:
        try:
            child.terminate()
        except OSError:
            # It may have exited between poll() and terminate(). wait() below
            # confirms that, or escalates to kill if it is still alive.
            pass
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            child.wait()
    else:
        child.wait()


def _captured_size(stdout, stderr):
    return os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size


def _read_output(stdout, stderr, allowance):
    stdout.seek(0)
    stderr.seek(0)
    out = stdout.read(allowance)
    err = stderr.read(max(0, allowance - len(out)))
    return out.decode('utf-8', errors='replace'), err.decode('utf-8', errors='replace')


def capture_process(args, *, stop=None, timeout=60, max_output_bytes=4 * 1024 * 1024, cwd=None):
    """Run without a window; always reap the child before returning or raising.

    ``max_output_bytes`` is the combined stdout/stderr limit. Output is spooled
    to temporary files and checked at most every 50 ms, so noisy children cannot
    fill OS pipes or force unbounded in-memory capture. All returned/error output
    is decoded as UTF-8. A stop signal raises ``runner.Cancelled``; timeout and
    nonzero exits preserve subprocess exceptions, and excess output raises
    ``OutputLimitExceeded``.
    """
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('本地进程超时必须为正的有限秒数')
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError('本地进程输出上限必须为正整数')
    if isinstance(args, (str, bytes)):
        raise ValueError('本地进程参数必须使用参数列表')
    command = [str(arg) for arg in args]
    if stop is not None and stop.is_set():
        raise Cancelled('已停止本地媒体检查')
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile(mode='w+b') as stdout, tempfile.TemporaryFile(mode='w+b') as stderr:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                 cwd=cwd, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            with owned_process(child):
                try:
                    while True:
                        if stop is not None and stop.is_set():
                            raise Cancelled('已停止本地媒体检查')
                        if _captured_size(stdout, stderr) > max_output_bytes:
                            raise OutputLimitExceeded(max_output_bytes)
                        if child.poll() is not None:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        pause = min(0.05, remaining)
                        if stop is None:
                            time.sleep(pause)
                        else:
                            stop.wait(pause)
                finally:
                    if os.name != 'nt':
                        _reap(child)
        except subprocess.TimeoutExpired as error:
            error.output, error.stderr = _read_output(stdout, stderr, max_output_bytes)
            raise
        # A child can finish and append its last output between the loop's size
        # check and poll(). Check again before reading either spool into memory.
        if _captured_size(stdout, stderr) > max_output_bytes:
            raise OutputLimitExceeded(max_output_bytes)
        out, err = _read_output(stdout, stderr, max_output_bytes)
        result = subprocess.CompletedProcess(command, child.returncode, out, err)
        result.check_returncode()
        return result
