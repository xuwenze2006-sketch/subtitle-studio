"""Versioned delivery files never mutate processing files or copy the source."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import file_layout as layout


SRT = '1\r\n00:00:01,000 --> 00:00:02,000\r\n字幕\r\n\r\n'.encode('utf-8')


class FileLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '素材.mp4'
        self.source.write_bytes(b'never copy this media')
        self.project = self.root / 'project'
        self.project.mkdir()
        self.folder = self.project / '样片' / '01' / '识别任务'
        self.folder.mkdir(parents=True)
        self.track = self.folder / '原文.srt'
        self.track.write_bytes(SRT)
        self.state = self.folder / 'state.json'
        self.state.write_text(json.dumps({'status': 'complete', 'review_status': 'unreviewed'}))
        self.selection = {'id': 'sample-1', 'name': '样片 1', 'offset_ms': 30000,
                          'folder': self.folder, 'tracks': ['原文.srt']}

    def save(self):
        return layout.save_subtitle_snapshot(self.project, self.source, self.selection,
                                             created_at='2026-10-03T09:00:00+08:00')

    def test_new_project_is_dated_deterministic_for_same_time_and_not_created(self):
        now = datetime(2026, 10, 3, 9, 5, 4)
        path = layout.timestamped_project(self.source, self.root / 'jobs', now)
        self.assertEqual(path.parent, self.root / 'jobs' / '2026-10-03')
        self.assertRegex(path.name, r'^090504_素材-[0-9a-f]{10}$')
        self.assertFalse(path.exists())
        self.assertNotEqual(path, layout.timestamped_project(self.root / 'other' / self.source.name,
                                                            self.root / 'jobs', now))

    def test_filename_preserves_chinese_but_removes_path_and_windows_reserved_names(self):
        stamp = datetime(2026, 10, 3, 9, 5, 4).timestamp()
        self.assertEqual(layout.subtitle_filename(self.source, '样片 1', '原文.srt', stamp),
                         '素材_样片 1_原文草稿_20261003_090504.srt')
        name = layout.subtitle_filename(Path('CON.mp4'), '../A:B\\C? .', '中文草稿.srt', stamp)
        self.assertNotRegex(name, r'[<>:"/\\|?*\x00-\x1f]')
        self.assertFalse(name.upper().startswith('CON.'))
        self.assertLess(len(layout.subtitle_filename(Path('长' * 300 + '.mp4'), '段' * 300,
                                                     '双语草稿.srt', stamp)), 150)
        with self.assertRaises(ValueError):
            layout.subtitle_filename(self.source, 'main', '../state.json', stamp)

    def test_snapshot_preserves_bytes_times_and_records_bound_metadata_without_media_copy(self):
        before = self.track.stat()
        identity = {'path': str(self.source), 'size': self.source.stat().st_size,
                    'mtime_ns': self.source.stat().st_mtime_ns, 'sha256': 'a' * 64}
        (self.project / 'campaign.json').write_text(json.dumps({'source': identity}))
        result = self.save()
        target = Path(result['folder'])
        self.assertTrue(target.is_relative_to(self.project / '导出'))
        self.assertEqual(self.track.read_bytes(), SRT)
        self.assertEqual(self.track.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(Path(result['files'][0]['path']).read_bytes(), SRT)
        manifest = json.loads((target / '版本记录.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['selection']['offset_ms'], 30000)
        self.assertEqual(manifest['selection']['timestamp_basis'], 'sample-relative')
        self.assertEqual(manifest['created_at'], '2026-10-03T09:00:00+08:00')
        self.assertEqual(manifest['generation_status'], 'complete')
        self.assertEqual(manifest['files'][0]['sha256'], hashlib.sha256(SRT).hexdigest())
        self.assertEqual(manifest['files'][0]['canonical'], '原文.srt')
        self.assertEqual(manifest['source']['sha256'], 'a' * 64)
        self.assertEqual(manifest['review_status'], 'unreviewed')
        self.assertIsNotNone(datetime.fromisoformat(result['saved_at']).utcoffset())
        self.assertEqual(json.loads((target / '输入来源.json').read_text(encoding='utf-8')), manifest['source'])
        self.assertFalse((self.project / '输入来源.json').exists())
        self.assertEqual(sorted(p.suffix for p in target.iterdir()), ['.json', '.json', '.srt'])

    def test_same_second_saves_allocate_distinct_versions_without_overwrite(self):
        fixed = datetime(2026, 10, 3, 9, 5, 4, tzinfo=timezone.utc)
        with patch.object(layout, 'datetime') as clock:
            clock.now.return_value = fixed
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            first = self.save()
            second = self.save()
        self.assertNotEqual(first['folder'], second['folder'])
        self.assertTrue(Path(second['folder']).name.endswith('-02'))
        self.assertEqual(Path(first['files'][0]['path']).read_bytes(), SRT)

    def test_invalid_empty_incomplete_and_oversized_tracks_never_publish(self):
        for data in (b'not srt', b'\xff', b'1\n00:00:02,000 --> 00:00:01,000\nx\n',
                     b'x' * (16 * 1024 * 1024 + 1)):
            with self.subTest(size=len(data)):
                self.track.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.save()
        self.track.write_bytes(b'')
        self.state.write_text('{"status":"running"}')
        with self.assertRaises(ValueError):
            self.save()
        self.assertFalse((self.project / '导出').exists())
        self.state.write_text('{"status":"complete"}')
        result = self.save()
        self.assertEqual(Path(result['files'][0]['path']).read_bytes(), b'')

    def test_no_tracks_or_outside_selection_is_rejected(self):
        self.selection['tracks'] = ['state.json']
        with self.assertRaises(ValueError):
            self.save()
        self.selection['tracks'] = ['中文草稿.srt']
        with self.assertRaises(ValueError):
            self.save()
        self.selection['folder'] = self.root
        with self.assertRaises(ValueError):
            self.save()

    def test_external_export_junction_is_rejected_without_touching_destination(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.project / '导出'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(list(outside.iterdir()), [])

    def test_user_edit_during_save_is_detected_and_own_staging_is_cleaned(self):
        original = Path.write_bytes
        original_stat = self.track.stat()
        def write(path, data):
            result = original(path, data)
            if path != self.track and path.suffix == '.srt':
                original(self.track, SRT.replace('字幕'.encode(), '修改'.encode()))
                os.utime(self.track, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            return result
        with patch.object(Path, 'write_bytes', write):
            with self.assertRaisesRegex(ValueError, '变化|更改'):
                self.save()
        self.assertIn('修改'.encode(), self.track.read_bytes())
        self.assertEqual(list((self.project / '导出').iterdir()), [])

    def test_write_failure_cleans_only_own_files(self):
        exports = self.project / '导出'
        exports.mkdir()
        keep = exports / 'existing.txt'
        keep.write_bytes(b'keep')
        original = Path.write_text
        def write(path, text, *args, **kwargs):
            if path.name == '版本记录.json':
                raise OSError('synthetic metadata failure')
            return original(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', write), self.assertRaises(OSError):
            self.save()
        self.assertEqual(list(exports.iterdir()), [keep])
        self.assertEqual(keep.read_bytes(), b'keep')

    def test_failed_publication_removes_partial_version_but_keeps_existing_export(self):
        exports = self.project / '导出'
        exports.mkdir()
        keep = exports / 'existing.srt'
        keep.write_bytes(SRT)
        original = Path.rename
        def rename(path, target):
            if path.name == '版本记录.json':
                raise OSError('synthetic publication failure')
            return original(path, target)
        with patch.object(Path, 'rename', rename), self.assertRaises(OSError):
            self.save()
        self.assertEqual(list(exports.iterdir()), [keep])
        self.assertEqual(keep.read_bytes(), SRT)

    def test_does_not_read_source_bytes_or_reuse_hash_with_mismatched_source_metadata(self):
        (self.project / 'campaign.json').write_text(json.dumps({'source': {
            'path': str(self.source), 'sha256': 'a' * 64, 'size': 1,
            'mtime_ns': self.source.stat().st_mtime_ns}}))
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path == self.source:
                raise AssertionError('large input must never be opened')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            result = self.save()
        metadata = json.loads((Path(result['folder']) / '输入来源.json').read_text(encoding='utf-8'))
        self.assertNotIn('sha256', metadata)

    def test_invalid_track_selector_shape_is_rejected(self):
        for value in (['原文.srt', {}], '原文.srt', None):
            self.selection['tracks'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.save()


if __name__ == '__main__':
    unittest.main()
