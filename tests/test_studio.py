import json
import os
from contextlib import contextmanager, nullcontext, redirect_stdout
import io
import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import quote


class StudioTests(unittest.TestCase):
    def setUp(self):
        from subtitle_pipeline import studio
        self.s=studio
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=self.root/'studio.json'
        self.source=self.root/'video.mp4';self.source.write_bytes(b'fake media')
        self.campaign=self.root/'project';self.campaign.mkdir()
        self.env={'DEEPSEEK_API_KEY':'secret-do-not-return'}
        guards=[patch.object(studio,'account_environment',side_effect=lambda:self.env),
                patch.object(studio,'encrypted_names',return_value={'DEEPSEEK_API_KEY'}),
                patch.object(studio,'engine_available',return_value=True),
                # This suite uses process doubles; native ownership has real tests.
                patch.object(studio,'owned_process',side_effect=nullcontext),
                patch.object(studio,'wait_for_process_exit',return_value=True)]
        for guard in guards:guard.start();self.addCleanup(guard.stop)
        self.app=studio.StudioController(source=str(self.source),campaign=str(self.campaign),state_path=self.store)

    def test_open_project_returns_the_exact_path_for_the_frontend_receipt(self):
        with patch.object(self.s.os, 'startfile', create=True) as open_file:
            result = self.app.open_result('project', self.app._project_id())
        open_file.assert_called_once_with(self.campaign.resolve())
        self.assertEqual(result, {'opened': True, 'path': str(self.campaign.resolve())})

    def test_failed_open_does_not_return_a_success_receipt(self):
        with patch.object(self.s.os, 'startfile', side_effect=OSError('Explorer unavailable'), create=True):
            with self.assertRaisesRegex(OSError, 'Explorer unavailable'):
                self.app.open_result('project', self.app._project_id())

    def test_state_never_contains_key_and_reopens_selected_project(self):
        state=self.app.state()
        self.assertTrue(state['accounts']['deepseek']['configured'])
        self.assertEqual(state['accounts']['deepseek']['storage'],'encrypted')
        self.assertNotIn('secret-do-not-return',json.dumps(state))
        self.app.select_project({'source':str(self.source),'campaign':str(self.campaign),'baseline':''})
        again=self.s.StudioController(state_path=self.store)
        self.assertEqual(again.state()['source'],str(self.source))
        self.assertNotIn('secret-do-not-return',self.store.read_text(encoding='utf-8'))

    def test_save_key_is_independent_of_free_confirmation_and_not_returned(self):
        with patch.object(self.s,'save_secrets') as save,patch.object(self.s,'write_public_environment') as public:
            response=self.app.save_credentials({'provider':'siliconflow','key':'sf-fake-key'})
        save.assert_called_once_with({'SILICONFLOW_API_KEY':'sf-fake-key'})
        public.assert_not_called()
        self.assertNotIn('sf-fake-key',json.dumps(response))
        with patch.object(self.s,'save_secrets') as save:
            for data in ({'provider':'other','key':'key'},{'provider':'siliconflow','key':'bad key'}):
                with self.assertRaises(ValueError):self.app.save_credentials(data)
            save.assert_not_called()

    def test_busy_prevents_project_change_and_stop_is_cooperative(self):
        self.app.job['busy']=True
        self.app.job['action']='local'
        with self.assertRaises(ValueError):self.app.select_project({'source':'new'})
        with self.assertRaises(ValueError):self.app.start({'action':'local'})
        self.app.stop()
        self.assertTrue(self.app.stop_event.is_set())
        self.assertTrue(self.app.job['busy'])

    def test_slow_preview_does_not_block_stop_or_progress(self):
        entered=threading.Event();release=threading.Event()
        original=self.s.StudioController._read_track
        def slow(view,*args):
            if not entered.is_set():
                entered.set()
                if not release.wait(4):raise AssertionError('preview was not released')
            return original(view,*args)
        self.app.job.update(busy=True,action='local')
        with patch.object(self.s.StudioController,'_read_track',slow),ThreadPoolExecutor(max_workers=2) as pool:
            preview=pool.submit(self.app.preview)
            try:
                self.assertTrue(entered.wait(2))
                stopped=pool.submit(self.app.stop)
                self.assertEqual(stopped.result(timeout=2),{'stopping':True})
                self.assertTrue(self.app.stop_event.is_set())
                advanced=pool.submit(self.app._progress,{'translated':17})
                advanced.result(timeout=2)
                self.assertEqual(self.app.job['translated'],17)
            finally:release.set()
            preview.result(timeout=2)

    def test_slow_state_does_not_block_progress_and_returns_isolated_job(self):
        entered=threading.Event();release=threading.Event()
        original=self.s.StudioController._saved_state
        def slow(view):
            if not entered.is_set():
                entered.set()
                if not release.wait(4):raise AssertionError('state read was not released')
            return original(view)
        self.app.job.update(action='local',logs=['original'])
        with patch.object(self.s.StudioController,'_saved_state',slow),ThreadPoolExecutor(max_workers=2) as pool:
            state=pool.submit(self.app.state)
            try:
                self.assertTrue(entered.wait(2))
                advanced=pool.submit(self.app._progress,{'translated':17,'message':'advanced'})
                advanced.result(timeout=2)
            finally:release.set()
            snapshot=state.result(timeout=2)
        self.assertEqual(snapshot['job']['translated'],17)
        self.assertEqual(snapshot['job']['logs'][0],'original')
        self.assertTrue(any('advanced' in line for line in snapshot['job']['logs']))
        snapshot['job']['logs'].append('response mutation')
        self.assertNotIn('response mutation',self.app.job['logs'])
        self.assertEqual(self.app.job['translated'],17)

    def test_state_refreshes_job_and_actions_when_run_starts_during_read(self):
        entered=threading.Event();release=threading.Event();worker_release=threading.Event()
        original=self.s.StudioController._saved_state
        def slow(view):
            if not entered.is_set():
                entered.set()
                if not release.wait(4):raise AssertionError('state read was not released')
            return original(view)
        def run(*args,**kwargs):
            if not worker_release.wait(4):raise AssertionError('worker was not released')
            return {'status':'complete'}
        with patch.object(self.s.StudioController,'_saved_state',slow),patch.object(self.s,'run_pipeline',side_effect=run),ThreadPoolExecutor(max_workers=2) as pool:
            state=pool.submit(self.app.state)
            try:
                self.assertTrue(entered.wait(2))
                pool.submit(self.app.start,{'action':'local'}).result(timeout=2)
                release.set()
                snapshot=state.result(timeout=2)
                self.assertTrue(snapshot['job']['busy'])
                self.assertEqual(snapshot['job']['action'],'local')
                self.assertTrue(snapshot['actions']['stop'])
                self.assertFalse(snapshot['actions']['local'])
            finally:
                release.set();worker_release.set()
                if self.app.worker:self.app.worker.join(2)

    def test_project_bound_preview_rejects_selection_change_including_aba(self):
        for aba in (False,True):
            with self.subTest(aba=aba):
                self.app.select_project({'source':str(self.source),'campaign':str(self.campaign)})
                identity=self.app._project_id()
                alternate=self.root/('alternate-'+str(aba));alternate.mkdir()
                entered=threading.Event();release=threading.Event()
                original=self.s.StudioController._read_track
                def slow(view,*args):
                    if not entered.is_set():
                        entered.set()
                        if not release.wait(4):raise AssertionError('preview was not released')
                    return original(view,*args)
                with patch.object(self.s.StudioController,'_read_track',slow),ThreadPoolExecutor(max_workers=2) as pool:
                    preview=pool.submit(self.app.preview,'main',identity)
                    try:
                        self.assertTrue(entered.wait(2))
                        selection=pool.submit(self.app.select_project,{'source':str(self.source),'campaign':str(alternate)})
                        selection.result(timeout=2)
                        if aba:
                            selection=pool.submit(self.app.select_project,{'source':str(self.source),'campaign':str(self.campaign)})
                            selection.result(timeout=2)
                            self.assertEqual(self.app._project_id(),identity)
                    finally:release.set()
                    with self.assertRaises(self.s.ProjectSelectionChanged):preview.result(timeout=2)

    def test_state_retries_snapshot_after_project_selection_changes(self):
        entered=threading.Event();release=threading.Event()
        original=self.s.StudioController._saved_state
        def slow(view):
            if not entered.is_set():
                entered.set()
                if not release.wait(4):raise AssertionError('state read was not released')
            return original(view)
        alternate=self.root/'alternate';alternate.mkdir()
        with patch.object(self.s.StudioController,'_saved_state',slow),ThreadPoolExecutor(max_workers=2) as pool:
            state=pool.submit(self.app.state)
            try:
                self.assertTrue(entered.wait(2))
                selection=pool.submit(self.app.select_project,{'source':str(self.source),'campaign':str(alternate)})
                selected=selection.result(timeout=2)
            finally:release.set()
            snapshot=state.result(timeout=2)
        self.assertEqual(snapshot['campaign'],str(alternate))
        self.assertEqual(snapshot['project_id'],selected['project_id'])

    def test_slow_media_download_and_export_path_reads_do_not_block_progress(self):
        (self.campaign/'原文.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nfixture\n',encoding='utf-8')
        for method,read in (('_selection_media',lambda:self.app.media_path('main')),
                            ('_preview_selection',lambda:self.app.download_path('原文.srt','main')),
                            ('_exported_video_path',self.app.exported_video_path)):
            with self.subTest(method=method):
                entered=threading.Event();release=threading.Event()
                original=getattr(self.s.StudioController,method)
                def slow(view,*args):
                    entered.set()
                    if not release.wait(4):raise AssertionError('path read was not released')
                    return original(view,*args)
                with patch.object(self.s.StudioController,method,slow),ThreadPoolExecutor(max_workers=2) as pool:
                    pending=pool.submit(read)
                    try:
                        self.assertTrue(entered.wait(2))
                        pool.submit(self.app._progress,{'translated':17}).result(timeout=2)
                    finally:release.set()
                    pending.result(timeout=2)

    def test_siliconflow_pricing_saved_separately_and_free_confirmation_is_rejected(self):
        data={'price_per_second':0.000220,'pricing_reference':'Current account model price','confirmed':True}
        with patch.object(self.s,'write_public_environment') as public:
            self.app.save_siliconflow_pricing(data)
        values=public.call_args.args[0]
        self.assertEqual(float(values['SILICONFLOW_ASR_CNY_PER_SECOND']),0.000220)
        self.assertTrue(values['SILICONFLOW_PRICING_VERIFIED_ON'])
        self.assertFalse(any('KEY' in name for name in values))
        with patch.object(self.s,'write_public_environment') as public:
            with self.assertRaises(ValueError):
                self.app.save_credentials({'provider':'siliconflow','free_confirmed':True})
            public.assert_not_called()

    def test_siliconflow_pricing_requires_explicit_confirmed_finite_price_and_source(self):
        data={'price_per_second':0.000220,'pricing_reference':'Current account price','confirmed':True}
        for change in ({'confirmed':False},{'price_per_second':float('nan')},{'price_per_second':-1}, {'pricing_reference':''}):
            with self.subTest(change=change),patch.object(self.s,'write_public_environment') as public:
                with self.assertRaises(ValueError):self.app.save_siliconflow_pricing({**data,**change})
                public.assert_not_called()
        self.app.job['busy']=True
        with self.assertRaises(ValueError):self.app.save_siliconflow_pricing(data)

    def test_cloud_gates_are_enforced_server_side(self):
        for action in ('full','approve','export','siliconflow-pilot','samples'):
            with self.subTest(action=action),self.assertRaises(ValueError):self.app.start({'action':action})
        self.assertFalse(self.app.job['busy'])

    def test_local_run_uses_existing_pipeline_and_real_counts(self):
        with patch.object(self.s,'run_pipeline',return_value={'status':'complete','total':2,'recognized':2,'translated':0,'message':'done'}) as run:
            self.app.start({'action':'local','language':'ja','chunk_seconds':60,'workers':1,'translate':False})
            self.app.worker.join(3)
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.job['recognized'],2)
        self.assertEqual(run.call_args.args[0].source,self.source)
        self.assertFalse(run.call_args.args[0].translate)

    def test_preview_and_download_only_use_whitelisted_existing_subtitles(self):
        (self.campaign/'原文.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nテスト\n',encoding='utf-8')
        result=self.app.preview()
        self.assertEqual(result['cues'][0]['ja'],'テスト')
        self.assertEqual(result['downloads'],['原文.srt'])
        self.assertEqual(self.app.download_path('原文.srt'),self.campaign/'原文.srt')
        for name in ('../studio.json','state.json','中文草稿.srt'):
            with self.assertRaises(ValueError):self.app.download_path(name)

    def test_preview_media_and_download_reuse_parsed_tracks_across_read_views(self):
        track='1\n00:00:01,000 --> 00:00:02,000\nfixture\n'
        for name in self.s.OUTPUTS[:2]:
            (self.campaign/name).write_text(track,encoding='utf-8')
        with patch.object(self.s,'parse_srt',wraps=self.s.parse_srt) as parser:
            first=self.app.preview()
            self.assertEqual(first,self.app.preview())
            self.assertEqual(self.app.media_path(),self.source)
            self.assertEqual(self.app.download_path('原文.srt'),self.campaign/'原文.srt')
        self.assertEqual(parser.call_count,2)

    def test_preview_cache_observes_manual_edits_and_never_hides_corruption(self):
        path=self.campaign/'原文.srt'
        body='1\n00:00:01,000 --> 00:00:02,000\nfirst\n'
        path.write_text(body,encoding='utf-8')
        self.assertEqual(self.app.preview()['cues'][0]['ja'],'first')
        original=path.stat()
        path.write_text(body.replace('first','other'),encoding='utf-8')
        os.utime(path,ns=(original.st_atime_ns,original.st_mtime_ns))
        self.assertEqual(self.app.preview()['cues'][0]['ja'],'other')
        path.write_text('broken subtitle',encoding='utf-8')
        with self.assertRaises(ValueError):self.app.preview()
        path.unlink()
        self.assertEqual(self.app.preview()['cues'],[])

    def make_samples(self):
        samples=[]
        for index,start in enumerate((30,1680),1):
            folder=self.campaign/'样片'/f'{index:02d}'
            job=folder/'识别任务';job.mkdir(parents=True)
            (folder/'preview.mp4').write_bytes(f'sample {index}'.encode())
            (job/'原文.srt').write_text(f'1\n00:00:01,000 --> 00:00:02,000\n字幕{index}\n',encoding='utf-8')
            (job/'中文草稿.srt').write_text(f'1\n00:00:01,000 --> 00:00:02,000\n译文{index}\n',encoding='utf-8')
            samples.append({'name':f'样片 {index}','folder':f'样片/{index:02d}','start_sec':start,'end_sec':start+90})
        manifest={'status':'samples_ready','asr_provider':'qwen_asr','source':{'path':str(self.source)},'samples':samples}
        (self.campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def test_sample_preview_defaults_to_available_sample_and_keeps_local_timeline(self):
        self.make_samples()
        result=self.app.preview()
        self.assertEqual(result['selected_id'],'sample-1')
        self.assertEqual(result['offset_ms'],30000)
        self.assertEqual(result['media_url'],'/api/media?sample=sample-1&project='+result['project_id'])
        self.assertEqual(result['cues'][0]['start_ms'],1000)
        self.assertEqual(result['cues'][0]['zh'],'译文1')
        self.assertEqual([item['id'] for item in result['selections']],['main','sample-1','sample-2'])
        self.assertEqual(self.app.media_path(),self.campaign/'样片'/'01'/'preview.mp4')
        self.assertEqual(self.app.download_path('原文.srt'),self.campaign/'样片'/'01'/'识别任务'/'原文.srt')
        second=self.app.preview('sample-2')
        self.assertEqual(second['offset_ms'],1680000)
        self.assertEqual(second['cues'][0]['ja'],'字幕2')
        self.assertEqual(self.app.download_path('原文.srt','sample-2'),self.campaign/'样片'/'02'/'识别任务'/'原文.srt')
        self.assertEqual(self.app.media_path('main'),self.source)
        self.assertEqual(self.app.state()['project_config']['asr_provider'],'qwen_asr')
        self.assertEqual(self.app.state()['project_config']['engine'],'bailian')

    def test_main_outputs_still_take_precedence_and_audio_fallback_is_supported(self):
        manifest=self.make_samples()
        (self.campaign/'原文.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\n本体\n',encoding='utf-8')
        self.assertEqual(self.app.preview()['selected_id'],'main')
        self.assertEqual(self.app.media_path(),self.source)
        self.assertEqual(self.app.download_path('原文.srt'),self.campaign/'原文.srt')
        folder=self.campaign/'样片'/'02'
        (folder/'preview.mp4').unlink();(folder/'input.wav').write_bytes(b'wave')
        self.assertFalse(self.app.preview('sample-2')['media_available'])
        with self.assertRaises(ValueError):self.app.media_path('sample-2')
        manifest['source_kind']='audio'
        (self.campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        self.assertEqual(self.app.media_path('sample-2'),folder/'input.wav')

    def test_sample_selector_never_accepts_paths_or_other_project_ids(self):
        self.make_samples()
        for selector in ('../project','样片/01','sample-0','sample-3','sample-1/../../studio.json',str(self.root)):
            with self.subTest(selector=selector):
                for operation in (lambda:self.app.preview(selector),lambda:self.app.media_path(selector),
                                  lambda:self.app.download_path('原文.srt',selector)):
                    with self.assertRaises(ValueError):operation()
        other=self.root/'other';other.mkdir()
        self.app.campaign=str(other)
        with self.assertRaises(ValueError):self.app.download_path('原文.srt','sample-1')

    def test_manifest_traversal_cannot_escape_selection(self):
        manifest=self.make_samples()
        manifest['samples'][0]['folder']='../outside'
        (self.campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaises(ValueError):self.app.preview('sample-1')
        self.assertNotIn('sample-1',[item['id'] for item in self.app.preview()['selections']])

    def test_junction_cannot_redirect_sample_to_other_project(self):
        manifest=self.make_samples()
        link=self.campaign/'linked';outside=self.root/'private';outside.mkdir()
        if os.name=='nt':
            import _winapi
            _winapi.CreateJunction(str(outside),str(link))
        else:link.symlink_to(outside,target_is_directory=True)
        self.addCleanup(lambda:link.rmdir() if os.name=='nt' else link.unlink())
        manifest['samples'][1]['folder']='linked'
        (self.campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaises(ValueError):self.app.download_path('原文.srt','sample-2')
        with self.assertRaises(ValueError):self.app.preview('sample-2')
        with self.assertRaises(ValueError):self.app.media_path('sample-2')

    def test_only_recorded_export_at_expected_source_sibling_can_be_opened_or_downloaded(self):
        manifest=self.make_samples()
        from subtitle_pipeline.integrity import sha256
        output=self.source.with_name(self.source.stem+'_中文字幕_修订版.mp4')
        output.write_bytes(b'exported video')
        self.assertIsNone(self.app.preview().get('exported_video'))
        manifest['source']['sha256']=sha256(self.source)
        manifest.update(status='exported',output=str(output),output_sha256=sha256(output),
                        output_binding={'source_sha256':manifest['source']['sha256'],'captions_sha256':'c'*64,'review_artifacts_sha256':'d'*64,'render_version':1})
        (self.campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        result=self.app.preview()
        self.assertEqual(result['exported_video']['name'],output.name)
        self.assertEqual(result['exported_video']['media_url'],'/api/output-video?project='+result['project_id'])
        self.assertEqual(self.app.download_path('video'),output)
        with patch.object(self.s.os,'startfile',create=True) as open_file:
            self.app.open_result('video');self.app.open_result('video-folder')
        self.assertEqual([call.args[0] for call in open_file.call_args_list],[output,output.parent])
        manifest['output']=str(self.source)
        (self.campaign/'campaign.json').write_text(json.dumps(manifest),encoding='utf-8')
        self.assertIsNone(self.app.preview()['exported_video'])
        with self.assertRaises(ValueError):self.app.download_path('video')
        with self.assertRaises(ValueError):self.app.open_result('video')

    def test_budget_summary_is_compact_read_only_and_keeps_shared_scope_explicit(self):
        self.make_samples()
        ledger=self.campaign/'费用账本.json'
        data={'version':1,'budget_cny':20,'stop_cny':18,'requests':{
            'request-secret-id':{'status':'reserved','attempts':[],'reserved_cny':2,
                                 'raw_path':'private-response-path','provider':'qwen'}}}
        ledger.write_text(json.dumps(data),encoding='utf-8')
        before=ledger.read_bytes()
        result=self.app.state()['budget']
        self.assertEqual(result['reserved_cny'],2)
        self.assertEqual(result['remaining_cny'],18)
        self.assertEqual(result['scope'],'shared')
        self.assertNotIn('request-secret-id',json.dumps(result))
        self.assertNotIn('private-response-path',json.dumps(result))
        self.assertEqual(ledger.read_bytes(),before)
        self.assertFalse(ledger.with_suffix('.json.lock').exists())
        ledger.write_text('{}',encoding='utf-8')
        self.assertIsNone(self.app.state()['budget'])

    def test_campaign_displays_explicit_shared_budget_without_creating_a_local_ledger(self):
        self.make_samples()
        manifest_path=self.campaign/'campaign.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        ledger=self.root/'shared-ledger.json'
        manifest['budget_ledger']=str(ledger.resolve())
        manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
        ledger.write_text(json.dumps({'version':1,'budget_cny':20,'stop_cny':18,'requests':{
            'pending':{'status':'reserved','attempts':[],'reserved_cny':3.5,
                       'raw_path':'synthetic-response.json','provider':'qwen_asr'}}}),encoding='utf-8')
        before=ledger.read_bytes()
        result=self.app.state()['budget']
        self.assertIsNotNone(result)
        self.assertEqual(result['committed_cny'],3.5)
        self.assertEqual(result['scope'],'shared')
        self.assertEqual(ledger.read_bytes(),before)
        self.assertFalse((self.campaign/'费用账本.json').exists())

    def test_external_shared_ledger_requires_saved_configuration_to_match_current_source_and_project(self):
        ledger=self.root/'shared-ledger.json'
        ledger.write_text(json.dumps({'version':1,'budget_cny':20,'stop_cny':18,'requests':{}}),encoding='utf-8')
        config={'source':str(self.source),'project':str(self.campaign),'asr_provider':'qwen_asr',
                'budget_ledger':str(ledger),'budget_cny':20}
        state=self.campaign/'state.json'
        state.write_text(json.dumps({'config':config}),encoding='utf-8')
        self.assertEqual(self.app.state()['budget']['scope'],'shared')
        config['source']=str(self.root/'other.mp4')
        state.write_text(json.dumps({'config':config}),encoding='utf-8')
        self.assertIsNone(self.app.state()['budget'])

    def test_http_preview_media_and_download_follow_the_same_allowlisted_sample(self):
        import _socket
        import socket
        self.make_samples()
        token='synthetic-loopback-token'
        server=self.s.ThreadingHTTPServer(('127.0.0.1',0),self.s.BaseHTTPRequestHandler)
        host=f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass=self.s.make_handler(self.app,token,host,self.root)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,extra=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            local_socket=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            try:
                # The offline runner still blocks every ordinary outbound
                # connection. This one test connects only to its own exact
                # loopback endpoint, without changing any global guard.
                local_socket.settimeout(3)
                _socket.socket.connect(local_socket,('127.0.0.1',server.server_port))
                connection.sock=local_socket
                connection.request('GET',path,headers={'X-Subtitle-Token':token,**(extra or {})})
                response=connection.getresponse()
                return response.status,response.read()
            finally:
                connection.close();local_socket.close()
        try:
            code,body=request('/api/preview?sample=sample-2')
            self.assertEqual(code,200)
            preview=json.loads(body)
            self.assertEqual(preview['selected_id'],'sample-2')
            self.assertEqual(request(preview['media_url']),(200,b'sample 2'))
            self.assertEqual(request(preview['media_url'],{'Range':'bytes=0-5'}),(206,b'sample'))
            code,subtitle=request('/api/download?sample=sample-2&name='+quote('原文.srt'))
            self.assertEqual(code,200);self.assertIn('字幕2',subtitle.decode('utf-8'))
            self.assertEqual(request('/api/media?sample=../elsewhere')[0],400)
            self.assertEqual(request('/api/download?sample=sample-9&name='+quote('原文.srt'))[0],400)
            another=self.root/'another';another.mkdir()
            self.app.select_project({'source':str(self.source),'campaign':str(another)})
            code,body=request(preview['media_url'])
            self.assertEqual(code,400)
            self.assertIn('切换',json.loads(body)['error'])
            self.assertEqual(request('/api/download?sample=sample-2&name='+quote('原文.srt')+'&project='+preview['project_id'])[0],400)
        finally:
            server.shutdown();server.server_close();thread.join(3)

    def test_project_binding_rejects_previous_windows_media_and_download_after_selection_changes(self):
        self.make_samples()
        preview=self.app.preview()
        identity=preview['project_id']
        self.assertEqual(self.app.state()['project_id'],identity)
        self.assertEqual(len(identity),64)
        self.assertNotIn(str(self.campaign),identity)
        self.assertEqual(self.app.preview('sample-1',identity)['project_id'],identity)
        self.assertEqual(self.app.media_path('sample-1',identity),self.campaign/'样片'/'01'/'preview.mp4')
        self.assertTrue(self.app.download_path('原文.srt','sample-1',identity).is_file())
        for invalid in ('', '不是有效ID', 42, {}):
            with self.assertRaises(ValueError):self.app.preview('sample-1',invalid)
        next_project=self.root/'another-project';next_project.mkdir()
        self.app.select_project({'source':str(self.source),'campaign':str(next_project)})
        self.assertNotEqual(self.app.state()['project_id'],identity)
        for operation in (lambda:self.app.preview('sample-1',identity),
                          lambda:self.app.media_path('sample-1',identity),
                          lambda:self.app.download_path('原文.srt','sample-1',identity),
                          lambda:self.app.exported_video_path(identity),
                          lambda:self.app.open_result('project',identity)):
            with self.assertRaisesRegex(ValueError,'切换|刷新'):operation()
        self.assertEqual(self.app.media_path('main'),self.source)

    def test_stale_run_project_binding_rejects_all_actions_without_mutating_job_or_starting_work(self):
        prior=self.app.state()['project_id']
        another=self.root/'run-target';another.mkdir()
        self.app.select_project({'source':str(self.source),'campaign':str(another)})
        before=json.dumps(self.app.job,sort_keys=True)
        state_bytes=self.store.read_bytes()
        with patch.object(self.s,'run_pipeline') as pipeline,patch.object(self.s,'start_process') as cloud, \
                patch.object(self.s.threading,'Thread') as worker:
            for action in ('local','prepare','samples','siliconflow-pilot','approve','full','accept-final','export'):
                with self.subTest(action=action),self.assertRaisesRegex(ValueError,'切换'):
                    self.app.start({'action':action,'project_id':prior,'reviewed':20,
                                    'timing_passed':20,'content_passed':True})
                self.assertEqual(json.dumps(self.app.job,sort_keys=True),before)
                self.assertEqual(self.store.read_bytes(),state_bytes)
                self.assertEqual(list(another.iterdir()),[])
            pipeline.assert_not_called();cloud.assert_not_called();worker.assert_not_called()

    def test_project_loader_recovers_existing_local_parameters(self):
        config={'source':str(self.source),'project':str(self.campaign),'language':'ja',
                'chunk_seconds':60,'overlap_seconds':2,'workers':2,'threads':4,'translate':False}
        (self.campaign/'state.json').write_text(json.dumps({'config':config,'status':'cancelled'}),encoding='utf-8')
        self.app.select_project({'campaign':str(self.campaign)})
        self.assertEqual(self.app.state()['project_config']['workers'],2)
        self.assertEqual(self.app.state()['project_config']['model'],'small')
        self.assertEqual(self.app.state()['source'],str(self.source))

    def test_existing_project_cannot_silently_process_previous_video_after_source_change(self):
        other=self.root/'other.mp4';other.write_bytes(b'other')
        (self.campaign/'state.json').write_text(json.dumps({'config':{'source':str(self.source),'project':str(self.campaign)}}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'另一个素材'):
            self.app.select_project({'campaign':str(self.campaign),'source':str(other)})
        self.assertEqual(self.app.source,str(self.source))

    def test_new_task_allocates_unused_directory_without_adopting_old_same_source_results(self):
        base=self.root/'jobs'/'video';base.mkdir(parents=True)
        (base/'state.json').write_text(json.dumps({'config':{'source':str(self.source),'project':str(base)}}),encoding='utf-8')
        second=base.with_name(base.name+'-02');second.mkdir()
        old_bytes=(base/'state.json').read_bytes()
        with patch.object(self.s,'timestamped_project',return_value=base):
            result=self.app.select_project({'source':str(self.source),'campaign':'','baseline':'','new_task':True})
        self.assertEqual(Path(result['campaign']),base.with_name(base.name+'-03'))
        self.assertEqual(result['project_config'],{})
        self.assertEqual((base/'state.json').read_bytes(),old_bytes)
        self.assertFalse(Path(result['campaign']).exists())

    def test_new_task_rejects_existing_nonempty_explicit_directory_but_accepts_empty_one(self):
        (self.campaign/'notes.txt').write_text('keep me',encoding='utf-8')
        other=self.root/'other.mp4';other.write_bytes(b'new source')
        body={'source':str(other),'campaign':str(self.campaign),'new_task':True}
        with self.assertRaisesRegex(ValueError,'新|空'):self.app.select_project(body)
        self.assertEqual(self.app.source,str(self.source))
        empty=self.root/'empty-project';empty.mkdir()
        result=self.app.select_project({**body,'campaign':str(empty)})
        self.assertEqual(result['source'],str(other))
        self.assertEqual(result['campaign'],str(empty))
        self.assertEqual(result['baseline'],'')

    def test_new_task_flag_is_boolean_and_omitting_it_preserves_resume(self):
        for value in ('true',1,None,{},[]):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.app.select_project({'new_task':value})
        self.make_samples()
        resumed=self.app.select_project({'campaign':str(self.campaign)})
        self.assertEqual(resumed['project_config']['engine'],'bailian')
        self.assertEqual(resumed['campaign_status'],'samples_ready')

    def test_new_task_accepts_only_the_residual_lock_guard_not_other_files(self):
        guard=self.campaign/'run.guard.lock';guard.write_bytes(b'\0')
        body={'source':str(self.source),'campaign':str(self.campaign),'new_task':True}
        self.assertEqual(self.app.select_project(body)['campaign'],str(self.campaign))
        (self.campaign/'notes.txt').write_text('keep me',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'空项目目录'):
            self.app.select_project(body)
        self.assertEqual(guard.read_bytes(),b'\0')

    def test_cloud_campaign_and_qwen_result_cannot_start_local_pipeline(self):
        self.make_samples()
        with patch.object(self.s,'run_pipeline') as run:
            self.assertFalse(self.app.state()['actions']['local'])
            with self.assertRaises(ValueError):self.app.start({'action':'local'})
            run.assert_not_called()
        (self.campaign/'campaign.json').unlink()
        state={'config':{'source':str(self.source),'project':str(self.campaign),'asr_provider':'qwen_asr'},
               'identity':{'engine':'qwen_asr'}}
        (self.campaign/'state.json').write_text(json.dumps(state),encoding='utf-8')
        with patch.object(self.s,'run_pipeline') as run:
            self.assertFalse(self.app.state()['actions']['local'])
            with self.assertRaises(ValueError):self.app.start({'action':'local'})
            run.assert_not_called()

    def test_failed_run_restores_redacted_bounded_summary_after_restart_and_project_switch(self):
        self.make_samples()
        self.app._redactions=['secret-do-not-return']
        self.app.job.update(action='samples',status='needs_attention',message='failure secret-do-not-return',
                            total=3,recognized=1,translated=0,
                            logs=['log secret-do-not-return '+str(n)+'x'*800 for n in range(70)])
        self.app._persist()
        self.assertNotIn('secret-do-not-return',self.store.read_text(encoding='utf-8'))
        restored=self.s.StudioController(state_path=self.store)
        job=restored.state()['job']
        self.assertEqual(job['status'],'needs_attention')
        self.assertEqual((job['recognized'],job['total']),(1,3))
        self.assertTrue(job['restored'])
        self.assertLessEqual(len(job['logs']),30)
        self.assertTrue(all(len(line)<=500 for line in job['logs']))
        other=self.root/'other-project';other.mkdir()
        (other/'state.json').write_text(json.dumps({'config':{'source':str(self.source),'project':str(other)},'status':'cancelled'}),encoding='utf-8')
        self.assertNotEqual(restored.select_project({'campaign':str(other)})['job']['status'],'needs_attention')
        back=restored.select_project({'campaign':str(self.campaign)})
        self.assertEqual(back['job']['message'],'failure [已隐藏]')
        self.assertEqual(back['job']['recognized'],1)

    def test_interrupted_summary_is_restored_as_attention_without_launch_or_ledger_changes(self):
        self.make_samples()
        ledger=self.campaign/'费用账本.json'
        ledger.write_text('{"sentinel":"unknown request kept"}',encoding='utf-8')
        original=ledger.read_bytes()
        self.app.job.update(action='samples',busy=True,status='running',total=3,recognized=1)
        self.app._persist()
        with patch.object(self.s,'run_pipeline') as local,patch.object(self.s,'start_process') as cloud:
            reopened=self.s.StudioController(state_path=self.store)
            result=reopened.state()
            self.assertFalse(result['job']['busy'])
            self.assertEqual(result['job']['status'],'needs_attention')
            self.assertTrue(result['job']['interrupted'])
            self.assertIn('中断',result['job']['message'])
            local.assert_not_called();cloud.assert_not_called()
        self.assertEqual(ledger.read_bytes(),original)
        self.assertFalse((self.campaign/'approval.json').exists())

    def test_summary_alone_cannot_restore_generated_success_or_approval(self):
        self.app.job.update(action='samples',status='samples_ready',total=3,recognized=3,translated=3)
        self.app._persist()
        result=self.s.StudioController(state_path=self.store).state()
        self.assertNotEqual(result['job']['status'],'samples_ready')
        self.assertFalse(result['actions']['approve'])
        self.assertFalse(result['approval_exists'])

    def test_summary_io_failure_does_not_fail_completed_local_work(self):
        with patch.object(self.s,'atomic_json',side_effect=OSError('synthetic storage unavailable')), \
                patch.object(self.s,'run_pipeline',return_value={'status':'complete','message':'generated','total':1,'recognized':1,'translated':0}):
            self.app.start({'action':'local','translate':False})
            self.app.worker.join(3)
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.job['status'],'complete')
        self.assertTrue(self.app.state()['persistence_warning'])

    def test_final_summary_is_redacted_before_runtime_keys_are_cleared(self):
        def finish(*args,**kwargs):
            self.app.job['message']='last status secret-do-not-return'
            self.app.job['logs']=['last log secret-do-not-return']
            return {'status':'complete','total':1,'recognized':1}
        with patch.object(self.s,'run_pipeline',side_effect=finish):
            self.app.start({'action':'local','translate':False})
            self.app.worker.join(3)
        self.assertNotIn('secret-do-not-return',self.store.read_text(encoding='utf-8'))
        self.assertEqual(self.app._redactions,[])

    def test_progress_keeps_live_messages_and_logs_bounded_before_finish(self):
        for index in range(155):
            self.app._progress({'message':f'line {index} '+('x'*10000),
                                'recognized':index,'translated':index-1})
        job=self.app.state()['job']
        self.assertEqual((job['recognized'],job['translated']),(154,153))
        self.assertEqual(len(job['logs']),150)
        self.assertLessEqual(len(job['message']),800)
        self.assertTrue(all(len(line)<=500 for line in job['logs']))
        self.assertIn('line 5 ',job['logs'][0])
        self.assertIn('line 154 ',job['logs'][-1])
        self.assertLessEqual(sum(map(len,job['logs'])),75000)

    def test_live_progress_redacts_before_truncation_and_deduplicates_displayed_text(self):
        registered='saved-provider-secret-never-return'
        unregistered='sk-fake-debug-token-12345678'
        self.app._redactions=[registered]
        message='x'*470+' '+registered+' api_key='+unregistered+' '+('tail '*1000)
        self.app._progress({'message':message})
        self.app._progress({'message':message})
        job=self.app.state()['job']
        self.assertEqual(len(job['logs']),1)
        self.assertNotIn(registered,json.dumps(job))
        self.assertNotIn(unregistered,json.dumps(job))
        self.assertIn('[已隐藏]',job['message'])
        self.assertIn('[已隐藏]',job['logs'][0])
        self.assertLessEqual(len(job['logs'][0]),500)
        previous=job['message']
        self.app._progress({'recognized':4})
        self.assertEqual(self.app.job['message'],previous)

    def test_worker_launch_failure_releases_busy_and_persists_recoverable_failure(self):
        with patch.object(self.s.threading.Thread,'start',side_effect=RuntimeError('no worker')), \
                patch.object(self.s,'run_pipeline') as pipeline:
            with self.assertRaises(RuntimeError):
                self.app.start({'action':'local','translate':False})
        pipeline.assert_not_called()
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.job['status'],'needs_attention')
        self.assertEqual(self.app._redactions,[])
        again=self.s.StudioController(state_path=self.store)
        self.assertFalse(again.job['busy'])
        self.assertFalse(again.job['interrupted'])
        self.assertIn('启动',again.job['message'])

    def test_stop_marker_cleanup_failure_cannot_leave_finished_job_busy(self):
        marker=self.root/'blocked.stop';marker.write_text('stop')
        self.app._cancel_marker=marker
        self.app.job.update(busy=True,action='prepare',status='running')
        self.app._redactions=['secret-do-not-return']
        real_unlink=Path.unlink
        def unlink(path,*args,**kwargs):
            if path==marker:raise PermissionError('synthetic marker in use')
            return real_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink), \
                patch.object(self.s,'start_process',return_value=Mock(stdout=io.StringIO(''),wait=Mock(return_value=1),returncode=1)):
            self.app._execute('prepare',[])
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.job['status'],'needs_attention')
        self.assertEqual(self.app._redactions,[])
        self.assertIsNone(self.app._cancel_marker)
        self.assertIn('停止标记',self.app.job['message'])
        self.assertFalse(self.s.StudioController(state_path=self.store).job['busy'])

    def test_non_dictionary_config_and_invalid_cloud_source_fail_load_without_selection_changes(self):
        self.app._persist();before=self.store.read_bytes()
        invalid=self.root/'invalid-project';invalid.mkdir()
        cloud={'source':{'path':str(self.source)},'asr_provider':'qwen_asr','status':'prepared'}
        (invalid/'campaign.json').write_text(json.dumps(cloud),encoding='utf-8')
        for config in (None,[],['bad'],42,'bad',{}):
            (invalid/'state.json').write_text(json.dumps({'config':config}),encoding='utf-8')
            with self.subTest(config=config),self.assertRaises(ValueError):
                self.app.select_project({'campaign':str(invalid)})
            self.assertEqual(self.app.campaign,str(self.campaign))
            self.assertEqual(self.store.read_bytes(),before)

    def test_explicit_load_rejects_missing_plain_and_corrupt_projects_without_changing_selection(self):
        self.app._persist()
        before=self.store.read_bytes()
        empty=self.root/'plain';empty.mkdir()
        corrupt=self.root/'corrupt';corrupt.mkdir()
        (corrupt/'state.json').write_text('{"config":{"source":42}}',encoding='utf-8')
        invalid_cloud=self.root/'invalid-cloud';invalid_cloud.mkdir()
        (invalid_cloud/'campaign.json').write_text(json.dumps({'source':{'path':{}},'status':'prepared'}),encoding='utf-8')
        for folder in (self.root/'missing',empty,corrupt,invalid_cloud):
            with self.subTest(folder=folder.name),self.assertRaisesRegex(ValueError,'不存在|字幕项目|损坏|配置'):
                self.app.select_project({'campaign':str(folder)})
            self.assertEqual(self.app.campaign,str(self.campaign))
            self.assertEqual(self.app.source,str(self.source))
            self.assertEqual(self.store.read_bytes(),before)

    def test_history_marks_missing_directory_without_removing_record_or_writing_state(self):
        missing=self.root/'gone'
        self.app.recent=[{'path':str(missing),'title':'lost','status':'complete','updated':1}]
        result=self.app.state()
        self.assertTrue(result['recent'][0]['missing'])
        self.assertEqual(len(self.app.recent),1)
        self.assertFalse(self.store.exists())

    def test_cloud_child_receives_own_stop_file_until_it_exits(self):
        baseline=self.root/'old.srt';baseline.write_text('',encoding='utf-8')
        self.app.baseline=str(baseline)
        created=threading.Event();release=threading.Event();captured=[]
        def process(command,*,environ):
            marker=Path(environ['SUBTITLE_STUDIO_STOP_FILE'])
            captured.append(marker);created.set()
            if not release.wait(3):raise AssertionError('parent did not release child')
            return Mock(stdout=io.StringIO(''),wait=Mock(return_value=2),returncode=2)
        with patch.object(self.s,'start_process',side_effect=process):
            self.app.start({'action':'prepare'})
            try:
                self.assertTrue(created.wait(3))
                self.app.stop()
                self.assertTrue(captured[0].is_file())
                self.assertNotEqual(captured[0],self.campaign/'STOP.flag')
            finally:
                release.set();self.app.worker.join(3)
        self.assertEqual(self.app.job['status'],'cancelled')
        self.assertFalse(captured[0].exists())

    def test_cloud_output_failure_keeps_busy_and_stop_marker_until_child_exits(self):
        baseline=self.root/'old.srt';baseline.write_text('',encoding='utf-8')
        self.app.baseline=str(baseline)
        reading=threading.Event();fail_output=threading.Event()
        waiting=threading.Event();exit_child=threading.Event();closed=threading.Event()
        captured=[]
        class Output:
            def __iter__(self):
                reading.set()
                if not fail_output.wait(3):raise AssertionError('output failure was not released')
                raise OSError('synthetic child output failure')
                yield
            def close(self):closed.set()
        def wait():
            waiting.set()
            if not exit_child.wait(3):raise AssertionError('fake child was not released')
            return 2
        def process(command,*,environ):
            captured.append(Path(environ['SUBTITLE_STUDIO_STOP_FILE']))
            return Mock(stdout=Output(),wait=wait,returncode=2)
        with patch.object(self.s,'start_process',side_effect=process):
            self.app.start({'action':'prepare'})
            try:
                self.assertTrue(reading.wait(3))
                fail_output.set()
                self.assertTrue(waiting.wait(1),'controller did not retain ownership of its child')
                self.assertTrue(self.app.job['busy'])
                self.assertTrue(self.app.stop_event.is_set())
                self.assertTrue(captured[0].is_file())
                self.assertTrue(closed.is_set())
                with self.assertRaisesRegex(ValueError,'已有任务'):
                    self.app.start({'action':'prepare'})
            finally:
                fail_output.set();exit_child.set();self.app.worker.join(3)
        self.assertFalse(self.app.worker.is_alive())
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.job['status'],'needs_attention')
        self.assertFalse(captured[0].exists())

    def test_progress_failure_drains_healthy_child_output_before_wait_and_retains_error(self):
        baseline=self.root/'old.srt';baseline.write_text('',encoding='utf-8')
        self.app.baseline=str(baseline)
        drained=threading.Event();waiting=threading.Event();exit_child=threading.Event()
        markers=[]
        def output():
            yield '{"message":"synthetic progress failure"}\n'
            for _ in range(10):yield '{"message":"later output"}\n'
            drained.set()
        def wait():
            waiting.set()
            if not exit_child.wait(3):raise AssertionError('fake child was not released')
            return 2
        def process(command,*,environ):
            markers.append(Path(environ['SUBTITLE_STUDIO_STOP_FILE']))
            return Mock(stdout=output(),wait=wait,returncode=2)
        progress=self.app._progress
        def fail_progress(data):
            if data.get('message')=='synthetic progress failure':
                raise RuntimeError('original progress error')
            progress(data)
        with patch.object(self.s,'start_process',side_effect=process), \
                patch.object(self.app,'_progress',side_effect=fail_progress):
            self.app.start({'action':'prepare'})
            try:
                self.assertTrue(waiting.wait(3))
                self.assertTrue(drained.is_set(),'healthy stdout was abandoned before child.wait')
                self.assertTrue(self.app.job['busy'])
                self.assertTrue(markers[0].is_file())
            finally:
                exit_child.set();self.app.worker.join(3)
        self.assertFalse(self.app.worker.is_alive())
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.job['status'],'needs_attention')
        self.assertEqual(self.app.job['message'],'original progress error')
        self.assertFalse(markers[0].exists())

    def test_failed_cloud_stop_marker_does_not_mark_natural_completion_cancelled(self):
        self.app.job.update(busy=True,action='prepare',status='running')
        marker=self.root/'stops'/'failed.stop'
        self.app._cancel_marker=marker
        write_text=Path.write_text
        def write(path,*args,**kwargs):
            if path==marker:raise PermissionError('synthetic stop marker unavailable')
            return write_text(path,*args,**kwargs)
        with patch.object(Path,'write_text',write), self.assertRaises(PermissionError):
            self.app.stop()
        self.assertFalse(self.app.stop_event.is_set())
        self.assertTrue(self.app.job['busy'])
        self.assertFalse(marker.exists())
        (self.campaign/'campaign.json').write_text('{"status":"prepared"}',encoding='utf-8')
        with patch.object(self.s,'start_process',return_value=Mock(stdout=io.StringIO(''),wait=Mock(return_value=0),returncode=0)):
            self.app._execute('prepare',[])
        self.assertEqual(self.app.job['status'],'prepared')
        self.assertFalse(self.app.job['busy'])

    def test_worker_result_uses_the_exit_code_after_ownership_cleanup(self):
        self.app.job.update(busy=True,action='prepare',status='running')
        (self.campaign/'campaign.json').write_text('{"status":"prepared"}',encoding='utf-8')
        child=Mock(stdout=io.StringIO(''),wait=Mock(return_value=0),returncode=0)
        @contextmanager
        def owner(process):
            yield process
            process.returncode=7
        with patch.object(self.s,'start_process',return_value=child), \
                patch.object(self.s,'owned_process',owner,create=True), \
                patch.object(self.s,'wait_for_process_exit',return_value=True,create=True):
            self.app._execute('prepare',[])
        self.assertEqual(self.app.job['status'],'needs_attention')
        self.assertTrue(child.stdout.closed)
        self.assertFalse(self.app.job['busy'])

    def test_worker_setup_and_pipe_cleanup_failure_keep_the_original_error(self):
        self.app.job.update(busy=True,action='prepare',status='running')
        primary=RuntimeError('original worker ownership setup error')
        class Output(io.StringIO):
            def close(self):
                if self.closed:return
                super().close()
                raise OSError('secondary pipe close error')
        child=Mock(stdout=Output(''),wait=Mock(return_value=0),returncode=1)
        @contextmanager
        def failed_owner(process):
            raise primary
            yield process
        with patch.object(self.s,'start_process',return_value=child), \
                patch.object(self.s,'owned_process',failed_owner,create=True), \
                patch.object(self.s,'wait_for_process_exit',return_value=True,create=True):
            self.app._execute('prepare',[])
        self.assertEqual(self.app.job['message'],str(primary))
        self.assertTrue(child.stdout.closed)
        self.assertTrue(any('secondary pipe close error' in note for note in primary.__notes__))
        self.assertFalse(self.app.job['busy'])

    def test_unconfirmed_worker_exit_keeps_busy_and_marker_until_native_exit(self):
        self.app.job.update(busy=True,action='prepare',status='running')
        marker=self.root/'stops'/'unconfirmed.stop'
        self.app._cancel_marker=marker
        child=Mock(stdout=io.StringIO(''),wait=Mock(return_value=0),returncode=None)
        waiting=threading.Event();release=threading.Event()
        @contextmanager
        def failed_cleanup(process):
            yield process
            raise RuntimeError('worker cleanup did not confirm exit')
        def observe(process,milliseconds):
            if milliseconds==0:return False
            waiting.set()
            if not release.wait(3):raise AssertionError('synthetic native exit was not released')
            process.returncode=1
            return True
        with patch.object(self.s,'start_process',return_value=child), \
                patch.object(self.s,'owned_process',failed_cleanup,create=True), \
                patch.object(self.s,'wait_for_process_exit',side_effect=observe,create=True):
            worker=threading.Thread(target=self.app._execute,args=('prepare',[]))
            worker.start()
            try:
                self.assertTrue(waiting.wait(1),'controller released worker without confirming its exit')
                self.assertTrue(self.app.job['busy'])
                self.assertFalse(self.app.idle_ready())
                self.assertTrue(marker.is_file())
                self.assertEqual(self.app.job['status'],'needs_attention')
            finally:
                release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.app.job['busy'])
        self.assertFalse(marker.exists())
        self.assertIn('worker cleanup',self.app.job['message'])
        self.assertNotIn('正在等待回收',self.app.job['message'])
        self.assertIn('后台进程已退出',self.app.job['message'])

    def test_exit_observation_and_reporting_errors_cannot_release_live_worker(self):
        self.app.job.update(busy=True,action='prepare',status='running')
        marker=self.root/'stops'/'unconfirmed-error.stop'
        marker.parent.mkdir();marker.write_text('synthetic stop')
        self.app._cancel_marker=marker
        child=Mock(stdout=io.StringIO(''),wait=Mock(return_value=0),returncode=None)
        waiting=threading.Event();release=threading.Event();attempts=[]
        @contextmanager
        def failed_cleanup(process):
            yield process
            raise RuntimeError('original cleanup error')
        def observe(process,milliseconds):
            attempts.append(milliseconds)
            if len(attempts)==1:raise OSError('synthetic native observation error')
            waiting.set()
            if not release.wait(3):raise AssertionError('synthetic exit was not released')
            process.returncode=1
            return True
        progress=self.app._progress
        def report(data):
            if '退出状态尚未确认' in data.get('message',''):
                raise OSError('synthetic reporting error')
            progress(data)
        with patch.object(self.s,'start_process',return_value=child), \
                patch.object(self.s,'owned_process',failed_cleanup,create=True), \
                patch.object(self.s,'wait_for_process_exit',side_effect=observe,create=True), \
                patch.object(self.app,'stop',side_effect=OSError('synthetic stop reporting error')), \
                patch.object(self.app,'_progress',side_effect=report):
            worker=threading.Thread(target=self.app._execute,args=('prepare',[]))
            worker.start()
            try:
                self.assertTrue(waiting.wait(1),'native observation did not retain the live child')
                self.assertTrue(self.app.job['busy'])
                self.assertTrue(marker.exists())
            finally:
                release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.app.job['busy'])
        self.assertFalse(marker.exists())
        self.assertEqual(attempts,[0,250])


