"""Draft video exports use temporary evidence and a fake local encoder only."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow
from subtitle_pipeline import manual_review
from subtitle_pipeline.languages import video_output_path
from subtitle_pipeline.subtitles import Cue, parse_srt, render_srt
from tests.test_cloud_workflow import write_complete_qwen_evidence


class DraftVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'synthetic source video')
        self.campaign = self.root / 'campaign'
        self.folder = self.campaign / '整片'
        self.folder.mkdir(parents=True)
        self.manifest = {'source': {'path': str(self.source), 'sha256': workflow.sha256(self.source)},
                         'language': 'ja', 'target': 'zh-CN', 'status': 'full_ready',
                         'asr_provider': 'qwen_asr', 'duration_ms': 40000, 'samples': []}
        self.write(self.campaign / 'campaign.json', self.manifest)
        self.write(self.folder / '需复核.json', [])
        for name, body in [('原文.srt', 'こんにちは'), ('中文草稿.srt', '你好'),
                           ('双语草稿.srt', '你好\nこんにちは')]:
            (self.folder / name).write_text(render_srt([
                Cue(i * 1500, i * 1500 + 1000, body) for i in range(20)]), encoding='utf-8')
        write_complete_qwen_evidence(self.folder, self.source, 40000)
        self.stop = threading.Event()
        guards = ExitStack()
        self.addCleanup(guards.close)
        guards.enter_context(patch('socket.create_connection', side_effect=AssertionError('No network')))
        guards.enter_context(patch.object(workflow, 'emit'))
        guards.enter_context(patch.object(workflow, 'media_info', return_value={
            'streams': [{'codec_type': 'video', 'width': 640, 'height': 360,
                         'r_frame_rate': '30/1', 'nb_frames': '1200'}],
            'format': {'duration': '40.0'}}))
        guards.enter_context(patch.object(workflow, 'audio_digest', return_value='same-audio-hash'))
        self.encoder = guards.enter_context(patch.object(workflow.r, 'run_process', side_effect=self.fake_encode))

    def write(self, path, value):
        workflow.r.atomic_json(path, value)

    def fake_encode(self, args, log, stop, **kwargs):
        output = Path(args[-1])
        if output.suffix == '.ass':
            output.write_text('[Script Info]\nPlayResX: 640\nPlayResY: 360\nStyle: Default,Arial\n', encoding='utf-8')
        elif output.suffix == '.mp4':
            output.write_bytes(b'synthetic encoded video')

    def edit(self, **changes):
        view = manual_review.load_review(self.folder)
        cue = view['cues'][0]
        return manual_review.save_review(self.folder, {
            'expected_revision': view['revision'], 'cue_id': cue['id'],
            **{key: cue[key] for key in ('start_ms', 'end_ms', 'source_text', 'target_text', 'review_status')},
            'note': '', 'translation_confirmed': False, **changes,
        })

    def test_draft_names_are_separate_for_every_target_language(self):
        for target, label in [('zh-CN', '中文字幕'), ('en', '英文字幕'), ('ja', '日文字幕')]:
            with self.subTest(target=target):
                self.assertEqual(video_output_path(self.source, target, draft=True).name,
                                 'source_' + label + '_未审核草稿.mp4')
                self.assertEqual(video_output_path(self.source, target).name,
                                 'source_' + label + '_修订版.mp4')

    def test_unreviewed_machine_draft_exports_without_approval_or_status_changes(self):
        prior = {path: path.read_bytes() for path in self.folder.rglob('*') if path.is_file()}
        workflow.export_video(self.campaign, self.stop, draft=True)
        saved = workflow.read_json(self.campaign / 'campaign.json')
        self.assertEqual(saved['status'], 'full_ready')
        self.assertTrue(Path(saved['draft_output']).is_file())
        self.assertEqual(saved['draft_output_sha256'], workflow.sha256(saved['draft_output']))
        binding = saved['draft_output_binding']
        self.assertEqual(binding['review_status'], 'unreviewed_draft')
        self.assertIsNone(binding['manual_revision'])
        self.assertEqual((binding['language'], binding['target']), ('ja', 'zh-CN'))
        self.assertEqual(binding['machine_source_sha256'], workflow.sha256(self.folder / '原文.srt'))
        self.assertEqual(binding['machine_target_sha256'], workflow.sha256(self.folder / '中文草稿.srt'))
        self.assertFalse((self.campaign / 'approval.json').exists())
        self.assertFalse((self.campaign / 'final-review.json').exists())
        self.assertNotIn('output', saved)
        self.assertEqual({path: path.read_bytes() for path in prior}, prior)
        verification = workflow.read_json(self.campaign / '导出' / '草稿视频' / 'verification.json')
        self.assertEqual(verification['output_binding']['review_status'], 'unreviewed_draft')

    def test_unchecked_issues_and_pending_translation_keep_current_manual_edits(self):
        self.edit(target_text='人工草稿', review_status='issue')
        view = self.edit(source_text='おはよう', review_status='issue')
        self.assertEqual((view['summary']['checked'], view['summary']['issues'],
                          view['summary']['pending_translation']), (0, 1, 1))
        record = self.folder / '人工校对' / '校对记录.json'
        before = record.read_bytes()
        workflow.export_video(self.campaign, self.stop, draft=True)
        captions = self.campaign / '导出' / '草稿视频' / 'captions.srt'
        self.assertEqual(parse_srt(captions.read_text(encoding='utf-8'))[0].text, '人工草稿')
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse((self.campaign / 'final-review.json').exists())
        self.assertEqual(workflow.read_json(self.campaign / 'campaign.json')['draft_output_binding']['manual_revision'],
                         view['revision'])

    def test_formal_export_still_requires_approval(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            workflow.export_video(self.campaign, self.stop)
        self.encoder.assert_not_called()

    def test_draft_keeps_existing_review_and_formal_export_evidence_unchanged(self):
        self.write(self.campaign / 'approval.json', {'existing': 'sample-review'})
        self.write(self.campaign / 'final-review.json', {'existing': 'final-review'})
        self.manifest.update(status='exported', output='old-reviewed.mp4',
                             output_sha256='reviewed-hash', output_binding={'reviewed': True})
        self.write(self.campaign / 'campaign.json', self.manifest)
        before = {name: (self.campaign / name).read_bytes() for name in ('approval.json', 'final-review.json')}
        workflow.export_video(self.campaign, self.stop, draft=True)
        saved = workflow.read_json(self.campaign / 'campaign.json')
        for name in ('status', 'output', 'output_sha256', 'output_binding'):
            self.assertEqual(saved[name], self.manifest[name])
        self.assertEqual({name: (self.campaign / name).read_bytes() for name in before}, before)

    def test_missing_part_or_changed_source_blocks_before_encoding(self):
        (self.folder / '片段' / '0001' / 'source.local.srt').unlink()
        with self.assertRaises(ValueError): workflow.export_video(self.campaign, self.stop, draft=True)
        write_complete_qwen_evidence(self.folder, self.source, 40000)
        self.source.write_bytes(b'changed source')
        with self.assertRaises(ValueError): workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()

    def test_truncated_machine_base_blocks_despite_complete_parts(self):
        for name in ('原文.srt', '中文草稿.srt'):
            path = self.folder / name
            path.write_text(render_srt(parse_srt(path.read_text(encoding='utf-8'))[:1]), encoding='utf-8')
        self.assertTrue(workflow.state_is_complete(self.folder))
        with self.assertRaisesRegex(ValueError, '合并原文|片段|基底'):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()

    def test_corrupt_or_changed_manual_base_blocks_before_encoding(self):
        self.edit(target_text='人工草稿')
        record = self.folder / '人工校对' / '校对记录.json'
        before = record.read_bytes()
        record.write_text('{invalid', encoding='utf-8')
        with self.assertRaises(ValueError): workflow.export_video(self.campaign, self.stop, draft=True)
        record.write_bytes(before)
        captions = self.folder / '中文草稿.srt'
        captions.write_text(captions.read_text(encoding='utf-8').replace('你好', '改变'), encoding='utf-8')
        with self.assertRaises(ValueError): workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()

    def test_missing_previously_exported_manual_record_cannot_fall_back_to_machine(self):
        self.edit(target_text='人工草稿')
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.reset_mock()
        (self.folder / '人工校对' / '校对记录.json').unlink()
        with self.assertRaisesRegex(ValueError, '人工|记录|缺失'):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()

    def test_missing_accepted_manual_record_also_blocks_a_first_draft(self):
        self.write(self.campaign / 'final-review.json', {'manual_revision': 'a' * 64})
        with self.assertRaisesRegex(ValueError, '人工|记录|缺失'):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()

    def test_active_generation_lock_prevents_reading_or_encoding_a_draft(self):
        with workflow.r.ProjectLock(self.folder), self.assertRaises(RuntimeError):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()

    def test_failed_media_verification_never_publishes_or_records_a_draft(self):
        with patch.object(workflow, 'audio_digest', side_effect=['source-audio', 'different-audio']):
            with self.assertRaisesRegex(ValueError, '音频'):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertNotIn('draft_output', workflow.read_json(self.campaign / 'campaign.json'))

    def test_english_and_japanese_drafts_burn_the_selected_target_track(self):
        for language, target, filename, text in [('ja', 'en', '英文草稿.srt', 'Hello'),
                                                 ('en', 'ja', '日文草稿.srt', 'おはよう')]:
            with self.subTest(target=target):
                cues = parse_srt((self.folder / '原文.srt').read_text(encoding='utf-8'))
                (self.folder / filename).write_text(render_srt([
                    Cue(c.start_ms, c.end_ms, text) for c in cues]), encoding='utf-8')
                self.manifest.update(language=language, target=target)
                self.write(self.campaign / 'campaign.json', self.manifest)
                write_complete_qwen_evidence(self.folder, self.source, 40000, language=language, target=target)
                workflow.export_video(self.campaign, self.stop, draft=True)
                captions = self.campaign / '导出' / '草稿视频' / 'captions.srt'
                self.assertEqual(parse_srt(captions.read_text(encoding='utf-8'))[0].text, text)
                self.assertTrue(video_output_path(self.source, target, draft=True).exists())

    def test_identical_draft_reuses_verified_file_but_modified_subtitles_never_overwrite_it(self):
        workflow.export_video(self.campaign, self.stop, draft=True)
        final = video_output_path(self.source, 'zh-CN', draft=True)
        before = final.read_bytes()
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()
        self.edit(target_text='新草稿')
        with self.assertRaisesRegex(ValueError, '已存在|覆盖'):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()
        self.assertEqual(final.read_bytes(), before)

    def test_cli_has_distinct_draft_action(self):
        self.assertEqual(workflow.main(['export-draft', '--campaign', str(self.campaign)]), 0)
        self.assertTrue(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertFalse((self.campaign / 'final-review.json').exists())

    def video_encode_count(self):
        return sum(Path(call.args[0][-1]).suffix == '.mp4' for call in self.encoder.call_args_list)

    def decode_check_count(self):
        return sum('-f' in call.args[0] and 'null' in call.args[0]
                   for call in self.encoder.call_args_list)

    def fail_during_copy(self):
        with patch.object(workflow.shutil, 'copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                workflow.export_video(self.campaign, self.stop, draft=True)

    def test_missing_mkv_frame_count_is_not_a_parameter_mismatch(self):
        source_info = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360,
                                   'r_frame_rate': '60/2'}], 'format': {'duration': '40'}}
        output_info = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360,
                                   'r_frame_rate': '30/1', 'nb_frames': '1200'}],
                       'format': {'duration': '40'}}
        with patch.object(workflow, 'media_info', side_effect=[source_info, output_info]):
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertTrue(video_output_path(self.source, 'zh-CN', draft=True).exists())

    def test_known_frame_count_mismatch_still_blocks_publication(self):
        def info(path, *a, **kw):
            return {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360,
                                 'r_frame_rate': '30/1',
                                 'nb_frames': '1200' if Path(path) == self.source else '1190'}],
                    'format': {'duration': '40'}}
        with patch.object(workflow, 'media_info', side_effect=info):
            with self.assertRaisesRegex(ValueError, 'nb_frames'):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())

    def fail_after_encoding(self):
        with patch.object(workflow, 'audio_digest', side_effect=OSError('temporary read failure')):
            with self.assertRaises(OSError):
                workflow.export_video(self.campaign, self.stop, draft=True)

    def cancel_post_encode_source_hash(self):
        original=workflow.cancellable_sha256
        def cancel(path,stop,**kwargs):
            # The workflow alias now reads source only after encoding; the
            # generation verifier performs its independent preflight read.
            if Path(path)==self.source:
                stop.set()
                raise workflow.r.Cancelled('cancel post-encode source verification')
            return original(path,stop,**kwargs)
        with patch.object(workflow,'cancellable_sha256',side_effect=cancel):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.stop.clear()
        return self.campaign/'导出'/'草稿视频'/'encoding-checkpoint.json'

    def test_cancelled_source_validation_resumes_sealed_output_without_publication_cache(self):
        checkpoint=self.cancel_post_encode_source_hash()
        receipt=workflow.read_json(checkpoint)
        partial=checkpoint.parent/'result.partial.mp4'
        self.assertEqual(receipt['status'],'pending_source_validation')
        self.assertEqual(receipt['sha256'],workflow.sha256(partial))
        self.assertEqual(receipt['size_bytes'],partial.stat().st_size)
        self.assertEqual(len(receipt['source_token']),5)
        self.encoder.reset_mock()
        with patch.object(workflow,'audio_digest',return_value='same-audio-hash') as audio, \
                patch.object(workflow,'_publication_evidence',side_effect=AssertionError('pending output needs first media validation')), \
                patch.object(workflow,'cancellable_sha256',wraps=workflow.cancellable_sha256) as hashes:
            workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(audio.call_count,2)
        self.assertEqual(self.decode_check_count(),3)
        hashed=[Path(call.args[0]) for call in hashes.call_args_list]
        self.assertEqual(hashed.count(self.source),1)
        self.assertEqual(hashed.count(partial),1)
        self.assertEqual(workflow.read_json(checkpoint)['status'],'encoded')

    def test_repeated_cancellation_keeps_pending_hash_evidence_without_reencoding(self):
        checkpoint=self.cancel_post_encode_source_hash()
        before=checkpoint.read_bytes()
        self.encoder.reset_mock()
        self.cancel_post_encode_source_hash()
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(checkpoint.read_bytes(),before)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)

    def test_cancelled_pending_output_rehash_keeps_receipt_for_later_verification(self):
        checkpoint=self.cancel_post_encode_source_hash()
        before=checkpoint.read_bytes()
        original=workflow.cancellable_sha256
        def cancel(path,stop,**kwargs):
            if Path(path).name=='result.partial.mp4':
                stop.set()
                raise workflow.r.Cancelled('cancel repeated output verification')
            return original(path,stop,**kwargs)
        self.encoder.reset_mock()
        with patch.object(workflow,'cancellable_sha256',side_effect=cancel):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(checkpoint.read_bytes(),before)
        self.stop.clear();self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)

    def test_failure_after_pending_checkpoint_commit_can_resume_without_reencoding(self):
        original=workflow.r.atomic_json
        def crash_after_commit(path,value):
            result=original(path,value)
            if Path(path).name=='encoding-checkpoint.json' and value.get('status')=='pending_source_validation':
                raise OSError('simulated interruption after pending checkpoint commit')
            return result
        with patch.object(workflow.r,'atomic_json',side_effect=crash_after_commit):
            with self.assertRaisesRegex(OSError,'pending checkpoint commit'):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(self.decode_check_count(),3)

    def test_cancellation_before_partial_hash_completion_is_never_marked_pending(self):
        original=workflow.cancellable_sha256
        def cancel(path,stop,**kwargs):
            if Path(path).name=='result.partial.mp4':
                stop.set()
                raise workflow.r.Cancelled('partial hash incomplete')
            return original(path,stop,**kwargs)
        with patch.object(workflow,'cancellable_sha256',side_effect=cancel):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign,self.stop,draft=True)
        folder=self.campaign/'导出'/'草稿视频'
        self.assertEqual(workflow.read_json(folder/'encoding-checkpoint.json')['status'],'encoding')
        self.stop.clear();self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),1)
        self.assertTrue(list((folder/'保留的编码').glob('*.mp4')))

    def test_pending_source_token_change_blocks_reuse_even_with_identical_source_bytes(self):
        checkpoint=self.cancel_post_encode_source_hash()
        stat=self.source.stat()
        os.utime(self.source,ns=(stat.st_atime_ns,stat.st_mtime_ns+1_000_000_000))
        self.encoder.reset_mock()
        with self.assertRaisesRegex(ValueError,'源视频|原始视频|素材'):
            workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(workflow.read_json(checkpoint)['status'],'pending_source_validation')
        self.assertTrue((checkpoint.parent/'result.partial.mp4').is_file())

    def test_pending_source_full_hash_rejects_same_size_and_mtime_content_change(self):
        checkpoint=self.cancel_post_encode_source_hash()
        stat=self.source.stat()
        self.source.write_bytes(b'x'*stat.st_size)
        os.utime(self.source,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.encoder.reset_mock()
        with self.assertRaisesRegex(ValueError,'源视频|原始视频|素材'):
            workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(workflow.read_json(checkpoint)['status'],'pending_source_validation')

    def test_pending_partial_full_hash_rejects_same_size_and_mtime_content_change(self):
        checkpoint=self.cancel_post_encode_source_hash()
        partial=checkpoint.parent/'result.partial.mp4'
        stat=partial.stat();changed=b'x'*stat.st_size
        partial.write_bytes(changed)
        os.utime(partial,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),1)
        self.assertTrue(any(path.read_bytes()==changed for path in (checkpoint.parent/'保留的编码').glob('*.mp4')))

    def test_pending_subtitle_change_preserves_old_encode_and_requires_new_encoding(self):
        checkpoint=self.cancel_post_encode_source_hash()
        self.edit(target_text='新的字幕版本')
        self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),1)
        self.assertTrue(list((checkpoint.parent/'保留的编码').glob('*.mp4')))

    def test_pending_encoder_parameter_change_cannot_reuse_encode(self):
        checkpoint=self.cancel_post_encode_source_hash()
        receipt=workflow.read_json(checkpoint)
        args=receipt['identity']['encoder_args']
        args[args.index('-preset')+1]='different-old-setting'
        self.write(checkpoint,receipt)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),1)
        self.assertTrue(list((checkpoint.parent/'保留的编码').glob('*.mp4')))

    def test_pending_receipt_damage_never_weakens_output_proof(self):
        checkpoint=self.cancel_post_encode_source_hash()
        original=checkpoint.read_bytes()
        for corruption in ('json','identity','source_token','token_shape','token_bool','digest','digest_mismatch',
                           'size','size_bool','size_zero','size_mismatch','status'):
            with self.subTest(corruption=corruption):
                checkpoint.write_bytes(original)
                receipt=workflow.read_json(checkpoint)
                if corruption=='json':checkpoint.write_text('{broken',encoding='utf-8')
                else:
                    if corruption=='identity':receipt.pop('identity')
                    elif corruption=='source_token':receipt.pop('source_token',None)
                    elif corruption=='token_shape':receipt['source_token']=[1]
                    elif corruption=='token_bool':receipt['source_token']=[True]*5
                    elif corruption=='digest':receipt['sha256']='incomplete'
                    elif corruption=='digest_mismatch':receipt['sha256']='0'*64
                    elif corruption=='size':receipt.pop('size_bytes',None)
                    elif corruption=='size_bool':receipt['size_bytes']=True
                    elif corruption=='size_zero':receipt['size_bytes']=0
                    elif corruption=='size_mismatch':receipt['size_bytes']+=1
                    elif corruption=='status':receipt['status']='old_pending_format'
                    self.write(checkpoint,receipt)
                self.encoder.reset_mock()
                self.fail_after_encoding()
                self.assertEqual(self.video_encode_count(),1)
                self.assertTrue(list((checkpoint.parent/'保留的编码').glob('*.mp4')))

    def test_source_changes_during_pending_source_hash_cannot_complete_checkpoint(self):
        checkpoint=self.cancel_post_encode_source_hash()
        original=workflow.cancellable_sha256
        def changed_after_hash(path,stop,**kwargs):
            digest=original(path,stop,**kwargs)
            if Path(path)==self.source:
                stat=self.source.stat()
                os.utime(self.source,ns=(stat.st_atime_ns,stat.st_mtime_ns+1_000_000_000))
            return digest
        self.encoder.reset_mock()
        with patch.object(workflow,'cancellable_sha256',side_effect=changed_after_hash):
            with self.assertRaisesRegex(ValueError,'源视频|素材'):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(workflow.read_json(checkpoint)['status'],'pending_source_validation')

    def test_pending_requires_source_content_hash_even_when_all_source_tokens_match(self):
        checkpoint=self.cancel_post_encode_source_hash()
        original=workflow.cancellable_sha256
        def changed_digest(path,stop,**kwargs):
            if Path(path)==self.source:return '0'*64
            return original(path,stop,**kwargs)
        self.encoder.reset_mock()
        with patch.object(workflow,'cancellable_sha256',side_effect=changed_digest):
            with self.assertRaisesRegex(ValueError,'源视频|素材'):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(workflow.read_json(checkpoint)['status'],'pending_source_validation')

    def assert_changes_during_partial_hash_are_not_sealed(self,changed):
        original=workflow.cancellable_sha256
        def change_after_hash(path,stop,**kwargs):
            digest=original(path,stop,**kwargs)
            path=Path(path)
            if path.name=='result.partial.mp4':
                if changed=='source':
                    stat=self.source.stat()
                    os.utime(self.source,ns=(stat.st_atime_ns,stat.st_mtime_ns+1_000_000_000))
                elif changed=='subtitle':
                    (path.parent/'captions.ass').write_text('changed while sealing',encoding='utf-8')
                else:path.write_bytes(b'changed while hashing')
            return digest
        with patch.object(workflow,'cancellable_sha256',side_effect=change_after_hash):
            with self.assertRaises(ValueError):
                workflow.export_video(self.campaign,self.stop,draft=True)
        checkpoint=self.campaign/'导出'/'草稿视频'/'encoding-checkpoint.json'
        self.assertEqual(workflow.read_json(checkpoint)['status'],'encoding')
        self.assertTrue((checkpoint.parent/'result.partial.mp4').is_file())

    def test_source_changed_during_partial_hash_cannot_be_sealed(self):
        self.assert_changes_during_partial_hash_are_not_sealed('source')

    def test_subtitle_changed_during_partial_hash_cannot_be_sealed(self):
        self.assert_changes_during_partial_hash_are_not_sealed('subtitle')

    def test_partial_changed_during_its_hash_cannot_be_sealed(self):
        self.assert_changes_during_partial_hash_are_not_sealed('partial')

    def assert_completed_encode_rehash_changes_block_publication(self,changed):
        self.fail_during_copy()
        original=workflow.cancellable_sha256
        def change_after_hash(path,stop,**kwargs):
            digest=original(path,stop,**kwargs)
            path=Path(path)
            if path.name=='result.partial.mp4':
                if changed=='source':
                    stat=self.source.stat()
                    self.source.write_bytes(b'x'*stat.st_size)
                    os.utime(self.source,ns=(stat.st_atime_ns,stat.st_mtime_ns))
                elif changed in ('ass','srt'):
                    (path.parent/('captions.'+changed)).write_text('changed during completed output validation',encoding='utf-8')
                else:path.write_bytes(b'replaced during completed output validation')
            return digest
        self.encoder.reset_mock()
        with patch.object(workflow,'cancellable_sha256',side_effect=change_after_hash), \
                patch.object(workflow,'_publication_evidence',wraps=workflow._publication_evidence) as evidence:
            with self.assertRaises(ValueError):
                workflow.export_video(self.campaign,self.stop,draft=True)
        evidence.assert_not_called()
        self.assertEqual(self.video_encode_count(),0)
        self.assertFalse(video_output_path(self.source,'zh-CN',draft=True).exists())
        self.assertTrue((self.campaign/'导出'/'草稿视频'/'result.partial.mp4').is_file())

    def test_encoded_resume_rechecks_source_bytes_even_with_same_size_and_mtime(self):
        self.assert_completed_encode_rehash_changes_block_publication('source')

    def test_encoded_resume_checks_ass_again_before_using_publication_evidence(self):
        self.assert_completed_encode_rehash_changes_block_publication('ass')

    def test_encoded_resume_checks_srt_again_before_using_publication_evidence(self):
        self.assert_completed_encode_rehash_changes_block_publication('srt')

    def test_encoded_resume_checks_partial_stability_before_using_publication_evidence(self):
        self.assert_completed_encode_rehash_changes_block_publication('partial')

    def test_retry_after_validation_failure_reuses_completed_encoding(self):
        self.fail_after_encoding()
        self.assertEqual(self.video_encode_count(), 1)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        self.assertTrue(video_output_path(self.source, 'zh-CN', draft=True).exists())

    def test_modified_subtitles_invalidate_encoding_checkpoint(self):
        self.fail_after_encoding()
        self.edit(target_text='新版草稿')
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)

    def test_corrupt_partial_cannot_be_resumed(self):
        self.fail_after_encoding()
        partial = self.campaign / '导出' / '草稿视频' / 'result.partial.mp4'
        partial.write_bytes(b'corrupt partial')
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)
        self.assertTrue(any(path.read_bytes() == b'corrupt partial'
                            for path in (partial.parent / '保留的编码').glob('*.mp4')))

    def test_legacy_partial_without_receipt_is_preserved_not_assumed_current(self):
        folder = self.campaign / '导出' / '草稿视频'
        folder.mkdir(parents=True)
        (folder / 'result.partial.mp4').write_bytes(b'legacy encoding')
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)
        self.assertTrue(any(path.read_bytes() == b'legacy encoding'
                            for path in (folder / '保留的编码').glob('*.mp4')))

    def test_failed_encoder_cannot_create_a_completed_checkpoint(self):
        def fail(args, *a, **kw):
            self.fake_encode(args, *a, **kw)
            if Path(args[-1]).suffix == '.mp4': raise workflow.r.Cancelled('stopped')
        with patch.object(workflow.r, 'run_process', side_effect=fail):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)

    def test_source_changed_during_encoding_cannot_be_cached_as_original(self):
        original = self.source.read_bytes()
        def change_source(args, *a, **kw):
            self.fake_encode(args, *a, **kw)
            if Path(args[-1]).suffix == '.mp4': self.source.write_bytes(b'changed video')
        with patch.object(workflow.r, 'run_process', side_effect=change_source):
            with self.assertRaisesRegex(ValueError, '素材|源视频'):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.source.write_bytes(original)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)

    def test_ass_changed_during_encoding_cannot_be_cached_as_original(self):
        def change_ass(args, *a, **kw):
            self.fake_encode(args, *a, **kw)
            if Path(args[-1]).suffix == '.mp4':
                (Path(args[-1]).parent / 'captions.ass').write_text('edited while encoding', encoding='utf-8')
        with patch.object(workflow.r, 'run_process', side_effect=change_ass):
            with self.assertRaisesRegex(ValueError, '字幕'):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)

    def test_retry_after_failed_copy_does_not_repeat_encoding(self):
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            self.fail_during_copy()
        self.assertEqual(audio.call_count, 2)
        self.assertEqual(self.decode_check_count(), 3)
        self.encoder.reset_mock()
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio, \
                patch.object(workflow, 'cancellable_sha256', wraps=workflow.cancellable_sha256) as hashes:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        audio.assert_not_called()
        self.assertEqual(self.decode_check_count(), 0)
        hashed = [Path(call.args[0]) for call in hashes.call_args_list]
        partial = self.campaign / '导出' / '草稿视频' / 'result.partial.mp4'
        self.assertEqual(hashed.count(self.source), 1)
        self.assertEqual(hashed.count(partial), 1)
        self.assertEqual(sum(path.parent == self.source.parent and path.suffix == '.tmp' for path in hashed), 1)

    def test_changed_partial_cannot_reuse_old_media_validation_after_reencoding(self):
        self.fail_during_copy()
        partial = self.campaign / '导出' / '草稿视频' / 'result.partial.mp4'
        partial.write_bytes(b'changed encoded video')
        self.encoder.reset_mock()
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)
        self.assertEqual(audio.call_count, 2)
        self.assertEqual(self.decode_check_count(), 3)

    def test_changed_subtitles_cannot_reuse_old_media_validation(self):
        self.fail_during_copy()
        self.edit(target_text='修改后的字幕')
        self.encoder.reset_mock()
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)
        self.assertEqual(audio.call_count, 2)
        self.assertEqual(self.decode_check_count(), 3)

    def test_changed_encoder_parameters_cannot_reuse_old_media_validation(self):
        self.fail_during_copy()
        checkpoint = self.campaign / '导出' / '草稿视频' / 'encoding-checkpoint.json'
        saved = workflow.read_json(checkpoint)
        args = saved['identity']['encoder_args']
        args[args.index('-preset') + 1] = 'slower-old-setting'
        self.write(checkpoint, saved)
        self.encoder.reset_mock()
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 1)
        self.assertEqual(audio.call_count, 2)
        self.assertEqual(self.decode_check_count(), 3)

    def test_incomplete_or_corrupt_validation_receipt_repeats_media_checks(self):
        self.fail_during_copy()
        receipt = self.campaign / '导出' / '草稿视频' / 'publication-checkpoint.json'
        original = receipt.read_bytes()
        for corruption in ('invalid_json', 'missing_video', 'invalid_duration', 'mismatched_duration',
                           'mismatched_dimensions', 'invalid_rate', 'invalid_stream_type', 'invalid_audio', 'old_version'):
            with self.subTest(corruption=corruption):
                receipt.write_bytes(original)
                saved = workflow.read_json(receipt)
                if corruption == 'invalid_json':
                    receipt.write_text('{invalid', encoding='utf-8')
                else:
                    if corruption == 'missing_video': saved['verification'].pop('video_fields')
                    elif corruption == 'invalid_duration': saved['verification']['duration'] = 'NaN'
                    elif corruption == 'mismatched_duration': saved['verification']['duration'] = '45'
                    elif corruption == 'mismatched_dimensions': saved['verification']['video_fields']['width'] = 1920
                    elif corruption == 'invalid_rate': saved['verification']['video_fields']['r_frame_rate'] = '0/0'
                    elif corruption == 'invalid_stream_type': saved['verification']['video_fields']['codec_type'] = 'audio'
                    elif corruption == 'invalid_audio':
                        saved['verification']['source_audio_sha256'] = ['invalid']
                        saved['verification']['output_audio_sha256'] = ['invalid']
                    elif corruption == 'old_version': saved['version'] = 0
                    self.write(receipt, saved)
                self.encoder.reset_mock()
                with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
                    self.fail_during_copy()
                self.assertEqual(self.video_encode_count(), 0)
                self.assertEqual(audio.call_count, 2)
                self.assertEqual(self.decode_check_count(), 3)

    def test_missing_frame_count_field_is_not_complete_validation_evidence(self):
        self.fail_during_copy()
        receipt = self.campaign / '导出' / '草稿视频' / 'publication-checkpoint.json'
        saved = workflow.read_json(receipt)
        saved['verification']['video_fields'].pop('nb_frames')
        self.write(receipt, saved)
        source_info = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360,
                                   'r_frame_rate': '30/1'}], 'format': {'duration': '40'}}
        self.encoder.reset_mock()
        with patch.object(workflow, 'media_info', return_value=source_info), \
                patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        self.assertEqual(audio.call_count, 2)
        self.assertEqual(self.decode_check_count(), 3)

    def test_explicit_unknown_frame_count_remains_valid_cached_evidence(self):
        info = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360,
                             'r_frame_rate': '30/1'}], 'format': {'duration': '40'}}
        with patch.object(workflow, 'media_info', return_value=info):
            self.fail_during_copy()
            receipt = self.campaign / '导出' / '草稿视频' / 'publication-checkpoint.json'
            self.assertIsNone(workflow.read_json(receipt)['verification']['video_fields']['nb_frames'])
            self.encoder.reset_mock()
            with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        audio.assert_not_called()
        self.assertEqual(self.decode_check_count(), 0)

    def test_retry_after_published_video_but_failed_manifest_repairs_metadata(self):
        with patch.object(workflow, 'write_manifest', side_effect=OSError('temporary write failure')):
            with self.assertRaises(OSError):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertTrue(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertNotIn('draft_output', workflow.read_json(self.campaign / 'campaign.json'))
        self.encoder.reset_mock()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        self.assertIn('draft_output', workflow.read_json(self.campaign / 'campaign.json'))

    def assert_cancelled_publication_record(self, filename, *, deny=False, reuse=None):
        folder=self.campaign/'导出'/'草稿视频'
        final=video_output_path(self.source,'zh-CN',draft=True)
        if reuse=='verification':
            workflow.export_video(self.campaign,self.stop,draft=True)
            (folder/'verification.json').unlink()
        elif reuse=='manifest':
            with patch.object(workflow,'write_manifest',side_effect=OSError('fixture record pending')):
                with self.assertRaises(OSError):
                    workflow.export_video(self.campaign,self.stop,draft=True)
        self.encoder.reset_mock();workflow.emit.reset_mock()
        destination=self.campaign/'campaign.json' if filename=='campaign.json' else folder/filename
        replace=os.replace
        failure=PermissionError('synthetic publication sharing conflict')
        failure.winerror=32
        attempts=[]
        def stopped(source,target):
            if Path(target)==destination:
                attempts.append(True)
                self.stop.set()
                if deny:raise failure
            return replace(source,target)
        with patch.object(workflow.r.os,'replace',side_effect=stopped), \
                patch('subtitle_pipeline.atomic_io.time.sleep') as sleeps, \
                self.assertRaises(workflow.r.Cancelled) as caught:
            workflow.export_video(self.campaign,self.stop,draft=True)
        if deny:
            self.assertIs(caught.exception.__cause__,failure)
            self.assertEqual(len(attempts),6 if os.name=='nt' else 1)
            self.assertEqual([call.args[0] for call in sleeps.call_args_list],
                             [.02,.04,.08,.16,.32] if os.name=='nt' else [])
        else:self.assertEqual(len(attempts),1)
        self.assertFalse(any(call.kwargs.get('export_progress',{}).get('phase')=='done'
                             for call in workflow.emit.call_args_list))
        self.assertFalse(any(call.kwargs.get('status')=='draft_exported'
                             for call in workflow.emit.call_args_list))
        self.assertTrue(final.is_file())
        saved=final.read_bytes()
        self.assertEqual(workflow.read_json(folder/'encoding-checkpoint.json')['status'],'encoded')
        self.assertTrue((folder/'publication-checkpoint.json').is_file())
        self.assertTrue((folder/'result.partial.mp4').is_file())
        if reuse is not None:self.assertEqual(self.video_encode_count(),0)
        self.stop.clear();self.encoder.reset_mock()
        with patch.object(workflow,'_publish_video',side_effect=AssertionError('already published')):
            workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),0)
        self.assertEqual(final.read_bytes(),saved)
        self.assertEqual(workflow.read_json(self.campaign/'campaign.json')['draft_output'],str(final))
        self.assertTrue((folder/'verification.json').is_file())

    def test_cancel_during_final_verification_replace_never_emits_done(self):
        self.assert_cancelled_publication_record('verification.json')

    def test_cancel_during_final_manifest_replace_never_emits_done(self):
        self.assert_cancelled_publication_record('campaign.json')

    def test_cancel_and_sharing_error_during_final_manifest_preserves_cause(self):
        self.assert_cancelled_publication_record('campaign.json',deny=True)

    def test_cancel_during_reused_final_verification_repair_never_emits_done(self):
        self.assert_cancelled_publication_record('verification.json',reuse='verification')

    def test_cancel_and_sharing_error_during_reused_final_repair_preserves_cause(self):
        self.assert_cancelled_publication_record('verification.json',deny=True,reuse='verification')

    def test_cancel_during_published_final_manifest_repair_never_emits_done(self):
        self.assert_cancelled_publication_record('campaign.json',reuse='manifest')

    def test_already_cancelled_export_does_not_probe_convert_or_encode(self):
        self.stop.set()
        with patch.object(workflow, 'media_info') as probe:
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.encoder.assert_not_called()
        probe.assert_not_called()
        self.assertFalse((self.campaign / '导出').exists())

    def test_cancel_during_audio_validation_keeps_encode_but_never_publishes(self):
        calls=[]
        def digest(path, *a, **kw):
            calls.append(path)
            self.stop.set()
            return 'same-audio-hash'
        with patch.object(workflow, 'audio_digest', side_effect=digest):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertEqual(workflow.read_json(self.campaign / '导出' / '草稿视频' / 'encoding-checkpoint.json')['status'], 'encoded')
        self.stop.clear()
        self.encoder.reset_mock()
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        self.assertEqual(audio.call_count, 2)
        self.assertEqual(self.decode_check_count(), 3)

    def test_cancel_during_destination_copy_retains_encode_and_leaves_no_final(self):
        def interrupt_copy(source, destination, *a):
            destination.write(source.read(4))
            self.stop.set()
            destination.write(source.read(4))
        with patch.object(workflow.shutil, 'copyfileobj', side_effect=interrupt_copy):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertFalse(any(path.suffix == '.tmp' for path in self.root.iterdir()))
        self.stop.clear()
        self.encoder.reset_mock()
        with patch.object(workflow, 'audio_digest', return_value='same-audio-hash') as audio:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(self.video_encode_count(), 0)
        audio.assert_not_called()
        self.assertEqual(self.decode_check_count(), 0)

    @unittest.skipUnless(os.name=='nt','Windows rename sharing conflicts')
    def test_transient_publication_conflicts_retry_same_validated_copy(self):
        final=video_output_path(self.source,'zh-CN',draft=True)
        original=workflow.os.rename
        attempts=[]
        def sharing(src,dst):
            if Path(dst)==final:
                attempts.append((Path(src),Path(dst)))
                if len(attempts)<3:
                    error=PermissionError('temporary sharing violation');error.winerror=32
                    raise error
            return original(src,dst)
        with patch.object(workflow.os,'rename',side_effect=sharing), \
                patch.object(self.stop,'wait',return_value=False), \
                patch.object(workflow,'cancellable_copy',wraps=workflow.cancellable_copy) as copy, \
                patch.object(workflow,'cancellable_sha256',wraps=workflow.cancellable_sha256) as hashes:
            workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(self.video_encode_count(),1)
        self.assertEqual(copy.call_count,1)
        self.assertEqual(len(attempts),3)
        self.assertEqual(len({src for src,_ in attempts}),1)
        self.assertEqual(sum(Path(call.args[0]).suffix=='.tmp' for call in hashes.call_args_list),1)
        self.assertEqual(final.read_bytes(),b'synthetic encoded video')

    @unittest.skipUnless(os.name=='nt','Windows rename sharing conflicts')
    def test_competing_final_appearing_during_backoff_is_never_overwritten(self):
        final=video_output_path(self.source,'zh-CN',draft=True)
        original=workflow.os.rename
        attempts=[]
        def competing(src,dst):
            if Path(dst)==final:
                attempts.append(True)
                if len(attempts)==1:
                    final.write_bytes(b'other process output')
                    error=PermissionError('temporary sharing violation');error.winerror=32
                    raise error
            return original(src,dst)
        with patch.object(workflow.os,'rename',side_effect=competing),patch.object(self.stop,'wait',return_value=False):
            with self.assertRaises(FileExistsError):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertEqual(attempts,[True,True])
        self.assertEqual(final.read_bytes(),b'other process output')
        self.assertTrue((self.campaign/'导出'/'草稿视频'/'result.partial.mp4').is_file())
        self.assertFalse(list(self.root.glob('*.tmp')))

    @unittest.skipUnless(os.name=='nt','Windows rename sharing conflicts')
    def test_cancelling_publish_backoff_cleans_temp_and_keeps_completed_encode(self):
        final=video_output_path(self.source,'zh-CN',draft=True)
        error=PermissionError('temporary sharing violation');error.winerror=32
        def stop_wait(delay):self.stop.set();return True
        with patch.object(workflow.os,'rename',side_effect=error),patch.object(self.stop,'wait',side_effect=stop_wait):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertFalse(final.exists())
        self.assertFalse(list(self.root.glob('*.tmp')))
        self.assertTrue((self.campaign/'导出'/'草稿视频'/'result.partial.mp4').is_file())

    @unittest.skipUnless(os.name=='nt','Windows cleanup sharing conflicts')
    def test_cleanup_failure_preserves_primary_error_and_reports_owned_temp(self):
        primary=OSError('original copy failure')
        original=Path.unlink
        cleanup_paths=[]
        other=self.root/'other.tmp';other.write_bytes(b'keep unrelated temporary file')
        def locked(path,*args,**kwargs):
            if path.parent==self.root and path.suffix=='.tmp' and path!=other:
                cleanup_paths.append(path)
                error=PermissionError('temporary cleanup sharing violation');error.winerror=32
                raise error
            return original(path,*args,**kwargs)
        with patch.object(workflow,'cancellable_copy',side_effect=primary),patch.object(Path,'unlink',locked), \
                patch.object(self.stop,'wait',return_value=False),patch.object(workflow,'emit') as emit:
            with self.assertRaises(OSError) as raised:
                workflow.export_video(self.campaign,self.stop,draft=True)
        self.assertIs(raised.exception,primary)
        self.assertEqual(len(cleanup_paths),6)
        self.assertEqual(len(set(cleanup_paths)),1)
        self.assertTrue(cleanup_paths[0].is_file())
        self.assertTrue(any(call.kwargs.get('cleanup_pending')==str(cleanup_paths[0])
                            for call in emit.call_args_list))
        self.assertTrue(any(str(cleanup_paths[0]) in note for note in raised.exception.__notes__))
        self.assertEqual(other.read_bytes(),b'keep unrelated temporary file')

    @unittest.skipUnless(os.name=='nt','Windows cleanup sharing conflicts')
    def test_cleanup_or_warning_failure_never_masks_cancelled_or_publish_error(self):
        original=Path.unlink
        for kind in ('cancelled','publish'):
            with self.subTest(kind=kind):
                final=self.root/f'{kind}.mp4'
                primary=workflow.r.Cancelled('original cancellation') if kind=='cancelled' else OSError('original rename failure')
                cleanup_paths=[]
                def locked(path,*args,**kwargs):
                    if path.parent==self.root and path.name.startswith('.'+final.name+'.'):
                        cleanup_paths.append(path)
                        error=PermissionError('cleanup sharing violation');error.winerror=32
                        raise error
                    return original(path,*args,**kwargs)
                def cancelled_copy(*args):
                    self.stop.set()
                    raise primary
                self.stop.clear()
                with ExitStack() as stack:
                    stack.enter_context(patch.object(Path,'unlink',locked))
                    stack.enter_context(patch.object(workflow,'emit',side_effect=BrokenPipeError('warning unavailable')))
                    if kind=='cancelled':
                        stack.enter_context(patch.object(workflow,'cancellable_copy',side_effect=cancelled_copy))
                    else:
                        stack.enter_context(patch.object(workflow.os,'rename',side_effect=primary))
                        stack.enter_context(patch.object(self.stop,'wait',return_value=False))
                    with self.assertRaises(type(primary)) as raised:
                        workflow._publish_video(self.source,final,self.stop,expected_hash=workflow.sha256(self.source))
                self.assertIs(raised.exception,primary)
                self.assertEqual(len(cleanup_paths),1 if kind=='cancelled' else 6)
                self.assertFalse(final.exists())
                self.assertTrue(cleanup_paths[0].is_file())
                self.assertTrue(any(str(cleanup_paths[0]) in note for note in primary.__notes__))
                self.assertEqual(self.source.read_bytes(),b'synthetic source video')


if __name__ == '__main__': unittest.main()
