import base64
import importlib
import io
import json
import os
import subprocess
import sys
import unittest
from email.message import Message
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.response import addinfourl
from unittest.mock import patch


class Response(io.BytesIO):
    def __init__(self, body, *, chunk_size=None):
        super().__init__(body)
        self.chunk_size = chunk_size
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        return super().read(size)


class TranslationTransportWorkerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('subtitle_pipeline.translation_transport'),
                             'bounded translation worker is not implemented')
        self.module = importlib.import_module('subtitle_pipeline.translation_transport')

    def run_worker(self, raw=None, *, url='https://example.invalid/?q=private', offline=False):
        if raw is None:
            raw = json.dumps({'url': url}, ensure_ascii=False).encode('utf-8')
        output = io.StringIO()
        diagnostics = io.StringIO()
        with patch.dict(os.environ, {'SUBTITLE_STUDIO_OFFLINE': '1' if offline else '0'}), \
                patch.object(sys, 'stderr', diagnostics):
            self.assertEqual(self.module.main(io.BytesIO(raw), output), 0)
        self.assertTrue(output.getvalue().isascii())
        self.assertEqual(diagnostics.getvalue(), '')
        return json.loads(output.getvalue())

    def test_success_transfers_original_bytes_and_request_headers(self):
        response = Response(b'\xff\x00private')
        requests = []

        def open_request(request, data=None, timeout=15):
            requests.append((request, timeout))
            return response

        with patch('urllib.request.OpenerDirector.open', side_effect=open_request):
            result = self.run_worker()
        self.assertEqual(result, {'kind': 'ok', 'body': '/wBwcml2YXRl'})
        self.assertTrue(response.closed)
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, 'https://example.invalid/?q=private')
        self.assertEqual(timeout, 15)
        self.assertEqual(request.get_header('Accept'), 'application/json')
        self.assertIn('Chrome/139.0.0.0', request.get_header('User-agent'))

    def test_short_reads_are_accumulated_until_eof(self):
        response = Response(b'abcdef', chunk_size=1)
        with patch('urllib.request.OpenerDirector.open', return_value=response):
            self.assertEqual(self.run_worker(), {'kind': 'ok', 'body': 'YWJjZGVm'})
        self.assertGreater(len(response.read_sizes), 6)
        self.assertTrue(all(0 < size <= 1_048_577 for size in response.read_sizes))

    def test_response_exact_limit_is_accepted_and_one_extra_byte_is_rejected(self):
        for count, expected_kind in ((1_048_576, 'ok'), (1_048_577, 'too_large')):
            with self.subTest(count=count):
                response = Response(b'x' * count, chunk_size=16381)
                with patch('urllib.request.OpenerDirector.open', return_value=response):
                    result = self.run_worker()
                self.assertEqual(result['kind'], expected_kind)
                if expected_kind == 'ok':
                    self.assertEqual(len(base64.b64decode(result['body'], validate=True)), count)
                else:
                    self.assertEqual(result, {'kind': 'too_large'})
                self.assertTrue(response.closed)
                self.assertTrue(all(0 < size <= 1_048_577 for size in response.read_sizes))

    def test_request_exact_limit_is_accepted_but_one_extra_byte_never_opens_network(self):
        prefix = b'{"url":"https://example.invalid/?q='
        suffix = b'"}'
        raw = prefix + b'x' * (32768 - len(prefix) - len(suffix)) + suffix
        response = Response(b'[]')
        with patch('urllib.request.OpenerDirector.open', return_value=response) as opener:
            self.assertEqual(self.run_worker(raw), {'kind': 'ok', 'body': 'W10='})
            self.assertEqual(opener.call_count, 1)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network attempted')) as opener:
            self.assertEqual(self.run_worker(raw + b' '), {'kind': 'too_large'})
        opener.assert_not_called()

    def test_request_reads_only_limit_plus_one_bytes(self):
        class ObservedInput(io.BytesIO):
            def read(self, size=-1):
                self.size = size
                return super().read(size)

        source = ObservedInput(b'x' * 40000)
        output = io.StringIO()
        self.assertEqual(self.module.main(source, output), 0)
        self.assertEqual(source.size, 32769)
        self.assertEqual(json.loads(output.getvalue()), {'kind': 'too_large'})
        self.assertEqual(source.tell(), 32769)

    def test_invalid_json_and_url_are_sanitized_before_network(self):
        malformed = [b'', b'\xff', b'{', b'[]', b'null', b'{"url":1}', b'{"url":null}',
                     b'{}', b'{"url":""}']
        urls = ['file:///private', 'ftp://example.invalid/private', 'data:text/plain,private',
                'http://user:private@127.0.0.1/', 'http://@127.0.0.1/',
                'http://example.invalid:bad/', 'http:///private',
                'http://example.invalid/\nprivate', 'http://example.invalid/\x00private',
                'http://example.invalid/\x7fprivate']
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network attempted')) as opener:
            for raw in malformed:
                with self.subTest(raw=raw):
                    self.assertEqual(self.run_worker(raw), {'kind': 'network'})
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(self.run_worker(url=url), {'kind': 'network'})
        opener.assert_not_called()

    def test_http_error_is_closed_without_reading_or_disclosing_its_body(self):
        body = Response(b'private HTTP error')
        body.read = lambda size=-1: self.fail('HTTP error body must not be read')
        error = HTTPError('https://example.invalid/?q=private', 403, 'private', {}, body)
        with patch('urllib.request.OpenerDirector.open', side_effect=error):
            self.assertEqual(self.run_worker(), {'kind': 'http', 'code': 403})
        self.assertTrue(body.closed)

    def test_network_and_read_failures_do_not_disclose_original_exception(self):
        for error in (URLError('private URL and subtitle'), TimeoutError('private'),
                      OSError('private'), ValueError('private')):
            with self.subTest(error=type(error).__name__), \
                    patch('urllib.request.OpenerDirector.open', side_effect=error):
                self.assertEqual(self.run_worker(), {'kind': 'network'})
        response = Response(b'private')
        response.read = lambda size=-1: (_ for _ in ()).throw(OSError('private read'))
        with patch('urllib.request.OpenerDirector.open', return_value=response):
            self.assertEqual(self.run_worker(), {'kind': 'network'})
        self.assertTrue(response.closed)

    def fake_http_response(self, url, *, code=200, location=None):
        headers = Message()
        if location is not None:
            headers['Location'] = location
        body = Response(b'[]' if code == 200 else b'private redirect body')
        response = addinfourl(body, headers, url, code=code)
        response.msg = 'OK' if code == 200 else 'Found'
        return response, body

    def test_offline_rejects_dns_names_external_ips_and_userinfo_before_dns(self):
        urls = ['http://localhost/private', 'http://example.invalid/private', 'https://8.8.8.8/',
                'http://127.1/', 'http://2130706433/', 'http://user:private@127.0.0.1/',
                'http://[::ffff:8.8.8.8]/']
        with patch('socket.getaddrinfo', side_effect=AssertionError('DNS attempted')) as resolve, \
                patch('urllib.request.HTTPHandler.http_open', side_effect=AssertionError('HTTP attempted')) as open_http:
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(self.run_worker(url=url, offline=True), {'kind': 'network'})
        resolve.assert_not_called()
        open_http.assert_not_called()

    def test_offline_disables_environment_proxies_for_ipv4_and_ipv6_literals(self):
        for url, expected_host in [('http://127.0.0.1:4321/private', '127.0.0.1:4321'),
                                   ('http://[::1]:4321/private', '[::1]:4321')]:
            seen = []

            def http_open(handler, request):
                seen.append((request.host, request.has_proxy()))
                return self.fake_http_response(request.full_url)[0]

            with self.subTest(url=url), patch.dict(os.environ, {
                    'http_proxy': 'http://proxy.invalid:8080', 'HTTP_PROXY': 'http://proxy.invalid:8080',
                    'no_proxy': '', 'NO_PROXY': ''}), \
                    patch('urllib.request.HTTPHandler.http_open', new=http_open), \
                    patch('socket.getaddrinfo', side_effect=AssertionError('DNS attempted')):
                self.assertEqual(self.run_worker(url=url, offline=True), {'kind': 'ok', 'body': 'W10='})
            self.assertEqual(seen, [(expected_host, False)])

    def test_offline_checks_each_redirect_and_closes_rejected_body_without_reading(self):
        targets = ['http://example.invalid/private', 'http://8.8.8.8/private',
                   'http://user:private@127.0.0.1/private', 'ftp://127.0.0.1/private',
                   'http://127.0.0.1/\nprivate']
        for target in targets:
            response, body = self.fake_http_response('http://127.0.0.1/start', code=302, location=target)
            body.read = lambda size=-1: self.fail('redirect error body must not be read')
            seen = []

            def http_open(handler, request):
                seen.append(request.full_url)
                return response

            with self.subTest(target=target), patch('urllib.request.HTTPHandler.http_open', new=http_open), \
                    patch('socket.getaddrinfo', side_effect=AssertionError('DNS attempted')):
                self.assertEqual(self.run_worker(url='http://127.0.0.1/start', offline=True),
                                 {'kind': 'network'})
            self.assertEqual(seen, ['http://127.0.0.1/start'])
            self.assertTrue(body.closed)

    def test_offline_follows_relative_loopback_redirect_without_reading_discarded_body(self):
        initial, initial_body = self.fake_http_response('http://127.0.0.1/start', code=302,
                                                       location='/finish')
        initial_body.read = lambda size=-1: self.fail('discarded redirect body must not be read')
        final, final_body = self.fake_http_response('http://127.0.0.1/finish')
        seen = []

        def http_open(handler, request):
            seen.append(request.full_url)
            return initial if len(seen) == 1 else final

        with patch('urllib.request.HTTPHandler.http_open', new=http_open), \
                patch('socket.getaddrinfo', side_effect=AssertionError('DNS attempted')):
            self.assertEqual(self.run_worker(url='http://127.0.0.1/start', offline=True),
                             {'kind': 'ok', 'body': 'W10='})
        self.assertEqual(seen, ['http://127.0.0.1/start', 'http://127.0.0.1/finish'])
        self.assertTrue(initial_body.closed)
        self.assertTrue(final_body.closed)

    def test_module_cli_rejects_forbidden_url_with_only_sanitized_ascii_json(self):
        environment = dict(os.environ, SUBTITLE_STUDIO_OFFLINE='1')
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m',
                                 'subtitle_pipeline.translation_transport'],
                                input=b'{"url":"https://example.invalid/?q=private"}',
                                capture_output=True, timeout=5, env=environment)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout.decode('ascii')), {'kind': 'network'})
        self.assertEqual(result.stderr, b'')

    def test_normal_mode_keeps_default_environment_proxy_routing(self):
        seen = []

        def http_open(handler, request):
            seen.append((request.host, request.has_proxy()))
            return self.fake_http_response(request.full_url)[0]

        with patch.dict(os.environ, {
                'http_proxy': 'http://proxy.invalid:8080', 'HTTP_PROXY': 'http://proxy.invalid:8080',
                'no_proxy': '', 'NO_PROXY': ''}), patch('urllib.request._opener', None), \
                patch('urllib.request.HTTPHandler.http_open', new=http_open), \
                patch('socket.getaddrinfo', side_effect=AssertionError('DNS attempted')):
            self.assertEqual(self.run_worker(url='http://example.invalid/private'),
                             {'kind': 'ok', 'body': 'W10='})
        self.assertEqual(seen, [('proxy.invalid:8080', True)])

    def test_normal_mode_closes_redirect_body_unread_and_follows_relative_location(self):
        initial, initial_body = self.fake_http_response('http://example.invalid/start', code=302,
                                                       location='/finish')
        initial_body.read = lambda size=-1: self.fail('discarded redirect body must not be read')
        final, final_body = self.fake_http_response('http://example.invalid/finish')
        seen = []

        def http_open(handler, request):
            seen.append(request.full_url)
            return initial if len(seen) == 1 else final

        with patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}), \
                patch('urllib.request._opener', None), \
                patch('urllib.request.HTTPHandler.http_open', new=http_open), \
                patch('socket.getaddrinfo', side_effect=AssertionError('DNS attempted')):
            self.assertEqual(self.run_worker(url='http://example.invalid/start'),
                             {'kind': 'ok', 'body': 'W10='})
        self.assertEqual(seen, ['http://example.invalid/start', 'http://example.invalid/finish'])
        self.assertTrue(initial_body.closed)
        self.assertTrue(final_body.closed)

    def test_declared_content_length_truncation_rejects_even_valid_json_prefix(self):
        class SocketFixture:
            def makefile(self, mode):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n[]')

        response = HTTPResponse(SocketFixture())
        response.begin()
        with patch('urllib.request.OpenerDirector.open', return_value=response):
            self.assertEqual(self.run_worker(), {'kind': 'network'})
        self.assertTrue(response.isclosed())

    def test_redirect_preserves_urllib_location_byte_quoting(self):
        for location, expected in (('/caf\xe9 and tea', '/caf%E9%20and%20tea'),
                                   ('/中文'.encode().decode('iso-8859-1'), '/%E4%B8%AD%E6%96%87'),
                                   ('http://127.0.0.1', '/')):
            with self.subTest(location=location):
                initial, body = self.fake_http_response('http://127.0.0.1/start', code=302,
                                                        location=location)
                final, _ = self.fake_http_response('http://127.0.0.1' + expected)
                seen = []
                def http_open(handler, request):
                    seen.append(request.full_url)
                    return initial if len(seen) == 1 else final
                with patch('urllib.request.HTTPHandler.http_open', new=http_open):
                    self.assertEqual(self.run_worker(url='http://127.0.0.1/start', offline=True),
                                     {'kind': 'ok', 'body': 'W10='})
                self.assertEqual(seen, ['http://127.0.0.1/start', 'http://127.0.0.1' + expected])
                self.assertTrue(body.closed)


if __name__ == '__main__':
    unittest.main()
