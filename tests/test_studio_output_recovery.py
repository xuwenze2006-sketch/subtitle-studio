"""Cloud output recovery uses only temporary files and offline account doubles."""
import json
import unittest
from unittest.mock import patch

from tests import test_studio as local_fixture
from tests import test_studio_review as review_fixture


class StudioOutputRecoveryTests(unittest.TestCase):
    write = review_fixture.StudioReviewTests.write
    request = review_fixture.StudioReviewTests.request

    def setUp(self):
        review_fixture.StudioReviewTests.setUp(self)
        ready = patch.object(review_fixture.studio.cloud_settings, 'load_settings', return_value=object())
        ready.start(); self.addCleanup(ready.stop)
        self.write(self.campaign / 'approval.json', {'synthetic': True})

    def files(self):
        return {str(path.relative_to(self.campaign)): path.read_bytes()
                for path in self.campaign.rglob('*') if path.is_file()}

    def assert_processing_blocked(self):
        state = self.app.state()
        self.assertTrue(state['project_error'])
        self.assertFalse(any(value for action, value in state['actions'].items() if action != 'stop'))
        for action in ('prepare', 'samples', 'full', 'accept-final', 'export-draft', 'local'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.app._start_payload({'action': action, 'content_passed': True})

    def test_missing_full_state_keeps_manual_record_visible_as_binding_conflict(self):
        self.app.review_cue(self.request(target_text='人工保留译文', translation_confirmed=True))
        (self.folder / 'state.json').unlink()
        before = self.files()
        view = self.app.preview('main')
        self.assertEqual(view['selections'][0]['name'], '整片')
        self.assertTrue(view.get('manual_review', {}).get('conflict'))
        self.assertTrue(view['issues'])
        self.assertNotIn('revision', view['manual_review'])
        with self.assertRaises(ValueError): self.app.download_path('中文草稿.srt', 'main')
        with self.assertRaises(ValueError):
            self.app.save_subtitles({'project_id': self.app._project_id(), 'sample': 'main'})
        self.assertFalse((self.campaign / '导出').exists())
        self.assert_processing_blocked()
        self.assertEqual(self.files(), before)

    def test_missing_manual_state_cannot_save_obsolete_root_subtitles(self):
        self.app.review_cue(self.request(target_text='人工保留译文', translation_confirmed=True))
        for name in ('原文.srt', '中文草稿.srt', '双语草稿.srt'):
            (self.campaign / name).write_text('1\n00:00:00,000 --> 00:00:01,000\n根目录旧结果\n', encoding='utf-8')
        (self.folder / 'state.json').unlink()
        before = self.files()
        with self.assertRaises(ValueError):
            self.app.save_subtitles({'project_id': self.app._project_id(), 'sample': 'main'})
        with self.assertRaises(ValueError): self.app.download_path('中文草稿.srt', 'main')
        self.assertFalse((self.campaign / '导出').exists())
        self.assertEqual(self.files(), before)

    def test_missing_full_state_never_selects_obsolete_root_tracks(self):
        for name in ('原文.srt', '中文草稿.srt', '双语草稿.srt'):
            (self.campaign / name).write_text('1\n00:00:00,000 --> 00:00:01,000\n根目录旧结果\n', encoding='utf-8')
        (self.folder / 'state.json').unlink()
        before = self.files()
        view = self.app.preview('main')
        self.assertEqual(len(view['cues']), 20)
        self.assertEqual(view['cues'][0]['target_text'], '你好')
        self.assertEqual(self.app.download_path('中文草稿.srt', 'main'), self.folder / '中文草稿.srt')
        self.assert_processing_blocked()
        self.assertEqual(self.files(), before)

    def test_unusable_full_state_blocks_processing_without_hiding_raw_subtitles(self):
        path = self.folder / 'state.json'
        for damage in ('missing', 'directory', '{broken', '[]', '{}'):
            with self.subTest(damage=damage):
                if path.is_dir(): path.rmdir()
                else: path.unlink(missing_ok=True)
                if damage == 'directory': path.mkdir()
                elif damage != 'missing': path.write_text(damage, encoding='utf-8')
                before = self.files()
                self.assert_processing_blocked()
                view = self.app.preview('main')
                self.assertEqual(len(view['cues']), 20)
                self.assertEqual(view['cues'][0]['target_text'], '你好')
                self.assertEqual(self.app.download_path('原文.srt', 'main'), self.folder / '原文.srt')
                self.assertEqual(self.files(), before)

    def test_restoring_exact_state_restores_manual_revision_without_repair_writes(self):
        saved = self.app.review_cue(self.request(target_text='人工保留译文', translation_confirmed=True))
        path = self.folder / 'state.json'
        original = path.read_bytes()
        before = self.files()
        path.unlink()
        self.assertTrue(self.app.preview('main').get('manual_review', {}).get('conflict'))
        self.assert_processing_blocked()
        path.write_bytes(original)
        self.assertFalse(self.app.state()['project_error'])
        restored = self.app.preview('main')
        self.assertEqual(restored['manual_review']['revision'], saved['manual_review']['revision'])
        self.assertEqual(restored['cues'][0]['target_text'], '人工保留译文')
        self.assertEqual(self.files(), before)

    def test_healthy_completed_cloud_results_keep_existing_actions_and_downloads(self):
        before = self.files()
        state = self.app.state()
        self.assertFalse(state['project_error'])
        self.assertTrue(state['actions']['full'])
        self.assertTrue(state['actions']['accept-final'])
        self.assertEqual(self.app.preview('main')['cues'][0]['target_text'], '你好')
        self.assertEqual(self.app.download_path('中文草稿.srt', 'main'), self.folder / '中文草稿.srt')
        self.assertEqual(self.files(), before)

    def test_initial_cloud_and_unrelated_full_directory_do_not_block_preparation(self):
        self.campaign = self.root / 'fresh-cloud'; self.campaign.mkdir()
        self.app.campaign = str(self.campaign)
        self.manifest.update(status='prepared', samples=[])
        full = self.campaign / '整片'; full.mkdir()
        (full / 'unrelated.txt').write_bytes(b'preserve arbitrary nested files')
        self.write(full / 'prepare.json', {'synthetic': True})
        self.write(self.campaign / 'approval.json', {'synthetic': True})
        for stage in ('prepared', 'full_incomplete'):
            with self.subTest(stage=stage):
                self.manifest['status'] = stage
                self.write(self.campaign / 'campaign.json', self.manifest)
                before = self.files()
                state = self.app.state()
                self.assertFalse(state['project_error'])
                self.assertTrue(state['actions']['prepare'])
                self.assertTrue(state['actions']['samples' if stage == 'prepared' else 'full'])
                self.assertEqual(self.app.preview('main')['cues'], [])
                self.assertEqual(self.files(), before)

    def test_sample_only_cloud_retains_sample_preview_download_and_admission(self):
        self.campaign = self.root / 'sample-cloud'; self.campaign.mkdir()
        self.app.campaign = str(self.campaign)
        local_fixture.StudioTests.make_samples(self)
        before = self.files()
        state = self.app.state()
        self.assertFalse(state['project_error'])
        self.assertTrue(state['actions']['samples'])
        self.assertEqual(self.app.preview()['selected_id'], 'sample-1')
        self.assertEqual(self.app.preview('sample-2')['cues'][0]['target_text'], '译文2')
        self.assertEqual(self.app.download_path('原文.srt', 'sample-2'),
                         self.campaign / '样片' / '02' / '识别任务' / '原文.srt')
        self.assertEqual(self.files(), before)

    def test_full_state_damage_does_not_hide_independently_valid_samples(self):
        sample = self.campaign / '样片' / '01'
        job = sample / '识别任务'; job.mkdir(parents=True)
        (sample / 'preview.mp4').write_bytes(b'synthetic sample')
        (job / '原文.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n样片原文\n', encoding='utf-8')
        self.manifest['samples'] = [{'folder': '样片/01', 'start_sec': 30, 'end_sec': 120}]
        self.write(self.campaign / 'campaign.json', self.manifest)
        (self.folder / 'state.json').unlink()
        self.assert_processing_blocked()
        self.assertEqual(self.app.preview('sample-1')['cues'][0]['source_text'], '样片原文')
        self.assertEqual(self.app.download_path('原文.srt', 'sample-1'), job / '原文.srt')


class StudioLocalOutputRecoveryControls(unittest.TestCase):
    setUp = local_fixture.StudioTests.setUp

    def test_local_root_results_ignore_unbound_nested_artifacts(self):
        config = {'source': str(self.source), 'project': str(self.campaign),
                  'asr_provider': 'whisper_cpp', 'language': 'en', 'target': 'zh-CN'}
        (self.campaign / 'state.json').write_text(json.dumps({'config': config,
                'identity': {'engine': 'whisper_cpp'}, 'status': 'cancelled'}), encoding='utf-8')
        root_track = '1\n00:00:00,000 --> 00:00:01,000\n本地根目录结果\n'
        for name in ('原文.srt', '中文草稿.srt'):
            (self.campaign / name).write_text(root_track, encoding='utf-8')
        full = self.campaign / '整片'; full.mkdir()
        (full / '原文.srt').write_text(root_track.replace('本地根目录结果', '无身份子目录'), encoding='utf-8')
        (full / '需复核.json').write_text('[]', encoding='utf-8')
        before = {path: path.read_bytes() for path in self.campaign.rglob('*') if path.is_file()}
        self.assertFalse(self.app.state()['project_error'])
        self.assertTrue(self.app.state()['actions']['local'])
        self.assertEqual(self.app.preview('main')['cues'][0]['source_text'], '本地根目录结果')
        self.assertEqual(self.app.download_path('原文.srt', 'main'), self.campaign / '原文.srt')
        self.assertEqual(before, {path: path.read_bytes() for path in before})


if __name__ == '__main__': unittest.main()
