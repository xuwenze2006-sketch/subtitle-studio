"""Real owned children and loopback I/O; never contact a translation provider."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from subtitle_pipeline import local_process, translate as tr


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        original = subprocess.Popen
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            self.children.append(child)
            return child
        guard = patch.object(local_process.subprocess, 'Popen', side_effect=start)
        guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.reap)
        offline = patch.dict(os.environ, {'SUBTITLE_STUDIO_OFFLINE': '1'})
        offline.start()
        self.addCleanup(offline.stop)

    def reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)

    def assert_reaped(self):
        self.assertTrue(self.children, 'the test must exercise a real child')
        for child in self.children:
            self.assertIsNotNone(child.returncode)
            self.assertEqual(child.wait(timeout=0.1), child.returncode)

    @contextmanager
    def server(self, mode='ok', *, body=None, location=None):
        ready, release = threading.Event(), threading.Event()
        payload = body if body is not None else json.dumps([[['你好。']]], ensure_ascii=False).encode()
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(3)
                    self.request.recv(32768)
                    ready.set()
                    if mode in ('headers', 'tls'):
                        release.wait(5)
                        return
                    if mode == 'redirect':
                        self.request.sendall(('HTTP/1.1 302 Found\r\nLocation: ' + location +
                                              '\r\nContent-Length: 0\r\n\r\n').encode())
                        return
                    if mode == 'http':
                        self.request.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 9999\r\n\r\n')
                        release.wait(5)
                        return
                    self.request.sendall(('HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)) +
                                          '\r\nConnection: close\r\n\r\n').encode())
                    if mode == 'drip':
                        for byte in payload:
                            self.request.sendall(bytes([byte]))
                            if release.wait(0.1):
                                return
                    else:
                        self.request.sendall(payload)
                except (OSError, TimeoutError):
                    pass
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        with Server(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02))
            thread.start()
            try:
                yield f'http://127.0.0.1:{server.server_address[1]}/', ready
            finally:
                release.set()
                server.shutdown()
                thread.join(timeout=3)

    def test_normal_unicode_and_http_error_without_reading_error_body(self):
        with self.server() as (url, _):
            self.assertEqual(tr._request_json(url), [[['你好。']]])
        with self.server('http') as (url, _):
            started = time.monotonic()
            with self.assertRaises(HTTPError) as raised:
                tr._request_json(url)
            self.assertEqual(raised.exception.code, 429)
            raised.exception.close()
            self.assertLess(time.monotonic() - started, 2)
        self.assert_reaped()

    def test_total_deadline_covers_drip_body_headers_and_tls(self):
        for mode in ('drip', 'headers', 'tls'):
            with self.subTest(mode=mode), self.server(mode) as (url, ready), \
                 patch.object(tr, '_REQUEST_TIMEOUT', 0.6, create=True):
                if mode == 'tls':
                    url = url.replace('http:', 'https:')
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    tr._request_json(url)
                self.assertTrue(ready.is_set(), 'must reach the actual blocked I/O')
                self.assertLess(time.monotonic() - started, 2)
        self.assert_reaped()

    def test_cancel_inflight_body_and_headers(self):
        for mode in ('drip', 'headers', 'tls'):
            with self.subTest(mode=mode), self.server(mode) as (url, ready):
                if mode == 'tls':
                    url = url.replace('http:', 'https:')
                stop = threading.Event()
                def cancel():
                    if ready.wait(3):
                        stop.set()
                watcher = threading.Thread(target=cancel)
                watcher.start()
                try:
                    started = time.monotonic()
                    with self.assertRaises(tr.TranslationCancelled):
                        tr._request_json(url, stop_event=stop)
                    self.assertTrue(ready.is_set())
                    self.assertLess(time.monotonic() - started, 2)
                finally:
                    watcher.join(timeout=4)
        self.assert_reaped()

    def test_cancel_also_covers_blocked_dns_and_connect(self):
        original = local_process.capture_process
        for stage in ('getaddrinfo', 'connect'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / 'started'
                stop = threading.Event()
                target = 'socket.getaddrinfo' if stage == 'getaddrinfo' else 'socket.socket.connect'
                program = ("import runpy,socket,time; from pathlib import Path\n"
                           f"def stall(*a,**k):\n Path({str(ready)!r}).write_text('ready'); time.sleep(10)\n"
                           f"{target}=stall\n"
                           "runpy.run_module('subtitle_pipeline.translation_transport',run_name='__main__')")
                def capture(args, **kwargs):
                    return original([sys.executable, '-X', 'utf8', '-c', program], **kwargs)
                def cancel():
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    stop.set()
                watcher = threading.Thread(target=cancel)
                watcher.start()
                try:
                    started = time.monotonic()
                    with patch.object(local_process, 'capture_process', side_effect=capture):
                        with self.assertRaises(tr.TranslationCancelled):
                            tr._request_json('http://127.0.0.1:9/', stop_event=stop)
                    self.assertTrue(ready.exists())
                    self.assertLess(time.monotonic() - started, 2)
                finally:
                    watcher.join(timeout=4)
        self.assert_reaped()

    def test_deadline_also_covers_blocked_dns_and_connect(self):
        original = local_process.capture_process
        for stage in ('getaddrinfo', 'connect'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / 'started'
                target = 'socket.getaddrinfo' if stage == 'getaddrinfo' else 'socket.socket.connect'
                program = ("import runpy,socket,time; from pathlib import Path\n"
                           f"def stall(*a,**k):\n Path({str(ready)!r}).write_text('ready'); time.sleep(10)\n"
                           f"{target}=stall\n"
                           "runpy.run_module('subtitle_pipeline.translation_transport',run_name='__main__')")
                def capture(args, **kwargs):
                    return original([sys.executable, '-X', 'utf8', '-c', program], **kwargs)
                with patch.object(local_process, 'capture_process', side_effect=capture), \
                     patch.object(tr, '_REQUEST_TIMEOUT', 0.6, create=True):
                    with self.assertRaises(TimeoutError):
                        tr._request_json('http://127.0.0.1:9/')
                self.assertTrue(ready.exists(), 'must enter the selected network phase')
        self.assert_reaped()

    def test_cancel_preserves_completed_cache_releases_queue_and_does_not_expose_text_in_argv(self):
        original = tr._request_json
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory, self.server('drip') as (url, ready):
            cache = Path(directory) / 'cache.json'
            calls = []
            def request(provider_url, stop_event=None):
                calls.append(provider_url)
                if len(calls) == 1:
                    return [[['已完成。']]]
                return original(url, stop_event=stop_event)
            def cancel():
                if ready.wait(3):
                    stop.set()
            watcher = threading.Thread(target=cancel)
            watcher.start()
            try:
                with patch.object(tr, '_request_json', side_effect=request), \
                     patch.object(tr, '_MAX_BATCH_CUES', 1), patch.object(tr, '_REQUEST_INTERVAL', 0):
                    with self.assertRaises(tr.TranslationCancelled):
                        tr.translate_texts(['First.', 'Sensitive second.'], 'en', 'zh-CN', cache, stop)
                saved = json.loads(cache.read_text(encoding='utf-8'))
                self.assertEqual(len(saved['entries']), 1)
                self.assertEqual(next(iter(saved['entries'].values()))['translation'], '已完成。')
                self.assertTrue(tr._queue.acquire(timeout=0.2))
                tr._queue.release()
                with patch.object(tr, '_request_json', return_value=[[['第二句。']]]) as resumed:
                    self.assertEqual(tr.translate_texts(['First.', 'Sensitive second.'], 'en', 'zh-CN', cache),
                                     ['已完成。', '第二句。'])
                    self.assertEqual(resumed.call_count, 1)
            finally:
                watcher.join(timeout=4)
        self.assert_reaped()
        self.assertNotIn('Sensitive', repr(self.children[0].args))
        self.assertNotIn(url, repr(self.children[0].args))

    def test_offline_child_denies_public_endpoint_and_redirect(self):
        original = local_process.capture_process
        with tempfile.TemporaryDirectory() as directory:
            violation = Path(directory) / 'unexpected-network'
            # Independently block non-loopback DNS before the worker's guard.
            # A future guard regression must fail this test without using WAN.
            program = ("import runpy,socket,ipaddress; from pathlib import Path\n"
                       "original=socket.getaddrinfo\n"
                       "def guarded(host,*a,**k):\n"
                       " try: allowed=ipaddress.ip_address(host).is_loopback\n"
                       " except ValueError: allowed=False\n"
                       f" if not allowed: Path({str(violation)!r}).write_text('blocked'); raise OSError('blocked')\n"
                       " return original(host,*a,**k)\n"
                       "socket.getaddrinfo=guarded\n"
                       "runpy.run_module('subtitle_pipeline.translation_transport',run_name='__main__')")
            def capture(args, **kwargs):
                return original([sys.executable, '-X', 'utf8', '-c', program], **kwargs)
            started = time.monotonic()
            with patch.object(local_process, 'capture_process', side_effect=capture):
                with self.assertRaises(URLError):
                    tr._request_json('https://translate.googleapis.com/?q=PRIVATE')
                with self.server('redirect', location='https://example.invalid/private') as (url, ready):
                    with self.assertRaises(URLError):
                        tr._request_json(url)
                    self.assertTrue(ready.is_set())
            self.assertFalse(violation.exists(), 'worker attempted forbidden DNS/network')
            self.assertLess(time.monotonic() - started, 2)
        self.assert_reaped()

    def test_response_byte_limit_and_invalid_json(self):
        with self.server(body=b'x' * 1_048_577) as (url, _):
            with self.assertRaises(tr._InvalidResponse):
                tr._request_json(url)
        with self.server(body=b'not json') as (url, _):
            with self.assertRaises(tr._InvalidResponse):
                tr._request_json(url)
        self.assert_reaped()

    def test_parent_rejects_malformed_worker_envelopes(self):
        for payload in ('{}', '{', '{"kind":"http","code":true}',
                        '{"kind":"ok","body":"!!!"}',
                        json.dumps({'kind': 'ok', 'body': base64.b64encode(b'{}').decode(), 'extra': 1})):
            with self.subTest(payload=payload), \
                 patch.object(local_process, 'capture_process', return_value=subprocess.CompletedProcess([], 0, payload, '')):
                with self.assertRaises(tr._InvalidResponse):
                    tr._request_json('https://translate.googleapis.com/')


if __name__ == '__main__':
    unittest.main()
