"""Azure Fast transcription, with strict timing checks and no internal retries.

The shared budget ledger owns request caching, raw JSON, retries and uncertain
network outcomes. A stop request can prevent sending; it cannot cancel work
already accepted by the remote service.
"""

from dataclasses import dataclass
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
import uuid
import wave

from .atomic_io import cleanup_temporary, replace_with_retry
from .subtitles import Cue


@dataclass
class AsrResult:
    cues: list[Cue]
    issues: list[dict]
    metadata: dict


def _milliseconds(value, field: str) -> int:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or int(value) != value):
        raise ValueError(f"{field} must be a finite nonnegative integer")
    return int(value)


def _confidence(record: dict, issues: list[dict], phrase_index: int,
                word_index: int | None = None) -> None:
    if "confidence" not in record:
        return
    value = record["confidence"]
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("confidence must be a finite number between zero and one")
    if value < 0.65:
        issue = {"reason": "low_confidence", "phrase_index": phrase_index, "confidence": value}
        if word_index is not None:
            issue["word_index"] = word_index
        issues.append(issue)


def _compact(text: str) -> str:
    # Do not remove punctuation or alter Unicode characters to force a match.
    return "".join(text.split())


def _word_cues(record: dict, cue: Cue, issues: list[dict], index: int) -> list[Cue]:
    words = record.get("words")
    if words is None or words == []:
        issues.append({"reason": "word_timing_unavailable", "phrase_index": index})
        return [cue]
    if not isinstance(words, list):
        raise ValueError("phrase words must be an array")
    parsed = []
    missing_timing = False
    missing_text = False
    previous_end = cue.start_ms
    for word_index, word in enumerate(words):
        if not isinstance(word, dict):
            raise ValueError("each word must be an object")
        start = (_milliseconds(word["offsetMilliseconds"], "word offsetMilliseconds")
                 if "offsetMilliseconds" in word else None)
        duration = (_milliseconds(word["durationMilliseconds"], "word durationMilliseconds")
                    if "durationMilliseconds" in word else None)
        if start is not None and not cue.start_ms <= start < cue.end_ms:
            raise ValueError("word start lies outside its phrase")
        if duration is not None and (duration == 0 or duration > cue.end_ms - cue.start_ms):
            raise ValueError("word duration lies outside its phrase")
        if start is not None and duration is not None:
            end = start + duration
            if start < previous_end or end > cue.end_ms:
                raise ValueError("word times are reversed, overlapping or outside their phrase")
            previous_end = end
        else:
            end = None
            missing_timing = True
        text = word.get("word", word.get("text"))
        if text is not None and not isinstance(text, str):
            raise ValueError("word text must be a string")
        if not text or not _compact(text):
            missing_text = True
        if "word" in word and "text" in word:
            if not isinstance(word["text"], str) or _compact(text or "") != _compact(word["text"]):
                missing_text = True
        _confidence(word, issues, index, word_index)
        parsed.append((start, end, text or ""))
    if missing_timing:
        issues.append({"reason": "word_timing_unavailable", "phrase_index": index})
        return [cue]
    if missing_text or _compact("".join(word[2] for word in parsed)) != _compact(cue.text):
        issues.append({"reason": "word_text_mismatch", "phrase_index": index})
        return [cue]

    # Map verified token boundaries onto the ORIGINAL phrase, preserving every
    # character, including spaces and punctuation omitted by naive joins.
    positions = [position for position, char in enumerate(cue.text) if not char.isspace()]
    token_ends = []
    count = 0
    for _, _, text in parsed:
        count += len(_compact(text))
        token_ends.append(positions[count - 1] + 1)
    token_ends[-1] = len(cue.text)
    # First identify whole sentences, so a length split can look back to a
    # clause boundary and can see whether it would leave a tiny sentence tail.
    sentence_ends = [i + 1 for i, (_, _, text) in enumerate(parsed)
                     if re.search(r"[。！？!?；;．.]\s*[\"'」』）)】]*\s*$", text)]
    if not sentence_ends or sentence_ends[-1] != len(parsed):
        sentence_ends.append(len(parsed))
    char_counts = [0]
    for _, _, text in parsed:
        char_counts.append(char_counts[-1] + len(_compact(text)))

    def fits(start, end):
        return (char_counts[end] - char_counts[start] <= 32
                and parsed[end - 1][1] - parsed[start][0] <= 6000)

    chunks = []
    group_start = 0
    char_start = 0
    for sentence_end in sentence_ends:
        while group_start < sentence_end:
            # Keep an oversized individual token intact: splitting it would
            # invent a timestamp. All other cuts use the verified word edges.
            group_end = group_start + 1
            while group_end < sentence_end and fits(group_start, group_end + 1):
                group_end += 1
            if group_end < sentence_end:
                # A comma is preferable to cutting a compound such as 被災状況.
                # Ignore tiny lead-ins/tails (e.g. 他にも、 / が可能です。).
                clause_ends = [end for end in range(group_start + 1, group_end + 1)
                    if char_counts[end] - char_counts[group_start] >= 8
                    and char_counts[sentence_end] - char_counts[end] >= 8
                    and re.search(r"[、，,：:]\s*[\"'」』）)】]*\s*$", parsed[end - 1][2])]
                if clause_ends:
                    group_end = clause_ends[-1]
                elif char_counts[sentence_end] - char_counts[group_end] < 8:
                    # No useful punctuation: rebalance the final two pieces,
                    # but only if BOTH still obey the original display limits.
                    candidates = [end for end in range(group_start + 1, group_end + 1)
                        if char_counts[end] - char_counts[group_start] >= 8
                        and char_counts[sentence_end] - char_counts[end] >= 8
                        and fits(group_start, end) and fits(end, sentence_end)]
                    if candidates:
                        group_end = min(candidates, key=lambda end: (
                            abs(2 * char_counts[end] - char_counts[group_start]
                                - char_counts[sentence_end]), -end))
            char_end = token_ends[group_end - 1]
            chunks.append(Cue(parsed[group_start][0], parsed[group_end - 1][1],
                              cue.text[char_start:char_end]))
            char_start = char_end
            group_start = group_end
    # If no boundary is needed, keep the authoritative sentence-level timing.
    return chunks if len(chunks) > 1 else [cue]


