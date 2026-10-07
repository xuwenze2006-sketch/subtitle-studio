"""Content-bound manual subtitle edits, separate from machine generation.

Callers MUST hold ``runner.ProjectLock(folder)`` around save/materialize and any
coordinated load/approval operation. The smaller stable review lock additionally
serializes standalone review writers; it does not lock the generation pipeline.
Reading never creates files. Saving changes only one sparse JSON record, and
explicit materialization publishes an immutable, coherent three-track snapshot.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata

from .integrity import valid_translation_content
from .languages import output_names, validate_languages
from .review_base_cache import ReviewBaseCache
from .subtitles import Cue, parse_srt, render_srt


class ReviewConflict(ValueError):
    """A base track, review revision, or immutable snapshot changed externally."""


_CONTENT_FIELDS = ('start_ms', 'end_ms', 'source_text', 'target_text')
_PATCH_FIELDS = frozenset((*_CONTENT_FIELDS, 'review_status', 'note', 'translation_stale'))
_STATUSES = frozenset(('unchecked', 'checked', 'issue'))
_RECORD = '校对记录.json'
_BASE_CACHE = ReviewBaseCache()


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _encode(record):
    # Match runner.atomic_json so the revision names the exact committed bytes.
    return json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def _json(content, label):
    def members(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f'{label} 含重复字段')
            value[key] = item
        return value

    def constant(_):
        raise ValueError(f'{label} 含非法数值')

    try:
        return json.loads(content.decode('utf-8-sig'), object_pairs_hook=members,
                          parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'{label} 损坏，请保留原文件并检查') from error


def _canonical(path):
    # Windows may retain an extended prefix during concurrent file creation.
    # It names the same object but pathlib treats it as a different drive.
    value = str(path.resolve())
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)


def _root(folder):
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError('校对项目不能是符号链接')
    folder = _canonical(folder)
    if not folder.is_dir():
        raise ValueError('校对项目目录不存在')
    return folder


def _path(root, *parts):
    """Only fixed/internal names enter here; reject reparse points at each hop."""
    path = root
    for part in parts:
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(
                stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError('校对路径不能指向链接或目录联接')
    if not _canonical(path).is_relative_to(root):
        raise ValueError('校对路径超出项目目录')
    return path


def _read(path, label, *, optional=False):
    try:
        return path.read_bytes()
    except FileNotFoundError as error:
        if optional:
            return None
        raise ValueError(f'缺少{label}：{path.name}') from error
    except OSError as error:
        raise ValueError(f'无法读取{label}：{path.name}') from error


def _text(value, label, maximum, *, subtitle=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{label}须为不超过 {maximum} 字的文本')
    if any(unicodedata.category(char) in {'Cc', 'Cs'} and char != '\n' for char in value):
        raise ValueError(f'{label}含非法控制字符')
    if subtitle and (not value.strip() or any(not line.strip() for line in value.split('\n'))):
        raise ValueError(f'{label}不能为空或含 SRT 空行分隔符')
    return value


def _cue(cue, duration):
    if (type(cue['start_ms']) is not int or type(cue['end_ms']) is not int
            or not 0 <= cue['start_ms'] < cue['end_ms'] <= duration):
        raise ValueError('字幕时间须为媒体范围内的整数毫秒，且结束晚于开始')
    _text(cue['source_text'], '原文', 4000, subtitle=True)
    _text(cue['target_text'], '译文', 4000, subtitle=True)
    if not valid_translation_content(cue['source_text'], cue['target_text']):
        raise ValueError('译文缺少有效文字内容')
    if not isinstance(cue['review_status'], str) or cue['review_status'] not in _STATUSES:
        raise ValueError('校对状态无效')
    _text(cue['note'], '校对备注', 2000)
    if type(cue['translation_stale']) is not bool:
        raise ValueError('译文待确认标记须为布尔值')
    if cue['review_status'] == 'checked' and cue['translation_stale']:
        raise ValueError('译文待确认，不能标为已校对')


def _validate_cues(cues, duration):
    previous_start = -1
    for cue in cues:
        _cue(cue, duration)
        if cue['start_ms'] < previous_start:
            raise ValueError('字幕开始时间须按原有条目顺序递增；本轮不支持重排')
        previous_start = cue['start_ms']


def _record(path):
    raw = _read(path, '人工校对记录', optional=True)
    if raw is None:
        return None, None
    record = _json(raw, '人工校对记录')
    if (not isinstance(record, dict) or set(record) != {'version', 'binding', 'patches'}
            or type(record['version']) is not int or record['version'] != 1
            or not isinstance(record['binding'], dict) or not isinstance(record['patches'], dict)):
        raise ValueError('人工校对记录格式无效，请保留原文件并检查')
    return record, raw


def _base(root, language, target):
    validate_languages(language, target)
    state = _json(_read(_path(root, 'state.json'), '项目状态'), '项目状态')
    if (not isinstance(state, dict) or not isinstance(state.get('identity'), dict)
            or type(state.get('duration_ms')) is not int or state['duration_ms'] <= 0):
        raise ValueError('项目状态缺少有效 identity 或 duration_ms')
    identity = state['identity']
    source = identity.get('source')
    if (not isinstance(source, dict) or not isinstance(source.get('sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', source['sha256']) is None):
        raise ValueError('项目 identity 缺少有效素材 SHA256')
    if (identity.get('language', 'ja'), identity.get('target', 'zh-CN')) != (language, target):
        raise ValueError('校对语言与项目状态不一致')
    names = output_names(target)
    content = [_read(_path(root, name), '机器字幕') for name in names[:2]]
    report = _read(_path(root, '需复核.json'), '自动复核清单', optional=True)
    binding = {'identity': identity, 'duration_ms': state['duration_ms'],
               'language': language, 'target': target,
               'source_sha256': _digest(content[0]), 'target_sha256': _digest(content[1]),
               'issues_sha256': _digest(report) if report is not None else None}
    return binding, content, report


def _base_cues(content):
    try:
        source, translated = [parse_srt(raw.decode('utf-8-sig')) for raw in content]
    except (ValueError, UnicodeError) as error:
        raise ValueError('机器字幕格式无效，请先恢复有效原文和译文') from error
    if not source or len(source) != len(translated) or any(
            (a.start_ms, a.end_ms) != (b.start_ms, b.end_ms) for a, b in zip(source, translated)):
        raise ValueError('人工校对需要条目数量和时间轴一致的非空原文及译文')
    return [{'id': index, 'start_ms': original.start_ms, 'end_ms': original.end_ms,
             'source_text': original.text, 'target_text': target.text,
             'review_status': 'unchecked', 'note': '', 'translation_stale': False,
             'warnings': []}
            for index, (original, target) in enumerate(zip(source, translated), 1)]


def _warnings(report, cues):
    if report is None:
        return
    issues = _json(report, '自动复核清单')
    if not isinstance(issues, list):
        raise ValueError('自动复核清单须为列表')
    starts, maximum_ends = [], []
    maximum_end = -1
    for cue in cues:
        starts.append(cue['start_ms'])
        maximum_end = max(maximum_end, cue['end_ms'])
        maximum_ends.append(maximum_end)
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get('reason'), str):
            raise ValueError('自动复核条目缺少原因文字')
        reason = _text(issue['reason'], '自动复核原因', 4000)
        if 'start_ms' not in issue and 'end_ms' not in issue:
            continue
        start, end = issue.get('start_ms'), issue.get('end_ms')
        if type(start) is not int or type(end) is not int:
            raise ValueError('自动复核条目的时间范围无效')
        # check_cues reports the *original* invalid range for removed cues.
        # Such a report has no overlap to map onto a surviving valid cue.
        if end < start:
            continue
        lower = bisect_right(maximum_ends, start)
        upper = bisect_right(starts, start) if start == end else bisect_left(starts, end)
        for cue in cues[lower:upper]:
            overlaps = (start < cue['end_ms'] and end > cue['start_ms'])
            if start == end:
                overlaps = cue['start_ms'] <= start < cue['end_ms']
            if overlaps and reason not in cue['warnings']:
                cue['warnings'].append(reason)


def _summary(cues):
    total = len(cues)
    checked = sum(cue['review_status'] == 'checked' for cue in cues)
    issues = sum(cue['review_status'] == 'issue' for cue in cues)
    pending = sum(cue['translation_stale'] for cue in cues)
    required = min(20, total)
    return {'total': total, 'checked': checked, 'issues': issues,
            'pending_translation': pending, 'required_checks': required,
            'can_accept': bool(total and checked >= required and not issues and not pending)}


def _load(root, language, target):
    record, raw = _record(_path(root, '人工校对', _RECORD))
    try:
        binding, content, report = _base(root, language, target)
    except ValueError as error:
        if record is not None:
            raise ReviewConflict('机器字幕或项目状态已改变；人工校对记录已保留，请核对') from error
        raise
    if record is not None and record['binding'] != binding:
        raise ReviewConflict('机器字幕或项目状态已改变；人工校对记录已保留，请核对')
    base = _BASE_CACHE.read(binding, content, parser=parse_srt,
                            validator=_validate_cues, builder=_base_cues)
    exists = record is not None
    if record is None:
        record = {'version': 1, 'binding': binding, 'patches': {}}
        raw = _encode(record)
    cues = [dict(cue, warnings=[]) for cue in base]
    for key, patch in record['patches'].items():
        if (not isinstance(key, str) or re.fullmatch(r'[1-9][0-9]*', key) is None
                or len(key) > 12 or not 1 <= int(key) <= len(cues)
                or not isinstance(patch, dict) or not patch or not set(patch) <= _PATCH_FIELDS):
            raise ValueError('人工校对记录含无效条目或字段')
        cues[int(key) - 1].update(patch)
    _validate_cues(cues, binding['duration_ms'])
    _warnings(report, cues)
    view = {'revision': _digest(raw), 'binding': binding, 'cues': cues,
            'summary': _summary(cues), 'exists': exists}
    return view, base, record, raw


def load_review(folder: Path, *, language='ja', target='zh-CN') -> dict:
    """Read the current review without writes; coordinate with ProjectLock as needed."""
    return _load(_root(folder), language, target)[0]


@contextmanager
def _review_lock(root):
    from .cloud_budget import _file_lock
    path = _path(root, '人工校对', 'review.guard.lock')
    with _file_lock(path):
        yield


def save_review(folder: Path, data: dict, *, language='ja', target='zh-CN') -> dict:
    """CAS-save one cue. Caller MUST hold runner.ProjectLock(folder).

    ``review_status`` may be omitted to preserve an unchanged cue's status or
    clear it after content changes. Sending ``checked`` is explicit confirmation
    of this edit. ``translation_confirmed`` always requires a real JSON boolean.
    """
    required = {'expected_revision', 'cue_id', *_CONTENT_FIELDS, 'note', 'translation_confirmed'}
    if (not isinstance(data, dict) or not required <= set(data)
            or not set(data) <= required | {'review_status'}):
        raise ValueError('校对保存请求缺少字段或含未知字段')
    if type(data['translation_confirmed']) is not bool:
        raise ValueError('译文确认须为布尔值')
    root = _root(folder)
    with _review_lock(root):
        view, base, record, _ = _load(root, language, target)
        if not isinstance(data['expected_revision'], str) or data['expected_revision'] != view['revision']:
            raise ReviewConflict('校对版本已变化，请刷新后再保存；本次内容尚未写入')
        cue_id = data['cue_id']
        if type(cue_id) is not int or not 1 <= cue_id <= len(base):
            raise ValueError('校对条目编号无效')
        current = view['cues'][cue_id - 1]
        edited = {**current, **{key: data[key] for key in (*_CONTENT_FIELDS, 'note')}}
        changed = any(edited[key] != current[key] for key in _CONTENT_FIELDS)
        edited['review_status'] = data.get('review_status', 'unchecked' if changed else current['review_status'])
        if edited['source_text'] != current['source_text']:
            edited['translation_stale'] = True
        if edited['target_text'] != current['target_text'] or data['translation_confirmed']:
            edited['translation_stale'] = False
        cues = view['cues'][:]
        cues[cue_id - 1] = edited
        _validate_cues(cues, view['binding']['duration_ms'])
        patch = {key: edited[key] for key in (*_CONTENT_FIELDS, 'review_status', 'note', 'translation_stale')
                 if edited[key] != base[cue_id - 1][key]}
        if patch:
            record['patches'][str(cue_id)] = patch
        else:
            record['patches'].pop(str(cue_id), None)
        # The project lock prevents generation races; recheck also catches most
        # unsupported external editors before committing any manual changes.
        if _base(root, language, target)[0] != view['binding']:
            raise ReviewConflict('保存期间机器字幕发生变化；本次校对未写入')
        from .runner import atomic_json
        atomic_json(_path(root, '人工校对', _RECORD), record)
        return _load(root, language, target)[0]


def _snapshot_files(view, raw, target):
    source, translated, bilingual = [], [], []
    for cue in view['cues']:
        start, end = cue['start_ms'], cue['end_ms']
        source.append(Cue(start, end, cue['source_text']))
        translated.append(Cue(start, end, cue['target_text']))
        bilingual.append(Cue(start, end, cue['target_text'] + '\n' + cue['source_text']))
    files = {name: render_srt(track).encode('utf-8') for name, track in
             zip(output_names(target), (source, translated, bilingual))}
    files[_RECORD] = raw
    return files


def _verify_snapshot(root, folder, expected):
    if not folder.is_dir():
        raise ReviewConflict('人工校对快照路径已存在但不是目录')
    if {path.name for path in folder.iterdir()} != set(expected):
        raise ReviewConflict('人工校对快照文件集合已改变，请保留后核对')
    for name, content in expected.items():
        path = _path(root, *folder.relative_to(root).parts, name)
        if _read(path, '人工校对快照') != content:
            raise ReviewConflict('人工校对快照已改变，请保留后核对')


def materialize_review(folder: Path, *, language='ja', target='zh-CN',
                       require_accepted=False) -> dict:
    """Publish one immutable snapshot. Caller MUST hold runner.ProjectLock(folder)."""
    if type(require_accepted) is not bool:
        raise ValueError('验收要求须为布尔值')
    root = _root(folder)
    with _review_lock(root):
        view, _, _, raw = _load(root, language, target)
        if require_accepted and not view['summary']['can_accept']:
            raise ValueError('尚未达到人工校对门槛，或仍有人工疑点/待确认译文')
        expected = _snapshot_files(view, raw, target)
        versions = _path(root, '人工校对', '版本')
        destination = _path(root, '人工校对', '版本', view['revision'])
        if destination.exists():
            _verify_snapshot(root, destination, expected)
        else:
            versions.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=versions))
            try:
                for name, content in expected.items():
                    with (staging / name).open('xb') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                if _base(root, language, target)[0] != view['binding']:
                    raise ReviewConflict('导出期间机器字幕发生变化；快照未发布')
                # Review lock serializes publication. rename is a same-volume,
                # atomic directory publication; no destination is overwritten.
                staging.rename(destination)
            finally:
                if staging.exists():
                    if not _canonical(staging).is_relative_to(_canonical(versions)):
                        raise ValueError('校对暂存目录超出版本目录')
                    shutil.rmtree(staging)
            _verify_snapshot(root, destination, expected)
        return {'folder': destination, 'revision': view['revision'],
                'files': [{'path': str(destination / name), 'sha256': _digest(content)}
                          for name, content in expected.items()],
                'summary': view['summary'], 'binding': view['binding']}
