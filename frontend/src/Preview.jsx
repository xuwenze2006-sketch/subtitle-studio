import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  AudioLines,
  Captions,
  Clapperboard,
  Download,
  FileText,
  FolderOpen,
  ListChecks,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  SkipBack,
  SkipForward,
  X,
} from "lucide-react";
import { Badge, Button, Empty, Notice, timeLabel } from "./ui";
import CueTimeline from "./CueTimeline";
import CueLocator from "./CueLocator";
import ManualReview, { createReviewDraft, isReviewDirty, parseReviewTime } from "./ManualReview";
import { getReviewSubmissions, hasPendingReviewSubmissions, matchesSubmittedDraft, matchesSubmittedFields } from "./reviewSubmissions";
import "./preview-ux.css";
import "./review-layout.css";
import "./file-layout.css";

export default function Preview(props) {
  // A new project owns a new player, search and request lifetime.
  const projectKey = JSON.stringify([
    props.snapshot?.source,
    props.snapshot?.campaign,
    props.snapshot?.project_id,
  ]);
  return <PreviewSession key={projectKey} {...props} projectKey={projectKey} />;
}

function PreviewSession({
  api,
  snapshot,
  connection,
  onError,
  open,
  run,
  toWorkspace,
  toReview,
  onReviewChanged,
  sessionMemory,
  projectKey,
  variant = "review",
  disabled,
}) {
  const compact = variant === "workbench";
  const supportsFileVersions = !!snapshot?.file_layout;
  const [restoredSession] = useState(() =>
    sessionMemory?.current?.projectKey === projectKey ? sessionMemory.current : null,
  );
  const localSession = useRef({});
  const submissionOwner = sessionMemory || localSession.current;
  const [reviewSubmissions] = useState(() => getReviewSubmissions(submissionOwner, projectKey));
  const submissionVersion = useSyncExternalStore(reviewSubmissions.subscribe, reviewSubmissions.getSnapshot);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [time, setTime] = useState(restoredSession?.time ?? 0);
  const [mediaFailed, setMediaFailed] = useState(false);
  const [download, setDownload] = useState("");
  const [downloaded, setDownloaded] = useState("");
  const [savingVersion, setSavingVersion] = useState(false);
  const [savedVersion, setSavedVersion] = useState(null);
  const [playing, setPlaying] = useState(false);
  const [recentCue, setRecentCue] = useState(-1);
  const [completedReplay, setCompletedReplay] = useState(null);
  const [revision, setRevision] = useState(0);
  const [selections, setSelections] = useState([]);
  const [selection, setSelection] = useState(restoredSession?.selection ?? "");
  const [query, setQuery] = useState(restoredSession?.query ?? "");
  const [rate, setRate] = useState(restoredSession?.rate ?? 1);
  const [followPlayback, setFollowPlayback] = useState(restoredSession?.followPlayback ?? true);
  const [timelineMemory] = useState(() => restoredSession?.timelineMemory ?? { current: null });
  const [locateRequest, setLocateRequest] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reviewDraft, setReviewDraft] = useState(restoredSession?.reviewDraft ?? null);
  const [reviewFilter, setReviewFilter] = useState(restoredSession?.reviewFilter ?? "all");
  const [reviewActionBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState("");
  const [reviewConflict, setReviewConflict] = useState("");
  const [reviewNotice, setReviewNotice] = useState("");
  const selectedId = preview?.selected_id || selection || selections[0]?.id || "main";
  const reviewBusy = reviewActionBusy || reviewSubmissions.pending(selectedId);
  const reviewDirty = isReviewDirty(reviewDraft);
  useEffect(() => {
    const preventDraftLoss = event => {
      if (!reviewDirty && !hasPendingReviewSubmissions(submissionOwner)) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventDraftLoss);
    return () => window.removeEventListener("beforeunload", preventDraftLoss);
  }, [reviewDirty, submissionOwner]);
  const video = useRef(null);
  const restorePosition = useRef(restoredSession?.time ?? null);
  const requestVersion = useRef(0);
  const downloadRequest = useRef(null);
  const versionSaveRequest = useRef(null);
  const taskRefreshRequest = useRef(0);
  const replayRange = useRef(null);
  const advanceAfterSave = useRef(null);
  const refreshTaskState = useCallback(async (savedMessage) => {
    const version = requestVersion.current;
    const request = ++taskRefreshRequest.current;
    try {
      await onReviewChanged?.();
    } catch (error) {
      if (version === requestVersion.current && request === taskRefreshRequest.current) {
        setReviewError(`${savedMessage}，但任务状态刷新失败：${error.message} 请点击“刷新核对状态”重试。`);
      }
    }
  }, [onReviewChanged]);
  useLayoutEffect(() => () => {
    video.current?.pause();
    requestVersion.current += 1;
    downloadRequest.current = null;
    versionSaveRequest.current = null;
  }, []);
  useEffect(() => {
    const version = ++requestVersion.current;
    downloadRequest.current = null;
    versionSaveRequest.current = null;
    const controller = new AbortController();
    video.current?.pause();
    replayRange.current = null;
    setPreview(null);
    setTime(restorePosition.current ?? 0);
    setDownload("");
    setDownloaded("");
    setSavingVersion(false);
    setSavedVersion(null);
    setPlaying(false);
    setRecentCue(-1);
    setCompletedReplay(null);
    setLoadFailed(false);
    setReviewBusy(false);
    if (connection !== "connected") {
      setLoading(false);
      return undefined;
    }
    setLoading(true);
    setMediaFailed(false);
    const params = new URLSearchParams();
    if (selection) params.set("sample", selection);
    if (snapshot?.project_id) params.set("project", snapshot.project_id);
    const query = params.toString();
    api
      .request(query ? `/api/preview?${query}` : "/api/preview", undefined, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted && version === requestVersion.current) {
          if (restorePosition.current != null && data.selected_id && data.selected_id !== restoredSession?.selection) {
            restorePosition.current = null;
            setTime(0);
          }
          setPreview(data);
          setSelections(data.selections || []);
          setReviewDraft(current => {
            if (!current || isReviewDirty(current) || current.sample !== data.selected_id || !data.manual_review?.supported) return current;
            const cue = data.cues.find(item => item.id === current.cueId);
            return cue ? createReviewDraft(cue, data.manual_review.revision, data.selected_id) : null;
          });
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && version === requestVersion.current) {
          setLoadFailed(true);
          onError(error.message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted && version === requestVersion.current) setLoading(false);
      });
    return () => {
      controller.abort();
    };
  }, [
    api,
    connection,
    snapshot?.source,
    snapshot?.campaign,
    snapshot?.project_id,
    snapshot?.job?.busy,
    revision,
    selection,
    onError,
  ]);
  const cues = useMemo(() => (preview?.cues || []).map(cue=>({...cue,ja:cue.source_text ?? cue.ja,zh:cue.target_text ?? cue.zh})), [preview?.cues]);
  const indexedCues = useMemo(() => cues.map((cue, index) => ({cue, index,
    search: `${cue.ja || ""}\n${cue.zh || ""}`.normalize("NFKC").toLocaleLowerCase(),
  })), [cues]);
  const languageLabels={ja:'日语',en:'英语',zh:'中文','zh-CN':'中文',auto:'自动检测'};
  const supportsReview = !!preview?.manual_review?.supported;
  useLayoutEffect(() => {
    if (sessionMemory) sessionMemory.current = { projectKey, selection: preview?.selected_id || selection, time, query, rate, followPlayback, timelineMemory, reviewDraft, reviewFilter };
  }, [sessionMemory, projectKey, preview?.selected_id, selection, time, query, rate, followPlayback, timelineMemory, reviewDraft, reviewFilter]);
  useEffect(() => {
    const submission = reviewSubmissions.takeResult(selectedId);
    if (!submission) return;
    const advance = advanceAfterSave.current;
    advanceAfterSave.current = null;
    if (submission.status === "error") {
      const message = submission.error.message;
      setReviewError(message);
      if (/409|revision|conflict|版本|冲突/i.test(message)) setReviewConflict(message);
      return;
    }
    // The write result supersedes reads started before its acknowledgement.
    // Otherwise a delayed mount-time GET can replace the freshly saved cues.
    ++requestVersion.current;
    setLoading(false);
    setLoadFailed(false);
    const data = submission.data;
    const savedCueIndex = data.cues.findIndex(cue => cue.id === submission.cueId);
    const shouldAdvance = advance?.sample === selectedId && advance.cueId === submission.cueId &&
      advance.revision === submission.revision && matchesSubmittedFields(reviewDraft, submission) &&
      data.cues[savedCueIndex]?.review_status === "checked";
    const unchecked = shouldAdvance ? data.cues.map((cue, index) => ({cue, index})).filter(({cue}) =>
      !cue.review_status || cue.review_status === "unchecked") : [];
    const nextEntry = unchecked.find(({index}) => index > savedCueIndex) || unchecked[0];
    setPreview(data);
    setSelections(data.selections || []);
    setReviewDraft(current => {
      if (shouldAdvance && nextEntry) return createReviewDraft(nextEntry.cue, data.manual_review.revision, selectedId);
      if (!matchesSubmittedDraft(current, submission)) return current;
      const cue = data.cues.find(item => item.id === submission.cueId);
      if (!cue) return current;
      const saved = createReviewDraft(cue, data.manual_review.revision, selectedId);
      if (matchesSubmittedFields(current, submission)) return saved;
      return { ...current, revision: saved.revision, initial: saved.initial };
    });
    if (shouldAdvance && nextEntry) {
      video.current?.pause();
      replayRange.current = null;
      restorePosition.current = null;
      setCompletedReplay(null);
      setPlaying(false);
      setQuery("");
      setReviewFilter("unchecked");
      setFollowPlayback(false);
      timelineMemory.current = null;
      setLocateRequest({index:nextEntry.index});
      if (video.current) video.current.currentTime = nextEntry.cue.start_ms / 1000;
      setTime(nextEntry.cue.start_ms);
      setRecentCue(nextEntry.index);
    }
    setReviewNotice(shouldAdvance
      ? nextEntry ? "本句已保存，已定位下一条未检查字幕。" : "本句已保存，当前片段没有未检查字幕。"
      : matchesSubmittedFields(reviewDraft, submission) ? "本句修改与核对状态已保存。" : "本次提交已保存；之后的修改仍需保存。");
    void refreshTaskState("本句修改已保存");
  }, [reviewSubmissions, submissionVersion, selectedId, refreshTaskState, reviewDraft]);
  const offset = Number(preview?.offset_ms) || 0;
  const canPlay = !!preview?.media_available && !mediaFailed && !loading;
  const searchText = query.trim().normalize("NFKC").toLocaleLowerCase();
  const following = followPlayback && !searchText && (!supportsReview || reviewFilter === "all");
  const visibleCues = useMemo(() => indexedCues.filter(entry => (!searchText || entry.search.includes(searchText)) &&
    (!supportsReview || reviewFilter === "all" || (reviewFilter === "warnings" ? entry.cue.warnings?.length : (entry.cue.review_status || "unchecked") === reviewFilter))), [indexedCues, searchText, reviewFilter, supportsReview]);
  const pauseFollowing = useCallback(() => setFollowPlayback(false), []);
  const changeQuery = (value, revealDraft = true) => {
    timelineMemory.current = null;
    setQuery(value);
    if (revealDraft && searchText && !value.trim() && reviewDraft?.sample === selectedId) {
      const index = cues.findIndex(cue => cue.id === reviewDraft.cueId);
      if (index >= 0) setLocateRequest({ index });
    }
  };
  const active = cues.findIndex(
    (cue) => time >= cue.start_ms && time < cue.end_ms,
  );
  const previous = active >= 0 ? active - 1 : cues.findLastIndex((cue) => cue.start_ms < time);
  const next = active >= 0 ? (active + 1 < cues.length ? active + 1 : -1) : cues.findIndex((cue) => cue.start_ms > time);
  const replayCue = (completedReplay && (completedReplay.cueId == null
    ? cues[completedReplay.index] : cues.find(cue => cue.id === completedReplay.cueId))) ||
    cues[active >= 0 ? active : recentCue];
  useEffect(() => {
    if (active >= 0) setRecentCue(active);
  }, [active]);
  useEffect(() => {
    if (video.current) video.current.playbackRate = rate;
  }, [rate, preview]);
  const switchSelection = (value) => {
    if (reviewDirty) { setReviewNotice("有未保存修改，请先保存或放弃后再切换片段。"); return; }
    ++requestVersion.current;
    downloadRequest.current = null;
    versionSaveRequest.current = null;
    video.current?.pause();
    replayRange.current = null;
    restorePosition.current = null;
    setTime(0);
    setPlaying(false);
    setRecentCue(-1);
    setCompletedReplay(null);
    setDownloaded("");
    setSavingVersion(false);
    setSavedVersion(null);
    setReviewDraft(null);
    setReviewFilter("all");
    setReviewBusy(false);
    setReviewError("");
    setReviewConflict("");
    setReviewNotice("");
    setPreview(null);
    setQuery("");
    setFollowPlayback(true);
    setLocateRequest(null);
    timelineMemory.current = null;
    setLoading(true);
    setSelection(value);
  };
  const seek = useCallback((cue) => {
    if (video.current) {
      const index = cue.id == null ? cues.indexOf(cue) : cues.findIndex(item => item.id === cue.id);
      replayRange.current = null;
      setCompletedReplay(null);
      restorePosition.current = null;
      video.current.currentTime = cue.start_ms / 1000;
      setTime(cue.start_ms);
      setRecentCue(index);
      setLocateRequest({ index });
    }
  }, [cues]);
  const togglePlayback = async () => {
    const media = video.current;
    if (!media) return;
    replayRange.current = null;
    setCompletedReplay(null);
    if (!media.paused) {
      media.pause();
      return;
    }
    const version = requestVersion.current;
    try {
      await media.play();
    } catch (error) {
      if (version === requestVersion.current && error.name !== "AbortError") {
        onError("暂时无法播放，请使用播放器的播放按钮重试。");
      }
    }
  };
  const replay = useCallback(async (cue) => {
    if (!video.current) return;
    const version = requestVersion.current;
    seek(cue);
    replayRange.current = { cueId: cue.id,
      index: cue.id == null ? cues.indexOf(cue) : cues.findIndex(item => item.id === cue.id),
      start: cue.start_ms, end: cue.end_ms };
    try {
      await video.current.play();
    } catch (error) {
      if (version === requestVersion.current && error.name !== "AbortError") {
        replayRange.current = null;
        onError("暂时无法播放这句，请使用播放器的播放按钮重试。");
      }
    }
  }, [cues, seek, onError]);
  const updateTime = (event) => {
    const media = event.currentTarget;
    const range = replayRange.current;
    const end = range?.end;
    if (end != null && media.currentTime * 1000 >= end) {
      replayRange.current = null;
      // Older playback-only previews have no stable cue IDs.
      setCompletedReplay({ cueId: range.cueId, index: range.index, end });
      media.pause();
      media.currentTime = end / 1000;
    }
    setTime(media.currentTime * 1000);
  };
  const mediaError = (event) => {
    const media = event.currentTarget;
    // A second failure before metadata must retain the pending restore target.
    if (restorePosition.current == null) {
      restorePosition.current = Number.isFinite(media.currentTime) ? media.currentTime * 1000 : time;
    }
    replayRange.current = null;
    setCompletedReplay(null);
    media.pause();
    setPlaying(false);
    setMediaFailed(true);
  };
  const reloadMedia = () => {
    if (disabled || loading || connection !== "connected" || !preview?.media_available) return;
    setPlaying(false);
    setMediaFailed(false);
  };
  const handleShortcut = (event) => {
    if (!canPlay || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
        event.repeat || event.nativeEvent?.isComposing) return;
    // Native controls and editable fields keep their own keyboard behavior.
    if (event.target.closest?.("input, textarea, select, button, a, video, audio, [role='button'], [role='textbox'], [role='combobox'], [contenteditable]:not([contenteditable='false'])")) return;
    if (event.key === " ") {
      event.preventDefault();
      void togglePlayback();
    } else if (event.key === "[" && previous >= 0) {
      event.preventDefault();
      seek(cues[previous]);
    } else if (event.key === "]" && next >= 0) {
      event.preventDefault();
      seek(cues[next]);
    } else if (event.key.toLowerCase() === "r" && replayCue) {
      event.preventDefault();
      void replay(replayCue);
    }
  };
  const save = async (name) => {
    if (reviewDirty || reviewBusy || reviewSubmissions.pending(selectedId)) { setReviewNotice("请先保存本句修改，并等待保存完成后再导出字幕。"); return; }
    if (disabled || loading || downloadRequest.current || versionSaveRequest.current || !preview) return;
    // Review acknowledgements invalidate stale preview reads, not this export.
    const request = {};
    downloadRequest.current = request;
    setDownload(name);
    setDownloaded("");
    try {
      const filename = await api.download(name, selectedId, preview?.project_id);
      if (downloadRequest.current === request) setDownloaded(filename || name);
    } catch (error) {
      if (downloadRequest.current === request) onError(error.message);
    } finally {
      if (downloadRequest.current === request) {
        downloadRequest.current = null;
        setDownload("");
      }
    }
  };
  const saveVersion = async () => {
    if (reviewDirty || reviewBusy || reviewSubmissions.pending(selectedId)) { setReviewNotice("请先保存本句修改，并等待保存完成后再导出字幕。"); return; }
    if (!supportsFileVersions || disabled || loading || downloadRequest.current || versionSaveRequest.current || snapshot?.job?.busy || !preview) return;
    const request = {};
    versionSaveRequest.current = request;
    setSavingVersion(true);
    setSavedVersion(null);
    try {
      const result = await api.request("/api/save-subtitles", { project_id: preview.project_id, sample: selectedId });
      if (versionSaveRequest.current === request) setSavedVersion(result);
    } catch (error) {
      if (versionSaveRequest.current === request) onError(error.message);
    } finally {
      if (versionSaveRequest.current === request) {
        versionSaveRequest.current = null;
        setSavingVersion(false);
      }
    }
  };

  const editCue = useCallback((cue) => {
    if (reviewDraft?.cueId === cue.id) return;
    if (disabled || loading || snapshot?.job?.busy) return;
    if (reviewBusy) { setReviewNotice("正在保存或刷新核对状态，请等待完成后再编辑其他字幕。"); return; }
    if (reviewDirty) { setReviewNotice("有未保存修改，请先保存或放弃后再编辑其他字幕。"); return; }
    video.current?.pause();
    setPlaying(false);
    seek(cue);
    setReviewDraft(createReviewDraft(cue, preview.manual_review.revision, selectedId));
    setReviewError("");
    setReviewNotice("");
  }, [reviewDraft?.cueId, reviewDirty, reviewBusy, disabled, loading, snapshot?.job?.busy, seek, preview?.manual_review?.revision, selectedId]);
  const locateCue = (cue, index) => {
    if (disabled || loading || reviewBusy || snapshot?.job?.busy) return "当前任务忙碌，请等待完成后再定位字幕。";
    if (reviewDirty) return "请先保存或放弃未保存修改，再定位字幕。";
    if (supportsReview && reviewDraft?.cueId !== cue.id) editCue(cue);
    else {
      video.current?.pause();
      setPlaying(false);
      if (canPlay) seek(cue);
    }
    setQuery("");
    setReviewFilter("all");
    setFollowPlayback(false);
    timelineMemory.current = null;
    setLocateRequest({ index });
  };
  const discardReviewDraft = () => {
    const cue = cues.find(item => item.id === reviewDraft?.cueId);
    setReviewDraft(cue ? createReviewDraft(cue, preview.manual_review.revision, selectedId) : null);
    setReviewError("");
    setReviewNotice("");
  };
  const refreshReview = async ({ preserveNotice = false } = {}) => {
    if (!preview || reviewBusy || disabled || loading || snapshot?.job?.busy) return;
    const version = requestVersion.current;
    ++taskRefreshRequest.current;
    setReviewBusy(true);
    setReviewError("");
    try {
      const params = new URLSearchParams({ sample: selectedId, project: preview.project_id });
      const data = await api.request(`/api/preview?${params}`);
      if (version !== requestVersion.current) return;
      setPreview(data);
      setSelections(data.selections || []);
      setReviewConflict(data.manual_review?.conflict || "");
      setReviewDraft(current => {
        if (!current) return null;
        const cue = data.cues.find(item => item.id === current.cueId);
        if (!cue) return current;
        if (!isReviewDirty(current)) return createReviewDraft(cue, data.manual_review?.revision, selectedId);
        return { ...current, revision: data.manual_review?.revision, rebased: true, fields: { ...current.fields, translation_confirmed: false } };
      });
      if (!preserveNotice) setReviewNotice(reviewDirty ? "已刷新，输入已保留；请对照最新保存内容后重新保存。" : "已读取最新核对状态。");
      await refreshTaskState("核对状态已读取");
    } catch (error) {
      if (version === requestVersion.current) setReviewError(error.message);
    } finally {
      if (version === requestVersion.current) setReviewBusy(false);
    }
  };
  const reviewTimeRange = (fields) => {
    const start = parseReviewTime(fields.start), end = parseReviewTime(fields.end);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0) { setReviewError("请输入有效时间：HH:MM:SS.mmm 或非负秒数（最多三位小数）。"); return null; }
    if (end <= start) { setReviewError("结束时间必须晚于开始时间。"); return null; }
    return { start_ms: start, end_ms: end };
  };
  const listenReviewCue = () => {
    if (!canPlay || !reviewDraft || disabled || reviewBusy || snapshot?.job?.busy) return;
    const cue = cues.find(item => item.id === reviewDraft.cueId);
    const range = reviewTimeRange(reviewDraft.fields);
    if (!cue || !range) return;
    setReviewError("");
    void replay({ ...cue, ...range });
  };
  const saveReviewCue = (status, { advance = false } = {}) => {
    if (!supportsReview || !reviewDraft || disabled || loading || reviewBusy || snapshot?.job?.busy || reviewConflict || preview.manual_review.conflict) return;
    const fields = reviewDraft.fields;
    const range = reviewTimeRange(fields);
    if (!range) return;
    const { start_ms: start, end_ms: end } = range;
    if (status === "issue" && !fields.note.trim()) { setReviewError("请填写核对备注，说明本句疑点。"); return; }
    const cue = cues.find(item => item.id === reviewDraft.cueId);
    const stale = cue?.translation_stale || fields.source_text !== (cue?.source_text ?? cue?.ja ?? "");
    const finalStatus = stale && !fields.translation_confirmed && status === "checked" ? "unchecked" : status;
    // Supersede old status reads when a new operation starts, including the
    // interval before its write acknowledgement initiates the next refresh.
    ++taskRefreshRequest.current;
    setReviewError("");
    setReviewNotice("");
    const submitted = reviewSubmissions.submit(selectedId, reviewDraft, () => api.request("/api/review-cue", { project_id: preview.project_id, sample: selectedId, expected_revision: reviewDraft.revision,
        cue_id: reviewDraft.cueId, start_ms: start, end_ms: end, source_text: fields.source_text, target_text: fields.target_text,
        review_status: finalStatus, note: fields.note, translation_confirmed: fields.translation_confirmed }));
    if (submitted) advanceAfterSave.current = advance ? {sample:selectedId, cueId:reviewDraft.cueId, revision:reviewDraft.revision} : null;
  };
  const acceptReview = async () => {
    if (!supportsReview || selectedId !== "main" || disabled || loading || reviewBusy || reviewDirty || snapshot?.job?.busy || reviewConflict || !preview.manual_review.summary?.can_accept) return;
    const version = requestVersion.current;
    ++taskRefreshRequest.current;
    setReviewBusy(true);
    setReviewError("");
    try {
      const result = await api.request("/api/review-accept", { project_id: preview.project_id, sample: "main", expected_revision: preview.manual_review.revision, content_passed: true });
      if (version !== requestVersion.current) return;
      if (!result.accepted) throw new Error("整片验收尚未完成，请刷新并检查验收条件。");
      setReviewNotice("整片验收已记录，可继续导出带字幕视频。");
      void refreshTaskState("整片验收已记录");
      const params = new URLSearchParams({ sample: selectedId, project: preview.project_id });
      const data = await api.request(`/api/preview?${params}`);
      if (version === requestVersion.current) setPreview(data);
    } catch (error) {
      if (version === requestVersion.current) {
        setReviewError(error.message);
        if (/409|revision|conflict|版本|冲突/i.test(error.message)) setReviewConflict(error.message);
      }
    } finally {
      if (version === requestVersion.current) setReviewBusy(false);
    }
  };
  const nextUnchecked = () => {
    if (reviewDirty || reviewBusy) { setReviewNotice("有未保存修改，请先保存或放弃后再检查下一句。"); return; }
    const current = cues.findIndex(cue => cue.id === reviewDraft?.cueId);
    const unchecked = cues.map((cue, index) => ({ cue, index })).filter(({ cue }) => !cue.review_status || cue.review_status === "unchecked");
    const entry = unchecked.find(({ index }) => index > current) || unchecked[0];
    if (!entry) return;
    setReviewFilter("unchecked");
    changeQuery("", false);
    setFollowPlayback(false);
    editCue(entry.cue);
    setLocateRequest({ index: entry.index });
    if (canPlay) seek(entry.cue);
  };
  const draftExport = (snapshot?.actions?.["export-draft"] !== undefined || preview?.draft_video || snapshot?.job?.action === "export-draft") &&
    <section className="card preview-export-card preview-draft-export-card" aria-label="草稿成片导出">
      <div>
        <h2><Clapperboard size={18} /> 草稿 MP4 <Badge tone="warning">未审核草稿</Badge></h2>
        <p>可直接导出当前字幕；未经人工审核，原视频保留。</p>
        {preview?.draft_video && <p>{preview.draft_video.name}</p>}
      </div>
      <div className="preview-export-actions">
        {preview?.draft_video && <>
          <Button icon={Play} disabled={disabled} onClick={() => open("draft-video", preview.project_id)}>播放草稿</Button>
          <Button icon={FolderOpen} disabled={disabled} onClick={() => open("draft-video-folder", preview.project_id)}>打开草稿目录</Button>
        </>}
        <Button icon={Clapperboard} variant="primary" disabled={disabled || loading || !!snapshot?.job?.busy || !snapshot?.actions?.["export-draft"] || !run}
          busy={snapshot?.job?.busy && snapshot?.job?.action === "export-draft"} onClick={() => run("export-draft")}>直接导出草稿 MP4</Button>
      </div>
    </section>;

  return (
    <div className={`preview-content ${compact ? "preview-workbench" : "preview-review"}`} role="region" aria-label="字幕预览工作区" tabIndex={0} onKeyDown={handleShortcut}>
      <div className="preview-toolbar">
        <div className="preview-context">
          {!!selections.length && (
            <label className="preview-selection">
              <span>预览片段</span>
              <select aria-label="预览片段" value={selectedId} onChange={(event) => switchSelection(event.target.value)} disabled={connection !== "connected"}>
                {selections.map((item) => <option key={item.id} value={item.id}>{item.name}{item.offset_ms ? ` · ${timeLabel(item.offset_ms)}` : ""}</option>)}
              </select>
            </label>
          )}
          <Badge tone={cues.length ? "primary" : "neutral"}>
            {cues.length} 条字幕
          </Badge>
          <span className="preview-source-name" title={preview?.source_name}>{preview?.source_name || "当前任务"}</span>
          {preview?.source_language && <Badge>{languageLabels[preview.source_language]} → {languageLabels[preview.target_language]}</Badge>}
        </div>
        <div className="preview-toolbar-actions">
          <Button
            icon={RefreshCw}
            disabled={disabled || loading}
            busy={loading}
            onClick={() => { if (supportsReview) void refreshReview(); else { restorePosition.current = null; setRevision((v) => v + 1); } }}
          >
            刷新结果
          </Button>
          {compact && toReview && <Button icon={ListChecks} variant="primary" disabled={disabled} onClick={toReview}>进入校对台</Button>}
        </div>
      </div>
      {loading && <p className="preview-loading" role="status">正在读取所选片段…</p>}
      <div className="preview-layout">
        <section className="card player-card">
          <div className="player-stage">
            {canPlay ? (
              <>
                <video
                  ref={video}
                  key={selectedId}
                  src={preview.media_url || "/api/media"}
                  controls
                  preload="metadata"
                  onTimeUpdate={updateTime}
                  onLoadedMetadata={(event) => {
                    const media = event.currentTarget;
                    media.playbackRate = rate;
                    if (restorePosition.current != null) {
                      const position = restorePosition.current / 1000;
                      restorePosition.current = null;
                      media.currentTime = Number.isFinite(media.duration) ? Math.min(position, media.duration) : position;
                      setTime(media.currentTime * 1000);
                    }
                  }}
                  onPlay={() => { if (!replayRange.current) setCompletedReplay(null); setPlaying(true); }}
                  onPause={() => { replayRange.current = null; setPlaying(false); }}
                  onEnded={() => setPlaying(false)}
                  onSeeking={(event) => {
                    const range = replayRange.current;
                    const position = event.currentTarget.currentTime * 1000;
                    // The automatic end clamp also seeks; only a different
                    // position should discard the just-finished replay target.
                    if (completedReplay && Math.abs(position - completedReplay.end) > 0.5) setCompletedReplay(null);
                    if (range && (position < range.start || position >= range.end)) replayRange.current = null;
                  }}
                  onError={mediaError}
                  aria-label="源素材预览"
                />
                {active >= 0 && (
                  <div className="subtitle-overlay" aria-hidden="true">
                    {cues[active].ja && <span>{cues[active].ja}</span>}
                    {cues[active].zh && <strong>{cues[active].zh}</strong>}
                  </div>
                )}
              </>
            ) : (
              <Empty
                icon={Clapperboard}
                title={
                  loading ? "正在载入预览…" : mediaFailed
                    ? "视频暂时无法播放"
                    : "还没有可播放的素材"
                }
                action={mediaFailed && preview?.media_available && <Button icon={RefreshCw}
                  disabled={disabled || loading || connection !== "connected"} onClick={reloadMedia}>重新加载视频</Button>}
              >
                {mediaFailed
                  ? "可以重新加载视频后重试；若仍无法播放，可从项目目录使用本机播放器检查。"
                  : "选择素材并载入任务后，源视频会显示在这里。"}
              </Empty>
            )}
          </div>
          <div className="player-info">
            <div>
              <Clapperboard size={18} />
              <strong>{preview?.source_name || "源素材预览"}</strong>
            </div>
            <Badge>{selectedId === "main" ? "原始素材" : "样片预览"}</Badge>
          </div>
          <div className="preview-playback-controls">
            <div className="preview-transport">
              <Button icon={playing ? Pause : Play} variant="primary" disabled={!canPlay} onClick={togglePlayback}>{playing ? "暂停" : "播放"}</Button>
              <Button icon={SkipBack} aria-label="上一句" disabled={!canPlay || previous < 0} onClick={() => seek(cues[previous])}>上一句</Button>
              <Button icon={SkipForward} aria-label="下一句" disabled={!canPlay || next < 0} onClick={() => seek(cues[next])}>下一句</Button>
              <Button icon={RotateCcw} disabled={!canPlay || !replayCue} onClick={() => replay(replayCue)}>重播当前句</Button>
            </div>
            <label className="preview-rate">速度
              <select aria-label="播放速度" value={rate} disabled={!canPlay} onChange={(event) => setRate(Number(event.target.value))}>
                {[0.75, 1, 1.25, 1.5].map((value) => <option key={value} value={value}>{value}×</option>)}
              </select>
            </label>
          </div>
          {!compact && <p className="preview-shortcuts">点击预览空白处后：<kbd>空格</kbd> 播放/暂停 · <kbd>[</kbd> 上一句 · <kbd>]</kbd> 下一句 · <kbd>R</kbd> 重播</p>}
          <div className="player-caption">
            <Captions size={15} />
            <span>点字幕定位；点单句播放可听完即停。{offset > 0 && "字幕时间标注原片位置。"}</span>
          </div>
          {!!preview?.downloads?.length && (
            <div className="downloads">
              <h3>导出字幕文件</h3>
              <div>
                {preview.downloads.map((name) => (
                  <Button
                    key={name}
                    icon={Download}
                    disabled={disabled || loading || !!download || savingVersion || reviewDirty || reviewBusy}
                    busy={download === name}
                    aria-label={`下载 ${name} SRT`}
                    onClick={() => save(name)}
                  >
                    {name}
                  </Button>
                ))}
              </div>
              <p className="file-naming-hint">{supportsFileVersions ? "下载名自动包含素材名、片段和时间，便于区分不同结果。" : "字幕文件保存到浏览器的下载目录。"}</p>
              <div className="subtitle-version-actions">
                <Button icon={Save} disabled={!supportsFileVersions || disabled || loading || !!download || savingVersion || !!snapshot?.job?.busy || reviewDirty || reviewBusy} busy={savingVersion} onClick={saveVersion}>保存字幕版本</Button>
                <span>{supportsFileVersions ? "在项目内保留一份带时间的副本" : "重启字幕工坊后可使用版本保存"}</span>
              </div>
              {(reviewDirty || reviewBusy) && <p className="file-naming-hint">请先保存本句修改，并等待保存完成后再导出字幕。</p>}
              {savedVersion?.folder && <div className="subtitle-version-receipt" role="status">
                <strong>字幕版本已保存</strong>
                <span className="saved-version-path">{savedVersion.folder}</span>
                <small>保留当前字幕与审核状态；保存版本不代表内容已校对。</small>
                <Button icon={FolderOpen} disabled={disabled} onClick={() => open("exports", preview.project_id)}>打开导出目录</Button>
              </div>}
              {downloaded && <p className="preview-download-receipt" role="status">已交给浏览器：{downloaded}。可在浏览器下载记录中查看。</p>}
            </div>
          )}
          {compact && draftExport}
        </section>
        <section className="card cue-card">
          <div className="section-heading">
            <h2>
              <Captions size={18} /> 同步字幕
            </h2>
            <span className="section-meta">
              {searchText || reviewFilter !== "all" ? `${visibleCues.length} / ${cues.length} 条` : active >= 0 ? `${active + 1} / ${cues.length}` : "按时间顺序"}
            </span>
          </div>
          <div className="cue-follow-controls">
            <span>{searchText ? "正在浏览搜索结果" : following ? "手动滚动可暂停跟随" : "自由浏览中，播放不会移动列表"}</span>
            <Button icon={ListChecks} aria-label="跟随播放" aria-pressed={following}
              disabled={loading || !cues.length}
              onClick={() => {
                if (following) setFollowPlayback(false);
                else {
                  changeQuery("", false);
                  setReviewFilter("all");
                  setFollowPlayback(true);
                  if (active < 0 && recentCue >= 0) setLocateRequest({ index: recentCue });
                }
              }}>{following ? "跟随播放" : "恢复跟随"}</Button>
          </div>
          <div className="preview-search">
            <Search size={16} aria-hidden="true" />
            <input type="search" aria-label="搜索字幕" placeholder="搜索原文或译文…" value={query} onChange={(event) => changeQuery(event.target.value)} disabled={loading || !cues.length} />
            {query && <button type="button" aria-label="清空搜索" onClick={() => changeQuery("")}><X size={15} aria-hidden="true" /></button>}
          </div>
          {supportsReview && <label className="cue-review-filter">核对状态筛选<select aria-label="核对状态筛选" value={reviewFilter} onChange={event => { timelineMemory.current = null; setReviewFilter(event.target.value); }}><option value="all">全部</option><option value="unchecked">未检查</option><option value="issue">有疑点</option><option value="checked">已检查</option><option value="warnings">有自动检查提示</option></select></label>}
          <CueLocator key={selectedId} cues={cues} offset={offset} onLocate={locateCue}
            disabled={disabled || loading || reviewBusy || !!snapshot?.job?.busy} />
          <CueTimeline entries={visibleCues} active={active} offset={offset} canPlay={canPlay} seek={seek} replay={replay}
            onEdit={supportsReview ? editCue : undefined}
            following={following} onManualBrowse={pauseFollowing} locateRequest={locateRequest}
            viewMemory={timelineMemory} viewKey={JSON.stringify([selectedId, searchText, reviewFilter])} ready={!!preview && !loading}>
              <Empty
                icon={Captions}
                title={loading ? "正在读取字幕…" : loadFailed ? "字幕读取失败" : cues.length ? reviewFilter !== "all" ? "当前筛选下没有字幕" : "没有找到匹配的字幕" : "字幕还没有生成"}
              >
                {loadFailed ? "点击上方“刷新结果”重新读取。" : cues.length ? reviewFilter !== "all" ? "切换核对状态筛选，或清空搜索查看其他字幕。" : "换一个关键词，或清空搜索查看全部字幕。" : "完成识别后，在这里逐句核对文字和时间轴。"}
              </Empty>
          </CueTimeline>
          {supportsReview && <ManualReview preview={preview} selectedId={selectedId} draft={reviewDraft?.sample === selectedId ? reviewDraft : null}
            onDraftChange={setReviewDraft} onDiscard={discardReviewDraft} onSave={saveReviewCue} onRefresh={refreshReview} onAccept={acceptReview} onNext={nextUnchecked}
            onListen={listenReviewCue} canListen={canPlay}
            disabled={disabled || loading || !!snapshot?.job?.busy} busy={reviewBusy} error={reviewError}
            conflict={reviewConflict || preview.manual_review.conflict} notice={reviewNotice} />}
        </section>
      </div>
      {!compact && draftExport}
      {!compact && <section className="card preview-export-card" aria-label="成片导出">
        <div>
          <h2><Clapperboard size={18} /> 审核版 MP4</h2>
          <p>{preview?.exported_video ? preview.exported_video.name : snapshot?.actions?.export ? "将审核后的译文字幕压入画面，保留原视频。" : "需要审核版时，先完成整片字幕与人工抽检。"}</p>
        </div>
        <div className="preview-export-actions">
          {preview?.exported_video ? <>
            <Button icon={Play} disabled={disabled} onClick={() => open("video", preview.project_id)}>播放成片</Button>
            <Button icon={FolderOpen} disabled={disabled} onClick={() => open("video-folder", preview.project_id)}>打开成片目录</Button>
          </> : !snapshot?.actions?.export && toWorkspace ? <Button icon={ListChecks} onClick={toWorkspace}>返回任务完成审核</Button> : null}
          <Button icon={Clapperboard} variant="primary" disabled={disabled || !!snapshot?.job?.busy || !snapshot?.actions?.export || !run} busy={snapshot?.job?.busy && snapshot?.job?.action === "export"} onClick={() => run("export")}>导出带字幕 MP4</Button>
        </div>
      </section>}
      {!!preview?.issues?.length && (
        <Notice tone="warning" title="这些内容需要人工检查">
          <ul>
            {preview.issues.map((issue, index) => (
              <li key={index}>
                {typeof issue === "string" ? issue : JSON.stringify(issue)}
              </li>
            ))}
          </ul>
        </Notice>
      )}
      {!compact && !!preview?.auditions?.length && (
        <section className="card audition-card">
          <div className="section-heading">
            <div>
              <h2>
                <AudioLines size={19} /> 硅基流动试听
              </h2>
              <p>试听文字尚未对齐时间轴，不能作为正式 SRT 字幕使用。</p>
            </div>
            <Badge tone="warning">仅试听文字</Badge>
          </div>
          <div className="audition-list">
            {preview.auditions.map((audition, index) => (
              <article key={index}>
                <h3>
                  <FileText size={16} />
                  {audition.name || `片段 ${index + 1}`}
                </h3>
                <p>{audition.text}</p>
              </article>
            ))}
          </div>
        </section>
      )}
      {!compact && <div className="preview-review-note">
        <ListChecks size={19} />
        <div>
          <strong>{supportsReview ? "人工核对记录保存在当前任务" : "预览是检查的开始"}</strong>
          <p>{supportsReview ? "逐句保存核对状态，完成整片抽检后在上方记录验收结果。" : "正式验收请完成样片对照与抽检，并在字幕任务中记录检查结果。"}</p>
        </div>
        <Button
          icon={ListChecks}
          disabled={disabled || !snapshot?.actions?.review}
          onClick={() => open("review")}
        >
          打开样片对照
        </Button>
      </div>}
    </div>
  );
}
