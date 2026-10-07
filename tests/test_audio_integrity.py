"""Offline ordered audio preservation evidence, without media subprocesses."""
import copy
import importlib
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, call

from subtitle_pipeline.runner import Cancelled

try:
    audio = importlib.import_module('subtitle_pipeline.audio_integrity')
except ModuleNotFoundError:
    audio = None


def stream(language='eng', default=0, **changes):
    result = {'codec_type': 'audio', 'codec_name': 'aac', 'sample_rate': '48000',
              'channels': 2, 'channel_layout': 'stereo', 'tags': {'language': language},
              'disposition': {'default': default, 'forced': 0}}
    result.update(changes)
    return result


class AudioIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(audio, 'ordered audio-integrity helper is not implemented')
        self.source = Path('synthetic-source.mp4')
        self.output = Path('synthetic-output.mp4')
        self.info = {'streams': [{'codec_type': 'video'}, stream(), stream('jpn', 1)]}
        self.stop = threading.Event()
        self.digest = Mock(side_effect=lambda path, stop, *, index: 'SHA256=' + ('A' if index == 0 else 'b') * 64)

    def verify(self, output_info=None):
        return audio.verify_audio_tracks(self.info, output_info or copy.deepcopy(self.info),
                                         self.source, self.output, self.stop, self.digest)

    def test_preserves_ordered_metadata_and_hashes_with_explicit_audio_indexes(self):
        result = self.verify()
        expected = [
            {'codec_name': 'aac', 'sample_rate': 48000, 'channels': 2, 'channel_layout': 'stereo',
             'language': 'eng', 'default': 0, 'forced': 0, 'sha256': 'a' * 64},
            {'codec_name': 'aac', 'sample_rate': 48000, 'channels': 2, 'channel_layout': 'stereo',
             'language': 'jpn', 'default': 1, 'forced': 0, 'sha256': 'b' * 64},
        ]
        self.assertEqual(result, {'version': 2, 'source': expected, 'output': expected,
                                  'default_policy': 'preserved'})
        self.digest.assert_has_calls([call(self.source, self.stop, index=0), call(self.output, self.stop, index=0),
                                      call(self.source, self.stop, index=1), call(self.output, self.stop, index=1)])
        self.assertTrue(audio.audio_evidence_matches(result, self.info))

    def test_descriptors_ignore_container_index_and_non_audio_streams(self):
        changed = copy.deepcopy(self.info)
        changed['streams'][1]['index'] = 15
        changed['streams'].insert(1, {'codec_type': 'subtitle', 'index': 14})
        self.assertEqual(audio.audio_track_descriptors(changed), audio.audio_track_descriptors(self.info))

    def test_normalizes_rates_missing_language_and_optional_layout(self):
        source = {'streams': [stream(tags={}, sample_rate='048000', channel_layout='')]}
        output = {'streams': [stream('und', sample_rate=48000, channel_layout='')]}
        del source['streams'][0]['channel_layout']
        result = audio.verify_audio_tracks(source, output, self.source, self.output, self.stop, self.digest)
        self.assertEqual(result['source'][0]['language'], 'und')
        self.assertEqual(result['source'][0]['sample_rate'], 48000)
        self.assertEqual(result['source'][0]['channel_layout'], '')
        self.assertTrue(audio.audio_evidence_matches(result, source))

    def test_missing_audio_or_malformed_stream_inventory_fails_before_hashing(self):
        for info in ({}, None, {'streams': None}, {'streams': {}}, {'streams': []},
                     {'streams': [{'codec_type': 'video'}]}, {'streams': [None]}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                audio.verify_audio_tracks(info, self.info, self.source, self.output, self.stop, self.digest)
        self.digest.assert_not_called()

    def test_dropped_or_added_track_fails_before_hashing(self):
        for tracks in ([stream()], [stream(), stream('jpn', 1), stream('deu')]):
            with self.subTest(count=len(tracks)), self.assertRaisesRegex(ValueError, '音轨'):
                self.verify({'streams': tracks})
        self.digest.assert_not_called()

    def test_reordered_audio_tracks_fail_before_hashing(self):
        with self.assertRaisesRegex(ValueError, '音轨'):
            self.verify({'streams': [stream('jpn', 1), stream()]})
        self.digest.assert_not_called()

    def test_every_playback_metadata_mismatch_fails_before_hashing(self):
        changes = [dict(codec_name='ac3'), dict(sample_rate='44100'), dict(channels=1),
                   dict(channel_layout='5.1'), dict(tags={'language': 'deu'}),
                   dict(disposition={'default': 0, 'forced': 0}), dict(disposition={'default': 1, 'forced': 1})]
        for change in changes:
            output = copy.deepcopy(self.info)
            output['streams'][2].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '音轨'):
                self.verify(output)
        self.digest.assert_not_called()

    def test_required_metadata_missing_or_malformed_is_rejected(self):
        for field, values in {'codec_name': [None, '', 9], 'sample_rate': [None, 0, -1, True, 48000.0, 'bad'],
                              'channels': [None, 0, -1, True, '2'], 'channel_layout': [None, 1],
                              'tags': [None, [], {'language': 2}],
                              'disposition': [None, [], {'default': 2}, {'forced': '1'}, {'default': True}]}.items():
            for value in values:
                info = {'streams': [stream(**{field: value})]}
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    audio.audio_track_descriptors(info)
        for field in ('codec_name', 'sample_rate', 'channels'):
            item = stream()
            del item[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                audio.audio_track_descriptors({'streams': [item]})

    def test_changed_second_payload_is_not_hidden_by_matching_first_hash(self):
        self.digest.side_effect = lambda path, stop, *, index: 'SHA256=' + (
            'c' if path == self.output and index == 1 else ('a' if index == 0 else 'b')) * 64
        with self.assertRaisesRegex(ValueError, '音轨'):
            self.verify()
        self.assertEqual(self.digest.call_count, 4)

    def test_same_metadata_swapped_payloads_fail(self):
        self.info['streams'][2] = stream()
        self.digest.side_effect = lambda path, stop, *, index: 'SHA256=' + str(index if path == self.source else 1-index) * 64
        with self.assertRaisesRegex(ValueError, '音轨'):
            self.verify()

    def test_noncanonical_digest_is_rejected(self):
        for value in (None, {}, '', 'a' * 64, 'SHA256=' + 'g' * 64, 'SHA256=' + 'a' * 63,
                      'SHA256=' + 'a' * 64 + '\n', 'sha256=' + 'a' * 64, 'MD5=' + 'a' * 64):
            self.digest.side_effect = None
            self.digest.return_value = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '音轨'):
                self.verify()

    def test_precancel_does_not_hash(self):
        self.stop.set()
        with self.assertRaises(Cancelled):
            self.verify()
        self.digest.assert_not_called()

    def test_cancel_after_final_digest_does_not_return_success(self):
        def digest(path, stop, *, index):
            if path == self.output and index == 1:
                stop.set()
            return 'SHA256=' + ('a' if index == 0 else 'b') * 64
        self.digest.side_effect = digest
        with self.assertRaises(Cancelled):
            self.verify()

    def test_digest_cancel_and_io_error_passthrough_preserves_identity(self):
        for error in (Cancelled('synthetic cancel'), OSError('synthetic read failure')):
            self.digest.side_effect = error
            with self.subTest(error=error), self.assertRaises(type(error)) as raised:
                self.verify()
            self.assertIs(raised.exception, error)

    def test_evidence_requires_exact_shape_valid_types_and_hashes(self):
        good = self.verify()
        mutations = [None, [], {}, {**good, 'version': True}, {**good, 'version': 3},
                     {**good, 'extra': 1}, {**good, 'source': []}, {**good, 'output': good['output'][:1]},
                     {**good, 'source': tuple(good['source'])}]
        for field, value in [('sha256', 'SHA256='+'a'*64), ('sha256', 'A'*64), ('sha256', 'g'*64),
                             ('sample_rate', '48000'), ('sample_rate', True), ('channels', True),
                             ('default', False), ('forced', False), ('language', None), ('extra', 1)]:
            altered = copy.deepcopy(good)
            for side in ('source', 'output'):
                altered[side][0][field] = value
            mutations.append(altered)
        missing = copy.deepcopy(good)
        del missing['source'][0]['forced']
        mutations.append(missing)
        for value in mutations:
            with self.subTest(evidence=value):
                self.assertFalse(audio.audio_evidence_matches(value, self.info))

    def test_evidence_rejects_changed_payload_metadata_and_current_source(self):
        evidence = self.verify()
        altered = copy.deepcopy(evidence)
        altered['output'][1]['sha256'] = 'c' * 64
        self.assertFalse(audio.audio_evidence_matches(altered, self.info))
        for change in (dict(codec_name='ac3'), dict(sample_rate=44100), dict(channels=1),
                       dict(tags={'language': 'deu'}), dict(disposition={'default': 0})):
            changed_source = copy.deepcopy(self.info)
            changed_source['streams'][2].update(change)
            with self.subTest(change=change):
                self.assertFalse(audio.audio_evidence_matches(evidence, changed_source))
        self.assertFalse(audio.audio_evidence_matches(evidence, {'streams': [stream()]}))
        self.assertFalse(audio.audio_evidence_matches(evidence, {'streams': []}))
        self.assertFalse(audio.audio_evidence_matches(evidence, None))

    def test_evidence_and_descriptors_do_not_alias_input_metadata(self):
        evidence = self.verify()
        evidence['source'][0]['language'] = 'deu'
        self.assertEqual(evidence['output'][0]['language'], 'eng')
        self.assertEqual(self.info['streams'][1]['tags']['language'], 'eng')

    def unspecified_defaults(self):
        self.info['streams'][2]['disposition']['default'] = 0
        output = copy.deepcopy(self.info)
        output['streams'][1]['disposition']['default'] = 1
        return output

    def test_mp4_normalization_retains_raw_flags_and_records_explicit_policy(self):
        output = self.unspecified_defaults()
        original_source = copy.deepcopy(self.info)
        original_output = copy.deepcopy(output)
        proof = self.verify(output)
        self.assertEqual(set(proof), {'version', 'source', 'output', 'default_policy'})
        self.assertEqual(proof['version'], 2)
        self.assertEqual(proof['default_policy'], 'mp4_first_when_unspecified')
        self.assertEqual([track['default'] for track in proof['source']], [0, 0])
        self.assertEqual([track['default'] for track in proof['output']], [1, 0])
        self.assertEqual(self.info, original_source)
        self.assertEqual(output, original_output)
        self.assertTrue(audio.audio_evidence_matches(proof, self.info))

    def test_unchanged_all_zero_defaults_are_preserved_not_normalized(self):
        self.unspecified_defaults()
        proof = self.verify()
        self.assertEqual(proof['default_policy'], 'preserved')
        self.assertEqual([track['default'] for track in proof['output']], [0, 0])
        self.assertTrue(audio.audio_evidence_matches(proof, self.info))

    def test_normalization_rejects_later_multiple_or_changed_explicit_defaults(self):
        self.unspecified_defaults()
        for defaults in ([0, 1], [1, 1]):
            output = copy.deepcopy(self.info)
            for track, value in zip(output['streams'][1:], defaults):
                track['disposition']['default'] = value
            with self.subTest(defaults=defaults), self.assertRaisesRegex(ValueError, '音轨'):
                self.verify(output)
        self.info['streams'][2]['disposition']['default'] = 1
        output = copy.deepcopy(self.info)
        output['streams'][1]['disposition']['default'] = 1
        output['streams'][2]['disposition']['default'] = 0
        with self.assertRaisesRegex(ValueError, '音轨'):
            self.verify(output)
        self.digest.assert_not_called()

    def test_normalization_cannot_hide_other_metadata_changes(self):
        output = self.unspecified_defaults()
        for change in (dict(codec_name='ac3'), dict(sample_rate=44100), dict(channels=1),
                       dict(channel_layout='mono'), dict(tags={'language': 'deu'}),
                       dict(disposition={'default': 0, 'forced': 1})):
            changed = copy.deepcopy(output)
            changed['streams'][2].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '音轨'):
                self.verify(changed)
        self.digest.assert_not_called()

    def test_normalization_cannot_hide_changed_payload(self):
        output = self.unspecified_defaults()
        self.digest.side_effect = lambda path, stop, *, index: 'SHA256=' + (
            'c' if path == self.output and index == 1 else ('a' if index == 0 else 'b')) * 64
        with self.assertRaisesRegex(ValueError, '第 2 条音轨内容'):
            self.verify(output)

    def test_normalization_is_not_applied_to_non_mp4_output(self):
        output = self.unspecified_defaults()
        self.output = Path('synthetic-output.mkv')
        with self.assertRaisesRegex(ValueError, '音轨'):
            self.verify(output)
        self.digest.assert_not_called()

    def test_version_two_rejects_false_or_missing_policy_for_raw_flags(self):
        for normalized in (False, True):
            output = self.unspecified_defaults() if normalized else None
            proof = self.verify(output)
            wrong = 'preserved' if normalized else 'mp4_first_when_unspecified'
            for policy in (wrong, True, None, 'unknown', []):
                with self.subTest(normalized=normalized, policy=policy):
                    self.assertFalse(audio.audio_evidence_matches({**proof, 'default_policy': policy}, self.info))
            del proof['default_policy']
            self.assertFalse(audio.audio_evidence_matches(proof, self.info))

    def test_normalized_evidence_requires_raw_current_source_and_per_index_hashes(self):
        proof = self.verify(self.unspecified_defaults())
        changed_source = copy.deepcopy(self.info)
        changed_source['streams'][1]['disposition']['default'] = 1
        self.assertFalse(audio.audio_evidence_matches(proof, changed_source))
        for field, value in (('default', 1), ('forced', 1), ('sha256', 'c'*64), ('language', 'deu')):
            changed = copy.deepcopy(proof)
            changed['output'][1][field] = value
            with self.subTest(field=field):
                self.assertFalse(audio.audio_evidence_matches(changed, self.info))

    def test_old_version_one_stays_strict_and_cannot_carry_normalization(self):
        preserved = self.verify()
        old = {key: copy.deepcopy(preserved[key]) for key in ('source', 'output')}
        old['version'] = 1
        self.assertTrue(audio.audio_evidence_matches(old, self.info))
        self.assertFalse(audio.audio_evidence_matches({**old, 'default_policy': 'preserved'}, self.info))
        normalized = self.verify(self.unspecified_defaults())
        del normalized['default_policy']
        normalized['version'] = 1
        self.assertFalse(audio.audio_evidence_matches(normalized, self.info))


if __name__ == '__main__':
    unittest.main()
