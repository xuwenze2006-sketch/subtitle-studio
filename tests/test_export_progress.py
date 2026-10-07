import importlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class ExportProgressTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.export_progress'),
                             'export progress module must be implemented')
        self.progress = importlib.import_module('subtitle_pipeline.export_progress')

    def test_last_complete_block_wins_without_reading_partial_update(self):
        parsed = self.progress.parse_ffmpeg_progress(
            'out_time_us=30000000\nspeed=3.0x\nprogress=continue\n'
            'out_time_us=60000000\nspeed=4.0x\nprogress=continue\n'
            'out_time_us=90000000\nspeed=')
        self.assertEqual(parsed, {'encoded_seconds': 60.0, 'speed': 4.0})
        self.assertIsNone(self.progress.parse_ffmpeg_progress('out_time_us=5000000\n'))

    def test_timestamp_fallback_and_invalid_speed(self):
        parsed = self.progress.parse_ffmpeg_progress(
            'out_time_us=N/A\nout_time=01:02:03.500000\nspeed=N/A\nprogress=end\n')
        self.assertEqual(parsed, {'encoded_seconds': 3723.5, 'speed': None})
        for value in ('nan', 'inf', '-1', '0', '99999999999999999999999999999999999999999'):
            parsed = self.progress.parse_ffmpeg_progress(
                f'out_time_us=2000000\nspeed={value}x\nprogress=continue\n')
            self.assertIsNone(parsed['speed'])

    def test_progress_payload_never_exposes_invalid_numbers_or_unknown_fields(self):
        cleaned = self.progress.sanitize_export_progress({
            'phase': 'encoding', 'percent': 125, 'encoded_seconds': 130,
            'duration_seconds': 100, 'speed': float('inf'),
            'eta_seconds': -5, 'elapsed_seconds': float('nan'), 'secret': 'omit',
        })
        self.assertEqual(cleaned, {'phase': 'encoding', 'percent': 100.0,
                                  'encoded_seconds': 100.0, 'duration_seconds': 100.0,
                                  'speed': None, 'eta_seconds': None, 'elapsed_seconds': 0.0})
        json.dumps(cleaned, allow_nan=False)
        self.assertIsNone(self.progress.sanitize_export_progress({'phase': 'invented'}))
        self.assertIsNone(self.progress.sanitize_export_progress({'phase': []}))
        self.assertIsNone(self.progress.sanitize_export_progress([]))

    def test_unknown_duration_has_no_fabricated_completion_or_eta(self):
        emitted = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.txt'
            path.write_text('out_time_us=30000000\nspeed=3x\nprogress=end\n')
            reporter = self.progress.ExportProgress(0, lambda message, **data: emitted.append(data))
            with reporter.watch(path):
                pass
            encoding = emitted[-1]['export_progress']
            self.assertEqual(encoding['phase'], 'encoding')
            self.assertEqual(encoding['encoded_seconds'], 30.0)
            self.assertIsNone(encoding['percent'])
            self.assertIsNone(encoding['eta_seconds'])

    def test_stage_elapsed_time_uses_monotonic_clock(self):
        emitted = []
        with patch.object(self.progress.time, 'monotonic', side_effect=[100.0, 105.0, 110.0]):
            reporter = self.progress.ExportProgress(60, lambda message, **data: emitted.append(data))
            reporter.stage('preparing')
            reporter.stage('publishing')
        self.assertEqual([item['export_progress']['elapsed_seconds'] for item in emitted], [5.0, 10.0])

    def test_watch_reports_percent_speed_eta_then_later_stage_clears_encoding_estimate(self):
        emitted = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.txt'
            path.write_text('out_time_us=30000000\nspeed=3x\nprogress=continue\n')
            reporter = self.progress.ExportProgress(120, lambda message, **data: emitted.append(data))
            reporter.stage('preparing')
            with reporter.watch(path):
                pass
            encoding = [item['export_progress'] for item in emitted
                        if item['export_progress']['phase'] == 'encoding'
                        and item['export_progress']['encoded_seconds'] == 30][-1]
            self.assertEqual(encoding['percent'], 25.0)
            self.assertEqual(encoding['speed'], 3.0)
            self.assertEqual(encoding['eta_seconds'], 30.0)
            reporter.stage('validating')
            validating = emitted[-1]['export_progress']
            self.assertIsNone(validating['percent'])
            self.assertIsNone(validating['eta_seconds'])
            self.assertIsNone(validating['speed'])
            self.assertFalse(any(item['export_progress']['phase'] == 'done' for item in emitted))
            reporter.stage('done')
            self.assertEqual(emitted[-1]['export_progress']['percent'], 100.0)

    def test_watch_stops_and_never_claims_done_when_encoding_raises(self):
        emitted = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing-progress.txt'
            reporter = self.progress.ExportProgress(60, lambda message, **data: emitted.append(data),
                                                    poll_interval=0.01)
            with self.assertRaisesRegex(RuntimeError, 'encoder failed'):
                with reporter.watch(path):
                    raise RuntimeError('encoder failed')
            count = len(emitted)
            path.write_text('out_time_us=60000000\nspeed=3x\nprogress=end\n')
            time.sleep(0.04)
            self.assertEqual(len(emitted), count)
            self.assertFalse(any(item['export_progress']['phase'] == 'done' for item in emitted))

    def test_watch_ignores_regressing_timestamps_and_bounds_overrun(self):
        emitted = []
        seen = threading.Event()
        def emit(message, **data):
            emitted.append(data['export_progress'])
            if data['export_progress']['encoded_seconds'] == 30:
                seen.set()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.txt'
            path.write_text('out_time_us=30000000\nspeed=2x\nprogress=continue\n')
            reporter = self.progress.ExportProgress(60, emit, poll_interval=0.01)
            with reporter.watch(path):
                self.assertTrue(seen.wait(1))
                path.write_text('out_time_us=20000000\nspeed=2x\nprogress=continue\n')
                time.sleep(0.03)
                path.write_text('out_time_us=90000000\nspeed=2x\nprogress=end\n')
            seconds = [item['encoded_seconds'] for item in emitted]
            self.assertEqual(seconds, sorted(seconds))
            self.assertEqual(seconds[-1], 60.0)
            self.assertEqual(emitted[-1]['percent'], 100.0)
            self.assertEqual(emitted[-1]['eta_seconds'], 0.0)


