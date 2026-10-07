import base64
import importlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import wave

from subtitle_pipeline.cloud_budget import BudgetExceeded, BudgetLedger, CloudCancelled, CloudRequestError, HttpResponse, SubmissionUnknown
from subtitle_pipeline.subtitles import Cue


def word(text, start, end, punctuation=''):
    return {'text': text, 'begin_time': start, 'end_time': end,
            'punctuation': punctuation, 'fixed': True}


def sentence(text='こんにちは。 またね。', words=None, start=100, end=1800):
    return {'text': text, 'begin_time': start, 'end_time': end,
            'sentence_id': 1, 'sentence_end': True, 'channel_id': 0,
            'words': [word('こんにちは', 100, 800, '。'), word(' またね', 1100, 1800, '。')]
                     if words is None else words}


def response():
    return {'request_id': 'qwen-response-test',
            'output': {'text': 'こんにちは。 またね。', 'sentence': sentence()},
            'usage': {'duration': 2, 'input_tokens': 80, 'output_tokens': 8, 'total_tokens': 88}}


class NoNetworkTests(unittest.TestCase):
    def setUp(self):
        network = patch('urllib.request.OpenerDirector.open',
                        side_effect=AssertionError('Real network is disabled in Qwen tests'))
        network.start()
        self.addCleanup(network.stop)
        try:
            self.qwen = importlib.import_module('subtitle_pipeline.qwen_asr')
        except ModuleNotFoundError:
            self.fail('Qwen synchronous ASR adapter is not implemented')


