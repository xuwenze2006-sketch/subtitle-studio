import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import App from "./App";
import Preview from "./Preview";
import { createReviewDraft } from "./ManualReview";
import { getReviewSubmissions } from "./reviewSubmissions";
import { createReviewDraftStorage, readReviewDraft, writeReviewDraft } from "./reviewDraftStorage";

const projectKey = JSON.stringify(["D:\\original.mp4", "D:\\original", "original"]);
const snapshot = {
  source: "D:\\original.mp4", campaign: "D:\\original", baseline: "", project_id: "original",
  campaign_status: "prepared", project_config: {}, local_available: true,
  job: { busy: false, action: "", status: "idle", message: "", logs: [] },
  accounts: { bailian: {}, deepseek: {}, siliconflow: {} }, settings: {},
  actions: { local: true, prepare: true, export: false },
  recent: [{ path: "D:\\healthy", title: "正常任务", status: "prepared" }],
};
const cue = { id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "你好", review_status: "unchecked", note: "", warnings: [] };
const preview = {
  project_id: "original", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  media_available: false, source_language: "en", target_language: "zh-CN", cues: [cue], downloads: [],
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, can_accept: false } },
};
const dirtyDraft = () => {
  const draft = createReviewDraft(cue, "r1", "main");
  return { ...draft, fields: { ...draft.fields, target_text: "尚未保存的译文", note: "保留原始校对输入" } };
};
const click = name => act(async () => { fireEvent.click(screen.getByRole("button", { name, exact: true })); });
const warnsOnExit = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};
const deferred = () => {
  let reject;
  const promise = new Promise((_, fail) => { reject = fail; });
  return { promise, reject };
};
function setup({ draft = dirtyDraft(), request = vi.fn(async () => { throw new Error("预览暂时不可用"); }), owner } = {}) {
  const sessionMemory = owner || { current: draft ? { projectKey, selection: "main", reviewDraft: draft } : null };
  const props = { api: { request }, snapshot, connection: "connected", onError: vi.fn(), open: vi.fn(),
    onReviewChanged: vi.fn(), sessionMemory, disabled: false };
  return { ...render(<Preview {...props} />), props, request };
}
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

describe("unavailable previews retain a usable manual-draft recovery choice", () => {
  it("reveals the current draft after a failed read, then retries without silently rebasing it", async () => {
    const { props, request } = setup();
    expect(await screen.findByLabelText("草稿译文")).toHaveValue("尚未保存的译文");
    expect(screen.getByLabelText("草稿核对备注")).toHaveValue("保留原始校对输入");
    expect(screen.queryByRole("button", { name: "保存修改" })).not.toBeInTheDocument();
    expect(warnsOnExit()).toBe(true);
    request.mockResolvedValue({ ...preview, manual_review: { ...preview.manual_review, revision: "r2" } });
    await click("刷新结果");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    expect(props.sessionMemory.current.reviewDraft.revision).toBe("r1");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(screen.getByText(/草稿基准版本已变化/)).toBeInTheDocument();
    expect(request.mock.calls.every(([, body]) => body === undefined)).toBe(true);
  });

  it.each([400, 409])("recovers only this project's persisted main draft after an initial structured %i", async status => {
    writeReviewDraft(projectKey, dirtyDraft());
    const foreignKey = JSON.stringify([snapshot.source, snapshot.campaign, "foreign-project"]);
    const foreign = dirtyDraft();
    writeReviewDraft(foreignKey, { ...foreign, fields: { ...foreign.fields, target_text: "其他项目草稿" } });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: `预览读取失败 ${status}` }), { status })));
    const { createApi } = await import("./api");
    const api = createApi();
    const request = vi.fn((...args) => api.request(...args));
    const { props } = setup({ draft: null, request });
    expect(await screen.findByLabelText("草稿译文")).toHaveValue("尚未保存的译文");
    expect(props.sessionMemory.current.reviewDraft.revision).toBe("r1");
    expect(screen.queryByText("其他项目草稿")).not.toBeInTheDocument();
    expect(warnsOnExit()).toBe(true);
    expect(readReviewDraft(foreignKey, "main").draft.fields.target_text).toBe("其他项目草稿");
    expect(request.mock.calls[0][0]).toBe("/api/preview?project=original");
  });

  it("keeps a retained draft readable and explicitly discardable when manual review is unsupported", async () => {
    const request = vi.fn(async () => ({ ...preview, manual_review: { supported: false } }));
    setup({ request });
    expect(await screen.findByLabelText("草稿译文")).toHaveValue("尚未保存的译文");
    expect(screen.queryByRole("button", { name: "保存修改" })).not.toBeInTheDocument();
    await click("放弃未保存修改");
    expect(screen.queryByLabelText("草稿译文")).not.toBeInTheDocument();
    expect(warnsOnExit()).toBe(false);
    expect(request.mock.calls.every(([, body]) => body === undefined)).toBe(true);
  });

  it("does not discard while a different segment's shared submission is still pending", async () => {
    const draft = dirtyDraft();
    const owner = { current: { projectKey, selection: "main", reviewDraft: draft } };
    const pending = deferred();
    getReviewSubmissions(owner, projectKey).submit("sample-1", { ...draft, sample: "sample-1" }, () => pending.promise);
    setup({ owner });
    expect(await screen.findByLabelText("草稿译文")).toHaveValue("尚未保存的译文");
    expect(screen.getByRole("button", { name: "放弃未保存修改" })).toBeDisabled();
    await click("放弃未保存修改");
    expect(readReviewDraft(projectKey, "main").draft.fields.target_text).toBe("尚未保存的译文");
    await act(async () => { pending.reject(new Error("该片段提交未完成")); });
    expect(screen.getByRole("button", { name: "放弃未保存修改" })).toBeEnabled();
    await click("放弃未保存修改");
    expect(warnsOnExit()).toBe(false);
  });

  it("discards only this window's input while preserving another window's newer persisted draft", async () => {
    writeReviewDraft(projectKey, dirtyDraft());
    setup();
    await screen.findByLabelText("草稿译文");
    const foreignStorage = createReviewDraftStorage({ storage: localStorage });
    foreignStorage.read(projectKey, "main");
    const foreign = dirtyDraft();
    foreignStorage.write(projectKey, { ...foreign, fields: { ...foreign.fields, target_text: "另一窗口后来的草稿" } });
    await click("放弃未保存修改");
    expect(screen.queryByLabelText("草稿译文")).not.toBeInTheDocument();
    expect(readReviewDraft(projectKey, "main").draft.fields.target_text).toBe("另一窗口后来的草稿");
    expect(screen.getByText(/其他窗口.*草稿已保留/)).toBeInTheDocument();
    expect(warnsOnExit()).toBe(false);
  });
});

