"""Bounded, local FFmpeg progress reporting for video exports."""
from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
import threading
import time


PHASE_MESSAGES = {
    'preparing': '正在准备视频导出…',
    'encoding': '正在编码字幕视频…',
    'validating': '正在校验音轨与视频…',
    'publishing': '正在保存成片…',
    'done': '字幕视频已保存。',
}


def _number(value, *, maximum=1_000_000_000, minimum=0):
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def sanitize_export_progress(value):
    """Keep only supported phases and JSON-safe, bounded numeric values."""
    if (not isinstance(value, dict) or not isinstance(value.get('phase'), str)
            or value['phase'] not in PHASE_MESSAGES):
        return None
    phase = value['phase']
    duration = _number(value.get('duration_seconds')) or 0.0
    encoded = _number(value.get('encoded_seconds')) or 0.0
    if duration:
        encoded = min(encoded, duration)
    percent = _number(value.get('percent'), minimum=-1_000_000_000)
    percent = min(100.0, max(0.0, percent)) if percent is not None else None
    speed = _number(value.get('speed'), maximum=1_000_000)
    speed = speed if speed and phase == 'encoding' else None
    return {
        'phase': phase,
        'percent': (100.0 if phase == 'done' else percent if phase == 'encoding' else None),
        'encoded_seconds': encoded,
        'duration_seconds': duration,
        'speed': speed,
        'eta_seconds': _number(value.get('eta_seconds')) if phase == 'encoding' else None,
        'elapsed_seconds': _number(value.get('elapsed_seconds')) or 0.0,
    }


def _parsed_number(value, **bounds):
    try:
        return _number(float(value), **bounds)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_ffmpeg_progress(text):
    """Read the last block closed by progress=continue/end, ignoring torn writes."""
    block = {}
    complete = None
    for line in text.splitlines():
        key, separator, value = line.partition('=')
        if not separator:
            continue
        if key == 'progress':
            if value in ('continue', 'end'):
                complete = block
            block = {}
        else:
            block[key] = value.strip()
    if complete is None:
        return None
    micros = _parsed_number(complete.get('out_time_us'), maximum=1_000_000_000_000_000)
    seconds = micros / 1_000_000 if micros is not None else None
    if seconds is None:
        fields = complete.get('out_time', '').split(':')
        if len(fields) == 3:
            hours = _parsed_number(fields[0])
            minutes = _parsed_number(fields[1], maximum=59)
            fraction = _parsed_number(fields[2], maximum=60)
            if hours is not None and minutes is not None and fraction is not None:
                seconds = _number(hours * 3600 + minutes * 60 + fraction)
    if seconds is None:
        return None
    speed = _parsed_number(complete.get('speed', '').removesuffix('x').strip(), maximum=1_000_000)
    return {'encoded_seconds': seconds, 'speed': speed or None}


class ExportProgress:
    def __init__(self, duration_seconds, emit, *, poll_interval=1.0):
        self.duration = _number(duration_seconds) or 0.0
        self.emit = emit
        self.poll_interval = max(0.01, min(float(poll_interval), 5.0))
        self.started = time.monotonic()
        self.encoded = 0.0
        self._last_values = None

    def _report(self, phase, speed=None):
        remaining = max(0.0, self.duration - self.encoded)
        payload = sanitize_export_progress({
            'phase': phase,
            'percent': self.encoded / self.duration * 100 if self.duration else None,
            'encoded_seconds': self.encoded,
            'duration_seconds': self.duration,
            'speed': speed,
            'eta_seconds': remaining / speed if speed and self.duration else None,
            'elapsed_seconds': max(0.0, time.monotonic() - self.started),
        })
        self.emit(PHASE_MESSAGES[phase], export_progress=payload)

    def stage(self, phase):
        if phase not in PHASE_MESSAGES:
            raise ValueError('不支持的视频导出阶段')
        self._report(phase)

    def _read(self, path):
        try:
            with Path(path).open('rb') as stream:
                size = stream.seek(0, 2)
                offset = max(0, size - 65536)
                stream.seek(offset)
                data = stream.read(65536)
            if offset:
                data = data.partition(b'\n')[2]
            parsed = parse_ffmpeg_progress(data.decode('ascii', errors='replace'))
        except OSError:
            return
        if parsed is None:
            return
        encoded = max(self.encoded, parsed['encoded_seconds'])
        self.encoded = min(encoded, self.duration) if self.duration else encoded
        values = (self.encoded, parsed['speed'])
        if values != self._last_values:
            self._last_values = values
            self._report('encoding', parsed['speed'])

    @contextmanager
    def watch(self, progress_file):
        """Watch only while the caller runs FFmpeg; the caller clears stale files."""
        stopped = threading.Event()
        self.stage('encoding')

        def monitor():
            while not stopped.is_set():
                self._read(progress_file)
                stopped.wait(self.poll_interval)

        worker = threading.Thread(target=monitor, name='subtitle-export-progress', daemon=True)
        worker.start()
        try:
            yield self
        finally:
            stopped.set()
            worker.join()
            # Capture the final block even when a short encode finished between polls.
            self._read(progress_file)
