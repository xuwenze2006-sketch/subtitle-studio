"""Selection admission with real temporary project reads and isolated worker doubles."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio


class StudioSelectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-selection-concurrency-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.wav'; self.source.write_bytes(b'synthetic source')
        self.projects = {name:self.root/name for name in ('a','b','c')}
        for project in self.projects.values():
            project.mkdir()
            (project/'state.json').write_text(json.dumps({'status':'complete',
                'identity':{'engine':'whisper_cpp'}, 'config':{'source':str(self.source),
                'project':str(project),'translate':False}}), encoding='utf-8')
        for name in ('campaign.json','硅基流动试听状态.json'):
            (self.projects['b']/name).write_text('{}', encoding='utf-8')
        def blocked(*args, **kwargs):
            raise AssertionError('No network allowed in selection concurrency tests')
        for guard in (patch.object(studio,'account_environment',return_value={}),
                      patch.object(studio,'encrypted_names',return_value=set()),
                      patch.object(studio,'engine_available',return_value=True),
                      patch.object(studio,'start_process',side_effect=blocked),
                      patch.object(socket.socket,'connect',side_effect=blocked),
                      patch.object(socket.socket,'connect_ex',side_effect=blocked),
                      patch.object(socket,'create_connection',side_effect=blocked)):
            guard.start(); self.addCleanup(guard.stop)
        self.stores = 0
        self.new_app()

    def new_app(self):
        self.stores += 1; self.store = self.root/f'studio-{self.stores}.json'
        self.app = studio.StudioController(source=str(self.source),campaign=str(self.projects['a']),
                                           state_path=self.store)
        self.app._redactions = ['synthetic-run-secret']
        self.app.job.update(action='local',status='cancelled',recognized=7,
                            message='outgoing synthetic-run-secret')
        view = self.app._snapshot_view_locked(); view.campaign = str(self.projects['b'])
        self.target_id = view._project_id()
        self.app._last_runs[self.target_id] = {'action':'local','status':'complete','busy':False,
            'message':'stored result','total':2,'recognized':2,'translated':0,'logs':[], 'interrupted':False}

    def snapshot(self):
        return (self.app.source,self.app.campaign,self.app.baseline,deepcopy(self.app.job),
                self.app.stop_event,self.app._cancel_marker,self.app.worker,list(self.app._redactions),
                deepcopy(self.app._last_runs),deepcopy(self.app.recent),self.app._selection_revision,
                self.app._persist_revision,self.app._persist_pending)

    @contextmanager
    def delayed_read(self, filename='state.json', number=1, target=None):
        target = target or self.projects['b']/filename
        entered,release = threading.Event(),threading.Event(); reads = []
        original = studio.read_json
        def read(path,**kwargs):
            if Path(path)==target and threading.current_thread().name.startswith('pending-select'):
                reads.append(path)
                if len(reads)==number:
                    entered.set()
                    if not release.wait(5): raise AssertionError('selection read not released')
            return original(path,**kwargs)
        with patch.object(studio,'read_json',side_effect=read):
            try: yield entered,release
            finally: release.set()

    def test_initial_and_restored_project_reads_leave_controls_responsive_before_commit(self):
        # Removing only the initial JSON reads misses the restored completion evidence.
        for filename,number in (('state.json',1),('campaign.json',1),('state.json',2),
                                ('campaign.json',2),('硅基流动试听状态.json',1)):
            with self.subTest(filename=filename,number=number):
                self.new_app(); before = self.snapshot()
                with self.delayed_read(filename,number) as (entered,release), \
                        ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-select') as selectors, \
                        ThreadPoolExecutor(max_workers=3) as controls:
                    pending = selectors.submit(self.app.select_project,{'campaign':str(self.projects['b'])})
                    try:
                        self.assertTrue(entered.wait(2))
                        progress = controls.submit(self.app.progress)
                        touched = controls.submit(self.app.touch,SimpleNamespace(last_touch=0.0))
                        stopped = controls.submit(self.app.stop)
                        done,_ = wait((progress,touched,stopped),timeout=.3)
                        self.assertEqual(len(done),3,'selection read retained the live controller lock')
                        self.assertTrue(touched.result()); self.assertEqual(stopped.result(),{'stopped':False})
                        self.assertEqual(progress.result()['campaign'],str(self.projects['a']))
                        self.assertEqual(self.snapshot(),before)
                    finally: release.set()
                    result = pending.result(timeout=3)
                self.assertEqual(result['job']['status'],'complete')
                self.assertEqual(result['job']['recognized'],2)

    def test_selection_path_resolution_and_json_reads_never_own_the_live_lock(self):
        original_resolve,original_read = Path.resolve,studio.read_json
        def resolve(path,*args,**kwargs):
            self.assertFalse(self.app.lock._is_owned(),'selection resolved a filesystem path under the live lock')
            return original_resolve(path,*args,**kwargs)
        def read(path,**kwargs):
            self.assertFalse(self.app.lock._is_owned(),'selection read project JSON under the live lock')
            return original_read(path,**kwargs)
        old_id = self.app._project_id()
        with patch.object(Path,'resolve',resolve), patch.object(studio,'read_json',side_effect=read):
            result = self.app.select_project({'campaign':str(self.projects['b'])})
        self.assertEqual(result['job']['recognized'],2)
        self.assertEqual(self.app._last_runs[old_id]['message'],'outgoing [已隐藏]')
        self.assertEqual(json.loads(self.store.read_text(encoding='utf-8'))['campaign'],str(self.projects['b']))

    def test_competing_selection_and_same_token_aba_reject_stale_preflight(self):
        for aba in (False,True):
            with self.subTest(aba=aba):
                self.new_app(); old_id = self.app._project_id()
                original_summary = deepcopy(self.app._last_runs[self.target_id])
                def change():
                    result = self.app.select_project({'campaign':str(self.projects['c'])})
                    if aba: result = self.app.select_project({'campaign':str(self.projects['a'])})
                    return result
                with self.delayed_read() as (entered,release), \
                        ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-select') as selectors, \
                        ThreadPoolExecutor(max_workers=1) as controls:
                    pending = selectors.submit(self.app.select_project,{'campaign':str(self.projects['b'])})
                    try:
                        self.assertTrue(entered.wait(2)); winner = controls.submit(change)
                        done,_ = wait((winner,),timeout=.3)
                        self.assertEqual(len(done),1,'competing selection waited for detached load')
                        selected = winner.result(); after = self.snapshot(); saved = self.store.read_bytes()
                    finally: release.set()
                    with self.assertRaises(studio.ProjectSelectionChanged): pending.result(timeout=3)
                self.assertEqual(self.snapshot(),after); self.assertEqual(self.store.read_bytes(),saved)
                self.assertEqual(self.app._last_runs[self.target_id],original_summary)
                self.assertEqual(self.app._last_runs[old_id]['recognized'],7)
                self.assertEqual(selected['campaign'],str(self.projects['a' if aba else 'c']))
                self.assertEqual(self.app._project_id()==old_id,aba)

    def test_admitted_run_and_already_finished_run_both_invalidate_pending_selection(self):
        for finished in (False,True):
            with self.subTest(finished=finished):
                self.new_app(); token = self.app._project_id()
                worker_started,worker_exit = threading.Event(),threading.Event()
                if finished: worker_exit.set()
                def run(config,stop,on_progress):
                    worker_started.set()
                    if not worker_exit.wait(5): raise AssertionError('synthetic worker not released')
                    return {'status':'complete','recognized':11}
                with patch.object(studio,'run_pipeline',side_effect=run), \
                        self.delayed_read() as (entered,release), \
                        ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-select') as selectors, \
                        ThreadPoolExecutor(max_workers=1) as controls:
                    pending = selectors.submit(self.app.select_project,{'campaign':str(self.projects['b'])})
                    try:
                        self.assertTrue(entered.wait(2))
                        winner = controls.submit(self.app.start,{'action':'local','project_id':token})
                        done,_ = wait((winner,),timeout=.3)
                        self.assertEqual(len(done),1,'run admission waited for detached selection load')
                        self.assertTrue(winner.result()['started']); self.assertTrue(worker_started.wait(2))
                        if finished:
                            self.app.worker.join(3); self.assertFalse(self.app.worker.is_alive())
                        after = self.snapshot(); saved = self.store.read_bytes(); release.set()
                        with self.assertRaisesRegex(RuntimeError if finished else ValueError,'任务|处理'):
                            pending.result(timeout=3)
                        self.assertEqual(self.snapshot(),after); self.assertEqual(self.store.read_bytes(),saved)
                    finally:
                        release.set(); worker_exit.set()
                        if self.app.worker is not None: self.app.worker.join(3)

    def test_idle_shutdown_wins_before_selection_commit_and_rejects_late_selection(self):
        before = self.snapshot()
        with self.delayed_read() as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-select') as selectors, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending = selectors.submit(self.app.select_project,{'campaign':str(self.projects['b'])})
            try:
                self.assertTrue(entered.wait(2))
                shutdown = controls.submit(self.app.claim_idle_shutdown,SimpleNamespace(last_touch=time.monotonic()-181))
                done,_ = wait((shutdown,),timeout=.3)
                self.assertEqual(len(done),1,'idle shutdown waited for uncommitted selection')
                self.assertTrue(shutdown.result())
            finally: release.set()
            with self.assertRaisesRegex(RuntimeError,'重新打开'): pending.result(timeout=3)
        self.assertEqual(self.snapshot(),before); self.assertFalse(self.store.exists())
        with self.assertRaisesRegex(RuntimeError,'重新打开'):
            self.app.select_project({'campaign':str(self.projects['c'])})
        self.assertEqual(self.snapshot(),before)

    def test_concurrent_fresh_candidates_reserve_one_path_and_retry_allocates_next(self):
        base = self.root/'fresh'; body = {'source':str(self.source),'campaign':'','new_task':True}
        with patch.object(studio,'timestamped_project',return_value=base), \
                self.delayed_read(target=base/'state.json') as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-select') as selectors, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending = selectors.submit(self.app.select_project,body)
            try:
                self.assertTrue(entered.wait(2)); winner = controls.submit(self.app.select_project,body)
                done,_ = wait((winner,),timeout=.3)
                self.assertEqual(len(done),1,'fresh selection waited for detached candidate load')
                self.assertEqual(winner.result()['campaign'],str(base))
            finally: release.set()
            with self.assertRaises(studio.ProjectSelectionChanged): pending.result(timeout=3)
            self.assertEqual(sum(item['path']==str(base) for item in self.app.recent),1)
            retry = self.app.select_project(body)
        self.assertEqual(retry['campaign'],str(base.with_name('fresh-02')))
        self.assertFalse(base.exists()); self.assertFalse(Path(retry['campaign']).exists())

    def test_same_project_restores_latest_outgoing_summary(self):
        selected = self.app.select_project({'campaign':str(self.projects['a'])})
        self.assertEqual(selected['job']['recognized'],7)
        self.assertEqual(selected['job']['status'],'cancelled')
        self.assertEqual(selected['job']['message'],'outgoing [已隐藏]')
        self.assertTrue(selected['job']['restored'])

    def test_full_output_state_remains_authoritative_for_completion_evidence(self):
        full = self.projects['b']/'整片'; full.mkdir()
        (full/'state.json').write_text('{"status":"prepared"}',encoding='utf-8')
        selected = self.app.select_project({'campaign':str(self.projects['b'])})
        self.assertEqual(selected['job']['status'],'needs_attention')
        self.assertEqual(selected['job']['recognized'],2)
        self.assertTrue(selected['job']['restored'])


if __name__ == '__main__': unittest.main()
