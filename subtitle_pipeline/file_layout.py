"""Human-readable local deliveries; processing filenames remain unchanged.

The caller owns the live selection lock and relevant ProjectLocks and must
verify source binding and an idle job before saving a snapshot.
"""

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from .subtitles import parse_srt
from .languages import output_names, manifest_languages


OUTPUT_LABELS = {'原文.srt': '原文草稿', '中文草稿.srt': '中文字幕草稿', '英文草稿.srt': '英文字幕草稿',
                 '日文草稿.srt': '日文字幕草稿', '双语草稿.srt': '双语草稿'}
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)


def _safe_name(value, limit=48):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(value)).strip(' .')[:limit].rstrip(' .') or '未命名'
    return ('_' + value)[:limit] if _RESERVED.match(value) else value


def timestamped_project(source: Path, root: Path, now: datetime | None = None) -> Path:
    source = Path(source).expanduser().resolve()
    now = now or datetime.now().astimezone()
    identity = hashlib.sha256(str(source).casefold().encode('utf-8')).hexdigest()[:10]
    return Path(root) / now.strftime('%Y-%m-%d') / f'{now:%H%M%S}_{_safe_name(source.stem)}-{identity}'


def subtitle_filename(source: Path, selection_name: str, canonical_name: str, modified_at: float) -> str:
    if canonical_name not in OUTPUT_LABELS:
        raise ValueError('不支持的字幕文件')
    if isinstance(modified_at, bool) or not isinstance(modified_at, (int, float)) or not math.isfinite(modified_at):
        raise ValueError('字幕文件时间无效')
    try:
        stamp = datetime.fromtimestamp(modified_at).strftime('%Y%m%d_%H%M%S')
    except (OSError, OverflowError, ValueError):
        raise ValueError('字幕文件时间无效') from None
    return f'{_safe_name(Path(source).stem)}_{_safe_name(selection_name, 32)}_{OUTPUT_LABELS[canonical_name]}_{stamp}.srt'


def _contained(project, candidate):
    resolved = Path(candidate).resolve()
    if not resolved.is_relative_to(project):
        raise ValueError('结果路径不在所选项目内')
    return resolved


