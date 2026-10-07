"""Cancellation finalizes real part files without redundant completion scans."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import shutil
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from subtitle_pipeline import cloud_settings, integrity, runner as r
from subtitle_pipeline.subtitles import Cue, parse_srt


class CancelledFinalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='runner-finalization-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'synthetic.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * (30 * 16000))
        self.config = r.PipelineConfig(
            self.source, self.root / 'project', chunk_seconds=10,
            overlap_seconds=0, workers=1, asr_provider='qwen_asr',
            translation_provider='google', workflow_stage='draft')
        self.stop = threading.Event()
        self.verifications = []
        self.progress = []
        guard = ExitStack()
        self.addCleanup(guard.close)
        for target, name in (
                (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                (socket, 'create_connection'), (cloud_settings, 'load_settings'),
                (r, 'run_process'), (r, 'engine_paths')):
            guard.enter_context(patch.object(target, name, side_effect=AssertionError(
                'No network, credentials, or media subprocess in this fixture')))

    def recognize(self, config, chunk, folder, stop, **kwargs):
        (folder / 'recognition-metadata.json').write_text(
            '{"issues":[],"metadata":{}}', encoding='utf-8')
        (folder / 'asr-response.json').write_text(
            json.dumps({'synthetic': True, 'index': chunk.index}), encoding='utf-8')
        start = chunk.core_start_ms - chunk.audio_start_ms + 100
        return [Cue(start, start + 800, f'part {chunk.index}')]

    def prepare(self, config, state, stop):
        timeline = config.project / 'timeline.wav'
        shutil.copy2(config.source, timeline)
        state['timeline_hash'] = integrity.sha256(timeline)
        return r._pcm_file_token(timeline, stop)

    @contextmanager
    def services(self, *, recognize=None, translate=None, prepare=None):
        real_verify = r.verify_part

        def verify(folder, chunk, part, **kwargs):
            valid = real_verify(folder, chunk, part, **kwargs)
            self.verifications.append((chunk.index, kwargs['translated'], valid))
            return valid

        context = (SimpleNamespace(asr_endpoint='https://invalid.example'),
                   SimpleNamespace(summary=lambda: {}))
        with patch.multiple(
                r, probe_media=lambda *a, **kw: 30000,
                detect_silences=lambda *a: [],
                recognize_chunk=recognize or self.recognize,
                translate_texts=translate or (lambda texts, *a, **kw: ['译 ' + t for t in texts]),
                prepare_cloud_audio=prepare or self.prepare,
                cloud_context=lambda config: context, verify_part=verify):
            yield

    def run_pipeline(self, progress=None):
        def observe(state):
            self.progress.append(state['status'])
            if progress is not None:
                progress(state)
        return r.run_pipeline(self.config, self.stop, observe)

    def persisted(self, status):
        state = json.loads((self.config.project / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], status)
        self.assertEqual(self.progress[-1], status)
        self.assertFalse((self.config.project / 'run.lock').exists())
        return state

    def test_two_inflight_valid_results_are_saved_without_completion_scan(self):
        self.config.workers = 2
        barrier = threading.Barrier(3)
        submitted = []
        real_wait = r.futures.wait

        def recognize(config, chunk, folder, stop, **kwargs):
            submitted.append(chunk.index)
            barrier.wait(timeout=4)
            if not stop.wait(4):
                raise AssertionError('Cancellation handshake did not complete')
            return self.recognize(config, chunk, folder, stop, **kwargs)

        def wait(*args, **kwargs):
            if not self.stop.is_set():
                barrier.wait(timeout=4)
                self.stop.set()
            return real_wait(*args, **kwargs)

        with self.services(recognize=recognize,
                           translate=lambda *a, **kw: self.fail('New translation after stop')), \
                patch.object(r.futures, 'wait', side_effect=wait):
            state = self.run_pipeline()
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(sorted(submitted), [0, 1])
        saved = self.persisted('cancelled')
        self.assertEqual((saved['recognized'], saved['translated']), (2, 0))
        cues = parse_srt((self.config.project / '原文.srt').read_text(encoding='utf-8'))
        self.assertEqual([cue.text for cue in cues], ['part 0', 'part 1'])
        self.assertTrue(all(saved['parts'][str(i)]['raw_response_hash'] for i in (0, 1)))
        self.assertEqual(self.verifications, [])

    def test_last_translation_returned_after_stop_is_saved_without_completion_scan(self):
        def translate(texts, *args, **kwargs):
            if texts == ['part 2']:
                self.stop.set()
            return ['译 ' + text for text in texts]

        with self.services(translate=translate):
            state = self.run_pipeline()
        self.assertEqual(state['status'], 'cancelled')
        saved = self.persisted('cancelled')
        self.assertEqual((saved['recognized'], saved['translated']), (3, 3))
        self.assertEqual(saved['parts']['2']['translation'], 'done')
        cues = parse_srt((self.config.project / '中文草稿.srt').read_text(encoding='utf-8'))
        self.assertEqual([cue.text for cue in cues], ['译 part 0', '译 part 1', '译 part 2'])
        self.assertEqual(self.verifications, [])

    def test_cancelled_complete_cache_preserves_manual_translation_backup_and_auto_update(self):
        with self.services():
            self.assertEqual(self.run_pipeline()['status'], 'complete')
        self.verifications.clear()
        target = self.config.project / '中文草稿.srt'
        manual = '1\n00:00:00,100 --> 00:00:00,900\n人工保留译文\n'.encode('utf-8')
        target.write_bytes(manual)

        def stop_on_running(state):
            if state['status'] == 'running':
                self.stop.set()

        with self.services(recognize=lambda *a, **kw: self.fail('Cached ASR repeated'),
                           translate=lambda *a, **kw: self.fail('Cached translation repeated')):
            state = self.run_pipeline(stop_on_running)
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(target.read_bytes(), manual)
        backups = list((self.config.project / '用户修改备份').glob('*'))
        self.assertTrue(any(path.read_bytes() == manual for path in backups if path.is_file()))
        automatic = self.config.project / '自动更新' / '中文草稿.srt'
        self.assertEqual([cue.text for cue in parse_srt(automatic.read_text(encoding='utf-8'))],
                         ['译 part 0', '译 part 1', '译 part 2'])
        saved = self.persisted('cancelled')
        self.assertEqual((saved['recognized'], saved['translated']), (3, 3))
        self.assertIn('中文草稿.srt', saved['manual_outputs'])
        self.assertEqual(Path(saved['outputs']['中文草稿.srt']), Path('自动更新/中文草稿.srt'))
        self.assertEqual(self.verifications, [])

    def test_scheduler_error_with_stop_keeps_exception_and_persists_cancelled(self):
        original = RuntimeError('scheduler wait failure')

        def fail_wait(*args, **kwargs):
            self.stop.set()
            raise original

        with self.services(), patch.object(r.futures, 'wait', side_effect=fail_wait):
            with self.assertRaises(RuntimeError) as caught:
                self.run_pipeline()
        self.assertIs(caught.exception, original)
        self.persisted('cancelled')
        self.assertEqual(self.verifications, [])

    def test_preparation_cancelled_without_stop_persists_without_completion_scan(self):
        def cancel_prepare(*args):
            raise r.Cancelled('preparation cancelled')

        with self.services(prepare=cancel_prepare,
                           recognize=lambda *a, **kw: self.fail('ASR after preparation cancellation')):
            state = self.run_pipeline()
        self.assertFalse(self.stop.is_set())
        self.assertEqual(state['status'], 'cancelled')
        self.persisted('cancelled')
        self.assertEqual(self.verifications, [])

    def test_uncancelled_complete_still_verifies_all_asr_and_translation_evidence(self):
        with self.services():
            state = self.run_pipeline()
        self.assertEqual(state['status'], 'complete')
        self.persisted('complete')
        self.assertEqual(self.verifications, [
            (0, False, True), (1, False, True), (2, False, True),
            (0, True, True), (1, True, True), (2, True, True)])

    def test_stop_after_completion_scan_started_retains_existing_finalization_behavior(self):
        with self.services():
            real_verify = r.verify_part

            def stop_after_verify(*args, **kwargs):
                valid = real_verify(*args, **kwargs)
                self.stop.set()
                return valid

            with patch.object(r, 'verify_part', side_effect=stop_after_verify):
                state = self.run_pipeline()
        self.assertEqual(state['status'], 'cancelled')
        self.persisted('cancelled')
        self.assertEqual(self.verifications, [
            (0, False, True), (1, False, True), (2, False, True),
            (0, True, True), (1, True, True), (2, True, True)])

    def test_uncancelled_corrupt_raw_response_cannot_be_reported_complete(self):
        corrupted = False

        def corrupt_after_last_result(state):
            nonlocal corrupted
            if state.get('translated') == 3 and not corrupted:
                (self.config.project / '片段' / '0001' / 'asr-response.json').write_text(
                    '{"modified":true}', encoding='utf-8')
                corrupted = True

        with self.services():
            state = self.run_pipeline(corrupt_after_last_result)
        self.assertTrue(corrupted)
        self.assertEqual(state['status'], 'asr_incomplete')
        self.persisted('asr_incomplete')
        self.assertTrue(any(not translated and not valid for _, translated, valid in self.verifications))

    def test_uncancelled_corrupt_target_hash_cannot_be_reported_complete(self):
        corrupted = False

        def corrupt_after_last_result(state):
            nonlocal corrupted
            if state.get('translated') == 3 and not corrupted:
                path = self.config.project / '片段' / '0001' / 'target.local.srt'
                path.write_text(path.read_text(encoding='utf-8').replace('译 part', '变 part'),
                                encoding='utf-8')
                corrupted = True

        with self.services():
            state = self.run_pipeline(corrupt_after_last_result)
        self.assertTrue(corrupted)
        self.assertEqual(state['status'], 'translation_incomplete')
        self.persisted('translation_incomplete')
        self.assertEqual([call for call in self.verifications if not call[1]],
                         [(0, False, True), (1, False, True), (2, False, True)])
        self.assertTrue(any(translated and not valid for _, translated, valid in self.verifications))


if __name__ == '__main__':
    unittest.main()
