"""Original MP4 uses local generation and the existing authenticated draft flow."""
from contextlib import nullcontext
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import runner, studio
from subtitle_pipeline.subtitles import Cue
from tests import test_source_video_export as source_fixtures


class StudioSourceVideoExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='studio-original-video-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'video.mp4'
        self.source.write_bytes(b'Synthetic video input; decoder is stubbed in unit tests')
        self.project = self.root / 'project'
        for owner, name, value in ((studio, 'account_environment', {}),
                                   (studio, 'encrypted_names', set()),
                                   (studio, 'engine_available', False)):
            guard = patch.object(owner, name, return_value=value)
            guard.start(); self.addCleanup(guard.stop)
        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection')):
            guard = patch.object(owner, name, side_effect=AssertionError('No network'))
            guard.start(); self.addCleanup(guard.stop)
        config = runner.PipelineConfig(self.source, self.project, language='en', target='zh-CN',
            translate=False, chunk_seconds=10, overlap_seconds=1)
        with patch.object(runner, 'probe_media', return_value=9000), \
                patch.object(runner, 'detect_silences', return_value=[]), \
                patch.object(runner, 'recognize_chunk', return_value=[Cue(100, 1800, 'Original words')]), \
                patch.object(runner, 'translate_texts', side_effect=AssertionError('No translation')):
            self.assertEqual(runner.run_pipeline(config)['status'], 'complete')
        self.store = self.root / 'studio.json'
        self.app = studio.StudioController(source=str(self.source), campaign=str(self.project), state_path=self.store)

    def read_state(self):
        return json.loads((self.project / 'state.json').read_text(encoding='utf-8'))

    def test_complete_original_can_export_without_accounts_or_installed_recognizer_and_read_is_readonly(self):
        before = {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        view = self.app.preview('main')
        state = self.app.state()
        self.assertTrue(view.get('source_export_supported'), 'Completed original needs an MP4 capability')
        self.assertTrue(state['actions'].get('export-draft'))
        self.assertFalse(state['actions']['export'])
        self.assertFalse(state['actions']['accept-final'])
        self.assertFalse(state['local_available'])
        self.assertEqual(before, {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()})

    def test_start_pins_independent_source_worker_and_encoder_without_cloud_or_approval(self):
        state = self.app.state()
        self.assertTrue(state['actions'].get('export-draft'), 'Completed original export must be admitted')
        with patch.object(studio.threading, 'Thread') as worker:
            response = self.app.start({'action': 'export-draft', 'project_id': state['project_id'], 'encoder': 'cpu'})
        self.assertTrue(response['started'])
        action, command = worker.call_args.kwargs['args']
        self.assertEqual(action, 'export-draft')
        self.assertEqual(command[1:3], ['-m', 'subtitle_pipeline.source_video_export'])
        self.assertEqual(command[command.index('--project') + 1], str(self.project))
        self.assertEqual(command[command.index('--encoder') + 1], 'cpu')
        self.assertNotIn('--content-passed', command)
        self.assertNotIn('--campaign', command)
        self.assertFalse((self.project / 'campaign.json').exists())
        self.assertFalse((self.project / 'final-review.json').exists())
        self.app._finish_job()

    def test_bad_encoder_does_not_launch_or_change_project(self):
        with patch.object(studio.threading, 'Thread') as worker:
            with self.assertRaises(ValueError):
                self.app.start({'action': 'export-draft', 'encoder': 'unknown'})
        worker.assert_not_called()
        self.assertFalse(self.app.job['busy'])

    def test_compact_source_save_refreshes_export_capability_without_full_preview(self):
        view = self.app.preview('main')
        cue = view['cues'][0]
        request = {'project_id': view['project_id'], 'sample': 'main', 'response_mode': 'cue',
            'expected_revision': view['manual_review']['revision'], 'cue_id': cue['id'],
            **{name: cue[name] for name in ('start_ms', 'end_ms', 'source_text', 'target_text', 'review_status', 'note')},
            'source_text': 'Corrected original words', 'translation_confirmed': False}
        with patch.object(self.app, '_preview_data', side_effect=AssertionError('Full preview rebuilt')):
            receipt = self.app.review_cue(request)
        self.assertTrue(receipt.get('source_export_supported'), 'Compact receipt needs fresh export capability')
        self.assertEqual(receipt['manual_review']['mode'], 'source_only')
        self.assertIsNone(receipt['draft_video'])

    def test_incomplete_or_translated_state_cannot_offer_original_export(self):
        original = self.read_state()
        for label, mutate in (
                ('incomplete', lambda state: state.update(status='running')),
                ('translation', lambda state: state['config'].update(translate=True)),
                ('pending', lambda state: state.update(pending_outputs={'version': 2})),
                ('changed-source-path', lambda state: state['config'].update(source=str(self.root / 'other.mp4')))):
            with self.subTest(label=label):
                state = deepcopy(original); mutate(state)
                runner.atomic_json(self.project / 'state.json', state)
                self.assertFalse(self.app.state()['actions'].get('export-draft', False))
                self.assertFalse(self.app.preview('main').get('source_export_supported', False))
        runner.atomic_json(self.project / 'state.json', original)

    def test_refresh_revokes_source_export_when_job_became_busy_after_detached_read(self):
        with self.app.lock:
            view = self.app._snapshot_view_locked()
        result = view._state_data()
        self.assertTrue(result['actions'].get('export-draft'))
        self.app.job.update(busy=True, action='local', status='running')
        with self.app.lock:
            refreshed = self.app._refresh_state_job(view, result)
        self.assertFalse(refreshed['actions'].get('export-draft'))
        self.assertTrue(refreshed['actions']['stop'])

    def test_refresh_restores_source_export_when_job_finished_after_detached_read(self):
        self.app.job.update(busy=True, action='local', status='running')
        with self.app.lock:
            view = self.app._snapshot_view_locked()
        result = view._state_data()
        self.app.job.update(busy=False, status='complete')
        with self.app.lock:
            refreshed = self.app._refresh_state_job(view, result)
        self.assertTrue(refreshed['actions'].get('export-draft'))

    def test_source_export_progress_and_restart_do_not_upgrade_review_status(self):
        state_bytes = (self.project / 'state.json').read_bytes()
        self.app.job.update(action='export-draft', busy=True, status='running')
        progress = {'phase': 'encoding', 'percent': 50, 'encoded_seconds': 4.5,
                    'duration_seconds': 9, 'speed': 2, 'eta_seconds': 2.25, 'elapsed_seconds': 2.25}
        output = '\n'.join(json.dumps(value) for value in (
            {'status': 'running', 'export_progress': progress},
            {'status': 'draft_exported', 'message': '原文未审核草稿 MP4 已导出'}))
        with patch.object(studio, 'start_process') as start, \
                patch.object(studio, 'owned_process', side_effect=nullcontext), \
                patch.object(studio, 'wait_for_process_exit', return_value=True):
            start.return_value.stdout = io.StringIO(output)
            start.return_value.wait.return_value = 0
            start.return_value.returncode = 0
            self.app._execute('export-draft', ['synthetic-worker'])
        self.assertEqual(self.app.job['status'], 'draft_exported')
        self.assertEqual(self.app.job['export_progress']['percent'], 50)
        self.assertFalse(self.app.job['busy'])
        reopened = studio.StudioController(state_path=self.store)
        self.assertEqual(reopened.state()['job']['status'], 'draft_exported')
        self.assertFalse(reopened.state()['actions']['export'])
        self.assertEqual((self.project / 'state.json').read_bytes(), state_bytes)


class StudioSourceVideoPathTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixtures.SourceVideoExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for name, value in (('account_environment', {}), ('encrypted_names', set()), ('engine_available', False)):
            guard = patch.object(studio, name, return_value=value)
            guard.start(); self.addCleanup(guard.stop)
        self.app = studio.StudioController(source=str(self.fixture.source), campaign=str(self.fixture.project),
            state_path=self.fixture.root / 'studio-store.json')

    def test_real_source_receipt_drives_preview_download_and_open_without_approval(self):
        self.fixture.export(encoder='cpu')
        video = self.fixture.current()
        self.assertIsNotNone(video)
        view = self.app.preview('main')
        self.assertTrue(view['source_export_supported'])
        self.assertEqual(view['draft_video']['name'], video.name)
        self.assertEqual(view['draft_video']['review_status'], 'unreviewed_draft')
        self.assertIsNone(view['exported_video'])
        self.assertEqual(self.app.draft_video_path(view['project_id']), video)
        self.assertEqual(self.app.download_info('draft-video', 'main', view['project_id']), (video, video.name))
        with patch.object(studio.os, 'startfile', create=True) as opened:
            self.app.open_result('draft-video-folder', view['project_id'])
        opened.assert_called_once_with(video.parent)
        self.assertFalse((self.fixture.project / 'final-review.json').exists())

    def test_new_saved_revision_withdraws_old_link_preserves_bytes_and_exports_another_version(self):
        self.fixture.export(encoder='cpu')
        previous = self.fixture.current(); original = previous.read_bytes()
        view = self.app.preview('main'); cue = view['cues'][0]
        receipt = self.app.review_cue({'project_id': view['project_id'], 'sample': 'main', 'response_mode': 'cue',
            'expected_revision': view['manual_review']['revision'], 'cue_id': 1,
            **{key: cue[key] for key in ('start_ms', 'end_ms', 'source_text', 'target_text', 'review_status', 'note')},
            'source_text': 'Current corrected source', 'translation_confirmed': False})
        self.assertTrue(receipt['source_export_supported'])
        self.assertIsNone(receipt['draft_video'])
        self.assertIsNone(self.app.preview('main')['draft_video'])
        with self.assertRaises(ValueError):
            self.app.download_path('draft-video', 'main', view['project_id'])
        self.assertEqual(previous.read_bytes(), original)
        self.fixture.export(encoder='cpu')
        current = self.fixture.current()
        self.assertNotEqual(current, previous)
        self.assertEqual(self.app.draft_video_path(view['project_id']), current)
        self.assertEqual(previous.read_bytes(), original)

    def test_damaged_recorded_mp4_is_never_exposed_or_downloaded(self):
        self.fixture.export(encoder='cpu')
        video = self.fixture.current()
        self.assertIsNotNone(self.app.preview('main')['draft_video'])
        video.write_bytes(b'corrupted output')
        self.assertIsNone(self.app.preview('main')['draft_video'])
        with self.assertRaises(ValueError):
            self.app.download_path('draft-video', 'main')


if __name__ == '__main__':
    unittest.main()
