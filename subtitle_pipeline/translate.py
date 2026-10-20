"""Paced, resumable free translation with verified subtitle-to-result mapping.

Only subtitle text is sent to Google; audio never leaves this module's caller.
The free endpoints may be unavailable. Failures are explicit, never source-text
substitutions. Endpoint formats follow Subtitle Edit 5.2.0 GoogleTranslateV1.cs.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .atomic_io import cleanup_temporary, replace_with_retry
from .integrity import valid_translation_content


class TranslationError(RuntimeError):
    """A translation or its durable cache could not be completed safely."""


class TranslationCancelled(TranslationError):
    """The caller requested cancellation."""


class _InvalidResponse(TranslationError):
    pass


_MAX_BATCH_CUES = 16
_MAX_BATCH_CHARACTERS = 1200
_MAX_CUE_CHARACTERS = 1500
_REQUEST_INTERVAL = 0.5
_REQUEST_TIMEOUT = 15
_RETRY_DELAYS = (1.0, 3.0, 7.0)
_queue = threading.Lock()
_last_request_at = 0.0
_marker = re.compile(r'\[\[[SE]\d{4}\]\]')
_pair = re.compile(r'\[\[S(\d{4})\]\](.*?)\[\[E\1\]\]', re.DOTALL)


def _check_cancel(stop_event: threading.Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise TranslationCancelled('翻译已取消；已完成的译文保留在缓存中。')


def _wait(seconds: float, stop_event: threading.Event | None) -> None:
    _check_cancel(stop_event)
    if seconds > 0:
        if stop_event is None:
            time.sleep(seconds)
        elif stop_event.wait(seconds):
            _check_cancel(stop_event)


def _request_json(url: str, stop_event: threading.Event | None = None):
    # Lazy imports avoid runner -> translate -> local_process -> runner.
    from .local_process import capture_process, OutputLimitExceeded
    from .runner import Cancelled
    from .translation_transport import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES

    _check_cancel(stop_event)
    request = json.dumps({'url': url}, ensure_ascii=False).encode('utf-8')
    if len(request) > MAX_REQUEST_BYTES:
        raise _InvalidResponse('翻译请求过大。')
    limit = ((MAX_RESPONSE_BYTES + 2) // 3) * 4 + 1024
    try:
        # A regular temporary file avoids a blocking Windows stdin pipe write.
        # Neither subtitles nor their URL appear in process arguments.
        with tempfile.TemporaryFile(mode='w+b') as input_file:
            input_file.write(request)
            input_file.seek(0)
            result = capture_process(
                [sys.executable, '-X', 'utf8', '-m', 'subtitle_pipeline.translation_transport'],
                stdin=input_file, cwd=Path(__file__).resolve().parent.parent,
                stop=stop_event, timeout=_REQUEST_TIMEOUT, max_output_bytes=limit)
    except Cancelled as error:
        raise TranslationCancelled('翻译已取消；已完成的译文保留在缓存中。') from error
    except subprocess.TimeoutExpired as error:
        raise TimeoutError('翻译请求超过总等待时间。') from error
    except OutputLimitExceeded as error:
        raise _InvalidResponse('翻译服务响应过大。') from error
    except subprocess.CalledProcessError as error:
        raise URLError('翻译请求进程未正常完成。') from error
    _check_cancel(stop_event)
    try:
        if len(result.stdout) > limit:
            raise ValueError
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict):
            raise ValueError
        kind = envelope.get('kind')
        if kind == 'http' and set(envelope) == {'kind', 'code'}:
            code = envelope['code']
            if type(code) is not int or not 300 <= code <= 599:
                raise ValueError
            raise HTTPError('', code, '翻译服务 HTTP 错误', {}, None)
        if kind == 'network' and set(envelope) == {'kind'}:
            raise URLError('翻译服务网络请求失败。')
        if kind == 'too_large' and set(envelope) == {'kind'}:
            raise _InvalidResponse('翻译服务响应过大。')
        if (kind != 'ok' or set(envelope) != {'kind', 'body'}
                or not isinstance(envelope['body'], str)):
            raise ValueError
        body = base64.b64decode(envelope['body'], validate=True)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise _InvalidResponse('翻译请求进程返回无效结果。') from error
    try:
        return json.loads(body.decode('utf-8'))
    except (UnicodeError, ValueError) as error:
        raise _InvalidResponse('翻译服务未返回有效 JSON。') from error


def _response_text(payload, fallback: bool) -> str:
    try:
        if not isinstance(payload, list) or not payload:
            raise ValueError
        parts = []
        if fallback:
            for entry in payload:
                part = entry[0] if isinstance(entry, list) and entry else entry
                if not isinstance(part, str):
                    raise ValueError
                parts.append(part)
        else:
            if not isinstance(payload[0], list) or not payload[0]:
                raise ValueError
            for entry in payload[0]:
                if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
                    raise ValueError
                parts.append(entry[0])
        result = ''.join(parts).strip()
        if not result:
            raise ValueError
        return result
    except (IndexError, TypeError, ValueError) as error:
        raise _InvalidResponse('翻译服务返回空内容或未知数据格式。') from error


class _Client:
    def __init__(self, source: str, target: str, stop_event: threading.Event | None):
        self.source = source
        self.target = target
        self.stop_event = stop_event
        self.fallback = False

    def translate(self, text: str) -> str:
        global _last_request_at
        last_error = '未知错误'
        skip_backoff = False
        for attempt in range(4):
            _check_cancel(self.stop_event)
            if attempt and not skip_backoff:
                _wait(_RETRY_DELAYS[attempt - 1], self.stop_event)
            skip_backoff = False
            _wait(max(0.0, _REQUEST_INTERVAL - (time.monotonic() - _last_request_at)), self.stop_event)
            _check_cancel(self.stop_event)
            query = {'client': 'dict-chrome-ex' if self.fallback else 'gtx',
                     'sl': self.source, 'tl': self.target, 'q': text}
            if self.fallback:
                endpoint = 'https://clients5.google.com/translate_a/t'
            else:
                endpoint = 'https://translate.googleapis.com/translate_a/single'
                query['dt'] = 't'
            _last_request_at = time.monotonic()
            try:
                payload = _request_json(endpoint + '?' + urlencode(query), stop_event=self.stop_event)
                _check_cancel(self.stop_event)
                return _response_text(payload, self.fallback)
            except HTTPError as error:
                error.close()
                _check_cancel(self.stop_event)
                last_error = f'HTTP {error.code}'
                if error.code in (403, 429) and not self.fallback:
                    self.fallback = True
                    skip_backoff = True
                    continue
                if error.code not in (429, 500, 502, 503, 504):
                    break
            except (TimeoutError, URLError, OSError) as error:
                _check_cancel(self.stop_event)
                last_error = f'网络错误（{type(error).__name__}）'
        raise TranslationError(f'免费翻译暂不可用：{last_error}。稍后重试可复用已完成缓存。')


def _key(text: str, source: str, target: str) -> str:
    value = json.dumps([source, target, text], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {'version': 1, 'entries': {}}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('entries'), dict):
            raise ValueError('invalid cache format')
        for digest, entry in payload['entries'].items():
            if not isinstance(entry, dict) or any(not isinstance(entry.get(name), str) for name in ('source', 'target', 'text', 'translation')):
                raise ValueError('invalid cache entry')
            if digest != _key(entry['text'], entry['source'], entry['target']) or not entry['translation'].strip():
                raise ValueError('invalid cache entry identity')
        return payload
    except (OSError, ValueError, TypeError) as error:
        raise TranslationError(f'翻译缓存无法读取，已保留原文件：{path}') from error


def _save_cache(path: Path, payload: dict) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    except OSError as error:
        raise TranslationError(f'翻译缓存写入失败：{path}') from error
    finally:
        if temporary is not None:
            cleanup_temporary(temporary)


def _format_batch(texts: list[str]) -> str:
    return '\n'.join(f'[[S{index:04d}]]\n{text}\n[[E{index:04d}]]' for index, text in enumerate(texts))


def _batches(texts: list[str]):
    batch = []
    for text in texts:
        if _marker.search(text):
            if batch:
                yield batch
                batch = []
            yield [text]
            continue
        candidate = batch + [text]
        if batch and (len(candidate) > _MAX_BATCH_CUES or len(_format_batch(candidate)) > _MAX_BATCH_CHARACTERS):
            yield batch
            batch = []
        batch.append(text)
    if batch:
        yield batch


def _parse_batch(text: str, count: int) -> list[str] | None:
    results = {}
    previous_end = 0
    for match in _pair.finditer(text):
        if text[previous_end:match.start()].strip():
            return None
        previous_end = match.end()
        index = int(match[1])
        translation = match[2].strip()
        if index >= count or index in results or not translation or _marker.search(translation):
            return None
        results[index] = translation
    if text[previous_end:].strip() or set(results) != set(range(count)):
        return None
    return [results[index] for index in range(count)]


def _looks_copied(original: str, translation: str, source: str, target: str) -> bool:
    # A verified live response kept an English sentence after a translated
    # greeting. Catch obvious copied spans; this is not a semantic proofreader.
    if source == target or not target.lower().startswith('zh'):
        return False
    spans = re.findall(r'[A-Za-z]+(?:[ \t]+[A-Za-z]+){2,}|[ぁ-ゟァ-ヿ]{4,}', original)
    return any(span in translation for span in spans)


def translate_texts(texts: list[str], source: str, target: str, cache_path: Path,
                    stop_event: threading.Event | None = None) -> list[str]:
    """Translate cues in their original order, persisting each successful batch.

    Requests across callers in this process share one queue and a 0.5 s minimum
    interval. Up to 16 cues / 1200 characters are batched with paired IDs; failed
    ID validation falls back to individual cues. Stop is checked between cues,
    waits, retries and in-flight requests. Each request has a 15 s total budget;
    the owned request process is reaped before cancellation returns.
    """
    _check_cancel(stop_event)
    if not source or not target:
        raise TranslationError('请指定源语言和目标语言。')
    for index, text in enumerate(texts):
        if not isinstance(text, str) or len(text) > _MAX_CUE_CHARACTERS:
            raise TranslationError(f'第 {index + 1} 条字幕无效或超过 1500 字符，请先拆分该条字幕。')
    if not texts:
        return []
    while not _queue.acquire(timeout=0.1):
        _check_cancel(stop_event)
    try:
        _check_cancel(stop_event)
        cache_path = Path(cache_path)
        cache = _load_cache(cache_path)
        mapped = {}
        pending = []
        for text in dict.fromkeys(texts):
            entry = cache['entries'].get(_key(text, source, target))
            if not text.strip():
                mapped[text] = ''
            elif entry is not None and valid_translation_content(text, entry['translation']):
                mapped[text] = entry['translation']
            else:
                pending.append(text)
        client = _Client(source, target, stop_event)

        def save(originals: list[str], translations: list[str]) -> None:
            _check_cancel(stop_event)
            if any(not valid_translation_content(original, translation)
                   for original, translation in zip(originals, translations, strict=True)):
                raise _InvalidResponse('翻译服务返回的译文缺少正文；未保存该批译文，请稍后重试。')
            for original, translation in zip(originals, translations, strict=True):
                cache['entries'][_key(original, source, target)] = {
                    'source': source, 'target': target, 'text': original, 'translation': translation,
                }
                mapped[original] = translation
            _save_cache(cache_path, cache)

        for batch in _batches(pending):
            _check_cancel(stop_event)
            translated = None
            if len(batch) > 1:
                try:
                    translated = _parse_batch(client.translate(_format_batch(batch)), len(batch))
                except _InvalidResponse:
                    pass
            if translated is not None:
                for index, original in enumerate(batch):
                    if (not valid_translation_content(original, translated[index])
                            or _looks_copied(original, translated[index], source, target)):
                        translated[index] = client.translate(original)
                save(batch, translated)
            else:
                for text in batch:
                    save([text], [client.translate(text)])
        _check_cancel(stop_event)
        return [mapped[text] for text in texts]
    finally:
        _queue.release()
