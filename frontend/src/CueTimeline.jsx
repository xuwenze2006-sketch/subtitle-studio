import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Pencil, Play } from "lucide-react";
import { timeLabel } from "./ui";
import { reviewStatusLabel } from "./ManualReview";

const ESTIMATED_HEIGHT = 104;
const OVERSCAN = 5;

function preciseTime(value) {
  const ms = Math.max(0, Math.round(Number(value) || 0));
  return `${timeLabel(ms)}.${String(ms % 1000).padStart(3, "0")}`;
}

// Last row starting at/before this offset. Heights are measured, not truncated.
function rowAt(offsets, top) {
  let low = 0, high = Math.max(0, offsets.length - 2);
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (offsets[middle] <= top) low = middle;
    else high = middle - 1;
  }
  return low;
}

function revealRow(element, row) {
  if (!row) return false;
  const bounds = row.getBoundingClientRect(), box = element.getBoundingClientRect();
  if (bounds.bottom <= bounds.top || box.bottom <= box.top) return false;
  if (bounds.bottom - bounds.top > box.bottom - box.top || bounds.top < box.top) element.scrollTop += bounds.top - box.top;
  else if (bounds.bottom > box.bottom) element.scrollTop += bounds.bottom - box.bottom;
  return true;
}

const CueRow = memo(function CueRow({ entry, active, offset, canPlay, seek, replay, onEdit, top }) {
  const { cue, index } = entry;
  const start = preciseTime(cue.start_ms + offset);
  return <div className={`cue-row ${active ? "active-cue" : ""}`} data-cue-index={index}
    style={{ position: "absolute", top, left: 0 }}>
    <button className="cue-seek" onClick={() => seek(cue)} disabled={!canPlay}
      aria-current={active ? "true" : undefined} aria-label={`跳转到 ${start} ${cue.ja || cue.zh}`}>
      <div className="cue-index">{String(index + 1).padStart(2, "0")}</div>
      <div>
        <time>{start} <span>→ {preciseTime(cue.end_ms + offset)}</span></time>
        {cue.ja && <p className="cue-original">{cue.ja}</p>}
        {cue.zh && <p className="cue-translation">{cue.zh}</p>}
      </div>
    </button>
    <button className="cue-replay" aria-label={`播放第 ${index + 1} 句`} title="播放本句，结束后暂停"
      disabled={!canPlay} onClick={() => replay(cue)}><Play size={15} aria-hidden="true" /></button>
    {onEdit && <div className="cue-review-controls"><button className="cue-edit" aria-label={`编辑第 ${index + 1} 句`} title="编辑文字、时间与核对状态" onClick={() => onEdit(cue)}><Pencil size={14} aria-hidden="true" /></button><span className={`cue-review-state ${cue.translation_stale ? "stale" : cue.review_status || "unchecked"}`}>{cue.translation_stale ? "译文待核对" : reviewStatusLabel(cue.review_status)}</span></div>}
  </div>;
});

