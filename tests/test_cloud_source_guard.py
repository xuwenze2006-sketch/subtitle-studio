"""Preparation keeps source identity stable through the final video preview."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as w


@unittest.skipUnless(os.name == 'nt', 'Windows native source sharing')
class CloudSourceGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cloud-source-guard-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.mp4'
        self.content = b'synthetic original audio and video'
        self.source.write_bytes(self.content)
        self.campaign = self.root/'campaign'
        self.campaign.mkdir()
        self.stop = threading.Event()
        self.phases = []
        for target in ('socket.create_connection', 'socket.socket.connect'):
            blocker = patch(target, side_effect=AssertionError('offline'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def assert_protected(self):
        with self.assertRaises(OSError) as denied:
            self.source.write_bytes(b'changed source')
        self.assertEqual(denied.exception.errno, 13)
        replacement = self.root/'replacement.mp4'
        replacement.write_bytes(b'replacement source')
        with self.assertRaises(OSError) as denied:
            replacement.replace(self.source)
        self.assertEqual(denied.exception.errno, 13)
        self.assertEqual(self.source.read_bytes(), self.content)

    def assert_released(self):
        self.source.write_bytes(b'writable after prepare')
        replacement = self.root/'replacement.mp4'
        replacement.write_bytes(self.content)
        replacement.replace(self.source)
        self.assertEqual(self.source.read_bytes(), self.content)

    @contextmanager
    def services(self, *, checked=True, interrupt=None):
        identity = w.r.file_identity
        verify = w.verify_source
        review = w.build_review
        def phase(name):
            self.phases.append(name)
            if checked:
                self.assert_protected()
            if interrupt:
                interrupt(name)
        def probe(path, **kwargs):
            if Path(path) == self.source:
                phase('probe')
            return 2000
        def info(*args):
            phase('media-info')
            return {'streams':[{'codec_type':'audio'}, {'codec_type':'video'}]}
        def source_identity(*args, **kwargs):
            phase('identity')
            return identity(*args, **kwargs)
        def verified(*args, **kwargs):
            phase('verify')
            return verify(*args, **kwargs)
        def encode(args, log, stop):
            output = Path(args[-1])
            input_path = Path(args[args.index('-i')+1])
            name = ('preview-encode' if output.suffix == '.mp4' else
                    'timeline-encode' if input_path == self.source else 'sample-encode')
            phase(name)
            output.write_bytes(name.encode()+b':'+input_path.read_bytes())
        def preview(path, seconds, stop, **kwargs):
            phase('preview-validation')
            return {'version':2, 'sha256':w.sha256(path),
                    'expected_duration_ms':round(seconds*1000), 'video_frames':48}
        def reviewed(*args):
            phase('review')
            return review(*args)
        with patch.object(w.r, 'probe_media', side_effect=probe) as duration, \
             patch.object(w, 'media_info', side_effect=info), \
             patch.object(w.r, 'file_identity', side_effect=source_identity), \
             patch.object(w, 'verify_source', side_effect=verified), \
             patch.object(w.r, 'run_process', side_effect=encode), \
             patch.object(w, '_checked_preview', side_effect=preview), \
             patch.object(w, 'build_review', side_effect=reviewed), \
             patch.object(w, 'emit'):
            yield duration

    def assert_same_source_outputs(self, manifest):
        self.assertEqual(manifest['status'], 'prepared')
        self.assertEqual(manifest['source']['sha256'], hashlib.sha256(self.content).hexdigest())
        self.assertEqual((self.campaign/'源音频.wav').read_bytes(), b'timeline-encode:'+self.content)
        sample = self.campaign/'样片/01'
        self.assertEqual((sample/'input.wav').read_bytes(), b'sample-encode:timeline-encode:'+self.content)
        self.assertEqual((sample/'preview.mp4').read_bytes(), b'preview-encode:'+self.content)
        self.assertEqual(manifest['samples'][0]['preview_hash'], w.sha256(sample/'preview.mp4'))

    def test_new_video_protects_source_from_probe_through_late_preview(self):
        with self.services():
            result = w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(self.phases, ['probe', 'media-info', 'identity', 'timeline-encode',
                                     'sample-encode', 'preview-encode', 'preview-validation', 'review'])
        self.assert_same_source_outputs(result)
        self.assert_released()

    def test_resume_regenerates_missing_audio_and_video_under_same_source_guard(self):
        with self.services(checked=False):
            w.prepare(self.source, self.campaign, None, self.stop)
        for relative in ('源音频.wav', '样片/01/input.wav', '样片/01/preview.mp4'):
            (self.campaign/relative).unlink()
        self.phases.clear()
        with self.services():
            result = w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(self.phases, ['verify', 'timeline-encode', 'sample-encode',
                                     'preview-encode', 'preview-validation', 'review'])
        self.assert_same_source_outputs(result)
        self.assert_released()

    def test_cancel_during_late_preview_releases_source_and_keeps_preparing(self):
        def cancel(name):
            if name == 'preview-encode':
                self.stop.set()
                raise w.r.Cancelled('synthetic preview cancellation')
        with self.services(interrupt=cancel), self.assertRaises(w.r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(w.read_json(self.campaign/'campaign.json')['status'], 'preparing')
        self.assertFalse((self.campaign/'review.html').exists())
        self.assert_released()

    def test_preview_validation_error_releases_source_without_publishing(self):
        def fail(name):
            if name == 'preview-validation':
                raise ValueError('synthetic preview validation failure')
        with self.services(interrupt=fail), self.assertRaisesRegex(ValueError, 'synthetic preview'):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertFalse((self.campaign/'样片/01/preview.mp4').exists())
        self.assertTrue((self.campaign/'样片/01/preview.partial.mp4').exists())
        self.assertEqual(w.read_json(self.campaign/'campaign.json')['status'], 'preparing')
        self.assert_released()

    def test_existing_writer_is_refused_before_probe_or_manifest(self):
        with self.source.open('r+b'), self.services(checked=False) as probe, \
             self.assertRaises(OSError) as denied:
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertEqual(denied.exception.winerror, 32)
        probe.assert_not_called()
        self.assertFalse((self.campaign/'campaign.json').exists())
        self.assert_released()

    def test_initial_cancellation_precedes_source_guard(self):
        self.stop.set()
        with patch.object(w.r, 'stable_read_guard', side_effect=AssertionError('guard after stop')), \
             self.assertRaises(w.r.Cancelled):
            w.prepare(self.source, self.campaign, None, self.stop)
        self.assertFalse((self.campaign/'campaign.json').exists())
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
