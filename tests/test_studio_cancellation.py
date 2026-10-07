"""Deterministic CLI cancellation checks; provider calls are always fake."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow, siliconflow_pilot as pilot
from subtitle_pipeline.cloud_budget import CloudCancelled, HttpResponse


class StudioCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.campaign = self.root / 'campaign'
        self.campaign.mkdir()
        self.stop_file = self.root / 'this-job-stop.flag'
        for guard in (
            patch.dict(os.environ, {'SUBTITLE_STUDIO_STOP_FILE': str(self.stop_file)}),
            patch('socket.socket.connect', side_effect=AssertionError('No network in cancellation tests')),
            patch('socket.create_connection', side_effect=AssertionError('No network in cancellation tests')),
            patch.object(workflow, 'emit'),
            patch.object(pilot, 'emit'),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def test_paid_workflow_early_stop_never_reaches_paid_action(self):
        for action, function in (('samples', 'run_samples'), ('full', 'run_full')):
            self.stop_file.write_text('stop before child initialization', encoding='utf-8')
            with self.subTest(action=action), patch.object(workflow, function) as paid_action, \
                    patch.object(workflow.threading, 'Thread'):
                result = workflow.main([action, '--campaign', str(self.campaign)])
                self.assertEqual(result, 2)
                paid_action.assert_not_called()
                self.assertTrue(self.stop_file.is_file())
                self.assertEqual(workflow.emit.call_args.kwargs['status'], 'cancelled')

    def test_siliconflow_early_stop_prevents_even_model_lookup(self):
        self.stop_file.write_text('stop before child initialization', encoding='utf-8')
        model_body = json.dumps({'data': [{'id': pilot.MODEL}]}).encode()
        with patch.object(pilot, 'configuration', return_value='offline-fixture'), \
                patch.object(pilot, 'read_json', return_value={}), \
                patch.object(pilot, 'verify_source'), \
                patch.object(pilot, 'validate_samples', return_value=[]), \
                patch.object(pilot, 'request', return_value=HttpResponse(200, {}, model_body)) as request, \
                patch.object(pilot.threading, 'Thread'):
            result = pilot.main(['--campaign', str(self.campaign)])
        self.assertEqual(result, 2)
        request.assert_not_called()
        self.assertTrue(self.stop_file.is_file())
        self.assertEqual(pilot.emit.call_args.kwargs['status'], 'cancelled')

    def test_independent_stop_is_never_deleted_even_if_it_names_legacy_marker(self):
        marker = self.campaign / 'STOP.flag'
        marker.write_text('this job stop', encoding='utf-8')
        with patch.dict(os.environ, {'SUBTITLE_STUDIO_STOP_FILE': str(marker)}), \
                patch.object(workflow, 'run_samples') as paid_action, \
                patch.object(workflow.threading, 'Thread'):
            self.assertEqual(workflow.main(['samples', '--campaign', str(self.campaign)]), 2)
        paid_action.assert_not_called()
        self.assertEqual(marker.read_text(encoding='utf-8'), 'this job stop')

    def test_cloud_watcher_honors_independent_and_legacy_files(self):
        for marker in (self.stop_file, self.campaign / 'STOP.flag'):
            self.stop_file.unlink(missing_ok=True)
            observed = []
            def running(_campaign, stop):
                marker.write_text('stop running job', encoding='utf-8')
                observed.append(stop.wait(2))
                raise workflow.r.Cancelled('stopped fixture')
            with self.subTest(marker=marker.name), patch.object(workflow, 'run_samples', side_effect=running):
                result = workflow.main(['samples', '--campaign', str(self.campaign)])
            self.assertEqual(result, 2)
            self.assertEqual(observed, [True])
            self.assertTrue(marker.is_file())

    def test_siliconflow_watcher_honors_independent_and_legacy_files(self):
        for marker in (self.stop_file, self.campaign / 'STOP.flag'):
            self.stop_file.unlink(missing_ok=True)
            observed = []
            def running(_campaign, stop, *, start_watch):
                start_watch()
                marker.write_text('stop running job', encoding='utf-8')
                observed.append(stop.wait(2))
                raise CloudCancelled('stopped fixture')
            with self.subTest(marker=marker.name), patch.object(pilot, 'run', side_effect=running):
                result = pilot.main(['--campaign', str(self.campaign)])
            self.assertEqual(result, 2)
            self.assertEqual(observed, [True])
            self.assertTrue(marker.is_file())

    def test_legacy_cli_still_clears_previous_stop_before_new_action(self):
        marker = self.campaign / 'STOP.flag'
        marker.write_text('old job stop', encoding='utf-8')
        with patch.dict(os.environ, {'SUBTITLE_STUDIO_STOP_FILE': ''}), \
                patch.object(workflow, 'run_samples') as paid_action, \
                patch.object(workflow.threading, 'Thread'):
            result = workflow.main(['samples', '--campaign', str(self.campaign)])
        self.assertEqual(result, 0)
        paid_action.assert_called_once()
        self.assertFalse(marker.exists())
        self.assertFalse(paid_action.call_args.args[1].is_set())


if __name__ == '__main__':
    unittest.main()
