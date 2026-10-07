import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import App from "./App";

const original = {
  source: "D:\\original.mp4", campaign: "D:\\original", baseline: "", project_id: "original",
  campaign_status: "prepared", project_config: {}, local_available: true,
  job: { busy: false, action: "", status: "idle", message: "", total: 0, recognized: 0, translated: 0, logs: [] },
  accounts: { bailian: { ready: false }, deepseek: { ready: false }, siliconflow: { ready: false } },
  settings: {}, actions: { local: true, prepare: true, export: false },
  recent: [{ path: "D:\\original", title: "原任务", status: "prepared" }, { path: "D:\\other", title: "另一任务", status: "prepared" }],
};
const other = { ...original, source: "D:\\other.mp4", campaign: "D:\\other", project_id: "other" };
const review = {
  project_id: "original", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  media_available: false, source_language: "en", target_language: "zh-CN",
  cues: [{ id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "你好", review_status: "unchecked", note: "", warnings: [] }],
  downloads: [], issues: [], auditions: [],
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, required_checks: 1, can_accept: false } },
};
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
function server() {
  const service = { active: original, review, calls: [], selectionReply: original, afterSelection: original };
  const respond = (data, status = 200) => new Response(JSON.stringify(data), { status });
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    const body = options.body && JSON.parse(options.body);
    service.calls.push({ url, body });
    if (url === '/api/environment') return respond({version:1,checks:{},export_ready:false,recommended_encoder:null});
    if (url === "/api/state") {
      const delayed = service.nextStateRead;
      service.nextStateRead = null;
      return respond(delayed ? await delayed.promise : service.active);
    }
    if (url === "/api/project") {
      if (service.selectionFailure) return respond({ error: "原项目暂时不可用" }, 503);
      service.active = service.afterSelection;
      return respond(service.selectionReply);
    }
    if (url.startsWith("/api/preview")) {
      const id = new URL(url, "http://localhost").searchParams.get("project");
      if (id && id !== service.active.project_id) return respond({ error: "其他窗口已切换项目" }, 409);
      return respond(service.active.project_id === "original" ? service.review : { ...review, project_id: service.active.project_id });
    }
    if (url === "/api/review-cue") {
      if (service.write) return respond(await service.write.promise);
      if (body.project_id !== service.active.project_id || body.expected_revision !== service.review.manual_review.revision)
        return respond({ error: "校对版本冲突，请刷新" }, 409);
      service.review = { ...service.review, cues: [{ ...service.review.cues[0], ...body }],
        manual_review: { ...service.review.manual_review, revision: "r3" } };
      return respond(service.review);
    }
    return respond({ ok: true });
  }));
  return service;
}
const click = name => act(async () => { fireEvent.click(screen.getByRole("button", { name, exact: true })); });
const advance = milliseconds => act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
const warnsOnExit = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};
async function editDraft() {
  await act(async () => { render(<App />); });
  await click("编辑第 1 句");
  fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "尚未保存的译文" } });
}
async function detach(service, snapshot = other) {
  service.active = snapshot;
  await advance(4500);
}
beforeEach(() => {
  window.history.replaceState({}, "", "/#token=offline-test-token");
  vi.useFakeTimers();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => vi.useRealTimers());

describe("原校对任务的恢复与提交保护", () => {
  it.each(["save", "discard"])("recovers the exact original project after another window switches, allowing %s", async resolution => {
    const service = server();
    await editDraft();
    await detach(service);
    expect(screen.getByRole("button", { name: "恢复原校对任务" })).toBeEnabled();
    await click("载入任务 另一任务");
    expect(service.calls.filter(call => call.url === "/api/project")).toHaveLength(0);
    expect(warnsOnExit()).toBe(true);

    await click("恢复原校对任务");
    expect(service.calls.filter(call => call.url === "/api/project")).toEqual([{ url: "/api/project", body: { campaign: original.campaign } }]);
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "放弃未保存修改" })).toBeEnabled();
    expect(warnsOnExit()).toBe(true);
    await click(resolution === "save" ? "保存修改" : "放弃未保存修改");
    expect(warnsOnExit()).toBe(false);
    const writes = service.calls.filter(call => call.url === "/api/review-cue");
    if (resolution === "save") expect(writes[0].body).toMatchObject({ project_id: "original", expected_revision: "r1", target_text: "尚未保存的译文" });
    else expect(writes).toHaveLength(0);
  });

  it("keeps the draft's original CAS revision after recovery until explicitly refreshed", async () => {
    const service = server();
    await editDraft();
    await detach(service);
    service.review = { ...review, cues: [{ ...review.cues[0], target_text: "其他窗口保存的译文" }], manual_review: { ...review.manual_review, revision: "r2" } };
    await click("恢复原校对任务");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(screen.getByText(/草稿基准版本已变化/)).toBeInTheDocument();
    await click("保存修改");
    expect(service.calls.filter(call => call.url === "/api/review-cue")).toHaveLength(0);
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    expect(service.review.cues[0].target_text).toBe("其他窗口保存的译文");
    await click("刷新核对状态");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    await click("保存修改");
    expect(service.calls.filter(call => call.url === "/api/review-cue").map(call => call.body.expected_revision)).toEqual(["r2"]);
    expect(service.review.cues[0].target_text).toBe("尚未保存的译文");
    expect(warnsOnExit()).toBe(false);
  });

  it.each(["reply-source", "reply-campaign", "reply-id", "state-project", "state-id", "failure"])("preserves the original draft on recovery %s and permits a later explicit retry", async mismatch => {
    const service = server();
    await editDraft();
    await detach(service);
    if (mismatch.startsWith("reply-")) {
      const field = { "reply-source": "source", "reply-campaign": "campaign", "reply-id": "project_id" }[mismatch];
      service.selectionReply = { ...original, [field]: other[field] };
      service.afterSelection = other;
    } else if (mismatch === "state-project") service.afterSelection = other;
    else if (mismatch === "state-id") service.afterSelection = { ...original, project_id: "unexpected-project" };
    else service.selectionFailure = true;
    await click("恢复原校对任务");
    expect(screen.getByRole("alert")).toHaveTextContent(mismatch === "failure" ? "原项目暂时不可用" : /恢复.*不一致|恢复.*变化|恢复.*不匹配/);
    expect(screen.queryByRole("button", { name: "编辑第 1 句" })).not.toBeInTheDocument();
    expect(warnsOnExit()).toBe(true);
    expect(service.calls.filter(call => call.url === "/api/review-cue")).toHaveLength(0);

    service.selectionReply = service.afterSelection = original;
    service.selectionFailure = false;
    await click("恢复原校对任务");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    expect(warnsOnExit()).toBe(true);
  });

  it("shows the other project's real busy progress and delays recovery until it stops", async () => {
    const service = server();
    await editDraft();
    await detach(service, { ...other, job: { ...other.job, busy: true, status: "running", action: "export-draft", message: "另一任务正在编码",
      export_progress: { phase: "encoding", percent: 42, speed: 2, eta_seconds: 20 } } });
    expect(screen.getByRole("button", { name: "恢复原校对任务" })).toBeDisabled();
    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).toHaveAttribute("value", "42");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("other.mp4");
    expect(screen.getByLabelText("素材路径")).toHaveValue(other.source);
    expect(screen.getByRole("button", { name: "停止任务", exact: true }).title).toContain(other.source);
    expect(screen.getByText(/当前.*other\.mp4/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "载入任务 另一任务" })).toBeDisabled();
    await click("恢复原校对任务");
    expect(service.calls.filter(call => call.url === "/api/project")).toHaveLength(0);
    service.active = other;
    await advance(1100);
    expect(screen.getByRole("button", { name: "恢复原校对任务" })).toBeEnabled();
    await click("恢复原校对任务");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
  });

  it("ignores an old other-project poll arriving after the original review has recovered", async () => {
    const service = server();
    await editDraft();
    await detach(service);
    const oldPoll = deferred();
    service.nextStateRead = oldPoll;
    await advance(4500);
    await click("恢复原校对任务");
    await act(async () => { oldPoll.resolve(other); });
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    expect(screen.queryByRole("button", { name: "恢复原校对任务" })).not.toBeInTheDocument();
    expect(warnsOnExit()).toBe(true);
  });

  it("protects a pending checked-only submission across layouts and settings before allowing a new task", async () => {
    const service = server();
    service.write = deferred();
    await act(async () => { render(<App />); });
    await click("编辑第 1 句");
    await click("已核对本句");
    expect(warnsOnExit()).toBe(true);
    await click("新建任务");
    expect(screen.getByRole("alert")).toHaveTextContent(/回执|提交.*等待/);
    expect(screen.getByLabelText("译文（中文）")).toBeDisabled();
    await click("载入任务 另一任务");
    expect(screen.getByRole("alert")).toHaveTextContent(/回执|提交.*等待/);
    expect(service.calls.filter(call => call.url === "/api/project")).toHaveLength(0);
    await click("API 设置");
    expect(warnsOnExit()).toBe(true);
    await click("任务记录");
    expect(warnsOnExit()).toBe(true);
    service.review = { ...review, cues: [{ ...review.cues[0], review_status: "checked" }], manual_review: { ...review.manual_review, revision: "r2" } };
    await act(async () => { service.write.resolve(service.review); });
    expect(warnsOnExit()).toBe(false);
    await click("结果预览");
    expect(screen.getByLabelText("本句核对状态")).toHaveValue("checked");
    await click("新建任务");
    expect(screen.queryByLabelText("译文（中文）")).not.toBeInTheDocument();
    expect(service.calls.filter(call => call.url === "/api/review-cue")).toHaveLength(1);
  });
});

