"""Ordered, per-stream proof for exports that preserve every source audio track.

Media reads are supplied by the caller so cancellation/process ownership remain
in the existing export pipeline. This module does not select ASR input audio.
"""
from pathlib import Path
import re

from .runner import Cancelled


_METADATA = {'codec_name', 'sample_rate', 'channels', 'channel_layout',
             'language', 'default', 'forced'}
_RECORD = _METADATA | {'sha256'}
_DIGEST = re.compile(r'SHA256=([0-9a-fA-F]{64})')
_STORED_HASH = re.compile(r'[0-9a-f]{64}')


def _text(value, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError('音轨元数据缺失或无效')
    return value.strip().lower()


def _descriptor(stream):
    rate = stream.get('sample_rate')
    if isinstance(rate, str) and re.fullmatch(r'[0-9]+', rate):
        rate = int(rate)
    channels = stream.get('channels')
    if type(rate) is not int or rate <= 0 or type(channels) is not int or channels <= 0:
        raise ValueError('音轨采样率或声道数缺失或无效')
    tags = stream.get('tags', {})
    disposition = stream.get('disposition', {})
    if not isinstance(tags, dict) or not isinstance(disposition, dict):
        raise ValueError('音轨语言或播放标记无效')
    flags = {name: disposition.get(name, 0) for name in ('default', 'forced')}
    if any(type(value) is not int or value not in (0, 1) for value in flags.values()):
        raise ValueError('音轨默认或强制播放标记无效')
    language = tags.get('language')
    language = 'und' if language is None else (_text(language, empty=True) or 'und')
    return {'codec_name': _text(stream.get('codec_name')), 'sample_rate': rate,
            'channels': channels, 'channel_layout': _text(stream.get('channel_layout', ''), empty=True),
            'language': language, **flags}


def audio_track_descriptors(info):
    """Return canonical audio metadata in source order, without container indexes.

Missing language becomes ``und``; absent layout is the empty string, and absent
default/forced flags are zero. Required codec/rate/channel data must be valid.
An absent or malformed audio inventory raises ValueError rather than proving an
empty export correct.
"""
    if not isinstance(info, dict) or not isinstance(info.get('streams'), list):
        raise ValueError('音轨信息缺失或无效')
    result = []
    for stream in info['streams']:
        if not isinstance(stream, dict) or not isinstance(stream.get('codec_type'), str):
            raise ValueError('音轨信息缺失或无效')
        if stream['codec_type'] == 'audio':
            result.append(_descriptor(stream))
    if not result:
        raise ValueError('未找到可校验的音轨')
    return result


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise Cancelled('已停止音轨完整性校验')


def _normalize_digest(value):
    match = _DIGEST.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('音轨摘要格式无效')
    return match.group(1).lower()


def parse_audio_stream_hashes(text, expected_count):
    """Parse exactly the independently probed audio inventory, in output order."""
    if type(expected_count) is not int or expected_count <= 0 or not isinstance(text, str):
        raise ValueError('音轨摘要数量或输出格式无效')
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'(0|[1-9][0-9]*),a,SHA256=([0-9a-fA-F]{64})', line.strip())
        if match is None or int(match.group(1)) != len(records):
            raise ValueError('音轨摘要缺失、乱序或格式无效')
        records.append('SHA256=' + match.group(2).lower())
    if len(records) != expected_count:
        raise ValueError('音轨摘要数量与媒体音轨不一致')
    return records


def _read_digest(path, stop, index, digest):
    _check_stop(stop)
    value = digest(path, stop, index=index)
    _check_stop(stop)
    return _normalize_digest(value)


def _read_bulk_digests(path, stop, count, digests):
    _check_stop(stop)
    values = digests(path, stop, expected_count=count)
    _check_stop(stop)
    if not isinstance(values, list) or len(values) != count:
        raise ValueError('音轨摘要数量与媒体音轨不一致')
    return [_normalize_digest(value) for value in values]


def _default_policy(source_tracks, output_tracks):
    if source_tracks == output_tracks:
        return 'preserved'
    # MP4 may enable the first audio track when the source leaves every track
    # nondefault. Preserve both raw inventories; only this exact change is
    # permitted, with all other fields compared at the same audio index.
    if (all(track['default'] == 0 for track in source_tracks)
            and output_tracks[0]['default'] == 1
            and all(track['default'] == 0 for track in output_tracks[1:])
            and all(all(before[key] == after[key] for key in _METADATA - {'default'})
                    for before, after in zip(source_tracks, output_tracks))):
        return 'mp4_first_when_unspecified'
    return None


