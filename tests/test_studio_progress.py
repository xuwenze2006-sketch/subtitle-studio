import threading
import unittest
from unittest.mock import patch
from subtitle_pipeline import studio


class StudioProgressTests(unittest.TestCase):
    def test_progress_reads_only_owned_memory_and_returns_an_isolated_copy(self):
        app=studio.StudioController.__new__(studio.StudioController)
        app.lock=threading.RLock()
        app.source='synthetic.mp4'; app.campaign='synthetic campaign'; app.baseline=''
        app.persistence_warning=''; app.job={**app._idle_job(),'busy':True,'status':'running','logs':['one']}
        with patch.object(studio,'read_json',side_effect=AssertionError('progress read disk')), \
             patch.object(studio,'account_environment',side_effect=AssertionError('progress read credentials')):
            result=app.progress()
        self.assertEqual(result['project_id'],app._project_id())
        self.assertEqual(result['job']['status'],'running')
        self.assertNotIn('accounts',result)
        result['job']['logs'].append('not owned')
        self.assertEqual(app.job['logs'],['one'])
