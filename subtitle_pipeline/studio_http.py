"""Bounded loopback transport and lexical static-path validation for Studio."""
from http.server import ThreadingHTTPServer
import io
import os
from pathlib import Path
import re
import socket
import threading
import time


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


class _DeadlineSocketReader(io.RawIOBase):
    """Recompute a phase's remaining time before every underlying recv."""

    def __init__(self, connection):
        self.connection = connection
        self.idle_timeout = connection.gettimeout()
        self.deadline = None
        self.stream = connection.makefile('rb', 0)

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTP request read deadline exceeded')
            timeout = remaining if self.idle_timeout is None else min(self.idle_timeout, remaining)
            self.connection.settimeout(timeout)
        return self.stream.readinto(buffer)

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class RequestReader(io.BufferedReader):
    """Bound header/body reads without timers or a controller execution limit."""

    def __init__(self, connection, buffer_size=-1):
        super().__init__(_DeadlineSocketReader(connection),
                         buffer_size if buffer_size > 0 else io.DEFAULT_BUFFER_SIZE)

    def begin_read(self, timeout):
        self.raw.deadline = time.monotonic() + timeout

    def end_read(self):
        self.raw.deadline = None
        # Reads can leave a short remaining timeout on the socket. Restore the
        # original idle allowance before any response or media write.
        self.raw.connection.settimeout(self.raw.idle_timeout)


class StudioHTTPServer(ThreadingHTTPServer):
    """Bound handlers without making slow clients block the accept loop.

    Header/body phases have absolute read deadlines; writes have idle socket
    timeouts. Neither cancels background jobs or limits controller execution.
    """
    max_active_requests = 32
    request_timeout = 15.0
    request_queue_size = 32
    # Windows SO_REUSEADDR permits another process to share the same browser
    # origin. An origin containing review drafts must have one exclusive host.
    allow_reuse_address = os.name != 'nt'

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(self.max_active_requests)
        super().__init__(*args, **kwargs)

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

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
