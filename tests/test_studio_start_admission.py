"""Startup admission races with real temporary project reads and fake workers."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio


class StudioStartAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-start-admission-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source.wav'; self.source.write_bytes(b'synthetic source')
        self.project = self.root/'project'; self.project.mkdir()
        self.store = self.root/'studio.json'
        self.saved = {'status':'complete', 'identity':{'engine':'whisper_cpp'},
            'config':{'source':str(self.source),'project':str(self.project),'translate':False},
            'parts':{'0':{'asr':'done','fixture_padding':'x'*4096}}}
        self.write_state(self.saved)
        self.env = {'DEEPSEEK_API_KEY':'synthetic-secret-first'}
        self.runs = []
        self.worker_started = threading.Event(); self.worker_exit = threading.Event()
        self.addCleanup(self.worker_exit.set)
        self.app = studio.StudioController(source=str(self.source),campaign=str(self.project),state_path=self.store)
        def run(config, stop, on_progress):
            self.runs.append(config)
            self.worker_started.set()
            if not self.worker_exit.wait(5): raise AssertionError('fake worker not released')
            return {'status':'cancelled' if stop.is_set() else 'complete', 'recognized':1}
        def blocked(*args, **kwargs):
            raise AssertionError('No network allowed in startup admission tests')
        guards = (patch.object(studio,'account_environment',side_effect=lambda:dict(self.env)),
                  patch.object(studio,'encrypted_names',return_value=set()),
                  patch.object(studio,'engine_available',return_value=True),
                  patch.object(studio,'run_pipeline',side_effect=run),
                  patch.object(studio,'save_secrets',side_effect=lambda values:self.env.update(values)),
                  patch.object(studio,'write_public_environment',side_effect=lambda values:self.env.update(values)),
                  patch.object(socket.socket,'connect',side_effect=blocked),
                  patch.object(socket.socket,'connect_ex',side_effect=blocked),
                  patch.object(socket,'create_connection',side_effect=blocked))
        for guard in guards:
            guard.start(); self.addCleanup(guard.stop)
        self.addCleanup(self.finish_worker)

    def write_state(self, value):
        (self.project/'state.json').write_text(json.dumps(value),encoding='utf-8')

    @contextmanager
    def delayed_read(self, number=1):
        entered, release = threading.Event(), threading.Event()
        original = studio.read_json; reads = []
        def read(path):
            if (Path(path)==self.project/'state.json'
                    and threading.current_thread().name.startswith('pending-start')):
                reads.append(path)
                if len(reads)==number:
                    entered.set()
                    if not release.wait(5): raise AssertionError('preflight read not released')
            return original(path)
        with patch.object(studio,'read_json',side_effect=read):
            try: yield entered,release
            finally: release.set()

    def finish_worker(self):
        self.worker_exit.set()
        if self.app.worker is not None:
            self.app.worker.join(3)
            self.assertFalse(self.app.worker.is_alive())

    def snapshot(self):
        return (deepcopy(self.app.job),self.app.stop_event,self.app._cancel_marker,
                list(self.app._redactions),deepcopy(self.app._last_runs),self.app._persist_revision)

    def test_each_slow_startup_state_read_leaves_controls_responsive_before_admission(self):
        # The three reads cover cloud eligibility, state materialization, and local config.
        for number in (1,2,3):
            with self.subTest(read=number):
                self.app.job=self.app._idle_job(); self.worker_exit.clear(); self.worker_started.clear()
                before=self.snapshot()
                with self.delayed_read(number) as (entered,release), \
                        ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                        ThreadPoolExecutor(max_workers=3) as controls:
                    pending=starts.submit(self.app.start,{'action':'local','translate':False})
                    try:
                        self.assertTrue(entered.wait(2))
                        progress=controls.submit(self.app.progress)
                        touch=controls.submit(self.app.touch,SimpleNamespace(last_touch=0.0))
                        stop=controls.submit(self.app.stop)
                        done,_=wait((progress,touch,stop),timeout=.3)
                        self.assertEqual(len(done),3,'startup read retained the live controller lock')
                        self.assertEqual(stop.result(),{'stopped':False})
                        self.assertTrue(touch.result())
                        self.assertFalse(progress.result()['job']['busy'])
                        self.assertEqual(self.snapshot(),before)
                        self.assertFalse(self.worker_started.is_set())
                    finally: release.set()
                    self.assertTrue(pending.result(timeout=3)['started'])
                    self.finish_worker()

    def test_competing_start_admits_one_worker_and_rejects_pending_preflight(self):
        with self.delayed_read() as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending=starts.submit(self.app.start,{'action':'local'})
            try:
                self.assertTrue(entered.wait(2))
                winner=controls.submit(self.app.start,{'action':'local'})
                done,_=wait((winner,),timeout=.3)
                self.assertEqual(len(done),1,'competing admission waited for detached preflight')
                self.assertTrue(winner.result()['started'])
            finally: release.set()
            try:
                with self.assertRaisesRegex(ValueError,'已有任务'): pending.result(timeout=3)
                self.assertEqual(len(self.runs),1)
            finally: self.finish_worker()

    def test_late_project_identity_resolution_leaves_request_touch_responsive(self):
        token=self.app._project_id()
        original=studio.StudioController._project_id
        entered,release=threading.Event(),threading.Event(); calls=[]
        def identity(view):
            if threading.current_thread().name.startswith('pending-start'):
                calls.append(view)
                # With a request token: initial binding, state display identity,
                # then the final post-payload identity before atomic admission.
                if len(calls)==3:
                    entered.set()
                    if not release.wait(5): raise AssertionError('identity resolution not released')
            return original(view)
        with patch.object(studio.StudioController,'_project_id',identity), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending=starts.submit(self.app.start,{'action':'local','project_id':token})
            try:
                self.assertTrue(entered.wait(2))
                touched=controls.submit(self.app.touch,SimpleNamespace(last_touch=0.0))
                done,_=wait((touched,),timeout=.3)
                self.assertEqual(len(done),1,'project identity resolution held the live lock')
                self.assertTrue(touched.result())
            finally: release.set()
            self.assertTrue(pending.result(timeout=3)['started'])
            self.finish_worker()

    def test_project_token_is_revalidated_after_preflight_path_identity_changes(self):
        token=self.app._project_id(); changed=threading.Event()
        original=studio.StudioController._project_id
        def identity(view):
            value=original(view)
            if changed.is_set() and threading.current_thread().name.startswith('pending-start'):
                return '0'*64
            return value
        with patch.object(studio.StudioController,'_project_id',identity), \
                self.delayed_read() as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts:
            pending=starts.submit(self.app.start,{'action':'local','project_id':token})
            try:
                self.assertTrue(entered.wait(2)); changed.set()
            finally: release.set()
            with self.assertRaises(studio.ProjectSelectionChanged): pending.result(timeout=3)
        self.assertEqual(self.runs,[]); self.assertIsNone(self.app.worker)

    def test_competing_run_that_already_finished_still_invalidates_preflight(self):
        self.worker_exit.set()
        with self.delayed_read() as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending=starts.submit(self.app.start,{'action':'local'})
            try:
                self.assertTrue(entered.wait(2))
                winner=controls.submit(self.app.start,{'action':'local'})
                done,_=wait((winner,),timeout=.3)
                self.assertEqual(len(done),1,'competing admission waited for detached preflight')
                self.assertTrue(winner.result()['started']); self.finish_worker()
                self.assertFalse(self.app.job['busy'])
            finally: release.set()
            with self.assertRaisesRegex(RuntimeError,'任务.*变化'): pending.result(timeout=3)
        self.assertEqual(len(self.runs),1)

    def test_selection_aba_invalidates_pending_start_even_when_project_token_matches_again(self):
        other=self.root/'other'; other.mkdir()
        original=self.app._project_id()
        def switch():
            self.app.select_project({'source':str(self.source),'campaign':str(other)})
            self.app.select_project({'source':str(self.source),'campaign':str(self.project)})
        with self.delayed_read() as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending=starts.submit(self.app.start,{'action':'local','project_id':original})
            try:
                self.assertTrue(entered.wait(2))
                switched=controls.submit(switch)
                done,_=wait((switched,),timeout=.3)
                self.assertEqual(len(done),1,'selection waited for detached preflight')
                switched.result(); self.assertEqual(self.app._project_id(),original)
            finally: release.set()
            with self.assertRaises(studio.ProjectSelectionChanged): pending.result(timeout=3)
        self.assertEqual(self.runs,[]); self.assertIsNone(self.app.worker)

    def test_shutdown_during_detached_preflight_prevents_worker_admission(self):
        with self.delayed_read() as (entered,release), \
                ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                ThreadPoolExecutor(max_workers=1) as controls:
            pending=starts.submit(self.app.start,{'action':'local'})
            try:
                self.assertTrue(entered.wait(2))
                shutdown=controls.submit(self.app.claim_idle_shutdown,SimpleNamespace(last_touch=0.0))
                done,_=wait((shutdown,),timeout=.3)
                self.assertEqual(len(done),1,'shutdown waited for unadmitted preflight')
                self.assertTrue(shutdown.result())
            finally: release.set()
            with self.assertRaisesRegex(RuntimeError,'重新打开'): pending.result(timeout=3)
        self.assertEqual(self.runs,[]); self.assertIsNone(self.app.worker)

    def test_successful_account_or_pricing_change_invalidates_detached_preflight(self):
        changes=(lambda:self.app.save_credentials({'provider':'deepseek','key':'synthetic-secret-second'}),
                 lambda:self.app.save_settings({'confirmed':True,'pricing_reference':'Synthetic price fixture'}),
                 lambda:self.app.save_siliconflow_pricing({'confirmed':True,'price_per_second':.00022,
                     'pricing_reference':'Synthetic price fixture'}))
        for change in changes:
            with self.subTest(change=changes.index(change)), self.delayed_read(3) as (entered,release), \
                    ThreadPoolExecutor(max_workers=1,thread_name_prefix='pending-start') as starts, \
                    ThreadPoolExecutor(max_workers=1) as controls:
                pending=starts.submit(self.app.start,{'action':'local'})
                try:
                    self.assertTrue(entered.wait(2))
                    changed=controls.submit(change)
                    done,_=wait((changed,),timeout=.3)
                    self.assertEqual(len(done),1,'configuration write waited for detached preflight')
                    self.assertTrue(changed.result()['saved'])
                finally: release.set()
                with self.assertRaisesRegex(RuntimeError,'配置.*变化'): pending.result(timeout=3)
        self.assertEqual(self.runs,[]); self.assertIsNone(self.app.worker)
        self.assertEqual(self.app._redactions,[])

    def test_invalid_saved_config_and_unavailable_action_preserve_lifecycle_state(self):
        for action,changes in (('local',{'workers':3}),('samples',{})):
            with self.subTest(action=action):
                saved=deepcopy(self.saved); saved['config'].update(changes); self.write_state(saved)
                before=self.snapshot()
                with self.assertRaises(ValueError): self.app.start({'action':action})
                self.assertEqual(self.snapshot(),before)
                self.assertEqual(self.runs,[]); self.assertIsNone(self.app.worker)
                self.assertFalse(self.store.exists())

    def test_launch_failure_remains_recoverable_and_does_not_leak_preflight_secrets(self):
        with patch.object(studio.threading.Thread,'start',side_effect=RuntimeError('synthetic launch failure')):
            with self.assertRaisesRegex(RuntimeError,'启动'): self.app.start({'action':'local'})
        self.assertEqual(self.runs,[])
        self.assertFalse(self.app.job['busy']); self.assertIsNone(self.app.worker)
        self.assertEqual(self.app.job['status'],'needs_attention')
        self.assertEqual(self.app._redactions,[])
        text=self.store.read_text(encoding='utf-8')
        self.assertNotIn('synthetic-secret-first',text)
        restored=studio.StudioController(state_path=self.store)
        self.assertFalse(restored.job['busy']); self.assertFalse(restored.job['interrupted'])


if __name__=='__main__': unittest.main()
