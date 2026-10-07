"""Local capability probes must work offline, fail closed, and remain bounded."""
import importlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline.runner import Cancelled


class EnvironmentTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('subtitle_pipeline.environment')
        except ModuleNotFoundError:
            self.fail('The local environment check is not implemented')

    @staticmethod
    def completed(args, **kwargs):
        text = 'ffmpeg version 8.0-test\n' if '-version' in args else ''
        if args[0] == 'ffprobe' and '-version' in args:
            text = 'ffprobe version 8.0-test\n'
        return subprocess.CompletedProcess(args, 0, text, '')

    def test_missing_tools_are_reported_without_launching_or_exposing_paths(self):
        env = self.module()
        with patch.object(env.shutil, 'which', return_value=None), \
             patch.object(env, 'capture_process') as capture, \
             patch.object(env.r, 'engine_paths', side_effect=FileNotFoundError('private-user-path')):
            report = env.probe_environment()
        for key in ('ffmpeg', 'ffprobe', 'subtitles', 'libass', 'qsv', 'cpu', 'whisper'):
            self.assertFalse(report['checks'][key]['available'], key)
        self.assertIsNone(report['recommended_encoder'])
        self.assertFalse(report['export_ready'])
        self.assertNotIn('private-user-path', json.dumps(report))
        capture.assert_not_called()

    def test_declared_qsv_with_unusable_driver_recommends_cpu_after_real_trial(self):
        env = self.module()
        def capture(args, **kwargs):
            if 'h264_qsv' in args:
                raise subprocess.CalledProcessError(1, args, stderr='token=private-driver-error')
            return self.completed(args, **kwargs)
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=capture) as calls, \
             patch.object(env.r, 'engine_paths', side_effect=FileNotFoundError):
            report = env.probe_environment()
        self.assertFalse(report['checks']['qsv']['available'])
        self.assertTrue(report['checks']['cpu']['available'])
        self.assertEqual(report['recommended_encoder'], 'cpu')
        self.assertTrue(report['export_ready'])
        self.assertNotIn('private-driver-error', json.dumps(report))
        qsv = next(call for call in calls.call_args_list if 'h264_qsv' in call.args[0])
        self.assertIn('lavfi', qsv.args[0])
        self.assertIn('-frames:v', qsv.args[0])
        self.assertEqual(qsv.args[0][-2:], ['null', '-'])
        for call in calls.call_args_list:
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], 15)
            self.assertLessEqual(call.kwargs['max_output_bytes'], 256 * 1024)

    def test_explicit_cpu_never_probes_qsv(self):
        env = self.module()
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=self.completed) as capture:
            self.assertEqual(env.select_encoder('cpu'), 'cpu')
        commands = [call.args[0] for call in capture.call_args_list]
        self.assertTrue(any('libx264' in command for command in commands))
        self.assertFalse(any('h264_qsv' in command for command in commands))

    def test_explicit_qsv_reports_failed_trial_without_cpu_fallback(self):
        env = self.module()
        def capture(args, **kwargs):
            if 'h264_qsv' in args:
                raise subprocess.TimeoutExpired(args, 15)
            return self.completed(args, **kwargs)
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=capture) as calls:
            with self.assertRaisesRegex(ValueError, 'QSV.*CPU'):
                env.select_encoder('qsv')
        self.assertFalse(any('libx264' in call.args[0] for call in calls.call_args_list))

    def test_auto_prefers_working_qsv(self):
        env = self.module()
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=self.completed) as calls:
            self.assertEqual(env.select_encoder('auto'), 'qsv')
        self.assertFalse(any('libx264' in call.args[0] for call in calls.call_args_list))

    def test_auto_selects_cpu_only_after_qsv_trial_fails(self):
        env = self.module()
        def capture(args, **kwargs):
            if 'h264_qsv' in args:
                raise subprocess.CalledProcessError(1, args)
            return self.completed(args, **kwargs)
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=capture) as calls:
            self.assertEqual(env.select_encoder('auto'), 'cpu')
        trials = [call.args[0][call.args[0].index('-c:v') + 1]
                  for call in calls.call_args_list if '-c:v' in call.args[0]]
        self.assertEqual(trials, ['h264_qsv', 'libx264'])

    def test_no_working_encoder_has_a_clear_error_instead_of_a_false_cpu_success(self):
        env = self.module()
        def capture(args, **kwargs):
            if '-c:v' in args:
                raise subprocess.CalledProcessError(1, args)
            return self.completed(args, **kwargs)
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=capture):
            with self.assertRaisesRegex(ValueError, 'CPU.*没有可用'):
                env.select_encoder('auto')

    def test_stop_before_or_during_probe_never_returns_a_success_report(self):
        env = self.module()
        stop = threading.Event()
        stop.set()
        with patch.object(env, 'capture_process') as capture, self.assertRaises(Cancelled):
            env.probe_environment(stop)
        capture.assert_not_called()
        stop.clear()
        def stopping(args, **kwargs):
            stop.set()
            return self.completed(args, **kwargs)
        with patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=stopping), self.assertRaises(Cancelled):
            env.probe_environment(stop)

    def test_empty_whisper_model_is_not_reported_as_usable(self):
        env = self.module()
        with tempfile.TemporaryDirectory() as folder:
            exe, model = Path(folder) / 'whisper-cli.exe', Path(folder) / 'small.bin'
            exe.write_bytes(b'owned-executable-fixture')
            model.write_bytes(b'')
            with patch.object(env.shutil, 'which', return_value=None), \
                 patch.object(env.r, 'engine_paths', return_value=(exe, model)):
                report = env.probe_environment()
        self.assertFalse(report['checks']['whisper']['available'])

    def test_invalid_encoder_does_not_start_any_external_process(self):
        env = self.module()
        with patch.object(env, 'capture_process') as capture:
            for value in ('nvenc', '', True, None, 'CPU'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    env.select_encoder(value)
        capture.assert_not_called()

    def test_report_has_a_total_twenty_second_external_probe_budget(self):
        env = self.module()
        clock = [100.0]
        def capture(args, **kwargs):
            # A stalled child consumes its entire granted deadline. Later
            # probes cannot reset the clock and grant another full allowance.
            clock[0] += kwargs['timeout']
            return self.completed(args, **kwargs)
        with patch.object(env.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(env.shutil, 'which', side_effect=lambda name: name), \
             patch.object(env, 'capture_process', side_effect=capture) as calls, \
             patch.object(env.r, 'engine_paths', side_effect=FileNotFoundError):
            report = env.probe_environment()
        self.assertLessEqual(clock[0] - 100.0, 20)
        self.assertFalse(report['checks']['cpu']['available'])
        self.assertTrue(all(call.kwargs['timeout'] <= 5 for call in calls.call_args_list))

    def test_broken_tcl_runtime_is_reported_without_losing_other_checks(self):
        import tkinter
        env = self.module()
        with patch.object(tkinter, 'Tcl', side_effect=tkinter.TclError('private-installation-path')), \
             patch.object(env.shutil, 'which', return_value=None), \
             patch.object(env.r, 'engine_paths', side_effect=FileNotFoundError):
            try:
                report = env.probe_environment()
            except tkinter.TclError:
                self.fail('A broken Tcl runtime must become an unavailable check, not abort the report')
        self.assertFalse(report['checks']['tk']['available'])
        self.assertIn('ffmpeg', report['checks'])
        self.assertNotIn('private-installation-path', json.dumps(report))
