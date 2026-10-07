"""Qwen runner integration with real durable accounting and no network access."""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import runner
from subtitle_pipeline.cloud_budget import BudgetExceeded, BudgetLedger, HttpResponse
from subtitle_pipeline.integrity import sha256
from subtitle_pipeline.subtitles import Cue, parse_srt


class QwenPipelineTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'urllib.request.urlopen', 'urllib.request.build_opener'):
            guard=patch(target,side_effect=AssertionError('Network disabled in Qwen pipeline tests'))
            guard.start()
            self.addCleanup(guard.stop)
        guard=patch.object(runner,'run_process',side_effect=AssertionError('External commands disabled'))
        guard.start()
        self.addCleanup(guard.stop)
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.source=self.root/'source.mp4'
        self.source.write_bytes(b'synthetic input only')
        self.campaign=self.root/'campaign'
        self.campaign.mkdir()
        self.duration=25000
        self.config=runner.PipelineConfig(self.source,self.campaign/'job',chunk_seconds=10,
            overlap_seconds=1,workers=2,asr_provider='qwen_asr',translation_provider='google',
            budget_ledger=self.campaign/'budget.json')
        self.settings=SimpleNamespace(asr_endpoint='https://dashscope.aliyuncs.com/api/v1',
            asr_key='fake-key-never-transmitted',asr_input_rate=0.8,asr_output_rate=2.7)
        self.ledger=BudgetLedger(self.config.budget_ledger)
        self.sent=[]
        self.conversion_evidence=[]
        self.reservation=0.25
        self.budget_rejected=None
        self.payload=lambda index:{'utterances':[{'start_ms':1500,'end_ms':2500,'text':f'utterance {index}'}]}

    def fake_media(self,args,log,stop,**kwargs):
        if args[0]!='ffmpeg':
            raise AssertionError('Unexpected external command')
        destination=Path(args[-1])
        if destination.suffix!='.wav':
            raise AssertionError('Only synthetic WAV preparation is allowed')
        destination.parent.mkdir(parents=True,exist_ok=True)
        start=round(float(args[args.index('-ss')+1])*1000) if '-ss' in args else 0
        duration=round(float(args[args.index('-t')+1])*1000) if '-t' in args else self.duration
        destination.write_text(json.dumps({'start_ms':start,'duration_ms':duration}),encoding='utf-8')

    def fake_transcribe(self,audio_path,*,endpoint,key,model,ledger,request_id,raw_path,
                        input_rate_cny_per_million,output_rate_cny_per_million,stop_event=None,
                        allow_empty_draft=False,language='ja',before_submit=None):
        index=int(Path(audio_path).parent.name)-1
        canonical=ledger.path.parent/'responses'/'qwen'/(request_id+'.json')
        def send():
            self.sent.append(request_id)
            if self.budget_rejected is not None and not self.budget_rejected.wait(3):
                raise AssertionError('Concurrent reservation did not reach the ledger')
            return HttpResponse(200,{},json.dumps(self.payload(index)).encode('utf-8'))
        try:
            payload=ledger.execute(request_id,'qwen_asr',self.reservation,canonical,send,stop_event,
                **({'before_submit':before_submit} if before_submit is not None else {}))
        except BudgetExceeded:
            if self.budget_rejected is not None:
                self.budget_rejected.set()
            raise
        raw_path=Path(raw_path)
        raw_path.write_bytes(canonical.read_bytes())
        self.conversion_evidence.append(canonical.is_file() and json.loads(canonical.read_bytes())==payload)
        if not isinstance(payload.get('utterances'),list):
            raise ValueError('Paid result is missing timestamped utterances')
        cues=[Cue(item['start_ms'],item['end_ms'],item['text']) for item in payload['utterances']]
        unconfirmed_empty=payload.get('empty_recognition_requires_review') is True
        metadata={'model':model,'confirmed_silence':not cues and not unconfirmed_empty}
        issues=[]
        if unconfirmed_empty:
            if not allow_empty_draft:
                raise ValueError('Unconfirmed empty response is only allowed in draft mode')
            metadata['empty_recognition_requires_review']=True
            issues=[{'start_ms':0,'end_ms':self.duration,'reason':'No words detected; review the original audio'}]
        return SimpleNamespace(cues=cues,issues=issues,metadata=metadata)

    @contextmanager
    def services(self):
        adapter=ModuleType('subtitle_pipeline.qwen_asr')
        adapter.transcribe=self.fake_transcribe
        with patch.dict('sys.modules',{'subtitle_pipeline.qwen_asr':adapter}), \
             patch.object(runner,'cloud_context',return_value=(self.settings,self.ledger)), \
             patch.object(runner,'probe_media',side_effect=lambda _,**kw:self.duration), \
             patch.object(runner,'detect_silences',return_value=[]), \
             patch('subtitle_pipeline.audio_range.check_pcm_plan',return_value={'synthetic_fixture':True}), \
             patch.object(runner,'run_process',side_effect=self.fake_media), \
             patch.object(runner,'translate_texts',side_effect=lambda texts,*a,**kw:['translation '+text for text in texts]), \
             patch.object(workflow,'emit'):
            yield

    def read_state(self):
        return json.loads((self.config.project/'state.json').read_text(encoding='utf-8'))

    def write_state(self,state):
        runner.atomic_json(self.config.project/'state.json',state)

    def manifest(self,**extra):
        value={'source':{'path':str(self.source),'sha256':sha256(self.source)},
               'duration_ms':self.duration,'status':'samples_ready','samples':[]}
        value.update(extra)
        runner.atomic_json(self.campaign/'campaign.json',value)
        return value

    def test_raw_result_precedes_conversion_failure_and_resume_never_repays(self):
        self.duration=5000
        self.payload=lambda _:{'recognition_failed_to_supply_timing':True}
        with self.services():
            for _ in range(2):
                state=runner.run_pipeline(self.config)
                self.assertEqual(state['status'],'asr_incomplete')
                self.assertFalse(workflow.state_is_complete(self.config.project))
                self.assertTrue((self.config.project/'片段'/'0001'/'asr-response.json').is_file())
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.conversion_evidence,[True,True])
        self.assertEqual(self.ledger.summary()['spent_cny'],0.25)

    def test_qwen_language_changes_are_distinct_paid_identities_with_cached_resume(self):
        self.duration=5000
        identities=[]
        for language,target in [('ja','en'),('en','ja'),('zh','en')]:
            config=replace(self.config,project=self.campaign/language,language=language,target=target)
            with self.services():
                state=runner.run_pipeline(config)
                self.assertEqual(state['status'],'complete')
                self.assertEqual(state['parts']['0']['asr_evidence']['language'],language)
                runner.run_pipeline(config)
            metadata=json.loads((config.project/'片段'/'0001'/'recognition-metadata.json').read_text(encoding='utf-8'))
            identities.append(metadata['request_id'])
        self.assertEqual(len(set(identities)),3)
        self.assertEqual(len(self.sent),3)

    def test_sample_approval_evidence_requires_exact_language_pair(self):
        from subtitle_pipeline.integrity import sample_state_evidence
        self.duration=5000
        config=replace(self.config,language='en',target='ja')
        with self.services(): runner.run_pipeline(config)
        self.assertTrue(sample_state_evidence(config.project,language='en',target='ja'))
        for language,target in [('ja','ja'),('en','zh-CN')]:
            with self.subTest(language=language,target=target),self.assertRaises(ValueError):
                sample_state_evidence(config.project,language=language,target=target)

    def test_missing_part_record_recovers_from_paid_raw_without_new_request(self):
        with self.services():
            state=runner.run_pipeline(self.config)
            self.assertTrue(workflow.state_is_complete(self.config.project))
            state['parts'].pop('1')
            self.write_state(state)
            self.assertFalse(workflow.state_is_complete(self.config.project))
            resumed=runner.run_pipeline(self.config)
        self.assertEqual(resumed['status'],'complete')
        self.assertEqual(len(self.sent),3)
        self.assertEqual(self.ledger.summary()['spent_cny'],0.75)

    def test_cached_resume_skips_missing_or_corrupt_timeline_when_no_asr_is_pending(self):
        for translated in (True,False):
            for timeline_state in ('missing','corrupt'):
                with self.subTest(translated=translated,timeline=timeline_state):
                    config=replace(self.config,project=self.campaign/f'cached-{translated}-{timeline_state}',
                                   translate=translated)
                    with self.services():
                        self.assertEqual(runner.run_pipeline(config)['status'],'complete')
                    timeline=config.project/'timeline.wav'
                    if timeline_state=='missing':timeline.unlink()
                    else:timeline.write_bytes(b'corrupt derived audio')
                    paid_before=len(self.sent)
                    config=replace(config,translate=True)
                    with self.services(), \
                         patch.object(runner,'run_process',side_effect=AssertionError('cached ASR must not decode media')), \
                         patch.object(runner,'recognize_chunk',side_effect=AssertionError('cached ASR must not repeat')):
                        resumed=runner.run_pipeline(config)
                    self.assertEqual(resumed['status'],'complete')
                    self.assertEqual((resumed['recognized'],resumed['translated']),(3,3))
                    self.assertEqual(len(self.sent),paid_before)
                    if timeline_state=='missing':self.assertFalse(timeline.exists())
                    else:self.assertEqual(timeline.read_bytes(),b'corrupt derived audio')

    def test_cache_needing_review_does_not_prepare_irrelevant_timeline(self):
        with self.services():runner.run_pipeline(self.config)
        (self.config.project/'timeline.wav').unlink()
        (self.config.project/'片段'/'0001'/'asr-response.json').write_text('{"changed":true}',encoding='utf-8')
        paid_before=len(self.sent)
        with self.services(), \
             patch.object(runner,'run_process',side_effect=AssertionError('review conflict must not decode media')), \
             patch.object(runner,'recognize_chunk',side_effect=AssertionError('review conflict must not repeat ASR')):
            resumed=runner.run_pipeline(self.config)
        self.assertEqual(resumed['status'],'asr_incomplete')
        self.assertEqual(resumed['parts']['0']['asr'],'needs_review')
        self.assertEqual(resumed['review_status'],'needs_review')
        self.assertEqual(len(self.sent),paid_before)

    def test_missing_asr_prepares_timeline_once_after_reconciled_checkpoint(self):
        with self.services():state=runner.run_pipeline(self.config)
        state['parts'].pop('1')
        self.write_state(state)
        (self.config.project/'timeline.wav').unlink()
        outputs=[]
        def media(args,log,stop,**kwargs):
            name=Path(args[-1]).name
            outputs.append(name)
            checkpoint=self.read_state()
            if name=='timeline.partial.wav':
                self.assertEqual(checkpoint['recognized'],2)
                self.assertEqual(checkpoint['parts'].get('1'),{})
            elif name=='audio.wav':
                self.assertEqual(checkpoint['timeline_hash'],sha256(self.config.project/'timeline.wav'))
            return self.fake_media(args,log,stop,**kwargs)
        with self.services(),patch.object(runner,'run_process',side_effect=media):
            resumed=runner.run_pipeline(self.config)
        self.assertEqual(resumed['status'],'complete')
        self.assertEqual(outputs,['timeline.partial.wav','audio.wav'])
        self.assertEqual(len(self.sent),3)

    def test_lazy_audio_preparation_failure_or_cancellation_remains_resumable(self):
        for failure in (RuntimeError,runner.Cancelled):
            with self.subTest(failure=failure.__name__):
                config=replace(self.config,project=self.campaign/f'interrupted-{failure.__name__}')
                def interrupted(args,log,stop,**kwargs):
                    self.assertEqual(Path(args[-1]).name,'timeline.partial.wav')
                    self.fake_media(args,log,stop,**kwargs)
                    raise failure('synthetic extraction interruption')
                paid_before=len(self.sent)
                with self.services(),patch.object(runner,'run_process',side_effect=interrupted):
                    if failure is runner.Cancelled:
                        state=runner.run_pipeline(config)
                        self.assertEqual(state['status'],'cancelled')
                    else:
                        with self.assertRaisesRegex(RuntimeError,'synthetic extraction interruption'):
                            runner.run_pipeline(config)
                self.assertEqual(len(self.sent),paid_before)
                checkpoint=json.loads((config.project/'state.json').read_text(encoding='utf-8'))
                self.assertNotEqual(checkpoint['status'],'complete')
                self.assertEqual(checkpoint['recognized'],0)
                self.assertFalse((config.project/'run.lock').exists())
                self.assertTrue((config.project/'timeline.partial.wav').exists())
                with self.services():
                    self.assertEqual(runner.run_pipeline(config)['status'],'complete')

    def test_already_cancelled_cloud_run_does_not_prepare_timeline(self):
        stop=threading.Event()
        stop.set()
        with self.services(),patch.object(runner,'run_process',side_effect=AssertionError('stopped task must not decode')):
            state=runner.run_pipeline(self.config,stop)
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual(self.sent,[])
        self.assertFalse((self.config.project/'timeline.wav').exists())

    def test_explicit_long_draft_completes_and_resumes_without_claiming_review(self):
        self.duration=660000
        self.config=replace(self.config,chunk_seconds=120,workflow_stage='draft')
        with self.services():
            state=runner.run_pipeline(self.config)
            self.assertEqual(state['status'],'complete')
            self.assertEqual(state['review_status'],'unreviewed')
            self.assertEqual((state['recognized'],state['translated']),(6,6))
            self.assertEqual(state['config']['workflow_stage'],'draft')
            self.assertEqual(len(parse_srt((self.config.project/'双语草稿.srt').read_text(encoding='utf-8'))),6)
            resumed=runner.run_pipeline(self.config)
            self.assertEqual(resumed['status'],'complete')
            self.assertEqual(resumed['review_status'],'unreviewed')
            for stage in ('sample','full'):
                with self.subTest(stage=stage),self.assertRaises(ValueError):
                    runner.run_pipeline(replace(self.config,workflow_stage=stage))
        self.assertEqual(len(self.sent),6)
        self.assertEqual(self.ledger.summary()['spent_cny'],1.5)
        self.assertFalse((self.campaign/'approval.json').exists())
        self.assertFalse((self.campaign/'final-review.json').exists())

    def test_draft_resume_does_not_inherit_an_approved_review_label(self):
        self.config=replace(self.config,workflow_stage='draft')
        with self.services():
            state=runner.run_pipeline(self.config)
            for previous,expected in (('reviewed','unreviewed'),('needs_review','needs_review')):
                with self.subTest(previous=previous):
                    state['review_status']=previous
                    self.write_state(state)
                    resumed=runner.run_pipeline(self.config)
                    self.assertEqual(resumed['review_status'],expected)
                    self.assertEqual(self.read_state()['review_status'],expected)
        self.assertEqual(len(self.sent),3)

    def test_default_sample_cannot_generate_long_media(self):
        self.duration=660000
        self.config=replace(self.config,chunk_seconds=120)
        with self.services(),self.assertRaisesRegex(ValueError,'10分钟'):
            runner.run_pipeline(self.config)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.ledger.summary()['committed_cny'],0)

    def test_full_stage_still_requires_approval_before_starting_work(self):
        self.config=replace(self.config,workflow_stage='full')
        with self.services(),self.assertRaisesRegex(ValueError,'人工样片验收'):
            runner.run_pipeline(self.config)
        self.assertEqual(self.sent,[])
        self.assertFalse((self.config.project/'state.json').exists())

    def test_unconfirmed_empty_draft_cache_cannot_be_reused_as_sample(self):
        self.duration=5000
        self.config=replace(self.config,workflow_stage='draft')
        self.payload=lambda _:{'utterances':[],'empty_recognition_requires_review':True}
        with self.services():
            state=runner.run_pipeline(self.config)
            self.assertEqual(state['status'],'complete')
            self.assertEqual(state['review_status'],'unreviewed')
            self.assertIs(state['parts']['0'].get('empty_recognition_requires_review'),True)
            self.assertEqual(self.read_state()['parts']['0']['empty_recognition_requires_review'],True)
            issues=json.loads((self.config.project/'需复核.json').read_text(encoding='utf-8'))
            self.assertEqual([(issue['start_ms'],issue['end_ms']) for issue in issues],[(0,5000)])
            before={path:path.read_bytes() for path in self.campaign.rglob('*') if path.is_file()}
            with self.assertRaisesRegex(ValueError,'草稿'):
                runner.run_pipeline(replace(self.config,workflow_stage='sample'))
            self.assertEqual({path:path.read_bytes() for path in self.campaign.rglob('*') if path.is_file()},before)
            resumed=runner.run_pipeline(self.config)
            self.assertEqual(resumed['status'],'complete')
            self.assertEqual(resumed['review_status'],'unreviewed')
            self.assertIs(resumed['parts']['0']['empty_recognition_requires_review'],True)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.ledger.summary()['spent_cny'],.25)

    def test_missing_chunk_plan_range_cannot_be_reported_complete(self):
        with self.services():
            state=runner.run_pipeline(self.config)
            state['chunks'].pop()
            self.write_state(state)
            self.assertFalse(workflow.state_is_complete(self.config.project))
            try:
                resumed=runner.run_pipeline(self.config)
            except ValueError:
                pass
            else:
                self.assertNotEqual(resumed['status'],'complete')
        self.assertEqual(len(self.sent),3)

    def test_corrupt_part_raw_is_blocked_across_restarts_without_new_payment(self):
        with self.services():
            runner.run_pipeline(self.config)
            raw=self.config.project/'片段'/'0002'/'asr-response.json'
            raw.write_bytes(b'{corrupt original response')
            self.assertFalse(workflow.state_is_complete(self.config.project))
            for _ in range(2):
                state=runner.run_pipeline(self.config)
                self.assertEqual(state['status'],'asr_incomplete')
                self.assertEqual(state['parts']['1']['asr'],'needs_review')
                self.assertEqual(raw.read_bytes(),b'{corrupt original response')
        self.assertEqual(len(self.sent),3)
        self.assertEqual(self.ledger.summary()['spent_cny'],0.75)

    def test_explicit_silence_has_evidence_but_cannot_pass_twenty_line_review(self):
        self.duration=5000
        self.payload=lambda _:{'utterances':[]}
        self.config=replace(self.config,project=self.campaign/'samples'/'one'/'识别任务')
        with self.services():
            state=runner.run_pipeline(self.config)
            self.assertEqual(state['status'],'complete')
            self.assertNotIn('empty_recognition_requires_review',state['parts']['0'])
            self.assertTrue(workflow.state_is_complete(self.config.project))
            self.assertEqual(parse_srt((self.config.project/'原文.srt').read_text(encoding='utf-8')),[])
            self.manifest(samples=[{'name':'silent fixture','folder':'samples/one','start_sec':0,'end_sec':5}])
            with self.assertRaises(ValueError):
                workflow.approve(self.campaign,20,18,True)
        self.assertFalse((self.campaign/'approval.json').exists())

    def test_previous_provider_project_is_not_reused(self):
        local_config=replace(self.config,asr_provider='whisper_cpp')
        with self.services(), patch.object(runner,'recognize_chunk',return_value=[Cue(1500,2500,'old provider')]):
            self.assertEqual(runner.run_pipeline(local_config)['status'],'complete')
        with self.services(), self.assertRaises(ValueError):
            runner.run_pipeline(self.config)
        self.assertEqual(self.sent,[])

    def test_old_provider_evidence_cannot_satisfy_qwen_completion(self):
        with self.services():
            state=runner.run_pipeline(self.config)
        state['parts']['0']['asr_evidence']['provider']='whisper_cpp'
        self.write_state(state)
        self.assertFalse(workflow.state_is_complete(self.config.project))

    def prepare_review(self):
        self.duration=5000
        folder=self.campaign/'samples'/'one'
        folder.mkdir(parents=True)
        audio=folder/'input.wav'
        audio.write_bytes(b'synthetic sample audio')
        self.config=replace(self.config,source=audio,project=folder/'识别任务')
        self.payload=lambda _:{'utterances':[{'start_ms':i*200,'end_ms':i*200+100,'text':f'line {i}'} for i in range(20)]}
        with self.services():
            runner.run_pipeline(self.config)
            (folder/'preview.mp4').write_bytes(b'synthetic preview')
            (folder/'旧日语.srt').write_text('',encoding='utf-8')
            preview_hash=sha256(folder/'preview.mp4')
            self.manifest(samples=[{'name':'review fixture','folder':'samples/one','start_sec':0,
                'end_sec':5,'audio_hash':sha256(audio),'preview_hash':preview_hash,
                'preview_validation':{'version':2,'sha256':preview_hash,
                                      'expected_duration_ms':5000,'video_frames':120}}])
            workflow.approve(self.campaign,20,18,True)
        path=self.campaign/'approval.json'
        return replace(self.config,source=self.source,project=self.campaign/'整片',workflow_stage='full',approval_path=path)

    def test_review_gate_binds_source_provider_model_and_api(self):
        config=self.prepare_review()
        runner.validate_full_approval(config,sha256(self.source))
        for changes in ({'asr_provider':'whisper_cpp'},{'asr_model':'other-model'},
                        {'asr_api_version':'other-api'},{'translation_model':'other-translator'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runner.validate_full_approval(replace(config,**changes),sha256(self.source))
        with self.assertRaises(ValueError):
            runner.validate_full_approval(config,'different-source')
        (self.config.project/'中文草稿.srt').write_text('edited after review',encoding='utf-8')
        with self.assertRaises(ValueError):
            runner.validate_full_approval(config,sha256(self.source))

    def test_approval_cannot_hide_a_sample_from_another_provider_model_api_or_source(self):
        config=self.prepare_review()
        original=self.read_state()
        approval=json.loads(config.approval_path.read_text(encoding='utf-8'))
        for field,value in (('engine','whisper_cpp'),('asr_model','old-model'),('api','old-api'),('source_hash','wrong-source')):
            with self.subTest(field=field):
                state=json.loads(json.dumps(original))
                if field=='source_hash':
                    state['identity']['source']['sha256']=value
                else:
                    state['identity'][field]=value
                self.write_state(state)
                forged=json.loads(json.dumps(approval))
                for sample in forged.get('sample_states',[]):
                    sample['sha256']=sha256(self.config.project/'state.json')
                    if field=='source_hash':
                        sample['source_sha256']=value
                runner.atomic_json(config.approval_path,forged)
                with self.assertRaises(ValueError):
                    runner.validate_full_approval(config,sha256(self.source))

    def test_two_workers_cannot_reserve_past_shared_stop_line(self):
        self.duration=15000
        self.reservation=10
        self.budget_rejected=threading.Event()
        with self.services():
            state=runner.run_pipeline(self.config)
        self.assertTrue(self.budget_rejected.is_set())
        self.assertEqual(len(self.sent),1)
        self.assertEqual(state['recognized'],1)
        self.assertEqual(state['status'],'asr_incomplete')
        self.assertEqual(self.ledger.summary()['committed_cny'],10)
        self.assertLess(self.ledger.summary()['committed_cny'],18)

    def test_export_uses_preserved_public_human_translation(self):
        self.duration=5000
        self.config=replace(self.config,project=self.campaign/'整片')
        with self.services():
            runner.run_pipeline(self.config)
            public=self.config.project/'中文草稿.srt'
            public.write_text(public.read_text(encoding='utf-8').replace('translation utterance 0','human revised text'),encoding='utf-8')
            state=runner.run_pipeline(self.config)
            self.assertEqual(state['outputs']['中文草稿.srt'],str(Path('自动更新')/'中文草稿.srt'))
            manifest=self.manifest(status='final_reviewed')
            review={'status':'sampled_approved','content_passed':True,
                    'source_sha256':sha256(self.source),
                    'artifacts':workflow.artifacts_for(self.campaign,manifest,full=True)}
            runner.atomic_json(self.campaign/'final-review.json',review)
            info={'streams':[{'codec_type':'video','width':640,'height':480,'r_frame_rate':'25/1','nb_frames':'125'}],
                  'format':{'duration':'5'}}
            def encode(args,log,stop,**kwargs):
                destination=Path(args[-1])
                if destination.suffix=='.ass':
                    destination.write_text('PlayResX: 384\nPlayResY: 288\nStyle: Default,placeholder\n',encoding='utf-8')
                elif destination.suffix=='.mp4':
                    destination.write_bytes(b'encoded:'+(self.campaign/'导出'/'captions.srt').read_bytes())
                elif str(destination)!='-':
                    raise AssertionError('Unexpected export command')
            with patch.object(workflow,'media_info',return_value=info), \
                 patch.object(workflow,'audio_digest',return_value='same-audio'), \
                 patch.object(runner,'run_process',side_effect=encode):
                workflow.export_video(self.campaign,threading.Event())
        output=self.source.with_name('source_中文字幕_修订版.mp4')
        self.assertIn(b'human revised text',output.read_bytes())
        self.assertNotIn(b'translation utterance 0',output.read_bytes())
        self.assertEqual(len(self.sent),1)


if __name__=='__main__':
    unittest.main()