class TransportTests(unittest.TestCase):
    def file_handler(self,headers=None):
        from subtitle_pipeline.studio import make_handler
        handler=object.__new__(make_handler(None,'token','127.0.0.1:7777',Path('.')))
        handler.headers=headers or {}
        handler.wfile=io.BytesIO()
        handler.send_response=Mock()
        handler.send_header=Mock()
        handler.end_headers=Mock()
        return handler

    def test_empty_subtitle_download_is_success_but_empty_range_is_unsatisfiable(self):
        with tempfile.TemporaryDirectory() as temporary:
            subtitle=Path(temporary)/'原文.srt';subtitle.write_bytes(b'')
            handler=self.file_handler()
            handler.file_response(subtitle,download=True)
            handler.send_response.assert_called_once_with(200)
            self.assertIn(('Content-Length','0'),[call.args for call in handler.send_header.call_args_list])
            self.assertEqual(handler.wfile.getvalue(),b'')
            ranged=self.file_handler({'Range':'bytes=0-'})
            ranged.file_response(subtitle)
            ranged.send_response.assert_called_once_with(416)

    def test_file_headers_and_bytes_use_the_same_opened_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            subtitle=Path(temporary)/'原文.srt';subtitle.write_bytes(b'old')
            real_open=Path.open
            def replace_before_open(path,*args,**kwargs):
                if path==subtitle and args==('rb',):
                    with real_open(path,'wb') as out:out.write(b'new content')
                return real_open(path,*args,**kwargs)
            handler=self.file_handler()
            with patch.object(Path,'open',replace_before_open):
                handler.file_response(subtitle)
            headers=dict(call.args for call in handler.send_header.call_args_list)
            self.assertEqual(int(headers['Content-Length']),len(b'new content'))
            self.assertEqual(handler.wfile.getvalue(),b'new content')

    def test_reject_cross_origin_wrong_host_and_unknown_token(self):
        from subtitle_pipeline.studio import authorize_request
        good={'Host':'127.0.0.1:7777','Origin':'http://127.0.0.1:7777','X-Subtitle-Token':'test-token'}
        self.assertTrue(authorize_request(good,'test-token','127.0.0.1:7777'))
        for change in ({'Origin':'https://evil.example'},{'Host':'evil.example'},{'X-Subtitle-Token':'bad'}):
            self.assertFalse(authorize_request({**good,**change},'test-token','127.0.0.1:7777'))
        self.assertTrue(authorize_request({'Host':good['Host'],'Cookie':'subtitle_session_7777=test-token'},'test-token',good['Host']))
        self.assertFalse(authorize_request({'Host':good['Host'],'Cookie':'subtitle_session_8888=test-token'},'test-token',good['Host']))

    def test_video_range_and_invalid_ranges(self):
        from subtitle_pipeline.studio import byte_range
        self.assertEqual(byte_range('bytes=10-19',100),(10,19))
        self.assertEqual(byte_range('bytes=10-',100),(10,99))
        self.assertEqual(byte_range('bytes=-10',100),(90,99))
        self.assertEqual(byte_range(None,100),(0,99))
        for value in ('bytes=100-101','bytes=30-10','bytes=0-10,20-30','bytes=-0','bad'):
            with self.assertRaises(ValueError):byte_range(value,100)


