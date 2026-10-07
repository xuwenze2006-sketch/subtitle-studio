import { useEffect, useId, useState } from "react";
import { parseReviewTime } from "./ManualReview";
import "./cue-locator.css";

const MAX_INPUT_LENGTH = 32;

export default function CueLocator({ cues = [], offset = 0, disabled = false, onLocate }) {
  const id = useId();
  const [mode, setMode] = useState("index");
  const [value, setValue] = useState("");
  const [feedback, setFeedback] = useState(null);
  const blocked = disabled || !cues.length;
  const hintId = `${id}-hint`, feedbackId = `${id}-feedback`;

  useEffect(() => setFeedback(null), [cues, offset]);

  const reject = (message, invalid = true) => setFeedback({ message, error: true, invalid });
  const submit = event => {
    event.preventDefault();
    if (blocked) return;
    if (value.length > MAX_INPUT_LENGTH) {
      reject(`输入长度不能超过 ${MAX_INPUT_LENGTH} 个字符。`);
      return;
    }
    const text = value.trim();
    let index, gap = false;
    if (mode === "index") {
      const number = /^\d+$/.test(text) ? Number(text) : NaN;
      if (!Number.isSafeInteger(number) || number < 1 || number > cues.length) {
        reject(`请输入 1 到 ${cues.length} 之间的整数字幕序号。`);
        return;
      }
      index = number - 1;
    } else {
      const absoluteTime = parseReviewTime(text);
      if (!Number.isSafeInteger(absoluteTime) || absoluteTime < 0) {
        reject("请输入有效原片时间：HH:MM:SS.mmm 或非负秒数（最多三位小数），且不能超出安全整数范围。");
        return;
      }
      if (!Number.isSafeInteger(offset) || offset < 0) {
        reject("当前片段的原片时间偏移无效，暂时无法按时间定位。", false);
        return;
      }
      if (absoluteTime < offset) {
        reject("原片时间早于当前片段起点，请输入本片段范围内的时间。");
        return;
      }
      const localTime = absoluteTime - offset;
      index = cues.findIndex(cue => cue.end_ms > localTime);
      if (index < 0) {
        reject("原片时间已到达或超出当前字幕末尾，没有后续字幕可定位。");
        return;
      }
      gap = cues[index].start_ms > localTime;
    }
    try {
      if (typeof onLocate !== "function") {
        reject("当前无法定位字幕，请稍后重试。", false);
        return;
      }
      const error = onLocate(cues[index], index);
      if (typeof error === "string" && error.length) {
        reject(error.trim() || "定位未完成，请稍后重试。", false);
        return;
      }
      setFeedback({ error: false, message: gap
        ? `目标时间位于字幕空隙，已定位后续第 ${index + 1} 条字幕。`
        : `已定位第 ${index + 1} 条字幕。` });
    } catch (error) {
      reject(error?.message || "定位未完成，请稍后重试。", false);
    }
  };

  return <form className="cue-locator" aria-label="字幕定位" onSubmit={submit} noValidate>
    <label className="cue-locator-mode" htmlFor={`${id}-mode`}>定位方式
      <select id={`${id}-mode`} value={mode} disabled={blocked} onChange={event => {
        setMode(event.target.value);
        setValue("");
        setFeedback(null);
      }}>
        <option value="index">字幕序号</option>
        <option value="time">原片时间</option>
      </select>
    </label>
    <label className="cue-locator-value" htmlFor={`${id}-value`}>{mode === "index" ? "字幕序号" : "原片时间"}
      <input id={`${id}-value`} type="text" value={value} disabled={blocked}
        inputMode={mode === "index" ? "numeric" : "text"}
        placeholder={mode === "index" ? "例如 125" : "例如 00:12:34.500 或 754.5"}
        aria-invalid={feedback?.invalid ? "true" : "false"}
        aria-describedby={`${hintId}${feedback ? ` ${feedbackId}` : ""}`}
        onChange={event => { setValue(event.target.value); setFeedback(null); }} />
    </label>
    <button type="submit" className="button button-secondary" disabled={blocked}>定位字幕</button>
    <p id={hintId} className="cue-locator-hint">{!cues.length ? "暂无字幕可定位。" : mode === "index"
      ? `字幕序号从 1 开始，共 ${cues.length} 条。定位不会自动播放。`
      : "输入原片时间（不是样片内时间）；字幕空隙会定位后续句，不自动播放。"}</p>
    {feedback && <p id={feedbackId} className={`cue-locator-feedback${feedback.error ? " cue-locator-error" : ""}`}
      role={feedback.error ? "alert" : "status"}>{feedback.message}</p>}
  </form>;
}
