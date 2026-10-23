"""Durable progress is retained when a verified batch needs individual repairs."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from subtitle_pipeline import runner, translate as tr
from subtitle_pipeline.subtitles import Cue


TEXTS = ['First sentence.', 'Second sentence.', 'Third sentence.']
RESULTS = ['第一句。', '第二句。', '第三句。']
BATCH = '[[S0000]]…[[E0000]][[S0001]]第二句。[[E0001]][[S0002]]…[[E0002]]'


def response(text):
    return [[[text, '', None, None]], None, 'en']


def query(url):
    return parse_qs(urlparse(url).query)['q'][0]


class TranslationPartialCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='translation-partial-cache-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / 'cache.json'
        guard = patch.object(tr, '_wait', return_value=None)
        guard.start(); self.addCleanup(guard.stop)
        guard = patch('socket.socket.connect', side_effect=AssertionError('No external requests'))
        guard.start(); self.addCleanup(guard.stop)

    def entries(self, path=None):
        path = self.cache if path is None else path
        if not path.exists():
            return {}
        return {entry['text']: entry['translation'] for entry in
                json.loads(path.read_text(encoding='utf-8'))['entries'].values()}

    def fail_last(self, failure='timeout', *, queries=None):
        def request(url, stop_event=None):
            text = query(url)
            if queries is not None:
                queries.append(text)
            if text.startswith('[[S0000]]'):
                return response(BATCH)
            if text == TEXTS[0]:
                return response(RESULTS[0])
            self.assertEqual(text, TEXTS[2])
            if failure == 'timeout':
                raise TimeoutError('Offline injected repair failure')
            return response('…')
        return request

    def test_successes_survive_later_repair_failure_and_resume_sends_only_missing_cue(self):
        for failure in ('timeout', 'invalid'):
            with self.subTest(failure=failure):
                cache = self.root / (failure + '.json')
                requests = []
                with patch.object(tr, '_request_json', side_effect=self.fail_last(failure, queries=requests)):
                    with self.assertRaises(tr.TranslationError):
                        tr.translate_texts(TEXTS + [TEXTS[1], ''], 'en', 'zh-CN', cache)
                self.assertEqual(self.entries(cache), dict(zip(TEXTS[:2], RESULTS[:2])))
                self.assertEqual(len(requests), 6 if failure == 'timeout' else 3)
                resumed = []
                def request(url, stop_event=None):
                    resumed.append(query(url))
                    self.assertEqual(query(url), TEXTS[2])
                    return response(RESULTS[2])
                with patch.object(tr, '_request_json', side_effect=request):
                    actual = tr.translate_texts(TEXTS + [TEXTS[1], ''], 'en', 'zh-CN', cache)
                self.assertEqual(actual, RESULTS + [RESULTS[1], ''])
                self.assertEqual(resumed, [TEXTS[2]])

    def test_valid_peer_is_durable_before_first_repair_and_cancelled_response_is_not_saved(self):
        stop = threading.Event()
        def request(url, stop_event=None):
            if query(url).startswith('[[S0000]]'):
                return response('[[S0000]]…[[E0000]][[S0001]]第二句。[[E0001]]')
            self.assertEqual(self.entries(), {TEXTS[1]: RESULTS[1]})
            stop.set()
            return response(RESULTS[0])
        with patch.object(tr, '_request_json', side_effect=request):
            with self.assertRaises(tr.TranslationCancelled):
                tr.translate_texts(TEXTS[:2], 'en', 'zh-CN', self.cache, stop)
        self.assertEqual(self.entries(), {TEXTS[1]: RESULTS[1]})

    def test_ambiguous_batch_does_not_salvage_apparently_valid_peer(self):
        def request(url, stop_event=None):
            if query(url).startswith('[[S0000]]'):
                return response('[[S0000]]第一句。[[E0000]][[S0000]]第二句。[[E0000]]')
            self.assertFalse(self.cache.exists())
            raise TimeoutError('First individual fallback failed')
        with patch.object(tr, '_request_json', side_effect=request):
            with self.assertRaises(tr.TranslationError):
                tr.translate_texts(TEXTS[:2], 'en', 'zh-CN', self.cache)
        self.assertFalse(self.cache.exists())

    def test_all_valid_batch_has_one_request_and_one_atomic_save(self):
        body = ''.join(f'[[S{i:04d}]]{text}[[E{i:04d}]]' for i, text in enumerate(RESULTS))
        with patch.object(tr, '_request_json', return_value=response(body)) as request, \
                patch.object(tr, '_save_cache', wraps=tr._save_cache) as save:
            self.assertEqual(tr.translate_texts(TEXTS, 'en', 'zh-CN', self.cache), RESULTS)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(save.call_count, 1)

    def test_cache_write_failure_preserves_old_file_and_does_not_start_repair(self):
        with patch.object(tr, '_request_json', return_value=response('旧译文。')):
            tr.translate_texts(['Existing.'], 'en', 'zh-CN', self.cache)
        before = self.cache.read_bytes()
        with patch.object(tr, '_request_json', side_effect=self.fail_last()) as request, \
                patch.object(tr, 'replace_with_retry', side_effect=OSError('Offline disk failure')):
            with self.assertRaisesRegex(tr.TranslationError, '缓存写入失败'):
                tr.translate_texts(TEXTS, 'en', 'zh-CN', self.cache)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_runner_resume_preserves_asr_and_completes_from_partial_translation_cache(self):
        source = self.root / 'source.wav'
        source.write_bytes(b'Offline synthetic media identity')
        project = self.root / 'project'
        config = runner.PipelineConfig(source, project, language='en', target='zh-CN',
            translate=True, chunk_seconds=10, overlap_seconds=1)
        cues = [Cue(100, 900, TEXTS[0]), Cue(1200, 2000, TEXTS[1]), Cue(2200, 3000, TEXTS[2])]
        with patch.object(runner, 'probe_media', return_value=4000), \
                patch.object(runner, 'detect_silences', return_value=[]), \
                patch.object(runner, 'recognize_chunk', return_value=cues) as recognize, \
                patch.object(tr, '_request_json', side_effect=self.fail_last()):
            first = runner.run_pipeline(config)
        self.assertEqual(first['status'], 'translation_incomplete')
        self.assertEqual(recognize.call_count, 1)
        self.assertEqual(self.entries(project / 'translation-cache.json'), dict(zip(TEXTS[:2], RESULTS[:2])))
        local = project / '片段/0001/source.local.srt'
        before = local.read_bytes(), local.stat().st_mtime_ns
        requests = []
        def request(url, stop_event=None):
            requests.append(query(url))
            self.assertEqual(query(url), TEXTS[2])
            return response(RESULTS[2])
        with patch.object(runner, 'probe_media', return_value=4000), \
                patch.object(runner, 'detect_silences', return_value=[]), \
                patch.object(runner, 'recognize_chunk', side_effect=AssertionError('ASR repeated')), \
                patch.object(tr, '_request_json', side_effect=request):
            last = runner.run_pipeline(config)
        self.assertEqual(last['status'], 'complete')
        self.assertEqual(requests, [TEXTS[2]])
        self.assertEqual((local.read_bytes(), local.stat().st_mtime_ns), before)
        target = (local.parent / 'target.local.srt').read_text(encoding='utf-8')
        self.assertTrue(all(text in target for text in RESULTS))


if __name__ == '__main__':
    unittest.main()
