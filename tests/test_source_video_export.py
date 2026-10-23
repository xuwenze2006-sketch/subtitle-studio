"""Original-only MP4s use local evidence and preserve every input revision."""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from subtitle_pipeline import manual_review, media_export, runner
from subtitle_pipeline.integrity import sha256
from subtitle_pipeline.subtitles import Cue, render_srt


_SPEC = importlib.util.find_spec('subtitle_pipeline.source_video_export')
if _SPEC is not None:
    from subtitle_pipeline import source_video_export as api
else:
    api = None


class SourceVideoExportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(api, 'The local source-only MP4 export API is missing')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'original.mp4'
        self.source.write_bytes(b'original synthetic media')
        self.project = self.root / 'project'
        self.part = self.project / '片段' / '0001'
        self.part.mkdir(parents=True)
        self.body = render_srt([Cue(100, 900, 'original one'), Cue(1200, 2000, 'second')])
        for path in (self.project / '原文.srt', self.part / 'source.local.srt'):
            path.write_text(self.body, encoding='utf-8')
        self.project.joinpath('需复核.json').write_text('[]', encoding='utf-8')
        self.state = {
            'version': 2, 'status': 'complete', 'duration_ms': 4000,
            'identity': {'source': {'path': str(self.source), 'sha256': sha256(self.source)},
                         'engine': 'whisper_cpp', 'language': 'ja', 'target': 'zh-CN'},
            'config': {'source': str(self.source), 'project': str(self.project),
                       'translate': False, 'asr_provider': 'whisper_cpp',
                       'language': 'ja', 'target': 'zh-CN'},
            'chunks': [{'index': 0, 'core_start_ms': 0, 'core_end_ms': 4000,
                        'audio_start_ms': 0, 'audio_end_ms': 4000}],
            'parts': {'0': {'asr': 'done', 'source_hash': sha256(self.part / 'source.local.srt'),
                            'asr_evidence': {'provider': 'whisper_cpp', 'language': 'ja',
                                'source_sha256': sha256(self.source), 'audio_range_ms': [0, 4000]}}},
            'generated_hashes': {'原文.srt': sha256(self.project / '原文.srt')},
            'outputs': {'原文.srt': '原文.srt'}}
        self.save_state()
        self.stop = threading.Event()
        self.events = []
        self.info = {'streams': [
            {'codec_type': 'video', 'width': 640, 'height': 360,
             'r_frame_rate': '30/1', 'nb_frames': '120', 'disposition': {'attached_pic': 0}},
            {'codec_type': 'audio', 'codec_name': 'aac', 'sample_rate': '48000',
             'channels': 2, 'channel_layout': 'stereo', 'disposition': {'default': 1, 'forced': 0}}],
             'format': {'duration': '4.0'}}
        guards = ExitStack()
        self.addCleanup(guards.close)
        guards.enter_context(patch('socket.create_connection', side_effect=AssertionError('No network')))
        guards.enter_context(patch.object(media_export.environment, 'select_encoder',
            side_effect=lambda requested, stop=None: 'cpu' if requested == 'auto' else requested))
        self.probe = guards.enter_context(patch.object(media_export, 'media_info', side_effect=lambda *a: self.info))
        guards.enter_context(patch.object(media_export, 'audio_digest', return_value='SHA256=' + 'a' * 64))
        self.process = guards.enter_context(patch.object(runner, 'run_process', side_effect=self.fake_process))

    def save_state(self):
        runner.atomic_json(self.project / 'state.json', self.state)

    def fake_process(self, args, log, stop, **kwargs):
        output = Path(args[-1])
        if output.suffix == '.ass':
            output.write_text('[Script Info]\nPlayResX: 640\nPlayResY: 360\nStyle: Default,Arial\n', encoding='utf-8')
        elif output.suffix == '.mp4':
            output.write_bytes(b'encoded:' + (output.parent / 'captions.srt').read_bytes())

    def export(self, **kwargs):
        return api.export_source_video(self.project, self.stop,
            emit=lambda message, **data: self.events.append({'message': message, **data}), **kwargs)

    def current(self):
        return api.current_source_video(self.project, self.source,
            video_matches=lambda path, digest: sha256(path) == digest)

    def record(self):
        return json.loads((self.project / '导出' / '原文视频' / 'export.json').read_text(encoding='utf-8'))

    def encodes(self):
        return sum(Path(call.args[0][-1]).suffix == '.mp4' for call in self.process.call_args_list)

    def edit(self, **changes):
        view = manual_review.load_review(self.project,
            language=self.state['config']['language'], target=self.state['config']['target'])
        cue = view['cues'][0]
        data = {key: cue[key] for key in ('start_ms', 'end_ms', 'source_text', 'target_text', 'review_status', 'note')}
        data.update(expected_revision=view['revision'], cue_id=1, translation_confirmed=False, **changes)
        with runner.ProjectLock(self.project):
            return manual_review.save_review(self.project, data,
                language=self.state['config']['language'], target=self.state['config']['target'])

    def test_complete_original_exports_snapshot_and_separate_record_without_changing_inputs(self):
        before = {path: path.read_bytes() for path in self.project.rglob('*') if path.is_file()}
        self.assertTrue(api.source_export_available(self.project, self.source))
        self.export()
        result = self.current()
        self.assertIsNotNone(result)
        self.assertTrue(result.is_relative_to(self.project / '导出' / '原文视频'))
        self.assertIn('原文', result.name)
        self.assertIn('未审核草稿', result.name)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse((self.project / 'campaign.json').exists())
        self.assertFalse((self.project / 'final-review.json').exists())
        self.assertFalse((self.project / '中文草稿.srt').exists())
        self.assertFalse((self.project / '人工校对' / '校对记录.json').exists())
        self.assertEqual(self.record()['draft_output_binding']['mode'], 'source_only')
        self.assertEqual(self.encodes(), 1)

    def test_manual_revision_is_burned_and_new_revision_preserves_prior_video(self):
        self.edit(source_text='saved correction', start_ms=150)
        self.export()
        first = self.current()
        self.assertIn(b'saved correction', first.read_bytes())
        self.edit(source_text='next correction')
        self.assertIsNone(self.current())
        self.export()
        second = self.current()
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertIn(b'next correction', second.read_bytes())

    def test_identical_export_reuses_final_without_reencoding(self):
        self.export()
        first = self.current()
        self.export()
        self.assertEqual(first, self.current())
        self.assertEqual(self.encodes(), 1)

    def test_valid_external_source_edit_and_auto_language_are_delivery_inputs(self):
        self.state['identity']['language'] = self.state['config']['language'] = 'auto'
        self.state['parts']['0']['asr_evidence']['language'] = 'auto'
        self.save_state()
        (self.project / '原文.srt').write_text(self.body.replace('original one', 'human root edit'), encoding='utf-8')
        self.assertTrue(api.source_export_available(self.project, self.source))
        self.export()
        self.assertIn(b'human root edit', self.current().read_bytes())
        self.assertEqual(self.record()['draft_output_binding']['language'], 'auto')

    def test_capability_and_current_queries_do_not_create_files_or_probe_media(self):
        before = {path: path.read_bytes() for path in self.project.rglob('*') if path.is_file()}
        self.assertTrue(api.source_export_available(self.project, self.source))
        self.assertIsNone(self.current())
        self.assertEqual({path: path.read_bytes() for path in self.project.rglob('*') if path.is_file()}, before)
        self.probe.assert_not_called()
        self.process.assert_not_called()

    def test_pending_generation_cloud_translation_and_invalid_state_are_ineligible(self):
        original = json.loads(json.dumps(self.state))
        cases = [lambda: self.state.update(status='cancelled'),
                 lambda: self.state.update(pending_outputs=None),
                 lambda: self.state['config'].update(translate=0),
                 lambda: self.state['identity'].update(engine='qwen_asr'),
                 lambda: self.state.update(merged_source_needs_review=True),
                 lambda: self.state['config'].update(source=str(self.root / 'different.mp4'))]
        for change in cases:
            with self.subTest(change=change):
                self.state = json.loads(json.dumps(original)); change(); self.save_state()
                self.assertFalse(api.source_export_available(self.project, self.source))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.export()
        self.process.assert_not_called()

    def test_any_translated_public_track_blocks_source_mode(self):
        (self.project / '英文草稿.srt').write_text(self.body, encoding='utf-8')
        self.assertFalse(api.source_export_available(self.project, self.source))
        with self.assertRaises(ValueError): self.export()
        self.process.assert_not_called()

    def test_corrupt_part_and_chunk_hole_block_before_media_or_encoding(self):
        (self.part / 'source.local.srt').write_text(self.body + 'damage', encoding='utf-8')
        with self.assertRaises(ValueError): self.export()
        self.probe.assert_not_called()
        self.process.assert_not_called()
        (self.part / 'source.local.srt').write_text(self.body, encoding='utf-8')
        self.state['chunks'][0]['core_end_ms'] = 3999; self.save_state()
        with self.assertRaises(ValueError): self.export()

    def test_fresh_source_hash_rejects_changed_bytes_with_restored_mtime(self):
        self.export()
        old = self.source.stat()
        self.source.write_bytes(b'x' * old.st_size)
        os.utime(self.source, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(self.encodes(), 1)

    def test_deleted_empty_patch_record_never_downgrades_even_if_top_receipt_is_lost(self):
        self.edit()
        self.export()
        (self.project / '人工校对' / '校对记录.json').unlink()
        (self.project / '导出' / '原文视频' / 'export.json').unlink()
        self.assertIsNone(self.current())
        self.assertFalse(api.source_export_available(self.project, self.source))
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(self.encodes(), 1)

    def test_publication_failure_reuses_completed_partial(self):
        with patch.object(media_export, '_publish_video', side_effect=OSError('synthetic full destination')):
            with self.assertRaises(OSError): self.export()
        self.assertIsNone(self.current())
        self.export()
        self.assertIsNotNone(self.current())
        self.assertEqual(self.encodes(), 1)

    def test_top_pointer_failure_recovers_from_same_key_without_reencoding(self):
        original = runner.atomic_json
        target = self.project / '导出' / '原文视频' / 'export.json'
        def write(path, value):
            if Path(path) == target: raise OSError('synthetic pointer failure')
            return original(path, value)
        with patch.object(runner, 'atomic_json', side_effect=write):
            with self.assertRaises(OSError): self.export()
        self.assertIsNone(self.current())
        self.export()
        self.assertIsNotNone(self.current())
        self.assertEqual(self.encodes(), 1)

    def test_cancelled_before_admission_never_probes_or_creates_exports(self):
        self.stop.set()
        with self.assertRaises(runner.Cancelled): self.export()
        self.assertFalse((self.project / '导出').exists())
        self.probe.assert_not_called()

    def test_audio_only_media_is_rejected_without_encoding(self):
        self.info['streams'] = self.info['streams'][1:]
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(self.encodes(), 0)

    def test_project_lock_prevents_concurrent_generation(self):
        with runner.ProjectLock(self.project):
            with self.assertRaises(RuntimeError): self.export()
        self.probe.assert_not_called()

    def test_cli_honors_existing_studio_stop_marker(self):
        marker = self.root / 'stopped'; marker.write_text('stop', encoding='utf-8')
        with patch.dict(os.environ, {'SUBTITLE_STUDIO_STOP_FILE': str(marker)}), patch.object(api, '_emit'):
            self.assertEqual(api.main(['--project', str(self.project), '--encoder', 'cpu']), 2)
        self.probe.assert_not_called()

    def test_snapshot_corruption_removes_current_entry_without_repairing_it(self):
        self.export()
        snapshot = next((self.project / '人工校对' / '版本').glob('*/原文.srt'))
        snapshot.write_text('corrupt immutable snapshot', encoding='utf-8')
        self.assertIsNone(self.current())
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(snapshot.read_text(encoding='utf-8'), 'corrupt immutable snapshot')

    def test_invalid_top_record_or_media_proof_is_never_exposed(self):
        self.export()
        top = self.project / '导出' / '原文视频' / 'export.json'
        saved = top.read_bytes()
        record = json.loads(saved)
        record['version'] = True
        runner.atomic_json(top, record)
        self.assertIsNone(self.current())
        top.write_bytes(saved)
        publication = next((top.parent).glob('*/publication-checkpoint.json'))
        publication.write_text('{}', encoding='utf-8')
        self.assertIsNone(self.current())

    def test_source_changes_before_publication_leave_partial_and_never_publish(self):
        original = self.fake_process
        def encode(args, log, stop, **kwargs):
            original(args, log, stop, **kwargs)
            if Path(args[-1]).suffix == '.mp4':
                (self.project / '原文.srt').write_text(self.body.replace('original one', 'changed while rendering'), encoding='utf-8')
        self.process.side_effect = encode
        with self.assertRaises(ValueError): self.export()
        self.assertFalse(list((self.project / '导出' / '原文视频').glob('*.mp4')))
        self.assertTrue(list((self.project / '导出' / '原文视频').glob('*/result.partial.mp4')))
        self.assertFalse(any(event.get('status') == 'draft_exported' for event in self.events))

    def test_reverting_exact_overlay_reuses_its_prior_key(self):
        self.edit(source_text='first correction')
        record = self.project / '人工校对' / '校对记录.json'
        first_record = record.read_bytes()
        self.export(); first = self.current()
        self.edit(source_text='second correction')
        self.export(); second = self.current()
        record.write_bytes(first_record)
        self.export()
        self.assertEqual(self.current(), first)
        self.assertTrue(second.is_file())
        self.assertEqual(self.encodes(), 2)

    def test_multiple_audio_tracks_use_ordered_complete_payload_proof(self):
        self.info['streams'].append({**self.info['streams'][1], 'tags': {'language': 'jpn'},
                                     'disposition': {'default': 0, 'forced': 0}})
        with patch.object(media_export, 'audio_digests', return_value=['SHA256=' + 'a' * 64, 'SHA256=' + 'b' * 64]):
            self.export()
            self.assertIsNotNone(self.current())
            self.export()
        args = next(call.args[0] for call in self.process.call_args_list if Path(call.args[0][-1]).suffix == '.mp4')
        self.assertIn('0:a', args)
        self.assertNotIn('0:a:0', args)
        self.assertEqual(self.record()['draft_output_binding']['audio_policy'], 'all_tracks_v1')
        self.assertEqual(self.encodes(), 1)

    def test_keyed_foreign_final_is_preserved_without_claiming_or_overwriting(self):
        with patch.object(media_export, '_publish_video', side_effect=OSError('failed copy')):
            with self.assertRaises(OSError): self.export()
        checkpoint = next((self.project / '导出' / '原文视频').glob('*/publication-checkpoint.json'))
        key = checkpoint.parent.name
        final = checkpoint.parent.parent / ('original_原文_' + key + '_未审核草稿.mp4')
        final.write_bytes(b'unknown existing file')
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(final.read_bytes(), b'unknown existing file')
        self.assertIsNone(self.current())

    def test_per_key_record_loss_repairs_from_publication_without_reencoding(self):
        self.export()
        base = self.project / '导出' / '原文视频'
        next(base.glob('*/export.json')).unlink()
        (base / 'export.json').unlink()
        self.export()
        self.assertIsNotNone(self.current())
        self.assertEqual(self.encodes(), 1)

    def test_encoder_requests_keep_separate_identity_even_when_both_select_cpu(self):
        self.export(encoder='auto'); first = self.current()
        self.export(encoder='cpu'); second = self.current()
        self.assertNotEqual(first, second)
        self.export(encoder='auto')
        self.assertEqual(self.current(), first)
        self.assertEqual(self.encodes(), 2)

    def test_path_links_are_rejected_and_external_destination_remains_untouched(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.project / '导出').mkdir()
        link = self.project / '导出' / '原文视频'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name != 'nt':
                raise
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(created.returncode, 0, created.stderr.decode(errors='replace'))
            self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        self.assertFalse(api.source_export_available(self.project, self.source))
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(list(outside.iterdir()), [])

    def test_decode_log_alias_cannot_truncate_external_owned_file(self):
        with patch.object(media_export, '_publish_video', side_effect=OSError('failed copy')):
            with self.assertRaises(OSError): self.export()
        folder = next((self.project / '导出' / '原文视频').glob('*/publication-checkpoint.json')).parent
        victim = self.root / 'external-log.txt'; victim.write_text('preserve this', encoding='utf-8')
        alias = folder / 'decode-0.log'
        os.link(victim, alias)
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(victim.read_text(encoding='utf-8'), 'preserve this')

    def test_empty_pending_journal_cannot_be_interpreted_as_finished(self):
        self.state['merged_source_edit'] = {}; self.save_state()
        self.assertFalse(api.source_export_available(self.project, self.source))
        with self.assertRaises(ValueError): self.export()

    def test_source_duration_mismatch_is_rejected_before_encoding(self):
        self.info['format']['duration'] = '8.0'
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(self.encodes(), 0)

    def test_corrupt_keyed_receipt_cannot_report_success_with_an_unusable_current_video(self):
        self.export()
        receipt = next((self.project / '导出' / '原文视频').glob('*/export.json'))
        record = json.loads(receipt.read_bytes())
        record['source']['path'] = str(self.root / 'different.mp4')
        runner.atomic_json(receipt, record)
        before = receipt.read_bytes()
        self.events.clear()
        with self.assertRaises(ValueError): self.export()
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse(any(event.get('status') == 'draft_exported' for event in self.events))

    def test_surviving_top_manual_evidence_blocks_downgrade_after_marker_loss(self):
        self.edit(); self.export()
        (self.project / '人工校对' / '校对记录.json').unlink()
        (self.project / '导出' / '原文视频' / 'ownership.json').unlink()
        self.assertFalse(api.source_export_available(self.project, self.source))
        with self.assertRaises(ValueError): self.export()

    def test_state_change_during_encoding_is_never_published(self):
        original = self.fake_process
        def encode(args, log, stop, **kwargs):
            original(args, log, stop, **kwargs)
            if Path(args[-1]).suffix == '.mp4':
                self.state['updated'] = 12345; self.save_state()
        self.process.side_effect = encode
        with self.assertRaises(ValueError): self.export()
        self.assertFalse(list((self.project / '导出' / '原文视频').glob('*.mp4')))

    def test_cancel_during_media_validation_keeps_encode_and_never_publishes(self):
        def digest(*args, **kwargs):
            self.stop.set(); return 'SHA256=' + 'a' * 64
        with patch.object(media_export, 'audio_digest', side_effect=digest):
            with self.assertRaises(runner.Cancelled): self.export()
        self.assertFalse(list((self.project / '导出' / '原文视频').glob('*.mp4')))
        self.assertTrue(list((self.project / '导出' / '原文视频').glob('*/result.partial.mp4')))
        self.assertFalse(any(event.get('status') == 'draft_exported' for event in self.events))

    def test_source_stable_read_guard_remains_active_during_encoder(self):
        if os.name != 'nt':
            self.skipTest('Deny-write handles are a Windows guarantee')
        original = self.fake_process
        attempts = []
        def encode(args, log, stop, **kwargs):
            if Path(args[-1]).suffix == '.mp4':
                with self.assertRaises(PermissionError):
                    self.source.write_bytes(b'forbidden source replacement')
                attempts.append(True)
            original(args, log, stop, **kwargs)
        self.process.side_effect = encode
        self.export()
        self.assertEqual(attempts, [True])
        self.assertIsNotNone(self.current())


if __name__ == '__main__':
    unittest.main()