class QwenParsingTests(NoNetworkTests):
    def test_english_spaces_and_chinese_punctuation_survive_word_timing_split(self):
        fixtures=[('Hello world. Good morning.',
                   [word('Hello',100,350),word('world',400,700,'.'),
                    word('Good',900,1100),word('morning',1200,1800,'.')],
                   [Cue(100,700,'Hello world.'),Cue(900,1800,' Good morning.')]),
                  ('你好世界。早上好。',
                   [word('你好',100,350),word('世界',400,700,'。'),word('早上好',900,1800,'。')],
                   [Cue(100,700,'你好世界。'),Cue(900,1800,'早上好。')])]
        for text,words,expected in fixtures:
            with self.subTest(text=text):
                payload=response()
                payload['output']={'text':text,'sentence':sentence(text,words)}
                result=self.qwen.parse_response(payload,2000)
                self.assertEqual(result.cues,expected)
                self.assertEqual(''.join(cue.text for cue in result.cues),text)

    def test_draft_falls_back_to_sentence_timing_only_with_complete_word_text(self):
        payload=response()
        payload['output']['sentence']['words'][-1]['end_time']=900
        before=json.dumps(payload,sort_keys=True)
        with self.assertRaises(ValueError):
            self.qwen.parse_response(payload,2000)
        result=self.qwen.parse_response(payload,2000,allow_draft_timing=True)
        self.assertEqual(result.cues,[Cue(100,1800,'こんにちは。 またね。')])
        self.assertTrue(result.metadata['draft_timing_requires_review'])
        self.assertTrue(any(i['reason']=='invalid_word_timing_sentence_fallback' for i in result.issues))
        self.assertEqual(before,json.dumps(payload,sort_keys=True))
        payload['output']['sentence']['words'].pop(0)
        with self.assertRaises(ValueError):
            self.qwen.parse_response(payload,2000,allow_draft_timing=True)

    def test_draft_fallback_does_not_accept_invalid_sentence_or_unfixed_words(self):
        for field,value in [('end_time',2100),('begin_time',1800)]:
            payload=response();payload['output']['sentence'][field]=value
            with self.assertRaises(ValueError):
                self.qwen.parse_response(payload,2000,allow_draft_timing=True)
        payload=response();payload['output']['sentence']['words'][0]['fixed']=False
        with self.assertRaises(ValueError):
            self.qwen.parse_response(payload,2000,allow_draft_timing=True)

    def test_full_text_and_word_timing_are_preserved_without_fabrication(self):
        result = self.qwen.parse_response(response(), 2000)
        self.assertEqual(result.cues, [Cue(100, 800, 'こんにちは。'), Cue(1100, 1800, ' またね。')])
        self.assertEqual(result.issues, [])
        self.assertEqual(result.metadata['input_duration_ms'], 2000)
        self.assertEqual(result.metadata['token_usage']['input_tokens'], 80)

    def test_isolated_leading_sentence_period_is_removed_only_with_exact_fixed_word_evidence(self):
        payload=response()
        payload['output']['sentence']['text']='。'+payload['output']['sentence']['text']
        original=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        result=self.qwen.parse_response(payload,2000)
        self.assertEqual(result.cues,[Cue(100,800,'こんにちは。'),Cue(1100,1800,' またね。')])
        self.assertEqual(result.issues,[{'reason':'leading_sentence_period_normalized',
            'phrase_index':0,'start_ms':100,'end_ms':1800}])
        self.assertEqual(json.dumps(payload,ensure_ascii=False,sort_keys=True),original)

    def test_leading_period_normalization_still_requires_every_complete_sentence(self):
        payload=response()
        first=sentence('。こんにちは。',[word('こんにちは',100,800,'。')],100,800)
        second=sentence(' またね。',[word(' またね',1100,1800,'。')],1100,1800)
        second['sentence_id']=2
        payload['output'].update(sentences=[first,second],sentence=second)
        result=self.qwen.parse_response(payload,2000)
        self.assertEqual(result.cues,[Cue(100,800,'こんにちは。'),Cue(1100,1800,' またね。')])
        self.assertEqual(result.issues,[{'reason':'leading_sentence_period_normalized',
            'phrase_index':0,'start_ms':100,'end_ms':800}])
        payload['output'].update(sentences=[first],sentence=first)
        with self.assertRaisesRegex(ValueError,'完整|覆盖'):
            self.qwen.parse_response(payload,2000)

    def test_leading_period_never_excuses_missing_words_timing_or_other_text(self):
        def no_timing(record):record['words'][0].pop('end_time')
        cases={
            'missing_words':lambda record:record.pop('words'),
            'empty_words':lambda record:record.update(words=[]),
            'missing_timing':no_timing,
            'unstable_word':lambda record:record['words'][0].update(fixed=False),
            'missing_real_word':lambda record:record['words'].pop(),
            'extra_real_character':lambda record:record.update(text='。違'+record['text'][1:]),
            'double_period':lambda record:record.update(text='。'+record['text']),
            'different_punctuation':lambda record:record.update(text='.'+record['text'][1:]),
        }
        for label,change in cases.items():
            payload=response()
            record=payload['output']['sentence']
            record['text']='。'+record['text']
            change(record)
            with self.subTest(case=label),self.assertRaises(ValueError):
                self.qwen.parse_response(payload,2000)

    def test_complete_multiple_sentences_are_supported_without_duplicating_current_sentence(self):
        payload = response()
        first = sentence('こんにちは。', [word('こんにちは', 100, 800, '。')], 100, 800)
        second = sentence(' またね。', [word(' またね', 1100, 1800, '。')], 1100, 1800)
        second['sentence_id'] = 2
        payload['output'].update(sentences=[first, second], sentence=second)
        self.assertEqual(self.qwen.parse_response(payload, 2000).cues,
                         [Cue(100, 800, 'こんにちは。'), Cue(1100, 1800, ' またね。')])

    def test_last_sentence_only_is_rejected_when_output_text_contains_earlier_speech(self):
        payload = response()
        payload['output']['sentence'] = sentence('またね。', [word('またね', 1100, 1800, '。')], 1100, 1800)
        with self.assertRaisesRegex(ValueError, '完整|覆盖'):
            self.qwen.parse_response(payload, 2000)

    def test_missing_or_mismatched_words_are_never_silently_dropped(self):
        for words in [[word('こんにちは', 100, 800, '。')],
                      [word('こんにちは', 100, 800, '。'), word('違います', 1100, 1800, '。')]]:
            payload = response()
            payload['output']['sentence']['words'] = words
            with self.subTest(words=words), self.assertRaises(ValueError):
                self.qwen.parse_response(payload, 2000)

    def test_unstable_or_unfinished_results_fail(self):
        for field, value in [('fixed', False), ('fixed', None), ('fixed', 1)]:
            payload = response()
            payload['output']['sentence']['words'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.qwen.parse_response(payload, 2000)
        payload = response()
        del payload['output']['sentence']['words'][0]['fixed']
        with self.assertRaises(ValueError):
            self.qwen.parse_response(payload, 2000)
        for final in [False, None, 1]:
            payload = response()
            payload['output']['sentence']['sentence_end'] = final
            with self.subTest(final=final), self.assertRaises(ValueError):
                self.qwen.parse_response(payload, 2000)

    def test_word_ranges_outside_audio_or_phrase_overlapping_or_missing_fail(self):
        for field, value in [('begin_time', -1), ('begin_time', True), ('begin_time', 900),
                             ('end_time', 2100), ('end_time', 100), ('end_time', None),
                             ('end_time', float('nan'))]:
            payload = response()
            payload['output']['sentence']['words'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.qwen.parse_response(payload, 2000)
        payload = response()
        payload['output']['sentence']['words'][1]['begin_time'] = 700
        with self.assertRaises(ValueError):
            self.qwen.parse_response(payload, 2000)

    def test_missing_word_timing_keeps_whole_valid_sentence_and_marks_review(self):
        payload = response()
        del payload['output']['sentence']['words']
        result = self.qwen.parse_response(payload, 2000)
        self.assertEqual(result.cues, [Cue(100, 1800, 'こんにちは。 またね。')])
        self.assertEqual(result.issues[0]['reason'], 'word_timing_unavailable')
        self.assertEqual((result.issues[0]['start_ms'], result.issues[0]['end_ms']), (100, 1800))

    def test_silence_requires_explicit_complete_empty_result_and_processed_duration(self):
        payload = response()
        payload['output'] = {'text': '', 'sentence': sentence('', [], 0, 0)}
        payload['usage']['output_tokens'] = 0
        payload['usage']['total_tokens'] = 80
        self.assertEqual(self.qwen.parse_response(payload, 2000).cues, [])
        self.assertTrue(self.qwen.parse_response(payload, 2000).metadata['confirmed_silence'])
        for bad in [{}, {'output': {}}, {'output': {'text': ''}},
                    {'output': payload['output']}, {'output': {'text': '', 'sentences': []}, 'usage': payload['usage']}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.qwen.parse_response(bad, 2000)

    def test_token_limit_or_inconsistent_metering_is_rejected(self):
        for usage in [
                {'duration': 2, 'input_tokens': 7169, 'output_tokens': 8},
                {'duration': 2, 'input_tokens': 80, 'output_tokens': 1024},
                {'duration': 2, 'input_tokens': 80, 'output_tokens': 1025},
                {'duration': 2, 'input_tokens': True, 'output_tokens': 8},
                {'duration': 2, 'input_tokens': -1, 'output_tokens': 8},
                {'duration': 2, 'input_tokens': 80, 'output_tokens': '8'},
                {'duration': 2, 'input_tokens': 80, 'output_tokens': 8, 'total_tokens': 89}]:
            payload = response()
            payload['usage'] = usage
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                self.qwen.parse_response(payload, 2000)

    def test_missing_usage_keeps_valid_text_but_marks_conservative_accounting(self):
        payload = response()
        payload.pop('usage')
        result = self.qwen.parse_response(payload, 2000)
        self.assertEqual(len(result.cues), 2)
        self.assertIsNone(result.metadata['token_usage'])
        self.assertTrue(any(issue['reason'] == 'usage_unavailable' for issue in result.issues))

    def test_short_usage_duration_keeps_valid_words_and_flags_duration_difference(self):
        payload = response()
        payload['usage']['duration'] = 26
        result = self.qwen.parse_response(payload, 27008)
        self.assertEqual(result.cues, [Cue(100, 800, 'こんにちは。'), Cue(1100, 1800, ' またね。')])
        self.assertEqual(result.metadata['provider_duration_ms'], 26000)
        self.assertEqual(result.metadata['input_duration_ms'], 27008)
        self.assertEqual(result.issues, [{'reason':'usage_duration_differs',
            'start_ms':0, 'end_ms':27008, 'provider_duration_ms':26000}])

    def test_short_usage_duration_cannot_confirm_an_empty_result(self):
        payload = response()
        payload['output'] = {'text':'', 'sentence':sentence('', [], 0, 0)}
        payload['usage']['duration'] = 26
        with self.assertRaisesRegex(ValueError, '空识别|完整|静音'):
            self.qwen.parse_response(payload, 27008)

    def test_wrong_duration_error_object_or_wrong_channel_is_not_success(self):
        cases = [None, [], {'error': {'message': 'bad'}}, {'code': 'InvalidParameter', **response()}]
        payload = response()
        payload['usage']['duration'] = 200
        cases.append(payload)
        payload = response()
        payload['output']['sentence']['channel_id'] = 1
        cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.qwen.parse_response(payload, 2000)


class FakeResponse(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'application/json'}


class QwenTransportTests(NoNetworkTests):
    def test_english_and_chinese_language_hints_and_metadata_are_explicit(self):
        for language in ('en','zh'):
            with self.subTest(language=language), self.open_with() as send:
                result=self.qwen.transcribe(self.audio,**{**self.options,
                    'request_id':'language-'+language},language=language)
                body=json.loads(send.call_args.args[0].data)
                self.assertEqual(body['parameters']['language_hints'],[language])
                self.assertEqual(result.metadata['language'],language)

    def test_unsupported_language_cannot_upload_audio(self):
        with self.open_with() as send:
            with self.assertRaises(ValueError):
                self.qwen.transcribe(self.audio,**self.options,language='fr')
        send.assert_not_called()

    def test_qianwen_official_transport_preserves_payload_and_budget(self):
        endpoint='https://maas.qianwenaiapi.com/api/v1'
        with self.open_with() as opened:
            result=self.qwen.transcribe(self.audio,**{**self.options,'endpoint':endpoint})
        request=opened.call_args.args[0]
        self.assertEqual(request.full_url,endpoint+'/services/aigc/multimodal-generation/generation')
        self.assertEqual(json.loads(request.data)['parameters']['language_hints'],['ja'])
        self.assertTrue(result.cues)
        self.assertEqual(len(self.ledger.summary()['requests']),1)
        for invalid in ('http://maas.qianwenaiapi.com/api/v1',
                        'https://maas.qianwenaiapi.com.evil.test/api/v1',
                        'https://evil.maas.qianwenaiapi.com/api/v1',
                        'https://secret@maas.qianwenaiapi.com/api/v1',
                        'https://maas.qianwenaiapi.com:444/api/v1'):
            with self.subTest(endpoint=invalid), self.assertRaises(ValueError):
                self.qwen._endpoint_url(invalid)

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.audio = self.folder / 'input.wav'
        self.make_wav(self.audio)
        self.ledger = BudgetLedger(self.folder / 'ledger.json')
        self.options = {'endpoint': 'https://dashscope.aliyuncs.com/api/v1',
                        'key': 'fake-qwen-secret', 'ledger': self.ledger,
                        'request_id': 'qwen-audio-test', 'raw_path': self.folder / 'job' / 'asr-response.json'}

    @staticmethod
    def make_wav(path, seconds=2, rate=16000):
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b'\x00\x00' * int(seconds * rate))

    def open_with(self, payload=None):
        encoded = json.dumps(response() if payload is None else payload, ensure_ascii=False).encode('utf-8')
        return patch.object(self.qwen, '_open', side_effect=lambda *_args, **_kwargs: FakeResponse(encoded))

    def save_empty_rejection(self,request_id=None,body=None,status=400):
        request_id=request_id or self.options['request_id']
        body=body if body is not None else b'{"request_id":"provider-request","code":"CLIENT_ERROR","message":"ASR_RESPONSE_HAVE_NO_WORDS"}'
        canonical=self.folder/'responses'/'qwen'/(request_id+'.json')
        with self.assertRaises(CloudRequestError):
            self.ledger.execute(request_id,'qwen_asr',0.0084992,canonical,
                lambda:HttpResponse(status,{},body))
        return canonical.with_name(canonical.name+f'.attempt-01.http-{status}.body'),body

    def test_empty_draft_reuses_verified_rejection_without_resubmission_or_ledger_changes(self):
        raw,body=self.save_empty_rejection()
        original_ledger=self.ledger.path.read_bytes()
        with patch.object(self.qwen,'_open',side_effect=AssertionError('Rejected cache must never be resent')):
            for _ in range(2):
                result=self.qwen.transcribe(self.audio,**self.options,allow_empty_draft=True)
                self.assertEqual(result.cues,[])
                self.assertFalse(result.metadata['confirmed_silence'])
                self.assertTrue(result.metadata['empty_recognition_requires_review'])
                self.assertEqual(result.issues,[{'reason':'识别服务未返回字词，请核对是否有低声或语气词；未认定为静音',
                    'start_ms':0,'end_ms':2000,'requires_review':True}])
            with self.assertRaises(CloudRequestError):
                self.qwen.transcribe(self.audio,**self.options)
        self.assertEqual(self.ledger.path.read_bytes(),original_ledger)
        self.assertEqual(raw.read_bytes(),body)
        self.assertEqual(self.options['raw_path'].read_bytes(),body)
        self.assertEqual(self.ledger.summary()['requests'][self.options['request_id']]['status'],'rejected')

    def test_empty_draft_rejects_other_http_errors_and_ambiguous_json(self):
        cases=[
            (400,b'{"code":"OTHER","message":"ASR_RESPONSE_HAVE_NO_WORDS"}'),
            (400,b'{"code":"CLIENT_ERROR","message":"OTHER"}'),
            (403,b'{"code":"CLIENT_ERROR","message":"ASR_RESPONSE_HAVE_NO_WORDS"}'),
            (400,b'{"code":"OTHER","code":"CLIENT_ERROR","message":"ASR_RESPONSE_HAVE_NO_WORDS"}'),
            (400,b'{"code":"CLIENT_ERROR","message":"ASR_RESPONSE_HAVE_NO_WORDS","value":NaN}'),
            (400,b'[{"code":"CLIENT_ERROR","message":"ASR_RESPONSE_HAVE_NO_WORDS"}]'),
        ]
        for index,(status,body) in enumerate(cases):
            request_id=f'other-error-{index}'
            self.save_empty_rejection(request_id,body,status)
            with self.subTest(case=index),patch.object(self.qwen,'_open',side_effect=AssertionError('No resend')),self.assertRaises(CloudRequestError):
                self.qwen.transcribe(self.audio,**{**self.options,'request_id':request_id},allow_empty_draft=True)
        self.assertFalse(self.options['raw_path'].exists())

    def test_empty_draft_never_swallows_budget_cancel_or_unknown_errors(self):
        self.save_empty_rejection()
        for error_type in (BudgetExceeded,CloudCancelled,SubmissionUnknown):
            error=error_type('original ledger boundary')
            with self.subTest(error=error_type.__name__),patch.object(self.ledger,'execute',side_effect=error):
                with self.assertRaises(error_type) as caught:
                    self.qwen.transcribe(self.audio,**self.options,allow_empty_draft=True)
                self.assertIs(caught.exception,error)
        self.assertFalse(self.options['raw_path'].exists())

    def test_empty_draft_rejects_missing_tampered_or_misattributed_evidence(self):
        for change in ('missing','body','hash','provider','canonical','attempt_path','unknown'):
            request_id='invalid-evidence-'+change
            raw,body=self.save_empty_rejection(request_id)
            data=json.loads(self.ledger.path.read_text(encoding='utf-8'))
            record=data['requests'][request_id]
            attempt=record['attempts'][-1]
            if change=='missing':raw.unlink()
            elif change=='body':raw.write_bytes(body+b' ')
            elif change=='hash':attempt['raw_sha256']='0'*64
            elif change=='provider':record['provider']='different-provider'
            elif change=='canonical':record['raw_path']=str(self.folder/'other.json')
            elif change=='attempt_path':
                another=self.folder/'other.body';another.write_bytes(body)
                attempt['raw_path']=str(another)
            elif change=='unknown':record['status']=attempt['status']='unknown'
            self.ledger.path.write_text(json.dumps(data),encoding='utf-8')
            before=self.ledger.path.read_bytes()
            with self.subTest(change=change),patch.object(self.qwen,'_open',side_effect=AssertionError('No resend')),self.assertRaises(CloudRequestError):
                self.qwen.transcribe(self.audio,**{**self.options,'request_id':request_id},allow_empty_draft=True)
            self.assertEqual(self.ledger.path.read_bytes(),before)
        self.assertFalse(self.options['raw_path'].exists())

    def test_synchronous_official_request_preserves_audio_and_settles_actual_tokens(self):
        with self.open_with() as opened:
            result = self.qwen.transcribe(self.audio, **self.options)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fake-qwen-secret')
        self.assertEqual(request.get_header('X-dashscope-sse'), 'disable')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], 'qwen-audio-3.1-asr-flash')
        self.assertEqual(payload['parameters'], {'format': 'wav', 'sample_rate': '16000', 'language_hints': ['ja']})
        data = payload['input']['messages'][0]['content'][0]['input_audio']['data']
        self.assertTrue(data.startswith('data:audio/wav;base64,'))
        self.assertEqual(base64.b64decode(data.split(',', 1)[1]), self.audio.read_bytes())
        self.assertNotIn('fake-qwen-secret', request.data.decode())
        record = next(iter(self.ledger.summary()['requests'].values()))
        self.assertAlmostEqual(record['reserved_cny'], 0.0084992)
        self.assertAlmostEqual(record['actual_cny'], 0.0000856)
        self.assertEqual(record['provider'], 'qwen_asr')
        self.assertEqual(result.metadata['model'], 'qwen-audio-3.1-asr-flash')
        self.assertEqual(Path(record['raw_path']).read_bytes(), self.options['raw_path'].read_bytes())
        for path in self.folder.rglob('*.json'):
            self.assertNotIn('fake-qwen-secret', path.read_text(encoding='utf-8'))

    def test_same_request_different_job_copy_reuses_one_canonical_paid_response(self):
        with self.open_with() as opened:
            self.qwen.transcribe(self.audio, **self.options)
            self.options['raw_path'] = self.folder / 'another-job' / 'response.json'
            self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(self.options['raw_path'].is_file())

    def test_bad_schema_is_saved_once_and_never_paid_retried(self):
        payload = response()
        payload['output']['sentence']['text'] = 'last sentence only'
        with self.open_with(payload) as opened:
            for _attempt in range(2):
                with self.assertRaises(ValueError):
                    self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(self.options['raw_path'].is_file())

    def test_no_token_usage_settles_at_maximum_reservation(self):
        payload = response()
        payload.pop('usage')
        with self.open_with(payload):
            self.qwen.transcribe(self.audio, **self.options)
        self.assertAlmostEqual(self.ledger.summary()['spent_cny'], 0.0084992)

    def test_inconsistent_total_usage_preserves_maximum_reserved_cost(self):
        payload = response()
        payload['usage']['total_tokens'] = 999999
        with self.open_with(payload), self.assertRaises(self.qwen.MeteringError):
            self.qwen.transcribe(self.audio, **self.options)
        self.assertAlmostEqual(self.ledger.summary()['spent_cny'], 0.0084992)

    def test_excess_usage_is_accounted_then_stops_without_successful_asr_result(self):
        payload = response()
        payload['usage'] = {'duration': 2, 'input_tokens': 7200, 'output_tokens': 10}
        with self.open_with(payload), self.assertRaisesRegex(ValueError, '计量'):
            self.qwen.transcribe(self.audio, **self.options)
        self.assertAlmostEqual(self.ledger.summary()['spent_cny'], 0.005787)
        self.assertTrue(self.options['raw_path'].is_file())

    def test_metering_error_persistently_blocks_other_request_ids_but_preserves_cached_success(self):
        with self.open_with():
            self.qwen.transcribe(self.audio, **self.options)
        payload = response()
        payload['usage'] = {'duration': 2, 'input_tokens': 7200, 'output_tokens': 10}
        stop = threading.Event()
        with self.open_with(payload) as opened, self.assertRaises(self.qwen.MeteringError):
            self.qwen.transcribe(self.audio, **{**self.options, 'request_id': 'metering-error'}, stop_event=stop)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(stop.is_set())
        marker = self.folder / 'billing-review-required.json'
        saved = marker.read_bytes()
        contents = json.loads(saved)
        self.assertEqual(contents['request_id'], 'metering-error')
        self.assertEqual(contents['provider'], 'qwen_asr')
        self.assertEqual(contents['model'], 'qwen-audio-3.1-asr-flash')
        self.assertIn('计量', contents['reason'])
        self.assertNotIn('fake-qwen-secret', saved.decode('utf-8'))
        stop.clear()
        with self.open_with() as opened:
            with self.assertRaises(self.qwen.MeteringError):
                self.qwen.transcribe(self.audio, **{**self.options, 'request_id': 'different-next-request'}, stop_event=stop)
            result = self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(opened.call_count, 0)
        self.assertEqual(len(result.cues), 2)
        self.assertEqual(len(self.ledger.summary()['requests']), 2)
        self.assertEqual(marker.read_bytes(), saved)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])

    def test_response_copy_failure_cannot_bypass_metering_stop(self):
        payload = response()
        payload['usage'] = {'duration': 2, 'input_tokens': 7200, 'output_tokens': 10}
        blocked_parent = self.folder / 'not-a-directory'
        blocked_parent.write_text('preserve this file', encoding='utf-8')
        stopped = threading.Event()
        with self.open_with(payload) as opened:
            with self.assertRaises(self.qwen.MeteringError):
                self.qwen.transcribe(self.audio, **{**self.options,
                    'raw_path': blocked_parent / 'response.json'}, stop_event=stopped)
            marker = self.folder / 'billing-review-required.json'
            self.assertTrue(marker.is_file(), 'Copy failures must not suppress the billing hold')
            self.assertTrue(stopped.is_set())
            stopped.clear()
            with self.assertRaises(self.qwen.MeteringError):
                self.qwen.transcribe(self.audio, **{**self.options,
                    'request_id': 'after-copy-failure'}, stop_event=stopped)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(blocked_parent.read_text(encoding='utf-8'), 'preserve this file')
        record = self.ledger.summary()['requests']['qwen-audio-test']
        self.assertEqual(record['status'], 'success')
        self.assertAlmostEqual(record['actual_cny'], 0.005787)
        self.assertEqual(json.loads(Path(record['raw_path']).read_text(encoding='utf-8')), payload)
        from subtitle_pipeline.runner import PipelineConfig, cloud_context
        config = PipelineConfig(self.audio, self.folder / 'next-job', budget_ledger=self.ledger.path,
                                asr_provider='qwen_asr', translation_provider='deepseek')
        with patch('subtitle_pipeline.cloud_settings.load_settings',
                   side_effect=AssertionError('The billing hold must precede credential loading')):
            with self.assertRaisesRegex(ValueError, '计量异常'):
                cloud_context(config)

    def test_successful_response_copy_failure_is_reported_and_reuses_paid_cache(self):
        blocked_parent = self.folder / 'not-a-directory'
        blocked_parent.write_text('preserve this file', encoding='utf-8')
        with self.open_with() as opened:
            with self.assertRaises(OSError):
                self.qwen.transcribe(self.audio, **{**self.options,
                    'raw_path': blocked_parent / 'response.json'})
            result = self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(result.cues), 2)
        self.assertEqual(json.loads(self.options['raw_path'].read_text(encoding='utf-8')), response())
        self.assertEqual(blocked_parent.read_text(encoding='utf-8'), 'preserve this file')

    def test_marker_appearing_before_send_releases_reservation_and_never_uploads(self):
        execute = self.ledger.execute
        def gate(**kwargs):
            (self.folder / 'billing-review-required.json').write_text('{}', encoding='utf-8')
            return execute(**kwargs)
        with patch.object(self.ledger, 'execute', side_effect=gate), self.open_with() as opened:
            with self.assertRaises(CloudCancelled):
                self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(opened.call_count, 0)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)

    def test_timeout_remains_unknown_across_restart_without_resubmission(self):
        with patch.object(self.qwen, '_open', side_effect=URLError('fake-qwen-secret')) as opened:
            for _attempt in range(2):
                with self.assertRaises(SubmissionUnknown) as error:
                    self.qwen.transcribe(self.audio, **self.options)
                self.assertNotIn('fake-qwen-secret', str(error.exception))
        self.assertEqual(opened.call_count, 1)
        self.assertAlmostEqual(self.ledger.summary()['reserved_cny'], 0.0084992)

    def test_empty_wrong_format_long_and_truncated_wav_fail_before_reservation(self):
        for duration, rate in [(0, 16000), (2, 8000), (180.01, 16000)]:
            with self.subTest(duration=duration, rate=rate):
                self.make_wav(self.audio, duration, rate)
                with self.assertRaises(ValueError):
                    self.qwen.transcribe(self.audio, **self.options)
        self.make_wav(self.audio)
        self.audio.write_bytes(self.audio.read_bytes()[:-2])
        with self.assertRaises(ValueError):
            self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(self.ledger.summary()['requests'], {})

    def test_base64_size_limit_is_checked_before_request(self):
        self.audio.write_bytes(self.audio.read_bytes() + b'\x00' * 7_500_000)
        with self.assertRaises(ValueError):
            self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(self.ledger.summary()['requests'], {})

    def test_credentials_endpoint_model_and_copy_paths_are_validated_before_network(self):
        invalid = [
            ('endpoint', 'https://dashscope.aliyuncs.com.evil.example/api/v1'),
            ('endpoint', 'https://dashscope.aliyuncs.com/api/v1?'),
            ('endpoint', 'https://x.ap-southeast-1.maas.aliyuncs.com/api/v1'),
            ('endpoint', 'https://user:pass@dashscope.aliyuncs.com/api/v1'),
            ('endpoint', 'http://dashscope.aliyuncs.com/api/v1'),
            ('key', 'bad\nsecret'), ('key', ''), ('model', 'qwen-audio-3.0-asr-flash'),
            ('request_id', '../escape'), ('request_id', 'CON'),
            ('raw_path', self.ledger.path), ('raw_path', self.audio),
            ('input_rate_cny_per_million', float('nan')), ('output_rate_cny_per_million', 0)]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.qwen.transcribe(self.audio, **{**self.options, field: value})
        self.assertEqual(self.ledger.summary()['requests'], {})
        with self.open_with() as opened:
            self.qwen.transcribe(self.audio, **{**self.options,
                'endpoint': 'https://workspace-123.cn-beijing.maas.aliyuncs.com/api/v1/'})
        self.assertTrue(opened.call_args.args[0].full_url.startswith('https://workspace-123.cn-beijing.maas.aliyuncs.com/'))

    def test_cancellation_before_send_or_after_response_does_not_erase_paid_accounting(self):
        stopped = threading.Event()
        stopped.set()
        with self.assertRaises(CloudCancelled):
            self.qwen.transcribe(self.audio, **self.options, stop_event=stopped)
        self.assertEqual(self.ledger.summary()['requests'], {})
        stopped.clear()
        def reply(*_args, **_kwargs):
            stopped.set()
            return FakeResponse(json.dumps(response()).encode())
        with patch.object(self.qwen, '_open', side_effect=reply):
            with self.assertRaises(CloudCancelled):
                self.qwen.transcribe(self.audio, **self.options, stop_event=stopped)
        self.assertGreater(self.ledger.summary()['spent_cny'], 0)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)
        stopped.clear()
        self.assertEqual(len(self.qwen.transcribe(self.audio, **self.options, stop_event=stopped).cues), 2)

    def test_http_error_is_given_to_ledger_and_closed_without_adapter_retry(self):
        error = HTTPError('https://dashscope.aliyuncs.com/api/v1', 401, 'Unauthorized', {}, io.BytesIO(b'{}'))
        with patch.object(self.qwen, '_open', side_effect=error) as opened:
            with self.assertRaises(CloudRequestError):
                self.qwen.transcribe(self.audio, **self.options)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(error.closed)
        self.assertEqual(self.ledger.summary()['reserved_cny'], 0)


if __name__ == '__main__':
    unittest.main()
