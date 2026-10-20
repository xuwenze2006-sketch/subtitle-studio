"""One bounded urllib request, isolated from translation state and credentials.

The caller owns the total deadline and cancellation by controlling this worker
process. Request URLs arrive through stdin and never appear in its arguments or
error envelopes. This module intentionally imports only the standard library.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import string
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_REQUEST_BYTES = 32768
MAX_RESPONSE_BYTES = 1048576


class _TooLarge(ValueError):
    pass


def _guard_controls(value: str, *, header=False) -> None:
    if (not isinstance(value, str) or not value
            or any(ord(character) < 32 or ord(character) == 127
                   or (not header and 128 <= ord(character) <= 159) for character in value)):
        raise ValueError('invalid URL')


def _guard_url(url: str, *, offline: bool) -> None:
    _guard_controls(url)
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('invalid URL')
    # Accessing port also rejects malformed or out-of-range ports before I/O.
    parsed.port
    if offline:
        # A hostname such as localhost could resolve through DNS or a changed
        # hosts file. Accept only explicit loopback address literals.
        if '%' in parsed.hostname or not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError('offline address is not loopback')


class _RedirectHandler(HTTPRedirectHandler):
    """Guard every destination and close discarded redirect bodies unread."""

    def __init__(self, *, offline: bool):
        self.offline = offline

    def http_error_302(self, request, response, code, message, headers):
        destination = headers.get('Location') or headers.get('URI')
        if destination is None:
            return None  # urllib raises HTTPError; main closes its body unread.
        try:
            # urljoin strips some control characters, so inspect the original
            # Location before resolving relative redirects.
            # Raw HTTP headers may contain obs-text (including UTF-8 bytes
            # decoded as Latin1). Quoting below makes those bytes ASCII.
            _guard_controls(destination, header=True)
            parts = urlsplit(destination)
            if parts.netloc and not parts.path:
                destination = parts._replace(path='/').geturl()
            # Match urllib: headers are decoded from ISO-8859-1, so recover
            # their original bytes before quoting spaces/non-ASCII for I/O.
            destination = quote(destination, encoding='iso-8859-1', safe=string.punctuation)
            destination = urljoin(request.full_url, destination)
            _guard_url(destination, offline=self.offline)
            redirected = self.redirect_request(request, response, code, message, headers, destination)
            if redirected is None:
                return None
            visited = getattr(request, 'redirect_dict', {})
            if (visited.get(destination, 0) >= self.max_repeats
                    or len(visited) >= self.max_redirections):
                raise URLError('redirect limit exceeded')
            redirected.redirect_dict = visited
            visited[destination] = visited.get(destination, 0) + 1
        finally:
            response.close()
        return self.parent.open(redirected, timeout=request.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _read_body(response) -> bytes:
    body = bytearray()
    while len(body) <= MAX_RESPONSE_BYTES:
        chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            # HTTPResponse.read(size) can return EOF without raising when a
            # declared Content-Length has not been satisfied.
            remaining = getattr(response, 'length', None)
            if remaining is not None and remaining != 0:
                raise OSError('truncated response')
            return bytes(body)
        body.extend(chunk)
    raise _TooLarge()


def _perform_request(url: str, *, offline: bool) -> bytes:
    _guard_url(url, offline=offline)
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/139.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    })
    handlers = [_RedirectHandler(offline=offline)]
    if offline:
        handlers.insert(0, ProxyHandler({}))
    opener = build_opener(*handlers)
    response = opener.open(request, timeout=15)
    with response:
        return _read_body(response)


def main(input_stream=None, output_stream=None) -> int:
    """Read one UTF-8 request and emit one ASCII JSON result without diagnostics."""
    if input_stream is None:
        input_stream = sys.stdin.buffer
    if output_stream is None:
        output_stream = sys.stdout
    try:
        raw = input_stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise _TooLarge()
        payload = json.loads(raw.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
            raise ValueError('invalid request')
        body = _perform_request(payload['url'], offline=os.environ.get('SUBTITLE_STUDIO_OFFLINE') == '1')
        result = {'kind': 'ok', 'body': base64.b64encode(body).decode('ascii')}
    except _TooLarge:
        result = {'kind': 'too_large'}
    except HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        result = {'kind': 'http', 'code': error.code}
    except Exception:
        # Exception strings and urllib URLs can contain the original subtitle.
        # Keep all failure diagnostics out of stdout/stderr and command lines.
        result = {'kind': 'network'}
    output_stream.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')) + '\n')
    output_stream.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
