"""Light subtitle reads remain available while real MP4 verification is blocked."""
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio
from tests import test_studio_export_integrity, test_studio_source_video_export, test_studio_video_download


class StudioLightweightVideoTests(unittest.TestCase):
    setUp = test_studio_export_integrity.StudioExportIntegrityTests.setUp
    make_samples = test_studio_export_integrity.StudioExportIntegrityTests.make_samples
    publish_fixture = test_studio_export_integrity.StudioExportIntegrityTests.publish_fixture
    write_manifest = test_studio_export_integrity.StudioExportIntegrityTests.write_manifest
    request = test_studio_video_download.StudioVideoDownloadTests.request

    def spawn(self, operation):
        results, errors = [], []
        def run():
            try: results.append(operation())
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, results, errors

    def hash_barrier(self, path):
        entered, release = threading.Event(), threading.Event()
        original = hashlib.file_digest
        def digest(stream, *args, **kwargs):
            if Path(stream.name) == path:
                entered.set()
                if not release.wait(5): raise AssertionError('Hash barrier was not released')
            return original(stream, *args, **kwargs)
        return entered, release, patch.object(hashlib, 'file_digest', side_effect=digest)

    def test_light_reads_finish_before_actual_video_hash_is_released(self):
        self.publish_fixture()
        entered, release, guard = self.hash_barrier(self.output)
        with guard:
            full, full_results, full_errors = self.spawn(self.app.exported_video_path)
            workers = []
            try:
                self.assertTrue(entered.wait(2), 'Actual MP4 hash did not start')
                for operation in (lambda: self.app.state(include_video=False),
                                  lambda: self.app.preview('main', include_video=False)):
                    workers.append(self.spawn(operation))
                for worker, results, errors in workers:
                    worker.join(1)
                    self.assertFalse(worker.is_alive(), 'Light read waited for ongoing MP4 hash')
                    self.assertEqual(errors, [])
                    self.assertEqual(len(results), 1)
                self.assertTrue(full.is_alive())
                state, preview = [worker[1][0] for worker in workers]
                self.assertEqual(state['file_layout']['video_path'], '')
                self.assertTrue(preview['video_results_pending'])
                self.assertEqual(len(preview['cues']), 1)
                self.assertIsNone(preview['exported_video'])
                self.assertIsNone(preview['draft_video'])
            finally:
                release.set(); full.join(2)
                for worker, _, _ in workers: worker.join(2)
        self.assertEqual(full_errors, [])
        self.assertEqual(full_results, [self.output])

    def test_light_state_uses_only_completed_cache_and_never_opens_mp4(self):
        self.publish_fixture()
        original = Path.open
        def opened(path, *args, **kwargs):
            if path in (self.output, self.source): raise AssertionError('MP4 body opened by light read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', new=opened):
            self.assertEqual(self.app.state(include_video=False)['file_layout']['video_path'], '')
            self.assertEqual(len(self.app.preview('main', include_video=False)['cues']), 1)
        self.assertEqual(self.app.exported_video_path(), self.output)
        with patch.object(Path, 'open', new=opened):
            self.assertEqual(self.app.state(include_video=False)['file_layout']['video_path'], str(self.output))
        info = self.output.stat(); replacement = self.output.with_suffix('.replacement')
        replacement.write_bytes(b'x' * info.st_size)
        os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns)); replacement.replace(self.output)
        with patch.object(Path, 'open', new=opened):
            self.assertEqual(self.app.state(include_video=False)['file_layout']['video_path'], '')

    def test_light_cache_lookup_does_not_wait_for_lock_even_with_cached_hit(self):
        self.publish_fixture(); self.app.exported_video_path()
        lock = self.app._video_checks[0]
        lock.acquire()
        try:
            worker, results, errors = self.spawn(lambda: self.app.state(include_video=False))
            worker.join(1)
            self.assertFalse(worker.is_alive(), 'Cache-only lookup waited on digest lock')
            self.assertEqual(errors, [])
            self.assertEqual(results[0]['file_layout']['video_path'], '')
        finally:
            lock.release(); worker.join(2)

    def test_selection_receipt_and_command_admission_do_not_hash_video(self):
        self.publish_fixture()
        with patch.object(hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            receipt = self.app.select_project({'source': str(self.source), 'campaign': str(self.campaign)})
            self.assertEqual(receipt['file_layout']['video_path'], '')
            with self.assertRaises(ValueError): self.app._start_payload({'action': 'invalid-action'})
            self.assertEqual(digest.call_count, 0, 'Command path must not hash MP4')

    def test_independent_results_require_project_and_return_current_descriptors(self):
        self.publish_fixture(); identity = self.app._project_id()
        with self.assertRaises(ValueError): self.app.video_results('main')
        with self.assertRaises(ValueError): self.app.video_results('main', 'f'*64)
        result = self.app.video_results('main', identity)
        self.assertEqual((result['project_id'], result['selected_id'], result['revision']), (identity, 'main', None))
        self.assertEqual(result['exported_video']['name'], self.output.name)
        self.assertIsNone(result['draft_video'])
        self.output.write_bytes(b'corrupt')
        self.assertIsNone(self.app.video_results('main', identity)['exported_video'])

    def test_independent_results_reject_selection_epoch_change_including_aba(self):
        self.publish_fixture(); identity = self.app._project_id()
        entered, release, guard = self.hash_barrier(self.output)
        with guard:
            worker, results, errors = self.spawn(lambda: self.app.video_results('main', identity))
            try:
                self.assertTrue(entered.wait(2))
                with self.app.lock: self.app._selection_revision += 2
            finally: release.set(); worker.join(2)
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], studio.ProjectSelectionChanged)

    def test_source_capability_and_revision_guard_survive_independent_hash(self):
        fixture = test_studio_source_video_export.StudioSourceVideoPathTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture.export(encoder='cpu'); video = fixture.fixture.current(); app = fixture.app
        original = Path.open
        def opened(path, *args, **kwargs):
            if path in (video, fixture.fixture.source): raise AssertionError('Source capability read MP4 body')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', new=opened):
            view = app.preview('main', include_video=False)
            self.assertTrue(view['source_export_supported'])
            self.assertTrue(app.state(include_video=False)['actions']['export-draft'])
            self.assertEqual(len(view['cues']), 2)
        revision = view['manual_review']['revision']; identity = view['project_id']
        with patch.object(hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            with self.assertRaises(ValueError): app.video_results('main', identity, 'f'*64)
            self.assertEqual(digest.call_count, 0, 'Stale revision must fail before MP4 verification')
        app._video_checks[1].clear()
        entered, release, guard = self.hash_barrier(video)
        with guard:
            worker, results, errors = self.spawn(lambda: app.video_results('main', identity, revision))
            try:
                self.assertTrue(entered.wait(2))
                cue = view['cues'][0]
                app.review_cue({'project_id': identity, 'sample': 'main', 'response_mode': 'cue',
                    'expected_revision': revision, 'cue_id': cue['id'],
                    **{key: cue[key] for key in ('start_ms', 'end_ms', 'source_text', 'target_text', 'review_status', 'note')},
                    'source_text': 'Changed during verification', 'translation_confirmed': False})
            finally: release.set(); worker.join(2)
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        current = app.preview('main', include_video=False)
        self.assertNotEqual(current['manual_review']['revision'], revision)
        self.assertIsNone(app.video_results('main', identity)['draft_video'])

    def test_authenticated_http_queries_choose_light_reads_and_independent_results(self):
        self.publish_fixture()
        self.host = '127.0.0.1:7777'; self.token = 'synthetic-light-read-token'
        self.server_instance = SimpleNamespace(server_port=7777, last_touch=0)
        self.handler_type = studio.make_handler(self.app, self.token, self.host, self.root)
        identity = self.app._project_id()
        for route in ('/api/state?include_video=false', '/api/preview?sample=main&include_video=false&project='+identity):
            with patch.object(hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
                status, _, body = self.request('GET', route)
                self.assertEqual(digest.call_count, 0, 'HTTP light read must not hash MP4')
            self.assertEqual(status, 200); data = json.loads(body)
            if route.startswith('/api/preview'): self.assertTrue(data['video_results_pending'])
        route = '/api/video-results?sample=main&project='+identity
        self.assertEqual(self.request('GET', route, authorized=False)[0], 403)
        self.assertEqual(self.request('GET', route, headers={'Origin':'https://external.invalid'})[0], 403)
        self.assertEqual(self.request('GET', '/api/video-results?sample=main')[0], 400)
        self.assertEqual(self.request('GET', route+'&revision=stale')[0], 400)
        status, _, body = self.request('GET', route)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['exported_video']['name'], self.output.name)


if __name__ == '__main__': unittest.main()
