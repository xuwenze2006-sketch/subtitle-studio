"""Shared, explicit language names and compatible subtitle output names."""
from pathlib import Path

SOURCE_LABELS = {'ja': '日语', 'en': '英语', 'zh': '中文', 'auto': '自动检测'}
TARGET_LABELS = {'zh-CN': '中文', 'en': '英语', 'ja': '日语'}


def default_target(source):
    return 'en' if source == 'zh' else 'zh-CN'


def validate_languages(source, target, allow_auto=False):
    if not isinstance(source, str) or source not in SOURCE_LABELS or source == 'auto' and not allow_auto:
        raise ValueError('原文语言仅支持日语、英语和中文' + ('，或自动检测' if allow_auto else ''))
    if not isinstance(target, str) or target not in TARGET_LABELS:
        raise ValueError('译文语言仅支持中文、英语和日语')


def target_filename(target):
    try:
        return {'zh-CN': '中文草稿.srt', 'en': '英文草稿.srt', 'ja': '日文草稿.srt'}[target]
    except (KeyError, TypeError):
        raise ValueError('不支持的译文语言') from None


def output_names(target):
    return ('原文.srt', target_filename(target), '双语草稿.srt')


def manifest_languages(manifest):
    source, target = manifest.get('language', 'ja'), manifest.get('target', 'zh-CN')
    validate_languages(source, target)
    return source, target


def video_output_path(source, target, *, draft=False):
    source = Path(source)
    try:
        suffix = {'zh-CN': '中文字幕', 'en': '英文字幕', 'ja': '日文字幕'}[target]
    except (KeyError, TypeError):
        raise ValueError('不支持的译文语言') from None
    edition = '未审核草稿' if draft else '修订版'
    return source.with_name(source.stem + '_' + suffix + '_' + edition + '.mp4')
