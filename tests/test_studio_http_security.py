"""HTTP boundary regressions using synthetic files and exact loopback sockets."""
from contextlib import contextmanager
from datetime import date
import _socket
import http.client
import json
import math
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from subtitle_pipeline import studio


class StudioHTTPSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-http-security-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.static = self.root / 'static'
        self.static.mkdir()
        (self.static / 'index.html').write_bytes(b'local index')
        self.media = self.root / 'media.mp4'
        self.media.write_bytes(b'0123456789')
        self.controller = Mock()
        self.controller.state.return_value = {'app': 'synthetic'}
        self.controller.media_path.return_value = self.media
        self.token = 'synthetic-http-security-token'

    @contextmanager
    def server(self, *, timeout=.2, capacity=4, controller=None):
        class ObservedServer(studio.ThreadingHTTPServer):
            request_timeout = timeout
            max_active_requests = capacity

            def process_request_thread(inner, *args):
                with inner.changed:
                    inner.active += 1
                    inner.peak = max(inner.peak, inner.active)
                    inner.changed.notify_all()
                try:
                    return super().process_request_thread(*args)
                finally:
                    with inner.changed:
                        inner.active -= 1
                        inner.changed.notify_all()

            def handle_error(inner, *_args):
                inner.errors.append(True)

        server = ObservedServer(('127.0.0.1', 0), studio.BaseHTTPRequestHandler)
        server.changed = threading.Condition()
        server.active = server.peak = 0
        server.errors = []
        host = f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = studio.make_handler(
            controller if controller is not None else self.controller, self.token, host, self.static)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            yield server, host
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            with server.changed:
                self.assertTrue(server.changed.wait_for(lambda: server.active == 0, timeout=timeout + 1))

    @contextmanager
    def local_socket(self, server):
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            stream.settimeout(1)
            # Preserve the offline runner's global outbound-network guard.
            # This raw connect can reach only this test's own loopback server.
            _socket.socket.connect(stream, ('127.0.0.1', server.server_port))
            yield stream
        finally:
            stream.close()

    def request(self, server, path='/api/state', *, headers=None):
        with self.local_socket(server) as stream:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.sock = stream
            try:
                connection.request('GET', path, headers={'X-Subtitle-Token': self.token, **(headers or {})})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

    def wait_active(self, server, count):
        with server.changed:
            self.assertTrue(server.changed.wait_for(lambda: server.active == count, timeout=2),
                            f'Expected {count} active handlers, found {server.active}')

    def assert_closed(self, stream):
        try:
            self.assertEqual(stream.recv(4096), b'')
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        except TimeoutError:
            self.fail('The incomplete HTTP connection retained its handler without a deadline')

    def test_windows_network_device_drive_and_traversal_paths_reject_before_resolution(self):
        handler = object.__new__(studio.make_handler(None, self.token, '127.0.0.1:7777', self.static))
        handler.headers = {'Host': '127.0.0.1:7777'}
        handler.json_response = Mock()
        handler.file_response = Mock()
        paths = ('/%5C%5Caudit.invalid%5Cshare%5Cprobe.txt', '/%5c%5c.%5cNUL',
                 '/%5c%5c?%5cUNC%5caudit.invalid%5cshare', '/C%3A/windows/file',
                 '/C%3Arelative', '/assets%5c..%5cfile', '/file.txt%3Astream',
                 '/../outside', '/%2e%2e/outside', '/%00', '/NUL', '/assets/CON.txt')
        for path in paths:
            with self.subTest(path=path):
                handler.path = path
                handler.json_response.reset_mock()
                with patch.object(Path, 'resolve', side_effect=AssertionError('Unsafe path reached filesystem resolution')):
                    handler.do_GET()
                self.assertEqual(handler.json_response.call_args.args[1], 404)
        handler.file_response.assert_not_called()

    def test_resolved_symlink_escape_still_rejects(self):
        handler = object.__new__(studio.make_handler(None, self.token, '127.0.0.1:7777', self.static))
        handler.headers = {'Host': '127.0.0.1:7777'}
        handler.path = '/escape.txt'
        handler.json_response = Mock()
        handler.file_response = Mock()
        real_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.root / 'outside.txt' if path.name == 'escape.txt' else real_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve):
            handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[1], 404)
        handler.file_response.assert_not_called()

    def test_nonascii_header_and_cookie_authentication_fail_closed(self):
        for fields in ({'X-Subtitle-Token': 'é'}, {'Cookie': 'subtitle_session_7777="é"'}):
            with self.subTest(fields=fields):
                try:
                    allowed = studio.authorize_request({'Host': '127.0.0.1:7777', **fields}, self.token, '127.0.0.1:7777')
                except (TypeError, ValueError) as error:
                    self.fail(f'Malformed token escaped authentication: {error}')
                self.assertFalse(allowed)

    def test_nonfinite_legacy_prices_leave_state_and_configuration_repair_available(self):
        controller = studio.StudioController(state_path=self.root / 'absent-state.json')
        values = {'DASHSCOPE_API_KEY': 'synthetic-asr-key', 'DEEPSEEK_API_KEY': 'synthetic-translation-key',
                  'CLOUD_PRICING_VERIFIED_ON': date.today().isoformat(), 'CLOUD_PRICING_REFERENCE': 'synthetic prices'}
        with patch.object(studio, 'account_environment', return_value=values), \
                patch.object(studio, 'encrypted_names', return_value=set()), \
                patch.object(studio, 'engine_available', return_value=False), self.server(controller=controller) as (server, _host):
            for value in ('NaN', 'Infinity', '-Infinity', '1e309'):
                with self.subTest(value=value):
                    values['QWEN_ASR_INPUT_CNY_PER_MILLION'] = value
                    code, body = self.request(server)
                    self.assertEqual(code, 200)
                    state = json.loads(body)
                    self.assertFalse(state['accounts']['bailian']['ready'])
                    self.assertTrue(state['settings_error'])
                    self.assertTrue(math.isfinite(state['settings']['asr_input_rate']))
                    self.assertEqual(values['QWEN_ASR_INPUT_CNY_PER_MILLION'], value)

    def test_incomplete_unauthenticated_headers_expire_and_release_the_slot(self):
        with self.server(capacity=1) as (server, host):
            with self.local_socket(server) as stream:
                stream.sendall(f'GET / HTTP/1.1\r\nHost: {host}\r\n'.encode())
                self.wait_active(server, 1)
                self.assert_closed(stream)
            self.wait_active(server, 0)
            self.assertEqual(self.request(server)[0], 200)
        self.controller.stop.assert_not_called()

    def test_incomplete_post_body_expires_without_calling_business_actions(self):
        with self.server(capacity=1) as (server, host):
            with self.local_socket(server) as stream:
                stream.sendall((f'POST /api/stop HTTP/1.1\r\nHost: {host}\r\nX-Subtitle-Token: {self.token}\r\n'
                                'Content-Type: application/json\r\nContent-Length: 30\r\n\r\n{').encode())
                self.wait_active(server, 1)
                self.assert_closed(stream)
            self.wait_active(server, 0)
            self.assertEqual(self.request(server)[0], 200)
            self.assertEqual(server.errors, [])
        self.controller.stop.assert_not_called()

    def test_capacity_is_bounded_and_recovers_after_clients_disconnect(self):
        with self.server(timeout=3, capacity=2) as (server, host):
            with self.local_socket(server) as first, self.local_socket(server) as second:
                prefix = f'GET / HTTP/1.1\r\nHost: {host}\r\n'.encode()
                first.sendall(prefix)
                second.sendall(prefix)
                self.wait_active(server, 2)
                with self.local_socket(server) as extra:
                    try:
                        extra.sendall(f'GET / HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())
                    except (ConnectionResetError, ConnectionAbortedError):
                        pass
                    self.assert_closed(extra)
                self.assertEqual(server.peak, 2)
            self.wait_active(server, 0)
            self.assertEqual(self.request(server)[0], 200)

    def test_one_slow_client_does_not_block_state_static_files_or_media_ranges(self):
        with self.server(timeout=2, capacity=2) as (server, host):
            with self.local_socket(server) as slow:
                slow.sendall(f'GET / HTTP/1.1\r\nHost: {host}\r\n'.encode())
                self.wait_active(server, 1)
                self.assertEqual(self.request(server)[0], 200)
                self.wait_active(server, 1)
                self.assertEqual(self.request(server, '/'), (200, b'local index'))
                self.wait_active(server, 1)
                self.assertEqual(self.request(server, '/api/media', headers={'Range': 'bytes=2-5'}), (206, b'2345'))

    def test_socket_idle_deadline_does_not_cancel_slow_business_work(self):
        def read_state():
            time.sleep(.15)
            return {'app': 'synthetic'}
        self.controller.state.side_effect = read_state
        with self.server(timeout=.05, capacity=1) as (server, _host):
            self.assertEqual(self.request(server)[0], 200)
        self.controller.stop.assert_not_called()

    def test_handler_exception_releases_capacity(self):
        with self.server(capacity=1) as (server, _host):
            self.controller.state.side_effect = KeyError('synthetic handler failure')
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request(server)
            self.wait_active(server, 0)
            self.controller.state.side_effect = None
            self.assertEqual(self.request(server)[0], 200)

    def test_thread_start_failure_releases_capacity(self):
        with self.server(capacity=1) as (server, _host):
            with patch.object(threading.Thread, 'start', side_effect=RuntimeError('synthetic thread start failure')):
                with self.assertRaisesRegex(RuntimeError, 'thread start failure'):
                    server.process_request(Mock(), ('127.0.0.1', 1))
            self.assertEqual(self.request(server)[0], 200)


if __name__ == '__main__':
    unittest.main()
