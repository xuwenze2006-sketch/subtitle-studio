"""Subtitle saves use temporary projects and never inspect unrelated video bytes."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio
from subtitle_pipeline.languages import output_names, video_output_path
from tests.studio_export_fixtures import approve_export_fixture


class StudioSnapshotResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='snapshot-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.mp4'
        self.source.write_bytes(b'synthetic source; no decoder is used')
        self.project = self.root / 'project'
        self.folder = self.project / '整片'
        self.folder.mkdir(parents=True)
        info = self.source.stat()
        self.identity = {'path': str(self.source), 'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                         'size': info.st_size, 'mtime_ns': info.st_mtime_ns}
        self.state = {'status': 'complete', 'created': 1700000000,
                      'config': {'source': str(self.source), 'project': str(self.project),
                                 'language': 'ja', 'target': 'zh-CN', 'asr_provider': 'qwen_asr'}}
        self.write_state()
        self.manifest = {'version': 1, 'source': self.identity, 'language': 'ja', 'target': 'zh-CN',
                         'status': 'full_ready', 'asr_provider': 'qwen_asr', 'samples': []}
        self.write_json(self.project / 'campaign.json', self.manifest)
        self.tracks = dict(zip(output_names('zh-CN'), (
            '1\r\n00:00:01,000 --> 00:00:02,000\r\nこんにちは\r\n\r\n'.encode('utf-8'),
            '1\n00:00:01,000 --> 00:00:02,000\n你好\n\n'.encode('utf-8'),
            '\ufeff1\n00:00:01,000 --> 00:00:02,000\n你好\nこんにちは\n\n'.encode('utf-8'))))
        for name, body in self.tracks.items():
            (self.folder / name).write_bytes(body)
        for guard in (patch.object(studio, 'account_environment', return_value={}),
                      patch.object(studio, 'encrypted_names', return_value=set()),
                      patch.object(studio, 'engine_available', return_value=False),
                      patch.object(studio, 'start_process', side_effect=AssertionError('process access forbidden'))):
            guard.start()
            self.addCleanup(guard.stop)
        self.app = studio.StudioController(source=str(self.source), campaign=str(self.project),
                                           state_path=self.root / 'studio.json')

    def write_json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def write_state(self):
        self.write_json(self.project / 'state.json', self.state)
        self.write_json(self.folder / 'state.json', {
            **self.state, 'config': {**self.state['config'], 'project': str(self.folder)}})

    def publish_video(self):
        self.video = video_output_path(self.source, 'zh-CN')
        body = b'synthetic approved video\0' * (1024 * 1024)
        self.video.write_bytes(body)
        self.manifest.update(status='exported', output=str(self.video),
                             output_sha256=hashlib.sha256(body).hexdigest(),
                             output_binding={'source_sha256': self.identity['sha256'], 'render_version': 1})
        approve_export_fixture(self.project, self.manifest)
        self.write_json(self.project / 'campaign.json', self.manifest)

    def save_record(self):
        receipt = self.app.save_subtitles({'project_id': self.app._project_id(), 'sample': 'main'})
        folder = Path(receipt['folder'])
        record = json.loads((folder / '版本记录.json').read_text(encoding='utf-8'))
        self.assertEqual(len(receipt['files']), 3)
        self.assertEqual({item['canonical'] for item in record['files']}, set(self.tracks))
        for item in record['files']:
            body = self.tracks[item['canonical']]
            self.assertEqual((folder / item['path']).read_bytes(), body)
            self.assertEqual(item['sha256'], hashlib.sha256(body).hexdigest())
            self.assertEqual((self.folder / item['canonical']).read_bytes(), body)
        return record

    def assert_created_timestamp(self, record, expected):
        if expected is None:
            self.assertEqual(record['created_at'], '')
        else:
            created = datetime.fromisoformat(record['created_at'])
            self.assertIsNotNone(created.utcoffset())
            self.assertEqual(created.timestamp(), expected)

    def test_saving_exported_subtitles_never_opens_or_hashes_official_video(self):
        self.publish_video()
        self.app.recent = [{'path': str(self.project), 'created': 1700000100}]
        video_opens, video_hashes = [], []
        open_file, digest_file = Path.open, hashlib.file_digest

        def observe_open(path, *args, **kwargs):
            if path == self.video:
                video_opens.append(str(path))
            return open_file(path, *args, **kwargs)

        def observe_digest(stream, *args, **kwargs):
            if Path(stream.name) == self.video:
                video_hashes.append(str(stream.name))
            return digest_file(stream, *args, **kwargs)

        with patch.object(Path, 'open', observe_open), patch.object(hashlib, 'file_digest', observe_digest):
            record = self.save_record()
        self.assert_created_timestamp(record, 1700000100)
        self.assertEqual({'opens': video_opens, 'hashes': video_hashes}, {'opens': [], 'hashes': []},
                         'Saving subtitles must not read or hash the independent official video')

    def test_snapshot_creation_time_ignores_other_projects_and_uses_saved_state(self):
        self.app.recent = [{'path': str(self.root / 'other'), 'created': 1700000100}]
        self.assert_created_timestamp(self.save_record(), 1700000000)

    def test_snapshot_creation_time_uses_saved_state_for_false_recent_values(self):
        for created in (None, False, 0, '', {}):
            with self.subTest(created=created):
                self.app.recent = [{'path': str(self.project), 'created': created}]
                self.assert_created_timestamp(self.save_record(), 1700000000)

    def test_snapshot_creation_time_keeps_truthy_invalid_recent_values_empty(self):
        for created in (True, -1, '1700000100', float('nan'), float('inf'), 1e300):
            with self.subTest(created=created):
                self.app.recent = [{'path': str(self.project), 'created': created}]
                self.assert_created_timestamp(self.save_record(), None)

    def test_snapshot_creation_time_rejects_missing_or_invalid_saved_values(self):
        self.app.recent = []
        for created in (None, False, True, 0, -1, '1700000000', float('nan'), float('inf'), 1e300):
            with self.subTest(created=created):
                self.state['created'] = created
                self.write_state()
                self.assert_created_timestamp(self.save_record(), None)
        self.state.pop('created')
        self.write_state()
        self.assert_created_timestamp(self.save_record(), None)

    def test_file_layout_still_qualifies_video_and_rejects_replaced_content(self):
        self.publish_video()
        layout = self.app.state()['file_layout']
        self.assertEqual(layout['video_path'], str(self.video))
        self.assertEqual(datetime.fromisoformat(layout['created_at']).timestamp(), 1700000000)
        self.video.write_bytes(b'replaced independent video')
        self.assertEqual(self.app.state()['file_layout']['video_path'], '')


if __name__ == '__main__':
    unittest.main()
