"""Event-coordinated Studio summary writes; never touch a real project/account."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio


class StudioPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.source=self.root/'source.mp4';self.source.write_bytes(b'fixture')
        self.project=self.root/'project';self.project.mkdir()
        self.store=self.root/'studio.json'
        for guard in (patch.object(studio,'account_environment',return_value={}),
                      patch.object(studio,'encrypted_names',return_value=set()),
                      patch.object(studio,'engine_available',return_value=True)):
            guard.start();self.addCleanup(guard.stop)
        self.app=studio.StudioController(source=str(self.source),campaign=str(self.project),state_path=self.store)
        self.app.job.update(action='local',busy=True,status='running')
        self.app._last_saved_progress=0

    @contextmanager
    def slow_first_write(self,failures=()):
        entered=threading.Event();release=threading.Event();writes=[]
        original=studio.atomic_json
        def write(path,payload):
            if Path(path)!=self.store:return original(path,payload)
            writes.append((payload,deepcopy(payload)))
            number=len(writes)
            if number==1:
                entered.set()
                if not release.wait(6):raise AssertionError('summary write not released')
            if number in failures:raise OSError('synthetic summary failure')
            return original(path,payload)
        with patch.object(studio,'atomic_json',side_effect=write):
            try:yield entered,release,writes
            finally:release.set()

    def test_slow_progress_save_does_not_delay_stop_or_state(self):
        with self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=3) as pool:
            first=pool.submit(self.app._progress,{'recognized':1,'message':'first'})
            try:
                self.assertTrue(entered.wait(2))
                stopped=pool.submit(self.app.stop).result(timeout=2)
                self.assertTrue(stopped['stopping'])
                self.assertTrue(self.app.stop_event.is_set())
                current=pool.submit(self.app.state).result(timeout=2)
                self.assertTrue(current['job']['busy'])
                self.assertIn('正在停止',current['job']['message'])
            finally:release.set()
            first.result(timeout=2)

    def test_active_save_coalesces_latest_progress_and_final_without_mutating_first_snapshot(self):
        identity=self.app._project_id()
        with self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.app._progress,{'recognized':1,'message':'first'})
            try:
                self.assertTrue(entered.wait(2))
                def updates():
                    for count in range(2,42):
                        self.app._progress({'recognized':count,'message':f'progress {count}'})
                    self.app._progress({'status':'complete','message':'finished'})
                    self.app._finish_job()
                pool.submit(updates).result(timeout=2)
                self.assertFalse(self.app.job['busy'])
            finally:release.set()
            first.result(timeout=2)
        self.assertEqual(len(writes),2)
        self.assertEqual(writes[0][0],writes[0][1])
        self.assertEqual(writes[0][1]['last_runs'][identity]['recognized'],1)
        saved=json.loads(self.store.read_text(encoding='utf-8'))['last_runs'][identity]
        self.assertEqual(saved['recognized'],41)
        self.assertEqual(saved['status'],'complete')
        self.assertFalse(saved['busy'])

    def test_final_save_cannot_overwrite_a_new_selection(self):
        identity=self.app._project_id()
        self.app.job.update(status='complete',recognized=3)
        other=self.root/'other';other.mkdir()
        with self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
            finished=pool.submit(self.app._finish_job)
            try:
                self.assertTrue(entered.wait(2))
                selected=pool.submit(self.app.select_project,{'source':str(self.source),'campaign':str(other)}).result(timeout=2)
                self.assertEqual(selected['campaign'],str(other))
            finally:release.set()
            finished.result(timeout=2)
        saved=json.loads(self.store.read_text(encoding='utf-8'))
        self.assertEqual(saved['campaign'],str(other))
        self.assertEqual(saved['last_runs'][identity]['recognized'],3)
        self.assertFalse(saved['last_runs'][identity]['busy'])

    def test_selection_response_remains_bound_while_its_save_is_slow(self):
        self.app.job=self.app._idle_job()
        other=self.root/'other';other.mkdir()
        with self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.app.select_project,{'source':str(self.source),'campaign':str(self.project)})
            try:
                self.assertTrue(entered.wait(2))
                selected=pool.submit(self.app.select_project,{'source':str(self.source),'campaign':str(other)}).result(timeout=2)
                self.assertEqual(selected['campaign'],str(other))
            finally:release.set()
            with self.assertRaises(studio.ProjectSelectionChanged):first.result(timeout=2)
        self.assertEqual(json.loads(self.store.read_text(encoding='utf-8'))['campaign'],str(other))

    def test_warning_follows_latest_attempt_in_both_failure_orders(self):
        for failures in ((1,),(2,)):
            with self.subTest(failures=failures):
                self.app._last_saved_progress=0
                with self.slow_first_write(failures) as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
                    first=pool.submit(self.app._progress,{'recognized':1})
                    try:
                        self.assertTrue(entered.wait(2))
                        pool.submit(self.app._progress,{'recognized':2}).result(timeout=2)
                    finally:release.set()
                    first.result(timeout=2)
                self.assertEqual(len(writes),2)
                self.assertEqual(bool(self.app.state()['persistence_warning']),failures==(2,))
                # A failed writer must release ownership for the next save.
                self.app._persist()
                self.assertFalse(self.app.persistence_warning)

    def test_older_write_success_does_not_clear_newer_snapshot_error(self):
        with self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.app._progress,{'recognized':1})
            try:
                self.assertTrue(entered.wait(2))
                with patch.object(self.app,'_remember_job',side_effect=ValueError('invalid summary fixture')):
                    pool.submit(self.app._persist).result(timeout=2)
                self.assertTrue(self.app.persistence_warning)
            finally:release.set()
            first.result(timeout=2)
        self.assertTrue(self.app.persistence_warning)
        self.app._persist()
        self.assertFalse(self.app.persistence_warning)

    def test_slow_state_response_refreshes_save_warning_without_job_change(self):
        entered=threading.Event();release=threading.Event()
        original=studio.StudioController._saved_state
        def read(view):
            entered.set()
            if not release.wait(6):raise AssertionError('state read not released')
            return original(view)
        with patch.object(studio.StudioController,'_saved_state',read),ThreadPoolExecutor(max_workers=2) as pool:
            result=pool.submit(self.app.state)
            try:
                self.assertTrue(entered.wait(2))
                with patch.object(studio,'atomic_json',side_effect=OSError('save failure')):
                    self.app._persist()
                self.assertTrue(self.app.persistence_warning)
            finally:release.set()
            self.assertTrue(result.result(timeout=2)['persistence_warning'])

    def test_automatic_idle_exit_waits_for_pending_final_save(self):
        with self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
            final=pool.submit(self.app._finish_job)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(pool.submit(self.app.idle_ready).result(timeout=2))
            finally:release.set()
            final.result(timeout=2)
        self.assertTrue(self.app.idle_ready())

    def test_slow_start_summary_does_not_prevent_stopping_launched_worker(self):
        self.app.job=self.app._idle_job()
        worker_started=threading.Event();worker_exit=threading.Event()
        def run(config,stop,on_progress):
            worker_started.set()
            if not worker_exit.wait(6):raise AssertionError('worker not released')
            return {'status':'cancelled' if stop.is_set() else 'complete'}
        with patch.object(studio,'run_pipeline',side_effect=run),self.slow_first_write() as (entered,release,writes),ThreadPoolExecutor(max_workers=2) as pool:
            starting=pool.submit(self.app.start,{'action':'local'})
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(worker_started.wait(2))
                self.assertTrue(pool.submit(self.app.stop).result(timeout=2)['stopping'])
                self.assertTrue(self.app.stop_event.is_set())
            finally:
                release.set();worker_exit.set()
            starting.result(timeout=2)
            self.app.worker.join(2)
            self.assertFalse(self.app.worker.is_alive())
        self.assertFalse(self.app.job['busy'])

    def test_interrupted_writer_releases_ownership_for_a_later_save(self):
        self.app.job.update(busy=False,recognized=1)
        with patch.object(studio,'atomic_json',side_effect=KeyboardInterrupt('synthetic interrupt')):
            with self.assertRaises(KeyboardInterrupt):self.app._persist()
        self.app.job['recognized']=2
        self.app._persist()
        saved=json.loads(self.store.read_text(encoding='utf-8'))
        self.assertEqual(saved['last_runs'][self.app._project_id()]['recognized'],2)
        self.assertTrue(self.app.idle_ready())


if __name__=='__main__':unittest.main()
