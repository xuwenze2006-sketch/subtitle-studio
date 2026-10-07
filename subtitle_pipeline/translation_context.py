"""Frozen, source-bound context at audio chunk boundaries (no network I/O)."""
from copy import deepcopy
import re

from .integrity import fingerprint, HumanEditConflict

VERSION = 1
_MAX_GAP_MS = 15000
_MAX_TEXT_CHARACTERS = 1200
_FIELDS = {'version', 'chunk_index', 'source_hash', 'first_start_ms',
           'previous_index', 'previous_source_hash', 'before', 'sha256'}


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def validate_snapshot(snapshot, source_hash=None):
    try:
        if (not isinstance(snapshot, dict) or set(snapshot) != _FIELDS
                or type(snapshot['version']) is not int or snapshot['version'] != VERSION
                or type(snapshot['chunk_index']) is not int or snapshot['chunk_index'] < 0
                or not _hash(snapshot['source_hash'])
                or source_hash is not None and snapshot['source_hash'] != source_hash
                or not _hash(snapshot['sha256'])
                or snapshot['sha256'] != fingerprint({k: v for k, v in snapshot.items() if k != 'sha256'})):
            raise ValueError()
        first, previous, rows = snapshot['first_start_ms'], snapshot['previous_index'], snapshot['before']
        if first is not None and (type(first) is not int or first < 0): raise ValueError()
        if previous is None:
            if snapshot['previous_source_hash'] is not None or rows: raise ValueError()
        elif (type(previous) is not int or previous < 0 or previous != snapshot['chunk_index'] - 1
              or not _hash(snapshot['previous_source_hash'])): raise ValueError()
        if not isinstance(rows, list) or len(rows) > 2: raise ValueError()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'index', 'start_ms', 'end_ms', 'text'}
                    or any(type(row[k]) is not int for k in ('index', 'start_ms', 'end_ms'))
                    or row['index'] < 0 or not 0 <= row['start_ms'] < row['end_ms']
                    or first is None or not 0 <= first - row['end_ms'] <= _MAX_GAP_MS
                    or not isinstance(row['text'], str) or not row['text'].strip()):
                raise ValueError()
            row['text'].encode('utf-8')
        if sum(len(row['text']) for row in rows) > _MAX_TEXT_CHARACTERS: raise ValueError()
        if rows != sorted(rows, key=lambda row: (row['start_ms'], row['end_ms'], row['index'])):
            raise ValueError()
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise HumanEditConflict('跨段翻译上下文缺失或损坏，请恢复原记录；未重新提交翻译。') from None


def build_snapshot(chunk, source_hash, cues, previous_chunk=None, previous_hash=None, previous_cues=()):
    first = min((cue.start_ms + chunk.audio_start_ms for cue in cues), default=None)
    rows = []
    if previous_chunk is not None and first is not None:
        for index, cue in enumerate(previous_cues):
            start, end = cue.start_ms + previous_chunk.audio_start_ms, cue.end_ms + previous_chunk.audio_start_ms
            if 0 <= first - end <= _MAX_GAP_MS:
                rows.append({'index': index, 'start_ms': start, 'end_ms': end, 'text': cue.text})
    rows = sorted(rows, key=lambda row: (row['start_ms'], row['end_ms'], row['index']))[-2:]
    while sum(len(row['text']) for row in rows) > _MAX_TEXT_CHARACTERS:
        rows.pop(0)
    payload = {'version': VERSION, 'chunk_index': chunk.index, 'source_hash': source_hash,
               'first_start_ms': first,
               'previous_index': previous_chunk.index if previous_chunk is not None else None,
               'previous_source_hash': previous_hash if previous_chunk is not None else None, 'before': rows}
    snapshot = {**payload, 'sha256': fingerprint(payload)}
    validate_snapshot(snapshot, source_hash)
    return snapshot


def existing_snapshot(part, source_hash):
    if 'translation_contexts' not in part and 'translation_context_bindings' not in part:
        return None
    contexts, bindings = part.get('translation_contexts'), part.get('translation_context_bindings')
    if (not isinstance(contexts, dict) or not isinstance(bindings, dict)
            or set(contexts) != set(bindings) or not contexts):
        raise HumanEditConflict('跨段翻译上下文历史不完整，请恢复原记录；未重新提交翻译。')
    for digest, snapshot in contexts.items():
        validate_snapshot(snapshot, digest)
        if snapshot['sha256'] != bindings[digest]:
            raise HumanEditConflict('跨段翻译上下文校验不一致；未重新提交翻译。')
    return deepcopy(contexts.get(source_hash))


def remember_snapshot(part, snapshot):
    validate_snapshot(snapshot)
    previous = existing_snapshot(part, snapshot['source_hash'])
    if previous is not None and previous != snapshot:
        raise HumanEditConflict('本段已有不同的翻译上下文，不能替换已冻结的请求。')
    part.setdefault('translation_contexts', {})[snapshot['source_hash']] = deepcopy(snapshot)
    part.setdefault('translation_context_bindings', {})[snapshot['source_hash']] = snapshot['sha256']


def source_texts(snapshot):
    validate_snapshot(snapshot)
    return [row['text'] for row in snapshot['before']]
