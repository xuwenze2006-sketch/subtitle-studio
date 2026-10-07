"""Opening validated results must leave task control responsive during I/O."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio


class StudioOpenConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-open-concurrency-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'synthetic.mp4'
        self.source.write_bytes(b'synthetic source')
        self.project = self.root / 'project'
        self.project.mkdir()
        self.output = self.source.with_name(self.source.stem + '_中文字幕_修订版.mp4')
        self.output.write_bytes(b'verified official output')
        self.draft = self.source.with_name(self.source.stem + '_中文字幕_未审核草稿.mp4')
        self.draft.write_bytes(b'verified draft output')
        full = self.project / '整片'
        full.mkdir()
        for name in ('原文.srt', '中文草稿.srt'):
            (full / name).write_bytes(b'owned synthetic subtitle')
        subtitle_digest = hashlib.sha256(b'owned synthetic subtitle').hexdigest()
        manifest = {'source': {'path': str(self.source), 'sha256': 'a' * 64}, 'status': 'exported',
                    'output': str(self.output), 'output_sha256': hashlib.sha256(self.output.read_bytes()).hexdigest(),
                    'output_binding': {'source_sha256': 'a' * 64, 'render_version': 1},
                    'draft_output': str(self.draft), 'draft_output_sha256': hashlib.sha256(self.draft.read_bytes()).hexdigest(),
                    'draft_output_binding': {'source_sha256': 'a' * 64, 'render_version': 1,
                        'review_status': 'unreviewed_draft', 'manual_revision': None,
                        'machine_source_sha256': subtitle_digest, 'machine_target_sha256': subtitle_digest}}
        (self.project / 'campaign.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.app = studio.StudioController(source=str(self.source), campaign=str(self.project),
                                           state_path=self.root / 'absent-state.json')

    def test_opening_cold_video_or_folder_allows_progress_and_stop(self):
        for target, expected in (('video', self.output), ('video-folder', self.output.parent),
                                 ('draft-video', self.draft), ('draft-video-folder', self.draft.parent)):
            with self.subTest(target=target):
                self.app._video_checks[1].clear()
                self.app.stop_event.clear()
                self.app.job.update(busy=True, action='local')
                hashing, release, responsive = threading.Event(), threading.Event(), threading.Event()
                errors, results = [], []
                original_digest = hashlib.file_digest

                def gated_hash(stream, algorithm):
                    hashing.set()
                    if not release.wait(3):
                        raise AssertionError('Synthetic hash gate was not released')
                    return original_digest(stream, algorithm)

                def open_result():
                    try:
                        results.append(self.app.open_result(target, self.app._project_id()))
                    except BaseException as error:
                        errors.append(error)

                def control_task():
                    try:
                        self.app.progress()
                        self.app.stop()
                        responsive.set()
                    except BaseException as error:
                        errors.append(error)

                with patch.object(studio.hashlib, 'file_digest', side_effect=gated_hash), \
                        patch.object(studio.os, 'startfile', create=True) as opened:
                    opening = threading.Thread(target=open_result)
                    opening.start()
                    controls = None
                    try:
                        self.assertTrue(hashing.wait(1), 'Official file verification was not reached')
                        controls = threading.Thread(target=control_task)
                        controls.start()
                        self.assertTrue(responsive.wait(.5), 'Video verification held the task controller lock')
                        self.assertTrue(self.app.stop_event.is_set())
                    finally:
                        release.set()
                        opening.join(3)
                        if controls is not None:
                            controls.join(3)
                    self.assertFalse(opening.is_alive())
                    self.assertFalse(controls.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(results, [{'opened': True, 'path': str(expected)}])
                    opened.assert_called_once_with(expected)

    def test_project_switch_during_video_verification_rejects_the_os_launch(self):
        hashing, release, switched = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_digest = hashlib.file_digest
        identity = self.app._project_id()

        def gated_hash(stream, algorithm):
            hashing.set()
            if not release.wait(3):
                raise AssertionError('Synthetic hash gate was not released')
            return original_digest(stream, algorithm)

        def open_result():
            try:
                self.app.open_result('video', identity)
            except BaseException as error:
                errors.append(error)

        def switch():
            # Exercise the real selection epoch boundary without unrelated
            # state/credential reads made by the project-selection UI route.
            with self.app.lock:
                self.app.source = str(self.root / 'other-source.mp4')
                self.app._selection_revision += 1
            switched.set()

        with patch.object(studio.hashlib, 'file_digest', side_effect=gated_hash), \
                patch.object(studio.os, 'startfile', create=True) as opened:
            opening = threading.Thread(target=open_result)
            opening.start()
            changing = None
            try:
                self.assertTrue(hashing.wait(1))
                changing = threading.Thread(target=switch)
                changing.start()
                self.assertTrue(switched.wait(.5), 'Video verification blocked a project switch')
            finally:
                release.set()
                opening.join(3)
                if changing is not None:
                    changing.join(3)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], studio.ProjectSelectionChanged)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
