"""Offline multilingual outputs, identity isolation and non-destructive edits."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner
from subtitle_pipeline.integrity import fingerprint
from subtitle_pipeline.languages import (default_target, manifest_languages, output_names,
                                         target_filename, validate_languages, video_output_path)
from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


class MultilingualPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.wav'
        self.source.write_bytes(b'offline audio fixture')
        self.recognized, self.translated = [], []

    def config(self, language, target, **changes):
        return replace(runner.PipelineConfig(self.source, self.root/(language+'-'+target),
                       language=language, target=target, chunk_seconds=10, overlap_seconds=1), **changes)

    def services(self, fail=False):
        def recognize(config, chunk, folder, stop):
            if fail: self.fail('completed recognition repeated')
            self.recognized.append(config.language)
            return [Cue(100, 1100, config.language+' source')]
        def translate(texts, source, target, *args, **kwargs):
            if fail: self.fail('completed translation repeated')
            self.translated.append((source, target))
            return [target+' translation' for text in texts]
        return patch.multiple(runner, probe_media=lambda path, **kw: 5000, detect_silences=lambda *a: [],
                              recognize_chunk=recognize, translate_texts=translate)

    def test_all_six_pairs_write_named_outputs_and_resume_without_calls(self):
        for language in ('ja', 'en', 'zh'):
            for target in ('ja', 'en', 'zh-CN'):
                if target.split('-')[0] == language: continue
                with self.subTest(language=language, target=target):
                    config = self.config(language, target)
                    with self.services(): state = runner.run_pipeline(config)
                    self.assertEqual(state['status'], 'complete')
                    self.assertEqual(self.recognized[-1], language)
                    self.assertEqual(self.translated[-1], (language, target))
                    for name in output_names(target): self.assertTrue((config.project/name).is_file())
                    if target != 'zh-CN': self.assertFalse((config.project/'中文草稿.srt').exists())
                    original = parse_srt((config.project/'原文.srt').read_text(encoding='utf-8'))
                    translated = parse_srt((config.project/target_filename(target)).read_text(encoding='utf-8'))
                    self.assertEqual([(c.start_ms,c.end_ms) for c in original],
                                     [(c.start_ms,c.end_ms) for c in translated])
                    with self.services(fail=True):
                        self.assertEqual(runner.run_pipeline(config)['status'], 'complete')

    def test_changing_either_language_rejects_old_project_before_asr_or_translation(self):
        config = self.config('en', 'ja')
        with self.services(): runner.run_pipeline(config)
        for revised in (replace(config, language='zh'), replace(config, target='zh-CN')):
            with self.subTest(language=revised.language, target=revised.target), self.services(fail=True):
                with self.assertRaises(ValueError): runner.run_pipeline(revised)

    def test_english_and_japanese_manual_translation_is_retained_on_resume(self):
        for target in ('en', 'ja'):
            with self.subTest(target=target):
                config = self.config('zh', target)
                with self.services(): runner.run_pipeline(config)
                public = config.project/'片段'/'0001'/target_filename(target)
                public.write_text(render_srt([Cue(100, 1100, 'human corrected '+target)]), encoding='utf-8')
                with self.services(fail=True): state = runner.run_pipeline(config)
                self.assertEqual(state['status'], 'complete')
                self.assertEqual(state['review_status'], 'needs_review')
                self.assertIn('human corrected '+target, public.read_text(encoding='utf-8'))
                self.assertIn('human corrected '+target,
                              (config.project/target_filename(target)).read_text(encoding='utf-8'))

    def test_translation_disabled_keeps_original_only(self):
        config = self.config('zh', 'en', translate=False)
        with self.services(): state = runner.run_pipeline(config)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(self.translated, [])
        self.assertTrue((config.project/'原文.srt').is_file())
        self.assertFalse((config.project/'英文草稿.srt').exists())

    def test_pending_english_output_checkpoint_can_recover_after_interrupted_publication(self):
        config = self.config('zh', 'en')
        with self.services(): state = runner.run_pipeline(config)
        relative = target_filename(config.target)
        digest = state['generated_hashes'].pop(relative)
        state['pending_outputs'] = {'version':1, 'identity':fingerprint(state['identity']),
            'checkpoint':runner._output_checkpoint(state), 'outputs':{relative:digest}}
        runner.atomic_json(config.project/'state.json', state)
        with self.services(fail=True): resumed = runner.run_pipeline(config)
        self.assertNotIn('pending_outputs', resumed)
        self.assertNotIn(relative, resumed.get('manual_outputs', []))
        self.assertEqual(resumed['status'], 'complete')


class LanguageContractTests(unittest.TestCase):
    def test_legacy_manifest_and_filenames_stay_compatible(self):
        self.assertEqual(manifest_languages({}), ('ja', 'zh-CN'))
        self.assertEqual(output_names('zh-CN'), ('原文.srt','中文草稿.srt','双语草稿.srt'))
        self.assertEqual(video_output_path(Path('movie.mp4'), 'zh-CN').name, 'movie_中文字幕_修订版.mp4')
        self.assertEqual(default_target('zh'), 'en')
        self.assertEqual(default_target('en'), 'zh-CN')
        validate_languages('zh', 'zh-CN')  # Existing same-language jobs remain readable.

    def test_unsupported_language_is_rejected_before_request(self):
        for source, target in [('fr','en'), ('en','fr'), ([], 'en'), ('ja', None), ('auto','en')]:
            with self.subTest(source=source,target=target), self.assertRaises(ValueError):
                validate_languages(source, target)
        validate_languages('auto', 'ja', allow_auto=True)


if __name__ == '__main__': unittest.main()
