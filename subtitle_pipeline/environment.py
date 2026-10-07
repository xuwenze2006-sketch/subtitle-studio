"""Offline, bounded checks for the local subtitle and video toolchain.

Only a few generated frames and an owned temporary subtitle are used. No
provider settings, credentials, network requests, or user media are inspected.
Raw child output and absolute installation paths never enter the report.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import runner as r
from .local_process import capture_process, OutputLimitExceeded

PROBE_TIMEOUT_SECONDS = 5
PROBE_TOTAL_SECONDS = 20


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise r.Cancelled('已停止本机环境检查')


def _result(available, message, **details):
    return {'available': bool(available), 'message': message, **details}


def _capture(args, stop, *, cwd=None, deadline=None):
    _check_stop(stop)
    remaining = PROBE_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, PROBE_TOTAL_SECONDS)
    result = capture_process(args, stop=stop, timeout=min(PROBE_TIMEOUT_SECONDS, remaining),
                             max_output_bytes=256 * 1024, cwd=cwd)
    _check_stop(stop)
    return result


def _tool_version(name, stop, *, deadline=None):
    if not shutil.which(name):
        return _result(False, name + ' 未安装或不在 PATH 中')
    try:
        result = _capture([name, '-version'], stop, deadline=deadline)
        found = re.search(r'\b' + name + r' version ([\w.+-]{1,80})', result.stdout)
        if not found:
            return _result(False, name + ' 版本输出无法识别')
        return _result(True, name + ' 可运行', version=found.group(1))
    except (OSError, subprocess.SubprocessError, OutputLimitExceeded):
        _check_stop(stop)
        return _result(False, name + ' 无法运行或检查超时')


def _frame_args():
    return ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error',
            '-f', 'lavfi', '-i', 'color=c=black:s=128x128:r=25',
            '-frames:v', '2', '-an', '-threads', '2', '-filter_threads', '1']


def _encoder_trial(encoder, stop, *, deadline=None):
    """A declared encoder is insufficient: initialize it and encode two frames."""
    args = _frame_args()
    if encoder == 'qsv':
        args += ['-vf', 'format=nv12', '-c:v', 'h264_qsv',
                 '-global_quality', '20', '-preset', 'fast', '-async_depth', '8']
    else:
        args += ['-vf', 'format=yuv420p', '-c:v', 'libx264',
                 '-preset', 'ultrafast', '-crf', '20']
    args += ['-f', 'null', '-']
    label = 'QSV' if encoder == 'qsv' else 'CPU (libx264)'
    try:
        _capture(args, stop, deadline=deadline)
        return _result(True, label + ' 已通过本机短编码探测')
    except (OSError, subprocess.SubprocessError, OutputLimitExceeded):
        _check_stop(stop)
        return _result(False, label + ' 短编码探测失败或超时')


def _subtitle_trial(stop, *, deadline=None):
    try:
        with tempfile.TemporaryDirectory(prefix='subtitle-environment-') as temporary:
            (Path(temporary) / 'captions.srt').write_text(
                '1\n00:00:00,000 --> 00:00:01,000\nSubtitle check\n', encoding='utf-8')
            _capture(_frame_args() + ['-vf', 'subtitles=captions.srt', '-f', 'null', '-'],
                     stop, cwd=temporary, deadline=deadline)
        return _result(True, 'subtitles / libass 已通过本机字幕滤镜探测')
    except (OSError, subprocess.SubprocessError, OutputLimitExceeded):
        _check_stop(stop)
        return _result(False, 'subtitles / libass 字幕滤镜不可用或检查超时')


def _tk_check():
    try:
        import tkinter
    except ImportError:
        return _result(False, 'Tk 模块或 Tcl 运行时不可用')
    try:
        runtime = tkinter.Tcl()
        patchlevel = runtime.eval('info patchlevel')
        return _result(True, 'Tk 模块和 Tcl 可运行（未启动窗口）',
                       version=str(tkinter.TkVersion), tcl_version=patchlevel)
    except (tkinter.TclError, RuntimeError, OSError):
        return _result(False, 'Tk 模块或 Tcl 运行时不可用')


def select_encoder(encoder='auto', stop=None):
    """Choose before encoding; an encode failure never triggers a second encode."""
    if not isinstance(encoder, str) or encoder not in ('auto', 'qsv', 'cpu'):
        raise ValueError('编码方式须为 auto、qsv 或 cpu')
    _check_stop(stop)
    deadline = time.monotonic() + PROBE_TOTAL_SECONDS
    if not _tool_version('ffmpeg', stop, deadline=deadline)['available']:
        raise ValueError('FFmpeg 不可运行，无法导出视频；请刷新环境自检')
    if encoder in ('auto', 'qsv'):
        if _encoder_trial('qsv', stop, deadline=deadline)['available']:
            return 'qsv'
        if encoder == 'qsv':
            raise ValueError('QSV 本机短编码探测失败；请检查驱动或明确选择 CPU 编码')
    if _encoder_trial('cpu', stop, deadline=deadline)['available']:
        return 'cpu'
    raise ValueError('CPU (libx264) 本机短编码探测失败；当前没有可用的视频编码器')


def probe_environment(stop=None) -> dict:
    """Report actual local capabilities, without accessing a cloud provider."""
    _check_stop(stop)
    deadline = time.monotonic() + PROBE_TOTAL_SECONDS
    checks = {'python': _result(True, 'Python 可运行', version='.'.join(map(str, sys.version_info[:3])))}
    checks['tk'] = _tk_check()
    _check_stop(stop)
    checks['ffmpeg'] = _tool_version('ffmpeg', stop, deadline=deadline)
    checks['ffprobe'] = _tool_version('ffprobe', stop, deadline=deadline)
    if checks['ffmpeg']['available']:
        checks['subtitles'] = _subtitle_trial(stop, deadline=deadline)
        checks['qsv'] = _encoder_trial('qsv', stop, deadline=deadline)
        checks['cpu'] = _encoder_trial('cpu', stop, deadline=deadline)
    else:
        for key, label in (('subtitles', 'subtitles / libass'), ('qsv', 'QSV'), ('cpu', 'CPU (libx264)')):
            checks[key] = _result(False, label + ' 未检查：FFmpeg 不可用')
    checks['libass'] = dict(checks['subtitles'])
    try:
        executable, model = r.engine_paths()
        available = executable.stat().st_size > 0 and model.stat().st_size > 0
        checks['whisper'] = _result(available,
            'Whisper CPP / small 模型文件已安装（未加载模型）' if available else 'Whisper CPP / small 模型文件为空')
    except (OSError, ValueError):
        checks['whisper'] = _result(False, '未找到已安装的 Whisper CPP / small 模型')
    _check_stop(stop)
    recommended = 'qsv' if checks['qsv']['available'] else 'cpu' if checks['cpu']['available'] else None
    return {'version': 1, 'checked_at': time.time(), 'checks': checks,
            'timeout_seconds': PROBE_TOTAL_SECONDS,
            'per_process_timeout_seconds': PROBE_TIMEOUT_SECONDS,
            'recommended_encoder': recommended,
            'export_ready': bool(checks['ffmpeg']['available'] and checks['ffprobe']['available']
                                 and checks['subtitles']['available'] and recommended)}
