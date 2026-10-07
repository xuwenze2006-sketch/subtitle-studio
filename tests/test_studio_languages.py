import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_studio


class StudioLanguageTests(unittest.TestCase):
    setUp = test_studio.StudioTests.setUp
    make_samples = test_studio.StudioTests.make_samples

    def test_local_source_and_target_are_independent(self):
        with patch.object(self.s.threading, 'Thread') as worker:
            self.app.start({'action': 'local', 'language': 'zh', 'target': 'en', 'translate': True})
        config = worker.call_args.kwargs['args'][1]
        self.assertEqual((config.language, config.target), ('zh', 'en'))

    def test_cloud_prepare_receives_selected_pair(self):
        with patch.object(self.s.threading, 'Thread') as worker:
            self.app.start({'action': 'prepare', 'language': 'en', 'target': 'ja'})
        command = worker.call_args.kwargs['args'][1]
        self.assertEqual(command[command.index('--language') + 1], 'en')
        self.assertEqual(command[command.index('--target') + 1], 'ja')

    def test_cloud_state_restores_pair_without_local_config(self):
        manifest = self.make_samples()
        manifest.update(language='zh', target='en')
        (self.campaign / 'campaign.json').write_text(json.dumps(manifest), encoding='utf-8')
        state = self.app.state()
        self.assertEqual((state['project_config']['language'], state['project_config']['target']), ('zh', 'en'))
        self.assertEqual(state['language_options']['sources'], ['ja', 'en', 'zh'])

    def test_preview_and_snapshot_use_actual_target_filename(self):
        config = {'source': str(self.source), 'project': str(self.campaign), 'language': 'zh', 'target': 'en'}
        (self.campaign / 'state.json').write_text(json.dumps({'config': config, 'status': 'complete'}), encoding='utf-8')
        srt = '1\n00:00:00,000 --> 00:00:01,000\n你好\n'
        (self.campaign / '原文.srt').write_text(srt, encoding='utf-8')
        (self.campaign / '英文草稿.srt').write_text(srt.replace('你好', 'Hello'), encoding='utf-8')
        (self.campaign / '中文草稿.srt').write_text(srt.replace('你好', 'stale'), encoding='utf-8')
        preview = self.app.preview('main')
        self.assertEqual(preview['target_language'], 'en')
        self.assertEqual(preview['cues'][0]['source_text'], '你好')
        self.assertEqual(preview['cues'][0]['target_text'], 'Hello')
        self.assertEqual(preview['downloads'], ['原文.srt', '英文草稿.srt'])
        with self.assertRaises(ValueError): self.app.download_path('中文草稿.srt', 'main')
        result = self.app.save_subtitles({'project_id': self.app._project_id(), 'sample': 'main'})
        self.assertEqual(len(result['files']), 2)
        record = json.loads((Path(result['folder']) / '版本记录.json').read_text(encoding='utf-8'))
        self.assertEqual((record['source_language'], record['target_language']), ('zh', 'en'))

    def test_legacy_project_keeps_japanese_chinese_defaults(self):
        self.make_samples()
        state = self.app.state()['project_config']
        self.assertEqual((state['language'], state['target']), ('ja', 'zh-CN'))

    def test_exported_video_requires_current_language_binding(self):
        manifest = self.make_samples()
        from subtitle_pipeline.integrity import sha256
        output = self.source.with_name(self.source.stem + '_英文字幕_修订版.mp4')
        output.write_bytes(b'fixture-video')
        manifest['source']['sha256'] = sha256(self.source)
        manifest.update(language='zh', target='en', status='exported', output=str(output),
                        output_sha256=sha256(output),
                        output_binding={'render_version': 1, 'source_sha256': manifest['source']['sha256']})
        path = self.campaign / 'campaign.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertIsNone(self.app.exported_video_path())
        manifest['output_binding'].update(language='zh', target='en')
        path.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(self.app.exported_video_path(), output)
        manifest['output_binding']['language'] = 'ja'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertIsNone(self.app.exported_video_path())


if __name__ == '__main__': unittest.main()
