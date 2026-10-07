"""Bulk hashing must keep each ordered proof and fail closed on partial output."""
from copy import deepcopy
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, call, patch

from subtitle_pipeline import audio_integrity as audio
from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline.local_process import OutputLimitExceeded
from subtitle_pipeline.runner import Cancelled
from tests.test_audio_integrity import stream


HASHES = ['SHA256=' + 'a' * 64, 'SHA256=' + 'b' * 64]
STDOUT = '\n'.join(f'{index},a,{digest}' for index, digest in enumerate(HASHES)) + '\n'


class AudioStreamHashParserTests(unittest.TestCase):
    def parse(self, value=STDOUT, count=2):
        return audio.parse_audio_stream_hashes(value, count)

    def test_preserves_output_mapping_order_and_normalizes_only_hash_case(self):
        self.assertEqual(self.parse(' 0,a,SHA256=' + 'A' * 64 + '\r\n\n1,a,' + HASHES[1]), HASHES)

    def test_equal_and_empty_payload_hashes_are_real_separate_tracks(self):
        empty = 'SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        self.assertEqual(self.parse(f'0,a,{empty}\n1,a,{empty}\n'), [empty, empty])

    def test_invalid_expected_count_is_never_inferred_from_stdout(self):
        for count in (None, True, False, 0, -1, 2.0, '2'):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.parse(count=count)

    def test_rejects_missing_extra_duplicate_reordered_and_non_audio_records(self):
        invalid = ['', '0,a,' + HASHES[0], STDOUT + '2,a,' + HASHES[0],
                   '0,a,' + HASHES[0] + '\n0,a,' + HASHES[1],
                   '1,a,' + HASHES[0] + '\n0,a,' + HASHES[1],
                   STDOUT.replace('1,a,', '1,v,'), STDOUT.replace('1,a,', '01,a,'),
                   STDOUT.replace('1,a,', '١,a,'), '# header\n' + STDOUT]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(value)

    def test_rejects_bad_algorithm_hash_or_non_string_output(self):
        for value in (None, b'0,a,' + HASHES[0].encode(), [],
                      STDOUT.replace('SHA256', 'MD5'), STDOUT.replace('a' * 64, 'a' * 63),
                      STDOUT.replace('a' * 64, 'z' * 64), STDOUT + 'unexpected diagnostics'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(value)


class BulkAudioCaptureTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.source = Path('synthetic-two-audio.mkv')

    def digest(self, count=2):
        return workflow.audio_digests(self.source, self.stop, expected_count=count)

    def test_one_capture_maps_every_audio_stream_with_packet_copy(self):
        completed = subprocess.CompletedProcess([], 0, stdout=STDOUT, stderr='')
        with patch.object(workflow, 'capture_process', return_value=completed) as capture:
            self.assertEqual(self.digest(), HASHES)
        capture.assert_called_once_with([
            'ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.source), '-map', '0:a',
            '-c:a', 'copy', '-f', 'streamhash', '-hash', 'sha256', '-'],
            stop=self.stop, timeout=300)

    def test_invalid_count_or_preexisting_stop_does_not_launch_capture(self):
        with patch.object(workflow, 'capture_process') as capture:
            for count in (True, 0, -2, '2'):
                with self.subTest(count=count), self.assertRaises(ValueError):
                    self.digest(count)
            self.stop.set()
            with self.assertRaises(Cancelled):
                self.digest()
        capture.assert_not_called()

    def test_capture_errors_keep_identity_and_do_not_return_partial_proof(self):
        failures = [Cancelled('stop during hashing'), subprocess.TimeoutExpired(['ffmpeg'], 300),
                    subprocess.CalledProcessError(1, ['ffmpeg'], output=STDOUT),
                    OSError('read failure'), OutputLimitExceeded(20)]
        for failure in failures:
            with self.subTest(failure=failure), patch.object(workflow, 'capture_process', side_effect=failure):
                with self.assertRaises(type(failure)) as caught:
                    self.digest()
                self.assertIs(caught.exception, failure)

    def test_stop_during_capture_rejects_even_complete_hash_output(self):
        def capture(*_args, **_kwargs):
            self.stop.set()
            return subprocess.CompletedProcess([], 0, stdout=STDOUT, stderr='')
        with patch.object(workflow, 'capture_process', side_effect=capture), self.assertRaises(Cancelled):
            self.digest()

    def test_successful_exit_with_truncated_records_is_not_success(self):
        with patch.object(workflow, 'capture_process', return_value=subprocess.CompletedProcess(
                [], 0, stdout='0,a,' + HASHES[0], stderr='')), self.assertRaises(ValueError):
            self.digest()

    def test_stop_while_parsing_final_output_does_not_return_success(self):
        def parse(_text, _count):
            self.stop.set()
            return list(HASHES)
        with patch.object(workflow,'capture_process',return_value=subprocess.CompletedProcess(
                [],0,stdout=STDOUT,stderr='')),patch.object(workflow,'parse_audio_stream_hashes',side_effect=parse):
            with self.assertRaises(Cancelled):
                self.digest()


class BulkAudioIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.info = {'streams': [{'codec_type': 'video'}, stream(), stream('jpn', 1)]}
        self.source = Path('source.mkv')
        self.output = Path('output.mp4')
        self.stop = threading.Event()
        self.legacy = Mock(side_effect=lambda _path, _stop, *, index: HASHES[index])
        self.bulk = Mock(return_value=list(HASHES))

    def verify(self, output_info=None):
        return audio.verify_audio_tracks(self.info, output_info or deepcopy(self.info),
            self.source, self.output, self.stop, self.legacy, digests=self.bulk)

    def test_two_bulk_reads_produce_same_existing_versioned_proof(self):
        expected = audio.verify_audio_tracks(self.info, deepcopy(self.info),
            self.source, self.output, self.stop, self.legacy)
        self.legacy.reset_mock()
        actual = self.verify()
        self.assertEqual(actual, expected)
        self.bulk.assert_has_calls([call(self.source, self.stop, expected_count=2),
                                   call(self.output, self.stop, expected_count=2)])
        self.assertEqual(self.bulk.call_count, 2)
        self.legacy.assert_not_called()
        self.assertTrue(audio.audio_evidence_matches(actual, self.info))

    def test_bad_metadata_or_track_count_fails_before_bulk_reads(self):
        for streams in ([stream()], [stream('jpn', 1), stream()], [stream(), stream('jpn', 0)]):
            with self.subTest(streams=streams), self.assertRaises(ValueError):
                self.verify({'streams': streams})
        self.bulk.assert_not_called()

    def test_bad_bulk_inventory_or_digest_never_becomes_valid_proof(self):
        for hashes in (None, HASHES[0], (), [], HASHES[:1], HASHES + [HASHES[0]],
                       [HASHES[0], 'wrong'], [HASHES[0], None]):
            with self.subTest(hashes=hashes):
                self.bulk.return_value = hashes
                with self.assertRaises(ValueError):
                    self.verify()

    def test_second_track_change_cannot_hide_behind_matching_first_track(self):
        self.bulk.side_effect = [list(HASHES), [HASHES[0], 'SHA256=' + 'f' * 64]]
        with self.assertRaisesRegex(ValueError, '2'):
            self.verify()

    def test_cancellation_in_last_bulk_read_discards_success(self):
        def bulk(path, _stop, *, expected_count):
            if path == self.output:
                self.stop.set()
            return list(HASHES)
        self.bulk.side_effect = bulk
        with self.assertRaises(Cancelled):
            self.verify()

    def test_bulk_io_failure_is_not_retried_per_track(self):
        failure = OSError('bulk source read interrupted')
        self.bulk.side_effect = failure
        with self.assertRaises(OSError) as caught:
            self.verify()
        self.assertIs(caught.exception, failure)
        self.bulk.assert_called_once()
        self.legacy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
