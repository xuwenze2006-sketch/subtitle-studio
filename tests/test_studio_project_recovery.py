"""A damaged selected project must not make the desktop service unreachable."""
import _socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch

from tests import test_studio as fixture


class StudioProjectRecoveryTests(unittest.TestCase):
    setUp = fixture.StudioTests.setUp

    def damage(self, content='{"config":null,"status":"cancelled"}'):
        path = self.campaign / 'state.json'
        path.write_text(content, encoding='utf-8')
        return path

    def assert_recoverable(self, app=None, busy=False):
        app = app or self.app
        result = app.state()
        self.assertEqual(result['app'], '字幕工坊')
        self.assertTrue(result['project_error'])
        self.assertEqual(result['source'], str(self.source))
        self.assertEqual(result['campaign'], str(self.campaign))
        self.assertEqual(result['project_id'], app._project_id())
        self.assertEqual(result['job']['busy'], busy)
        self.assertEqual(result['actions']['stop'], busy)
        self.assertFalse(any(value for key, value in result['actions'].items() if key != 'stop'))
        self.assertEqual(result['file_layout']['video_path'], '')
        self.assertEqual(result['accounts']['deepseek']['configured'], True)
        json.dumps(result, allow_nan=False)
        return result

    def test_restored_malformed_state_remains_connected_without_rewriting_evidence(self):
        path = self.damage()
        self.app._persist()
        before = {p: p.read_bytes() for p in (path, self.store, self.source)}
        restored = self.s.StudioController(state_path=self.store)
        self.assert_recoverable(restored)
        self.assertFalse(restored.progress()['job']['busy'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_invalid_state_json_and_config_shapes_have_actionable_recovery(self):
        for content in ('{broken', '[]', '{"config":null}', '{"config":[]}',
                        '{"config":42}', '{"config":"old"}', '{"identity":null}'):
            with self.subTest(content=content):
                path = self.damage(content)
                original = path.read_bytes()
                self.assert_recoverable()
                self.assertEqual(path.read_bytes(), original)
        self.damage('{"identity":null}')
        with patch.object(self.s,'engine_available',return_value=False): self.assert_recoverable()

    def test_invalid_manifest_stays_readable_without_enabling_processing(self):
        path = self.campaign / 'campaign.json'
        for content in ('{broken', '[]', '{"source":null,"status":"exported"}',
                        '{"source":[],"status":"prepared"}'):
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                self.assert_recoverable()
                self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_damaged_project_cannot_start_new_work(self):
        path = self.damage()
        original = path.read_bytes()
        with patch.object(self.s, 'start_process', side_effect=AssertionError('started cloud work')), \
                patch.object(self.s, 'run_pipeline', side_effect=AssertionError('started local work')):
            for action in ('local', 'prepare', 'samples', 'full', 'export', 'export-draft'):
                with self.subTest(action=action), self.assertRaises(ValueError):
                    self.app.start({'action':action})
        self.assertFalse(self.app.job['busy'])
        self.assertEqual(path.read_bytes(), original)

    def test_repair_and_switch_to_a_good_project_clear_the_error(self):
        path = self.damage()
        self.assert_recoverable()
        config = {'source':str(self.source), 'project':str(self.campaign)}
        path.write_text(json.dumps({'config':config,'status':'cancelled'}), encoding='utf-8')
        self.assertFalse(self.app.state().get('project_error'))
        self.assertTrue(self.app.state()['actions']['local'])
        self.damage()
        good = self.root / 'good-project'; good.mkdir()
        (good / 'state.json').write_text(json.dumps({'config':{**config,'project':str(good)}}), encoding='utf-8')
        loaded = self.app.select_project({'campaign':str(good)})
        self.assertFalse(loaded.get('project_error'))
        self.assertEqual(loaded['campaign'], str(good))
        self.assertTrue(loaded['actions']['local'])

    def test_live_job_changes_preserve_stop_without_reenabling_corrupt_project(self):
        path = self.damage()
        read_text = Path.read_text
        for busy in (True, False):
            entered = threading.Event(); release = threading.Event()
            def delayed(candidate, *args, **kwargs):
                if candidate == path:
                    entered.set()
                    if not release.wait(5): raise AssertionError('read was not released')
                return read_text(candidate, *args, **kwargs)
            with patch.object(Path, 'read_text', delayed), ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.app.state)
                try:
                    self.assertTrue(entered.wait(3))
                    with self.app.lock: self.app.job = {**self.app.job,'busy':busy,'status':'running' if busy else 'cancelled'}
                    self.assertEqual(self.app.progress()['job']['busy'], busy)
                finally: release.set()
                result = pending.result(3)
            self.assertTrue(result['project_error'])
            self.assertEqual(result['job']['busy'], busy)
            self.assertEqual(result['actions']['stop'], busy)
            self.assertFalse(any(value for key,value in result['actions'].items() if key != 'stop'))

    @contextmanager
    def server(self):
        token = 'synthetic-project-recovery-token'
        server = self.s.ThreadingHTTPServer(('127.0.0.1',0), self.s.BaseHTTPRequestHandler)
        host = f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = self.s.make_handler(self.app, token, host, self.root)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        connection_type = http.client.HTTPConnection
        class ExactLoopbackConnection(connection_type):
            def connect(connection):
                if (connection.host, connection.port) != ('127.0.0.1', server.server_port):
                    raise AssertionError('unexpected endpoint')
                connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                connection.sock.settimeout(3)
                _socket.socket.connect(connection.sock, ('127.0.0.1',server.server_port))
        try:
            with patch.object(self.s.http.client, 'HTTPConnection', ExactLoopbackConnection):
                yield token, server.server_port
        finally:
            server.shutdown(); worker.join(3); server.server_close()

    def test_real_http_state_and_existing_instance_reconnect_survive_project_damage(self):
        path = self.damage(); original = path.read_bytes()
        with self.server() as (token, port):
            connection = self.s.http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request('GET','/api/state',headers={'X-Subtitle-Token':token})
                response = connection.getresponse(); body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(body['project_error'])
            finally: connection.close()
            runtime = self.root / 'runtime.json'
            record = {'pid':os.getpid(), 'url':f'http://127.0.0.1:{port}/#token={token}'}
            runtime.write_text(json.dumps(record), encoding='utf-8')
            self.assertEqual(self.s.running_instance(runtime), record)
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__': unittest.main()
