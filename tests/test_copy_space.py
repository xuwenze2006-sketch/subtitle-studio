"""Tiny-file checks for destination-volume publication space preflight."""
import errno
import importlib
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class CopySpaceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='subtitle-copy-space-')).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.source_dir, self.destination_dir = self.root / 'encoded', self.root / 'target'
        self.source_dir.mkdir()
        self.destination_dir.mkdir()
        self.partial = self.source_dir / 'result.partial.mp4'
        self.partial.write_bytes(b'actual encoded bytes')
        self.final = self.destination_dir / 'final.mp4'
        self.alias = self.root / 'destination-alias'
        self.marker = self.destination_dir / 'target-marker'
        self.addCleanup(self.cleanup)

    def cleanup(self):
        # Remove the junction itself before touching its separately owned target.
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith('subtitle-copy-space-'))
        if os.path.lexists(self.alias):
            self.assertEqual(self.alias.resolve(strict=True), self.destination_dir)
            self.alias.rmdir() if os.name == 'nt' else self.alias.unlink()
            self.assertTrue(self.destination_dir.is_dir())
            self.assertTrue(self.marker.is_file())
        for path in (self.partial, self.final, self.marker):
            path.unlink(missing_ok=True)
        self.source_dir.rmdir()
        self.destination_dir.rmdir()
        self.root.rmdir()

    def module(self):
        name = 'subtitle_pipeline.copy_space'
        self.assertIsNotNone(importlib.util.find_spec(name), 'Destination-volume copy-space preflight is not implemented')
        return importlib.import_module(name)

    def usage(self, free):
        return SimpleNamespace(total=1_000_000, used=0, free=free)

    def test_uses_actual_encoded_size_and_queries_only_resolved_destination(self):
        helper = self.module()
        before = self.partial.read_bytes()
        with patch.object(helper.shutil, 'disk_usage', return_value=self.usage(len(before))) as usage:
            self.assertIsNone(helper.require_copy_space(self.partial, self.final))
        usage.assert_called_once_with(self.destination_dir.resolve(strict=True))
        self.assertEqual(self.partial.read_bytes(), before)
        self.assertFalse(self.final.exists())

    def test_known_insufficient_space_raises_enospc_without_writing_or_removing_encoded_file(self):
        helper = self.module()
        before = self.partial.read_bytes()
        available = len(before) - 1
        with patch.object(helper.shutil, 'disk_usage', return_value=self.usage(available)):
            with self.assertRaises(OSError) as raised:
                helper.require_copy_space(self.partial, self.final)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertEqual(raised.exception.filename, str(self.destination_dir))
        self.assertIn(str(len(before)), raised.exception.strerror)
        self.assertIn(str(available), raised.exception.strerror)
        self.assertIn('编码结果已保留', raised.exception.strerror)
        self.assertEqual(self.partial.read_bytes(), before)
        self.assertFalse(self.final.exists())

    def test_exact_and_greater_free_space_are_allowed(self):
        helper = self.module()
        size = self.partial.stat().st_size
        for free in (size, size + 1):
            with self.subTest(free=free), patch.object(helper.shutil, 'disk_usage', return_value=self.usage(free)):
                helper.require_copy_space(self.partial, self.final)

    def test_same_directory_still_needs_one_additional_copy_not_zero_or_two(self):
        helper = self.module()
        final = self.source_dir / 'final.mp4'
        size = self.partial.stat().st_size
        with patch.object(helper.shutil, 'disk_usage', return_value=self.usage(size)) as usage:
            helper.require_copy_space(self.partial, final)
        usage.assert_called_once_with(self.source_dir)
        with patch.object(helper.shutil, 'disk_usage', return_value=self.usage(size - 1)):
            with self.assertRaises(OSError) as raised:
                helper.require_copy_space(self.partial, final)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertFalse(final.exists())

    def test_zero_byte_source_does_not_make_this_helper_a_media_validator(self):
        helper = self.module()
        self.partial.write_bytes(b'')
        with patch.object(helper.shutil, 'disk_usage', return_value=self.usage(0)):
            helper.require_copy_space(self.partial, self.final)

    def test_junction_directory_is_resolved_before_querying_target_volume(self):
        helper = self.module()
        self.marker.write_bytes(b'owned target remains intact')
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(self.destination_dir), str(self.alias))
        else:
            self.alias.symlink_to(self.destination_dir, target_is_directory=True)
        self.assertEqual(self.alias.resolve(strict=True), self.destination_dir)
        with patch.object(helper.shutil, 'disk_usage', return_value=self.usage(1000)) as usage:
            helper.require_copy_space(self.partial, self.alias / self.final.name)
        usage.assert_called_once_with(self.destination_dir)
        self.assertEqual(self.marker.read_bytes(), b'owned target remains intact')
        self.assertFalse(self.final.exists())

    def test_disk_statistics_error_is_preserved_instead_of_reporting_enough_space(self):
        helper = self.module()
        failure = OSError(errno.EIO, 'volume statistics unavailable')
        with patch.object(helper.shutil, 'disk_usage', side_effect=failure):
            with self.assertRaises(OSError) as raised:
                helper.require_copy_space(self.partial, self.final)
        self.assertIs(raised.exception, failure)
        self.assertTrue(self.partial.is_file())
        self.assertFalse(self.final.exists())

    def test_missing_source_or_target_parent_does_not_fall_back_to_another_volume(self):
        helper = self.module()
        with patch.object(helper.shutil, 'disk_usage') as usage:
            for source, final in ((self.root / 'missing.mp4', self.final),
                                  (self.partial, self.root / 'missing' / 'final.mp4')):
                with self.subTest(source=source, final=final), self.assertRaises(FileNotFoundError):
                    helper.require_copy_space(source, final)
            usage.assert_not_called()

    def test_invalid_statistics_are_not_treated_as_reliable_capacity(self):
        helper = self.module()
        original_stat = Path.stat
        for value in (-1, True, 1.5, None):
            with self.subTest(size=value):
                def source_stat(path, *args, **kwargs):
                    return SimpleNamespace(st_size=value) if path == self.partial else original_stat(path, *args, **kwargs)
                with patch.object(Path, 'stat', source_stat), patch.object(helper.shutil, 'disk_usage') as usage:
                    with self.assertRaises(ValueError):
                        helper.require_copy_space(self.partial, self.final)
                    usage.assert_not_called()
            with self.subTest(free=value), patch.object(helper.shutil, 'disk_usage', return_value=self.usage(value)):
                with self.assertRaises(ValueError):
                    helper.require_copy_space(self.partial, self.final)


if __name__ == '__main__':
    unittest.main()
