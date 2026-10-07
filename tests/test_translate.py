import importlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


class TranslationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('subtitle_pipeline.translate')
        except ModuleNotFoundError:
            self.fail('subtitle translation is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'cache.json'
        self.wait = patch.object(self.module, '_wait', side_effect=self.fake_wait)
        self.wait.start()
        self.addCleanup(self.wait.stop)
        self.batch = patch.object(self.module, '_MAX_BATCH_CUES', 1)
        self.batch.start()
        self.addCleanup(self.batch.stop)

    @staticmethod
    def fake_wait(seconds, stop_event):
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError('cancelled')

    def test_mapping_preserves_multiline_cues_and_duplicate_positions(self):
        def respond(url):
            value = parse_qs(urlparse(url).query)['q'][0]
            return {'First\nline.': [[['第一行。', value, None, None]], None, 'en'],
                    'Second.': [[['第二行。', value, None, None]], None, 'en']}[value]
        with patch.object(self.module, '_request_json', side_effect=respond) as request:
            result = self.module.translate_texts(['First\nline.', 'Second.', 'First\nline.'], 'en', 'zh-CN', self.cache)
        self.assertEqual(result, ['第一行。', '第二行。', '第一行。'])
        self.assertEqual(request.call_count, 2)

    def test_success_is_durable_across_calls_and_language_pairs(self):
        with patch.object(self.module, '_request_json', return_value=[[["你好。", "Hello.", None, None]], None, 'en']):
            self.module.translate_texts(['Hello.'], 'en', 'zh-CN', self.cache)
        with patch.object(self.module, '_request_json', side_effect=AssertionError('cache miss')):
            result = self.module.translate_texts(['Hello.'], 'en', 'zh-CN', self.cache)
        self.assertEqual(result, ['你好。'])
        with patch.object(self.module, '_request_json', return_value=[[["こんにちは。", "Hello.", None, None]], None, 'en']):
            result = self.module.translate_texts(['Hello.'], 'en', 'ja', self.cache)
        self.assertEqual(result, ['こんにちは。'])
        payload = json.loads(self.cache.read_text(encoding='utf-8'))
        self.assertEqual(len(payload['entries']), 2)
        self.assertTrue(all(len(key) == 64 for key in payload['entries']))
        self.assertEqual(list(self.cache.parent.glob('*.tmp')), [])

    def test_http_403_switches_to_fallback_for_remaining_cues(self):
        bodies = []
        errors = []
        def respond(url):
            host = urlparse(url).hostname
            if host == 'translate.googleapis.com':
                body = io.BytesIO(b'Access denied')
                bodies.append(body)
                error = HTTPError(url, 403, 'Forbidden', {}, body)
                errors.append(error)
                self.addCleanup(error.close)
                raise error
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query['client'], ['dict-chrome-ex'])
            return [{'Hello.': '你好。', 'Bye.': '再见。'}[query['q'][0]]]
        with patch.object(self.module, '_request_json', side_effect=respond) as request:
            result = self.module.translate_texts(['Hello.', 'Bye.'], 'en', 'zh-CN', self.cache)
        self.assertEqual(result, ['你好。', '再见。'])
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(body.closed for body in bodies))

    def test_auto_detect_fallback_response_is_parsed_without_language_suffix(self):
        responses = [HTTPError('https://translate.googleapis.com', 429, 'Limited', {}, None), [['你好。', 'en']]]
        with patch.object(self.module, '_request_json', side_effect=responses):
            result = self.module.translate_texts(['Hello.'], 'auto', 'zh-CN', self.cache)
        self.assertEqual(result, ['你好。'])

    def test_bad_response_never_returns_source_or_caches_success(self):
        for payload in [None, {}, [], [[[]]], [[[None, 'source']]], [['not-a-segment']], [[["   ", 'source']]]]:
            with self.subTest(payload=payload):
                with patch.object(self.module, '_request_json', return_value=payload):
                    with self.assertRaises(self.module.TranslationError):
                        self.module.translate_texts(['source'], 'en', 'zh-CN', self.cache)
                self.assertFalse(self.cache.exists())

    def test_timeout_retries_are_bounded_and_partial_progress_is_saved(self):
        def respond(url):
            if parse_qs(urlparse(url).query)['q'] == ['first']:
                return [[['第一', 'first', None, None]], None, 'en']
            raise TimeoutError('timed out')
        with patch.object(self.module, '_request_json', side_effect=respond) as request:
            with self.assertRaises(self.module.TranslationError):
                self.module.translate_texts(['first', 'second'], 'en', 'zh-CN', self.cache)
        self.assertEqual(request.call_count, 5)
        with patch.object(self.module, '_request_json', side_effect=AssertionError('must be cached')):
            self.assertEqual(self.module.translate_texts(['first'], 'en', 'zh-CN', self.cache), ['第一'])

    def test_cancelled_before_start_does_not_make_request(self):
        event = threading.Event()
        event.set()
        with patch.object(self.module, '_request_json', side_effect=AssertionError('must not request')):
            with self.assertRaises(self.module.TranslationCancelled):
                self.module.translate_texts(['first'], 'en', 'zh-CN', self.cache, event)

    def test_cancel_during_request_stops_without_saving_incomplete_output(self):
        event = threading.Event()
        def respond(url):
            event.set()
            return [[['第一', 'first', None, None]], None, 'en']
        with patch.object(self.module, '_request_json', side_effect=respond):
            with self.assertRaises(self.module.TranslationCancelled):
                self.module.translate_texts(['first', 'second'], 'en', 'zh-CN', self.cache, event)
        self.assertFalse(self.cache.exists())

    def test_long_cue_fails_before_network_instead_of_truncating(self):
        with patch.object(self.module, '_request_json', side_effect=AssertionError('must not request')):
            with self.assertRaises(self.module.TranslationError):
                self.module.translate_texts(['x' * 1501], 'en', 'zh-CN', self.cache)

    def test_empty_input_and_empty_cue_preserve_positions_without_requests(self):
        with patch.object(self.module, '_request_json', side_effect=AssertionError('must not request')):
            self.assertEqual(self.module.translate_texts([], 'en', 'zh-CN', self.cache), [])
            self.assertEqual(self.module.translate_texts(['', '  '], 'en', 'zh-CN', self.cache), ['', ''])

    def test_corrupt_cache_is_reported_without_overwriting_it(self):
        self.cache.write_text('{truncated', encoding='utf-8')
        with patch.object(self.module, '_request_json', side_effect=AssertionError('must not request')):
            with self.assertRaises(self.module.TranslationError):
                self.module.translate_texts(['source'], 'en', 'zh-CN', self.cache)
        self.assertEqual(self.cache.read_text(encoding='utf-8'), '{truncated')

    def test_rate_limit_spaces_request_starts_at_least_half_second(self):
        self.wait.stop()
        clock = [100.0]
        starts = []
        def wait(seconds, event):
            clock[0] += seconds
        def respond(url):
            starts.append(clock[0])
            return [[['译文', 'source', None, None]], None, 'en']
        with patch.object(self.module.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.module, '_last_request_at', 0.0), \
             patch.object(self.module, '_wait', side_effect=wait), \
             patch.object(self.module, '_request_json', side_effect=respond):
            self.module.translate_texts(['one', 'two', 'three'], 'en', 'zh-CN', self.cache)
        self.assertEqual(starts, [100.0, 100.5, 101.0])

    def test_batch_ids_restore_order_and_repeated_cues_are_not_sent_twice(self):
        def respond(url):
            value = parse_qs(urlparse(url).query)['q'][0]
            self.assertEqual(value.count('Hello.'), 1)
            return [[['[[S0001]]再见。[[E0001]]\n[[S0000]]你好。[[E0000]]', value, None, None]], None, 'en']
        with patch.object(self.module, '_MAX_BATCH_CUES', 16), \
             patch.object(self.module, '_request_json', side_effect=respond) as request:
            result = self.module.translate_texts(['Hello.', 'Bye.', 'Hello.'], 'en', 'zh-CN', self.cache)
        self.assertEqual(result, ['你好。', '再见。', '你好。'])
        self.assertEqual(request.call_count, 1)

    def test_missing_duplicate_or_extra_batch_markers_fall_back_without_wrong_mapping(self):
        bad_batches = ['[[S0000]]你好。[[E0000]]',
                       '[[S0000]]你好。[[E0000]][[S0000]]再见。[[E0000]]',
                       '外部说明[[S0000]]你好。[[E0000]][[S0001]]再见。[[E0001]]']
        for batch_text in bad_batches:
            with self.subTest(batch_text=batch_text):
                cache = self.cache.parent / (str(len(list(self.cache.parent.iterdir()))) + '.json')
                def respond(url):
                    value = parse_qs(urlparse(url).query)['q'][0]
                    result = batch_text if '[[S0000]]' in value else {'Hello.': '你好。', 'Bye.': '再见。'}[value]
                    return [[[result, value, None, None]], None, 'en']
                with patch.object(self.module, '_MAX_BATCH_CUES', 16), \
                     patch.object(self.module, '_request_json', side_effect=respond) as request:
                    result = self.module.translate_texts(['Hello.', 'Bye.'], 'en', 'zh-CN', cache)
                self.assertEqual(result, ['你好。', '再见。'])
                self.assertEqual(request.call_count, 3)

    def test_batches_obey_length_and_count_bounds(self):
        queries = []
        def respond(url):
            value = parse_qs(urlparse(url).query)['q'][0]
            queries.append(value)
            return [[[value, value, None, None]], None, 'en']
        texts = [f'Caption {i}: ' + 'x' * 80 for i in range(40)]
        with patch.object(self.module, '_MAX_BATCH_CUES', 16), \
             patch.object(self.module, '_request_json', side_effect=respond):
            self.assertEqual(self.module.translate_texts(texts, 'en', 'zh-CN', self.cache), texts)
        self.assertTrue(all(len(query) <= 1200 for query in queries))
        self.assertTrue(all(query.count('[[S') <= 16 for query in queries))
        self.assertGreater(len(queries), 1)

    def test_batch_with_copied_source_sentence_retries_affected_cue(self):
        responses = [
            [[['[[S0000]]你好。 This is a subtitle test.[[E0000]]\n[[S0001]]今天我们一起学习。[[E0001]]', '', None, None]], None, 'en'],
            [[['你好。这是一个字幕测试。', 'Hello. This is a subtitle test.', None, None]], None, 'en'],
        ]
        with patch.object(self.module, '_MAX_BATCH_CUES', 16), \
             patch.object(self.module, '_request_json', side_effect=responses) as request:
            result = self.module.translate_texts(['Hello. This is a subtitle test.', 'Today we are learning together.'], 'en', 'zh-CN', self.cache)
        self.assertEqual(result, ['你好。这是一个字幕测试。', '今天我们一起学习。'])
        self.assertEqual(request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
