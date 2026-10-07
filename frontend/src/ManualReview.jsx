import { useEffect, useMemo, useState } from "react";
import { Check, Flag, ListChecks, Play, RefreshCw, Save } from "lucide-react";
import { Button } from "./ui";
import "./manual-review.css";

const labels = { en: "英语", ja: "日语", zh: "中文", "zh-CN": "中文", auto: "自动检测" };
export const reviewStatusLabel = status => ({ checked: "已检查", issue: "有疑点", unchecked: "未检查" }[status] || "未检查");
const formatTime = ms => {
  const value = Math.max(0, Math.round(Number(ms) || 0));
  return `${String(Math.floor(value / 3600000)).padStart(2, "0")}:${String(Math.floor(value / 60000) % 60).padStart(2, "0")}:${String(Math.floor(value / 1000) % 60).padStart(2, "0")}.${String(value % 1000).padStart(3, "0")}`;
};
export function parseReviewTime(value) {
  const text = value.trim();
  if (/^\d+(?:\.\d{1,3})?$/.test(text)) return Math.round(Number(text) * 1000);
  const parts = /^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(text);
  return parts ? Number(parts[1]) * 3600000 + Number(parts[2]) * 60000 + Number(parts[3]) * 1000 + Number((parts[4] || "").padEnd(3, "0")) : NaN;
}
export function createReviewDraft(cue, revision, sample) {
  const fields = { start: formatTime(cue.start_ms), end: formatTime(cue.end_ms), source_text: cue.source_text ?? cue.ja ?? "", target_text: cue.target_text ?? cue.zh ?? "", note: cue.note || "", review_status: cue.review_status || "unchecked", translation_confirmed: false };
  return { cueId: cue.id, sample, revision, initial: fields, fields };
}
export const isReviewDirty = draft => !!draft && Object.keys(draft.fields).some(key => draft.fields[key] !== draft.initial[key]);

