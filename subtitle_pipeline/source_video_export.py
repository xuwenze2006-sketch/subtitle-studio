"""Local, original-only unreviewed MP4 export, separate from cloud approval."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading

from . import manual_review, media_export, runner as r
from .file_observation import stable_read_guard
from .integrity import fingerprint, validate_chunk_plan, verify_part
from .languages import validate_languages
from .subtitles import Chunk, Cue, render_srt


_DIGEST = re.compile(r'[0-9a-f]{64}')
_TRANSLATED = ('中文草稿.srt', '英文草稿.srt', '日文草稿.srt', '双语草稿.srt')


def _emit(message, **data):
    print(json.dumps({'message': message, **data}, ensure_ascii=False), flush=True)


def _plain(path):
    """Reject reparse points before resolving away their identity."""
    path = Path(path)
    if not path.is_absolute() or '\0' in str(path) or len(str(path)) > 4096:
        raise ValueError('原文导出路径必须是有效绝对路径')
    for current in reversed((path, *path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(
                stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError('原文导出路径不能指向符号链接或目录联接')
    return path.resolve()


def _path(root, *parts):
    path = _plain(root.joinpath(*parts))
    if not path.is_relative_to(root):
        raise ValueError('原文导出路径超出项目目录')
    return path


def _json(path, *, optional=False):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if optional:
            return None, None
        raise ValueError('缺少原文导出所需记录：' + path.name) from None
    value = manual_review._json(raw, path.name)
    if not isinstance(value, dict):
        raise ValueError('原文导出记录必须是对象：' + path.name)
    return value, raw


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _export_root(project):
    return _path(project, '导出', '原文视频')


def _ownership(project, source, source_hash):
    path = _path(_export_root(project), 'ownership.json')
    record, _ = _json(path, optional=True)
    expected = {'version': 1, 'mode': 'source_only', 'project': str(project),
                'source': str(source), 'source_sha256': source_hash}
    if record is not None and (not isinstance(record.get('manual_record_seen'), bool)
            or record != {**expected, 'manual_record_seen': record.get('manual_record_seen')}):
        raise ValueError('原文导出所有权记录已损坏或来源不一致，请保留后核对')
    return path, record, expected


def _inputs(project, source=None, *, delivery=True):
    """Only fixed small files; no media probe, hash scan, lock, or writes."""
    project = _plain(project)
    if not project.is_dir() or _path(project, 'campaign.json').exists():
        raise ValueError('原文视频导出仅支持独立本地原文项目')
    state, raw = _json(_path(project, 'state.json'))
    config, identity = state.get('config'), state.get('identity')
    if (not isinstance(config, dict) or not isinstance(identity, dict)
            or config.get('translate') is not False
            or identity.get('engine') != 'whisper_cpp'
            or config.get('asr_provider', 'whisper_cpp') != 'whisper_cpp'
            or state.get('status') != 'complete' or 'pending_outputs' in state
            or 'merged_source_edit' in state
            or any(state.get(name) for name in ('merged_source_needs_review',
                                               'finalization_error', 'scheduler_error'))):
        raise ValueError('本地原文字幕尚未完整生成，或项目仍有待处理的生成冲突')
    duration = state.get('duration_ms')
    original = identity.get('source')
    if (type(duration) is not int or duration <= 0 or not isinstance(original, dict)
            or not _digest(original.get('sha256'))):
        raise ValueError('原文项目缺少有效素材身份或时长')
    configured = _plain(config.get('source', ''))
    original_path = _plain(original.get('path', ''))
    if (_plain(config.get('project', '')) != project or configured != original_path
            or source is not None and _plain(source) != configured or not configured.is_file()):
        raise ValueError('原文项目来源或项目目录不一致，请重新载入正确项目')
    language, target = config.get('language', 'ja'), config.get('target', 'zh-CN')
    validate_languages(language, target, allow_auto=True)
    if (identity.get('language', 'ja'), identity.get('target', 'zh-CN')) != (language, target):
        raise ValueError('原文项目语言配置与生成身份不一致')
    if any(_path(project, name).exists() for name in _TRANSLATED):
        raise ValueError('项目包含译文字幕，不能降级为原文导出')
    view = manual_review.load_review(project, language=language, target=target)
    if view.get('mode') != 'source_only':
        raise ValueError('当前校对记录不是原文模式，请保留后核对')
    _, owner, _ = _ownership(project, configured, original['sha256'])
    top, _ = _json(_path(_export_root(project), 'export.json'), optional=True)
    previous_manual = isinstance(top, dict) and isinstance(top.get('draft_output_binding'), dict) and (
        top['draft_output_binding'].get('manual_record_exists') is True
        or top['draft_output_binding'].get('manual_revision') is not None)
    if not view['exists'] and (owner is not None and owner['manual_record_seen'] or previous_manual):
        raise ValueError('原人工校对记录缺失，不能回退机器字幕导出原文视频')
    if not delivery:
        return project, configured, state, view, None
    snapshot = _path(project, '人工校对', '版本', view['revision'])
    captions = render_srt([Cue(cue['start_ms'], cue['end_ms'], cue['source_text'])
                           for cue in view['cues']]).encode('utf-8')
    artifacts = [{'path': str(snapshot / '原文.srt'), 'sha256': _hash(captions)},
                 {'path': str(snapshot / '校对记录.json'), 'sha256': view['revision']}]
    binding = {'mode': 'source_only', 'source_sha256': original['sha256'],
        'captions_sha256': _hash(captions), 'review_artifacts_sha256': fingerprint(artifacts),
        'render_version': 1, 'review_status': 'unreviewed_draft', 'language': language, 'target': target,
        'generation_state_sha256': _hash(raw), 'machine_source_sha256': view['binding']['source_sha256'],
        'manual_binding_sha256': fingerprint(view['binding']), 'manual_record_exists': view['exists'],
        'manual_revision': view['revision'] if view['exists'] else None, 'snapshot_revision': view['revision']}
    return project, configured, state, view, binding


def source_export_available(project, source):
    """Read-only capability; full source and chunk evidence are checked at export."""
    try:
        _inputs(project, source, delivery=False)
        return True
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def _verify_generation(project, state, source_hash, stop):
    try:
        chunks = [Chunk(**value) for value in state['chunks']]
        validate_chunk_plan(chunks, state['duration_ms'])
        if not isinstance(state['parts'], dict):
            raise ValueError('原文片段证明无效')
        language = state['config'].get('language', 'ja')
        for chunk in chunks:
            media_export._check_export_stop(stop)
            folder = _path(project, '片段', f'{chunk.index + 1:04d}')
            _path(folder, 'source.local.srt')
            part = state['parts'].get(str(chunk.index), {})
            if (not isinstance(part, dict) or not verify_part(folder, chunk, part,
                    translated=False, cloud=False, provider='whisper_cpp',
                    source_sha256=source_hash, language=language, stop=stop)):
                raise ValueError('原文识别片段内容或来源证明不完整，请保留项目后核对')
    except (KeyError, TypeError) as error:
        raise ValueError('原文分段计划或识别证明损坏') from error


def _locations(project, source, binding, requested):
    key = fingerprint({'binding': binding, 'requested_encoder': requested})
    base = _export_root(project)
    folder = _path(base, key)
    # Limit the human-readable portion; the complete digest owns identity.
    stem = source.stem[:60].rstrip(' .') or '视频'
    final = _path(base, stem + '_原文_' + key + '_未审核草稿.mp4')
    return key, base, folder, final


def _validate_work_paths(folder, final):
    _plain(folder); _plain(final)
    for name in ('export.json', 'encoding-checkpoint.json', 'publication-checkpoint.json',
                 'verification.json', 'captions.srt', 'captions.ass', 'result.partial.mp4',
                 'encode-progress.txt', 'encode.log', 'ass.log', '保留的编码'):
        _path(folder, name)
    logs = [folder / 'ass.log', folder / 'encode.log']
    if folder.is_dir():
        logs.extend(folder.glob('decode-*.log'))
    for log in logs:
        path = _path(folder, log.name)
        if path.exists() and path.stat().st_nlink > 1:
            raise ValueError('原文导出日志不能与其他文件共享硬链接，未修改关联文件')


def _snapshot_matches(project, view, binding):
    folder = _path(project, '人工校对', '版本', view['revision'])
    if not folder.is_dir() or {item.name for item in folder.iterdir()} != {'原文.srt', '校对记录.json'}:
        return False
    return (_hash(_path(folder, '原文.srt').read_bytes()) == binding['captions_sha256']
            and _hash(_path(folder, '校对记录.json').read_bytes()) == view['revision'])


def _stored_source_info(binding):
    if 'audio_policy' not in binding:
        return None
    tracks = binding.get('audio_tracks')
    if binding['audio_policy'] != 'all_tracks_v1' or not isinstance(tracks, list) or len(tracks) < 2:
        raise ValueError('原文成片音轨绑定无效')
    return {'streams': [{'codec_type': 'audio', **{name: track[name] for name in
            ('codec_name', 'sample_rate', 'channels', 'channel_layout')},
            'tags': {'language': track['language']},
            'disposition': {'default': track['default'], 'forced': track['forced']}}
            for track in tracks]}


def current_source_video(project, source, *, video_matches):
    """Expose only an owned output bound to the current saved delivery input."""
    try:
        project = _plain(project)
        record, _ = _json(_path(_export_root(project), 'export.json'), optional=True)
        if (record is None or type(record.get('version')) is not int or record['version'] != 1
                or record.get('mode') != 'source_only' or record.get('status') != 'draft_exported'):
            return None
        requested = record.get('requested_encoder')
        if requested not in ('auto', 'qsv', 'cpu'):
            return None
        project, source, state, view, binding = _inputs(project, source)
        key, _, folder, final = _locations(project, source, binding, requested)
        _validate_work_paths(folder, final)
        saved = record.get('draft_output_binding')
        if (record.get('input_key') != key or record.get('draft_output') != str(final)
                or record.get('source') != {'path': str(source), 'sha256': binding['source_sha256']}
                or not isinstance(saved, dict) or saved.get('encoder') not in ('cpu', 'qsv')
                or requested not in ('auto', saved['encoder'])
                or not _digest(record.get('draft_output_sha256')) or not final.is_file()):
            return None
        # The encoder adds audio inventory to the base binding. Its versioned
        # evidence is validated again by the worker, without probing in readers.
        expected = {**binding, 'encoder': saved['encoder']}
        if 'audio_policy' in saved:
            expected.update(audio_policy=saved['audio_policy'], audio_tracks=saved.get('audio_tracks'))
        if saved != expected:
            return None
        if (not _snapshot_matches(project, view, binding)
                or media_export._publication_evidence(folder, saved, record['draft_output_sha256'],
                                                      _stored_source_info(saved)) is None):
            return None
        return final if (video_matches(source, binding['source_sha256'])
                         and video_matches(final, record['draft_output_sha256'])) else None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def export_source_video(project, stop, *, encoder='auto', emit=None):
    """Pin a complete local generation and render its current original captions."""
    if encoder not in ('auto', 'qsv', 'cpu') or not isinstance(encoder, str):
        raise ValueError('编码方式须为 auto、qsv 或 cpu')
    emit = _emit if emit is None else emit
    media_export._check_export_stop(stop)
    project = _plain(project)
    if not project.is_dir():
        raise ValueError('原文项目目录不存在')
    _path(project, 'run.lock'); _path(project, 'run.guard.lock')
    with r.ProjectLock(project):
        project, source, state, view, base_binding = _inputs(project)
        with stable_read_guard(source):
            actual = r.file_identity(source, stop=stop)
            if actual['sha256'] != base_binding['source_sha256']:
                raise ValueError('原视频内容已改变，不能复用旧项目导出')
            _verify_generation(project, state, actual['sha256'], stop)
            key, base, folder, final = _locations(project, source, base_binding, encoder)
            _validate_work_paths(folder, final)
            owner_path, previous_owner, owner = _ownership(project, source, actual['sha256'])
            base.mkdir(parents=True, exist_ok=True)
            r.atomic_json(owner_path, {**owner, 'manual_record_seen': bool(view['exists']
                or previous_owner is not None and previous_owner['manual_record_seen'])})
            snapshot = manual_review.materialize_review(project,
                language=base_binding['language'], target=base_binding['target'], require_accepted=False)
            if (snapshot['revision'] != view['revision'] or snapshot['binding'] != view['binding']
                    or snapshot.get('mode') != 'source_only'
                    or {Path(item['path']).name for item in snapshot['files']} != {'原文.srt', '校对记录.json'}
                    or fingerprint(snapshot['files']) != base_binding['review_artifacts_sha256']):
                raise ValueError('准备原文视频时校对版本发生变化，请重新导出')
            captions = _path(project, '人工校对', '版本', view['revision'], '原文.srt')

            def validate_inputs():
                media_export._check_export_stop(stop)
                _validate_work_paths(folder, final)
                if _inputs(project, source)[4] != base_binding:
                    raise ValueError('导出期间原文字幕或项目状态已变化，未发布或登记为当前成片')
                # The snapshot is immutable but unsupported external writers
                # must still not replace it after materialization.
                for item in snapshot['files']:
                    path = _path(project, '人工校对', '版本', view['revision'], Path(item['path']).name)
                    if _hash(path.read_bytes()) != item['sha256']:
                        raise ValueError('导出期间原文校对快照发生变化，未发布成片')

            previous, _ = _json(_path(folder, 'export.json'), optional=True)
            expected = {'version': 1, 'mode': 'source_only', 'status': 'draft_exported',
                'source': {'path': str(source), 'sha256': actual['sha256']},
                'input_key': key, 'requested_encoder': encoder, 'duration_ms': state['duration_ms']}
            if previous is not None and (type(previous.get('version')) is not int
                    or any(previous.get(name) != value for name, value in expected.items())):
                raise ValueError('原文成片版本记录已损坏或来源不一致，请保留后核对')
            manifest = expected if previous is None else previous

            def write_record(_project, record):
                validate_inputs()
                r.atomic_json(_path(folder, 'export.json'), record)
                media_export._check_export_stop(stop)
                validate_inputs()
                r.atomic_json(_path(base, 'export.json'), record)

            dependencies = media_export.RenderDependencies(write_record, emit)
            media_export.render_video(project, manifest, source, captions, dict(base_binding), stop,
                folder=folder, final=final, draft=True, encoder=encoder, dependencies=dependencies,
                validate_inputs=validate_inputs, edition='原文未审核草稿',
                review_label='原文字幕未审核，仅完成自动媒体完整性检查')
            return manifest


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='本地原文字幕未审核 MP4 导出')
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--encoder', choices=('auto', 'qsv', 'cpu'), default='auto')
    args = parser.parse_args(argv)
    stop, finished = threading.Event(), threading.Event()
    marker_value = os.environ.get('SUBTITLE_STUDIO_STOP_FILE', '').strip()
    marker = Path(marker_value) if marker_value else None

    def watch():
        while not finished.wait(.2):
            if marker is not None and marker.exists():
                stop.set()

    try:
        if marker is not None and marker.exists():
            stop.set()
        threading.Thread(target=watch, daemon=True).start()
        export_source_video(args.project, stop, encoder=args.encoder)
        return 2 if stop.is_set() else 0
    except Exception as error:
        _emit(str(error), status='cancelled' if stop.is_set() else 'needs_attention')
        return 2
    finally:
        finished.set()


if __name__ == '__main__':
    raise SystemExit(main())