def parse_response(payload: dict, duration_ms: int) -> AsrResult:
    """Parse Fast API JSON without fabricating, clipping or deleting speech."""
    requested = _milliseconds(duration_ms, "input duration_ms")
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("Azure transcription did not return a successful object")
    if "durationMilliseconds" not in payload:
        raise ValueError("Azure transcription is missing durationMilliseconds")
    reported = _milliseconds(payload["durationMilliseconds"], "durationMilliseconds")
    if abs(reported - requested) > max(1000, requested * 0.005):
        raise ValueError("Azure transcription duration does not match the input audio")
    phrases = payload.get("phrases")
    if not isinstance(phrases, list):
        raise ValueError("Azure transcription must contain a phrases array")
    combined = payload.get("combinedPhrases", [])
    if not isinstance(combined, list):
        raise ValueError("Azure transcription combinedPhrases must be an array")
    for item in combined:
        if not isinstance(item, dict) or not isinstance(item.get("text", ""), str):
            raise ValueError("Azure combined phrase text must be a string")
        if not phrases and item.get("text", "").strip():
            raise ValueError("Azure returned recognized text without phrase timestamps")
    cues = []
    issues = []
    previous_start = 0
    for index, record in enumerate(phrases):
        if not isinstance(record, dict):
            raise ValueError("each phrase must be an object")
        start = _milliseconds(record.get("offsetMilliseconds"), "phrase offsetMilliseconds")
        duration = _milliseconds(record.get("durationMilliseconds"), "phrase durationMilliseconds")
        end = start + duration
        if duration == 0 or end > requested or end > reported or start < previous_start:
            raise ValueError("phrase times are reversed or outside the recording")
        previous_start = start
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("phrase text must be a nonempty string")
        issue_start = len(issues)
        _confidence(record, issues, index)
        cues.extend(_word_cues(record, Cue(start, end, text), issues, index))
        # Runner offsets every review item from chunk-relative to media time.
        # Even word-level doubts refer to this validated original phrase range.
        for issue in issues[issue_start:]:
            issue.update(start_ms=start, end_ms=end)
    return AsrResult(cues, issues, {"duration_ms": reported, "input_duration_ms": requested,
                                    "phrase_count": len(phrases)})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Azure credentials must never be forwarded to a redirect target.
        return None


