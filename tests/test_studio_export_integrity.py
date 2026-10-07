"""Official video links stay bound to recorded bytes, using only temporary files."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from tests import test_studio


class StudioExportIntegrityTests(unittest.TestCase):
    setUp = test_studio.StudioTests.setUp
    make_samples = test_studio.StudioTests.make_samples

    def publish_fixture(self):
        manifest = self.make_samples()
        self.output = self.source.with_name(self.source.stem + '_中文字幕_修订版.mp4')
        self.output.write_bytes(b'verified export')
        manifest['source']['sha256'] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        manifest.update(status='exported', output=str(self.output),
                        output_sha256=hashlib.sha256(self.output.read_bytes()).hexdigest(),
                        output_binding={'source_sha256': manifest['source']['sha256'],
                                        'captions_sha256': 'c'*64,
                                        'review_artifacts_sha256': 'd'*64,
                                        'render_version': 1})
        self.write_manifest(manifest)
        return manifest

    def write_manifest(self, manifest):
        (self.campaign / 'campaign.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_replaced_official_video_is_not_previewed_downloaded_or_opened(self):
        self.publish_fixture()
        self.assertEqual(self.app.exported_video_path(), self.output)
        self.output.write_bytes(b'overwritten file')
        self.assertIsNone(self.app.preview()['exported_video'])
        self.assertIsNone(self.app.exported_video_path())
        with self.assertRaises(ValueError):
            self.app.download_path('video')
        with patch.object(self.s.os, 'startfile', create=True) as opened:
            with self.assertRaises(ValueError):
                self.app.open_result('video')
        opened.assert_not_called()

    def test_unchanged_official_video_reuses_hash_across_state_preview_and_media_reads(self):
        self.publish_fixture()
        with patch.object(self.s.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            self.assertEqual(self.app.exported_video_path(), self.output)
            for _ in range(3):
                self.assertEqual(self.app.state()['file_layout']['video_path'], str(self.output))
                self.assertIsNotNone(self.app.preview()['exported_video'])
                self.assertEqual(self.app.exported_video_path(), self.output)
                self.assertEqual(self.app.download_path('video'), self.output)
            self.assertEqual(digest.call_count, 1)

    def test_same_size_replacement_with_preserved_mtime_invalidates_cached_file_identity(self):
        self.publish_fixture()
        self.assertEqual(self.app.exported_video_path(), self.output)
        info = self.output.stat()
        replacement = self.output.with_suffix('.replacement')
        replacement.write_bytes(b'x' * info.st_size)
        os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
        replacement.replace(self.output)
        self.assertEqual(self.output.stat().st_size, info.st_size)
        self.assertEqual(self.output.stat().st_mtime_ns, info.st_mtime_ns)
        self.assertIsNone(self.app.exported_video_path())

    def test_export_binding_from_another_source_is_rejected_before_hashing_video(self):
        manifest = self.publish_fixture()
        manifest['output_binding']['source_sha256'] = 'f' * 64
        self.write_manifest(manifest)
        with patch.object(self.s.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
            self.assertIsNone(self.app.exported_video_path())
            self.assertEqual(digest.call_count, 0)


if __name__ == '__main__':
    unittest.main()
