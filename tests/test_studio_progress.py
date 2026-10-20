import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from subtitle_pipeline import studio


class StudioProgressTests(unittest.TestCase):
    def test_slow_project_identity_does_not_block_stop_or_worker_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app=studio.StudioController(source=str(root/'video.mp4'),
                campaign=str(root/'project'),state_path=root/'studio.json')
            app.job.update(busy=True,action='local',status='running')
            entered=threading.Event();release=threading.Event()
            original=Path.resolve
            def slow(path,*args,**kwargs):
                if threading.current_thread().name.startswith('poll-reader') and not entered.is_set():
                    entered.set()
                    if not release.wait(5):raise AssertionError('identity read was not released')
                return original(path,*args,**kwargs)
            with patch.object(Path,'resolve',slow), \
                    ThreadPoolExecutor(max_workers=1,thread_name_prefix='poll-reader') as readers, \
                    ThreadPoolExecutor(max_workers=1) as controls:
                polling=readers.submit(app.progress)
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(controls.submit(app.stop).result(timeout=2),{'stopping':True})
                    self.assertTrue(app.stop_event.is_set())
                    controls.submit(app._progress,{'translated':17}).result(timeout=2)
                    with app.lock:app.persistence_warning='synthetic save warning'
                finally:release.set()
                result=polling.result(timeout=2)
            self.assertEqual(result['job']['translated'],17)
            self.assertIn('停止',result['job']['message'])
            self.assertEqual(result['persistence_warning'],'synthetic save warning')

    def test_progress_does_not_load_project_files_and_returns_an_isolated_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            app=studio.StudioController(source='synthetic.mp4',campaign='synthetic campaign',
                state_path=Path(directory)/'studio.json')
            app.job={**app._idle_job(),'busy':True,'status':'running','logs':['one']}
            with patch.object(studio,'read_json',side_effect=AssertionError('progress read project files')), \
                 patch.object(studio,'account_environment',side_effect=AssertionError('progress read credentials')):
                result=app.progress()
            self.assertEqual(result['project_id'],app._project_id())
            self.assertEqual(result['job']['status'],'running')
            self.assertNotIn('accounts',result)
            result['job']['logs'].append('not owned')
            self.assertEqual(app.job['logs'],['one'])

    def test_project_switch_during_identity_read_retries_with_the_new_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app=studio.StudioController(source=str(root/'one.mp4'),campaign=str(root/'one'),
                state_path=root/'studio.json')
            entered=threading.Event();release=threading.Event()
            original=Path.resolve
            def slow(path,*args,**kwargs):
                if threading.current_thread().name.startswith('poll-reader') and not entered.is_set():
                    entered.set()
                    if not release.wait(5):raise AssertionError('identity read was not released')
                return original(path,*args,**kwargs)
            with patch.object(Path,'resolve',slow),patch.object(app,'state',return_value={}), \
                    ThreadPoolExecutor(max_workers=1,thread_name_prefix='poll-reader') as readers, \
                    ThreadPoolExecutor(max_workers=1) as controls:
                polling=readers.submit(app.progress)
                try:
                    self.assertTrue(entered.wait(2))
                    controls.submit(app.select_project,{'source':str(root/'two.mp4'),
                        'campaign':str(root/'two')}).result(timeout=2)
                finally:release.set()
                result=polling.result(timeout=2)
            self.assertEqual(result['source'],str((root/'two.mp4').resolve()))
            self.assertEqual(result['campaign'],str((root/'two').resolve()))
            self.assertEqual(result['project_id'],app._project_id())

    def test_repeated_project_switch_during_identity_read_rejects_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            app=studio.StudioController(source='synthetic.mp4',campaign='synthetic campaign',
                state_path=Path(directory)/'studio.json')
            original=studio.StudioController._progress_data
            def changed(view):
                result=original(view)
                with app.lock:app._selection_revision+=1
                return result
            with patch.object(studio.StudioController,'_progress_data',changed):
                with self.assertRaises(studio.ProjectSelectionChanged):app.progress()