describe("damaged selected projects still permit an explicit draft-resolution escape", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each([400, 409])("can switch to a healthy project after explicitly discarding a draft hidden by preview %i", async status => {
    const service = { bad: false, active: snapshot, calls: [] };
    const problem = "当前项目配置无法读取，请保留原目录并检查；可载入其他任务或新建任务。";
    vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
      const body = options.body && JSON.parse(options.body);
      service.calls.push({ url, body });
      const respond = (data, code = 200) => new Response(JSON.stringify(data), { status: code });
      if (url === "/api/state?include_video=false") return respond(service.bad ? { ...service.active, project_error: problem, actions: {} } : service.active);
      if (url === "/api/environment") return respond({ version: 1, checks: {}, export_ready: false });
      if (url.startsWith("/api/preview")) return service.bad ? respond({ error: "当前项目预览暂时不可用" }, status) : respond(preview);
      if (url === "/api/project") {
        service.bad = false;
        service.active = { ...snapshot, project_id: "healthy", source: "D:\\healthy.mp4", campaign: "D:\\healthy" };
        return respond(service.active);
      }
      return respond({ ok: true });
    }));
    await act(async () => { render(<App />); });
    await click("编辑第 1 句");
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "尚未保存的译文" } });
    service.bad = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
    expect(screen.getByText(problem)).toBeVisible();
    expect(screen.getByText("本地服务已连接")).toBeVisible();
    await click("API 设置");
    await click("结果预览");
    expect(screen.getByLabelText("草稿译文")).toHaveValue("尚未保存的译文");
    expect(screen.queryByRole("button", { name: "保存修改" })).not.toBeInTheDocument();
    await click("载入任务 正常任务");
    expect(service.calls.filter(call => call.url === "/api/project")).toHaveLength(0);
    await click("新建任务");
    expect(warnsOnExit()).toBe(true);
    expect(screen.getByLabelText("草稿译文")).toHaveValue("尚未保存的译文");
    await click("放弃未保存修改");
    expect(warnsOnExit()).toBe(false);
    await click("载入任务 正常任务");
    expect(service.calls.filter(call => call.url === "/api/project")).toEqual([{ url: "/api/project", body: { campaign: "D:\\healthy" } }]);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("healthy.mp4");
    expect(service.calls.some(call => ["/api/run", "/api/review-cue", "/api/review-accept"].includes(call.url))).toBe(false);
  });
});
