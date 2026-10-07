"""Destination capacity failure must preserve completed export recovery evidence."""
import errno
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import media_export
from subtitle_pipeline import copy_space
from subtitle_pipeline.languages import video_output_path
import tests.test_draft_video as fixtures


class PublicationSpaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DraftVideoTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_known_insufficient_space_does_not_create_copy_or_lose_encode(self):
        test = self.fixture
        with patch.object(copy_space.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
                patch.object(media_export, 'cancellable_copy', wraps=workflow.cancellable_copy) as copies:
            with self.assertRaises(OSError) as raised:
                workflow.export_video(test.campaign, test.stop, draft=True)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        copies.assert_not_called()
        folder = test.campaign / '导出' / '草稿视频'
        self.assertEqual(workflow.read_json(folder / 'encoding-checkpoint.json')['status'], 'encoded')
        self.assertTrue((folder / 'publication-checkpoint.json').is_file())
        self.assertTrue((folder / 'result.partial.mp4').is_file())
        self.assertFalse(video_output_path(test.source, 'zh-CN', draft=True).exists())
        self.assertFalse(list(test.root.glob('.*.tmp')))
        self.assertNotIn('draft_output', workflow.read_json(test.campaign / 'campaign.json'))

    def test_recovery_after_freeing_space_does_not_repeat_encoding_or_media_checks(self):
        test = self.fixture
        with patch.object(copy_space.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError) as raised:
                workflow.export_video(test.campaign, test.stop, draft=True)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        test.encoder.reset_mock()
        with patch.object(media_export, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(test.campaign, test.stop, draft=True)
        self.assertEqual(test.video_encode_count(), 0)
        self.assertEqual(test.decode_check_count(), 0)
        audio.assert_not_called()
        manifest = workflow.read_json(test.campaign / 'campaign.json')
        final = Path(manifest['draft_output'])
        self.assertEqual(workflow.sha256(final), manifest['draft_output_sha256'])

    def test_cancelled_publication_does_not_query_capacity_or_open_copy(self):
        test = self.fixture
        test.stop.set()
        final = test.root / 'not-created.mp4'
        with patch.object(copy_space.shutil, 'disk_usage') as usage, \
                patch.object(media_export, 'cancellable_copy') as copies:
            with self.assertRaises(workflow.r.Cancelled):
                workflow._publish_video(test.source, final, test.stop)
        usage.assert_not_called()
        copies.assert_not_called()
        self.assertFalse(final.exists())

    def test_statistics_failure_is_original_error_and_still_preserves_encoding(self):
        test = self.fixture
        error = OSError(errno.EIO, 'synthetic target-volume metadata failure')
        with patch.object(copy_space.shutil, 'disk_usage', side_effect=error), \
                patch.object(media_export, 'cancellable_copy') as copies:
            with self.assertRaises(OSError) as raised:
                workflow.export_video(test.campaign, test.stop, draft=True)
        self.assertIs(raised.exception, error)
        copies.assert_not_called()
        self.assertTrue((test.campaign / '导出' / '草稿视频' / 'result.partial.mp4').is_file())
        self.assertFalse(video_output_path(test.source, 'zh-CN', draft=True).exists())


if __name__ == '__main__':
    unittest.main()
