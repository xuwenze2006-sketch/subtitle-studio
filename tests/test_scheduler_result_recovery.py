"""Owned successful futures survive scheduler faults without another ASR pass.

The pipeline, executor, Qwen parser and temporary budget ledger remain real.
Only PCM extraction/probing and HTTP transport use deterministic offline fakes.
"""
import base64
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import wave

from subtitle_pipeline import cloud_settings, qwen_asr, runner as r
from subtitle_pipeline.cloud_budget import BudgetLedger
from subtitle_pipeline.integrity import sha256
from subtitle_pipeline.subtitles import parse_srt


class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'application/json'}


class SchedulerResultRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='scheduler-result-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.wav'
        with wave.open(str(self.source), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b''.join(bytes([value, 0]) * 80000 for value in (10, 20, 30, 40)))
        self.config = r.PipelineConfig(self.source, self.root / 'project',
            chunk_seconds=10, overlap_seconds=0, workers=2, asr_provider='qwen_asr',
            translation_provider='google', translate=False, workflow_stage='draft',
            budget_ledger=self.root / 'local-budget.json')
        self.ledger = BudgetLedger(self.config.budget_ledger)
        self.stop = threading.Event()
        self.primary = RuntimeError('owned scheduler fault')
        self.calls = {'media': [], 'adapter': [], 'send': [], 'translation': []}
        self.futures = []
        self.progress = []
        self.audio_indices = {}
        guard = ExitStack()
        self.addCleanup(guard.close)
        for target, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                (socket, 'create_connection'), (urllib.request, 'urlopen'),
                (urllib.request, 'build_opener'), (cloud_settings, 'load_settings'),
                (r, 'engine_paths')):
            guard.enter_context(patch.object(target, name,
                side_effect=AssertionError('No network, credentials or model process allowed')))

    def fake_media(self, args, log, stop, **kwargs):
        destination = Path(args[-1])
        self.assertEqual(args[0], 'ffmpeg')
        self.assertEqual(destination.suffix, '.wav')
        self.assertTrue(destination.is_relative_to(self.root))
        input_path = Path(args[args.index('-i') + 1])
        start = float(args[args.index('-ss') + 1]) if '-ss' in args else 0
        duration = float(args[args.index('-t') + 1]) if '-t' in args else 20
        with wave.open(str(input_path), 'rb') as stream:
            stream.setpos(round(start * 16000))
            pcm = stream.readframes(round(duration * 16000))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(pcm)
        if destination.name == 'audio.wav':
            self.audio_indices[sha256(destination)] = int(destination.parent.name) - 1
        self.calls['media'].append(destination)

    def fake_open(self, request, timeout):
        self.assertEqual(request.get_header('Authorization'), 'Bearer offline-fixture')
        request_body = json.loads(request.data)
        encoded = request_body['input']['messages'][0]['content'][0]['input_audio']['data']
        audio = base64.b64decode(encoded.split(',', 1)[1])
        index = self.audio_indices[hashlib.sha256(audio).hexdigest()]
        with wave.open(io.BytesIO(audio), 'rb') as stream:
            seconds = stream.getnframes() / stream.getframerate()
        text = f'合成片段{index}'
        payload = {'request_id': f'synthetic-{index}',
            'output': {'text': text, 'sentence': {'text': text, 'begin_time': 100,
                'end_time': 900, 'sentence_id': 1, 'sentence_end': True, 'channel_id': 0,
                'words': [{'text': text, 'begin_time': 100, 'end_time': 900, 'fixed': True}]}},
            'usage': {'duration': seconds, 'input_tokens': 100, 'output_tokens': 20,
                      'total_tokens': 120}}
        self.calls['send'].append(index)
        return Response(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    @contextmanager
    def services(self, *, transform=None, cancel_second=False):
        native_recognize = r.recognize_chunk
        case = self
        release_first = threading.Event()
        settings = SimpleNamespace(asr_endpoint='https://dashscope.aliyuncs.com/api/v1',
            asr_key='offline-fixture', asr_input_rate=.8, asr_output_rate=2.7)

        class ObservedExecutor(ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                if cancel_second:
                    kwargs['max_workers'] = 1
                super().__init__(*args, **kwargs)

            def submit(self, function, *args, **kwargs):
                index = args[0].index

                def work():
                    if cancel_second and index == 0 and not release_first.wait(5):
                        raise AssertionError('Queued cancellation handshake failed')
                    value = function(*args, **kwargs)
                    return transform(index, value) if transform else value

                future = super().submit(work)
                record = {'future': future, 'index': index, 'result_calls': 0,
                          'function': function.__name__}
                real_result = future.result

                def result(*result_args, **result_kwargs):
                    record['result_calls'] += 1
                    return real_result(*result_args, **result_kwargs)

                future.result = result
                case.futures.append(record)
                if cancel_second and index == 1:
                    case.assertTrue(future.cancel(), 'The second real queued future must cancel')
                    release_first.set()
                return future

        def recognize(*args, **kwargs):
            self.calls['adapter'].append(args[1].index)
            return native_recognize(*args, **kwargs)

        def translate(texts, *args, **kwargs):
            self.calls['translation'].append(list(texts))
            return ['译 ' + text for text in texts]

        def probe(path, **kwargs):
            with wave.open(str(path), 'rb') as stream:
                return round(stream.getnframes() * 1000 / stream.getframerate())

        with patch.multiple(r, run_process=self.fake_media, probe_media=probe,
                detect_silences=lambda *args: [], recognize_chunk=recognize,
                translate_texts=translate, cloud_context=lambda config: (settings, self.ledger)), \
                patch.object(qwen_asr, '_open', side_effect=self.fake_open), \
                patch.object(r.futures, 'ThreadPoolExecutor', ObservedExecutor):
            yield

    def run_fault(self, *, normal_stop=False, fault_on_wait=1, after_done=None,
                  set_stop=True):
        real_wait = r.futures.wait
        waits = 0

        def wait(futures, *args, **kwargs):
            nonlocal waits
            done, pending = real_wait(futures, timeout=5, return_when=ALL_COMPLETED)
            self.assertFalse(pending, 'Offline worker fixture did not finish')
            waits += 1
            if waits == fault_on_wait:
                if after_done:
                    after_done()
                if set_stop:
                    self.stop.set()
                if not normal_stop:
                    raise self.primary
            return done, pending

        def progress(state):
            self.progress.append((state['status'], state['recognized'], state['translated']))

        with patch.object(r.futures, 'wait', side_effect=wait):
            if normal_stop:
                result = r.run_pipeline(self.config, self.stop, progress)
                self.assertEqual(result['status'], 'cancelled')
            else:
                with self.assertRaises(RuntimeError) as caught:
                    r.run_pipeline(self.config, self.stop, progress)
                self.assertIs(caught.exception, self.primary)
        state = json.loads((self.config.project / 'state.json').read_text(encoding='utf-8'))
        expected_status = 'cancelled' if set_stop else 'asr_incomplete'
        self.assertEqual(state['status'], expected_status)
        self.assertEqual(self.progress[-1][0], expected_status)
        self.assertFalse((self.config.project / 'run.lock').exists())
        return state

    def combined(self):
        return [cue.text for cue in parse_srt(
            (self.config.project / '原文.srt').read_text(encoding='utf-8'))]

    def assert_only_first_accepted(self, state):
        self.assertNotEqual(state['parts']['1'].get('asr'), 'done')
        self.assertEqual(state['recognized'], 1)
        self.assertEqual(state['parts']['0']['asr'], 'done')
        self.assertEqual(self.combined(), ['合成片段0'])

    def test_two_successful_owned_results_survive_exact_scheduler_error(self):
        with self.services():
            state = self.run_fault()
        self.assertEqual(state['recognized'], 2)
        self.assertEqual(self.combined(), ['合成片段0', '合成片段1'])
        for index in (0, 1):
            part = state['parts'][str(index)]
            folder = self.config.project / '片段' / f'{index+1:04d}'
            self.assertEqual(part['raw_response_hash'], sha256(folder / 'asr-response.json'))
            self.assertEqual(part['source_hash'], sha256(folder / 'source.local.srt'))
        self.assertEqual([record['result_calls'] for record in self.futures], [1, 1])

    def test_resume_needs_no_reextraction_adapter_call_or_new_send(self):
        with self.services():
            self.run_fault()
            counts = {key: len(value) for key, value in self.calls.items()}
            budget = self.ledger.summary()
            raw = {path: sha256(path) for path in (self.root / 'responses' / 'qwen').glob('*.json')}
            self.stop.clear()
            resumed = r.run_pipeline(self.config, self.stop)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(resumed['recognized'], 2)
        self.assertEqual(self.ledger.summary(), budget)
        self.assertEqual({path: sha256(path) for path in raw}, raw)
        self.assertEqual({key: len(value) for key, value in self.calls.items()}, counts)

    def test_failed_future_files_are_not_accepted_but_successful_sibling_is(self):
        worker_error = RuntimeError('owned worker failed after writing files')

        def transform(index, value):
            if index == 1:
                raise worker_error
            return value

        with self.services(transform=transform):
            state = self.run_fault()
        self.assertIs(self.futures[1]['future'].exception(), worker_error)
        self.assertTrue((self.config.project / '片段' / '0002' / 'source.local.srt').is_file())
        self.assert_only_first_accepted(state)

    def test_real_cancelled_queued_future_is_not_accepted(self):
        with self.services(cancel_second=True):
            state = self.run_fault()
        self.assertTrue(self.futures[1]['future'].cancelled())
        self.assertEqual(self.calls['adapter'], [0])
        self.assertEqual(self.calls['send'], [0])
        self.assert_only_first_accepted(state)

    def test_malformed_result_shapes_do_not_mark_done(self):
        for index, malformed in enumerate((None, [], {'unrelated': True})):
            with self.subTest(result=malformed):
                self.config.project = self.root / f'malformed-{index}'
                self.stop.clear()
                with self.services(transform=lambda i, value: malformed if i == 1 else value):
                    state = self.run_fault()
                self.assert_only_first_accepted(state)

    def test_mismatched_result_hash_does_not_mark_done(self):
        def transform(index, value):
            return {**value, 'source_hash': '0' * 64} if index == 1 else value
        with self.services(transform=transform):
            state = self.run_fault()
        self.assert_only_first_accepted(state)

    def test_foreign_source_or_chunk_evidence_does_not_mark_done(self):
        for index, change in enumerate(({'source_sha256': '0' * 64},
                                       {'audio_range_ms': [0, 10000]}, {'provider': 'whisper_cpp'})):
            with self.subTest(change=change):
                self.config.project = self.root / f'foreign-{index}'
                self.stop.clear()

                def transform(i, value):
                    return {**value, 'asr_evidence': {**value['asr_evidence'], **change}} if i == 1 else value

                with self.services(transform=transform):
                    state = self.run_fault()
                self.assert_only_first_accepted(state)

    def test_file_changed_after_future_completion_is_preserved_and_not_accepted(self):
        path = self.config.project / '片段' / '0002' / 'source.local.srt'
        changed = '1\n00:00:00,100 --> 00:00:00,900\n人工改动保留\n'
        with self.services():
            state = self.run_fault(after_done=lambda: path.write_text(changed, encoding='utf-8'))
        self.assertEqual(path.read_text(encoding='utf-8'), changed)
        self.assert_only_first_accepted(state)

    def test_public_source_edit_after_asr_future_is_preserved_and_not_accepted(self):
        path = self.config.project / '片段' / '0002' / '原文.srt'
        changed = '1\n00:00:10,100 --> 00:00:10,900\n公开原文人工修改\n'.encode('utf-8')
        with self.services():
            state = self.run_fault(after_done=lambda: path.write_bytes(changed))
        self.assertEqual(path.read_bytes(), changed)
        self.assert_only_first_accepted(state)
        self.assertEqual(sorted(self.calls['send']), [0, 1])
        self.assertEqual(sorted(self.calls['adapter']), [0, 1])

    def test_incorrect_asr_cue_count_is_not_accepted(self):
        for index, count in enumerate((99, True)):
            with self.subTest(count=count):
                self.config.project = self.root / f'count-{index}'
                self.stop.clear()
                with self.services(transform=lambda i, value: {**value, 'cues': count} if i == 1 else value):
                    state = self.run_fault()
                self.assert_only_first_accepted(state)

    def test_exception_finalization_starts_no_translation_or_remaining_asr(self):
        self.config.chunk_seconds = 5
        self.config.translate = True
        with self.services():
            state = self.run_fault()
        self.assertEqual(sorted(self.calls['adapter']), [0, 1])
        self.assertEqual(sorted(self.calls['send']), [0, 1])
        self.assertEqual(self.calls['translation'], [])
        self.assertEqual([(item['function'], item['index']) for item in self.futures],
                         [('asr_task', 0), ('asr_task', 1)])
        self.assertEqual(state['recognized'], 2)
        self.assertEqual(state['translated'], 0)

    def test_already_consumed_results_are_not_replayed_during_fault_recovery(self):
        self.config.chunk_seconds = 5
        recovery_started = False
        recovery_source_reads = []
        original_open = Path.open

        def begin_recovery():
            nonlocal recovery_started
            recovery_started = True

        def opened(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if recovery_started and path.name == 'source.local.srt' and mode == 'rb':
                recovery_source_reads.append(int(path.parent.name) - 1)
            return original_open(path, *args, **kwargs)

        with self.services(), patch.object(Path, 'open', new=opened):
            state = self.run_fault(fault_on_wait=2, after_done=begin_recovery)
        self.assertEqual(state['recognized'], 4)
        self.assertEqual(self.combined(), ['合成片段0', '合成片段1', '合成片段2', '合成片段3'])
        self.assertEqual([record['result_calls'] for record in self.futures], [1, 1, 1, 1])
        self.assertEqual(sorted(recovery_source_reads), [2, 3],
                         'Only held results need proof hashing; do not rescan consumed parts')

    def test_normal_stop_still_consumes_each_completed_result_once(self):
        with self.services():
            state = self.run_fault(normal_stop=True)
        self.assertEqual(state['recognized'], 2)
        self.assertEqual(self.combined(), ['合成片段0', '合成片段1'])
        self.assertEqual([record['result_calls'] for record in self.futures], [1, 1])

    def test_completed_translation_is_recovered_without_starting_next_translation(self):
        self.config.translate = True
        with self.services():
            state = self.run_fault(fault_on_wait=2)
        translated = [record for record in self.futures if record['function'] == 'translation_task']
        self.assertEqual(len(translated), 1)
        index = translated[0]['index']
        self.assertEqual(len(self.calls['translation']), 1)
        self.assertEqual(sorted(self.calls['adapter']), [0, 1])
        self.assertEqual(state['recognized'], 2)
        self.assertEqual(state['translated'], 1)
        self.assertEqual(state['parts'][str(index)]['translation'], 'done')
        self.assertNotEqual(state['parts'][str(1 - index)].get('translation'), 'done')
        target = self.config.project / '中文草稿.srt'
        self.assertEqual([cue.text for cue in parse_srt(target.read_text(encoding='utf-8'))],
                         [f'译 合成片段{index}'])
        self.assertEqual([record['result_calls'] for record in self.futures], [1, 1, 1])

    def test_translation_for_changed_source_is_not_accepted_or_overwritten(self):
        self.config.translate = True
        changed = '1\n00:00:00,100 --> 00:00:00,900\n保留人工修改原文\n'
        affected = {}

        def change_source():
            record = next(item for item in self.futures if item['function'] == 'translation_task')
            affected['index'] = record['index']
            affected['path'] = self.config.project / '片段' / f"{record['index']+1:04d}" / 'source.local.srt'
            affected['path'].write_text(changed, encoding='utf-8')

        with self.services():
            state = self.run_fault(fault_on_wait=2, after_done=change_source)
        self.assertEqual(affected['path'].read_text(encoding='utf-8'), changed)
        self.assertNotEqual(state['parts'][str(affected['index'])].get('translation'), 'done')
        self.assertEqual(state['translated'], 0)
        self.assertEqual(len(self.calls['translation']), 1)
        self.assertEqual(sorted(self.calls['send']), [0, 1])
        self.assertFalse((self.config.project / '中文草稿.srt').exists())

    def test_public_source_or_target_edit_after_translation_future_is_preserved(self):
        self.config.translate = True
        self.config.target = 'en'
        target_name = r.target_filename(self.config.target)
        changed = '1\n00:00:00,100 --> 00:00:00,900\nPublic manual edit\n'.encode('utf-8')
        for index, name in enumerate(('原文.srt', target_name)):
            with self.subTest(name=name):
                self.config.project = self.root / f'public-translation-{index}'
                self.stop.clear()
                affected = {}
                start = {key: len(value) for key, value in self.calls.items()}
                future_start = len(self.futures)

                def change_public():
                    record = next(item for item in self.futures[future_start:] if item['function'] == 'translation_task')
                    affected['index'] = record['index']
                    affected['path'] = self.config.project / '片段' / f"{record['index']+1:04d}" / name
                    affected['sent'] = len(self.calls['send'])
                    affected['adapted'] = len(self.calls['adapter'])
                    affected['path'].write_bytes(changed)

                with self.services():
                    state = self.run_fault(fault_on_wait=2, after_done=change_public)
                self.assertEqual(affected['path'].read_bytes(), changed)
                self.assertNotEqual(state['parts'][str(affected['index'])].get('translation'), 'done')
                self.assertEqual(state['translated'], 0)
                self.assertFalse((self.config.project / target_name).exists())
                self.assertEqual(len(self.calls['translation']) - start['translation'], 1)
                self.assertEqual(len(self.calls['send']), affected['sent'])
                self.assertEqual(len(self.calls['adapter']), affected['adapted'])

    def test_translation_verification_io_failure_does_not_replace_scheduler_error(self):
        self.config.translate = True
        original_open = Path.open
        affected = {}
        failed_reads = []
        secondary = PermissionError('owned target proof read denied')

        def deny_target_read_after_done():
            record = next(item for item in self.futures if item['function'] == 'translation_task')
            affected['index'] = record['index']
            affected['path'] = self.config.project / '片段' / f"{record['index']+1:04d}" / 'target.local.srt'
            self.assertTrue(affected['path'].is_file())

        def opened(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if path == affected.get('path') and mode == 'rb':
                failed_reads.append(path)
                raise secondary
            return original_open(path, *args, **kwargs)

        with self.services(), patch.object(Path, 'open', new=opened):
            state = self.run_fault(fault_on_wait=2, after_done=deny_target_read_after_done)
        self.assertNotEqual(state['parts'][str(affected['index'])].get('translation'), 'done')
        self.assertEqual(state['translated'], 0)
        self.assertEqual(failed_reads, [affected['path']])
        self.assertEqual(len(self.calls['translation']), 1)
        self.assertFalse((self.config.project / '中文草稿.srt').exists())

    def test_later_fault_preserves_manual_merged_source_backup_and_automatic_output(self):
        self.config.chunk_seconds = 5
        public = self.config.project / '原文.srt'
        manual = ('1\n00:00:00,100 --> 00:00:00,900\n人工源文0\n\n'
                  '2\n00:00:05,100 --> 00:00:05,900\n人工源文1\n').encode('utf-8')
        with self.services():
            state = self.run_fault(fault_on_wait=2, after_done=lambda: public.write_bytes(manual))
        self.assertEqual(public.read_bytes(), manual)
        backups = list((self.config.project / '用户修改备份').glob('*'))
        self.assertTrue(any(path.is_file() and path.read_bytes() == manual for path in backups))
        self.assertIn('原文.srt', state['manual_outputs'])
        self.assertEqual(Path(state['outputs']['原文.srt']), Path('自动更新/原文.srt'))
        automatic = self.config.project / '自动更新' / '原文.srt'
        self.assertEqual([cue.text for cue in parse_srt(automatic.read_text(encoding='utf-8'))],
                         ['合成片段0', '合成片段1', '合成片段2', '合成片段3'])
        self.assertEqual(state['recognized'], 4)

    def test_fault_without_stop_never_publishes_complete_after_accepting_all_results(self):
        with self.services():
            state = self.run_fault(set_stop=False)
        self.assertFalse(self.stop.is_set())
        self.assertNotIn('complete', [status for status, _, _ in self.progress])
        self.assertEqual(state['recognized'], 2)
        self.assertEqual(self.combined(), ['合成片段0', '合成片段1'])
        self.assertEqual(sorted(self.calls['send']), [0, 1])

    def test_fault_without_stop_hashes_only_owned_results_and_clears_diagnostic_on_resume(self):
        self.config.chunk_seconds = 5
        recovery_started = False
        proof_reads = []
        original_open = Path.open

        def begin_recovery():
            nonlocal recovery_started
            recovery_started = True

        def opened(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get('mode', 'r')
            if recovery_started and path.name == 'source.local.srt' and mode == 'rb':
                proof_reads.append(int(path.parent.name) - 1)
            return original_open(path, *args, **kwargs)

        with self.services(), patch.object(Path, 'open', new=opened):
            state = self.run_fault(fault_on_wait=2, after_done=begin_recovery, set_stop=False)
            self.assertEqual(sorted(proof_reads), [2, 3])
            self.assertEqual(state['scheduler_error']['type'], 'RuntimeError')
            self.assertEqual(state['scheduler_error']['message'], str(self.primary))
            self.assertEqual(state['scheduler_error']['recovered_asr'], 2)
            self.assertIn('调度', state['message'])
            self.assertNotIn('处理完成', state['message'])
            recovery_started = False
            counts = {key: len(value) for key, value in self.calls.items()}
            resumed = r.run_pipeline(self.config, self.stop)
        self.assertEqual(resumed['status'], 'complete')
        self.assertNotIn('scheduler_error', resumed)
        self.assertEqual({key: len(value) for key, value in self.calls.items()}, counts)

    def test_native_interrupt_does_not_accept_unconsumed_successful_results(self):
        interruption = KeyboardInterrupt('owned direct interrupt')
        native_wait = r.futures.wait

        def wait(owned, *args, **kwargs):
            _, pending = native_wait(owned, timeout=5, return_when=ALL_COMPLETED)
            self.assertFalse(pending)
            self.stop.set()
            raise interruption

        with self.services(), patch.object(r.futures, 'wait', side_effect=wait):
            with self.assertRaises(KeyboardInterrupt) as caught:
                r.run_pipeline(self.config, self.stop)
        self.assertIs(caught.exception, interruption)
        state = json.loads((self.config.project / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['recognized'], 0)
        self.assertNotIn('scheduler_error', state)
        self.assertEqual([record['result_calls'] for record in self.futures], [0, 0])
        self.assertTrue(all(record['future'].done() for record in self.futures))
        self.assertFalse((self.config.project / 'run.lock').exists())

    def test_failed_result_secondary_note_keeps_original_scheduler_exception(self):
        worker_error = OSError('owned completed future raised')

        def transform(index, value):
            if index == 1:
                raise worker_error
            return value

        with self.services(transform=transform):
            self.run_fault()
        notes = '\n'.join(getattr(self.primary, '__notes__', []))
        self.assertIn(str(worker_error), notes)
        self.assertIn('2', notes)
        self.assertEqual([record['result_calls'] for record in self.futures], [1, 1])

    def test_stored_worker_interrupt_is_not_a_new_interrupt_in_scheduler_thread(self):
        for index, stored_error in enumerate((KeyboardInterrupt('worker interrupted'),
                                              SystemExit('worker exited'))):
            with self.subTest(error=type(stored_error).__name__):
                self.config.project = self.root / f'worker-interrupt-{index}'
                self.stop.clear()

                def transform(chunk_index, value):
                    if chunk_index == 1:
                        raise stored_error
                    return value

                native_wait = r.futures.wait

                def wait(owned, *args, **kwargs):
                    _, pending = native_wait(owned, timeout=5, return_when=ALL_COMPLETED)
                    self.assertFalse(pending)
                    self.stop.set()
                    raise self.primary

                start = len(self.futures)
                with self.services(transform=transform), patch.object(r.futures, 'wait', side_effect=wait):
                    with self.assertRaises(BaseException) as caught:
                        r.run_pipeline(self.config, self.stop)
                self.assertIs(caught.exception, self.primary)
                state = json.loads((self.config.project / 'state.json').read_text(encoding='utf-8'))
                self.assert_only_first_accepted(state)
                self.assertIn(str(stored_error), '\n'.join(self.primary.__notes__))
                self.assertEqual([item['result_calls'] for item in self.futures[start:]], [1, 0])

    def test_new_interrupt_while_observing_future_keeps_priority(self):
        interruption = KeyboardInterrupt('new recovery interruption')
        native_wait = r.futures.wait

        def interrupted():
            raise interruption

        def wait(owned, *args, **kwargs):
            _, pending = native_wait(owned, timeout=5, return_when=ALL_COMPLETED)
            self.assertFalse(pending)
            self.futures[0]['future'].exception = interrupted
            self.stop.set()
            raise self.primary

        with self.services(), patch.object(r.futures, 'wait', side_effect=wait):
            with self.assertRaises(BaseException) as caught:
                r.run_pipeline(self.config, self.stop)
        self.assertIs(caught.exception, interruption)
        self.assertEqual([record['result_calls'] for record in self.futures], [0, 0])
        self.assertFalse((self.config.project / 'run.lock').exists())


if __name__ == '__main__':
    unittest.main()
