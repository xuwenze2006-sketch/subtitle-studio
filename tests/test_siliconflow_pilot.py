import importlib.util
from datetime import date
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave


class SiliconflowPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.wav=self.root/'sample.wav'
        with wave.open(str(self.wav),'wb') as stream:
            stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(b'\0\0'*16000)
        guard=patch('socket.socket.connect',side_effect=AssertionError('Tests must not access network'))
        guard.start();self.addCleanup(guard.stop)
        self.pricing={'SILICONFLOW_API_KEY':'fake', 'SILICONFLOW_ASR_CNY_PER_SECOND':'0.000220',
            'SILICONFLOW_PRICING_VERIFIED_ON':date.today().isoformat(),
            'SILICONFLOW_PRICING_REFERENCE':'offline account quotation',
            'SILICONFLOW_FREE_ASR_VERIFIED_ON':date.today().isoformat()}
        guard=patch('subtitle_pipeline.cloud_settings.read_environment',return_value=self.pricing)
        guard.start();self.addCleanup(guard.stop)

    def test_candidate_entrypoint_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.siliconflow_pilot'))

    def test_same_sample_reuses_original_response_without_upload(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse
        ledger=BudgetLedger(self.root/'cost.json')
        body=b'{"text":"neutral sample"}'
        with patch.object(p,'request',return_value=HttpResponse(200,{},body)) as send:
            a=p.transcribe(self.wav,key='fake',ledger=ledger)
            b=p.transcribe(self.wav,key='fake',ledger=ledger)
        self.assertEqual(send.call_count,1)
        self.assertEqual(a,b)
        self.assertAlmostEqual(ledger.summary()['committed_cny'],0.000220)
        self.assertFalse(a['timing_verified'])

    def test_language_specific_auditions_do_not_adopt_different_language_cache(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse
        ledger=BudgetLedger(self.root/'cost.json')
        results=[]
        with patch.object(p,'request',return_value=HttpResponse(200,{},b'{"text":"offline"}')) as send:
            for language in ('ja','en','zh','en'):
                results.append(p.transcribe(self.wav,key='fake',ledger=ledger,language=language))
        self.assertEqual(send.call_count,3)
        self.assertEqual(len({item['request_id'] for item in results}),3)
        self.assertEqual(results[1],results[3])
        self.assertEqual(results[2]['language'],'zh')
        self.assertEqual(results[2]['language_detection'],'provider_auto')
        self.assertFalse(results[2]['timing_verified'])

    def test_audition_titles_follow_source_language(self):
        from subtitle_pipeline import siliconflow_pilot as p
        self.assertIn('英语识别试听',p.render_audition([],language='en'))
        self.assertNotIn('日语识别试听',p.render_audition([],language='zh'))

    def test_bad_response_retains_raw_and_is_not_resent(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse
        ledger=BudgetLedger(self.root/'cost.json')
        with patch.object(p,'request',return_value=HttpResponse(200,{},b'{}')) as send:
            for _ in range(2):
                with self.assertRaises(ValueError):p.transcribe(self.wav,key='fake',ledger=ledger)
        self.assertEqual(send.call_count,1)

    def test_rounded_audio_seconds_are_reserved_before_upload_without_zero_usage_discount(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse
        with wave.open(str(self.wav),'wb') as stream:
            stream.setparams((1,2,16000,0,'NONE','not compressed'))
            stream.writeframes(b'\0\0'*16001)
        ledger=BudgetLedger(self.root/'cost.json')
        observed=[]
        def send(*_args):
            observed.append(ledger.summary()['reserved_cny'])
            return HttpResponse(200,{},b'{"text":"offline","usage":{"total_tokens":0}}')
        with patch.object(p,'request',side_effect=send):
            result=p.transcribe(self.wav,key='fake',ledger=ledger)
        self.assertEqual(observed,[0.00044])
        summary=ledger.summary()
        self.assertEqual(summary['committed_cny'],0.00044)
        record=next(iter(summary['requests'].values()))
        self.assertEqual(record['cost_source'],'reservation')
        self.assertNotIn('free',record['provider'])
        self.assertEqual(result['price_per_second'],0.00022)

    def test_paid_stop_line_prevents_upload(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,BudgetExceeded,HttpResponse
        self.pricing['SILICONFLOW_ASR_CNY_PER_SECOND']='18'
        with patch.object(p,'request',return_value=HttpResponse(200,{},b'{"text":"offline"}')) as request:
            with self.assertRaises(BudgetExceeded):
                p.transcribe(self.wav,key='fake',ledger=BudgetLedger(self.root/'cost.json'))
        request.assert_not_called()

    def test_old_free_confirmation_cannot_authorize_new_requests(self):
        from subtitle_pipeline import siliconflow_pilot as p
        self.pricing.pop('SILICONFLOW_ASR_CNY_PER_SECOND')
        self.pricing.pop('SILICONFLOW_PRICING_VERIFIED_ON')
        self.pricing.pop('SILICONFLOW_PRICING_REFERENCE')
        with self.assertRaises(ValueError):p.configuration()

    def test_legacy_zero_cost_cache_is_blocked_for_explicit_billing_review(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse,CloudRequestError
        ledger=BudgetLedger(self.root/'cost.json')
        identity=p.fingerprint({'provider':'siliconflow','model':p.MODEL,
            'audio_sha256':p.sha256(self.wav),'schema':1})
        raw=ledger.path.parent/'responses'/'siliconflow'/f'{identity}.json'
        ledger.execute(identity,'siliconflow_free_asr',0,raw,
            lambda:HttpResponse(200,{},b'{"text":"legacy cached result"}'),actual_cost=lambda _:0)
        before=ledger.path.read_bytes()
        with patch.object(p,'request') as request:
            with self.assertRaisesRegex(CloudRequestError,'旧|核对|核账'):
                p.transcribe(self.wav,key='fake',ledger=ledger)
        request.assert_not_called()
        self.assertEqual(ledger.path.read_bytes(),before)

    def test_changed_price_cannot_cause_paid_cache_to_be_uploaded_again(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse,CloudRequestError
        ledger=BudgetLedger(self.root/'cost.json')
        with patch.object(p,'request',return_value=HttpResponse(200,{},b'{"text":"offline"}')) as request:
            p.transcribe(self.wav,key='fake',ledger=ledger)
            self.pricing['SILICONFLOW_ASR_CNY_PER_SECOND']='0.000440'
            with self.assertRaises(CloudRequestError):p.transcribe(self.wav,key='fake',ledger=ledger)
        self.assertEqual(request.call_count,1)

    def test_unknown_submit_does_not_retry(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import BudgetLedger,SubmissionUnknown
        ledger=BudgetLedger(self.root/'cost.json')
        with patch.object(p,'request',side_effect=TimeoutError()) as send:
            for _ in range(2):
                with self.assertRaises(SubmissionUnknown):p.transcribe(self.wav,key='fake',ledger=ledger)
        self.assertEqual(send.call_count,1)

    def test_preflight_rejects_missing_model(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import HttpResponse
        with patch.object(p,'request',return_value=HttpResponse(200,{},b'{"data":[{"id":"other"}]}')):
            with self.assertRaises(ValueError):p.verify_model('fake')

    def test_html_marks_text_as_untimed_and_escapes_it(self):
        from subtitle_pipeline import siliconflow_pilot as p
        html=p.render_audition([{'name':'test','video':'v.mp4','old':'old','text':'</script><img onerror=bad>'}])
        self.assertIn('未对齐',html)
        self.assertNotIn('<img onerror=bad>',html)

    def make_campaign(self, durations=(90,120,90), damaged_index=None):
        from subtitle_pipeline.integrity import sha256
        source=self.root/'source.mp4';source.write_bytes(b'local fake source')
        samples=[]
        for index,((start,end),duration) in enumerate(zip(((30,120),(1680,1800),(5400,5490)),durations)):
            folder=self.root/f'sample-{index}';folder.mkdir()
            audio=folder/'input.wav'
            with wave.open(str(audio),'wb') as stream:
                stream.setparams((1,2,16000,0,'NONE','not compressed'))
                stream.writeframes(bytes([index,0])*int(16000*duration))
            (folder/'旧日语.srt').write_text('',encoding='utf-8')
            (folder/'preview.mp4').write_bytes(b'offline preview')
            preview_hash=sha256(folder/'preview.mp4')
            samples.append({'name':f'sample {index}','folder':folder.name,
                'start_sec':start,'end_sec':end,'audio_hash':sha256(audio),
                'preview_hash':preview_hash,'preview_validation':{'version':2,'sha256':preview_hash,
                'expected_duration_ms':round((end-start)*1000),'video_frames':round((end-start)*24)}})
            if index==damaged_index:audio.write_bytes(audio.read_bytes()+b'modified')
        manifest={'status':'prepared','source':{'path':str(source),'sha256':sha256(source)},'samples':samples}
        (self.root/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def fake_request(self,path,key,body=None,content_type=None):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline.cloud_budget import HttpResponse
        return HttpResponse(200,{},json.dumps({'data':[{'id':p.MODEL}]} if path=='/models' else {'text':'offline'}).encode())

    def test_all_actual_durations_are_verified_before_model_lookup_or_upload(self):
        from subtitle_pipeline import siliconflow_pilot as p
        self.make_campaign((180,180,180))
        with patch.object(p,'configuration',return_value='fake'),patch.object(p,'request',side_effect=self.fake_request) as request,patch.object(p,'emit'):
            with self.assertRaises(ValueError):p.run(self.root,threading.Event())
        self.assertEqual(0,request.call_count)

    def test_last_sample_hash_failure_prevents_even_first_sample_upload(self):
        from subtitle_pipeline import siliconflow_pilot as p
        self.make_campaign(damaged_index=2)
        with patch.object(p,'configuration',return_value='fake'),patch.object(p,'request',side_effect=self.fake_request) as request,patch.object(p,'emit'):
            with self.assertRaises(ValueError):p.run(self.root,threading.Event())
        self.assertEqual(0,request.call_count)

    def test_cli_recovers_dead_pid_lock_and_reuses_original_results(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline import runner
        self.make_campaign()
        before=(self.root/'campaign.json').read_bytes()
        (self.root/'run.lock').write_text(json.dumps({'pid':999999999}),encoding='utf-8')
        (self.root/'STOP.flag').write_text('previous interrupted run',encoding='utf-8')
        with patch.object(runner,'pid_alive',return_value=False),patch.object(p,'configuration',return_value='fake'), \
                patch.object(p,'request',side_effect=self.fake_request) as request,patch.object(p,'emit'):
            first=p.main(['--campaign',str(self.root)])
            self.assertEqual(0,first)
            second=p.main(['--campaign',str(self.root)])
        self.assertEqual(0,second)
        self.assertEqual(3,sum(call.args[0]=='/audio/transcriptions' for call in request.call_args_list))
        self.assertEqual(before,(self.root/'campaign.json').read_bytes())
        self.assertFalse((self.root/'run.lock').exists())
        self.assertFalse((self.root/'STOP.flag').exists())

    def test_live_pid_lock_keeps_other_owners_stop_flag_and_starts_no_watcher(self):
        from subtitle_pipeline import siliconflow_pilot as p
        from subtitle_pipeline import runner
        (self.root/'run.lock').write_text(json.dumps({'pid':12345}),encoding='utf-8')
        (self.root/'STOP.flag').write_text('other owner stop',encoding='utf-8')
        with patch.object(runner,'pid_alive',return_value=True),patch.object(p.threading,'Thread') as thread, \
                patch.object(p,'request') as request,patch.object(p,'emit'):
            self.assertEqual(2,p.main(['--campaign',str(self.root)]))
        thread.assert_not_called();request.assert_not_called()
        self.assertEqual('other owner stop',(self.root/'STOP.flag').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
