"""Direct full-draft review is explicit and independent of paid generation."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import media_export
from subtitle_pipeline.subtitles import Cue, render_srt, parse_srt
from subtitle_pipeline.integrity import sha256
from tests.test_cloud_workflow import write_complete_qwen_evidence


class ManualReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source.mp4'; self.source.write_bytes(b'fixture')
        self.campaign=self.root/'campaign'; self.folder=self.campaign/'整片'; self.folder.mkdir(parents=True)
        self.manifest={'source':{'path':str(self.source),'sha256':sha256(self.source)},
                       'language':'ja','target':'zh-CN','status':'full_ready','asr_provider':'qwen_asr','samples':[]}
        self.write(self.campaign/'campaign.json',self.manifest)
        self.write(self.folder/'需复核.json',[])
        for name,body in [('原文.srt','こんにちは'),('中文草稿.srt','你好'),('双语草稿.srt','你好\nこんにちは')]:
            (self.folder/name).write_text(render_srt([Cue(i*1500,i*1500+1000,body) for i in range(20)]),encoding='utf-8')
        write_complete_qwen_evidence(self.folder, self.source, 40000)
        self.assertTrue(workflow.state_is_complete(self.folder))

    def write(self,path,data): path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')

    def mark(self,count=20):
        from subtitle_pipeline.manual_review import load_review,save_review
        view=load_review(self.folder)
        for cue in view['cues'][:count]:
            view=save_review(self.folder,{'expected_revision':view['revision'],'cue_id':cue['id'],
                **{k:cue[k] for k in ('start_ms','end_ms','source_text','target_text')},
                'review_status':'checked','note':'已听看','translation_confirmed':False})
        return view

    def test_completed_draft_can_be_accepted_without_sample_approval_only_after_recorded_checks(self):
        view=self.mark()
        with patch.object(workflow.r,'validate_full_approval',side_effect=AssertionError('Already completed draft needs no new sample gate')):
            response=workflow.accept_final(self.campaign,True,expected_revision=view['revision'])
        self.assertTrue(response['accepted'])
        self.assertEqual(response['reviewed_cues'],20)
        self.assertFalse((self.campaign/'approval.json').exists())
        saved=workflow.read_json(self.campaign/'final-review.json')
        self.assertEqual(saved['manual_revision'],view['revision'])
        self.assertEqual(saved['reviewed_cue_ids'],list(range(1,21)))
        self.assertEqual(saved['status'],'sampled_approved')
        self.assertTrue(all(Path(a['path']).is_file() for a in saved['artifacts']))

    def test_checkbox_alone_insufficient_checks_changed_revision_and_missing_segments_are_rejected(self):
        view=self.mark(19)
        with self.assertRaises(ValueError): workflow.accept_final(self.campaign,True,expected_revision=view['revision'])
        view=self.mark()
        for flag,revision,complete in [(False,view['revision'],True),(True,'stale',True),(True,view['revision'],False)]:
            with patch.object(workflow,'state_is_complete',return_value=complete),self.assertRaises(ValueError):
                workflow.accept_final(self.campaign,flag,expected_revision=revision)
        self.assertFalse((self.campaign/'final-review.json').exists())

    def test_later_manual_change_or_deleted_review_record_blocks_export_before_encoding(self):
        from subtitle_pipeline.manual_review import load_review,save_review
        view=self.mark()
        workflow.accept_final(self.campaign,True,expected_revision=view['revision'])
        cue=view['cues'][0]
        save_review(self.folder,{'expected_revision':view['revision'],'cue_id':cue['id'],
            **{k:cue[k] for k in ('start_ms','end_ms','source_text','target_text')},
            'review_status':'issue','note':'再确认','translation_confirmed':False})
        with patch.object(workflow.r,'run_process') as encode:
            with self.assertRaises(ValueError): workflow.export_video(self.campaign,threading.Event())
            encode.assert_not_called()
        (self.folder/'人工校对'/'校对记录.json').unlink()
        with patch.object(workflow.r,'run_process') as encode:
            with self.assertRaises(ValueError): workflow.export_video(self.campaign,threading.Event())
            encode.assert_not_called()

    def test_export_uses_reviewed_target_snapshot_instead_of_machine_translation(self):
        from subtitle_pipeline.manual_review import save_review
        view=self.mark()
        cue=view['cues'][0]
        view=save_review(self.folder,{'expected_revision':view['revision'],'cue_id':cue['id'],
             'start_ms':100,'end_ms':1100,'source_text':'おはよう','target_text':'早上好',
             'review_status':'checked','note':'人工修改原文、译文和时间轴','translation_confirmed':True})
        class StopBeforeEncoding(Exception): pass
        workflow.accept_final(self.campaign,True,expected_revision=view['revision'])
        # The export inventories audio before considering old output bindings.
        # This test's file is synthetic bytes, not an ffprobe-readable video.
        with patch.object(media_export.environment,'select_encoder',return_value='qsv'), \
                patch.object(media_export,'media_info',return_value={'streams':[
                {'codec_type':'video'},{'codec_type':'audio'}]}), \
                patch.object(workflow.r,'run_process',side_effect=StopBeforeEncoding):
            with self.assertRaises(StopBeforeEncoding): workflow.export_video(self.campaign,threading.Event())
        text=(self.campaign/'导出'/'captions.srt').read_text(encoding='utf-8')
        self.assertEqual(parse_srt(text)[0],Cue(100,1100,'早上好'))
        self.assertNotIn('早上好',(self.folder/'中文草稿.srt').read_text(encoding='utf-8'))
        self.assertNotIn('おはよう',(self.folder/'原文.srt').read_text(encoding='utf-8'))


if __name__=='__main__': unittest.main()
