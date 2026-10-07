"""Qwen Audio 3.1 short-file synchronous ASR with conservative paid accounting.

Only the documented qwen-audio-3.1-asr-flash model is accepted. The caller owns
stable request IDs covering audio, endpoint, model and parameters. The ledger
owns original response storage, HTTP retry policy and uncertain submissions.
Azure's shared result and pure timing helpers are reused; no Azure call occurs.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave

from .atomic_io import cleanup_temporary
from .azure_asr import AsrResult, _compact, _copy_raw_response, _milliseconds, _word_cues
from .cloud_budget import CloudCancelled, CloudRequestError, HttpResponse, _file_lock, _json_object, _write_bytes
from .subtitles import Cue


_MODEL = 'qwen-audio-3.1-asr-flash'
_MAX_INPUT_TOKENS = 7168
_MAX_OUTPUT_TOKENS = 1024
_MAX_AUDIO_SECONDS = 180
_MAX_ENCODED_BYTES = 10_000_000
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class MeteringError(ValueError):
    """The provider's usage does not satisfy the verified accounting contract."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _check_cancel(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise CloudCancelled('Qwen 识别已停止；已经发出的请求记录保留。')


def _endpoint_url(endpoint: str) -> str:
    if (not isinstance(endpoint, str) or endpoint != endpoint.strip()
            or re.search(r'[\x00-\x20\x7f]', endpoint)):
        raise ValueError('Qwen Endpoint 必须是百炼北京或千问AI平台官方 HTTPS API 地址。')
    try:
        parts = urllib.parse.urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValueError('Qwen Endpoint 无效。') from None
    allowed = (parts.hostname in ('dashscope.aliyuncs.com', 'maas.qianwenaiapi.com') or re.fullmatch(
        r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.cn-beijing\.maas\.aliyuncs\.com', parts.hostname or ''))
    if (not allowed or parts.scheme != 'https' or parts.username is not None or parts.password is not None
            or port not in (None, 443) or parts.path not in ('/api/v1', '/api/v1/')
            or '?' in endpoint or '#' in endpoint):
        raise ValueError('Qwen Endpoint 仅允许百炼北京或千问AI平台官方 /api/v1 地址。')
    return f'https://{parts.hostname}/api/v1/services/aigc/multimodal-generation/generation'


def _usage_tokens(payload) -> tuple[int, int] | None:
    usage = payload.get('usage') if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = usage.get('input_tokens'), usage.get('output_tokens')
    if any(type(value) is not int or value < 0 for value in counts):
        return None
    return counts


def _billing_marker(path: Path, request_id: str, model: str, error: MeteringError, payload) -> None:
    """Persist an accounting stop; never clear or replace an existing review."""
    if path.exists():
        return
    tokens = _usage_tokens(payload)
    contents = {'provider': 'qwen_asr', 'model': model, 'request_id': request_id,
                'reason': str(error), 'usage': None if tokens is None else {
                    'input_tokens': tokens[0], 'output_tokens': tokens[1]}}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.',
                                         suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            output.write(json.dumps(contents, ensure_ascii=False).encode('utf-8'))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # Another worker has already recorded the first anomaly.
    finally:
        if temporary is not None:
            cleanup_temporary(temporary)


def _usage(payload, duration_ms: int, issues: list) -> tuple[dict | None, int | None]:
    usage = payload.get('usage')
    tokens = _usage_tokens(payload)
    if usage is not None and not isinstance(usage, dict):
        raise MeteringError('Qwen 计量异常：usage 不是对象，请检查原始响应。')
    if isinstance(usage, dict) and any(name in usage for name in ('input_tokens', 'output_tokens', 'total_tokens')):
        if tokens is None:
            raise MeteringError('Qwen 计量异常：Token 用量缺失或无效，请检查原始响应。')
        input_tokens, output_tokens = tokens
        if input_tokens > _MAX_INPUT_TOKENS or output_tokens > _MAX_OUTPUT_TOKENS:
            raise MeteringError('Qwen 计量异常：用量超过已核实的单次 Token 上限，停止后续识别。')
        if output_tokens >= _MAX_OUTPUT_TOKENS:
            raise MeteringError('Qwen 输出达到 Token 上限，存在截断疑点；请检查原始响应。')
        if 'total_tokens' in usage and (type(usage['total_tokens']) is not int
                or usage['total_tokens'] != input_tokens + output_tokens):
            raise MeteringError('Qwen 计量异常：总 Token 数与输入、输出不一致。')
    if tokens is None:
        issues.append({'reason': 'usage_unavailable', 'start_ms': 0, 'end_ms': duration_ms})
    reported_duration = None
    if isinstance(usage, dict) and 'duration' in usage:
        seconds = _milliseconds(usage['duration'], 'usage.duration')
        reported_duration = seconds * 1000
        if reported_duration <= 0 or reported_duration > duration_ms + 1000:
            raise ValueError('Qwen 返回的处理时长与输入音频不一致。')
        # The documented integer seconds describe processed audio, not an
        # exact WAV-length echo. A shorter value cannot prove missing speech.
        # Preserve it for review; word ranges still use the actual WAV length.
        if duration_ms - reported_duration > 1000:
            issues.append({'reason':'usage_duration_differs', 'start_ms':0,
                           'end_ms':duration_ms, 'provider_duration_ms':reported_duration})
    token_usage = None if tokens is None else {
        'input_tokens': tokens[0], 'output_tokens': tokens[1], 'total_tokens': sum(tokens)}
    return token_usage, reported_duration


def _sentence_records(output: dict) -> list[dict]:
    if 'sentences' in output:
        records = output['sentences']
        if not isinstance(records, list) or not records or any(not isinstance(record, dict) for record in records):
            raise ValueError('Qwen 未返回完整句子记录。')
        # Some response shapes include both the accumulated list and a current
        # sentence. It must be one of that list, not an unaccounted extra cue.
        if 'sentence' in output and output['sentence'] not in records:
            raise ValueError('Qwen 当前句与完整句子列表不一致。')
        return records
    if not isinstance(output.get('sentence'), dict):
        raise ValueError('Qwen 未返回完整句级时间戳。')
    return [output['sentence']]


def parse_response(payload: dict, duration_ms: int, *, allow_draft_timing: bool = False) -> AsrResult:
    """Accept only final timestamps that account for the entire recognized text."""
    duration_ms = _milliseconds(duration_ms, 'input duration_ms')
    if duration_ms <= 0:
        raise ValueError('Qwen 输入音频时长必须大于零。')
    if not isinstance(payload, dict) or 'error' in payload or payload.get('code'):
        raise ValueError('Qwen 未返回成功的识别对象。')
    output = payload.get('output')
    if not isinstance(output, dict) or not isinstance(output.get('text'), str):
        raise ValueError('Qwen 缺少完整 output.text，不能将缺失结果视为静音。')
    issues = []
    token_usage, reported_duration = _usage(payload, duration_ms, issues)
    records = _sentence_records(output)
    for record in records:
        if record.get('sentence_end') is not True or not isinstance(record.get('text'), str):
            raise ValueError('Qwen 句子尚未完整结束。')
        if 'channel_id' in record and (type(record['channel_id']) is not int or record['channel_id'] != 0):
            raise ValueError('Qwen 返回了未请求的音轨。')
    full_text = output['text']
    if not _compact(full_text):
        if (reported_duration is None or abs(reported_duration - duration_ms) > 1000
                or len(records) != 1
                or records[0].get('words') != [] or _compact(records[0]['text'])):
            raise ValueError('Qwen 空识别缺少明确完成证据，不能视为静音。')
        return AsrResult([], issues, {'duration_ms': duration_ms, 'input_duration_ms': duration_ms,
            'phrase_count': 0, 'token_usage': token_usage, 'confirmed_silence': True,
            'provider_duration_ms': reported_duration})

    cues = []
    complete_texts = []
    previous_end = 0
    sentence_ids = set()
    for index, record in enumerate(records):
        text = record['text']
        if not _compact(text):
            raise ValueError('Qwen 在非空识别中返回空句子。')
        if 'sentence_id' in record:
            sentence_id = record['sentence_id']
            if type(sentence_id) is not int or sentence_id < 1 or sentence_id in sentence_ids:
                raise ValueError('Qwen 句子 ID 无效或重复。')
            sentence_ids.add(sentence_id)
        start = _milliseconds(record.get('begin_time'), 'sentence.begin_time')
        end = _milliseconds(record.get('end_time'), 'sentence.end_time')
        if not previous_end <= start < end <= duration_ms:
            raise ValueError('Qwen 句级时间戳重叠、倒置或超过音频范围。')
        previous_end = end
        converted = {'text': text}
        words = record.get('words')
        if words is not None and words != []:
            if not isinstance(words, list):
                raise ValueError('Qwen words 必须是列表。')
            converted_words = []
            previous_word_end = start
            invalid_word_timing = False
            for item in words:
                if not isinstance(item, dict) or item.get('fixed') is not True:
                    raise ValueError('Qwen 词级时间戳尚未固定。')
                word_start = _milliseconds(item.get('begin_time'), 'word.begin_time')
                word_end = _milliseconds(item.get('end_time'), 'word.end_time')
                if not previous_word_end <= word_start < word_end <= end:
                    if allow_draft_timing is not True:
                        raise ValueError('Qwen 词级时间戳重叠、倒置或超过句子范围。')
                    invalid_word_timing = True
                previous_word_end = word_end
                word_text, punctuation = item.get('text'), item.get('punctuation', '')
                if not isinstance(word_text, str) or not _compact(word_text) or not isinstance(punctuation, str):
                    raise ValueError('Qwen 词文本或标点无效。')
                converted_words.append({'word': word_text + punctuation,
                    'offsetMilliseconds': word_start, 'durationMilliseconds': word_end - word_start})
            word_text = ''.join(item['word'] for item in converted_words)
            if text.startswith('。') and not text.startswith('。。') and word_text == text[1:]:
                # Some final responses add one sentence-leading period absent
                # from both the fixed words and the full transcript. Normalize
                # only this exact discrepancy without changing the raw payload.
                text = text[1:]
                converted['text'] = text
                issues.append({'reason':'leading_sentence_period_normalized',
                    'phrase_index':index,'start_ms':start,'end_ms':end})
            if _compact(word_text) != _compact(text):
                raise ValueError('Qwen 词级记录未完整覆盖识别文本。')
            if invalid_word_timing:
                issues.append({'reason':'invalid_word_timing_sentence_fallback',
                    'phrase_index':index,'start_ms':start,'end_ms':end,'requires_review':True})
            else:
                converted['words'] = converted_words
        complete_texts.append(text)
        issue_start = len(issues)
        cues.extend(_word_cues(converted, Cue(start, end, text), issues, index))
        for issue in issues[issue_start:]:
            issue.update(start_ms=start, end_ms=end)
    if _compact(''.join(complete_texts)) != _compact(full_text):
        raise ValueError('Qwen 句级时间戳未覆盖完整 output.text，不能丢弃先前句子。')
    return AsrResult(cues, issues, {'duration_ms': duration_ms, 'input_duration_ms': duration_ms,
        'phrase_count': len(records), 'token_usage': token_usage, 'confirmed_silence': False,
        'provider_duration_ms': reported_duration,
        'draft_timing_requires_review':any(i['reason']=='invalid_word_timing_sentence_fallback' for i in issues)})


def _read_audio(audio_path: Path) -> tuple[bytes, int]:
    # Precheck before reading a potentially large file; check again after the
    # read so a concurrently enlarged file cannot bypass the encoded limit.
    if audio_path.stat().st_size * 4 // 3 >= _MAX_ENCODED_BYTES:
        raise ValueError('Qwen Base64 音频必须小于 10 MB。')
    audio = audio_path.read_bytes()
    if ((len(audio) + 2) // 3) * 4 + len('data:audio/wav;base64,') >= _MAX_ENCODED_BYTES:
        raise ValueError('Qwen Base64 音频必须小于 10 MB。')
    try:
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError('Qwen 输入必须是 16 kHz 单声道 16-bit PCM WAV。')
            frames = wav.getnframes()
            if frames <= 0 or frames > _MAX_AUDIO_SECONDS * 16000:
                raise ValueError('Qwen 本轮单次音频须大于零且不超过 180 秒。')
            if len(wav.readframes(frames + 1)) != frames * 2:
                raise ValueError('Qwen 输入 WAV 已截断。')
    except (wave.Error, EOFError):
        raise ValueError('Qwen 输入不是有效 PCM WAV。') from None
    return audio, (frames * 1000 + 15999) // 16000


def _empty_draft_rejection(ledger, request_id, canonical_raw, reserved_cny):
    """Read only one exact, integrity-bound terminal error; never alter billing."""
    try:
        with _file_lock(ledger.lock_path):
            record = ledger._load()['requests'].get(request_id)
            if (not isinstance(record, dict) or record.get('status') != 'rejected'
                    or record.get('provider') != 'qwen_asr'
                    or Path(record['raw_path']).resolve() != canonical_raw
                    or record.get('reserved_cny') != reserved_cny):
                return None
            attempt = record['attempts'][-1]
            if attempt.get('status') != 'rejected' or attempt.get('http_status') != 400:
                return None
            expected = canonical_raw.with_name(canonical_raw.name +
                f".attempt-{attempt['number']:02d}.http-400.body")
            if Path(attempt['raw_path']).resolve() != expected:
                return None
            with expected.open('rb') as source:
                body = source.read(_MAX_RESPONSE_BYTES + 1)
            if (len(body) > _MAX_RESPONSE_BYTES
                    or hashlib.sha256(body).hexdigest() != attempt.get('raw_sha256')):
                return None
            payload = _json_object(body)
            if (payload.get('code') != 'CLIENT_ERROR'
                    or payload.get('message') != 'ASR_RESPONSE_HAVE_NO_WORDS'):
                return None
            return body
    except (CloudRequestError, OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def transcribe(audio_path: Path, *, endpoint: str = 'https://dashscope.aliyuncs.com/api/v1',
               key: str, model: str = _MODEL, ledger, request_id: str, raw_path: Path | None,
               input_rate_cny_per_million: float = 0.8, output_rate_cny_per_million: float = 2.7,
               stop_event=None, allow_empty_draft: bool = False, language: str = 'ja',
               before_submit=None) -> AsrResult:
    """Perform one synchronous request; all retries and raw reuse belong to ledger."""
    _check_cancel(stop_event)
    from .languages import validate_languages
    validate_languages(language,'zh-CN')
    if (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', request_id)
            or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', request_id, re.IGNORECASE)):
        raise ValueError('Qwen 请求 ID 必须是安全、稳定的文件名。')
    if model != _MODEL:
        raise ValueError('本适配器仅支持已核价的 qwen-audio-3.1-asr-flash。')
    url = _endpoint_url(endpoint)
    if not isinstance(key, str) or not key or re.search(r'[\x00-\x20\x7f]', key):
        raise ValueError('Qwen API Key 缺失或格式无效。')
    rates = input_rate_cny_per_million, output_rate_cny_per_million
    if any(isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not math.isfinite(rate) or rate <= 0 for rate in rates):
        raise ValueError('Qwen Token 单价须为有限正数。')
    audio_path = Path(audio_path).resolve()
    ledger_path = Path(ledger.path).resolve()
    canonical_raw = ledger_path.parent / 'responses' / 'qwen' / (request_id + '.json')
    billing_marker = ledger_path.parent / 'billing-review-required.json'
    destination = Path(raw_path).resolve() if raw_path is not None else canonical_raw
    if destination in (audio_path, ledger_path, ledger_path.with_name(ledger_path.name + '.lock'), billing_marker):
        raise ValueError('Qwen 原始响应副本不能覆盖音频或费用账本。')
    if destination.is_relative_to(ledger_path.parent / 'responses') and destination != canonical_raw:
        raise ValueError('Qwen 原始响应副本不能覆盖其他请求的共享缓存。')
    if billing_marker.exists() and not canonical_raw.is_file():
        raise MeteringError('Qwen 计量异常尚待人工复核，已禁止新的收费识别请求。')
    audio, duration_ms = _read_audio(audio_path)
    body = json.dumps({'model': model,
        'input': {'messages': [{'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {
            'data': 'data:audio/wav;base64,' + base64.b64encode(audio).decode('ascii')}}]}]},
        'parameters': {'format': 'wav', 'sample_rate': '16000', 'language_hints': [language]}},
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    reserved = (_MAX_INPUT_TOKENS * rates[0] + _MAX_OUTPUT_TOKENS * rates[1]) / 1_000_000
    if not math.isfinite(reserved) or reserved <= 0:
        raise ValueError('Qwen 费用预留超出有效范围。')

    def send():
        _check_cancel(stop_event)
        if billing_marker.exists():
            raise CloudCancelled('Qwen 计量异常尚待人工复核，请求尚未发送。')
        request = urllib.request.Request(url, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
            'Accept': 'application/json', 'X-DashScope-SSE': 'disable'})
        try:
            result = _open(request, timeout=300)
        except urllib.error.HTTPError as error:
            result = error
        with result:
            content = result.read(_MAX_RESPONSE_BYTES + 1)
            if len(content) > _MAX_RESPONSE_BYTES:
                raise ValueError('Qwen 响应超过安全大小，请核对请求记录。')
            status = result.code if isinstance(result, urllib.error.HTTPError) else result.status
            return HttpResponse(status, dict(result.headers), content)
        # CloudCancelled must never be raised after a request may have been sent.

    def actual_cost(payload):
        tokens = _usage_tokens(payload)
        if tokens is None:
            return None
        usage = payload['usage']
        if 'total_tokens' in usage and (type(usage['total_tokens']) is not int
                or usage['total_tokens'] != sum(tokens)):
            return None
        # Record observed use even when it violates the advertised limits. The
        # parser subsequently raises MeteringError rather than hiding the cost.
        return (tokens[0] * rates[0] + tokens[1] * rates[1]) / 1_000_000

    try:
        payload = ledger.execute(request_id=request_id, provider='qwen_asr', reserved_cny=reserved,
            raw_path=canonical_raw, send=send, stop_event=stop_event, actual_cost=actual_cost,
            **({'before_submit':before_submit} if before_submit is not None else {}))
    except CloudRequestError as error:
        if allow_empty_draft is not True or type(error) is not CloudRequestError:
            raise
        body = _empty_draft_rejection(ledger, request_id, canonical_raw, reserved)
        if body is None:
            raise
        if raw_path is not None:
            # Copy exactly the bytes verified above; do not turn the rejected
            # ledger entry or its original HTTP 400 body into a success.
            _write_bytes(destination, body)
        _check_cancel(stop_event)
        return AsrResult([], [{'reason':'识别服务未返回字词，请核对是否有低声或语气词；未认定为静音',
            'start_ms':0,'end_ms':duration_ms,'requires_review':True}], {
            'duration_ms':duration_ms,'input_duration_ms':duration_ms,'phrase_count':0,
            'token_usage':None,'provider_duration_ms':None,'confirmed_silence':False,
            'empty_recognition_requires_review':True,'requires_review':True,
            'provider':'qwen_asr','model':model,'language':language,
            'input_token_limit':_MAX_INPUT_TOKENS,'output_token_limit':_MAX_OUTPUT_TOKENS})
    try:
        try:
            result = parse_response(payload, duration_ms, allow_draft_timing=allow_empty_draft)
        except MeteringError as error:
            try:
                _billing_marker(billing_marker, request_id, model, error, payload)
            finally:
                if stop_event is not None:
                    stop_event.set()
            raise
    except Exception:
        # The ledger already preserved the canonical paid response. A failure
        # to publish a secondary copy must not hide its validation/billing error.
        try:
            _copy_raw_response(canonical_raw, destination)
        except OSError:
            pass
        raise
    _copy_raw_response(canonical_raw, destination)
    _check_cancel(stop_event)
    result.metadata.update(provider='qwen_asr', model=model, language=language,
                           input_token_limit=_MAX_INPUT_TOKENS, output_token_limit=_MAX_OUTPUT_TOKENS)
    return result
