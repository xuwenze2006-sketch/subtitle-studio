import unittest
from unittest.mock import patch

from subtitle_pipeline import studio
from subtitle_pipeline.cloud_gui import build_command


class StudioEnvironmentTests(unittest.TestCase):
    def test_environment_check_does_not_hold_the_task_controller_lock(self):
        app=studio.StudioController.__new__(studio.StudioController)
        class ForbiddenLock:
            def __enter__(self): raise AssertionError('environment held task lock')
        app.lock=ForbiddenLock()
        report={'version':1,'checks':{},'export_ready':False}
        with patch('subtitle_pipeline.environment.probe_environment',return_value=report):
            self.assertEqual(app.environment(),report)

    def test_export_worker_command_preserves_an_explicit_cpu_choice(self):
        command=build_command('export',campaign='synthetic campaign',encoder='cpu')
        self.assertEqual(command[-2:],['--encoder','cpu'])
        command=build_command('export-draft',campaign='synthetic campaign',encoder='qsv')
        self.assertEqual(command[-2:],['--encoder','qsv'])

    def test_invalid_encoder_cannot_enter_a_worker_command(self):
        for mode in (True,'unknown','cpu;bad'):
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                build_command('export',campaign='synthetic campaign',encoder=mode)