def _endpoint_url(endpoint: str, api_version: str) -> str:
    if not isinstance(endpoint, str) or endpoint != endpoint.strip() or re.search(r"[\x00-\x20\x7f]", endpoint):
        raise ValueError("Azure endpoint must be an HTTPS resource root")
    try:
        parts = urllib.parse.urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValueError("Azure endpoint is invalid") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "?" in endpoint or "#" in endpoint
            or parts.path not in ("", "/") or port not in (None, 443)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.cognitiveservices\.azure\.com", parts.hostname or "")):
        raise ValueError("Azure endpoint must be an official cognitiveservices.azure.com HTTPS resource root")
    if not isinstance(api_version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", api_version):
        raise ValueError("Azure API version must be a date version")
    return f"https://{parts.hostname}/speechtotext/transcriptions:transcribe?api-version={api_version}"


def _copy_raw_response(source: Path, destination: Path) -> None:
    """Publish an exact local copy; a failed copy cannot invalidate paid cache."""
    if source == destination:
        return
    body = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix="." + destination.name + ".",
                                         suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        replace_with_retry(temporary, destination)
    finally:
        if temporary is not None:
            cleanup_temporary(temporary)


def transcribe(audio_path: Path, *, endpoint: str, key: str,
               api_version: str = "2025-10-15", language: str = "ja-JP", ledger,
               request_id: str, raw_path: Path | None, hourly_rate_cny: float,
               stop_event=None) -> AsrResult:
    """Submit one mono PCM WAV through the shared cache and budget ledger."""
    from .cloud_budget import CloudCancelled, HttpResponse

    if stop_event is not None and stop_event.is_set():
        raise CloudCancelled("Azure transcription cancelled before sending")
    if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", request_id, re.IGNORECASE)):
        raise ValueError("Azure request ID must be a safe stable filename")
    ledger_path = Path(ledger.path).resolve()
    audio_path = Path(audio_path).resolve()
    response_root = (ledger_path.parent / "responses").resolve()
    canonical_raw = (response_root / "azure" / (request_id + ".json")).resolve()
    destination_raw = Path(raw_path).resolve() if raw_path is not None else canonical_raw
    if destination_raw in (audio_path, ledger_path, ledger_path.with_name(ledger_path.name + ".lock")):
        raise ValueError("Azure response copy must not replace the input audio or budget ledger")
    if destination_raw.is_relative_to(response_root) and destination_raw != canonical_raw:
        raise ValueError("Azure response copy must not replace another shared paid response")
    url = _endpoint_url(endpoint, api_version)
    if not isinstance(key, str) or not key.strip() or re.search(r"[\x00-\x20\x7f]", key):
        raise ValueError("Azure subscription key is missing or invalid")
    if (isinstance(hourly_rate_cny, bool) or not isinstance(hourly_rate_cny, (int, float))
            or not math.isfinite(hourly_rate_cny) or hourly_rate_cny <= 0):
        raise ValueError("Azure hourly rate must be a finite positive number")
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})+", language):
        raise ValueError("Azure locale must be a language-region tag")
    audio_bytes = Path(audio_path).read_bytes()
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("Azure input must be 16000 Hz mono 16-bit PCM WAV")
            frames = wav.getnframes()
            if frames <= 0 or len(wav.readframes(frames + 1)) != frames * 2:
                raise ValueError("Azure input WAV is empty or truncated")
    except (wave.Error, EOFError):
        raise ValueError("Azure input must be a valid PCM WAV") from None
    duration_ms = (frames * 1000 + 15999) // 16000
    billable_seconds = (frames + 15999) // 16000
    reserved_cny = hourly_rate_cny * billable_seconds / 3600
    if not math.isfinite(reserved_cny) or reserved_cny <= 0:
        raise ValueError("Azure estimated cost must be finite and positive")
    boundary = f"codex-azure-{uuid.uuid4().hex}"
    definition = json.dumps({"locales": [language], "profanityFilterMode": "None"}).encode("utf-8")
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio.wav"\r\n'
            'Content-Type: audio/wav\r\n\r\n').encode("ascii") + audio_bytes
    body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="definition"\r\n'
             'Content-Type: application/json\r\n\r\n').encode("ascii") + definition
    body += f"\r\n--{boundary}--\r\n".encode("ascii")

    def send():
        if stop_event is not None and stop_event.is_set():
            raise CloudCancelled("Azure transcription cancelled before sending")
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        })
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=300) as result:
                return HttpResponse(result.status, dict(result.headers.items()), result.read())
        except urllib.error.HTTPError as error:
            # HTTP status codes belong to the ledger's retry/accounting policy.
            with error:
                return HttpResponse(error.code, dict(error.headers.items()), error.read())

    payload = ledger.execute(request_id=request_id, provider="azure_fast", reserved_cny=reserved_cny,
                             raw_path=canonical_raw, send=send, stop_event=stop_event)
    _copy_raw_response(canonical_raw, destination_raw)
    result = parse_response(payload, duration_ms)
    result.metadata.update(provider="azure_fast", api_version=api_version, language=language,
                           billable_seconds=billable_seconds)
    return result
