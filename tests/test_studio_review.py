"""Manual review uses synthetic projects; no speech or translation requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio, cloud_workflow
from subtitle_pipeline.integrity import sha256
from subtitle_pipeline.subtitles import Cue, render_srt, parse_srt
from tests.test_cloud_workflow import write_complete_qwen_evidence


class StudioReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'sample.mp4'
        self.source.write_bytes(b'synthetic video')
        self.campaign = self.root / 'campaign'
        self.folder = self.campaign / '整片'
        self.folder.mkdir(parents=True)
        self.manifest = {'version': 1, 'source': {'path': str(self.source), 'sha256': sha256(self.source)},
                         'language': 'ja', 'target': 'zh-CN', 'status': 'full_ready',
                         'asr_provider': 'qwen_asr', 'samples': [], 'duration_ms': 40000}
        self.state = {'status': 'complete', 'duration_ms': 40000,
                      'identity': {'source': self.manifest['source'], 'language': 'ja', 'target': 'zh-CN'},
                      'config': {'source': str(self.source), 'project': str(self.folder), 'asr_provider': 'qwen_asr',
                                 'language': 'ja', 'target': 'zh-CN'}}
        self.write(self.campaign / 'campaign.json', self.manifest)
        self.write(self.folder / '需复核.json', [])
        for name, text in [('原文.srt', 'こんにちは'), ('中文草稿.srt', '你好'), ('双语草稿.srt', '你好\nこんにちは')]:
            (self.folder / name).write_text(render_srt([Cue(i*1500, i*1500+1000, text) for i in range(20)]), encoding='utf-8')
        self.state = write_complete_qwen_evidence(self.folder, self.source, 40000, state=self.state)
        self.assertTrue(cloud_workflow.state_is_complete(self.folder))
        for guard in (patch.object(studio, 'account_environment', return_value={}),
                      patch.object(studio, 'encrypted_names', return_value=set()),
                      patch.object(studio, 'engine_available', return_value=False)):
            guard.start(); self.addCleanup(guard.stop)
        self.app = studio.StudioController(source=str(self.source), campaign=str(self.campaign), state_path=self.root/'studio.json')

    def write(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def request(self, **changes):
        view = self.app.preview('main')
        cue = view['cues'][0]
        return {'project_id': view['project_id'], 'sample': 'main',
                'expected_revision': view['manual_review']['revision'], 'cue_id': cue['id'],
                'start_ms': cue['start_ms'], 'end_ms': cue['end_ms'],
                'source_text': cue['source_text'], 'target_text': cue['target_text'],
                'review_status': 'checked', 'note': '', 'translation_confirmed': False, **changes}

    def test_preview_exposes_unchecked_review_without_writing(self):
        view = self.app.preview('main')
        self.assertTrue(view['manual_review']['supported'])
        self.assertEqual(view['manual_review']['summary']['checked'], 0)
        self.assertEqual(view['cues'][0]['review_status'], 'unchecked')
        self.assertFalse((self.folder/'人工校对').exists())

    def test_edit_download_and_version_share_corrected_subtitles_without_mutating_machine_result(self):
        original = (self.folder/'原文.srt').read_bytes()
        translated = (self.folder/'中文草稿.srt').read_bytes()
        response = self.app.review_cue(self.request(source_text='おはよう', target_text='早上好',
                                                  start_ms=100, end_ms=1100, translation_confirmed=True))
        self.assertEqual(response['cues'][0]['source_text'], 'おはよう')
        self.assertEqual(response['manual_review']['summary']['checked'], 1)
        self.assertEqual((self.folder/'原文.srt').read_bytes(), original)
        self.assertEqual((self.folder/'中文草稿.srt').read_bytes(), translated)
        for name, text in [('原文.srt','おはよう'),('中文草稿.srt','早上好'),('双语草稿.srt','早上好\nおはよう')]:
            path = self.app.download_path(name, 'main', self.app._project_id())
            cues = parse_srt(path.read_text(encoding='utf-8'))
            self.assertEqual((cues[0].start_ms,cues[0].end_ms,cues[0].text),(100,1100,text))
        saved = self.app.save_subtitles({'project_id':self.app._project_id(),'sample':'main'})
        bodies = [Path(item['path']).read_text(encoding='utf-8') for item in saved['files']]
        self.assertTrue(any('早上好\nおはよう' in text for text in bodies))
        reopened = studio.StudioController(source=str(self.source),campaign=str(self.campaign),state_path=self.root/'other.json')
        self.assertEqual(reopened.preview('main')['manual_review']['summary']['checked'],1)

    def test_busy_stale_project_revision_and_other_process_locks_cannot_overwrite(self):
        data = self.request()
        self.app.job['busy'] = True
        with self.assertRaises(ValueError): self.app.review_cue(data)
        self.app.job['busy'] = False
        with self.assertRaises(ValueError): self.app.review_cue({**data,'project_id':'stale'})
        with studio.ProjectLock(self.campaign), self.assertRaises(RuntimeError): self.app.review_cue(data)
        self.app.review_cue(data)
        with self.assertRaisesRegex(ValueError, '版本|冲突'): self.app.review_cue(data)
        self.assertEqual(self.app.preview('main')['manual_review']['summary']['checked'],1)

    def test_conflicting_generation_is_visible_and_never_downloads_old_manual_results(self):
        self.app.review_cue(self.request())
        (self.folder/'原文.srt').write_text(render_srt([Cue(0,1000,'変更')]),encoding='utf-8')
        view = self.app.preview('main')
        self.assertTrue(view['manual_review'].get('conflict'))
        with self.assertRaises(ValueError): self.app.download_path('原文.srt','main',self.app._project_id())

    def test_accept_requires_explicit_confirmation_current_revision_and_main_selection(self):
        data={'project_id':self.app._project_id(),'sample':'main',
              'expected_revision':self.app.preview('main')['manual_review']['revision'],'content_passed':True}
        with patch.object(cloud_workflow,'accept_final') as accept:
            for change in ({'content_passed':False},{'content_passed':'true'},{'project_id':'stale'},{'sample':'sample-1'}):
                with self.assertRaises(ValueError): self.app.review_accept({**data,**change})
            accept.assert_not_called()

    def test_later_edit_and_missing_manual_record_both_invalidate_approval_in_ui(self):
        view=self.app.review_cue(self.request())
        self.write(self.campaign/'final-review.json',{'manual_revision':view['manual_review']['revision']})
        self.manifest['status']='final_reviewed'
        self.write(self.campaign/'campaign.json',self.manifest)
        self.assertEqual(self.app.state()['campaign_status'],'final_reviewed')
        self.app.review_cue(self.request(note='需要再听',review_status='issue'))
        self.assertEqual(self.app.state()['campaign_status'],'full_ready')
        (self.folder/'人工校对'/'校对记录.json').unlink()
        result=self.app.state()
        self.assertEqual(result['campaign_status'],'full_ready')
        self.assertFalse(result['actions']['export'])

    def test_missing_accepted_review_never_falls_back_to_machine_preview_download_or_snapshot(self):
        view = self.app.preview('main')
        for index, cue in enumerate(view['cues']):
            view = self.app.review_cue({
                'project_id': view['project_id'], 'sample': 'main',
                'expected_revision': view['manual_review']['revision'], 'cue_id': cue['id'],
                **{key: cue[key] for key in ('start_ms', 'end_ms', 'source_text', 'target_text')},
                'target_text': '人工修订译文' if index == 0 else cue['target_text'],
                'review_status': 'checked', 'note': '已听看', 'translation_confirmed': True,
            })
        self.app.review_accept({
            'project_id': view['project_id'], 'sample': 'main',
            'expected_revision': view['manual_review']['revision'], 'content_passed': True,
        })
        (self.folder / '人工校对' / '校对记录.json').unlink()

        preview = self.app.preview('main')
        self.assertTrue(preview['manual_review'].get('conflict'))
        self.assertEqual(preview['cues'], [])
        self.assertEqual(preview['downloads'], [])
        for name in ('原文.srt', '中文草稿.srt', '双语草稿.srt'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '人工|校对|缺失'):
                self.app.download_path(name, 'main', view['project_id'])
        with self.assertRaisesRegex(ValueError, '人工|校对|缺失'):
            self.app.save_subtitles({'project_id': view['project_id'], 'sample': 'main'})
        self.assertFalse((self.campaign / '导出').exists())


if __name__ == '__main__': unittest.main()
