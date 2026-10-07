"""PCM coverage checks use synthetic WAV files and never read user audio."""

from fractions import Fraction
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from subtitle_pipeline.runner import Cancelled

try:
    audio_range = importlib.import_module('subtitle_pipeline.audio_range')
except ModuleNotFoundError:
    audio_range = None


class ObservedFile:
    def __init__(self, stream, after_read=None):
        self.stream = stream
        self.after_read = after_read
        self.reads = []
        self.seeks = []

    def read(self, size=-1):
        position = self.stream.tell()
        data = self.stream.read(size)
        self.reads.append((position, size, len(data)))
        if self.after_read:
            self.after_read(position, data)
        return data

    def seek(self, offset, whence=0):
        self.seeks.append((offset, whence))
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()

    def fileno(self):
        return self.stream.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class PCMPlanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(audio_range, 'PCM coverage helper is not implemented')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'timeline.wav'
        self.stop = threading.Event()

    def make_wav(self, frames, *, rate=16000, channels=1, width=2):
        with wave.open(str(self.path), 'wb') as stream:
            stream.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0' * (frames * channels * width))
        return self.path

    def check(self, planned_ms):
        return audio_range.check_pcm_plan(self.path, planned_ms, self.stop)

    def test_one_second_plan_gap_rejected_with_actionable_preserving_message(self):
        self.make_wav(8021 * 16)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError) as caught:
            self.check(7021)
        for text in ('7021', '8021', '250', '未启动新识别', '保留'):
            self.assertIn(text, str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)

    def test_fractional_pcm_duration_preserved_in_json_safe_evidence(self):
        self.make_wav(128341)  # 8021.3125 ms, never rounded for comparison.
        result = self.check(8022)
        self.assertEqual(result, {
            'version': 1, 'planned_ms': 8022, 'pcm_frames': 128341,
            'pcm_rate': 16000, 'pcm_ms_num': 128341, 'pcm_ms_den': 16,
            'delta_ms_num': -11, 'delta_ms_den': 16, 'tolerance_ms': 250,
        })
        self.assertEqual(json.loads(json.dumps(result)), result)
        result = self.check(8000)
        self.assertEqual(Fraction(result['delta_ms_num'], result['delta_ms_den']), Fraction(341, 16))

    def test_exact_duration_matches(self):
        self.make_wav(128000)
        result = self.check(8000)
        self.assertEqual((result['delta_ms_num'], result['delta_ms_den']), (0, 1))

    def test_exact_positive_and_negative_tolerance_allowed(self):
        for frames, delta in ((124000, -250), (132000, 250)):
            with self.subTest(frames=frames):
                self.make_wav(frames)
                result = self.check(8000)
                self.assertEqual(Fraction(result['delta_ms_num'], result['delta_ms_den']), delta)

    def test_one_sample_beyond_either_tolerance_rejected_without_rounding(self):
        for frames in (123999, 132001):
            with self.subTest(frames=frames):
                self.make_wav(frames)
                with self.assertRaisesRegex(ValueError, '250'):
                    self.check(8000)

    def test_invalid_plan_is_rejected_before_opening_audio(self):
        for planned in (True, False, 0, -1, 8000.0, '8000', None, Fraction(8000)):
            with self.subTest(planned=planned), patch.object(Path, 'open') as opened:
                with self.assertRaisesRegex(ValueError, '正整数'):
                    self.check(planned)
                opened.assert_not_called()

    def test_rejects_empty_pcm_even_with_plausibly_small_plan(self):
        self.make_wav(0)
        with self.assertRaisesRegex(ValueError, '帧'):
            self.check(1)

    def test_wrong_sample_rate_channels_width_and_compression_rejected(self):
        for options in ({'rate': 8000}, {'channels': 2}, {'width': 1}):
            with self.subTest(options=options):
                self.make_wav(128000, **options)
                with self.assertRaisesRegex(ValueError, '16000|16k|16 k|16 bit'):
                    self.check(8000)
        self.make_wav(128000)
        data = bytearray(self.path.read_bytes())
        data[20:22] = (3).to_bytes(2, 'little')  # WAVE_FORMAT_IEEE_FLOAT.
        self.path.write_bytes(data)
        with self.assertRaises(ValueError):
            self.check(8000)

    def test_declared_last_frame_must_physically_exist_in_full(self):
        for missing in (1, 2, 100):
            with self.subTest(missing=missing):
                self.make_wav(128000)
                with self.path.open('r+b') as stream:
                    stream.truncate(self.path.stat().st_size - missing)
                with self.assertRaisesRegex(ValueError, '截断|不完整'):
                    self.check(8000)

    def test_malformed_header_is_rejected_as_value_error(self):
        for data in (b'', b'RIFF\x24\0\0\0WAVEfmt ', b'not a WAV'):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.check(8000)

    def test_declared_sample_bits_must_be_sixteen_not_wave_rounded_width(self):
        for bits in (9, 15):
            with self.subTest(bits=bits):
                self.make_wav(128000)
                data = bytearray(self.path.read_bytes())
                data[34:36] = bits.to_bytes(2, 'little')
                self.path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, '16'):
                    self.check(8000)

    def test_raw_byte_rate_and_block_align_must_match_declared_pcm(self):
        for start, length, value in ((28, 4, 16000), (28, 4, 0), (32, 2, 1), (32, 2, 4)):
            with self.subTest(start=start, value=value):
                self.make_wav(128000)
                data = bytearray(self.path.read_bytes())
                data[start:start + length] = value.to_bytes(length, 'little')
                self.path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'PCM'):
                    self.check(8000)

    def test_extensible_pcm_format_keeps_wave_guid_validation(self):
        for valid_guid in (True, False):
            with self.subTest(valid_guid=valid_guid):
                self.make_wav(128000)
                data = bytearray(self.path.read_bytes())
                data[16:20] = (40).to_bytes(4, 'little')
                data[20:22] = (65534).to_bytes(2, 'little')
                guid = bytes.fromhex('0100000000001000800000aa00389b71')
                if not valid_guid:
                    guid = b'\3' + guid[1:]
                extension = (22).to_bytes(2, 'little') + (16).to_bytes(2, 'little') + (4).to_bytes(4, 'little') + guid
                data[36:36] = extension
                data[4:8] = (len(data) - 8).to_bytes(4, 'little')
                self.path.write_bytes(data)
                if valid_guid:
                    self.assertEqual(self.check(8000)['pcm_frames'], 128000)
                else:
                    with self.assertRaises(ValueError):
                        self.check(8000)

    def test_odd_data_length_is_not_silently_rounded_to_complete_frames(self):
        for physical_extra_byte in (False, True):
            with self.subTest(physical_extra_byte=physical_extra_byte):
                self.make_wav(128000)
                data = bytearray(self.path.read_bytes())
                data[40:44] = (256001).to_bytes(4, 'little')
                if physical_extra_byte:
                    data.extend(b'\0\0')
                    data[4:8] = (len(data) - 8).to_bytes(4, 'little')
                self.path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, '完整|帧'):
                    self.check(8000)

    def test_preexisting_cancel_does_not_open_audio(self):
        self.stop.set()
        with patch.object(Path, 'open') as opened:
            with self.assertRaises(Cancelled):
                self.check(8000)
            opened.assert_not_called()

    def test_cancel_during_header_read_stops_before_another_read(self):
        self.make_wav(128000)
        with self.path.open('rb') as stream:
            observed = ObservedFile(stream, lambda *_: self.stop.set())
            with patch.object(Path, 'open', return_value=observed):
                with self.assertRaises(Cancelled):
                    self.check(8000)
            self.assertTrue(stream.closed)
            self.assertEqual(len(observed.reads), 1)

    def test_cancel_after_last_pcm_frame_read_cannot_return_evidence(self):
        self.make_wav(128000)
        last = self.path.stat().st_size - 2
        def after_read(position, data):
            if position == last:
                self.stop.set()
        with self.path.open('rb') as stream:
            observed = ObservedFile(stream, after_read)
            with patch.object(Path, 'open', return_value=observed):
                with self.assertRaises(Cancelled):
                    self.check(8000)
            self.assertEqual(observed.reads[-1], (last, 2, 2))
            self.assertTrue(stream.closed)

    def test_cancel_and_io_failure_during_read_preserves_cancellation(self):
        self.make_wav(128000)
        for cancel in (True, False):
            with self.subTest(cancel=cancel), self.path.open('rb') as stream:
                self.stop.clear()
                failure = OSError('synthetic read failed')
                observed = ObservedFile(stream)
                def fail_read(size):
                    if cancel:
                        self.stop.set()
                    raise failure
                observed.read = fail_read
                with patch.object(Path, 'open', return_value=observed):
                    if cancel:
                        with self.assertRaises(Cancelled) as caught:
                            self.check(8000)
                        self.assertIs(caught.exception.__cause__, failure)
                    else:
                        with self.assertRaises(OSError) as caught:
                            self.check(8000)
                        self.assertIs(caught.exception, failure)
                self.assertTrue(stream.closed)

    def test_metadata_io_errors_convert_to_cancelled_only_after_stop(self):
        self.make_wav(128000)
        for operation in ('stat', 'open', 'fstat', 'tell'):
            for cancel in (True, False):
                with self.subTest(operation=operation, cancel=cancel):
                    self.stop.clear()
                    failure = OSError('synthetic ' + operation + ' failed')
                    def fail(*args, **kwargs):
                        if cancel:
                            self.stop.set()
                        raise failure
                    with self.path.open('rb') as stream:
                        observed = ObservedFile(stream)
                        target = Path if operation in ('stat', 'open') else (audio_range.os if operation == 'fstat' else observed)
                        with patch.object(Path, 'open', return_value=observed), patch.object(target, operation, side_effect=fail):
                            if cancel:
                                with self.assertRaises(Cancelled) as caught:
                                    self.check(8000)
                                self.assertIs(caught.exception.__cause__, failure)
                            else:
                                with self.assertRaises(OSError) as caught:
                                    self.check(8000)
                                self.assertIs(caught.exception, failure)
                    self.assertTrue(stream.closed)

    def test_cancel_after_final_path_stat_cannot_return_evidence(self):
        self.make_wav(128000)
        original = Path.stat
        calls = []
        def stat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            calls.append(path)
            if len(calls) == 2:
                self.stop.set()
            return value
        with patch.object(Path, 'stat', stat):
            with self.assertRaises(Cancelled):
                self.check(8000)

    def test_metadata_change_while_reading_refuses_evidence(self):
        self.make_wav(128000)
        original = self.path.stat()
        def after_read(position, data):
            if position == original.st_size - 2:
                os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns + 10_000_000))
        with self.path.open('rb') as stream:
            observed = ObservedFile(stream, after_read)
            with patch.object(Path, 'open', return_value=observed):
                with self.assertRaisesRegex(ValueError, '变化|改变'):
                    self.check(8000)

    def test_replaced_path_stat_does_not_match_still_open_file(self):
        self.make_wav(128000)
        original = self.path.stat()
        fields = {name: getattr(original, name) for name in
                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        from types import SimpleNamespace
        changed = SimpleNamespace(**{**fields, 'st_ino': fields['st_ino'] + 1})
        with patch.object(Path, 'stat', side_effect=[original, changed]):
            with self.assertRaisesRegex(ValueError, '变化|改变'):
                self.check(8000)

    def test_same_open_file_is_checked_again_after_reading(self):
        self.make_wav(128000)
        original = self.path.stat()
        fields = {name: getattr(original, name) for name in
                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        from types import SimpleNamespace
        changed = SimpleNamespace(**{**fields, 'st_size': fields['st_size'] + 2})
        with patch.object(audio_range.os, 'fstat', side_effect=[original, changed]):
            with self.assertRaisesRegex(ValueError, '变化|改变'):
                self.check(8000)

    def test_path_and_descriptor_ctime_representations_may_differ(self):
        self.make_wav(128000)
        original = self.path.stat()
        fields = {name: getattr(original, name) for name in
                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        from types import SimpleNamespace
        descriptor = SimpleNamespace(**{**fields, 'st_ctime_ns': fields['st_ctime_ns'] + 10_000_000})
        with patch.object(audio_range.os, 'fstat', return_value=descriptor):
            self.assertEqual(self.check(8000)['delta_ms_num'], 0)

    def test_pcm_payload_is_seeked_over_not_scanned_or_hashed(self):
        self.make_wav(320000)
        size = self.path.stat().st_size
        with self.path.open('rb') as stream:
            observed = ObservedFile(stream)
            with patch.object(Path, 'open', return_value=observed), patch('hashlib.sha256') as digest:
                result = self.check(20000)
            self.assertEqual(result['pcm_frames'], 320000)
            digest.assert_not_called()
            self.assertTrue(stream.closed)
        self.assertTrue(all(0 < count <= 16 for _, count, _ in observed.reads))
        self.assertLess(sum(count for _, _, count in observed.reads), 128)
        self.assertEqual(observed.reads[-1], (size - 2, 2, 2))
        self.assertTrue(any(offset >= size - 2 for offset, whence in observed.seeks if whence == 0))

    def test_unrelated_chunk_payload_is_seeked_over_with_riff_padding(self):
        self.make_wav(128000)
        data = bytearray(self.path.read_bytes())
        junk = b'JUNK' + (65537).to_bytes(4, 'little') + b'x' * 65537 + b'\0'
        data[12:12] = junk
        data[4:8] = (len(data) - 8).to_bytes(4, 'little')
        self.path.write_bytes(data)
        with self.path.open('rb') as stream:
            observed = ObservedFile(stream)
            with patch.object(Path, 'open', return_value=observed):
                self.assertEqual(self.check(8000)['pcm_frames'], 128000)
        self.assertTrue(all(0 < count <= 16 for _, count, _ in observed.reads))
        self.assertLess(sum(count for _, _, count in observed.reads), 128)

    def test_filesystem_error_is_not_reported_as_success_or_format_error(self):
        self.make_wav(128000)
        failure = PermissionError('synthetic access denied')
        with patch.object(Path, 'open', side_effect=failure):
            with self.assertRaises(PermissionError) as caught:
                self.check(8000)
            self.assertIs(caught.exception, failure)


if __name__ == '__main__':
    unittest.main()
