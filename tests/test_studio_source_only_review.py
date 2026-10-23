"""Original-only editing uses real local project state and no providers."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner, studio
from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


class StudioSourceOnlyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studio-source-review-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.wav'
        self.source.write_bytes(b'isolated source; decoder is stubbed')
        self.cues = [Cue(i * 1500 + 100, i * 1500 + 1000, f'Original sentence {i + 1}') for i in range(4)]
        for target, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                             (socket, 'create_connection')):
            guard = patch.object(target, name, side_effect=AssertionError('No provider/network requests'))
            guard.start(); self.addCleanup(guard.stop)
        for name, value in (('account_environment', {}), ('encrypted_names', set()),
                            ('engine_available', True)):
            guard = patch.object(studio, name, return_value=value)
            guard.start(); self.addCleanup(guard.stop)
        self.project = self.root / 'project'
        self.initial = self.make_project(self.project)
        self.app = self.application(self.project)
        self.machine_bytes = {str(path.relative_to(self.project)): path.read_bytes()
            for path in self.project.rglob('*.srt')}
        self.source_bytes = self.source.read_bytes()

    def make_project(self, folder, language='en'):
        config = runner.PipelineConfig(self.source, folder, language=language, target='zh-CN',
            translate=False, chunk_seconds=10, overlap_seconds=1)
        with patch.object(runner, 'probe_media', return_value=9000), \
                patch.object(runner, 'detect_silences', return_value=[]), \
                patch.object(runner, 'recognize_chunk', return_value=self.cues), \
                patch.object(runner, 'translate_texts', side_effect=AssertionError('No translation')):
            result = runner.run_pipeline(config)
        self.assertEqual(result['status'], 'complete')
        return result

    def application(self, folder):
        return studio.StudioController(source=str(self.source), campaign=str(folder),
            state_path=self.root / (folder.name + '-studio.json'))

    def preview(self):
        view = self.app.preview('main')
        self.assertIn('manual_review', view, 'Original-only results need in-app review')
        self.assertEqual(view['manual_review'].get('mode'), 'source_only')
        self.assertIs(view['manual_review'].get('acceptance_supported'), False)
        return view

    def request(self, **changes):
        view = self.preview()
        cue = view['cues'][0]
        return {'project_id': view['project_id'], 'sample': 'main',
            'expected_revision': view['manual_review']['revision'], 'cue_id': cue['id'],
            'start_ms': cue['start_ms'], 'end_ms': cue['end_ms'],
            'source_text': cue['source_text'], 'target_text': '', 'review_status': 'checked',
            'note': '', 'translation_confirmed': False, **changes}

    def assert_machine_preserved(self):
        for relative, raw in self.machine_bytes.items():
            self.assertEqual((self.project / relative).read_bytes(), raw)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertFalse((self.project / '中文草稿.srt').exists())
        self.assertFalse((self.project / '双语草稿.srt').exists())

    def test_original_only_preview_is_editable_without_writing_review_or_fake_translation(self):
        view = self.preview()
        self.assertEqual(view['manual_review']['summary']['checked'], 0)
        self.assertFalse(view['manual_review']['summary']['can_accept'])
        self.assertEqual(len(view['cues']), 4)
        self.assertTrue(all(cue['target_text'] == '' for cue in view['cues']))
        self.assertFalse((self.project / '人工校对').exists())
        self.assert_machine_preserved()

    def test_edit_download_and_version_are_the_same_corrected_original(self):
        response = self.app.review_cue(self.request(source_text='Corrected original',
            start_ms=200, end_ms=1200, note='heard and checked'))
        self.assertEqual(response['manual_review']['mode'], 'source_only')
        self.assertEqual(response['manual_review']['summary']['checked'], 1)
        self.assertEqual(response['cues'][0]['source_text'], 'Corrected original')
        downloaded = self.app.download_path('原文.srt', 'main', self.app._project_id())
        cues = parse_srt(downloaded.read_text(encoding='utf-8'))
        self.assertEqual((cues[0].start_ms, cues[0].end_ms, cues[0].text), (200, 1200, 'Corrected original'))
        saved = self.app.save_subtitles({'project_id': self.app._project_id(), 'sample': 'main'})
        subtitles = [Path(item['path']) for item in saved['files'] if Path(item['path']).suffix == '.srt']
        self.assertEqual(len(subtitles), 1)
        self.assertEqual(subtitles[0].read_bytes(), downloaded.read_bytes())
        manifest = json.loads((subtitles[0].parent / '版本记录.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['manual_review']['mode'], 'source_only')
        self.assertEqual([item['canonical'] for item in manifest['files']], ['原文.srt'])
        self.assert_machine_preserved()

    def test_compact_receipt_preserves_mode_capability_and_reopens_saved_revision(self):
        request = self.request(source_text='Persisted without translation', response_mode='cue')
        receipt = self.app.review_cue(request)
        self.assertEqual(receipt['kind'], 'review-cue')
        self.assertEqual(receipt['base_revision'], request['expected_revision'])
        self.assertEqual(receipt['manual_review']['mode'], 'source_only')
        self.assertIs(receipt['manual_review']['acceptance_supported'], False)
        reopened = self.application(self.project).preview('main')
        self.assertEqual(reopened['manual_review']['revision'], receipt['manual_review']['revision'])
        self.assertEqual(reopened['cues'][0]['source_text'], 'Persisted without translation')
        self.assertEqual(reopened['cues'][0]['review_status'], 'checked')
        self.assert_machine_preserved()

    def test_auto_source_language_can_be_corrected_without_translation(self):
        folder = self.root / 'auto-project'
        self.make_project(folder, 'auto')
        app = self.application(folder)
        view = app.preview('main')
        self.assertEqual(view.get('manual_review', {}).get('mode'), 'source_only')
        self.assertEqual(view['source_language'], 'auto')

    def test_existing_translated_tracks_remain_bilingual_when_translation_option_is_false(self):
        target = [Cue(cue.start_ms, cue.end_ms, '缓存译文') for cue in self.cues]
        (self.project / '中文草稿.srt').write_text(render_srt(target), encoding='utf-8')
        (self.project / '双语草稿.srt').write_text(render_srt(target), encoding='utf-8')
        view = self.app.preview('main')
        self.assertTrue(view.get('manual_review', {}).get('supported'))
        self.assertNotEqual(view['manual_review'].get('mode'), 'source_only')

    def test_incomplete_translation_task_does_not_silently_become_original_only(self):
        path = self.project / 'state.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['config']['translate'] = True
        path.write_text(json.dumps(state), encoding='utf-8')
        view = self.app.preview('main')
        self.assertNotEqual(view.get('manual_review', {}).get('mode'), 'source_only')

    def test_existing_source_record_reports_conflict_on_mode_change_and_keeps_bytes(self):
        self.app.review_cue(self.request())
        record = self.project / '人工校对' / '校对记录.json'
        before = record.read_bytes()
        path = self.project / 'state.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['config']['translate'] = True
        path.write_text(json.dumps(state), encoding='utf-8')
        view = self.app.preview('main')
        self.assertTrue(view['manual_review'].get('conflict'))
        self.assertEqual(record.read_bytes(), before)

    def test_original_only_rejects_hidden_translation_and_translation_downloads(self):
        for changes in ({'target_text': 'unexpected hidden draft'}, {'translation_confirmed': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.app.review_cue(self.request(**changes))
        for name in ('中文草稿.srt', '双语草稿.srt'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.app.download_path(name, 'main', self.app._project_id())
        self.assert_machine_preserved()

    def test_source_only_checked_does_not_grant_final_bilingual_acceptance(self):
        view = self.app.review_cue(self.request())
        with self.assertRaises(ValueError):
            self.app.review_accept({'project_id': self.app._project_id(), 'sample': 'main',
                'expected_revision': view['manual_review']['revision'], 'content_passed': True})
        self.assert_machine_preserved()

    def test_source_only_completion_does_not_claim_translated_drafts(self):
        self.assertIn('原文', self.initial['message'])
        self.assertNotIn('译文', self.initial['message'])

    def test_adopting_external_original_edit_does_not_claim_translation_backups(self):
        path = self.project / '原文.srt'
        path.write_text(path.read_text(encoding='utf-8').replace('Original sentence 1',
            'Externally corrected sentence'), encoding='utf-8', newline='\n')
        config = runner.PipelineConfig(self.source, self.project, language='en', target='zh-CN',
            translate=False, chunk_seconds=10, overlap_seconds=1)
        with patch.object(runner, 'probe_media', return_value=9000), \
                patch.object(runner, 'detect_silences', return_value=[]), \
                patch.object(runner, 'recognize_chunk', side_effect=AssertionError('No re-recognition')):
            result = runner.run_pipeline(config)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result.get('merged_source_edited'))
        self.assertIn('原文', result['message'])
        self.assertNotIn('译文', result['message'])
        self.assertIn('Externally corrected sentence', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
