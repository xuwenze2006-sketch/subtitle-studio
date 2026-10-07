"""Draft video is usable without manufacturing human approval."""
from contextlib import nullcontext
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_gui, studio
from subtitle_pipeline.integrity import sha256
from tests import test_studio_review as fixtures


class StudioDraftVideoTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.StudioReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app=self.fixture.app
        self.campaign=self.fixture.campaign
        self.folder=self.fixture.folder
        self.source=self.fixture.source

    def publish_fixture(self):
        # Equivalent metadata to a completed synthetic encode, never real video.
        self.video=self.source.with_name(self.source.stem+'_中文字幕_未审核草稿.mp4')
        self.video.write_bytes(b'draft-video')
        manifest=dict(self.fixture.manifest)
        manifest.update(draft_output=str(self.video),draft_output_sha256=sha256(self.video),
            draft_output_binding={'source_sha256':manifest['source']['sha256'],'render_version':1,
                'review_status':'unreviewed_draft','manual_revision':None,
                'machine_source_sha256':sha256(self.folder/'原文.srt'),
                'machine_target_sha256':sha256(self.folder/'中文草稿.srt'),
                'captions_sha256':sha256(self.folder/'中文草稿.srt')})
        self.fixture.write(self.campaign/'campaign.json',manifest)
        return manifest

    def test_complete_unreviewed_project_can_start_local_draft_export_without_accounts(self):
        state=self.app.state()
        self.assertTrue(state['actions'].get('export-draft'))
        self.assertFalse(state['actions']['export'])
        command=cloud_gui.build_command('export-draft',campaign=self.campaign)
        self.assertIn('export-draft',command)
        self.assertNotIn('--content-passed',command)
        with patch.object(studio.threading,'Thread') as worker:
            response=self.app.start({'project_id':state['project_id'],'action':'export-draft'})
        self.assertTrue(response['started'])
        self.assertEqual(self.app._run_summary(self.app.job)['action'],'export-draft')
        worker.return_value.start.assert_called_once()
        self.assertFalse((self.campaign/'final-review.json').exists())
        self.assertFalse((self.folder/'人工校对').exists())

    def test_busy_or_incomplete_projects_do_not_offer_draft_export(self):
        for status,busy in [('prepared',False),('full_incomplete',False),('full_ready',True)]:
            with self.subTest(status=status,busy=busy):
                actions=cloud_gui.available_actions(status,approval_exists=False,ready=False,busy=busy)
                self.assertFalse(actions.get('export-draft',False))

    def test_worker_keeps_draft_completion_without_turning_campaign_into_reviewed(self):
        self.app.job.update(action='export-draft',busy=True,status='running')
        with patch.object(studio,'start_process') as start, \
                patch.object(studio,'owned_process',side_effect=nullcontext), \
                patch.object(studio,'wait_for_process_exit',return_value=True):
            start.return_value.stdout=io.StringIO('{"status":"draft_exported","message":"未审核草稿MP4已导出"}')
            start.return_value.wait.return_value=0
            start.return_value.returncode=0
            self.app._execute('export-draft',['synthetic-worker'])
        self.assertEqual(self.app.job['status'],'draft_exported')
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(self.app.state()['campaign_status'],'full_ready')

    def test_draft_preview_download_open_are_separate_from_reviewed_video(self):
        self.publish_fixture()
        view=self.app.preview('main')
        self.assertIsNone(view['exported_video'])
        self.assertEqual(view['draft_video']['review_status'],'unreviewed_draft')
        self.assertEqual(view['draft_video']['name'],self.video.name)
        self.assertEqual(self.app.download_path('draft-video','main',view['project_id']),self.video)
        path,name=self.app.download_info('draft-video','main',view['project_id'])
        self.assertEqual((path,name),(self.video,self.video.name))
        with patch.object(studio.os,'startfile',create=True) as opened:
            self.app.open_result('draft-video-folder',view['project_id'])
        opened.assert_called_once_with(self.video.parent)
        self.assertEqual(self.app.state()['campaign_status'],'full_ready')

    def test_stale_manual_edits_and_foreign_output_paths_are_not_presented_as_current_draft(self):
        manifest=self.publish_fixture()
        self.app.review_cue(self.fixture.request(target_text='人工新译文',review_status='unchecked'))
        self.assertIsNone(self.app.preview('main').get('draft_video'))
        with self.assertRaises(ValueError):self.app.download_path('draft-video','main')
        manifest['draft_output']=str(self.source)
        self.fixture.write(self.campaign/'campaign.json',manifest)
        self.assertIsNone(self.app.preview('main').get('draft_video'))

    def test_damaged_video_is_rejected_and_unchanged_media_hash_is_reused(self):
        self.publish_fixture()
        with patch.object(studio.hashlib,'file_digest',wraps=studio.hashlib.file_digest) as digest:
            self.assertIsNotNone(self.app.preview('main')['draft_video'])
            self.assertEqual(self.app.download_path('draft-video','main'),self.video)
            self.assertEqual(digest.call_count,1)
            self.video.write_bytes(b'bad-video!!')
            self.assertIsNone(self.app.preview('main')['draft_video'])
            with self.assertRaises(ValueError):self.app.download_path('draft-video','main')


if __name__=='__main__':unittest.main()
