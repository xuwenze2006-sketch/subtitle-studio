"""Bounded loopback transport and lexical static-path validation for Studio."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
import threading


_WINDOWS_DEVICE = re.compile(r'(?i)^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$')


def static_file_path(directory, url_path):
    """Reject Windows network/device syntax before any filesystem resolution.

    The URL has already been decoded once. Keep the resolved containment check
    as well, since a lexically relative path can still traverse a symlink.
    """
    relative = url_path[1:]
    parts = relative.split('/')
    if (not url_path.startswith('/') or url_path.startswith('//')
            or '\\' in relative or ':' in relative
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
            or any(part in ('.', '..') or _WINDOWS_DEVICE.fullmatch(part.split('.')[0].rstrip(' '))
                   for part in parts)):
        raise ValueError('Invalid static path')
    root = Path(directory).resolve()
    resolved = (root / (relative or 'index.html')).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('Static file is unavailable')
    return resolved


class StudioHTTPServer(ThreadingHTTPServer):
    """Bound handlers without making slow clients block the accept loop.

    Socket timeouts limit idle network reads/writes, not controller execution;
    they neither cancel background jobs nor impose a deadline on local work.
    """
    max_active_requests = 32
    request_timeout = 15.0
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(self.max_active_requests)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        try:
            request.settimeout(self.request_timeout)
        except BaseException:
            request.close()
            raise
        return request, address

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            # Do not wait for a slot or write to an untrusted stalled peer on
            # the accept thread. The client may retry once a slot is available.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()
