"""Encoding policy is bound to evidence; changing it never reuses another encode."""
from pathlib import Path
import unittest
from unittest.mock import patch

from subtitle_pipeline import cloud_workflow as workflow, media_export
from subtitle_pipeline.languages import video_output_path
from tests import test_draft_video as fixture


class MediaExportEncoderTests(unittest.TestCase):
    write = fixture.DraftVideoTests.write
    fake_encode = fixture.DraftVideoTests.fake_encode

    def setUp(self):
        fixture.DraftVideoTests.setUp(self)

    def export(self, encoder):
        try:
            workflow.export_video(self.campaign, self.stop, draft=True, encoder=encoder)
        except TypeError as error:
            self.fail('The export encoder policy is not implemented: ' + str(error))

    def video_commands(self):
        return [call.args[0] for call in self.encoder.call_args_list
                if str(call.args[0][-1]).endswith('.mp4')]

    def completed_qsv(self, stage):
        if stage == 'published':
            self.export('qsv')
        elif stage == 'publication':
            with patch.object(media_export, '_publish_video', side_effect=OSError('owned copy failure')):
                with self.assertRaises(OSError):
                    self.export('qsv')
        elif stage == 'encoded':
            with patch.object(media_export, 'audio_digest', side_effect=OSError('owned media read failure')):
                with self.assertRaises(OSError):
                    self.export('qsv')
        else:
            fixture.DraftVideoTests.cancel_post_encode_source_hash(self)
        self.encoder.reset_mock()
        return self.campaign / '导出' / '草稿视频'

    def assert_qsv_recovery_without_hardware(self, stage, requested):
        folder = self.completed_qsv(stage)
        partial = folder / 'result.partial.mp4'
        encoded = partial.read_bytes()
        final = video_output_path(self.source, 'zh-CN', draft=True)
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('QSV driver no longer available')), \
             patch.object(media_export, 'cancellable_sha256', wraps=workflow.cancellable_sha256) as hashes:
            self.export(requested)
        self.assertEqual(self.video_commands(), [])
        self.assertEqual(final.read_bytes(), encoded)
        self.assertEqual(workflow.read_json(self.campaign / 'campaign.json')['draft_output_binding']['encoder'], 'qsv')
        if stage != 'published':
            paths = [Path(call.args[0]) for call in hashes.call_args_list]
            self.assertIn(self.source, paths)
            self.assertIn(partial, paths)
        if stage in ('encoded', 'pending'):
            self.assertEqual(sum('null' in call.args[0] for call in self.encoder.call_args_list), 3)
        else:
            self.assertEqual(sum('null' in call.args[0] for call in self.encoder.call_args_list), 0)

    def test_published_qsv_auto_recovery_does_not_require_working_hardware(self):
        self.assert_qsv_recovery_without_hardware('published', 'auto')

    def test_published_qsv_explicit_recovery_does_not_require_working_hardware(self):
        self.assert_qsv_recovery_without_hardware('published', 'qsv')

    def test_encoded_qsv_auto_recovery_still_validates_media_without_working_hardware(self):
        self.assert_qsv_recovery_without_hardware('encoded', 'auto')

    def test_encoded_qsv_explicit_recovery_still_validates_media_without_working_hardware(self):
        self.assert_qsv_recovery_without_hardware('encoded', 'qsv')

    def test_publication_qsv_auto_recovery_does_not_require_working_hardware(self):
        self.assert_qsv_recovery_without_hardware('publication', 'auto')

    def test_publication_qsv_explicit_recovery_does_not_require_working_hardware(self):
        self.assert_qsv_recovery_without_hardware('publication', 'qsv')

    def test_pending_qsv_auto_recovery_still_validates_source_and_media_without_hardware(self):
        self.assert_qsv_recovery_without_hardware('pending', 'auto')

    def test_pending_qsv_explicit_recovery_still_validates_source_and_media_without_hardware(self):
        self.assert_qsv_recovery_without_hardware('pending', 'qsv')

    def test_publication_proof_recovers_unrecorded_final_without_working_hardware(self):
        self.completed_qsv('published')
        manifest = workflow.read_json(self.campaign / 'campaign.json')
        for key in ('draft_output', 'draft_output_binding', 'draft_output_encoder', 'draft_output_sha256'):
            manifest.pop(key)
        self.write(self.campaign / 'campaign.json', manifest)
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('QSV driver no longer available')):
            self.export('auto')
        saved = workflow.read_json(self.campaign / 'campaign.json')
        self.assertEqual(saved['draft_output_encoder'], 'qsv')
        self.assertEqual(Path(saved['draft_output']).read_bytes(), b'synthetic encoded video')
        self.assertEqual(self.video_commands(), [])

    def test_auto_recovers_new_qsv_publication_when_manifest_still_names_old_cpu_bytes(self):
        self.export('cpu')
        final = video_output_path(self.source, 'zh-CN', draft=True)
        old = final.with_name('owned-old-cpu.mp4')
        final.rename(old)
        def new_qsv(args, log, stop, **kwargs):
            self.fake_encode(args, log, stop, **kwargs)
            if str(args[-1]).endswith('.mp4'):
                Path(args[-1]).write_bytes(b'new owned qsv encoded video')
        with patch.object(workflow.r, 'run_process', side_effect=new_qsv), \
             patch.object(workflow, 'write_manifest', side_effect=OSError('owned manifest save failure')):
            with self.assertRaises(OSError):
                self.export('qsv')
        self.assertEqual(final.read_bytes(), b'new owned qsv encoded video')
        self.assertEqual(workflow.read_json(self.campaign / 'campaign.json')['draft_output_encoder'], 'cpu')
        self.encoder.reset_mock()
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('QSV driver no longer available')):
            self.export('auto')
        saved = workflow.read_json(self.campaign / 'campaign.json')
        self.assertEqual(saved['draft_output_encoder'], 'qsv')
        self.assertEqual(saved['draft_output_sha256'], workflow.sha256(final))
        self.assertEqual(final.read_bytes(), b'new owned qsv encoded video')
        self.assertEqual(old.read_bytes(), b'synthetic encoded video')
        self.assertEqual(self.video_commands(), [])

    def test_auto_keeps_a_completed_cpu_encode_when_qsv_becomes_available(self):
        with patch.object(media_export, '_publish_video', side_effect=OSError('owned copy failure')):
            with self.assertRaises(OSError):
                self.export('cpu')
        self.encoder.reset_mock()
        with patch.object(media_export.environment, 'select_encoder', return_value='qsv'):
            self.export('auto')
        self.assertEqual(workflow.read_json(self.campaign / 'campaign.json')['draft_output_encoder'], 'cpu')
        self.assertEqual(self.video_commands(), [])

    def test_explicit_cpu_cannot_claim_a_published_qsv_final_even_without_working_qsv(self):
        self.completed_qsv('published')
        final = video_output_path(self.source, 'zh-CN', draft=True)
        original = final.read_bytes()
        with patch.object(media_export.environment, 'select_encoder', return_value='cpu'):
            with self.assertRaisesRegex(ValueError, '已存在'):
                self.export('cpu')
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual(workflow.read_json(self.campaign / 'campaign.json')['draft_output_encoder'], 'qsv')
        self.assertEqual(self.video_commands(), [])

    def test_corrupt_qsv_partial_requires_fresh_encoder_selection(self):
        folder = self.completed_qsv('publication')
        partial = folder / 'result.partial.mp4'
        partial.write_bytes(b'corrupt owned partial')
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('no fresh encoder available')):
            with self.assertRaisesRegex(ValueError, 'no fresh encoder'):
                self.export('auto')
        self.assertEqual(partial.read_bytes(), b'corrupt owned partial')
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertEqual(self.video_commands(), [])

    def test_corrupt_qsv_partial_auto_fallback_encodes_cpu_and_keeps_prior_partial(self):
        folder = self.completed_qsv('publication')
        (folder / 'result.partial.mp4').write_bytes(b'corrupt owned partial')
        with patch.object(media_export.environment, 'select_encoder', return_value='cpu'):
            self.export('auto')
        self.assertEqual(len(self.video_commands()), 1)
        self.assertIn('libx264', self.video_commands()[0])
        self.assertEqual(workflow.read_json(self.campaign / 'campaign.json')['draft_output_encoder'], 'cpu')
        self.assertEqual([path.read_bytes() for path in (folder / '保留的编码').glob('*.mp4')],
                         [b'corrupt owned partial'])

    def test_changed_subtitles_require_fresh_selection_instead_of_qsv_recovery(self):
        folder = self.completed_qsv('publication')
        original = (folder / 'result.partial.mp4').read_bytes()
        fixture.DraftVideoTests.edit(self, target_text='changed owned subtitle')
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('no fresh encoder available')):
            with self.assertRaisesRegex(ValueError, 'no fresh encoder'):
                self.export('auto')
        self.assertEqual((folder / 'result.partial.mp4').read_bytes(), original)
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertEqual(self.video_commands(), [])

    def test_changed_source_cannot_recover_qsv_without_working_hardware(self):
        folder = self.completed_qsv('publication')
        original = (folder / 'result.partial.mp4').read_bytes()
        self.source.write_bytes(b'changed owned source')
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('no fresh encoder available')):
            with self.assertRaisesRegex(ValueError, '原.*视频|素材'):
                self.export('auto')
        self.assertEqual((folder / 'result.partial.mp4').read_bytes(), original)
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertEqual(self.video_commands(), [])

    def test_cpu_export_uses_libx264_and_records_actual_encoder(self):
        self.export('cpu')
        command = self.video_commands()[0]
        self.assertIn('libx264', command)
        self.assertNotIn('h264_qsv', command)
        self.assertIn('subtitles=captions.ass,format=yuv420p', command)
        manifest = workflow.read_json(self.campaign / 'campaign.json')
        self.assertEqual(manifest['draft_output_encoder'], 'cpu')
        self.assertEqual(manifest['draft_output_binding']['encoder'], 'cpu')
        identity = workflow.read_json(self.campaign / '导出' / '草稿视频' / 'encoding-checkpoint.json')['identity']
        self.assertEqual(identity['encoder'], 'cpu')
        self.assertEqual(identity['output_binding']['encoder'], 'cpu')

    def test_switch_from_qsv_to_cpu_archives_previous_partial_and_encodes_once(self):
        from subtitle_pipeline import media_export
        with patch.object(media_export, '_publish_video', side_effect=OSError('owned copy failure')):
            with self.assertRaises(OSError):
                self.export('qsv')
        folder = self.campaign / '导出' / '草稿视频'
        original = (folder / 'result.partial.mp4').read_bytes()
        self.encoder.reset_mock()
        self.export('cpu')
        self.assertEqual(len(self.video_commands()), 1)
        archives = list((folder / '保留的编码').glob('*.mp4'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), original)
        self.assertIn('libx264', self.video_commands()[0])

    def test_failed_cpu_encode_is_not_retried_with_qsv_or_published(self):
        def failing(args, log, stop, **kwargs):
            if str(args[-1]).endswith('.mp4'):
                raise RuntimeError('owned encoder failure')
            return self.fake_encode(args, log, stop, **kwargs)
        with patch.object(workflow.r, 'run_process', side_effect=failing) as encoding:
            with self.assertRaisesRegex(RuntimeError, 'owned encoder failure'):
                self.export('cpu')
        commands = [call.args[0] for call in encoding.call_args_list
                    if str(call.args[0][-1]).endswith('.mp4')]
        self.assertEqual(len(commands), 1)
        self.assertIn('libx264', commands[0])
        self.assertFalse(video_output_path(self.source, 'zh-CN', draft=True).exists())
        self.assertNotIn('draft_output', workflow.read_json(self.campaign / 'campaign.json'))

    def test_legacy_qsv_checkpoint_resumes_without_reencoding(self):
        from subtitle_pipeline import media_export
        with patch.object(media_export, '_publish_video', side_effect=OSError('owned copy failure')):
            with self.assertRaises(OSError):
                self.export('qsv')
        folder = self.campaign / '导出' / '草稿视频'
        encoded = workflow.read_json(folder / 'encoding-checkpoint.json')
        self.assertEqual(encoded['identity'].get('encoder'), 'qsv')
        encoded['identity'].pop('encoder')
        encoded['identity']['output_binding'].pop('encoder')
        self.write(folder / 'encoding-checkpoint.json', encoded)
        publication = workflow.read_json(folder / 'publication-checkpoint.json')
        publication['output_binding'].pop('encoder')
        publication['verification']['output_binding'].pop('encoder')
        publication['verification'].pop('encoder')
        self.write(folder / 'publication-checkpoint.json', publication)
        self.encoder.reset_mock()
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('QSV driver no longer available')):
            self.export('qsv')
        self.assertEqual(self.video_commands(), [])
        saved = workflow.read_json(self.campaign / 'campaign.json')
        self.assertEqual(saved['draft_output_binding']['encoder'], 'qsv')

    def test_legacy_pending_qsv_checkpoint_still_requires_source_and_media_checks(self):
        checkpoint = fixture.DraftVideoTests.cancel_post_encode_source_hash(self)
        encoded = workflow.read_json(checkpoint)
        self.assertEqual(encoded['status'], 'pending_source_validation')
        encoded['identity'].pop('encoder')
        encoded['identity']['output_binding'].pop('encoder')
        self.write(checkpoint, encoded)
        self.encoder.reset_mock()
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('QSV driver no longer available')):
            self.export('qsv')
        self.assertEqual(self.video_commands(), [])
        self.assertEqual(sum(str(call.args[0][-1]) == '-' for call in self.encoder.call_args_list), 3)
        self.assertEqual(workflow.read_json(checkpoint)['status'], 'encoded')

    def test_a_legacy_published_qsv_final_remains_reusable_without_touching_the_video(self):
        self.export('qsv')
        saved = workflow.read_json(self.campaign / 'campaign.json')
        final = Path(saved['draft_output'])
        original = final.read_bytes()
        saved['draft_output_binding'].pop('encoder')
        saved.pop('draft_output_encoder')
        self.write(self.campaign / 'campaign.json', saved)
        folder = self.campaign / '导出' / '草稿视频'
        for name in ('publication-checkpoint.json', 'verification.json'):
            record = workflow.read_json(folder / name)
            record['output_binding'].pop('encoder')
            if 'verification' in record:
                record['verification']['output_binding'].pop('encoder')
                record['verification'].pop('encoder')
            else:
                record.pop('encoder')
            self.write(folder / name, record)
        self.encoder.reset_mock()
        with patch.object(media_export.environment, 'select_encoder',
                          side_effect=ValueError('QSV driver no longer available')):
            self.export('qsv')
        self.assertEqual(self.video_commands(), [])
        self.assertEqual(final.read_bytes(), original)

    def test_invalid_encoder_blocks_before_any_output_write(self):
        before = {path: path.read_bytes() for path in self.campaign.rglob('*') if path.is_file()}
        with self.assertRaises(ValueError):
            self.export('nvenc')
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse((self.campaign / '导出').exists())

    def test_cli_forwards_encoder_choice_and_auto_default(self):
        for action, options, expected in (
                ('export', ['--encoder', 'cpu'], {'encoder': 'cpu'}),
                ('export-draft', ['--encoder', 'qsv'], {'draft': True, 'encoder': 'qsv'}),
                ('export', [], {'encoder': 'auto'})):
            with self.subTest(action=action, options=options), patch.object(workflow, 'export_video') as export:
                code = workflow.main([action, '--campaign', str(self.campaign), *options])
            self.assertEqual(code, 0)
            self.assertEqual(export.call_args.kwargs, expected)
            self.assertEqual(export.call_args.args[0], self.campaign.resolve())
