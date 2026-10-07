import importlib.util
from contextlib import chdir
import unittest
import tempfile
import json
import errno
import os
from pathlib import Path
import threading
from unittest.mock import Mock, patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt


def write_complete_qwen_evidence(project, source, duration_ms, *, language='ja', target='zh-CN', state=None):
    """Bind synthetic public tracks to real, offline-verifiable part artifacts."""
    part_folder = project / '片段' / '0001'
    part_folder.mkdir(parents=True, exist_ok=True)
    for public_name, part_name in [('原文.srt', 'source.local.srt'),
                                   (workflow.target_filename(target), 'target.local.srt')]:
        (part_folder / part_name).write_bytes((project / public_name).read_bytes())
    (part_folder / 'asr-response.json').write_bytes(b'{}')
    input_hash = workflow.sha256(source)
    source_hash = workflow.sha256(part_folder / 'source.local.srt')
    complete = dict(state or {})
    complete.update({
        'status': 'complete', 'duration_ms': duration_ms,
        'identity': {'engine': 'qwen_asr', 'api': 'dashscope-v1',
                     'asr_model': 'qwen-audio-3.1-asr-flash', 'translation_model': 'deepseek-flash',
                     'source': {'path': str(source.resolve()), 'sha256': input_hash},
                     'language': language, 'target': target},
        'chunks': [{'index': 0, 'core_start_ms': 0, 'core_end_ms': duration_ms,
                    'audio_start_ms': 0, 'audio_end_ms': duration_ms}],
        'parts': {'0': {
            'asr': 'done', 'translation': 'done',
            'asr_evidence': {'provider': 'qwen_asr', 'model': 'qwen-audio-3.1-asr-flash',
                             'api': 'dashscope-v1', 'language': language,
                             'source_sha256': input_hash, 'audio_range_ms': [0, duration_ms]},
            'source_hash': source_hash, 'translation_source_hash': source_hash,
            'target_hash': workflow.sha256(part_folder / 'target.local.srt'),
            'raw_response_hash': workflow.sha256(part_folder / 'asr-response.json'),
        }},
    })
    workflow.r.atomic_json(project / 'state.json', complete)
    return complete


class CloudWorkflowTests(unittest.TestCase):
    def test_workflow_entrypoint_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.cloud_workflow'))

    def test_sample_windows_are_five_minutes_and_fixed(self):
        from subtitle_pipeline.cloud_workflow import WINDOWS
        self.assertEqual(WINDOWS,((30,120),(1680,1800),(5400,5490)))
        self.assertEqual(sum(b-a for a,b in WINDOWS),300)

    def test_approval_requires_ready_content_and_twenty_reviewed_lines(self):
        from subtitle_pipeline.cloud_workflow import validate_review
        for values in [(False,20,20,True),(True,19,19,True),(True,20,17,True),(True,20,18,False)]:
            with self.assertRaises(ValueError):validate_review(*values)
        validate_review(True,20,18,True)

    def test_review_html_escapes_subtitles_and_has_no_remote_dependency(self):
        from subtitle_pipeline.cloud_workflow import render_review
        from subtitle_pipeline.subtitles import Cue
        html=render_review([{'name':'片段','video':'preview.mp4','start':30,
              'old':[Cue(0,1000,'</script><script>bad()</script>')],
              'new':[],'zh':[]}])
        self.assertNotIn('<script>bad()',html)
        self.assertNotIn('https://',html)
        self.assertIn('旧日语',html)

    def test_review_labels_follow_source_and_target_languages(self):
        sample={'name':'sample','video':'preview.mp4','start':0,'old':[],'new':[],'zh':[]}
        for language,target,source_label,target_label in [('zh','en','中文','英语'),('en','ja','英语','日语')]:
            with self.subTest(language=language,target=target):
                content=workflow.render_review([sample],language=language,target=target)
                self.assertIn(f'千问 新{source_label}',content)
                self.assertIn(f'DeepSeek {target_label}',content)
                self.assertNotIn('日语字幕样片对照',content)


class SamplePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'synthetic-video')
        self.stop = threading.Event()
        self.addCleanup(patch.stopall)
        patch.object(workflow, 'emit').start()
        patch('socket.create_connection', side_effect=AssertionError('Offline test')).start()
        patch('socket.socket.connect', side_effect=AssertionError('Offline test')).start()

    def prepare_media(self, duration_ms, *, baseline=None, media_kind='video', budget_ledger=None,
                      language='ja',target=None):
        campaign = self.root / str(duration_ms)
        campaign.mkdir()
        commands = []
        def encode(args, log, stop):
            commands.append(args)
            Path(args[-1]).write_bytes(b'synthetic-media')
        def checked_preview(path,seconds,stop,**kwargs):
            return {'version':2,'sha256':workflow.sha256(path),
                    'expected_duration_ms':round(seconds*1000),'video_frames':round(seconds*24)}
        with patch.object(workflow.r, 'probe_media', return_value=duration_ms), \
                patch.object(workflow, 'media_info', return_value={'streams':
                    [{'codec_type':'audio'}] + ([{'codec_type':'video'}] if media_kind=='video' else [])}), \
                patch.object(workflow.r, 'run_process', side_effect=encode), \
                patch.object(workflow,'_checked_preview',side_effect=checked_preview):
            options={'budget_ledger':budget_ledger} if budget_ledger is not None else {}
            manifest = workflow.prepare(self.source, campaign, baseline, self.stop,
                                        language=language,target=target,**options)
        return campaign, manifest, commands

    def test_prepare_persists_language_pair_and_uses_target_file_in_review(self):
        for i,(language,target) in enumerate([('zh','en'),('en','ja'),('ja','en')]):
            with self.subTest(language=language,target=target):
                campaign,manifest,_=self.prepare_media(120000+i,language=language,target=target)
                self.assertEqual((manifest['language'],manifest['target']),(language,target))
                config=workflow.config_for(self.source,campaign/'job',campaign,full=True)
                self.assertEqual((config.language,config.target),(language,target))
                folder=campaign/manifest['samples'][0]['folder']
                self.assertTrue((folder/('旧日语.srt' if language=='ja' else '旧原文.srt')).exists())
                job=folder/'识别任务';job.mkdir()
                (job/workflow.target_filename(target)).write_text(render_srt([Cue(0,1000,'selected translation')]),encoding='utf-8')
                workflow.build_review(campaign,manifest)
                self.assertIn('selected translation',(campaign/'review.html').read_text(encoding='utf-8'))

    def test_chinese_source_defaults_to_english_and_resume_cannot_change_pair(self):
        campaign,manifest,_=self.prepare_media(120000,language='zh')
        self.assertEqual(manifest['target'],'en')
        before=(campaign/'campaign.json').read_bytes()
        with patch.object(workflow.r,'run_process',side_effect=AssertionError('No re-encoding')):
            workflow.prepare(self.source,campaign,None,self.stop,language='zh',target='en')
            for language,target in [('en','en'),('zh','ja'),('ja','zh-CN')]:
                with self.subTest(language=language,target=target),self.assertRaisesRegex(ValueError,'语言'):
                    workflow.prepare(self.source,campaign,None,self.stop,language=language,target=target)
        self.assertEqual((campaign/'campaign.json').read_bytes(),before)

    def test_legacy_manifest_without_languages_remains_japanese_to_chinese(self):
        campaign,manifest,_=self.prepare_media(120000)
        manifest.pop('language');manifest.pop('target')
        workflow.r.atomic_json(campaign/'campaign.json',manifest)
        before=(campaign/'campaign.json').read_bytes()
        config=workflow.config_for(self.source,campaign/'job',campaign)
        self.assertEqual((config.language,config.target),('ja','zh-CN'))
        workflow.prepare(self.source,campaign,None,self.stop)
        with self.assertRaisesRegex(ValueError,'语言'):
            workflow.prepare(self.source,campaign,None,self.stop,language='en',target='zh-CN')
        self.assertEqual((campaign/'campaign.json').read_bytes(),before)

    def test_two_campaigns_share_spending_reservations_and_stop_line(self):
        from subtitle_pipeline.cloud_budget import BudgetLedger,BudgetExceeded,HttpResponse,SubmissionUnknown
        shared=self.root/'shared-budget'/'fees.json'
        campaigns=[]
        for duration in (120000,120001):
            with chdir(self.root):
                campaign,manifest,_=self.prepare_media(duration,budget_ledger=Path('shared-budget')/'fees.json')
            self.assertEqual(manifest['budget_ledger'],str(shared.resolve()))
            self.assertEqual((manifest['budget_cny'],manifest['stop_cny']),(20,18))
            config=workflow.config_for(self.source,campaign/'job',campaign)
            self.assertEqual(config.budget_ledger,shared.resolve())
            self.assertEqual(config.budget_cny,20)
            campaigns.append(campaign)
        first,second=campaigns
        first_ledger=BudgetLedger(workflow.config_for(self.source,first/'job',first).budget_ledger)
        second_ledger=BudgetLedger(workflow.config_for(self.source,second/'job',second,full=True).budget_ledger)
        first_ledger.execute('first-paid','test',6,first/'response.json',
                             lambda:HttpResponse(200,{},b'{"ok":true}'))
        def interrupted():
            raise TimeoutError('synthetic interrupted response')
        with self.assertRaises(SubmissionUnknown):
            second_ledger.execute('second-pending','test',11,second/'response.json',interrupted)
        for campaign in campaigns:
            report=workflow.cost_report(campaign)
            self.assertEqual((report['spent_cny'],report['reserved_cny'],report['committed_cny']),(6,11,17))
            self.assertEqual((report['budget_cny'],report['stop_cny']),(20,18))
            self.assertEqual(workflow.read_json(campaign/'费用记录.json'),report)
            self.assertFalse((campaign/'费用账本.json').exists())
        with self.assertRaises(BudgetExceeded):
            first_ledger.execute('blocked','test',1,first/'blocked.json',
                                 lambda:self.fail('shared stop line must prevent transmission'))

    def test_default_campaign_ledger_remains_local_and_independent(self):
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse
        first,manifest,_=self.prepare_media(120000)
        second,_,_=self.prepare_media(120001)
        self.assertNotIn('budget_ledger',manifest)
        local=workflow.config_for(self.source,first/'job',first).budget_ledger
        self.assertEqual(local,first/'费用账本.json')
        BudgetLedger(local).execute('local-paid','test',3,first/'response.json',
                                    lambda:HttpResponse(200,{},b'{"ok":true}'))
        self.assertEqual(workflow.cost_report(first)['spent_cny'],3)
        self.assertEqual(workflow.cost_report(second)['committed_cny'],0)

    def test_resume_cannot_switch_existing_local_or_shared_ledger(self):
        from subtitle_pipeline.cloud_budget import BudgetLedger,HttpResponse
        for shared in (False,True):
            with self.subTest(shared=shared):
                chosen=self.root/'shared-budget'/'fees.json' if shared else None
                campaign,_,_=self.prepare_media(120000+shared,budget_ledger=chosen)
                ledger=workflow.config_for(self.source,campaign/'job',campaign).budget_ledger
                BudgetLedger(ledger).execute('existing','test',3,campaign/'response.json',
                                            lambda:HttpResponse(200,{},b'{"ok":true}'))
                manifest_before=(campaign/'campaign.json').read_bytes()
                ledger_before=ledger.read_bytes()
                with patch.object(workflow.r,'run_process',side_effect=AssertionError('No media changes on resume')):
                    workflow.prepare(self.source,campaign,None,self.stop)
                    workflow.prepare(self.source,campaign,None,self.stop,budget_ledger=ledger)
                    with self.assertRaisesRegex(ValueError,'账本'):
                        workflow.prepare(self.source,campaign,None,self.stop,budget_ledger=self.root/'replacement.json')
                self.assertEqual((campaign/'campaign.json').read_bytes(),manifest_before)
                self.assertEqual(ledger.read_bytes(),ledger_before)
                self.assertFalse((self.root/'replacement.json').exists())

    def test_prepare_cli_persists_explicit_shared_ledger(self):
        campaign=self.root/'cli-project'
        shared=self.root/'shared-budget'/'fees.json'
        def encode(args,log,stop):
            Path(args[-1]).write_bytes(b'synthetic-media')
        with patch.object(workflow.r,'probe_media',return_value=120000), \
             patch.object(workflow,'media_info',return_value={'streams':[{'codec_type':'audio'}]}), \
             patch.object(workflow.r,'run_process',side_effect=encode),chdir(self.root):
            result=workflow.main(['prepare','--source',str(self.source),'--campaign',str(campaign),
                                  '--budget-ledger',str(Path('shared-budget')/'fees.json')])
        self.assertEqual(result,0)
        self.assertEqual(workflow.read_json(campaign/'campaign.json')['budget_ledger'],str(shared.resolve()))
        self.assertFalse(shared.exists(),'local preparation must not create a spending ledger')

    def test_prepare_cli_propagates_selected_source_and_translation_languages(self):
        campaign=self.root/'cli-multilingual'
        def encode(args,log,stop):
            Path(args[-1]).write_bytes(b'synthetic-media')
        with patch.object(workflow.r,'probe_media',return_value=120000), \
             patch.object(workflow,'media_info',return_value={'streams':[{'codec_type':'audio'}]}), \
             patch.object(workflow.r,'run_process',side_effect=encode):
            result=workflow.main(['prepare','--source',str(self.source),'--campaign',str(campaign),
                                  '--language','zh','--target','ja'])
        self.assertEqual(result,0)
        manifest=workflow.read_json(campaign/'campaign.json')
        self.assertEqual((manifest['language'],manifest['target']),('zh','ja'))
        self.assertFalse((campaign/'费用账本.json').exists())

    def test_audio_source_prepares_playable_wav_review_without_video_encoding(self):
        self.source = self.root / 'source.wav'
        self.source.write_bytes(b'synthetic-wave-source')
        campaign, manifest, commands = self.prepare_media(120000, media_kind='audio')
        self.assertEqual(manifest['source_kind'], 'audio')
        sample = campaign / '样片/01'
        self.assertTrue((sample / 'input.wav').is_file())
        self.assertEqual(manifest['samples'][0]['audio_hash'], workflow.sha256(sample / 'input.wav'))
        self.assertFalse((sample / 'preview.mp4').exists())
        self.assertFalse(any('0:v:0' in command or '-c:v' in command for command in commands))
        self.assertEqual(manifest['status'], 'prepared')
        review = (campaign / 'review.html').read_text(encoding='utf-8')
        from html.parser import HTMLParser
        elements = []
        class MediaParser(HTMLParser):
            def handle_starttag(self, tag, attributes):
                if tag in ('audio','video'):
                    elements.append((tag,dict(attributes)))
        MediaParser().feed(review)
        self.assertEqual([(tag,attrs['src']) for tag,attrs in elements], [('audio','样片/01/input.wav')])
        self.assertFalse((campaign / 'approval.json').exists())

    def test_probe_video_track_wins_over_audio_filename_extension(self):
        self.source = self.root / 'misnamed.wav'
        self.source.write_bytes(b'video-with-an-audio-extension')
        campaign, manifest, commands = self.prepare_media(120000)
        self.assertEqual(manifest['source_kind'], 'video')
        self.assertTrue((campaign / '样片/01/preview.mp4').is_file())
        self.assertTrue(any('0:v:0' in command for command in commands))

    def test_prepare_accepts_two_minute_video_without_baseline_as_one_draft_sample(self):
        campaign, manifest, commands = self.prepare_media(120000)
        self.assertEqual([(s['start_sec'], s['end_sec']) for s in manifest['samples']], [(0, 120)])
        self.assertEqual(manifest['status'], 'prepared')
        self.assertIsNone(manifest['baseline'])
        self.assertEqual((manifest['budget_cny'], manifest['stop_cny']), (20, 18))
        self.assertEqual((campaign / '样片/01/旧日语.srt').read_text(encoding='utf-8'), '')
        self.assertTrue((campaign / '样片/01/preview.mp4').is_file())
        self.assertTrue((campaign / 'review.html').is_file())
        self.assertFalse((campaign / 'approval.json').exists())
        self.assertEqual([float(args[args.index('-t') + 1]) for args in commands if '-t' in args], [120, 120])

    def test_preparation_windows_cover_length_boundaries_without_overlap_or_overrun(self):
        cases = [
            (300000, [(0, 300000)]),
            (300001, [(0, 90000), (90000, 210000), (210001, 300001)]),
            (301003, [(0, 90000), (90501, 210501), (211003, 301003)]),
            (5000000, [(0, 90000), (2440000, 2560000), (4910000, 5000000)]),
            (5489999, [(0, 90000), (2684999, 2804999), (5399999, 5489999)]),
            (5490000, [(30000, 120000), (1680000, 1800000), (5400000, 5490000)]),
            (7200000, [(30000, 120000), (1680000, 1800000), (5400000, 5490000)]),
        ]
        for duration_ms, expected in cases:
            with self.subTest(duration_ms=duration_ms):
                _, manifest, _ = self.prepare_media(duration_ms)
                ranges = [(round(s['start_sec'] * 1000), round(s['end_sec'] * 1000))
                          for s in manifest['samples']]
                self.assertEqual(ranges, expected)
                self.assertEqual(sum(end - start for start, end in ranges), min(duration_ms, 300000))
                self.assertTrue(all(0 <= start < end <= duration_ms for start, end in ranges))
                self.assertTrue(all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:])))

    def test_fractional_sample_boundary_clips_baseline_in_integer_milliseconds(self):
        baseline = self.root / 'baseline.srt'
        baseline.write_text(render_srt([Cue(90500, 91000, 'reference')]), encoding='utf-8')
        campaign, _, _ = self.prepare_media(301003, baseline=baseline)
        cues = parse_srt((campaign / '样片/02/旧日语.srt').read_text(encoding='utf-8'))
        self.assertEqual(cues, [Cue(0, 499, 'reference')])

    def test_resume_preserves_existing_plan_manifest_and_cached_outputs(self):
        campaign, manifest, _ = self.prepare_media(7200000)
        sample = manifest['samples'][0]
        sample['start_sec'], sample['end_sec'] = 12, 102
        manifest['status'] = 'samples_ready'
        workflow.write_manifest(campaign, manifest)
        sample_dir = campaign / sample['folder']
        (sample_dir / '旧日语.srt').write_text(render_srt([Cue(0, 1000, 'manual reference')]), encoding='utf-8')
        cached = sample_dir / '识别任务' / 'state.json'
        cached.parent.mkdir()
        cached.write_text('{"review_status":"unreviewed","existing":true}', encoding='utf-8')
        files = [campaign / 'campaign.json', cached, sample_dir / '旧日语.srt',
                 sample_dir / 'input.wav', sample_dir / 'preview.mp4']
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        with patch.object(workflow.r, 'probe_media', side_effect=AssertionError('Do not replan cached project')), \
                patch.object(workflow.r, 'run_process', side_effect=AssertionError('Do not encode cached media')):
            resumed = workflow.prepare(self.source, campaign, None, self.stop)
        self.assertEqual(resumed['samples'][0]['start_sec'], 12)
        self.assertEqual(resumed['status'], 'samples_ready')
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}, before)
        self.assertFalse((campaign / 'approval.json').exists())

    def test_resume_rejects_changed_sample_media_without_overwriting_or_reserving_fees(self):
        for duration_ms, relative in [(7200000, '样片/01/input.wav'),
                                     (7200001, '样片/01/preview.mp4'),
                                     (7200002, '源音频.wav')]:
            with self.subTest(relative=relative):
                campaign, _, _ = self.prepare_media(duration_ms)
                altered = campaign / relative
                altered.write_bytes(b'changed-by-user')
                before = (campaign / 'campaign.json').read_bytes()
                with patch.object(workflow.r, 'run_process', side_effect=AssertionError('Must retain altered media')):
                    with self.assertRaisesRegex(ValueError, '变化'):
                        workflow.prepare(self.source, campaign, None, self.stop)
                self.assertEqual(altered.read_bytes(), b'changed-by-user')
                self.assertEqual((campaign / 'campaign.json').read_bytes(), before)
                self.assertFalse((campaign / '费用账本.json').exists())


class WorkflowRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.campaign = root / 'project'
        self.campaign.mkdir()
        source_dir = root / 'source-volume'
        source_dir.mkdir()
        self.source = source_dir / 'source.mp4'
        self.source.write_bytes(b'original-video-never-modify')
        self.manifest = {'source': {'path': str(self.source), 'sha256': workflow.sha256(self.source)},
                         'duration_ms':100000,'status': 'samples_ready', 'samples': []}
        self.stop = threading.Event()
        self.addCleanup(patch.stopall)
        patch.object(workflow, 'emit').start()
        patch('socket.create_connection',side_effect=AssertionError('Network disabled in workflow tests')).start()
        patch.object(workflow.r,'run_process',side_effect=AssertionError('External commands disabled in workflow tests')).start()

    def save_manifest(self):
        workflow.r.atomic_json(self.campaign / 'campaign.json', self.manifest)

    def test_explicit_draft_cli_records_unreviewed_mode_without_fabricating_approval(self):
        self.save_manifest()
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        with patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow.r,'run_pipeline',return_value={}) as run, \
                patch.object(workflow,'state_is_complete',return_value=True), \
                patch.object(workflow.r,'validate_full_approval') as approval:
            result=workflow.main(['draft','--campaign',str(self.campaign)])
        self.assertEqual(result,0)
        self.assertEqual(run.call_args.args[0].workflow_stage,'draft')
        self.assertIsNone(run.call_args.args[0].approval_path)
        approval.assert_not_called()
        saved=workflow.read_json(self.campaign/'campaign.json')
        self.assertEqual(saved['generation_mode'],'unreviewed_draft')
        self.assertEqual(saved['review_status'],'unreviewed')
        self.assertEqual(saved['status'],'full_ready')
        self.assertFalse((self.campaign/'approval.json').exists())
        self.assertFalse((self.campaign/'final-review.json').exists())

    def test_explicit_draft_keeps_budget_preflight(self):
        self.save_manifest()
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        with patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow,'cost_report',return_value={'committed_cny':17,'requests':{}}), \
                patch.object(workflow.r,'run_pipeline') as run:
            with self.assertRaisesRegex(ValueError,'18元'):
                workflow.run_full(self.campaign,self.stop,draft=True)
        run.assert_not_called()

    def test_full_cli_returns_failure_when_generation_is_only_partial(self):
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        for action in ('draft','full'):
            with self.subTest(action=action):
                self.save_manifest()
                with patch.object(workflow,'load_settings',return_value=settings), \
                        patch.object(workflow.r,'run_pipeline',return_value={'status':'translation_incomplete'}), \
                        patch.object(workflow,'state_is_complete',return_value=False), \
                        patch.object(workflow.r,'validate_full_approval'):
                    result=workflow.main([action,'--campaign',str(self.campaign)])
                self.assertEqual(result,2)
                self.assertEqual(workflow.read_json(self.campaign/'campaign.json')['status'],'full_incomplete')

    def test_samples_cli_distinguishes_saved_partial_results_from_success(self):
        self.manifest['samples']=[{'folder':'样片/01','start_sec':0,'end_sec':90,'name':'test','audio_hash':'audio'}]
        self.save_manifest()
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        settings.public_config.return_value={}
        with patch.object(workflow,'verify_source'),patch.object(workflow,'sample_review_media'), \
                patch.object(workflow,'_samples_unchanged',return_value=False), \
                patch.object(workflow,'sha256',return_value='audio'), \
                patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow.r,'run_pipeline',return_value={'status':'asr_incomplete'}), \
                patch.object(workflow,'state_is_complete',return_value=False), \
                patch.object(workflow,'build_review'):
            result=workflow.main(['samples','--campaign',str(self.campaign)])
        self.assertEqual(result,2)
        self.assertEqual(workflow.read_json(self.campaign/'campaign.json')['status'],'samples_incomplete')

    def test_full_estimate_uses_only_this_campaign_sample_translation_requests(self):
        self.manifest.update(duration_ms=15000000,samples=[{'folder':'样片/01','start_sec':0,'end_sec':300}])
        cache=self.campaign/'样片/01/识别任务/deepseek-translation-cache.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'entries':{'a'*64:{}}}),encoding='utf-8')
        self.save_manifest()
        summary={'committed_cny':.5,'requests':{
            'deepseek-'+('a'*64):{'provider':'deepseek','actual_cny':.01},
            'deepseek-other-film':{'provider':'deepseek','actual_cny':1.0}}}
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        with patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow,'cost_report',return_value=summary), \
                patch.object(workflow.r,'run_pipeline',return_value={}) as run, \
                patch.object(workflow,'state_is_complete',return_value=True):
            workflow.run_full(self.campaign,self.stop,draft=True)
        run.assert_called_once()
        estimate=workflow.read_json(self.campaign/'campaign.json')['estimated_total_cny']
        self.assertLess(estimate,4)

    def complete_project(self, project, count, *, language='ja',target='zh-CN'):
        part_folder = project / '片段' / '0001'
        part_folder.mkdir(parents=True)
        duration=100000 if project.name=='整片' else 90000
        source=self.source if project.name=='整片' else project.parent/'input.wav'
        if source!=self.source:
            source.write_bytes(b'synthetic sample audio')
        input_hash=workflow.sha256(source)
        cues = [Cue(i * 2000, i * 2000 + 1000, f'line {i}') for i in range(count)]
        original = render_srt(cues)
        translated = render_srt([Cue(c.start_ms, c.end_ms, f'译文 {i}') for i, c in enumerate(cues)])
        (part_folder / 'source.local.srt').write_text(original, encoding='utf-8')
        (part_folder / 'target.local.srt').write_text(translated, encoding='utf-8')
        (part_folder / 'asr-response.json').write_bytes(b'{}')
        (project / '原文.srt').write_text(original, encoding='utf-8')
        (project / workflow.target_filename(target)).write_text(translated, encoding='utf-8')
        (project / '双语草稿.srt').write_text(translated, encoding='utf-8')
        (project / '需复核.json').write_text('[]', encoding='utf-8')
        source_hash = workflow.sha256(part_folder / 'source.local.srt')
        part = {'asr': 'done', 'translation': 'done', 'asr_evidence': {
                    'provider':'qwen_asr','model':'qwen-audio-3.1-asr-flash','api':'dashscope-v1',
                    'source_sha256':input_hash,'audio_range_ms':[0,duration]},
                'source_hash': source_hash, 'translation_source_hash': source_hash,
                'target_hash': workflow.sha256(part_folder / 'target.local.srt'),
                'raw_response_hash': workflow.sha256(part_folder / 'asr-response.json')}
        state={'status': 'complete','duration_ms':duration,
            'identity':{'engine':'qwen_asr','api':'dashscope-v1','asr_model':'qwen-audio-3.1-asr-flash',
                        'translation_model':'deepseek-flash','source':{'path':str(source.resolve()),'sha256':input_hash}},
            'chunks': [{'index': 0, 'core_start_ms': 0, 'core_end_ms': duration,
                        'audio_start_ms': 0, 'audio_end_ms': duration}], 'parts': {'0': part}}
        if (language,target)!=('ja','zh-CN'):
            state['identity'].update(language=language,target=target)
            part['asr_evidence']['language']=language
        workflow.r.atomic_json(project / 'state.json',state)
        self.assertTrue(workflow.state_is_complete(project,language=language,target=target))

    def test_completion_and_public_validation_cannot_reuse_another_language_pair(self):
        project=self.campaign/'整片'
        self.complete_project(project,20,language='zh',target='en')
        self.manifest.update(language='zh',target='en')
        artifacts=workflow.artifacts_for(self.campaign,self.manifest,full=True)
        self.assertFalse(workflow.state_is_complete(project))
        self.assertFalse(workflow.state_is_complete(project,language='en',target='en'))
        self.assertFalse(workflow.state_is_complete(project,language='zh',target='ja'))
        self.assertEqual(workflow.validate_public_subtitles(project,artifacts,language='zh',target='en')['cue_count'],20)
        with self.assertRaisesRegex(ValueError,'语言'):
            workflow.validate_public_subtitles(project,artifacts)

    def test_hash_valid_punctuation_translation_cannot_mark_lexical_source_complete(self):
        project = self.campaign / '整片'
        self.complete_project(project, 1)
        state_path = project / 'state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        folder = project / '片段' / '0001'
        source = folder / 'source.local.srt'
        target = folder / 'target.local.srt'
        for original, translated, valid in [('が可能です。', '.', False),
                                            ('①', '…', False),
                                            ('が可能です。', '可以做到。', True),
                                            ('♪', '♪', True)]:
            with self.subTest(original=original, translated=translated):
                source.write_text(render_srt([Cue(0, 1000, original)]), encoding='utf-8')
                target.write_text(render_srt([Cue(0, 1000, translated)]), encoding='utf-8')
                part = state['parts']['0']
                part.update(source_hash=workflow.sha256(source),
                            translation_source_hash=workflow.sha256(source),
                            target_hash=workflow.sha256(target))
                workflow.r.atomic_json(state_path, state)
                before = {path: path.read_bytes() for path in (source, target, state_path)}
                self.assertEqual(workflow.state_is_complete(project), valid)
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_pending_output_publication_cannot_be_reported_as_complete(self):
        project=self.campaign/'整片'
        self.complete_project(project,1)
        state_path=project/'state.json'
        state=workflow.read_json(state_path)
        for pending in ({'version':1,'files':[]},{},None):
            with self.subTest(pending=pending):
                state['pending_outputs']=pending
                workflow.r.atomic_json(state_path,state)
                before=state_path.read_bytes()
                self.assertFalse(workflow.state_is_complete(project))
                self.assertEqual(state_path.read_bytes(),before)

    def test_draft_review_flags_cannot_bypass_sample_resume_and_approval(self):
        self.make_samples(20)
        project=self.campaign/'samples/01/识别任务'
        state_path=project/'state.json';state=workflow.read_json(state_path)
        part_folder=project/'片段/0001'
        for public,local,field in [('原文.srt','source.local.srt','public_source_hash'),
                                  ('中文草稿.srt','target.local.srt','public_target_hash')]:
            (part_folder/public).write_bytes((part_folder/local).read_bytes())
            state['parts']['0'][field]=workflow.sha256(part_folder/public)
        state['generated_hashes']={name:workflow.sha256(project/name)
            for name in ('原文.srt','中文草稿.srt','双语草稿.srt')}
        for flag in ('empty_recognition_requires_review','draft_timing_requires_review'):
            with self.subTest(flag=flag):
                state['parts']['0'].pop('empty_recognition_requires_review',None)
                state['parts']['0'].pop('draft_timing_requires_review',None)
                workflow.r.atomic_json(state_path,state)
                self.assertTrue(workflow._samples_unchanged(self.campaign,self.manifest))
                state['parts']['0'][flag]=True;workflow.r.atomic_json(state_path,state)
                before=state_path.read_bytes()
                self.assertFalse(workflow._samples_unchanged(self.campaign,self.manifest))
                with self.assertRaisesRegex(ValueError,'草稿|复核'):
                    workflow.approve(self.campaign,20,18,True)
                self.assertFalse((self.campaign/'approval.json').exists())
                self.assertEqual(state_path.read_bytes(),before)

    def make_samples(self, count, *, language='ja',target='zh-CN'):
        sample = {'name': 'sample', 'folder': 'samples/01', 'start_sec': 30, 'end_sec': 120}
        self.manifest['samples'] = [sample]
        folder = self.campaign / sample['folder']
        self.complete_project(folder / '识别任务', count,language=language,target=target)
        if (language,target)!=('ja','zh-CN'):
            self.manifest.update(language=language,target=target)
        sample['audio_hash']=workflow.sha256(folder/'input.wav')
        (folder / 'preview.mp4').write_bytes(b'preview')
        sample['preview_hash']=workflow.sha256(folder/'preview.mp4')
        sample['preview_validation']={'version':2,'sha256':sample['preview_hash'],
                                      'expected_duration_ms':90000,'video_frames':2160}
        (folder / ('旧日语.srt' if language=='ja' else '旧原文.srt')).write_text('', encoding='utf-8')
        self.save_manifest()

    def approve_final_fixture(self):
        workflow.r.atomic_json(self.campaign / 'final-review.json', {
            'status': 'sampled_approved', 'content_passed': True,
            **{key:self.manifest[key] for key in ('language','target') if key in self.manifest},
            'source_sha256': self.manifest['source']['sha256'],
            'artifacts': workflow.artifacts_for(self.campaign, self.manifest, full=True)})

    def fake_media(self, args, log, stop, **kwargs):
        destination = Path(args[-1])
        if destination.suffix == '.ass':
            destination.write_text('PlayResX: 384\nPlayResY: 288\nStyle: Default,placeholder\n', encoding='utf-8')
        elif destination.suffix == '.mp4':
            destination.write_bytes(b'encoded-video:' + (self.campaign / '导出' / 'captions.srt').read_bytes())

    def prepare_export(self, *, language='ja',target='zh-CN'):
        self.complete_project(self.campaign / '整片', 20,language=language,target=target)
        if (language,target)!=('ja','zh-CN'):
            self.manifest.update(language=language,target=target)
        self.save_manifest()
        self.approve_final_fixture()
        info = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 480,
                             'r_frame_rate': '25/1', 'nb_frames': '2500'}], 'format': {'duration': '100'}}
        patch.object(workflow, 'media_info', return_value=info).start()
        patch.object(workflow, 'audio_digest', return_value='SHA256=matching-audio').start()
        self.media = patch.object(workflow.r, 'run_process', side_effect=self.fake_media).start()
        return workflow.video_output_path(self.source,target)

    def test_multilingual_sample_approval_binds_exact_pair(self):
        self.make_samples(20,language='en',target='ja')
        workflow.approve(self.campaign,20,18,True)
        approval=workflow.read_json(self.campaign/'approval.json')
        self.assertEqual((approval['language'],approval['target']),('en','ja'))
        self.assertIn('日文草稿.srt',{Path(a['path']).name for a in approval['artifacts']})
        self.assertIn('旧原文.srt',{Path(a['path']).name for a in approval['artifacts']})

    def test_english_export_uses_english_captions_and_language_binding(self):
        final=self.prepare_export(language='zh',target='en')
        workflow.export_video(self.campaign,self.stop)
        self.assertEqual(final.name,'source_英文字幕_修订版.mp4')
        self.assertEqual(final.read_bytes(),b'encoded-video:'+(self.campaign/'整片/英文草稿.srt').read_bytes())
        binding=workflow.read_json(self.campaign/'campaign.json')['output_binding']
        self.assertEqual((binding['language'],binding['target']),('zh','en'))

    def test_japanese_export_requires_review_for_same_language_pair(self):
        final=self.prepare_export(language='en',target='ja')
        review=workflow.read_json(self.campaign/'final-review.json')
        review['target']='en'
        workflow.r.atomic_json(self.campaign/'final-review.json',review)
        with self.assertRaisesRegex(ValueError,'验收'):
            workflow.export_video(self.campaign,self.stop)
        self.assertFalse(final.exists())
        review['target']='ja'
        workflow.r.atomic_json(self.campaign/'final-review.json',review)
        workflow.export_video(self.campaign,self.stop)
        self.assertEqual(final.name,'source_日文字幕_修订版.mp4')
        self.assertTrue(final.exists())

    def test_approval_rejects_fewer_than_twenty_actual_sample_cues(self):
        for count in (0, 19):
            with self.subTest(count=count):
                if count:
                    # Each fixture uses a different folder without deleting work.
                    self.campaign = self.campaign.parent / 'another-project'
                    self.campaign.mkdir()
                self.make_samples(count)
                with self.assertRaises(ValueError):
                    workflow.approve(self.campaign, 20, 18, True)
                self.assertFalse((self.campaign / 'approval.json').exists())

    def test_approval_rejects_reviewed_count_exceeding_available_cues(self):
        self.make_samples(20)
        with self.assertRaises(ValueError):
            workflow.approve(self.campaign, 21, 20, True)
        self.assertFalse((self.campaign / 'approval.json').exists())

    def test_twenty_available_cues_can_be_approved_and_count_is_recorded(self):
        self.make_samples(20)
        workflow.approve(self.campaign, 20, 18, True)
        approval = workflow.read_json(self.campaign / 'approval.json')
        self.assertEqual('approved', approval['status'])
        self.assertEqual(20, approval.get('available_cues'))

    def test_sample_approval_rejects_punctuation_only_public_translation(self):
        self.make_samples(20)
        project = self.campaign / self.manifest['samples'][0]['folder'] / '识别任务'
        captions = project / '中文草稿.srt'
        cues = parse_srt(captions.read_text(encoding='utf-8'))
        captions.write_text(render_srt([Cue(c.start_ms, c.end_ms, '.') for c in cues]), encoding='utf-8')
        before = captions.read_bytes()
        with self.assertRaises(ValueError):
            workflow.approve(self.campaign, 20, 18, True)
        self.assertFalse((self.campaign / 'approval.json').exists())
        self.assertEqual(captions.read_bytes(), before)

    def test_final_review_rejects_invalid_public_translation_before_saving_approval(self):
        self.make_samples(20)
        workflow.approve(self.campaign, 20, 18, True)
        project = self.campaign / '整片'
        self.complete_project(project, 20)
        captions = project / '中文草稿.srt'
        cues = parse_srt(captions.read_text(encoding='utf-8'))
        candidates = {
            'empty': '',
            'missing_cue': render_srt(cues[:-1]),
            'punctuation_only': render_srt([Cue(c.start_ms, c.end_ms, '…') for c in cues]),
            'outside_media': render_srt([Cue(c.start_ms + 200000, c.end_ms + 200000, c.text) for c in cues]),
            'timeline_mismatch': render_srt([Cue(c.start_ms, c.end_ms + 1, c.text) for c in cues]),
            'malformed': '1\n00:00:00,000 -> 00:00:01,000\n译文\n',
        }
        for label, text in candidates.items():
            with self.subTest(label=label):
                captions.write_text(text, encoding='utf-8')
                before = captions.read_bytes()
                with self.assertRaises(ValueError):
                    workflow.accept_final(self.campaign, True)
                self.assertFalse((self.campaign / 'final-review.json').exists())
                self.assertEqual(captions.read_bytes(), before)

    def test_final_review_rejects_both_public_files_emptied_despite_valid_parts(self):
        self.make_samples(20)
        workflow.approve(self.campaign, 20, 18, True)
        project = self.campaign / '整片'
        self.complete_project(project, 20)
        for name in ('原文.srt', '中文草稿.srt'):
            (project / name).write_text('', encoding='utf-8')
        with self.assertRaises(ValueError):
            workflow.accept_final(self.campaign, True)
        self.assertFalse((self.campaign / 'final-review.json').exists())

    def test_final_review_preserves_and_binds_valid_human_translation(self):
        self.make_samples(20)
        workflow.approve(self.campaign, 20, 18, True)
        project = self.campaign / '整片'
        self.complete_project(project, 20)
        captions = project / '中文草稿.srt'
        captions.write_text(captions.read_text(encoding='utf-8').replace('译文', '人工修订'), encoding='utf-8')
        before = captions.read_bytes()
        workflow.accept_final(self.campaign, True)
        review = workflow.read_json(self.campaign / 'final-review.json')
        artifact = next(item for item in review['artifacts'] if Path(item['path']) == captions)
        self.assertEqual(artifact['sha256'], workflow.sha256(captions))
        self.assertEqual(captions.read_bytes(), before)

    def test_manual_final_review_rejects_truncated_machine_base_despite_valid_parts(self):
        from subtitle_pipeline.manual_review import load_review, save_review
        project = self.campaign / '整片'
        self.complete_project(project, 20)
        self.manifest['status'] = 'full_ready'
        self.save_manifest()
        for name in ('原文.srt', '中文草稿.srt'):
            path = project / name
            cues = parse_srt(path.read_text(encoding='utf-8'))
            path.write_text(render_srt(cues[:1]), encoding='utf-8')
        self.assertTrue(workflow.state_is_complete(project))
        view = load_review(project)
        cue = view['cues'][0]
        view = save_review(project, {
            'expected_revision': view['revision'], 'cue_id': cue['id'],
            **{key: cue[key] for key in ('start_ms', 'end_ms', 'source_text', 'target_text')},
            'review_status': 'checked', 'note': '已听看', 'translation_confirmed': False,
        })
        self.assertTrue(view['summary']['can_accept'])
        before = {name: (project / name).read_bytes() for name in ('原文.srt', '中文草稿.srt')}
        with self.assertRaisesRegex(ValueError, '合并原文|片段|基底'):
            workflow.accept_final(self.campaign, True, expected_revision=view['revision'])
        self.assertFalse((self.campaign / 'final-review.json').exists())
        self.assertEqual({name: (project / name).read_bytes() for name in before}, before)

    def test_export_rejects_invalid_public_translation_even_when_old_review_hash_matches(self):
        final = self.prepare_export()
        captions = self.campaign / '整片' / '中文草稿.srt'
        cues = parse_srt(captions.read_text(encoding='utf-8'))
        captions.write_text(render_srt([Cue(c.start_ms, c.end_ms, '.') for c in cues]), encoding='utf-8')
        self.approve_final_fixture()
        before = captions.read_bytes()
        with self.assertRaises(ValueError):
            workflow.export_video(self.campaign, self.stop)
        self.assertFalse(final.exists())
        self.assertFalse((self.campaign / '导出').exists())
        self.assertEqual(captions.read_bytes(), before)

    def test_audio_sample_run_stays_draft_and_approval_binds_exact_audio(self):
        self.make_samples(20)
        self.manifest.update(source_kind='audio',status='prepared')
        self.save_manifest()
        folder=self.campaign/self.manifest['samples'][0]['folder']
        (folder/'preview.mp4').unlink()
        before=(folder/'input.wav').read_bytes()
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        settings.public_config.return_value={'provider':'test-only'}
        with patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow.r,'run_pipeline',return_value=None) as run:
            workflow.run_samples(self.campaign,self.stop)
        self.assertEqual(run.call_args.args[0].source,folder/'input.wav')
        self.assertEqual(run.call_args.args[0].workflow_stage,'sample')
        self.assertEqual((folder/'input.wav').read_bytes(),before)
        self.assertEqual(workflow.read_json(self.campaign/'campaign.json')['status'],'samples_ready')
        self.assertFalse((self.campaign/'approval.json').exists())
        workflow.approve(self.campaign,20,18,True)
        artifacts=workflow.read_json(self.campaign/'approval.json')['artifacts']
        evidence=next(item for item in artifacts if Path(item['path'])==folder/'input.wav')
        self.assertEqual(evidence['sha256'],self.manifest['samples'][0]['audio_hash'])
        self.assertFalse(any(Path(item['path']).name=='preview.mp4' for item in artifacts))

    def test_unchanged_completed_samples_resume_locally_without_budget_or_settings_gate(self):
        self.make_samples(20)
        folder = self.campaign / self.manifest['samples'][0]['folder']
        project = folder / '识别任务'
        state_path = project / 'state.json'
        state = workflow.read_json(state_path)
        part_folder = project / '片段' / '0001'
        for public, local, field in [('原文.srt', 'source.local.srt', 'public_source_hash'),
                                     ('中文草稿.srt', 'target.local.srt', 'public_target_hash')]:
            (part_folder / public).write_bytes((part_folder / local).read_bytes())
            state['parts']['0'][field] = workflow.sha256(part_folder / public)
        state['generated_hashes'] = {name: workflow.sha256(project / name)
                                    for name in ('原文.srt', '中文草稿.srt', '双语草稿.srt')}
        workflow.r.atomic_json(state_path, state)
        self.manifest['status'] = 'samples_incomplete'
        self.save_manifest()
        before = {path: path.read_bytes() for path in project.rglob('*') if path.is_file()}
        summary = {'committed_cny': 17.5}
        with patch.object(workflow, 'cost_report', return_value=summary), \
                patch.object(workflow, 'load_settings', side_effect=AssertionError('No cloud settings needed')), \
                patch.object(workflow.r, 'run_pipeline', side_effect=AssertionError('No work to run')):
            workflow.run_samples(self.campaign, self.stop)
        saved = workflow.read_json(self.campaign / 'campaign.json')
        self.assertEqual(saved['status'], 'samples_ready')
        self.assertEqual(saved['cost'], summary)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse((self.campaign / 'approval.json').exists())

    def test_edited_or_unproven_sample_cache_cannot_skip_the_normal_resume_gate(self):
        self.make_samples(20)
        folder = self.campaign / self.manifest['samples'][0]['folder']
        project = folder / '识别任务'
        state_path = project / 'state.json'
        state = workflow.read_json(state_path)
        part_folder = project / '片段' / '0001'
        for public, local, field in [('原文.srt', 'source.local.srt', 'public_source_hash'),
                                     ('中文草稿.srt', 'target.local.srt', 'public_target_hash')]:
            (part_folder / public).write_bytes((part_folder / local).read_bytes())
            state['parts']['0'][field] = workflow.sha256(part_folder / public)
        state['generated_hashes'] = {name: workflow.sha256(project / name)
                                    for name in ('原文.srt', '中文草稿.srt', '双语草稿.srt')}
        workflow.r.atomic_json(state_path, state)
        saved_files = {path: path.read_bytes() for path in project.rglob('*') if path.is_file()}
        mutations = [project / '中文草稿.srt', part_folder / '中文草稿.srt', part_folder / 'target.local.srt',
                     part_folder / 'asr-response.json', state_path]
        settings = Mock(asr_input_rate=.8, asr_output_rate=2.7)
        settings.public_config.return_value = {}
        for changed in mutations:
            with self.subTest(changed=changed.relative_to(project)):
                for path, contents in saved_files.items():
                    path.write_bytes(contents)
                if changed == state_path:
                    unproven = workflow.read_json(state_path)
                    unproven.pop('generated_hashes')
                    workflow.r.atomic_json(state_path, unproven)
                else:
                    changed.write_bytes(saved_files[changed] + b'changed')
                before = changed.read_bytes()
                with patch.object(workflow, 'cost_report', return_value={'committed_cny': 17.5}), \
                        patch.object(workflow, 'load_settings', return_value=settings), \
                        patch.object(workflow.r, 'run_pipeline', side_effect=AssertionError('Budget must block')):
                    with self.assertRaises(ValueError):
                        workflow.run_samples(self.campaign, self.stop)
                self.assertEqual(changed.read_bytes(), before)

    def test_legacy_video_sample_without_preview_cannot_start_paid_sample_processing(self):
        self.make_samples(20)
        folder=self.campaign/self.manifest['samples'][0]['folder']
        (folder/'preview.mp4').unlink()
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        settings.public_config.return_value={'provider':'test-only'}
        with patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow.r,'run_pipeline') as run:
            with self.assertRaises(ValueError):
                workflow.run_samples(self.campaign,self.stop)
        run.assert_not_called()
        self.assertFalse((self.campaign/'approval.json').exists())

    def test_changed_audio_sample_cannot_start_or_approve(self):
        self.make_samples(20)
        self.manifest['source_kind']='audio'
        self.save_manifest()
        folder=self.campaign/self.manifest['samples'][0]['folder']
        (folder/'preview.mp4').unlink()
        (folder/'input.wav').write_bytes(b'changed-audio')
        settings=Mock(asr_input_rate=.8,asr_output_rate=2.7)
        settings.public_config.return_value={'provider':'test-only'}
        with patch.object(workflow,'load_settings',return_value=settings), \
                patch.object(workflow.r,'run_pipeline') as run:
            with self.assertRaises(ValueError):workflow.run_samples(self.campaign,self.stop)
        run.assert_not_called()
        with self.assertRaises(ValueError):workflow.approve(self.campaign,20,18,True)
        self.assertFalse((self.campaign/'approval.json').exists())

    def test_changed_approved_subtitles_do_not_reuse_old_export(self):
        final = self.prepare_export()
        workflow.export_video(self.campaign, self.stop)
        old_video = final.read_bytes()
        captions = self.campaign / '整片' / '中文草稿.srt'
        captions.write_text(captions.read_text(encoding='utf-8').replace('译文', '修改'), encoding='utf-8')
        self.approve_final_fixture()
        self.media.reset_mock()
        with self.assertRaises(ValueError):
            workflow.export_video(self.campaign, self.stop)
        self.assertEqual(old_video, final.read_bytes())
        self.assertEqual(0, self.media.call_count)

    def test_unchanged_approved_subtitles_reuse_verified_export_without_encoding(self):
        final = self.prepare_export()
        workflow.export_video(self.campaign, self.stop)
        expected = final.read_bytes()
        self.media.reset_mock()
        workflow.export_video(self.campaign, self.stop)
        self.assertEqual(0, self.media.call_count)
        self.assertEqual(expected, final.read_bytes())

    def test_export_publishes_across_filesystems_without_cross_device_rename(self):
        final = self.prepare_export()
        actual_rename = os.rename
        def same_directory_rename(source, target):
            if Path(source).parent != Path(target).parent:
                raise OSError(errno.EXDEV, 'cross-device link')
            return actual_rename(source, target)
        with patch.object(workflow.os, 'rename', side_effect=same_directory_rename):
            workflow.export_video(self.campaign, self.stop)
        self.assertTrue(final.read_bytes().startswith(b'encoded-video:'))
        self.assertEqual(workflow.sha256(final), workflow.read_json(self.campaign / 'campaign.json')['output_sha256'])
        self.assertEqual(b'original-video-never-modify', self.source.read_bytes())

    def test_publication_copy_failure_retains_encoded_video_and_leaves_no_final(self):
        final = self.prepare_export()
        with patch.object(workflow.shutil, 'copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                workflow.export_video(self.campaign, self.stop)
        self.assertFalse(final.exists())
        self.assertTrue((self.campaign / '导出' / 'result.partial.mp4').exists())
        self.assertEqual([self.source], list(self.source.parent.iterdir()))

    def test_publication_never_overwrites_a_file_created_during_copy(self):
        final = self.prepare_export()
        original_copy = workflow.shutil.copyfileobj
        def copy_with_race(source, target, *args):
            original_copy(source, target, *args)
            final.write_bytes(b'another-writer-file')
        with patch.object(workflow.shutil, 'copyfileobj', side_effect=copy_with_race):
            with self.assertRaises(FileExistsError):
                workflow.export_video(self.campaign, self.stop)
        self.assertEqual(b'another-writer-file', final.read_bytes())
        self.assertTrue((self.campaign / '导出' / 'result.partial.mp4').exists())


if __name__=='__main__':unittest.main()
