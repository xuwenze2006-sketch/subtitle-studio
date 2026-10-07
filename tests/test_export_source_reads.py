"""Count actual complete source reads during owned fake-media exports."""
from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import media_export
from subtitle_pipeline.languages import video_output_path
from tests import test_draft_video as fixture


@contextmanager
def observed_reads(paths):
    expected = {Path(path).resolve() for path in paths}
    original = Path.open
    reads = []

    class Reader:
        def __init__(self, stream, record):
            self.stream, self.record = stream, record
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size=-1):
            data = self.stream.read(size)
            self.record['bytes'] += len(data)
            self.record['eof'] |= not data
            return data
        def readinto(self, buffer):
            count = self.stream.readinto(buffer)
            self.record['bytes'] += count
            self.record['eof'] |= not count
            return count

    def open_file(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        resolved = path.resolve()
        if resolved in expected and args and args[0] == 'rb':
            record = {'path': resolved, 'bytes': 0, 'eof': False, 'stream': stream}
            reads.append(record)
            return Reader(stream, record)
        return stream

    with patch.object(Path, 'open', new=open_file):
        yield reads


class ExportSourceReadTests(unittest.TestCase):
    write = fixture.DraftVideoTests.write
    fake_encode = fixture.DraftVideoTests.fake_encode

    def setUp(self):
        fixture.DraftVideoTests.setUp(self)
        self.write(self.campaign / 'final-review.json', {
            'status': 'sampled_approved', 'language': 'ja', 'target': 'zh-CN',
            'source_sha256': self.manifest['source']['sha256'],
            'artifacts': workflow.artifacts_for(self.campaign, self.manifest, full=True),
        })

    def assert_complete_reads(self, reads, expected):
        self.assertEqual([value['path'] for value in reads], [Path(p).resolve() for p in expected])
        for value in reads:
            self.assertEqual(value['bytes'], value['path'].stat().st_size)
            self.assertTrue(value['eof'], value)
            self.assertTrue(value['stream'].closed)

    def test_fresh_draft_and_formal_read_source_twice_including_after_encoding(self):
        for draft in (True, False):
            with self.subTest(draft=draft), observed_reads([self.source]) as reads:
                workflow.export_video(self.campaign, self.stop, draft=draft)
            self.assert_complete_reads(reads, [self.source, self.source])

    def test_existing_complete_final_reads_source_once_before_reuse(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                workflow.export_video(self.campaign, self.stop, draft=draft)
                self.encoder.reset_mock()
                with observed_reads([self.source]) as reads, \
                     patch.object(media_export, '_publish_video', side_effect=AssertionError('no republish')):
                    workflow.export_video(self.campaign, self.stop, draft=draft)
                self.assert_complete_reads(reads, [self.source])
                self.encoder.assert_not_called()

    def test_published_final_missing_manifest_records_reads_source_once(self):
        workflow.export_video(self.campaign, self.stop, draft=True)
        manifest = workflow.read_json(self.campaign / 'campaign.json')
        for key in ('draft_output', 'draft_output_sha256', 'draft_output_binding'):
            manifest.pop(key)
        self.write(self.campaign / 'campaign.json', manifest)
        self.encoder.reset_mock()
        with observed_reads([self.source]) as reads, \
             patch.object(media_export, '_publish_video', side_effect=AssertionError('no republish')):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source])
        self.encoder.assert_not_called()
        self.assertIn('draft_output', workflow.read_json(self.campaign / 'campaign.json'))

    def test_each_wrong_expected_digest_blocks_existing_final_before_media(self):
        workflow.export_video(self.campaign, self.stop, draft=True)
        manifest_path, state_path = self.campaign / 'campaign.json', self.folder / 'state.json'
        originals = {path: path.read_bytes() for path in (manifest_path, state_path)}
        final = video_output_path(self.source, 'zh-CN', draft=True)
        final_bytes = final.read_bytes()
        for path in (manifest_path, state_path):
            with self.subTest(record=path.name):
                value = workflow.read_json(path)
                source = value['source'] if path == manifest_path else value['identity']['source']
                source['sha256'] = 'b' * 64
                self.write(path, value)
                altered = path.read_bytes()
                self.encoder.reset_mock()
                with observed_reads([self.source]) as reads, \
                     patch.object(media_export, 'media_info', side_effect=AssertionError('no media for wrong source')):
                    with self.assertRaises(ValueError):
                        workflow.export_video(self.campaign, self.stop, draft=True)
                self.assert_complete_reads(reads, [self.source])
                self.encoder.assert_not_called()
                self.assertEqual(path.read_bytes(), altered)
                self.assertEqual(final.read_bytes(), final_bytes)
                path.write_bytes(originals[path])

    def test_changed_source_with_restored_metadata_is_actually_read_and_rejected(self):
        old = self.source.stat()
        self.source.write_bytes(b'X' * old.st_size)
        os.utime(self.source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.source.stat().st_size, old.st_size)
        self.assertEqual(self.source.stat().st_mtime_ns, old.st_mtime_ns)
        with observed_reads([self.source]) as reads, \
             patch.object(media_export, 'media_info', side_effect=AssertionError('no media for changed source')):
            with self.assertRaises(ValueError):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source])
        self.encoder.assert_not_called()

    def test_different_state_source_keeps_two_preflight_reads_and_post_encode_read(self):
        other = self.root / 'other-source.mp4'
        other.write_bytes(self.source.read_bytes())
        state = workflow.read_json(self.folder / 'state.json')
        state['identity']['source']['path'] = str(other)
        self.write(self.folder / 'state.json', state)
        with observed_reads([self.source, other]) as reads:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source, other, self.source])

    def test_different_state_source_cannot_hide_corrupt_manifest_file(self):
        other = self.root / 'other-source.mp4'
        other.write_bytes(self.source.read_bytes())
        state = workflow.read_json(self.folder / 'state.json')
        state['identity']['source']['path'] = str(other)
        self.write(self.folder / 'state.json', state)
        self.source.write_bytes(b'Z' * self.source.stat().st_size)
        with observed_reads([self.source, other]) as reads, \
             patch.object(media_export, 'media_info', side_effect=AssertionError('no media for corrupt manifest source')):
            with self.assertRaisesRegex(ValueError, '原.*视频'):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source])
        self.encoder.assert_not_called()

    def test_source_change_after_encoding_still_requires_full_read_and_rejects_output(self):
        original_encoder = self.fake_encode
        old = self.source.stat()
        def encode(args, log, stop, **kwargs):
            original_encoder(args, log, stop, **kwargs)
            if Path(args[-1]).suffix == '.mp4':
                self.source.write_bytes(b'Y' * old.st_size)
                os.utime(self.source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.encoder.side_effect = encode
        with observed_reads([self.source]) as reads:
            with self.assertRaises(ValueError):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source, self.source])
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertNotIn('draft_output', workflow.read_json(self.campaign / 'campaign.json'))
        self.assertTrue((self.campaign / '导出' / '草稿视频' / 'result.partial.mp4').exists())

    def test_completed_encoding_publication_failure_resumes_with_two_full_source_reads(self):
        with patch.object(media_export, '_publish_video', side_effect=OSError('owned publication failure')):
            with self.assertRaises(OSError):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.reset_mock()
        with observed_reads([self.source]) as reads:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source, self.source])
        self.assertFalse(any(Path(str(call.args[0][-1])).suffix == '.mp4'
                             for call in self.encoder.call_args_list))

    def test_pending_source_validation_resumes_with_two_full_reads_and_no_reencoding(self):
        original = workflow.cancellable_sha256
        def stop_post_encode_source(path, stop, **kwargs):
            value = original(path, stop, **kwargs)
            if Path(path) == self.source:
                stop.set()
                raise workflow.r.Cancelled('owned post-encode validation stop')
            return value
        with patch.object(media_export, 'cancellable_sha256', side_effect=stop_post_encode_source):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        checkpoint = workflow.read_json(self.campaign / '导出' / '草稿视频' / 'encoding-checkpoint.json')
        self.assertEqual(checkpoint['status'], 'pending_source_validation')
        self.stop.clear()
        self.encoder.reset_mock()
        with observed_reads([self.source]) as reads:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assert_complete_reads(reads, [self.source, self.source])
        self.assertFalse(any(Path(str(call.args[0][-1])).suffix == '.mp4'
                             for call in self.encoder.call_args_list))


if __name__ == '__main__':
    unittest.main()
