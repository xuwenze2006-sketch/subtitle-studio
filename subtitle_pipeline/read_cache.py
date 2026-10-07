"""Bounded, content-checked SRT parsing cache for Studio read requests.

This is only a performance cache, never processing evidence. Every lookup reads
and hashes the file, including edits that preserve its size and modification
time. Read/parse failures propagate instead of returning an older good result.
"""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading
from typing import Callable

from .subtitles import Cue


@dataclass(frozen=True)
class _Entry:
    digest: bytes
    parser: Callable[[str], list[Cue]]
    cues: tuple[Cue, ...]
    source_bytes: int


class SrtReadCache:
    def __init__(self, *, max_entries=12, max_source_bytes=8 * 1024 * 1024,
                 max_cues=50_000):
        for limit in (max_entries, max_source_bytes, max_cues):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError('Cache limits must be nonnegative integers')
        self.max_entries = max_entries
        self.max_source_bytes = max_source_bytes
        self.max_cues = max_cues
        self._entries = OrderedDict()
        self._source_bytes = 0
        self._cue_count = 0
        self._lock = threading.Lock()

    @staticmethod
    def _key(path):
        return os.path.normcase(str(Path(path).resolve()))

    def _remove(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._source_bytes -= entry.source_bytes
            self._cue_count -= len(entry.cues)

    def invalidate(self, path):
        key = self._key(path)
        with self._lock:
            self._remove(key)

    def read(self, path, parser):
        path = Path(path)
        key = self._key(path)
        try:
            # Disk I/O never holds the cache lock or the Studio controller lock.
            data = path.read_bytes()
        except OSError:
            with self._lock:
                self._remove(key)
            raise
        digest = hashlib.sha256(data).digest()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.digest == digest and entry.parser is parser:
                self._entries.move_to_end(key)
                return list(entry.cues)
            self._remove(key)
            # Parsing inside this small cache lock coalesces concurrent reads.
            # Immutable Cue objects can be shared; callers own the returned list.
            cues = tuple(parser(data.decode('utf-8-sig')))
            size = len(data)
            if (self.max_entries and size <= self.max_source_bytes
                    and len(cues) <= self.max_cues):
                while self._entries and (len(self._entries) >= self.max_entries
                        or self._source_bytes + size > self.max_source_bytes
                        or self._cue_count + len(cues) > self.max_cues):
                    self._remove(next(iter(self._entries)))
                self._entries[key] = _Entry(digest, parser, cues, size)
                self._source_bytes += size
                self._cue_count += len(cues)
            return list(cues)
