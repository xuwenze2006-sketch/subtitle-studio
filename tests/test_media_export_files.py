"""Export staging and recovery protect real fixture-owned files and NTFS links."""
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import cloud_workflow as workflow, media_export
from subtitle_pipeline.languages import video_output_path
from tests import test_draft_video as fixture
from tests.test_export_source_reads import observed_reads
from tests.test_cloud_workflow import write_complete_qwen_evidence


@contextmanager
def after_file_read(path, action):
    """Run an owned mutation after the real hash reader closes at EOF."""
    original = Path.open
    finished = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
            self.eof = False
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            result = self.stream.__exit__(*args)
            if self.eof and not finished:
                finished.append(True)
                action()
            return result
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size=-1):
            value = self.stream.read(size)
            self.eof |= not value
            return value

    def open_file(candidate, *args, **kwargs):
        stream = original(candidate, *args, **kwargs)
        return Reader(stream) if candidate == path and args and args[0] == 'rb' else stream

    with patch.object(Path, 'open', new=open_file):
        yield finished


class MediaExportFileTests(unittest.TestCase):
    write = fixture.DraftVideoTests.write
    fake_encode = fixture.DraftVideoTests.fake_encode

    def setUp(self):
        fixture.DraftVideoTests.setUp(self)
        self.write(self.campaign / 'final-review.json', {
            'status': 'sampled_approved', 'language': 'ja', 'target': 'zh-CN',
            'source_sha256': self.manifest['source']['sha256'],
            'artifacts': workflow.artifacts_for(self.campaign, self.manifest, full=True),
        })

    def export_folder(self, draft):
        folder = self.campaign / '导出'
        if draft:
            folder /= '草稿视频'
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def assert_no_success(self):
        self.assertFalse(any(call.kwargs.get('status') in ('draft_exported', 'exported')
                             or call.kwargs.get('export_progress', {}).get('phase') == 'done'
                             for call in workflow.emit.call_args_list))

    def test_srt_hardlink_to_source_is_replaced_without_truncating_source(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                stage = self.export_folder(draft) / 'captions.srt'
                os.link(self.source, stage)
                original = self.source.read_bytes()
                try:
                    with patch.object(workflow.r, 'run_process', side_effect=RuntimeError('stop before conversion')):
                        with self.assertRaisesRegex(RuntimeError, 'stop before conversion'):
                            workflow.export_video(self.campaign, self.stop, draft=draft)
                    self.assertEqual(self.source.read_bytes(), original)
                    self.assertFalse(os.path.samefile(self.source, stage))
                    self.assertEqual(stage.read_bytes(), (self.folder / '中文草稿.srt').read_bytes())
                finally:
                    self.source.write_bytes(original)

    def assert_ass_alias_preserved(self, link):
        other = self.root / 'other-owned-file.txt'
        other.write_bytes(b'preserve unrelated owned bytes')
        for draft in (True, False):
            stage = self.export_folder(draft) / 'captions.ass'
            for victim in (self.source, self.folder / '中文草稿.srt', other):
                with self.subTest(draft=draft, victim=victim.name):
                    stage.unlink(missing_ok=True)
                    link(victim, stage)
                    original = victim.read_bytes()
                    def convert_then_stop(args, log, stop, **kwargs):
                        self.fake_encode(args, log, stop, **kwargs)
                        raise RuntimeError('stop after conversion output')
                    try:
                        with patch.object(workflow.r, 'run_process', side_effect=convert_then_stop):
                            with self.assertRaisesRegex(RuntimeError, 'stop after conversion output'):
                                workflow.export_video(self.campaign, self.stop, draft=draft)
                        self.assertEqual(victim.read_bytes(), original)
                        self.assertEqual(stage.read_bytes(), original)
                    finally:
                        victim.write_bytes(original)

    def test_ass_hardlinks_do_not_overwrite_source_subtitles_or_other_files(self):
        self.assert_ass_alias_preserved(os.link)

    def test_ass_symlinks_do_not_overwrite_source_subtitles_or_other_files(self):
        probe = self.root / 'symlink-probe'
        try:
            probe.symlink_to(self.source)
        except OSError as error:
            self.skipTest('Symlink unavailable: ' + str(error))
        probe.unlink()
        self.assert_ass_alias_preserved(lambda victim, stage: stage.symlink_to(victim))

    def test_ass_conversion_owns_its_output_and_cancellation_cleans_only_temporaries(self):
        folder = self.export_folder(True)
        stage = folder / 'captions.ass'
        other = self.root / 'preserved-owned-ass.txt'
        other.write_bytes(b'preserve original alias target')
        os.link(other, stage)
        original_source = self.source.read_bytes()
        outputs = []

        def convert_then_cancel(args, log, stop, **kwargs):
            output = Path(args[-1])
            self.assertEqual(output.suffix, '.ass')
            self.assertTrue(output.is_file())
            self.assertFalse(os.path.samefile(output, stage))
            self.assertEqual(output.stat().st_nlink, 1)
            outputs.append(output)
            self.fake_encode(args, log, stop, **kwargs)
            stop.set()
            raise workflow.r.Cancelled('owned conversion stop')

        with patch.object(workflow.r, 'run_process', side_effect=convert_then_cancel):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertTrue(outputs)
        self.assertTrue(all(not output.exists() for output in outputs))
        self.assertEqual(list(folder.glob('.captions.*')), [])
        self.assertEqual(other.read_bytes(), b'preserve original alias target')
        self.assertEqual(stage.read_bytes(), b'preserve original alias target')
        self.assertEqual(self.source.read_bytes(), original_source)
        self.stop.clear()
        workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertTrue(video_output_path(self.source, 'zh-CN', draft=True).is_file())
        self.assertEqual(other.read_bytes(), b'preserve original alias target')
        self.assertEqual(list(folder.glob('.captions.*')), [])

    def test_directory_alias_cannot_make_staging_replace_the_source_path(self):
        for name in ('captions.srt', 'captions.ass'):
            with self.subTest(name=name):
                original_source = self.source
                physical_folder = self.root / ('alias-' + Path(name).suffix[1:])
                physical_folder.mkdir()
                renamed = physical_folder / name
                original_source.rename(renamed)
                self.source = renamed
                manifest = workflow.read_json(self.campaign / 'campaign.json')
                manifest['source']['path'] = str(renamed)
                self.write(self.campaign / 'campaign.json', manifest)
                write_complete_qwen_evidence(self.folder, renamed, 40000)
                folder = self.export_folder(True)
                folder.rmdir()
                if os.name == 'nt':
                    import _winapi
                    _winapi.CreateJunction(str(physical_folder), str(folder))
                else:
                    folder.symlink_to(physical_folder, target_is_directory=True)
                before = renamed.read_bytes()
                try:
                    with self.assertRaisesRegex(ValueError, '暂存.*冲突'):
                        workflow.export_video(self.campaign, self.stop, draft=True)
                    self.assertEqual(renamed.read_bytes(), before)
                finally:
                    if os.name == 'nt':
                        folder.rmdir()
                    else:
                        folder.unlink()
                    renamed.write_bytes(before)
                    renamed.rename(original_source)
                    self.source = original_source

    def test_existing_final_changed_during_hash_never_reports_verified_success(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                workflow.export_video(self.campaign, self.stop, draft=draft)
                final = video_output_path(self.source, 'zh-CN', draft=draft)
                original = final.read_bytes()
                before = (self.campaign / 'campaign.json').read_bytes()
                workflow.emit.reset_mock()
                with after_file_read(final, lambda: final.write_bytes(b'X' * len(original))) as changed:
                    with self.assertRaisesRegex(ValueError, '成片.*变化'):
                        workflow.export_video(self.campaign, self.stop, draft=draft)
                self.assertTrue(changed)
                self.assertEqual(final.read_bytes(), b'X' * len(original))
                self.assertEqual((self.campaign / 'campaign.json').read_bytes(), before)
                self.assert_no_success()

    @unittest.skipUnless(os.name == 'nt', 'Windows creation-time stat semantics')
    def test_equal_size_rewrite_with_restored_mtime_cannot_reuse_final(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                workflow.export_video(self.campaign, self.stop, draft=draft)
                final = video_output_path(self.source, 'zh-CN', draft=draft)
                old = final.stat()
                stat_token = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                             value.st_mtime_ns, value.st_ctime_ns)
                before = (self.campaign / 'campaign.json').read_bytes()
                def rewrite_and_restore():
                    final.write_bytes(b'Z' * old.st_size)
                    os.utime(final, ns=(old.st_atime_ns, old.st_mtime_ns))
                    # The previous five-part token really cannot detect this NTFS mutation.
                    self.assertEqual(stat_token(final.stat()), stat_token(old))
                workflow.emit.reset_mock()
                with after_file_read(final, rewrite_and_restore) as changed:
                    with self.assertRaisesRegex(ValueError, '成片.*变化'):
                        workflow.export_video(self.campaign, self.stop, draft=draft)
                self.assertTrue(changed)
                self.assertEqual(final.read_bytes(), b'Z' * old.st_size)
                self.assertEqual((self.campaign / 'campaign.json').read_bytes(), before)
                self.assert_no_success()

    @unittest.skipUnless(os.name == 'nt', 'Windows native metadata query')
    def test_native_metadata_failure_or_invalid_value_never_falls_back(self):
        import ctypes
        workflow.export_video(self.campaign, self.stop, draft=True)
        final = video_output_path(self.source, 'zh-CN', draft=True)
        original = final.read_bytes()
        before = (self.campaign / 'campaign.json').read_bytes()
        original_open = os.open
        for succeeds in (False, True):
            with self.subTest(query_succeeds=succeeds):
                descriptors = []
                def opened(path, flags, *args, **kwargs):
                    descriptor = original_open(path, flags, *args, **kwargs)
                    if Path(path) == final:
                        descriptors.append(descriptor)
                    return descriptor
                def unavailable(*args):
                    ctypes.set_last_error(5)
                    # A successful query with the zero-initialized ChangeTime is
                    # still unusable evidence. Neither case may become a fallback.
                    return int(succeeds)
                kernel = SimpleNamespace(GetFileInformationByHandleEx=Mock(side_effect=unavailable))
                workflow.emit.reset_mock()
                with patch.object(ctypes, 'WinDLL', return_value=kernel), \
                     patch.object(os, 'open', side_effect=opened):
                    with self.assertRaises(OSError):
                        workflow.export_video(self.campaign, self.stop, draft=True)
                self.assertTrue(descriptors)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.assertEqual(final.read_bytes(), original)
                self.assertEqual((self.campaign / 'campaign.json').read_bytes(), before)
                self.assert_no_success()

    def test_existing_final_replaced_during_hash_never_repairs_manifest(self):
        workflow.export_video(self.campaign, self.stop, draft=True)
        final = video_output_path(self.source, 'zh-CN', draft=True)
        old = final.stat()
        replacement = self.root / 'replacement.mp4'
        replacement.write_bytes(b'R' * old.st_size)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        manifest = workflow.read_json(self.campaign / 'campaign.json')
        for key in ('draft_output', 'draft_output_binding', 'draft_output_sha256'):
            manifest.pop(key)
        self.write(self.campaign / 'campaign.json', manifest)
        before = (self.campaign / 'campaign.json').read_bytes()
        workflow.emit.reset_mock()
        with after_file_read(final, lambda: replacement.replace(final)) as changed:
            with self.assertRaisesRegex(ValueError, '成片.*变化'):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertTrue(changed)
        self.assertEqual(final.read_bytes(), b'R' * old.st_size)
        self.assertEqual((self.campaign / 'campaign.json').read_bytes(), before)
        self.assert_no_success()

    def test_unchanged_final_recovery_performs_one_complete_final_read(self):
        workflow.export_video(self.campaign, self.stop, draft=True)
        final = video_output_path(self.source, 'zh-CN', draft=True)
        original = final.read_bytes()
        self.encoder.reset_mock()
        with observed_reads([final]) as reads:
            workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual([(record['bytes'], record['eof']) for record in reads], [(len(original), True)])
        self.assertEqual(final.read_bytes(), original)
        self.encoder.assert_not_called()

    def test_cancellation_during_final_hash_keeps_bytes_and_reports_no_success(self):
        workflow.export_video(self.campaign, self.stop, draft=True)
        final = video_output_path(self.source, 'zh-CN', draft=True)
        original = final.read_bytes()
        before = (self.campaign / 'campaign.json').read_bytes()
        workflow.emit.reset_mock()
        with after_file_read(final, self.stop.set):
            with self.assertRaises(workflow.r.Cancelled):
                workflow.export_video(self.campaign, self.stop, draft=True)
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual((self.campaign / 'campaign.json').read_bytes(), before)
        self.assert_no_success()


if __name__ == '__main__':
    unittest.main()