def _read_json(project, path):
    path = _contained(project, path)
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_SUBTITLE_BYTES + 1)
        if len(data) > MAX_SUBTITLE_BYTES:
            return {}
        value = json.loads(data.decode('utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_signature(info):
    # On Windows Python maps ctime differently for stat and an open handle;
    # inode, length, mtime and the separate content hash identify a generation.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_subtitle(project, path):
    path = _contained(project, path)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_SUBTITLE_BYTES:
            raise ValueError('字幕文件超过 16 MiB，未保存版本')
        data = stream.read(MAX_SUBTITLE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError('字幕文件超过 16 MiB，未保存版本')
    signature = _file_signature(before)
    if signature != _file_signature(after) or signature != _file_signature(path.stat()):
        raise ValueError('字幕在保存过程中发生变化，请重新保存')
    return data, signature, before.st_mtime


def _source_metadata(source, state, campaign):
    info = source.stat()
    if not source.is_file():
        raise ValueError('输入素材不存在')
    result = {'path': str(source), 'name': source.name, 'size': info.st_size,
              'mtime_ns': info.st_mtime_ns, 'mtime': info.st_mtime}
    identity = state.get('identity')
    candidates = [identity.get('source') if isinstance(identity, dict) else None, campaign.get('source')]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        digest = candidate.get('sha256')
        try:
            matches = (isinstance(candidate.get('path'), str) and Path(candidate['path']).resolve() == source
                       and candidate.get('size') == info.st_size and candidate.get('mtime_ns') == info.st_mtime_ns)
        except (OSError, ValueError):
            matches = False
        if matches and isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
            result.update(sha256=digest, sha256_origin='existing-project-record')
            break
    return result


def _delivery_name(folder, name):
    # Leave room for collision suffixes under conventional Windows path limits.
    available = min(180, 240 - len(str(folder)) - 6)
    if available < 48:
        raise ValueError('项目路径过长，无法保存带时间标注的字幕版本')
    if len(name) > available:
        name = name[:available - 41] + '_' + name[-40:]
    return name


def save_subtitle_snapshot(project: Path, source: Path, selection: dict, created_at: str = '') -> dict:
    project = Path(project).resolve()
    source = Path(source).resolve()
    if not project.is_dir():
        raise ValueError('字幕项目尚未创建')
    folder = _contained(project, selection['folder'])
    state = _read_json(project, folder / 'state.json')
    campaign = _read_json(project, project / 'campaign.json')
    config = state.get('config', {})
    source_language, target_language = manifest_languages(campaign) if campaign else (config.get('language','ja'),config.get('target','zh-CN'))
    selected_tracks = selection.get('tracks', output_names(target_language))
    if not isinstance(selected_tracks, (list, tuple)) or any(
            not isinstance(name, str) or name not in OUTPUT_LABELS for name in selected_tracks):
        raise ValueError('不支持的字幕文件')
    offset = selection.get('offset_ms', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('片段时间偏移无效')
    status = state.get('status', 'unknown')
    review = state.get('review_status', 'unreviewed')
    status = status if isinstance(status, str) else 'unknown'
    review = review if isinstance(review, str) else 'unreviewed'
    source_info = _source_metadata(source, state, campaign)
    tracks = []
    track_folder=_contained(project,selection.get('track_folder',folder))
    for canonical in dict.fromkeys(selected_tracks):
        path = _contained(project, track_folder / canonical)
        if not path.is_file():
            continue
        data, signature, modified_at = _read_subtitle(project, path)
        try:
            text = data.decode('utf-8-sig')
            cues = parse_srt(text)
        except (UnicodeError, ValueError):
            raise ValueError(f'{canonical} 不是有效的 UTF-8 SRT 字幕') from None
        if any(cue.end_ms <= cue.start_ms or not cue.text.strip() for cue in cues):
            raise ValueError(f'{canonical} 含无效字幕时间或空白文本')
        if not cues and status not in ('complete', 'completed', 'done'):
            raise ValueError('空字幕缺少处理完成记录，未保存版本')
        tracks.append({'canonical': canonical, 'source_path': path, 'data': data,
                       'signature': signature, 'modified_at': modified_at,
                       'sha256': hashlib.sha256(data).hexdigest()})
    if not tracks:
        raise ValueError('所选片段尚无可保存的字幕')

    now = datetime.now().astimezone()
    saved_at = now.isoformat(timespec='seconds')
    export_root = _contained(project, project / '导出')
    base = f'{now:%Y%m%d_%H%M%S}_{_safe_name(selection.get("name", "当前任务"), 32)}'
    expected_folder = export_root / base
    for track in tracks:
        track['name'] = _delivery_name(expected_folder, subtitle_filename(
            source, selection.get('name', '当前任务'), track['canonical'], now.timestamp()))
    manifest = {'version': 1, 'source': source_info, 'created_at': created_at if isinstance(created_at, str) else '',
                'source_language':source_language,'target_language':target_language,
                'saved_at': saved_at, 'generation_status': status, 'review_status': review,
                'review_status_origin': 'copied-project-state-not-revalidated',
                'selection': {'id': selection.get('id', 'main'), 'name': selection.get('name', '当前任务'),
                              'offset_ms': offset, 'timestamp_basis': 'source-relative' if selection.get('id') == 'main' else 'sample-relative'},
                'files': [{'canonical': track['canonical'], 'name': track['name'], 'path': track['name'],
                           'sha256': track['sha256'], 'source_mtime': track['modified_at'],
                           'source_mtime_ns': track['signature'][3]} for track in tracks]}
    if selection.get('manual_review'):
        manifest['manual_review']=selection['manual_review']
        manifest['review_status']='manual_review_in_progress'
        manifest['review_status_origin']='explicit-cue-review-records'
    export_root.mkdir(exist_ok=True)
    _contained(project, export_root)
    staging = Path(tempfile.mkdtemp(prefix='.字幕快照-', dir=export_root))
    staging = _contained(project, staging)
    owned_files = []
    final = None
    published = False
    try:
        for track in tracks:
            path = _contained(project, staging / track['name'])
            owned_files.append(path)
            path.write_bytes(track['data'])
        for name, value in (('输入来源.json', source_info), ('版本记录.json', manifest)):
            path = _contained(project, staging / name)
            owned_files.append(path)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        for track in tracks:
            current, signature, _ = _read_subtitle(project, track['source_path'])
            if signature != track['signature'] or hashlib.sha256(current).hexdigest() != track['sha256']:
                raise ValueError('字幕在保存过程中发生变化，请重新保存')
        if _file_signature(source.stat())[2:4] != (source_info['size'], source_info['mtime_ns']):
            raise ValueError('输入素材在保存过程中发生变化，请重新保存')
        # Exclusively reserve the delivery folder. The version manifest moves
        # last, so an interrupted publication never claims a complete snapshot.
        index = 1
        while True:
            candidate = export_root / (base if index == 1 else f'{base}-{index:02d}')
            _contained(project, candidate)
            try:
                candidate.mkdir()
                final = candidate
                break
            except FileExistsError:
                index += 1
        for path in list(owned_files):
            destination = _contained(project, final / path.name)
            if destination.exists() or destination.is_symlink():
                raise ValueError('目标字幕版本文件已存在，未覆盖')
            path.rename(destination)
            owned_files.remove(path)
            owned_files.append(destination)
        staging.rmdir()
        published = True
        return {'folder': str(final), 'files': [{'name': track['name'], 'path': str(final / track['name'])} for track in tracks],
                'saved_at': saved_at, 'review_status': review}
    finally:
        if not published:
            for path in reversed(owned_files):
                try:
                    if path.parent in (staging, final) and _contained(project, path) == path:
                        path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
            for directory in (staging, final):
                try:
                    if directory is not None and _contained(project, directory) == directory:
                        directory.rmdir()
                except (OSError, ValueError):
                    pass
