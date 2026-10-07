"""Bounded reuse of parsed and validated machine cues, never a review view.

The caller must read current bytes, construct their complete content binding,
and check any manual record binding before lookup. Manual patches, warnings and
revision/CAS checks remain outside this cache and run on every read.
"""

from collections import OrderedDict
from dataclasses import dataclass
import threading
from types import MappingProxyType


def _freeze(value):
    """Snapshot the JSON binding without retaining mutable caller containers."""
    if isinstance(value, dict):
        return (dict, tuple((key, _freeze(item)) for key, item in sorted(value.items())))
    if isinstance(value, list):
        return (list, tuple(_freeze(item) for item in value))
    return (type(value), value)


@dataclass(frozen=True)
class _Entry:
    cues: tuple
    source_bytes: int
    # Keep callables alive: an id in the key must not be recycled after patching.
    parser: object
    validator: object
    builder: object


class ReviewBaseCache:
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

    def read(self, binding, content, *, parser, validator, builder):
        key = (_freeze(binding), id(parser), id(validator), id(builder))
        size = sum(len(raw) for raw in content)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                cached = entry.cues
            else:
                # Coalesce simultaneous misses. Actual file I/O and all mutable
                # review overlay work take place outside this cache lock.
                base = builder(content)
                validator(base, binding['duration_ms'])
                if (self.max_entries and size <= self.max_source_bytes
                        and len(base) <= self.max_cues):
                    # Base cue fields are scalars except warnings. Store those
                    # as a tuple and expose no mutable cached containers.
                    cached = tuple(MappingProxyType(dict(cue, warnings=tuple(cue['warnings'])))
                                   for cue in base)
                    while self._entries and (len(self._entries) >= self.max_entries
                            or self._source_bytes + size > self.max_source_bytes
                            or self._cue_count + len(cached) > self.max_cues):
                        _, removed = self._entries.popitem(last=False)
                        self._source_bytes -= removed.source_bytes
                        self._cue_count -= len(removed.cues)
                    self._entries[key] = _Entry(cached, size, parser, validator, builder)
                    self._source_bytes += size
                    self._cue_count += len(cached)
                return base
        # Callers may edit/pop dictionaries or append warnings. Copy outside the
        # lock so independent warm reads do not serialize this allocation work.
        return [dict(cue, warnings=list(cue['warnings'])) for cue in cached]
