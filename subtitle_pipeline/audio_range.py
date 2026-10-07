"""Check PCM duration coverage, not source origin or subtitle alignment.

The caller must separately bind the already verified PCM contents to its current
plan. A 250 ms allowance is a bounded coverage guard, not a general timing rule.
"""

from fractions import Fraction
import os
from pathlib import Path
import wave


TOLERANCE_MS = 250
_PRESERVED = '未启动新识别，已有产物保留'


def _check_stop(stop, *, cause=None):
    if stop.is_set():
        # Keep this helper importable from runner without a circular import.
        from .runner import Cancelled
        raise Cancelled(f'已停止 PCM 长度校验，{_PRESERVED}') from cause


def _token(value):
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class _CheckedReader:
    def __init__(self, stream, stop):
        self.stream = stream
        self.stop = stop

    def read(self, size):
        _check_stop(self.stop)
        data = self.stream.read(size)
        _check_stop(self.stop)
        return data

    def seek(self, offset, whence=0):
        _check_stop(self.stop)
        result = self.stream.seek(offset, whence)
        _check_stop(self.stop)
        return result

    def tell(self):
        return self.stream.tell()


def _check_header(reader, file_size):
    """Inspect raw fields that wave rounds; skip chunk bodies without reading."""
    header = reader.read(12)
    if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
        raise ValueError(f'PCM WAV 头不完整或格式不支持，{_PRESERVED}')
    riff_end = 8 + int.from_bytes(header[4:8], 'little')
    while reader.tell() + 8 <= min(riff_end, file_size):
        chunk = reader.read(8)
        if len(chunk) != 8:
            break
        length = int.from_bytes(chunk[4:], 'little')
        end = reader.tell() + length
        if chunk[:4] == b'data' and length % 2:
            raise ValueError(f'PCM data 声明字节数不是完整 16 bit 帧，{_PRESERVED}')
        if end > min(riff_end, file_size):
            raise ValueError(f'PCM WAV 数据不完整，文件可能被截断，{_PRESERVED}')
        if chunk[:4] == b'fmt ':
            fmt = reader.read(min(length, 16))
            layout = tuple(int.from_bytes(fmt[first:last], 'little') for first, last in
                           ((2, 4), (4, 8), (8, 12), (12, 14), (14, 16)))
            if len(fmt) != 16 or layout != (1, 16000, 32000, 2, 16):
                raise ValueError(
                    f'PCM 必须声明为单声道、16000 Hz、16 bit、32000 bytes/s、帧宽 2 bytes，{_PRESERVED}'
                )
        if chunk[:4] == b'data':
            return
        reader.seek(end + length % 2)
    raise ValueError(f'PCM WAV 缺少完整 data 头，{_PRESERVED}')


def check_pcm_plan(path, planned_duration_ms, stop):
    """Return exact JSON-safe duration evidence after a header and tail read.

    No PCM payload scan, full hash, state write, or source-origin inference is
    performed. ``delta_ms`` is actual PCM duration minus the planned duration.
    """
    try:
        return _check_pcm_plan(path, planned_duration_ms, stop)
    except OSError as error:
        _check_stop(stop, cause=error)
        raise


def _check_pcm_plan(path, planned_duration_ms, stop):
    if type(planned_duration_ms) is not int or planned_duration_ms <= 0:
        raise ValueError(f'计划长度必须是正整数毫秒，{_PRESERVED}')
    _check_stop(stop)
    path = Path(path)
    before = _token(path.stat())
    _check_stop(stop)
    with path.open('rb') as stream:
        before_open = _token(os.fstat(stream.fileno()))
        # Windows path stat and descriptor fstat can expose different ctime
        # semantics. Compare each full token with its own later observation.
        if before_open[:4] != before[:4]:
            raise ValueError(f'PCM 文件在校验期间发生变化，{_PRESERVED}')
        try:
            reader = _CheckedReader(stream, stop)
            _check_header(reader, before_open[2])
            reader.seek(0)
            with wave.open(reader, 'rb') as pcm:
                if (pcm.getframerate(), pcm.getnchannels(), pcm.getsampwidth(), pcm.getcomptype()) != (16000, 1, 2, 'NONE'):
                    raise ValueError(f'PCM 必须是 16000 Hz、单声道、16 bit、NONE WAV，{_PRESERVED}')
                frames, rate = pcm.getnframes(), pcm.getframerate()
                if type(frames) is not int or frames <= 0:
                    raise ValueError(f'PCM 声明帧数必须为正，{_PRESERVED}')
                _check_stop(stop)
                pcm.setpos(frames - 1)
                if len(pcm.readframes(1)) != 2:
                    raise ValueError(f'PCM 最后一帧不完整，文件可能被截断，{_PRESERVED}')
                _check_stop(stop)
        except (wave.Error, EOFError) as error:
            _check_stop(stop)
            raise ValueError(f'PCM WAV 格式损坏或不支持，{_PRESERVED}') from error
        after_open = _token(os.fstat(stream.fileno()))
    after_path = _token(path.stat())
    _check_stop(stop)
    if before_open != after_open or before != after_path:
        raise ValueError(f'PCM 文件在校验期间发生变化，{_PRESERVED}')
    pcm_ms = Fraction(frames * 1000, rate)
    delta_ms = pcm_ms - planned_duration_ms
    if abs(delta_ms) > TOLERANCE_MS:
        actual = f'{float(pcm_ms):.4f}'.rstrip('0').rstrip('.')
        raise ValueError(
            f'计划长度 {planned_duration_ms} ms 与实际 PCM 长度 {actual} ms '
            f'相差超过 {TOLERANCE_MS} ms，{_PRESERVED}'
        )
    _check_stop(stop)
    return {
        'version': 1, 'planned_ms': planned_duration_ms,
        'pcm_frames': frames, 'pcm_rate': rate,
        'pcm_ms_num': pcm_ms.numerator, 'pcm_ms_den': pcm_ms.denominator,
        'delta_ms_num': delta_ms.numerator, 'delta_ms_den': delta_ms.denominator,
        'tolerance_ms': TOLERANCE_MS,
    }
