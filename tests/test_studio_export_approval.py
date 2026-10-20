"""Official video delivery is tied to current approved subtitle bytes."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline.integrity import fingerprint, sha256
from tests import test_studio_export_integrity as fixture


class StudioExportApprovalTests(unittest.TestCase):
    setUp = fixture.StudioExportIntegrityTests.setUp
    make_samples = fixture.StudioExportIntegrityTests.make_samples
    write_manifest = fixture.StudioExportIntegrityTests.write_manifest
    publish_fixture = fixture.StudioExportIntegrityTests.publish_fixture

    def write_review(self, review):
        (self.campaign / 'final-review.json').write_text(json.dumps(review), encoding='utf-8')

    def assert_ineligible(self):
        before = self.output.read_bytes()
        self.assertIsNone(self.app.exported_video_path())
        self.assertIsNone(self.app.preview('main')['exported_video'])
        self.assertEqual(self.app.state()['file_layout']['video_path'], '')
        with self.assertRaises(ValueError): self.app.download_path('video')
        with patch.object(self.s.os, 'startfile', create=True) as opened:
            with self.assertRaises(ValueError): self.app.open_result('video')
            opened.assert_not_called()
        self.assertEqual(self.output.read_bytes(), before)

    def test_changed_saved_target_cannot_deliver_old_burned_video(self):
        self.publish_fixture()
        self.assertEqual(self.app.exported_video_path(), self.output)
        path = self.campaign / '整片' / '中文草稿.srt'
        path.write_text(path.read_text(encoding='utf-8').replace('translation', 'corrected translation'), encoding='utf-8')
        self.assertEqual(self.app.preview('main')['cues'][0]['target_text'], 'corrected translation')
        self.assert_ineligible()

    def test_all_approved_artifact_changes_and_deletions_withdraw_current_link(self):
        self.publish_fixture()
        review = json.loads((self.campaign / 'final-review.json').read_text(encoding='utf-8'))
        for artifact in review['artifacts']:
            path = Path(artifact['path']); original = path.read_bytes()
            for missing in (False, True):
                with self.subTest(name=path.name, missing=missing):
                    if missing: path.unlink()
                    else: path.write_bytes(original + b'changed')
                    self.assertIsNone(self.app.exported_video_path())
                    path.write_bytes(original)
                    self.assertEqual(self.app.exported_video_path(), self.output)

    def test_missing_malformed_or_inapplicable_approval_fails_closed(self):
        self.publish_fixture()
        path = self.campaign / 'final-review.json'; original = path.read_bytes()
        for value in (None, b'{', b'[]', b'{"status":"sampled_approved","artifacts":null}'):
            with self.subTest(value=value):
                if value is None: path.unlink()
                else: path.write_bytes(value)
                self.assert_ineligible()
                path.write_bytes(original)
        review = json.loads(original)
        for field, value in (('status', 'draft'), ('source_sha256', 'f'*64), ('language','zh'), ('target','en'),
                             ('manual_revision', ['bad']), ('artifacts', []), ('artifacts', {}),
                             ('artifacts', [None]*4), ('artifacts', [{'path': None, 'sha256': []}]*4)):
            with self.subTest(field=field, value=value):
                self.write_review({**review, field:value})
                self.assert_ineligible()
        self.write_review(review)

    def test_approval_fingerprint_and_captions_hash_both_must_match_output(self):
        manifest = self.publish_fixture()
        for field in ('review_artifacts_sha256', 'captions_sha256'):
            with self.subTest(field=field):
                old = manifest['output_binding'][field]
                manifest['output_binding'][field] = 'e'*64
                self.write_manifest(manifest)
                self.assert_ineligible()
                manifest['output_binding'][field] = old
        self.write_manifest(manifest)
        self.assertEqual(self.app.exported_video_path(), self.output)

    def test_malformed_source_identity_cannot_escape_delivery_check(self):
        manifest = self.publish_fixture()
        for source in (None, [], {'path':None,'sha256':'a'*64}, {'path':str(self.source),'sha256':None}):
            with self.subTest(source=source):
                self.write_manifest({**manifest,'source':source})
                self.assertIsNone(self.app.exported_video_path())
                self.assertIsNone(self.app.preview('main')['exported_video'])
                with self.assertRaises(ValueError): self.app.download_path('video')
        self.write_manifest(manifest)

    def test_tampered_artifact_path_is_rejected_before_opening_foreign_file(self):
        manifest = self.publish_fixture()
        review = json.loads((self.campaign / 'final-review.json').read_text(encoding='utf-8'))
        foreign = self.root / 'private-owned-file'; foreign.write_bytes(b'owned private bytes')
        review['artifacts'][1] = {'path':str(foreign), 'sha256':sha256(foreign)}
        manifest['output_binding']['review_artifacts_sha256'] = fingerprint(review['artifacts'])
        self.write_review(review); self.write_manifest(manifest)
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == foreign: raise AssertionError('foreign artifact was opened')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded): self.assert_ineligible()

    def test_repeated_delivery_checks_do_not_probe_media_or_rescan_video(self):
        from subtitle_pipeline import cloud_workflow, media_export
        self.publish_fixture()
        with patch.object(self.s.hashlib,'file_digest',wraps=self.s.hashlib.file_digest) as digest, \
                patch.object(cloud_workflow,'state_is_complete',side_effect=AssertionError('generation scan')), \
                patch.object(media_export,'media_info',side_effect=AssertionError('media probe')), \
                patch.object(media_export.r,'run_process',side_effect=AssertionError('media process')):
            for _ in range(3):
                self.assertEqual(self.app.exported_video_path(),self.output)
                self.assertEqual(self.app.download_path('video'),self.output)
            self.assertEqual(digest.call_count,1)

    def test_duplicate_artifacts_are_not_a_complete_approval(self):
        manifest = self.publish_fixture()
        review = json.loads((self.campaign / 'final-review.json').read_text(encoding='utf-8'))
        review['artifacts'][2] = review['artifacts'][1]
        manifest['output_binding']['review_artifacts_sha256'] = fingerprint(review['artifacts'])
        self.write_review(review); self.write_manifest(manifest)
        self.assert_ineligible()


class StudioManualExportApprovalTests(unittest.TestCase):
    def setUp(self):
        from tests import test_studio_review
        test_studio_review.StudioReviewTests.setUp(self)

    def write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def prepare_manual_export(self):
        from subtitle_pipeline import cloud_workflow
        view = self.app.preview('main')
        for cue in view['cues']:
            view = self.app.review_cue({'project_id':view['project_id'], 'sample':'main',
                'expected_revision':view['manual_review']['revision'], 'cue_id':cue['id'],
                **{field:cue[field] for field in ('start_ms','end_ms','source_text','target_text')},
                'review_status':'checked', 'translation_confirmed':True, 'note':''})
        self.app.review_accept({'project_id':view['project_id'], 'sample':'main',
            'expected_revision':view['manual_review']['revision'], 'content_passed':True})
        review = cloud_workflow.read_json(self.campaign / 'final-review.json')
        self.output = self.source.with_name(self.source.stem + '_中文字幕_修订版.mp4')
        self.output.write_bytes(b'owned synthetic historical manual export')
        target = next(Path(item['path']) for item in review['artifacts'] if Path(item['path']).name=='中文草稿.srt')
        manifest = cloud_workflow.read_json(self.campaign / 'campaign.json')
        manifest.update(status='exported', output=str(self.output), output_sha256=sha256(self.output),
            output_binding={'source_sha256':manifest['source']['sha256'], 'render_version':1,
                            'captions_sha256':sha256(target), 'review_artifacts_sha256':fingerprint(review['artifacts'])})
        self.write(self.campaign / 'campaign.json', manifest)
        return review

    def test_current_manual_binding_and_immutable_snapshot_are_required(self):
        review = self.prepare_manual_export()
        before = self.output.read_bytes()
        self.assertEqual(self.app.exported_video_path(), self.output)
        self.assertEqual(self.app.download_path('video'), self.output)
        for artifact in review['artifacts']:
            path = Path(artifact['path']); original = path.read_bytes()
            with self.subTest(snapshot=path.name):
                path.write_bytes(original+b'changed')
                self.assertIsNone(self.app.exported_video_path())
                path.write_bytes(original)
                self.assertEqual(self.app.exported_video_path(), self.output)
        self.write(self.campaign/'final-review.json', {**review, 'manual_binding':{}})
        self.assertIsNone(self.app.exported_video_path())
        self.write(self.campaign/'final-review.json', review)
        record = self.folder/'人工校对'/'校对记录.json'; original = record.read_bytes()
        record.unlink()
        self.assertIsNone(self.app.exported_video_path())
        record.write_bytes(original)
        self.assertEqual(self.app.exported_video_path(), self.output)
        view = self.app.preview('main'); cue = view['cues'][0]
        self.app.review_cue({'project_id':view['project_id'], 'sample':'main',
            'expected_revision':view['manual_review']['revision'], 'cue_id':cue['id'],
            **{field:cue[field] for field in ('start_ms','end_ms','source_text','target_text')},
            'review_status':'checked', 'note':'new review note', 'translation_confirmed':True})
        self.assertIsNone(self.app.exported_video_path())
        self.assertEqual(self.output.read_bytes(), before)


if __name__ == '__main__': unittest.main()