export default function ManualReview({ preview, selectedId, draft, onDraftChange, onDiscard, onSave, onRefresh, onAccept, onNext,
  onListen, canListen, disabled, busy, error, conflict, notice }) {
  const [confirmed, setConfirmed] = useState(false);
  const review = preview.manual_review;
  useEffect(() => setConfirmed(false), [selectedId, review.revision]);
  const summary = review.summary || {};
  const cueId = draft?.cueId;
  const cueIndex = useMemo(() => preview.cues.findIndex(cue => cue.id === cueId), [preview.cues, cueId]);
  const hasUnchecked = useMemo(() => preview.cues.some(cue => !cue.review_status || cue.review_status === "unchecked"), [preview.cues]);
  const cue = preview.cues[cueIndex];
  const fields = draft?.fields;
  const dirty = isReviewDirty(draft);
  const sourceChanged = fields && fields.source_text !== (cue?.source_text ?? cue?.ja ?? "");
  const needsTranslation = !!cue?.translation_stale || sourceChanged;
  const writesDisabled = disabled || busy || !!conflict;
  const change = (field, value) => onDraftChange({ ...draft, fields: { ...fields, [field]: value,
    ...(["source_text", "target_text", "start", "end"].includes(field) ? { review_status: "unchecked" } : {}),
    ...(field === "source_text" ? { translation_confirmed: false } : {}) } });
  const handleShortcut = event => {
    event.stopPropagation();
    if (event.defaultPrevented || !event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
        event.repeat || event.nativeEvent?.isComposing || event.keyCode === 229 || event.key.toLowerCase() !== "s") return;
    event.preventDefault();
    if (cue && fields && !writesDisabled) onSave(fields.review_status);
  };
  return <section className="manual-review" aria-label="人工核对" onKeyDown={handleShortcut}>
    <div className="manual-review-heading"><h3><ListChecks size={17} /> 人工核对</h3><Button onClick={onNext} disabled={disabled || busy || !hasUnchecked}>下一条未检查</Button></div>
    <div className="manual-review-counts" aria-label="已保存核对统计">
      <strong>已检查 {summary.checked || 0} / {summary.total ?? preview.cues.length}</strong><span>疑点 {summary.issues || 0}</span><span>译文待核对 {summary.pending_translation || 0}</span>
    </div>
    <p className="manual-review-hint">点击字幕旁的编辑按钮，核对文字与时间。播放不会自动标记已检查；修改只保存到本地任务。</p>
    {error && <p className="manual-review-error" role="alert">{error}</p>}
    {conflict && <p className="manual-review-warning">{conflict}。输入已保留，请先刷新核对状态，再对照最新保存内容重新提交。</p>}
    {notice && <p className="manual-review-notice" role="status">{notice}</p>}
    <div className="manual-review-refresh"><Button icon={RefreshCw} onClick={onRefresh} disabled={disabled || busy}>刷新核对状态</Button></div>
    {cue && fields ? <div className="manual-review-editor" aria-label={`编辑第 ${cueIndex + 1} 句面板`}>
      <div className="manual-review-heading"><strong>第 {cueIndex + 1} 句 · {reviewStatusLabel(cue.review_status)}</strong>{dirty && <span className="manual-review-unsaved">有未保存修改</span>}</div>
      <div className="manual-review-times">
        <label>开始时间<input aria-label="开始时间" inputMode="decimal" value={fields.start} onChange={event => change("start", event.target.value)} disabled={disabled || busy} /></label>
        <label>结束时间<input aria-label="结束时间" inputMode="decimal" value={fields.end} onChange={event => change("end", event.target.value)} disabled={disabled || busy} /></label>
      </div>
      <p className="manual-review-hint">格式 HH:MM:SS.mmm 或秒数；样片使用片段内时间。</p>
      <label>原文（{labels[preview.source_language] || "原语言"}）<textarea rows={3} value={fields.source_text} onChange={event => change("source_text", event.target.value)} disabled={disabled || busy} /></label>
      <label>译文（{labels[preview.target_language] || "目标语言"}）<textarea rows={3} value={fields.target_text} onChange={event => change("target_text", event.target.value)} disabled={disabled || busy} /></label>
      {needsTranslation && <div className="manual-review-warning"><p>原文已修改或缓存译文已过期，译文需要重新核对。确认前不能标记已检查。</p><label className="manual-review-checkbox"><input type="checkbox" checked={fields.translation_confirmed} onChange={event => change("translation_confirmed", event.target.checked)} disabled={disabled || busy} />已核对修改后的译文</label></div>}
      {!!cue.warnings?.length && <ul className="manual-review-warnings" aria-label="自动检查提示">{cue.warnings.map((warning, index) => <li key={index}>{typeof warning === "string" ? warning : warning.message || warning.reason || JSON.stringify(warning)}</li>)}</ul>}
      <label>核对备注<textarea rows={2} value={fields.note} onChange={event => change("note", event.target.value)} disabled={disabled || busy} placeholder="记录听不清、翻译或时间轴疑点…" /></label>
      <label className="manual-review-status">保存状态<select aria-label="本句核对状态" value={fields.review_status} onChange={event => change("review_status", event.target.value)} disabled={disabled || busy}><option value="unchecked">未检查</option><option value="checked" disabled={needsTranslation && !fields.translation_confirmed}>已检查</option><option value="issue">有疑点</option></select></label>
      <div className="manual-review-actions">
        <Button icon={Play} disabled={disabled || busy || !canListen} onClick={onListen}>试听本句</Button>
        <Button icon={Save} busy={busy} disabled={writesDisabled} onClick={() => onSave(fields.review_status)}>保存修改</Button>
        <Button icon={Check} variant="primary" disabled={writesDisabled || (needsTranslation && !fields.translation_confirmed)} onClick={() => onSave("checked")}>已核对本句</Button>
        <Button icon={Check} disabled={writesDisabled || (needsTranslation && !fields.translation_confirmed)} onClick={event => { if (event.detail <= 1) onSave("checked", {advance:true}); }}>核对并下一条</Button>
        <Button icon={Flag} disabled={writesDisabled} onClick={() => onSave("issue")}>标记疑点</Button>
        {dirty && <Button onClick={onDiscard} disabled={busy}>放弃未保存修改</Button>}
      </div>
      <p className="manual-review-hint">“试听本句”使用当前输入的时间，不保存修改。Ctrl+S 保存当前修改。听看并检查后再标记已核对；“核对并下一条”保存成功后定位下一条未检查字幕，不自动播放。“标记疑点”请填写备注。</p>
      {draft.rebased && <details className="manual-review-latest"><summary>查看最新保存内容</summary><p>{cue.source_text ?? cue.ja}</p><p>{cue.target_text ?? cue.zh}</p><p>{formatTime(cue.start_ms)} → {formatTime(cue.end_ms)} · {reviewStatusLabel(cue.review_status)}</p></details>}
    </div> : <p className="manual-review-hint">尚未选择要核对的字幕。</p>}
    {selectedId === "main" && <div className="manual-review-accept">
      <p>整片验收至少需要已检查 {summary.required_checks ?? Math.min(20, preview.cues.length)} 句，并处理全部已标记的人工疑点与待核对译文。自动检查提示供核对时参考。</p>
      <label className="manual-review-checkbox"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={writesDisabled || dirty} />我已听看抽检并确认疑点已处理</label>
      <Button icon={Check} variant="primary" disabled={writesDisabled || dirty || !confirmed || !summary.can_accept} onClick={onAccept}>完成整片验收</Button>
    </div>}
  </section>;
}