def verify_audio_tracks(source_info, output_info, source, output, stop, digest, *, digests=None):
    """Compare every ordered stream and return versioned, independently owned proof.

``digest(path, stop, index=i)`` must return canonical ``SHA256=<64 hex>``.
Digest I/O/cancellation errors propagate unchanged. No success is returned if
the stop signal becomes set during the final digest read. Version 2 records
raw source/output flags and explicitly labels MP4's first-default normalization
when every source track is nondefault; all other metadata remains exact.
Optional ``digests(path, stop, expected_count=N)`` reads all tracks once per
file; it must supply the same ordered canonical strings. Metadata is checked
before either file is scanned, and the stored proof format does not change.
"""
    _check_stop(stop)
    source_tracks = audio_track_descriptors(source_info)
    output_tracks = audio_track_descriptors(output_info)
    if len(source_tracks) != len(output_tracks):
        raise ValueError('导出音轨数量与源视频不一致')
    policy = _default_policy(source_tracks, output_tracks)
    if (policy is None or (policy == 'mp4_first_when_unspecified'
                          and Path(output).suffix.lower() != '.mp4')):
        raise ValueError('导出音轨顺序或元数据与源视频不一致')
    if digests is not None:
        source_hashes = _read_bulk_digests(source, stop, len(source_tracks), digests)
        output_hashes = _read_bulk_digests(output, stop, len(output_tracks), digests)
    for index, (before, after) in enumerate(zip(source_tracks, output_tracks)):
        _check_stop(stop)
        before['sha256'] = (_read_digest(source, stop, index, digest) if digests is None
                            else source_hashes[index])
        after['sha256'] = (_read_digest(output, stop, index, digest) if digests is None
                           else output_hashes[index])
        if before['sha256'] != after['sha256']:
            raise ValueError(f'导出第 {index + 1} 条音轨内容与源视频不一致')
    _check_stop(stop)
    return {'version': 2, 'source': source_tracks, 'output': output_tracks,
            'default_policy': policy}


def _valid_record(record):
    if not isinstance(record, dict) or set(record) != _RECORD:
        return False
    if any(type(record[name]) is not int for name in ('sample_rate', 'channels', 'default', 'forced')):
        return False
    if not isinstance(record['sha256'], str) or _STORED_HASH.fullmatch(record['sha256']) is None:
        return False
    descriptor = _descriptor({'codec_name': record['codec_name'], 'sample_rate': record['sample_rate'],
        'channels': record['channels'], 'channel_layout': record['channel_layout'],
        'tags': {'language': record['language']},
        'disposition': {'default': record['default'], 'forced': record['forced']}})
    return descriptor == {key: record[key] for key in _METADATA}


def audio_evidence_matches(evidence, source_info):
    """Check strict stored proof against current source metadata, without media I/O.

The caller must also bind this proof to the full source/output file hashes.
Metadata and stored hashes alone cannot establish that files remain unchanged.
"""
    try:
        if (not isinstance(evidence, dict) or type(evidence.get('version')) is not int
                or evidence['version'] not in (1, 2)):
            return False
        keys = {'version', 'source', 'output'}
        if evidence['version'] == 2:
            keys.add('default_policy')
        if set(evidence) != keys:
            return False
        current = audio_track_descriptors(source_info)
        for side in ('source', 'output'):
            records = evidence[side]
            if (not isinstance(records, list) or len(records) != len(current)
                    or not all(_valid_record(record) for record in records)):
                return False
        source_tracks = [{key: record[key] for key in _METADATA} for record in evidence['source']]
        if source_tracks != current:
            return False
        if evidence['version'] == 1:
            # Existing version-1 evidence proves exact preservation only.
            return evidence['source'] == evidence['output']
        if any(before['sha256'] != after['sha256']
               for before, after in zip(evidence['source'], evidence['output'])):
            return False
        output_tracks = [{key: record[key] for key in _METADATA} for record in evidence['output']]
        policy = evidence['default_policy']
        return (isinstance(policy, str) and policy in ('preserved', 'mp4_first_when_unspecified')
                and policy == _default_policy(source_tracks, output_tracks))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
