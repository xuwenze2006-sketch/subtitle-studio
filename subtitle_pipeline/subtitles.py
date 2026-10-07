"""Subtitle text and absolute timeline operations, without I/O or model calls.

Recognition timestamps are relative to each chunk's *audio* start. Core ranges
partition the media; context outside a core helps recognition but does not own
cues. All time values in this module are integer milliseconds.
"""

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Chunk:
    index: int
    core_start_ms: int
    core_end_ms: int
    audio_start_ms: int
    audio_end_ms: int


_STAMP = r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})"
_TIMING = re.compile(rf"^\s*{_STAMP}\s*-->\s*{_STAMP}\s*$")


def _timestamp(groups: tuple[str, ...]) -> int:
    hours, minutes, seconds, milliseconds = map(int, groups)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def parse_srt(text: str) -> list[Cue]:
    """Parse numbered SRT blocks; reject malformed blocks instead of losing text.

    BOM, CRLF, and dot-separated milliseconds are accepted. Empty input is a
    valid empty result (for example, a silent audio chunk).
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    cues = []
    for block_index, block in enumerate(re.split(r"\n[ \t]*\n+", text), 1):
        lines = block.split("\n")
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError(f"Invalid SRT block {block_index}: missing index, timing or text")
        timing = _TIMING.fullmatch(lines[1])
        if timing is None:
            raise ValueError(f"Invalid SRT timing in block {block_index}")
        groups = timing.groups()
        cues.append(Cue(_timestamp(groups[:4]), _timestamp(groups[4:]), "\n".join(lines[2:])))
    return cues


def _format_timestamp(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def render_srt(cues: Iterable[Cue]) -> str:
    """Render UTF-8-compatible SRT text; call check_cues to repair bad ranges."""
    blocks = []
    for index, cue in enumerate(cues, 1):
        if cue.start_ms < 0 or cue.end_ms <= cue.start_ms:
            raise ValueError(f"Invalid cue time range at index {index}")
        if not cue.text.strip():
            raise ValueError(f"Empty cue text at index {index}")
        body = cue.text.replace("\r\n", "\n").replace("\r", "\n")
        blocks.append(f"{index}\n{_format_timestamp(cue.start_ms)} --> {_format_timestamp(cue.end_ms)}\n{body}")
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


def _nonnegative_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def plan_chunks(
    duration_ms: int,
    chunk_ms: int = 300_000,
    overlap_ms: int = 2000,
    silences: Iterable[tuple[int, int]] | None = None,
) -> list[Chunk]:
    """Partition media into contiguous cores with context on both sides.

    Near each nominal boundary, prefer a silence midpoint within 10 seconds
    (or 10% of a shorter chunk). Context is clipped only at the media ends.
    Silence intervals beyond media bounds are clipped or ignored.
    """
    _nonnegative_integer(duration_ms, "duration_ms")
    _nonnegative_integer(chunk_ms, "chunk_ms")
    _nonnegative_integer(overlap_ms, "overlap_ms")
    if chunk_ms == 0 or overlap_ms >= chunk_ms:
        raise ValueError("chunk_ms must be positive and overlap_ms must be smaller")
    silence_midpoints = []
    for start, end in silences or ():
        _nonnegative_integer(start, "silence start")
        _nonnegative_integer(end, "silence end")
        if start > end:
            raise ValueError("Silence start must not exceed end")
        if start < duration_ms and end > start:
            silence_midpoints.append((start + min(end, duration_ms)) // 2)
    tolerance = min(10_000, chunk_ms // 10)
    chunks = []
    start = 0
    while start < duration_ms:
        nominal = min(start + chunk_ms, duration_ms)
        end = nominal
        if nominal < duration_ms:
            candidates = [mid for mid in silence_midpoints
                          if start < mid < duration_ms and abs(mid - nominal) <= tolerance]
            if candidates:
                end = min(candidates, key=lambda mid: (abs(mid - nominal), mid))
        chunks.append(Chunk(len(chunks), start, end, max(0, start - overlap_ms),
                            min(duration_ms, end + overlap_ms)))
        start = end
    return chunks


def _text_identity(text: str) -> str:
    # Whitespace variation alone does not distinguish two ASR observations.
    return " ".join(text.split())


def _boundary_duplicate(first: Cue, second: Cue) -> bool:
    if _text_identity(first.text) != _text_identity(second.text):
        return False
    intersection = min(first.end_ms, second.end_ms) - max(first.start_ms, second.start_ms)
    shorter = min(first.end_ms - first.start_ms, second.end_ms - second.start_ms)
    return (intersection > 0 and intersection * 3 >= shorter * 2
            and abs(first.start_ms - second.start_ms) <= 1000
            and abs(first.end_ms - second.end_ms) <= 1000)


def merge_chunk_cues(chunk_cues: Iterable[tuple[Chunk, Iterable[Cue]]]) -> list[Cue]:
    """Offset by audio start, retain midpoint-owned cues, and sort globally.

    Core ownership is half-open, so a midpoint exactly on a boundary belongs
    to the following chunk. Genuine nonoverlapping repeated dialogue is kept.
    Only near-identical timing and text from adjacent chunks is deduplicated.
    """
    owned = []
    for chunk, cues in chunk_cues:
        for cue in cues:
            start = max(chunk.audio_start_ms, chunk.audio_start_ms + cue.start_ms)
            end = min(chunk.audio_end_ms, chunk.audio_start_ms + cue.end_ms)
            if end <= start:
                continue
            midpoint_twice = start + end
            if 2 * chunk.core_start_ms <= midpoint_twice < 2 * chunk.core_end_ms:
                owned.append((Cue(start, end, cue.text), chunk.index))
    owned.sort(key=lambda item: (item[0].start_ms, item[0].end_ms, item[1]))
    merged: list[tuple[Cue, int]] = []
    for cue, chunk_index in owned:
        duplicate = False
        for previous, previous_index in reversed(merged):
            # The timing similarity rule cannot match a start farther away.
            if previous.start_ms < cue.start_ms - 1000:
                break
            if abs(previous_index - chunk_index) == 1 and _boundary_duplicate(previous, cue):
                duplicate = True
                break
        if not duplicate:
            merged.append((cue, chunk_index))
    return [cue for cue, _ in merged]


def check_cues(cues: Iterable[Cue], duration_ms: int | None = None) -> tuple[list[Cue], list[dict]]:
    """Clean whitespace/ranges and flag review needs without rewriting speech.

    Invalid/empty cues and exact duplicate records are removed with a report.
    Overlaps, rapid text, long cues and close repeated text are review flags,
    never grounds for semantic changes. Issue indices refer to original input.
    """
    if duration_ms is not None:
        _nonnegative_integer(duration_ms, "duration_ms")
    cleaned: list[tuple[Cue, int]] = []
    issues: list[dict] = []
    seen = set()

    def report(index: int, cue: Cue, reason: str) -> None:
        issues.append({"index": index, "start_ms": cue.start_ms,
                       "end_ms": cue.end_ms, "reason": reason})

    for index, cue in enumerate(cues, 1):
        body = cue.text.replace("\r\n", "\n").replace("\r", "\n")
        body = "\n".join(line.strip() for line in body.split("\n") if line.strip())
        start, end = max(0, cue.start_ms), max(0, cue.end_ms)
        if duration_ms is not None:
            start, end = min(start, duration_ms), min(end, duration_ms)
        if (start, end) != (cue.start_ms, cue.end_ms):
            report(index, cue, "时间超出媒体范围，已裁到有效范围")
        if end <= start:
            report(index, cue, "结束时间不晚于开始时间，已移除；请结合原音复核")
            continue
        if not body:
            report(index, cue, "空白字幕已移除")
            continue
        valid = Cue(start, end, body)
        if valid in seen:
            report(index, cue, "时间和文字完全相同的重复记录已移除")
            continue
        seen.add(valid)
        length = end - start
        visible_count = len(re.sub(r"\s", "", body))
        if length > 12_000:
            report(index, valid, "单条持续超过12秒，请检查是否需要拆句或是否出现幻觉")
        if visible_count * 1000 > length * 20:
            report(index, valid, "字幕显示速度超过每秒20字，请核对识别和时间轴")
        if visible_count > 84 or body.count("\n") > 1:
            report(index, valid, "字幕较长或超过两行，请检查阅读负担")
        cleaned.append((valid, index))
    cleaned.sort(key=lambda item: (item[0].start_ms, item[0].end_ms))
    for (previous, _), (cue, index) in zip(cleaned, cleaned[1:]):
        if cue.start_ms < previous.end_ms:
            report(index, cue, "与前一条字幕时间重叠，保留原文供复核")
        if (0 <= cue.start_ms - previous.end_ms <= 2000
                and _text_identity(previous.text) == _text_identity(cue.text)):
            report(index, cue, "邻近字幕文字相同，可能是真实重复或识别重复，已保留")
    return [cue for cue, _ in cleaned], issues