describe("跨窗口运行任务的显示对象", () => {
  it("另一个任务运行时不跳到本窗口空预览，但可只读打开实际项目目录", async () => {
    const service = server();
    service.review = { ...review, cues: [], manual_review: { supported: false } };
    await act(async () => { render(<App />); });
    await detach(service, { ...other, job: { ...other.job, busy: true, status: "running", action: "local" } });

    const preview = screen.getByRole("button", { name: "查看字幕结果", exact: true });
    expect(preview).toBeDisabled();
    expect(preview.title).toMatch(/另.*任务/);
    await click("查看字幕结果");
    expect(screen.getByRole("button", { name: "字幕任务", exact: true })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("新任务尚未创建")).not.toBeInTheDocument();
    const open = screen.getByRole("button", { name: "打开项目目录", exact: true });
    expect(open).toBeEnabled();
    expect(open.title).toContain(other.campaign);
    await click("打开项目目录");
    expect(service.calls.filter(call => call.url === "/api/open")).toEqual([{ url: "/api/open", body: { target: "project", project_id: "other" } }]);
    expect(service.calls.filter(call => ["/api/project", "/api/run", "/api/stop"].includes(call.url))).toHaveLength(0);

    service.active = other;
    await advance(1100);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("original.mp4");
    expect(screen.getByRole("button", { name: "查看字幕结果", exact: true })).toBeEnabled();
    expect(screen.getByRole("button", { name: "打开项目目录", exact: true })).toBeDisabled();
  });

  it("实际运行任务显示云端引擎和英语日语组合，结束后恢复本窗口的本地原文设置", async () => {
    const service = server();
    const languageOptions = { sources: ["ja", "en", "zh"], targets: ["zh-CN", "en", "ja"] };
    service.active = { ...original, language_options: languageOptions, project_config: {
      engine: "local", asr_provider: "whisper_cpp", model: "small", language: "ja", target: "zh-CN", chunk_seconds: 120, workers: 1, translate: false,
    } };
    service.review = { ...review, cues: [], manual_review: { supported: false } };
    await act(async () => { render(<App />); });
    expect(screen.getByRole("radio", { name: /本地识别/ })).toHaveAttribute("aria-checked", "true");
    const running = { ...other, language_options: languageOptions, project_config: {
      asr_provider: "qwen_asr", language: "en", target: "ja", chunk_seconds: 300, workers: 2, translate: true,
    }, job: { ...other.job, busy: true, action: "full", status: "running", total: 10, recognized: 6, translated: 4 } };
    await detach(service, running);

    expect(screen.getByRole("radio", { name: /百炼完整流程/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /本地识别/ })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByLabelText("音频语言")).toHaveValue("en");
    expect(screen.getByLabelText("翻译目标")).toHaveValue("ja");
    expect(screen.getByLabelText("音频语言")).toBeDisabled();
    expect(screen.getByText("英语 → 日语")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "已翻译片段" })).toHaveAttribute("value", "4");
    expect(screen.getByRole("button", { name: "停止任务", exact: true }).title).toContain(other.source);
    expect(service.calls.filter(call => ["/api/project", "/api/run", "/api/stop"].includes(call.url))).toHaveLength(0);
    await click("停止任务");
    expect(service.calls.filter(call => call.url === "/api/stop")).toHaveLength(1);

    service.active = { ...running, job: { ...running.job, busy: false, status: "cancelled" } };
    await advance(1100);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("original.mp4");
    expect(screen.getByRole("radio", { name: /本地识别/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /百炼完整流程/ })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByLabelText("音频语言")).toHaveValue("ja");
    expect(screen.getByLabelText("翻译目标")).toHaveValue("zh-CN");
    expect(screen.getByLabelText("分段长度")).toHaveValue("120");
    expect(screen.getByLabelText("并发任务")).toHaveValue("1");
    expect(screen.getByRole("checkbox", { name: "生成译文与双语字幕" })).not.toBeChecked();
    expect(screen.getByText("日语 · 原文字幕")).toBeInTheDocument();
    expect(service.calls.filter(call => ["/api/project", "/api/run"].includes(call.url))).toHaveLength(0);
  });

  it.each([
    { name: "素材变化", source: "D:\\other.mp4", campaign: original.campaign, title: "other.mp4" },
    { name: "项目目录变化", source: original.source, campaign: "D:\\other", title: "original.mp4" },
  ])("无校对草稿时显示实际运行任务（$name），停止只在点击后发送", async ({ source, campaign, title }) => {
    const service = server();
    service.review = { ...review, cues: [], manual_review: { supported: false } };
    await act(async () => { render(<App />); });
    const running = { ...other, source, campaign, job: { ...other.job, busy: true, status: "running", action: "local", total: 10, recognized: 3 } };
    await detach(service, running);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(title);
    expect(screen.getByLabelText("素材路径")).toHaveValue(source);
    expect(screen.getByLabelText(/项目目录/, { selector: "input" })).toHaveValue(campaign);
    expect(screen.getByText(/当前服务正在处理/).closest(".notice-warning")).toHaveTextContent(title);
    expect(screen.getByRole("progressbar", { name: "已识别片段" })).toHaveAttribute("value", "3");
    const stop = screen.getByRole("button", { name: "停止任务", exact: true });
    expect(stop.title).toContain(source);
    expect(service.calls.filter(call => ["/api/project", "/api/run", "/api/stop"].includes(call.url))).toHaveLength(0);
    expect(screen.getByRole("button", { name: "载入任务 另一任务" })).not.toHaveClass("is-selected");
    await click("停止任务");
    expect(service.calls.filter(call => call.url === "/api/stop")).toEqual([{ url: "/api/stop", body: {} }]);
    expect(service.calls.filter(call => ["/api/project", "/api/run"].includes(call.url))).toHaveLength(0);

    service.active = { ...running, job: { ...running.job, busy: false, status: "cancelled" } };
    await advance(1100);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("original.mp4");
    expect(screen.getByLabelText("素材路径")).toHaveValue(original.source);
    expect(screen.getByLabelText(/项目目录/, { selector: "input" })).toHaveValue(original.campaign);
    expect(screen.queryByRole("button", { name: "停止任务", exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText(/当前服务正在处理/)).not.toBeInTheDocument();
  });

  it("展示另一个运行任务后恢复本窗口尚未提交的新素材和参数", async () => {
    const service = server();
    const languageOptions = { sources: ["ja", "en", "zh"], targets: ["zh-CN", "en", "ja"] };
    service.active = { ...original, language_options: languageOptions };
    await act(async () => { render(<App />); });
    await click("新建任务");
    fireEvent.change(screen.getByLabelText("素材路径"), { target: { value: "D:\\draft.mp4" } });
    fireEvent.change(screen.getByLabelText(/项目目录/, { selector: "input" }), { target: { value: "D:\\draft-output" } });
    fireEvent.change(screen.getByLabelText("音频语言"), { target: { value: "en" } });
    fireEvent.change(screen.getByLabelText("翻译目标"), { target: { value: "ja" } });
    fireEvent.change(screen.getByLabelText("分段长度"), { target: { value: "300" } });
    fireEvent.change(screen.getByLabelText("并发任务"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "生成译文与双语字幕" }));
    await detach(service, { ...other, language_options: languageOptions, job: { ...other.job, busy: true, action: "export-draft", status: "running", export_progress: { phase: "encoding", percent: 20 } } });

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("other.mp4");
    expect(screen.getByLabelText("素材路径")).toHaveValue(other.source);
    expect(screen.getByText(/当前服务正在处理/).closest(".notice-warning")).toHaveTextContent(/保留/);
    expect(screen.getByRole("button", { name: "停止任务", exact: true }).title).toContain(other.source);
    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).toHaveAttribute("value", "20");
    expect(screen.getByRole("button", { name: "开始本地识别" })).toBeDisabled();

    service.active = { ...other, language_options: languageOptions };
    await advance(1100);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("draft.mp4");
    expect(screen.getByLabelText("素材路径")).toHaveValue("D:\\draft.mp4");
    expect(screen.getByLabelText(/项目目录/, { selector: "input" })).toHaveValue("D:\\draft-output");
    expect(screen.getByLabelText("音频语言")).toHaveValue("en");
    expect(screen.getByLabelText("翻译目标")).toHaveValue("ja");
    expect(screen.getByLabelText("分段长度")).toHaveValue("300");
    expect(screen.getByLabelText("并发任务")).toHaveValue("2");
    expect(screen.getByRole("checkbox", { name: "生成译文与双语字幕" })).toBeChecked();
    expect(screen.getByRole("button", { name: "开始本地识别" })).toBeEnabled();
    expect(screen.queryByText(/当前服务正在处理/)).not.toBeInTheDocument();
    expect(service.calls.filter(call => ["/api/project", "/api/run", "/api/stop"].includes(call.url))).toHaveLength(0);
  });
});
