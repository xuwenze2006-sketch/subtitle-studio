"""Offline cross-chunk scheduling, frozen retry identity and edit protection."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner
from subtitle_pipeline.cloud_budget import BudgetLedger
from subtitle_pipeline.subtitles import Cue


class RunnerContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.wav'
        self.source.write_bytes(b'offline synthetic source')
        self.project = self.root / 'project'
        self.config = runner.PipelineConfig(self.source, self.project, chunk_seconds=10,
            overlap_seconds=1, workers=2, translation_provider='deepseek')
        self.ledger = BudgetLedger(self.root / 'ledger.json')
        self.calls = []

    def state(self):
        return json.loads((self.project / 'state.json').read_text(encoding='utf-8'))

    def recognize(self, config, chunk, folder, stop):
        start = chunk.core_start_ms - chunk.audio_start_ms + 100
        return [Cue(start, start + 800, f'part {chunk.index}')]

    def translate(self, texts, *args, **kwargs):
        self.calls.append((texts, kwargs.get('context_before')))
        index = int(texts[0].split()[-1])
        state = self.state()
        if state.get('translation_context_version') == 1:
            part = state['parts'][str(index)]
            self.assertIn(part['source_hash'], part['translation_context_bindings'])
            self.assertEqual(part['translation_context_bindings'][part['source_hash']],
                part['translation_contexts'][part['source_hash']]['sha256'])
        return ['translation ' + text for text in texts]

    @contextmanager
    def services(self, recognize=None, translate=None):
        settings = SimpleNamespace(deepseek_key='offline-only', deepseek_input_rate=2.0, deepseek_output_rate=8.0)
        with patch.object(runner, 'probe_media', return_value=25000), \
             patch.object(runner, 'detect_silences', return_value=[]), \
             patch.object(runner, 'recognize_chunk', side_effect=recognize or self.recognize), \
             patch.object(runner, 'cloud_context', return_value=(settings, self.ledger)), \
             patch('subtitle_pipeline.deepseek_translate.translate_texts', side_effect=translate or self.translate):
            yield

    def test_out_of_order_asr_uses_temporal_previous_and_persists_before_submit(self):
        second_ready = threading.Event()
        def recognize(config, chunk, folder, stop):
            if chunk.index == 0: self.assertTrue(second_ready.wait(4))
            if chunk.index == 1: second_ready.set()
            return self.recognize(config, chunk, folder, stop)
        with self.services(recognize=recognize): state = runner.run_pipeline(self.config)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(state.get('translation_context_version'), 1)
        contexts = {texts[0]: context for texts, context in self.calls}
        self.assertEqual(contexts, {'part 0': [], 'part 1': ['part 0'], 'part 2': ['part 1']})

    def test_first_translation_still_overlaps_next_recognition(self):
        translated = threading.Event()
        def recognize(config, chunk, folder, stop):
            if chunk.index == 1: self.assertTrue(translated.wait(4), 'unnecessary lookahead blocked first translation')
            return self.recognize(config, chunk, folder, stop)
        def translate(texts, *args, **kwargs):
            result = self.translate(texts, *args, **kwargs)
            if texts == ['part 0']: translated.set()
            return result
        with self.services(recognize, translate): state = runner.run_pipeline(self.config)
        self.assertEqual(state['status'], 'complete')
        self.assertTrue(translated.is_set())

    def test_failed_previous_chunk_does_not_change_frozen_context_on_resume(self):
        def recognize(config, chunk, folder, stop):
            if chunk.index == 0: raise RuntimeError('synthetic recognition failure')
            return self.recognize(config, chunk, folder, stop)
        def translate(texts, *args, **kwargs):
            self.translate(texts, *args, **kwargs)
            if texts == ['part 1']: raise RuntimeError('response interrupted after frozen snapshot')
            return ['translation ' + text for text in texts]
        with self.services(recognize, translate):
            self.assertEqual(runner.run_pipeline(self.config)['status'], 'asr_incomplete')
        with self.services(): self.assertEqual(runner.run_pipeline(self.config)['status'], 'complete')
        contexts = [context for texts, context in self.calls if texts == ['part 1']]
        self.assertEqual(contexts, [[], []])

    def test_missing_context_after_attempt_prevents_new_submission(self):
        def translate(texts, *args, **kwargs):
            result = self.translate(texts, *args, **kwargs)
            if texts == ['part 1']: raise RuntimeError('lost response')
            return result
        with self.services(translate=translate): runner.run_pipeline(self.config)
        state = self.state()
        state['parts']['1'].pop('translation_contexts', None)
        runner.atomic_json(self.project / 'state.json', state)
        with self.services(translate=lambda *_a, **_k: self.fail('damaged snapshot must block translation')):
            resumed = runner.run_pipeline(self.config)
        self.assertEqual(resumed['status'], 'translation_incomplete')
        self.assertIn('上下文', resumed['parts']['1']['translation_error'])

    def test_rebuilding_missing_source_cache_keeps_original_context_history(self):
        def recognize(config, chunk, folder, stop):
            if chunk.index == 0: raise RuntimeError('synthetic first recognition failure')
            return self.recognize(config, chunk, folder, stop)
        def translate(texts, *args, **kwargs):
            result = self.translate(texts, *args, **kwargs)
            if texts == ['part 1']: raise RuntimeError('unknown paid response')
            return result
        with self.services(recognize, translate): runner.run_pipeline(self.config)
        before = self.state()['parts']['1']['translation_contexts']
        (self.project / '片段' / '0002' / 'source.local.srt').unlink()
        with self.services(): resumed = runner.run_pipeline(self.config)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual([context for texts, context in self.calls if texts == ['part 1']], [[], []])
        self.assertEqual(resumed['parts']['1']['translation_contexts'], before)

    def test_snapshot_write_failure_prevents_paid_worker_from_starting(self):
        write = runner.atomic_json
        def fail_context(path, value):
            if Path(path).name == 'state.json' and any(
                    part.get('translation_context_bindings') for part in value.get('parts', {}).values()):
                raise OSError('snapshot disk failure')
            return write(path, value)
        with self.services(translate=lambda *_a, **_k: self.fail('unpersisted snapshot was used')), \
             patch.object(runner, 'atomic_json', side_effect=fail_context):
            with self.assertRaises(OSError): runner.run_pipeline(self.config)
        self.assertFalse(self.calls)

    def test_missing_context_policy_cannot_downgrade_an_existing_contextual_task(self):
        with self.services(): runner.run_pipeline(self.config)
        state = self.state()
        state.pop('translation_context_version')
        state['parts']['1'].pop('translation')
        runner.atomic_json(self.project / 'state.json', state)
        with self.services(translate=lambda *_a, **_k: self.fail('lost policy must not create old-format requests')):
            with self.assertRaisesRegex(ValueError, '上下文'):
                runner.run_pipeline(self.config)

    def test_source_changed_during_translation_is_not_marked_as_new_source_result(self):
        def translate(texts, *args, **kwargs):
            result = self.translate(texts, *args, **kwargs)
            if texts == ['part 0']:
                path = self.project / '片段' / '0001' / 'source.local.srt'
                path.write_text(path.read_text(encoding='utf-8').replace('part 0', 'edited source'), encoding='utf-8')
            return result
        with self.services(translate=translate): state = runner.run_pipeline(self.config)
        self.assertNotEqual(state['status'], 'complete')
        self.assertNotEqual(state['parts']['0'].get('translation'), 'done')
        self.assertFalse((self.project / '片段' / '0001' / 'target.local.srt').exists())

    def test_legacy_project_keeps_old_requests_and_completed_project_skips_translation(self):
        with self.services(): runner.run_pipeline(self.config)
        state = self.state()
        state.pop('translation_context_version', None)
        for part in state['parts'].values():
            part.pop('translation_contexts', None)
            part.pop('translation_context_bindings', None)
        state['parts']['1'].pop('translation')
        runner.atomic_json(self.project / 'state.json', state)
        self.calls.clear()
        with self.services(): resumed = runner.run_pipeline(self.config)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(self.calls, [(['part 1'], None)])
        self.assertNotIn('translation_context_version', resumed)
        with self.services(translate=lambda *_a, **_k: self.fail('completed translation repeated')):
            self.assertEqual(runner.run_pipeline(self.config)['status'], 'complete')
