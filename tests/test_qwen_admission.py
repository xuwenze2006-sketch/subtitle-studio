"""Real local ledger/adapter recovery; transport and media extraction are fake."""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
import wave

from subtitle_pipeline import runner as r, qwen_asr as q
from subtitle_pipeline import export_io
from subtitle_pipeline.cloud_budget import BudgetLedger, SubmissionUnknown
from tests import test_qwen_asr as fixtures


class Reply:
    status=200
    headers={}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size):return json.dumps(fixtures.response()).encode()


class QwenAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='qwen-admission-')
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.source=self.root/'source.mp4';self.source.write_bytes(b'self-owned fake media')
        self.plan_ms=7021;self.pcm_frames=128336
        self.stop=threading.Event();self.commands=[]
        self.config=r.PipelineConfig(self.source,self.root/'job',translate=False,
            chunk_seconds=120,overlap_seconds=2,workers=2,asr_provider='qwen_asr',workflow_stage='draft',
            budget_ledger=self.root/'budget.json')
        self.settings=SimpleNamespace(asr_endpoint='https://dashscope.aliyuncs.com/api/v1',
            asr_key='fake-key-no-network',asr_input_rate=.8,asr_output_rate=2.7)
        self.ledger=BudgetLedger(self.config.budget_ledger)

    def media(self,args,log,stop,**kwargs):
        self.commands.append([str(arg) for arg in args])
        path=Path(args[-1]);self.assertEqual(path.suffix,'.wav')
        count=self.pcm_frames if '-t' not in args else round(float(args[args.index('-t')+1])*16000)
        with wave.open(str(path),'wb') as wav:
            wav.setparams((1,2,16000,0,'NONE','not compressed'))
            wav.writeframes(b'\0\0'*count)

    def duration(self,path,**kwargs):
        if Path(path)==self.source:return self.plan_ms
        with wave.open(str(path),'rb') as wav:return round(wav.getnframes()*1000/wav.getframerate())

    @contextmanager
    def services(self):
        with patch('socket.create_connection',side_effect=AssertionError('network disabled')), \
                patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network disabled')), \
                patch.object(r,'cloud_context',return_value=(self.settings,self.ledger)), \
                patch.object(r,'probe_media',side_effect=self.duration), \
                patch.object(r,'detect_silences',return_value=[]), \
                patch.object(r,'run_process',side_effect=self.media), \
                patch.object(r,'translate_texts',side_effect=lambda texts,*a,**kw:['translation '+s for s in texts]):
            yield

    def pending(self):
        with self.services(),patch.object(q,'transcribe',side_effect=ValueError('offline pending fixture')):
            state=r.run_pipeline(self.config)
        self.assertEqual(state['status'],'asr_incomplete')
        return state

    def seed_raw(self, transport_error=None):
        self.pending()
        audio=self.config.project/'片段/0001/audio.wav'
        request_id=r.fingerprint({'provider':'qwen_asr','audio':r.sha256(audio),
            'endpoint':self.settings.asr_endpoint,'api':self.config.asr_api_version,
            'model':self.config.asr_model,'language_hints':[self.config.language],'diarization':False})
        with patch.object(q,'_open',return_value=Reply(),side_effect=transport_error) as sent:
            if transport_error is None:
                q.transcribe(audio,key=self.settings.asr_key,model=self.config.asr_model,
                    ledger=self.ledger,request_id=request_id,raw_path=audio.parent/'asr-response.json')
            else:
                with self.assertRaises(SubmissionUnknown):
                    q.transcribe(audio,key=self.settings.asr_key,model=self.config.asr_model,
                        ledger=self.ledger,request_id=request_id,raw_path=audio.parent/'asr-response.json')
        self.assertEqual(sent.call_count,1)
        return request_id

    def test_fresh_mismatched_pcm_never_reserves_or_sends(self):
        with self.services(),patch.object(q,'_open',return_value=Reply()) as sent:
            state=r.run_pipeline(self.config)
        self.assertEqual(sent.call_count,0)
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertIn('PCM',state['parts']['0']['error'])
        self.assertEqual(self.ledger.summary()['committed_cny'],0)
        self.assertTrue((self.config.project/'timeline.wav').exists())

    def test_valid_hash_cached_mismatch_never_reserves_or_sends(self):
        self.pending();self.commands.clear()
        with self.services(),patch.object(q,'_open',return_value=Reply()) as sent:
            state=r.run_pipeline(self.config)
        self.assertEqual(sent.call_count,0)
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertEqual(self.ledger.summary()['committed_cny'],0)
        self.assertEqual(len(self.commands),1)  # only the requested chunk

    def test_paid_success_and_received_recover_without_new_admission(self):
        request_id=self.seed_raw()
        for status in ('received','success'):
            with self.subTest(status=status):
                data=self.ledger._load();record=data['requests'][request_id]
                record['status']=status;record['attempts'][-1]['status']=status
                self.ledger._save(data)
                state=json.loads((self.config.project/'state.json').read_text(encoding='utf-8'))
                state['parts']={};r.atomic_json(self.config.project/'state.json',state)
                self.commands.clear()
                with self.services(),patch.object(q,'_open',side_effect=AssertionError('paid raw must be reused')), \
                        patch('subtitle_pipeline.audio_range.check_pcm_plan',side_effect=AssertionError('paid raw needs no admission')) as admission:
                    state=r.run_pipeline(self.config)
                admission.assert_not_called()
                self.assertEqual(state['status'],'complete')
                self.assertEqual(state['recognized'],1)
                self.assertEqual(len(self.ledger._load()['requests'][request_id]['attempts']),1)

    def test_unknown_stays_unknown_and_reserved_without_new_admission(self):
        self.seed_raw(URLError('offline ambiguous transport'))
        before=self.ledger._load();summary=self.ledger.summary()
        self.assertEqual(next(iter(before['requests'].values()))['status'],'unknown')
        with self.services(),patch.object(q,'_open',side_effect=AssertionError('unknown must not send')), \
                patch('subtitle_pipeline.audio_range.check_pcm_plan',side_effect=AssertionError('unknown must not admit')) as admission:
            state=r.run_pipeline(self.config)
        admission.assert_not_called()
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertEqual(self.ledger._load(),before)
        self.assertEqual(self.ledger.summary(),summary)

    def test_normal_pcm_and_completed_cache_need_no_repeat_media(self):
        self.plan_ms=8000;self.pcm_frames=128000
        with self.services(),patch.object(q,'_open',return_value=Reply()) as sent:
            state=r.run_pipeline(self.config)
        self.assertEqual(state['status'],'complete');self.assertEqual(sent.call_count,1)
        (self.config.project/'timeline.wav').unlink()
        with self.services(),patch.object(r,'run_process',side_effect=AssertionError('completed cache must not decode')), \
                patch.object(q,'_open',side_effect=AssertionError('completed cache must not submit')):
            completed=r.run_pipeline(replace(self.config,translate=True))
        self.assertEqual(completed['status'],'complete')
        self.assertEqual((completed['recognized'],completed['translated']),(1,1))

    def test_normal_pcm_fractional_rounding_is_accepted(self):
        self.plan_ms=8022;self.pcm_frames=128341
        with self.services(),patch.object(q,'_open',return_value=Reply()) as sent:
            state=r.run_pipeline(self.config)
        self.assertEqual(state['status'],'complete')
        self.assertEqual(sent.call_count,1)

    def test_stop_after_pcm_admission_has_no_reservation_or_transmission(self):
        from subtitle_pipeline.audio_range import check_pcm_plan
        self.plan_ms=8000;self.pcm_frames=128000
        def checked(*args):
            value=check_pcm_plan(*args)
            self.stop.set()
            return value
        with self.services(),patch('subtitle_pipeline.audio_range.check_pcm_plan',side_effect=checked), \
                patch.object(q,'_open',side_effect=AssertionError('cancelled admission must not transmit')):
            state=r.run_pipeline(self.config,stop_event=self.stop)
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual(self.ledger.summary()['committed_cny'],0)

    def test_adapter_forwards_admission_and_preserves_exact_rejection(self):
        audio=self.root/'audio.wav'
        with wave.open(str(audio),'wb') as wav:
            wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*32000)
        error=ValueError('offline admission denied')
        def reject():raise error
        with patch.object(q,'_open',side_effect=AssertionError('rejected must not transmit')), \
                self.assertRaises(ValueError) as caught:
            q.transcribe(audio,key=self.settings.asr_key,ledger=self.ledger,
                request_id='admission',raw_path=self.root/'raw.json',before_submit=reject)
        self.assertIs(caught.exception,error)
        self.assertEqual(self.ledger.summary()['committed_cny'],0)

    def replace_pcm(self, path):
        temporary=path.with_name(path.name+'.replacement')
        with wave.open(str(temporary),'wb') as wav:
            wav.setparams((1,2,16000,0,'NONE','not compressed'))
            wav.writeframes(b'\x01\0'*self.pcm_frames)
        os.replace(temporary,path)

    def assert_changed_before_chunk_is_blocked(self, cached):
        self.plan_ms=8000;self.pcm_frames=128000
        if cached:self.pending()
        prepare=r.prepare_cloud_audio
        def prepared(config,state,stop):
            token=prepare(config,state,stop)
            self.replace_pcm(config.project/'timeline.wav')
            return token
        with self.services(),patch.object(r,'prepare_cloud_audio',side_effect=prepared), \
                patch.object(q,'_open',return_value=Reply()) as sent:
            state=r.run_pipeline(self.config)
        sent.assert_not_called()
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertRegex(state['parts']['0']['error'],'PCM.*变化.*未启动新识别')
        self.assertEqual(self.ledger.summary()['committed_cny'],0)
        self.assertTrue((self.config.project/'timeline.wav').exists())
        self.assertTrue((self.config.project/'片段/0001/audio.wav').exists())

    def test_fresh_same_length_replacement_after_prepare_is_blocked(self):
        self.assert_changed_before_chunk_is_blocked(False)

    def test_cached_same_length_replacement_after_prepare_is_blocked(self):
        self.assert_changed_before_chunk_is_blocked(True)

    def test_replacement_during_chunk_extraction_is_blocked_before_reservation(self):
        self.plan_ms=8000;self.pcm_frames=128000
        original=self.media
        def extraction(args,*a,**kw):
            original(args,*a,**kw)
            if Path(args[-1]).name=='audio.wav':
                self.replace_pcm(self.config.project/'timeline.wav')
        with self.services(),patch.object(r,'run_process',side_effect=extraction), \
                patch.object(q,'_open',return_value=Reply()) as sent:
            state=r.run_pipeline(self.config)
        sent.assert_not_called()
        self.assertRegex(state['parts']['0']['error'],'PCM.*变化.*未启动新识别')
        self.assertEqual(self.ledger.summary()['committed_cny'],0)

    def test_fresh_partial_changed_during_hash_is_not_published(self):
        self.plan_ms=8000;self.pcm_frames=128000
        self.config.project.mkdir()
        state={'duration_ms':self.plan_ms}
        hash_file=export_io.cancellable_sha256
        def changed(path,stop):
            digest=hash_file(path,stop)
            self.replace_pcm(Path(path))
            return digest
        with self.services(),patch.object(export_io,'cancellable_sha256',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'PCM.*变化.*未启动新识别'):
                r.prepare_cloud_audio(self.config,state,self.stop)
        self.assertNotIn('timeline_hash',state)
        self.assertTrue((self.config.project/'timeline.partial.wav').exists())
        self.assertFalse((self.config.project/'timeline.wav').exists())

    def test_cached_pcm_changed_during_hash_keeps_original_state(self):
        self.plan_ms=8000;self.pcm_frames=128000
        state=self.pending();before=dict(state)
        hash_file=export_io.cancellable_sha256
        def changed(path,stop):
            digest=hash_file(path,stop)
            self.replace_pcm(Path(path))
            return digest
        with patch.object(export_io,'cancellable_sha256',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'PCM.*变化.*未启动新识别'):
                r.prepare_cloud_audio(self.config,state,self.stop)
        self.assertEqual(state,before)
        self.assertTrue((self.config.project/'timeline.wav').exists())

    def test_new_publication_must_be_the_same_verified_file_object(self):
        self.plan_ms=8000;self.pcm_frames=128000
        self.config.project.mkdir();state={'duration_ms':self.plan_ms}
        rename=os.replace
        # Avoid recursion inside the fixture's own replacement operation.
        def replacement(source,target):
            if Path(source).name=='timeline.partial.wav':
                other=Path(source).with_name('other.wav')
                with wave.open(str(other),'wb') as wav:
                    wav.setparams((1,2,16000,0,'NONE','not compressed'))
                    wav.writeframes(b'\x01\0'*self.pcm_frames)
                rename(other,source)
            rename(source,target)
        with self.services(),patch.object(r.os,'replace',side_effect=replacement):
            with self.assertRaisesRegex(ValueError,'PCM.*变化.*未启动新识别'):
                r.prepare_cloud_audio(self.config,state,self.stop)
        self.assertNotIn('timeline_hash',state)
        self.assertTrue((self.config.project/'timeline.wav').exists())

    def test_partial_ctime_change_after_hash_stops_before_rename(self):
        self.plan_ms=8000;self.pcm_frames=128000
        self.config.project.mkdir();state={'duration_ms':self.plan_ms}
        token_of=r._pcm_file_token
        observed=[]
        def token(path,stop):
            value=token_of(path,stop)
            if Path(path).name=='timeline.partial.wav':
                observed.append(value)
                if len(observed)==3:return (*value[:4],value[4]+1)
            return value
        with self.services(),patch.object(r,'_pcm_file_token',side_effect=token), \
                patch.object(r.os,'replace',side_effect=AssertionError('unverified rename')):
            with self.assertRaisesRegex(ValueError,'PCM.*变化.*未启动新识别'):
                r.prepare_cloud_audio(self.config,state,self.stop)
        self.assertNotIn('timeline_hash',state)
        self.assertTrue((self.config.project/'timeline.partial.wav').exists())

    def test_cancel_after_new_pcm_rename_does_not_bind_hash(self):
        self.plan_ms=8000;self.pcm_frames=128000
        self.config.project.mkdir();state={'duration_ms':self.plan_ms}
        rename=os.replace
        def stopped(source,target):
            rename(source,target)
            self.stop.set()
        with self.services(),patch.object(r.os,'replace',side_effect=stopped):
            with self.assertRaises(r.Cancelled):
                r.prepare_cloud_audio(self.config,state,self.stop)
        self.assertNotIn('timeline_hash',state)
        self.assertTrue((self.config.project/'timeline.wav').exists())

    def test_pcm_binding_metadata_and_hash_failures_preserve_error_or_cancel_cause(self):
        self.plan_ms=8000;self.pcm_frames=128000
        self.config.project.mkdir();target=self.config.project/'timeline.wav'
        self.replace_pcm(target)
        state={'duration_ms':self.plan_ms,'timeline_hash':r.sha256(target)}
        before=target.read_bytes();saved=dict(state)
        for operation in ('stat','hash'):
            for cancel in (False,True):
                with self.subTest(operation=operation,cancel=cancel):
                    self.stop.clear();failure=PermissionError('synthetic PCM access denied')
                    actual_stat=Path.stat;calls=[]
                    def fail(*args,**kwargs):
                        if cancel:self.stop.set()
                        raise failure
                    def stat(path,*args,**kwargs):
                        if path==target:
                            calls.append(path)
                            if len(calls)==2:return fail()
                        return actual_stat(path,*args,**kwargs)
                    patched=patch.object(Path,'stat',stat) if operation=='stat' else patch.object(export_io,'cancellable_sha256',side_effect=fail)
                    with patched:
                        with self.assertRaises(r.Cancelled if cancel else PermissionError) as caught:
                            r.prepare_cloud_audio(self.config,state,self.stop)
                    self.assertIs(caught.exception.__cause__ if cancel else caught.exception,failure)
                    self.assertEqual(state,saved)
                    self.assertEqual(target.read_bytes(),before)

    def test_pcm_rename_failure_preserves_original_error_or_cancel_cause(self):
        self.plan_ms=8000;self.pcm_frames=128000
        for cancel in (False,True):
            with self.subTest(cancel=cancel):
                self.stop.clear()
                project=self.root/f'rename-failure-{cancel}'
                project.mkdir()
                config=replace(self.config,project=project)
                state={'duration_ms':self.plan_ms}
                failure=PermissionError('synthetic PCM rename denied')
                def denied(*args):
                    if cancel:self.stop.set()
                    raise failure
                with self.services(),patch.object(r.os,'replace',side_effect=denied), \
                        self.assertRaises(r.Cancelled if cancel else PermissionError) as caught:
                    r.prepare_cloud_audio(config,state,self.stop)
                self.assertIs(caught.exception.__cause__ if cancel else caught.exception,failure)
                self.assertNotIn('timeline_hash',state)
                self.assertTrue((project/'timeline.partial.wav').exists())
                self.assertFalse((project/'timeline.wav').exists())

    def test_retry_after_pcm_replacement_does_not_send_again(self):
        from subtitle_pipeline import cloud_budget as cloud
        self.plan_ms=8000;self.pcm_frames=128000
        class Limited(Reply):
            status=429;headers={'Retry-After':'1'}
            def read(self,size):return b'{"limited":true}'
        def waited(delay,stop):
            self.replace_pcm(self.config.project/'timeline.wav')
            return False
        with self.services(),patch.object(q,'_open',side_effect=[Limited(),Reply()]) as sent, \
                patch.object(cloud,'_wait_retry',side_effect=waited):
            state=r.run_pipeline(self.config)
        self.assertEqual(sent.call_count,1)
        self.assertRegex(state['parts']['0']['error'],'PCM.*变化.*未启动新识别')
        record=next(iter(self.ledger._load()['requests'].values()))
        self.assertEqual(record['status'],'retry_wait')
        self.assertEqual(len(record['attempts']),1)
        self.assertGreater(self.ledger.summary()['reserved_cny'],0)

    def test_valid_preparation_hashes_timeline_once_per_run(self):
        self.plan_ms=8000;self.pcm_frames=128000
        self.config.project.mkdir();state={'duration_ms':self.plan_ms}
        for cached in (False,True):
            with self.subTest(cached=cached),self.services(), \
                    patch.object(export_io,'cancellable_sha256',wraps=export_io.cancellable_sha256) as hashed:
                token=r.prepare_cloud_audio(self.config,state,self.stop)
            self.assertEqual(hashed.call_count,1)
            stat=(self.config.project/'timeline.wav').stat()
            self.assertEqual(token,(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))

    def test_changed_pcm_does_not_block_existing_paid_or_unknown_response(self):
        self.plan_ms=8000;self.pcm_frames=128000
        request_id=self.seed_raw()
        for status in ('received','success','unknown'):
            with self.subTest(status=status):
                data=self.ledger._load();record=data['requests'][request_id]
                record['status']=status;record['attempts'][-1]['status']=status
                self.ledger._save(data)
                state=json.loads((self.config.project/'state.json').read_text(encoding='utf-8'))
                state['parts']={};r.atomic_json(self.config.project/'state.json',state)
                prepare=r.prepare_cloud_audio
                def prepared(config,state,stop):
                    token=prepare(config,state,stop)
                    self.replace_pcm(config.project/'timeline.wav')
                    return token
                with self.services(),patch.object(r,'prepare_cloud_audio',side_effect=prepared), \
                        patch.object(q,'_open',side_effect=AssertionError('cached response must not send')), \
                        patch('subtitle_pipeline.audio_range.check_pcm_plan',side_effect=AssertionError('no new admission')) as admitted:
                    state=r.run_pipeline(self.config)
                admitted.assert_not_called()
                self.assertEqual(state['status'],'asr_incomplete' if status=='unknown' else 'complete')
                self.assertEqual(len(self.ledger._load()['requests'][request_id]['attempts']),1)


if __name__=='__main__':unittest.main()