class StudioExportProgressTests(unittest.TestCase):
    def test_live_export_progress_is_bounded_and_not_restored_as_live_progress(self):
        from subtitle_pipeline.studio import StudioController
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'studio.json'
            app = StudioController(campaign=str(Path(directory) / 'project'), state_path=state_path)
            app.job.update(action='export-draft', busy=True)
            app._progress({'export_progress': {'phase': 'encoding', 'percent': 20,
                                               'encoded_seconds': 10, 'duration_seconds': 50,
                                               'speed': 2, 'eta_seconds': 20, 'elapsed_seconds': 5}})
            self.assertEqual(app.job.get('export_progress', {}).get('percent'), 20)
            app._progress({'export_progress': {'phase': 'unknown'}})
            self.assertEqual(app.job['export_progress']['phase'], 'encoding')
            restored = StudioController(state_path=state_path)
            self.assertNotIn('export_progress', restored.job)
            self.assertNotIn('export_progress', StudioController._idle_job())

    def test_other_jobs_cannot_receive_export_progress(self):
        from subtitle_pipeline.studio import StudioController
        with tempfile.TemporaryDirectory() as directory:
            app = StudioController(state_path=Path(directory) / 'studio.json')
            app.job.update(action='full', busy=True)
            app._progress({'export_progress': {'phase': 'encoding', 'percent': 20}})
            self.assertNotIn('export_progress', app.job)


if __name__ == '__main__':
    unittest.main()
