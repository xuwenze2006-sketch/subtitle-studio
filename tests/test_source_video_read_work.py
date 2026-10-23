"""Avoid delivery-only work in readers without weakening real export proofs."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_source_video_export as fixtures
from subtitle_pipeline import manual_review, source_video_export as api
from subtitle_pipeline.integrity import sha256


class SourceVideoReadWorkTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing synthetic media/project fixture. Inheriting its
        # TestCase would also discover and run all its unrelated tests again.
        self.fixture = fixtures.SourceVideoExportTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def current(self):
        fixture = self.fixture
        return api.current_source_video(fixture.project, fixture.source,
            video_matches=lambda path, digest: sha256(path) == digest)

    def test_missing_export_does_not_read_the_complete_review(self):
        with patch.object(manual_review, 'load_review',
                side_effect=AssertionError('No complete review is needed without an export receipt')):
            self.assertIsNone(self.current())

    def test_non_candidate_fixed_receipts_do_not_read_the_complete_review(self):
        path = self.fixture.project / '导出' / '原文视频' / 'export.json'
        path.parent.mkdir(parents=True)
        valid = {'version': 1, 'mode': 'source_only', 'status': 'draft_exported',
                 'requested_encoder': 'cpu'}
        cases = ['{', '[]', json.dumps({**valid, 'version': True}),
                 json.dumps({**valid, 'version': 2}), json.dumps({**valid, 'mode': 'bilingual'}),
                 json.dumps({**valid, 'status': 'encoding'}),
                 json.dumps({**valid, 'requested_encoder': 'invalid'})]
        for body in cases:
            with self.subTest(body=body):
                path.write_text(body, encoding='utf-8')
                with patch.object(manual_review, 'load_review',
                        side_effect=AssertionError('Non-candidate receipt must return before full review')):
                    self.assertIsNone(self.current())

    def test_qualification_does_not_render_delivery_captions(self):
        with patch.object(api, 'render_srt',
                side_effect=AssertionError('Boolean qualification must not render delivery captions')):
            self.assertTrue(api.source_export_available(self.fixture.project, self.fixture.source))

    def test_qualification_does_not_construct_snapshot_delivery_paths(self):
        original = api._path
        def path(root, *parts):
            if parts[:2] == ('人工校对', '版本'):
                raise AssertionError('Boolean qualification must not construct delivery snapshot paths')
            return original(root, *parts)
        with patch.object(api, '_path', side_effect=path):
            self.assertTrue(api.source_export_available(self.fixture.project, self.fixture.source))

    def test_qualification_still_rejects_invalid_current_cues(self):
        fixture = self.fixture
        fixture.edit()
        path = fixture.project / '人工校对' / '校对记录.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        record['patches']['1'] = {'start_ms': 3500, 'end_ms': 2000}
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertFalse(api.source_export_available(fixture.project, fixture.source))

    def test_qualification_still_rejects_missing_owned_manual_history(self):
        fixture = self.fixture
        fixture.edit()
        fixture.export()
        (fixture.project / '人工校对' / '校对记录.json').unlink()
        (fixture.project / '导出' / '原文视频' / 'export.json').unlink()
        self.assertFalse(api.source_export_available(fixture.project, fixture.source))
        self.assertIsNone(self.current())

    def test_candidate_output_still_requires_the_actual_revision_snapshot(self):
        fixture = self.fixture
        fixture.export()
        output = self.current()
        self.assertIsNotNone(output)
        before = output.read_bytes()
        revision = fixture.record()['draft_output_binding']['snapshot_revision']
        snapshot = fixture.project / '人工校对' / '版本' / revision / '原文.srt'
        snapshot.write_text('damaged snapshot', encoding='utf-8')
        self.assertIsNone(self.current())
        self.assertEqual(output.read_bytes(), before)

    def test_real_export_still_builds_complete_current_caption_binding(self):
        fixture = self.fixture
        fixture.edit()
        fixture.export()
        record = fixture.record()
        binding = record['draft_output_binding']
        review = manual_review.load_review(fixture.project)
        self.assertEqual(binding['manual_revision'], review['revision'])
        self.assertEqual(binding['snapshot_revision'], review['revision'])
        snapshot = fixture.project / '人工校对' / '版本' / review['revision'] / '原文.srt'
        self.assertEqual(binding['captions_sha256'], sha256(snapshot))
        self.assertIn(b'original one', Path(record['draft_output']).read_bytes())
        self.assertEqual(self.current(), Path(record['draft_output']))

    def test_later_qualification_observes_external_machine_subtitle_changes(self):
        fixture = self.fixture
        fixture.edit()
        self.assertTrue(api.source_export_available(fixture.project, fixture.source))
        path = fixture.project / '原文.srt'
        path.write_text(fixture.body.replace('original one', 'external change'), encoding='utf-8')
        self.assertFalse(api.source_export_available(fixture.project, fixture.source))


if __name__ == '__main__':
    unittest.main()
