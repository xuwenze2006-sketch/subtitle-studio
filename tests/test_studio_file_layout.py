"""File organization uses only temporary projects and local HTTP requests."""
from datetime import datetime
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote, unquote

from subtitle_pipeline import studio


class StudioFileLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '日本语视频.mp4'
        self.source.write_bytes(b'media fixture')
        self.project = self.root / 'legacy project'
        self.project.mkdir()
        self.store = self.root / 'studio.json'
        for guard in (patch.object(studio, 'account_environment', return_value={}),
                      patch.object(studio, 'encrypted_names', return_value=set()),
                      patch.object(studio, 'engine_available', return_value=True)):
            guard.start()
            self.addCleanup(guard.stop)
        self.app = studio.StudioController(source=str(self.source), campaign=str(self.project), state_path=self.store)
        self.state = {'status': 'complete', 'created': 1700000000, 'review_status': 'unreviewed',
                      'config': {'source': str(self.source), 'project': str(self.project)}}
        self.write(self.project / 'state.json', self.state)
        self.srt = '1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n'
        (self.project / '原文.srt').write_text(self.srt, encoding='utf-8')

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def save(self, **overrides):
        return self.app.save_subtitles({'project_id': self.app._project_id(), 'sample': 'main', **overrides})

    def test_new_tasks_get_date_time_and_reserved_paths_do_not_collide(self):
        base = self.root / 'jobs' / '2026-10-03' / '203500_日本语视频-hash'
        with patch.object(studio, 'timestamped_project', return_value=base):
            first = self.app.select_project({'new_task': True, 'source': str(self.source), 'campaign': ''})
            second = self.app.select_project({'new_task': True, 'source': str(self.source), 'campaign': ''})
        self.assertEqual(first['campaign'], str(base))
        self.assertEqual(second['campaign'], str(base.with_name(base.name + '-02')))
        self.assertFalse(base.exists(), 'Selecting a task must leave the initializer an empty directory')
        created = second['file_layout']['created_at']
        self.assertTrue(created)
        self.app._persist()
        self.assertEqual(self.app.state()['file_layout']['created_at'], created)
        restored = studio.StudioController(state_path=self.store)
        self.assertEqual(restored.state()['file_layout']['created_at'], created)

    def test_legacy_paths_and_known_creation_time_remain_unchanged(self):
        layout = self.app.state()['file_layout']
        self.assertEqual(layout['project_path'], str(self.project))
        self.assertEqual(layout['source_path'], str(self.source))
        self.assertEqual(datetime.fromisoformat(layout['created_at']).timestamp(), 1700000000)
        self.state.pop('created')
        self.write(self.project / 'state.json', self.state)
        self.assertEqual(self.app.state()['file_layout']['created_at'], '')

    def test_save_makes_distinct_local_versions_without_altering_live_outputs(self):
        original = {p.name: p.read_bytes() for p in self.project.iterdir() if p.is_file()}
        first, second = self.save(), self.save()
        self.assertNotEqual(first['folder'], second['folder'])
        for result in (first, second):
            self.assertTrue(Path(result['folder']).is_relative_to(self.project / '导出'))
            self.assertEqual(result['review_status'], 'unreviewed')
            self.assertEqual(len(result['files']), 1)
            self.assertIn(self.source.stem, result['files'][0]['name'])
            self.assertEqual(Path(result['files'][0]['path']).read_text(encoding='utf-8'), self.srt)
            self.assertTrue((Path(result['folder']) / '输入来源.json').is_file())
        for name, data in original.items():
            self.assertEqual((self.project / name).read_bytes(), data)

    def test_save_requires_explicit_current_binding_and_idle_project(self):
        for body in ({}, {'project_id': ''}, {'project_id': 'f' * 64}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.app.save_subtitles(body)
        self.app.job['busy'] = True
        with self.assertRaisesRegex(ValueError, '停止|运行'):
            self.save()
        self.assertFalse((self.project / '导出').exists())

    def test_save_respects_other_process_project_lock(self):
        with studio.ProjectLock(self.project), self.assertRaises(RuntimeError):
            self.save()
        self.assertFalse((self.project / '导出').exists())

    def test_changed_project_source_is_not_exported_under_old_media_name(self):
        self.state['config']['source'] = str(self.root / 'other.mp4')
        self.write(self.project / 'state.json', self.state)
        with self.assertRaisesRegex(ValueError, '来源'):
            self.save()
        self.assertFalse((self.project / '导出').exists())

    def test_sample_snapshot_uses_selected_folder_and_offsets(self):
        sample_folder = self.project / '样片' / '02' / '识别任务'
        sample_folder.mkdir(parents=True)
        sample_srt = self.srt.replace('こんにちは', '样片字幕')
        (sample_folder / '原文.srt').write_text(sample_srt, encoding='utf-8')
        self.write(sample_folder / 'state.json', {'status': 'complete'})
        self.write(self.project / 'campaign.json', {
            'source': {'path': str(self.source)}, 'asr_provider': 'qwen_asr', 'status': 'samples_ready',
            'samples': [{'name': '中间样片', 'folder': '样片/02', 'start_sec': 1680, 'end_sec': 1800}]})
        result = self.save(sample='sample-1')
        self.assertEqual(Path(result['files'][0]['path']).read_text(encoding='utf-8'), sample_srt)
        record = json.loads((Path(result['folder']) / '版本记录.json').read_text(encoding='utf-8'))
        self.assertIn('1680000', json.dumps(record))

    def test_http_download_header_and_save_endpoint(self):
        import _socket
        import socket
        server = studio.ThreadingHTTPServer(('127.0.0.1', 0), studio.BaseHTTPRequestHandler)
        host = f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = studio.make_handler(self.app, 'fixture-token', host, self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        headers = {'X-Subtitle-Token': 'fixture-token', 'Content-Type': 'application/json'}
        def request(method, path, body=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            local_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                local_socket.settimeout(5)
                _socket.socket.connect(local_socket, ('127.0.0.1', server.server_port))
                connection.sock = local_socket
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                return response.status, response.getheader('Content-Disposition'), response.read()
            finally:
                connection.close()
                local_socket.close()
        status, disposition, body = request('GET', '/api/download?name=' + quote('原文.srt') + '&project=' + self.app._project_id())
        self.assertEqual(status, 200)
        filename = unquote(disposition.split("UTF-8''")[1])
        self.assertIn(self.source.stem, filename)
        self.assertRegex(filename, r'_\d{8}_\d{6}\.srt$')
        self.assertEqual(body, (self.project / '原文.srt').read_bytes())
        status, _, body = request('POST', '/api/save-subtitles', json.dumps({'project_id': self.app._project_id(), 'sample': 'main'}))
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(Path(result['folder']).is_dir())

    def test_download_name_and_body_use_same_opened_file_generation(self):
        path, filename = self.app.download_info('原文.srt', 'main', self.app._project_id())
        updated = self.srt.replace('こんにちは', '新しい字幕').encode('utf-8')
        path.write_bytes(updated)
        os.utime(path, (1700000020, 1700000020))
        handler = object.__new__(studio.make_handler(self.app, 'fixture', '127.0.0.1:7777', self.root))
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.file_response(path, True, filename)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        stamp = datetime.fromtimestamp(1700000020).strftime('%Y%m%d_%H%M%S')
        self.assertIn(stamp, unquote(headers['Content-Disposition']))
        self.assertEqual(handler.wfile.getvalue(), updated)


if __name__ == '__main__':
    unittest.main()