class RunningInstanceTests(unittest.TestCase):
    def setUp(self):
        from subtitle_pipeline import studio
        self.s=studio
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.runtime=self.root/'runtime.json'
        self.existing={'url':'http://127.0.0.1:43210/#token=synthetic-instance-review-token','pid':4242}
        self.runtime.write_text(json.dumps(self.existing),encoding='utf-8')
        self.connection=Mock()
        self.connection.getresponse.return_value=Mock(status=200,read=Mock(return_value=b'{"app":"\\u5b57\\u5e55\\u5de5\\u574a"}'))
        for guard in (patch.object(studio,'pid_alive',return_value=True),
                      patch.object(studio.http.client,'HTTPConnection',return_value=self.connection)):
            guard.start();self.addCleanup(guard.stop)

    def test_live_response_timeout_reconnects_without_starting_second_server(self):
        self.connection.getresponse.side_effect=TimeoutError('server is still starting')
        static=self.root/'frontend'/'dist';static.mkdir(parents=True)
        (static/'index.html').write_text('synthetic frontend',encoding='utf-8')
        with patch.object(self.s,'ROOT',self.root),patch.object(self.s,'data_directory',return_value=self.root), \
                patch.object(self.s,'StudioController') as controller, \
                patch.object(self.s,'ThreadingHTTPServer',side_effect=AssertionError('Duplicate server started')), \
                patch.object(self.s,'open_desktop_window') as open_window,redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.s.main([]),0)
        controller.assert_not_called()
        open_window.assert_called_once_with(self.existing['url'])
        self.assertTrue(json.loads(output.getvalue())['reused'])
        self.assertEqual(json.loads(self.runtime.read_text(encoding='utf-8')),self.existing)

    def test_live_connect_timeout_does_not_look_like_absent_service(self):
        self.connection.request.side_effect=TimeoutError('loopback listen queue is busy')
        self.assertEqual(self.s.running_instance(self.runtime),self.existing)

    def test_live_unexpected_response_blocks_duplicate_service(self):
        for status,body in ((503,b'{}'),(403,b'{}'),(200,b'[]'),(200,b'{"app":"other"}'),(200,b'not JSON')):
            with self.subTest(status=status,body=body):
                self.connection.getresponse.return_value=Mock(status=status,read=Mock(return_value=body))
                with self.assertRaisesRegex(RuntimeError,'运行'):
                    self.s.running_instance(self.runtime)

    def test_live_disconnected_response_blocks_duplicate_service(self):
        self.connection.getresponse.side_effect=self.s.http.client.RemoteDisconnected('synthetic unavailable reply')
        with self.assertRaisesRegex(RuntimeError,'运行'):
            self.s.running_instance(self.runtime)

    def test_refused_connection_allows_fresh_service(self):
        self.connection.request.side_effect=ConnectionRefusedError('no listener')
        self.assertIsNone(self.s.running_instance(self.runtime))

    def test_dead_process_does_not_probe_stale_port(self):
        with patch.object(self.s,'pid_alive',return_value=False):
            self.assertIsNone(self.s.running_instance(self.runtime))
        self.connection.request.assert_not_called()

    def test_process_exit_during_timeout_allows_fresh_service(self):
        self.connection.getresponse.side_effect=TimeoutError('process exited')
        with patch.object(self.s,'pid_alive',side_effect=[True,False]):
            self.assertIsNone(self.s.running_instance(self.runtime))


if __name__=='__main__':unittest.main()
