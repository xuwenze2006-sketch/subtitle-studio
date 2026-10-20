"""Authenticated HTTP work must survive an otherwise valid idle decision."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import _socket
import http.client
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from subtitle_pipeline import studio


class StudioActiveRequestsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='studio-active-http-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'index.html').write_bytes(b'isolated static page')
        self.payload = b'a' * (256 * 1024) + b'b' * (256 * 1024)
        self.media = self.root / 'media.mp4'
        self.media.write_bytes(self.payload)
        self.app = studio.StudioController(state_path=self.root / 'absent.json')
        self.token = 'synthetic-active-request-token'
        for guard in (patch.object(studio, 'account_environment', return_value={}),
                      patch.object(studio, 'encrypted_names', return_value=set())):
            guard.start()
            self.addCleanup(guard.stop)

    @contextmanager
    def server(self, *, transform=lambda handler: handler, timeout=1):
        class ObservedServer(studio.ThreadingHTTPServer):
            request_timeout = timeout

            def process_request_thread(inner, *args):
                with inner.changed:
                    inner.active += 1
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
        server.active = 0
        server.errors = []
        server.last_touch = time.monotonic()
        host = f'127.0.0.1:{server.server_port}'
        server.RequestHandlerClass = transform(studio.make_handler(self.app, self.token, host, self.root))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.wait_finished(server)

    @contextmanager
    def connection(self, server):
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stream.settimeout(2)
        try:
            # Reach only this fixture's loopback socket; retain the offline
            # runner's outbound guard for every other destination.
            _socket.socket.connect(stream, ('127.0.0.1', server.server_port))
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.sock = stream
            try:
                yield connection
            finally:
                connection.close()
        finally:
            stream.close()

    def request(self, connection, path='/api/state', *, method='GET', body=None, headers=None):
        connection.request(method, path, body=body,
            headers={'X-Subtitle-Token': self.token, **(headers or {})})
        response = connection.getresponse()
        self.addCleanup(response.close)
        return response

    def wait_finished(self, server):
        with server.changed:
            self.assertTrue(server.changed.wait_for(lambda: server.active == 0, timeout=3),
                            'A fixture handler did not finish')

    def age(self, server):
        with self.app.lock:
            server.last_touch = time.monotonic() - 181

    def assert_released(self, server):
        self.wait_finished(server)
        self.age(server)
        self.assertTrue(self.app.claim_idle_shutdown(server), 'Completed request retained idle activity')

    def test_media_body_holds_shutdown_and_completion_starts_a_fresh_idle_period(self):
        first_block, release = threading.Event(), threading.Event()

        class OutputGate:
            def __init__(inner, original):
                inner.original = original

            def __getattr__(inner, name):
                return getattr(inner.original, name)

            def write(inner, data):
                result = inner.original.write(data)
                if len(data) == 256 * 1024 and not first_block.is_set():
                    first_block.set()
                    if not release.wait(3):
                        raise AssertionError('Media body gate was not released')
                return result

        def transform(base):
            class Handler(base):
                def setup(inner):
                    super().setup()
                    inner.wfile = OutputGate(inner.wfile)
            return Handler

        with patch.object(self.app, 'media_path', return_value=self.media), \
                self.server(transform=transform) as server, self.connection(server) as connection:
            try:
                response = self.request(connection, '/api/media')
                self.assertEqual(response.status, 200)
                self.assertEqual(int(response.getheader('Content-Length')), 524288)
                first = response.read(256 * 1024)
                self.assertTrue(first_block.wait(2))
                self.age(server)
                self.assertFalse(self.app.claim_idle_shutdown(server),
                                 'An authenticated media transfer was declared idle')
            finally:
                release.set()
            self.assertEqual(first + response.read(), self.payload)
            self.wait_finished(server)
            self.assertFalse(self.app.claim_idle_shutdown(server),
                             'A long completed request did not receive a fresh idle period')
            self.assertEqual(self.media.read_bytes(), self.payload)
            self.assert_released(server)

    def test_controller_execution_is_active_until_its_response_finishes(self):
        entered, release = threading.Event(), threading.Event()

        def state():
            entered.set()
            if not release.wait(3):
                raise AssertionError('Controller gate was not released')
            return {'isolated': True}

        with patch.object(self.app, 'state', side_effect=state), self.server() as server, \
                self.connection(server) as connection, ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lambda: (lambda response: (response.status, response.read()))(self.request(connection)))
            try:
                self.assertTrue(entered.wait(2))
                self.age(server)
                self.assertFalse(self.app.claim_idle_shutdown(server))
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2)[0], 200)
            self.assert_released(server)

    def observe_body(self, entered):
        def transform(base):
            class Reader:
                def __init__(inner, original):
                    inner.original = original

                def __getattr__(inner, name):
                    return getattr(inner.original, name)

                def read(inner, length):
                    entered.set()
                    return inner.original.read(length)

            class Handler(base):
                def setup(inner):
                    super().setup()
                    inner.rfile = Reader(inner.rfile)
            return Handler
        return transform

    def send_partial_body(self, connection, server):
        connection.sock.sendall((f'POST /api/session HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'
            f'X-Subtitle-Token: {self.token}\r\nContent-Type: application/json\r\n'
            'Content-Length: 2\r\n\r\n{').encode())

    def test_authenticated_body_read_is_active_and_released_after_success(self):
        entered = threading.Event()
        with self.server(transform=self.observe_body(entered)) as server, self.connection(server) as connection:
            self.send_partial_body(connection, server)
            self.assertTrue(entered.wait(2))
            self.age(server)
            try:
                self.assertFalse(self.app.claim_idle_shutdown(server), 'Authenticated body read was declared idle')
            finally:
                connection.sock.sendall(b'}')
            response = http.client.HTTPResponse(connection.sock)
            self.addCleanup(response.close)
            response.begin()
            self.assertEqual(response.status, 200)
            response.read()
            self.assert_released(server)

    def test_disconnected_authenticated_body_releases_activity(self):
        entered = threading.Event()
        with self.server(transform=self.observe_body(entered)) as server:
            with self.connection(server) as connection:
                self.send_partial_body(connection, server)
                self.assertTrue(entered.wait(2))
                connection.sock.shutdown(socket.SHUT_RDWR)
            self.assert_released(server)

    def test_unauthorized_static_and_incomplete_headers_do_not_prevent_shutdown(self):
        with self.server() as server:
            with self.connection(server) as connection:
                response = self.request(connection, headers={'X-Subtitle-Token': 'wrong'})
                self.assertEqual(response.status, 403)
                response.read()
            with self.connection(server) as connection:
                response = self.request(connection, '/')
                self.assertEqual((response.status, response.read()), (200, b'isolated static page'))
            self.wait_finished(server)
            with self.connection(server) as connection:
                connection.sock.sendall(f'GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'.encode())
                with server.changed:
                    self.assertTrue(server.changed.wait_for(lambda: server.active == 1, timeout=2))
                self.age(server)
                self.assertTrue(self.app.claim_idle_shutdown(server))
                connection.sock.shutdown(socket.SHUT_RDWR)

    def test_authentication_after_shutdown_claim_returns_existing_503(self):
        with self.server() as server:
            self.age(server)
            self.assertTrue(self.app.claim_idle_shutdown(server))
            for method, body in (('GET', None), ('POST', '{}')):
                with self.subTest(method=method), self.connection(server) as connection:
                    response = self.request(connection, '/api/session' if body else '/api/state',
                        method=method, body=body, headers={'Content-Type': 'application/json'})
                    self.assertEqual(response.status, 503)
                    response.read()
            self.wait_finished(server)

    def test_pending_static_and_unauthorized_responses_are_not_admitted(self):
        for path, token, status, method in (('/', self.token, 200, 'file_response'),
                                           ('/api/state', 'wrong', 403, 'json_response')):
            with self.subTest(path=path):
                self.app = studio.StudioController(state_path=self.root / 'absent.json')
                entered, release = threading.Event(), threading.Event()

                def transform(base):
                    original = getattr(base, method)

                    def respond(inner, *args, **kwargs):
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('Response gate was not released')
                        return original(inner, *args, **kwargs)

                    return type('GatedResponseHandler', (base,), {method: respond})

                with self.server(transform=transform) as server, self.connection(server) as connection, \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(lambda: self.request(connection, path,
                        headers={'X-Subtitle-Token': token}))
                    try:
                        self.assertTrue(entered.wait(2))
                        self.age(server)
                        self.assertTrue(self.app.claim_idle_shutdown(server))
                    finally:
                        release.set()
                    response = pending.result(timeout=2)
                    self.assertEqual(response.status, status)
                    response.read()

    def test_authenticated_body_timeout_releases_activity(self):
        entered = threading.Event()
        with self.server(transform=self.observe_body(entered), timeout=.1) as server, \
                self.connection(server) as connection:
            self.send_partial_body(connection, server)
            self.assertTrue(entered.wait(2))
            response = http.client.HTTPResponse(connection.sock)
            self.addCleanup(response.close)
            with self.assertRaises(http.client.RemoteDisconnected):
                response.begin()
            self.assert_released(server)

    def test_error_responses_release_each_admission(self):
        with patch.object(self.app, 'media_path', return_value=self.media), self.server() as server:
            cases = (('/api/missing', 'GET', None, {}, 404),
                     ('/api/media', 'GET', None, {'Range': 'bytes=999999-'}, 416),
                     ('/api/session', 'POST', '{}', {'Content-Type': 'text/plain'}, 415),
                     ('/api/session', 'POST', '[]', {'Content-Type': 'application/json'}, 400),
                     ('/api/missing', 'POST', '{}', {'Content-Type': 'application/json'}, 404))
            for path, method, body, headers, status in cases:
                with self.subTest(path=path, status=status), self.connection(server) as connection:
                    response = self.request(connection, path, method=method, body=body, headers=headers)
                    self.assertEqual(response.status, status)
                    response.read()
                self.wait_finished(server)
                self.assertTrue(self.app.idle_ready(), 'Error response retained idle activity')
            self.assert_released(server)

    def test_uncaught_get_exception_releases_activity(self):
        with patch.object(self.app, 'state', side_effect=AssertionError('isolated failure')), self.server() as server:
            with self.connection(server) as connection:
                with self.assertRaises(http.client.RemoteDisconnected):
                    self.request(connection)
            self.assert_released(server)
            self.assertEqual(server.errors, [True])

    def test_one_completed_request_cannot_release_another_live_request(self):
        entered = (threading.Event(), threading.Event())
        release = (threading.Event(), threading.Event())
        calls = iter(range(2))

        def state():
            with self.app.lock:
                index = next(calls)
            entered[index].set()
            if not release[index].wait(3):
                raise AssertionError('Concurrent controller gate was not released')
            return {'request': index}

        def get(server):
            with self.connection(server) as connection:
                response = self.request(connection)
                return response.status, response.read()

        with patch.object(self.app, 'state', side_effect=state), self.server() as server, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(get, server)
            try:
                self.assertTrue(entered[0].wait(2))
                second = pool.submit(get, server)
                self.assertTrue(entered[1].wait(2))
                release[0].set()
                self.assertEqual(first.result(timeout=2)[0], 200)
                self.age(server)
                self.assertFalse(self.app.claim_idle_shutdown(server),
                                 'The first completion released the second request')
            finally:
                for event in release:
                    event.set()
            self.assertEqual(second.result(timeout=2)[0], 200)
            self.assert_released(server)

    def test_keepalive_reentry_releases_each_request_before_next_headers(self):
        completed = threading.Condition()
        count = [0]

        def transform(base):
            class Handler(base):
                protocol_version = 'HTTP/1.1'

                def handle_one_request(inner):
                    try:
                        return super().handle_one_request()
                    finally:
                        with completed:
                            count[0] += 1
                            completed.notify_all()
            return Handler

        with self.server(transform=transform) as server, self.connection(server) as connection:
            for index in (1, 2):
                response = self.request(connection, '/api/session', method='POST', body='{}',
                                        headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status, 200)
                response.read()
                with completed:
                    self.assertTrue(completed.wait_for(lambda: count[0] >= index, timeout=2))
                self.assertTrue(self.app.idle_ready(), 'Keepalive retained completed request activity')
            self.age(server)
            self.assertTrue(self.app.claim_idle_shutdown(server), 'Waiting for next headers was counted as activity')
            response = self.request(connection, '/api/session', method='POST', body='{}',
                                    headers={'Content-Type': 'application/json', 'Connection': 'close'})
            self.assertEqual(response.status, 503)
            response.read()

    def test_reader_cleanup_failure_still_releases_activity(self):
        def transform(base):
            class Reader:
                def __init__(inner, original):
                    inner.original = original
                    inner.fail_cleanup = False

                def __getattr__(inner, name):
                    return getattr(inner.original, name)

                def end_read(inner):
                    inner.original.end_read()
                    if inner.fail_cleanup:
                        raise OSError('isolated reader cleanup failure')

            class Handler(base):
                def setup(inner):
                    super().setup()
                    inner.rfile = Reader(inner.rfile)

                def json_response(inner, *args, **kwargs):
                    result = super().json_response(*args, **kwargs)
                    inner.rfile.fail_cleanup = True
                    return result
            return Handler

        with self.server(transform=transform) as server, self.connection(server) as connection:
            response = self.request(connection, '/api/session', method='POST', body='{}',
                                    headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status, 200)
            response.read()
            self.assert_released(server)
            self.assertEqual(server.errors, [True])


if __name__ == '__main__':
    unittest.main()