export default memo(function CueTimeline({ entries, active, offset, canPlay, seek, replay, onEdit, children,
  following = true, onManualBrowse, locateRequest, viewMemory, viewKey, ready = true }) {
  const list = useRef(null);
  const pendingFocus = useRef(null);
  const pendingActive = useRef(null);
  const pendingRestore = useRef(null);
  const lastLocate = useRef(locateRequest);
  const observedTop = useRef(0);
  const [viewport, setViewport] = useState({ top: 0, height: 455 });
  const [heights, setHeights] = useState(() => new Map());
  const positions = useMemo(() => new Map(entries.map((entry, position) => [entry.index, position])), [entries]);
  const offsets = useMemo(() => {
    const result = [0];
    for (const entry of entries) result.push(result.at(-1) + (heights.get(entry.cue) || ESTIMATED_HEIGHT));
    return result;
  }, [entries, heights]);
  const total = offsets.at(-1);
  const top = Math.min(viewport.top, Math.max(0, total - viewport.height));
  const start = Math.max(0, rowAt(offsets, top) - OVERSCAN);
  const end = Math.min(entries.length, rowAt(offsets, top + viewport.height) + OVERSCAN + 1);

  const readViewport = useCallback(() => {
    const element = list.current;
    if (!element) return;
    const next = { top: element.scrollTop, height: element.clientHeight || 455 };
    observedTop.current = next.top;
    setViewport(previous => previous.top === next.top && previous.height === next.height ? previous : next);
  }, []);

  // Store a sentence anchor rather than pixels: the two layouts have different
  // widths and row heights. Loading placeholders must not erase this memory.
  useLayoutEffect(() => {
    const saved = !following && ready && viewMemory?.current?.key === viewKey ? viewMemory.current : null;
    // Editing timing or inserting a preceding row does not change sentence
    // identity. Older memories without an ID retain the stricter time match.
    const position = saved ? entries.findIndex(entry => saved.cueId != null
      ? entry.cue.id === saved.cueId
      : entry.index === saved.index && entry.cue.start_ms === saved.startMs) : -1;
    pendingRestore.current = position >= 0 ? { index: entries[position].index, fraction: saved.fraction } : null;
    list.current.scrollTop = position >= 0 ? offsets[position] + saved.fraction * (offsets[position + 1] - offsets[position]) : 0;
    readViewport();
    pendingFocus.current = null;
    pendingActive.current = null;
    setHeights(previous => {
      const next = new Map();
      for (const { cue } of entries) if (previous.has(cue)) next.set(cue, previous.get(cue));
      return next;
    });
  }, [entries, viewKey, ready, readViewport]);

  useLayoutEffect(() => {
    const explicit = locateRequest !== lastLocate.current;
    lastLocate.current = locateRequest;
    const wanted = explicit && locateRequest ? locateRequest.index : following ? active : pendingActive.current;
    const position = positions.get(wanted);
    const element = list.current;
    if (position == null || !element) { pendingActive.current = null; return; }
    pendingRestore.current = null;
    if (revealRow(element, element.querySelector(`[data-cue-index="${wanted}"]`))) {
      pendingActive.current = null;
      readViewport();
      return;
    }
    pendingActive.current = wanted;
    const rowTop = offsets[position], rowBottom = offsets[position + 1];
    const height = element.clientHeight || viewport.height;
    if (rowTop < element.scrollTop) element.scrollTop = rowTop;
    else if (rowBottom > element.scrollTop + height) element.scrollTop = Math.max(0, rowBottom - height);
    readViewport();
    // Playback may update the highlight while the user browses elsewhere.
    // An explicit seek still reveals its target without reenabling follow mode.
  }, [active, entries, following, locateRequest, offsets, viewport.height, readViewport]);

  useLayoutEffect(() => {
    const element = list.current;
    let frame = 0;
    const measure = () => {
      frame = 0;
      const rows = [...element.querySelectorAll('.cue-row')];
      const changes = [];
      for (const row of rows) {
        const position = positions.get(Number(row.dataset.cueIndex));
        const height = row.getBoundingClientRect().height;
        if (height > 0 && position != null && Math.abs((heights.get(entries[position].cue) || ESTIMATED_HEIGHT) - height) > 0.5)
          changes.push([position, height]);
      }
      if (changes.length) {
        const anchor = rowAt(offsets, element.scrollTop);
        const adjustment = changes.reduce((sum, [position, height]) => sum + (position < anchor ? height - (offsets[position + 1] - offsets[position]) : 0), 0);
        setHeights(previous => {
          const next = new Map(previous);
          for (const [position, height] of changes) next.set(entries[position].cue, height);
          return next;
        });
        if (adjustment) element.scrollTop += adjustment;
      }
      readViewport();
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      if (!frame) frame = requestAnimationFrame(measure);
    });
    observer?.observe(element);
    element.querySelectorAll('.cue-row').forEach(row => observer?.observe(row));
    return () => { observer?.disconnect(); if (frame) cancelAnimationFrame(frame); };
  }, [entries, start, end, heights, offsets, positions, readViewport]);

  useLayoutEffect(() => {
    const element = list.current;
    const saved = pendingRestore.current;
    if (saved) {
      const position = positions.get(saved.index);
      if (position != null) {
        element.scrollTop = offsets[position] + saved.fraction * (offsets[position + 1] - offsets[position]);
        if (heights.has(entries[position].cue)) pendingRestore.current = null;
        readViewport();
      } else pendingRestore.current = null;
    }
    const wanted = pendingFocus.current?.index ?? pendingActive.current;
    if (wanted == null) return;
    const row = element.querySelector(`[data-cue-index="${wanted}"]`);
    if (!row) return;
    revealRow(element, row);
    if (pendingFocus.current) {
      row.querySelector(pendingFocus.current.control)?.focus({ preventScroll: true });
      pendingFocus.current = null;
    }
    pendingActive.current = null;
    readViewport();
  });

  useLayoutEffect(() => {
    if (!ready || !entries.length || !viewMemory || pendingRestore.current) return;
    const position = rowAt(offsets, list.current.scrollTop);
    const entry = entries[position];
    viewMemory.current = { key: viewKey, cueId: entry.cue.id, index: entry.index, startMs: entry.cue.start_ms,
      fraction: Math.max(0, Math.min(1, (list.current.scrollTop - offsets[position]) / (offsets[position + 1] - offsets[position]))) };
  });

  const manualBrowse = () => {
    pendingRestore.current = null;
    pendingActive.current = null;
    onManualBrowse?.();
  };
  const handleScroll = () => {
    // Internal positioning records its scrollTop synchronously. A later,
    // different offset comes from the user (including native scrollbar drags).
    if (Math.abs(list.current.scrollTop - observedTop.current) > 1) manualBrowse();
    readViewport();
  };
  const moveFocus = event => {
    if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'PageUp'].includes(event.key) ||
      (event.key === ' ' && !event.target.closest?.('button'))) manualBrowse();
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const row = event.target.closest?.('.cue-row');
    if (!row || !entries.length) return;
    event.preventDefault();
    const position = positions.get(Number(row.dataset.cueIndex));
    const target = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1
      : Math.max(0, Math.min(entries.length - 1, position + (event.key === 'ArrowDown' ? 1 : -1)));
    const control = event.target.classList.contains('cue-replay') ? '.cue-replay' : event.target.classList.contains('cue-edit') ? '.cue-edit' : '.cue-seek';
    const targetRow = list.current.querySelector(`[data-cue-index="${entries[target].index}"]`);
    if (targetRow) {
      revealRow(list.current, targetRow);
      targetRow.querySelector(control)?.focus({ preventScroll: true });
      readViewport();
      return;
    }
    pendingFocus.current = { index: entries[target].index, control };
    list.current.scrollTop = offsets[target];
    readViewport();
  };

  return <div ref={list} className="cue-list" aria-label="字幕时间轴" onScroll={handleScroll} onKeyDown={moveFocus}
    onWheel={event => { if (event.deltaY && list.current.scrollHeight > list.current.clientHeight) manualBrowse(); }}
    onTouchMove={manualBrowse}>
    {entries.length ? <div className="cue-timeline-space" style={{ height: total, position: 'relative' }}>
      {entries.slice(start, end).map((entry, index) => <CueRow key={entry.index} entry={entry}
        active={entry.index === active} offset={offset} canPlay={canPlay} seek={seek} replay={replay} onEdit={onEdit} top={offsets[start + index]} />)}
    </div> : children}
  </div>;
});
