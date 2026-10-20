"""Offline recovery tests for omitted content from the free translator."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from subtitle_pipeline import runner as r, translate as tr
from subtitle_pipeline.subtitles import Cue


class FreeTranslationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def config(self, label):
        source = self.root / (label + '.wav')
        source.write_bytes(b'offline audio fixture')
        return r.PipelineConfig(source, self.root / label, language='en',
                                chunk_seconds=10, overlap_seconds=1)

    @contextmanager
    def services(self, source_text='Hello world.'):
        with patch.multiple(r, probe_media=lambda *a, **k: 1000,
                            detect_silences=lambda *a: [],
                            recognize_chunk=lambda *a, **k: [Cue(100, 900, source_text)]), \
             patch.object(tr, '_wait', return_value=None):
            yield

    @staticmethod
    def response(text):
        return [[[text, 'Hello world.', None, None]], None, 'en']

    def legacy_bad_result(self, config, *, human=False):
        """Recreate the content-bound records left by older runner versions."""
        with self.services(), patch.object(tr, '_request_json', return_value=self.response('你好世界。')):
            state = r.run_pipeline(config)
        self.assertEqual(state['status'], 'complete')
        folder = config.project / '片段' / '0001'
        for path in (folder / 'target.local.srt', folder / '中文草稿.srt',
                     config.project / '中文草稿.srt', config.project / '双语草稿.srt'):
            path.write_text(path.read_text(encoding='utf-8').replace('你好世界。', '…'), encoding='utf-8')
            relative = str(path.relative_to(config.project))
            if relative in state['generated_hashes']:
                state['generated_hashes'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        part = state['parts']['0']
        part['target_hash'] = hashlib.sha256((folder / 'target.local.srt').read_bytes()).hexdigest()
        part['public_target_hash'] = hashlib.sha256((folder / '中文草稿.srt').read_bytes()).hexdigest()
        if human:
            part['human_translation_edited'] = True
        state['status'] = 'translation_incomplete'
        (config.project / 'state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        cache_path = config.project / 'translation-cache.json'
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        for entry in cache['entries'].values():
            entry['translation'] = '…'
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        return folder

    def test_runner_retries_omitted_content_without_caching_or_completing_it(self):
        for bad_result in ('…', '。'):
            with self.subTest(result=bad_result):
                config = self.config('new-' + str(ord(bad_result)))
                with self.services(), patch.object(tr, '_request_json', return_value=self.response(bad_result)):
                    failed = r.run_pipeline(config)
                self.assertEqual(failed['status'], 'translation_incomplete')
                self.assertEqual(failed['translated'], 0)
                self.assertNotEqual(failed['parts']['0'].get('translation'), 'done')
                self.assertFalse((config.project / 'translation-cache.json').exists())
                self.assertFalse((config.project / '片段' / '0001' / 'target.local.srt').exists())
                with self.services(), \
                     patch.object(r, 'recognize_chunk', side_effect=AssertionError('ASR repeated')), \
                     patch.object(tr, '_request_json', return_value=self.response('你好世界。')):
                    recovered = r.run_pipeline(config)
                self.assertEqual(recovered['status'], 'complete')
                self.assertEqual(recovered['translated'], 1)
                self.assertIn('你好世界。', (config.project / '中文草稿.srt').read_text(encoding='utf-8'))

    def test_runner_recovers_unchanged_legacy_bad_part_and_cache(self):
        config = self.config('legacy-machine')
        folder = self.legacy_bad_result(config)
        previous = (folder / 'target.local.srt').read_bytes()
        with self.services(), \
             patch.object(r, 'recognize_chunk', side_effect=AssertionError('ASR repeated')), \
             patch.object(tr, '_request_json', return_value=self.response('你好世界。')) as request:
            recovered = r.run_pipeline(config)
        self.assertEqual(recovered['status'], 'complete')
        self.assertEqual(recovered['translated'], 1)
        self.assertEqual(request.call_count, 1)
        self.assertIn('你好世界。', (folder / 'target.local.srt').read_text(encoding='utf-8'))
        self.assertTrue(any(path.read_bytes() == previous
                            for path in (config.project / '用户修改备份').glob('*.srt')))
        cache = json.loads((config.project / 'translation-cache.json').read_text(encoding='utf-8'))
        self.assertEqual([entry['translation'] for entry in cache['entries'].values()], ['你好世界。'])

    def test_failed_retry_keeps_legacy_cache_bytes(self):
        config = self.config('legacy-cache')
        self.legacy_bad_result(config)
        cache_path = config.project / 'translation-cache.json'
        before = cache_path.read_bytes()
        with patch.object(tr, '_wait', return_value=None), \
             patch.object(tr, '_request_json', return_value=self.response('。')):
            with self.assertRaises(tr.TranslationError):
                tr.translate_texts(['Hello world.'], 'en', 'zh-CN', cache_path)
        self.assertEqual(cache_path.read_bytes(), before)

    @staticmethod
    def translation_copy_bytes(folder):
        return tuple(path.read_bytes() if path.exists() else None
                     for path in (folder / 'target.local.srt', folder / '中文草稿.srt'))

    def test_edits_during_legacy_recovery_preserve_both_copies_and_block_retry(self):
        for index, side in enumerate(('target.local.srt', '中文草稿.srt')):
            with self.subTest(side=side):
                config = self.config('inflight-edit-' + str(index))
                folder = self.legacy_bad_result(config)
                expected = []

                def respond(url, stop_event=None):
                    path = folder / side
                    path.write_text(path.read_text(encoding='utf-8').replace('…', '。'), encoding='utf-8')
                    expected.append(self.translation_copy_bytes(folder))
                    return self.response('你好世界。')

                with self.services(), patch.object(tr, '_request_json', side_effect=respond):
                    state = r.run_pipeline(config)
                self.assertEqual(state['status'], 'translation_incomplete')
                self.assertEqual(state['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])
                cache = json.loads((config.project / 'translation-cache.json').read_text(encoding='utf-8'))
                self.assertEqual([entry['translation'] for entry in cache['entries'].values()], ['你好世界。'])
                with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('conflict retransmitted')):
                    resumed = r.run_pipeline(config)
                self.assertEqual(resumed['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])

    def test_deletion_during_legacy_recovery_is_preserved_and_blocks_retry(self):
        for index, side in enumerate(('target.local.srt', '中文草稿.srt')):
            with self.subTest(side=side):
                config = self.config('inflight-delete-' + str(index))
                folder = self.legacy_bad_result(config)
                expected = []

                def respond(url, stop_event=None):
                    (folder / side).unlink()
                    expected.append(self.translation_copy_bytes(folder))
                    return self.response('你好世界。')

                with self.services(), patch.object(tr, '_request_json', side_effect=respond):
                    state = r.run_pipeline(config)
                self.assertEqual(state['status'], 'translation_incomplete')
                self.assertEqual(state['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])
                with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('deletion retransmitted')):
                    resumed = r.run_pipeline(config)
                self.assertEqual(resumed['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])

    def test_edit_after_recovery_validation_before_worker_start_prevents_request(self):
        for index, side in enumerate(('target.local.srt', '中文草稿.srt')):
            with self.subTest(side=side):
                config = self.config('before-worker-' + str(index))
                folder = self.legacy_bad_result(config)
                expected = []
                submit = r.futures.ThreadPoolExecutor.submit

                def edit_before_submit(pool, operation, *args, **kwargs):
                    if operation.__name__ == 'translation_task':
                        path = folder / side
                        path.write_text(path.read_text(encoding='utf-8').replace('…', '。'), encoding='utf-8')
                        expected.append(self.translation_copy_bytes(folder))
                    return submit(pool, operation, *args, **kwargs)

                with self.services(), \
                     patch.object(r.futures.ThreadPoolExecutor, 'submit', new=edit_before_submit), \
                     patch.object(tr, '_request_json', return_value=self.response('你好世界。')) as request:
                    state = r.run_pipeline(config)
                self.assertEqual(state['status'], 'translation_incomplete')
                self.assertEqual(state['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])
                self.assertEqual(request.call_count, 0)
                with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('conflict retransmitted')):
                    resumed = r.run_pipeline(config)
                self.assertEqual(resumed['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])

    def test_valid_manual_edit_during_recovery_can_be_adopted_on_resume(self):
        config = self.config('inflight-valid-edit')
        folder = self.legacy_bad_result(config)

        def respond(url, stop_event=None):
            local = folder / 'target.local.srt'
            local.write_text(local.read_text(encoding='utf-8').replace('…', '人工保留译文。'), encoding='utf-8')
            return self.response('你好世界。')

        with self.services(), patch.object(tr, '_request_json', side_effect=respond):
            state = r.run_pipeline(config)
        self.assertEqual(state['parts']['0']['translation'], 'needs_review')
        self.assertIn('人工保留译文。', (folder / 'target.local.srt').read_text(encoding='utf-8'))
        self.assertIn('…', (folder / '中文草稿.srt').read_text(encoding='utf-8'))
        with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('manual edit retransmitted')):
            resumed = r.run_pipeline(config)
        self.assertEqual(resumed['status'], 'complete')
        self.assertTrue(resumed['parts']['0']['human_translation_edited'])
        for name in ('target.local.srt', '中文草稿.srt'):
            self.assertIn('人工保留译文。', (folder / name).read_text(encoding='utf-8'))

    def test_failed_legacy_recovery_revalidates_targets_on_the_next_run(self):
        for index, side in enumerate(('target.local.srt', '中文草稿.srt')):
            with self.subTest(side=side):
                config = self.config('between-runs-' + str(index))
                folder = self.legacy_bad_result(config)
                with self.services(), patch.object(tr, '_request_json', side_effect=TimeoutError('offline')):
                    failed = r.run_pipeline(config)
                self.assertEqual(failed['status'], 'translation_incomplete')
                self.assertNotEqual(failed['parts']['0'].get('translation'), 'done')
                edited = folder / side
                edited.write_text(edited.read_text(encoding='utf-8').replace('…', '。'), encoding='utf-8')
                expected = self.translation_copy_bytes(folder)
                with self.services(), patch.object(tr, '_request_json', return_value=self.response('你好世界。')) as request:
                    resumed = r.run_pipeline(config)
                self.assertEqual(resumed['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected)
                self.assertEqual(request.call_count, 0)

    def test_failed_legacy_recovery_can_retry_unchanged_machine_files(self):
        config = self.config('retry-unchanged')
        folder = self.legacy_bad_result(config)
        before = self.translation_copy_bytes(folder)
        with self.services(), patch.object(tr, '_request_json', side_effect=TimeoutError('offline')):
            failed = r.run_pipeline(config)
        self.assertEqual(failed['status'], 'translation_incomplete')
        self.assertEqual(self.translation_copy_bytes(folder), before)
        with self.services(), patch.object(tr, '_request_json', return_value=self.response('你好世界。')) as request:
            resumed = r.run_pipeline(config)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(request.call_count, 1)
        self.assertIn('你好世界。', (folder / 'target.local.srt').read_text(encoding='utf-8'))

    def test_target_edit_does_not_replace_provider_process_interrupt(self):
        for index, interrupt in enumerate((KeyboardInterrupt, SystemExit)):
            with self.subTest(interrupt=interrupt.__name__):
                config = self.config('interrupted-' + str(index))
                folder = self.legacy_bad_result(config)
                expected = []

                def respond(url, stop_event=None):
                    local = folder / 'target.local.srt'
                    local.write_text(local.read_text(encoding='utf-8').replace('…', '。'), encoding='utf-8')
                    expected.append(self.translation_copy_bytes(folder))
                    raise interrupt('provider interrupted')

                with self.services(), patch.object(tr, '_request_json', side_effect=respond):
                    with self.assertRaises(interrupt):
                        r.run_pipeline(config)
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])
                with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('interrupted edit retransmitted')):
                    resumed = r.run_pipeline(config)
                self.assertEqual(resumed['parts']['0']['translation'], 'needs_review')
                self.assertEqual(self.translation_copy_bytes(folder), expected[0])

    def test_invalid_manual_edit_is_preserved_before_synchronizing_copies(self):
        for side in ('target.local.srt', '中文草稿.srt'):
            with self.subTest(side=side):
                config = self.config('manual-' + ('local' if side.startswith('target') else 'public'))
                with self.services(), patch.object(tr, '_request_json', return_value=self.response('你好世界。')):
                    r.run_pipeline(config)
                folder = config.project / '片段' / '0001'
                edited = folder / side
                edited.write_text(edited.read_text(encoding='utf-8').replace('你好世界。', '…'), encoding='utf-8')
                before = tuple((folder / name).read_bytes() for name in ('target.local.srt', '中文草稿.srt'))
                with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('manual edit retransmitted')):
                    for _ in range(2):
                        state = r.run_pipeline(config)
                        self.assertEqual(state['status'], 'translation_incomplete')
                        self.assertEqual(state['parts']['0']['translation'], 'needs_review')
                        self.assertEqual(tuple((folder / name).read_bytes()
                                               for name in ('target.local.srt', '中文草稿.srt')), before)

    def test_registered_legacy_human_translation_is_preserved_with_matching_hashes(self):
        config = self.config('legacy-human')
        folder = self.legacy_bad_result(config, human=True)
        before = tuple((folder / name).read_bytes() for name in ('target.local.srt', '中文草稿.srt'))
        with self.services(), patch.object(tr, '_request_json', side_effect=AssertionError('human translation retransmitted')):
            state = r.run_pipeline(config)
        self.assertEqual(state['status'], 'translation_incomplete')
        self.assertEqual(state['parts']['0']['translation'], 'needs_review')
        self.assertEqual(tuple((folder / name).read_bytes()
                               for name in ('target.local.srt', '中文草稿.srt')), before)

    def test_punctuation_and_music_sources_accept_symbol_only_translations(self):
        for index, (source, translated) in enumerate((('…', '。'), ('♪', '♫'))):
            with self.subTest(source=source):
                config = self.config('symbols-' + str(index))
                with self.services(source), patch.object(tr, '_request_json', return_value=self.response(translated)):
                    state = r.run_pipeline(config)
                self.assertEqual(state['status'], 'complete')
                self.assertIn(translated, (config.project / '中文草稿.srt').read_text(encoding='utf-8'))

    def test_omitted_batch_item_falls_back_to_its_own_cue(self):
        queries = []

        def respond(url, stop_event=None):
            query = parse_qs(urlparse(url).query)['q'][0]
            queries.append(query)
            if query.startswith('[[S0000]]'):
                return self.response('[[S0000]]…[[E0000]]\n[[S0001]]再见。[[E0001]]')
            self.assertEqual(query, 'Hello world.')
            return self.response('你好世界。')

        cache = self.root / 'batch-cache.json'
        with patch.object(tr, '_wait', return_value=None), patch.object(tr, '_request_json', side_effect=respond):
            result = tr.translate_texts(['Hello world.', 'Bye.'], 'en', 'zh-CN', cache)
        self.assertEqual(result, ['你好世界。', '再见。'])
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[1], 'Hello world.')


if __name__ == '__main__':
    unittest.main()
