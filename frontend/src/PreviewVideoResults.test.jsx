import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Preview from "./Preview";

const cue = { id: 1, start_ms: 1000, end_ms: 2000, source_text: "Hello", target_text: "你好", review_status: "unchecked", note: "", warnings: [] };
const draft = { name: "草稿_r1.mp4", artifact_id: "proof-r1" };
const base = { project_id: "video-results", selected_id: "main", selections: [{ id: "main", name: "整片" }, { id: "sample-1", name: "样片 1" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue], downloads: ["原文"],
  video_results_pending: true, draft_video: null, exported_video: null,
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, issues: 0, can_accept: false } } };
const snapshot = { project_id: "video-results", source: "film.mp4", campaign: "", job: { busy: false }, actions: { "export-draft": true } };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const results = (extra = {}) => ({ project_id: base.project_id, selected_id: "main", revision: "r1", draft_video: draft, exported_video: null, ...extra });
function setup(extra = {}) {
  const checks = [], saves = [], reads = [];
  const request = vi.fn((path, body, options) => {
    if (path.startsWith("/api/video-results")) { const pending = deferred(); checks.push({ ...pending, path, options }); return pending.promise; }
    if (path === "/api/review-cue" || path === "/api/save-subtitles") { const pending = deferred(); saves.push({ ...pending, body }); return pending.promise; }
    const params = new URLSearchParams(path.split("?")[1]);
    const data = { ...base, selected_id: params.get("sample") || "main", ...(extra.data || {}) };
    reads.push(data);
    return Promise.resolve(data);
  });
  const props = { api: { request, download: vi.fn(), downloadVideo: vi.fn() }, snapshot, connection: "connected",
    onError: vi.fn(), onReviewChanged: vi.fn(), open: vi.fn(), run: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, checks, saves, reads, request };
}
beforeEach(() => vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {}));
afterEach(() => vi.useRealTimers());

describe("independent MP4 result verification", () => {
  it("keeps editing and saving responsive, merges only video descriptors, and never checks per keystroke", async () => {
    const { checks, request, props } = setup();
    expect(await screen.findByText("正在检查已导出 MP4…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编辑第 1 句" }));
    const editor = screen.getByLabelText("原文（英语）");
    fireEvent.change(editor, { target: { value: "Unsaved during check" } });
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    expect(checks).toHaveLength(1);
    const params = new URL(checks[0].path, "http://localhost").searchParams;
    expect(Object.fromEntries(params)).toEqual({ project: "video-results", sample: "main", revision: "r1" });
    await act(async () => checks[0].resolve({ ...results(), cues: [{ ...cue, source_text: "Stale server cue" }] }));
    expect(editor).toHaveValue("Unsaved during check");
    expect(screen.queryByText("Stale server cue")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载草稿 MP4" })).toBeDisabled();
    expect(screen.queryByText("正在检查已导出 MP4…")).not.toBeInTheDocument();
    expect(request.mock.calls.filter(([path]) => path.startsWith("/api/video-results"))).toHaveLength(1);
    expect(props.onError).not.toHaveBeenCalled();
  });

  it("keeps a slow check alive after 15 seconds with a localized explanation", async () => {
    const { checks, props } = setup();
    await screen.findByText("正在检查已导出 MP4…");
    vi.useFakeTimers();
    // Start the independent timer while timers are controlled.
    fireEvent.click(screen.getByRole("button", { name: "刷新结果" }));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(15001));
    expect(screen.getByText(/大文件.*较长时间/)).toBeInTheDocument();
    expect(checks.at(-1).options.signal.aborted).toBe(false);
    expect(props.onError).not.toHaveBeenCalled();
    await act(async () => checks.at(-1).resolve(results()));
    expect(screen.getByRole("button", { name: "下载草稿 MP4" })).toBeEnabled();
  });

  it("shows a local error and retries only on explicit request", async () => {
    const { checks, props } = setup();
    await waitFor(() => expect(checks).toHaveLength(1));
    await act(async () => checks[0].reject(new Error("受控读取失败")));
    expect(screen.getByText(/已导出 MP4 检查失败/)).toBeInTheDocument();
    expect(props.onError).not.toHaveBeenCalled();
    expect(checks).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "重试 MP4 检查" }));
    await waitFor(() => expect(checks).toHaveLength(2));
    await act(async () => checks[1].resolve(results({ draft_video: null })));
    expect(screen.queryByText(/正在检查已导出 MP4|已导出 MP4 检查失败/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument();
  });

  it.each(["selection", "project", "refresh", "busy", "unmount"])("aborts on %s and ignores a transport that completes after cancellation", async transition => {
    const { checks, props, rerender, unmount } = setup();
    await waitFor(() => expect(checks).toHaveLength(1));
    if (transition === "selection") fireEvent.change(screen.getByLabelText("预览片段"), { target: { value: "sample-1" } });
    else if (transition === "project") rerender(<Preview {...props} snapshot={{ ...snapshot, project_id: "next", source: "next.mp4" }} />);
    else if (transition === "refresh") fireEvent.click(screen.getByRole("button", { name: "刷新结果" }));
    else if (transition === "busy") rerender(<Preview {...props} snapshot={{ ...snapshot, job: { busy: true } }} />);
    else unmount();
    expect(checks[0].options.signal.aborted).toBe(true);
    await act(async () => checks[0].resolve(results()));
    expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument();
    expect(props.onError).not.toHaveBeenCalled();
  });

  it.each(["pending", "receipt"])("rejects stale video results after review save %s", async boundary => {
    const { checks, saves } = setup();
    await waitFor(() => expect(checks).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "Saved new text" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(checks[0].options.signal.aborted).toBe(true);
    if (boundary === "receipt") await act(async () => saves[0].resolve({ kind: "review-cue", project_id: base.project_id, selected_id: "main", base_revision: "r1",
      cue: { ...cue, source_text: "Saved new text" }, manual_review: { ...base.manual_review, revision: "r2" } }));
    await act(async () => checks[0].resolve(results()));
    expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Saved new text");
    expect(checks).toHaveLength(boundary === "receipt" ? 2 : 1);
    if (boundary === "receipt") {
      expect(new URL(checks[1].path, "http://localhost").searchParams.get("revision")).toBe("r2");
      await act(async () => checks[1].resolve(results({ revision: "r2", draft_video: null })));
    }
  });

  it("rechecks with a new owner after a transient disabled interval", async () => {
    const { checks, props, rerender } = setup();
    await waitFor(() => expect(checks).toHaveLength(1));
    rerender(<Preview {...props} disabled />);
    expect(checks[0].options.signal.aborted).toBe(true);
    rerender(<Preview {...props} disabled={false} />);
    await waitFor(() => expect(checks).toHaveLength(2));
    await act(async () => checks[0].resolve(results()));
    expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument();
    await act(async () => checks[1].resolve(results()));
    expect(screen.getByRole("button", { name: "下载草稿 MP4" })).toBeEnabled();
  });

  it("does not automatically retry a failed check after a transient disabled interval", async () => {
    const { checks, props, rerender } = setup();
    await waitFor(() => expect(checks).toHaveLength(1));
    await act(async () => checks[0].reject(new Error("受控读取失败")));
    rerender(<Preview {...props} disabled />);
    rerender(<Preview {...props} disabled={false} />);
    expect(checks).toHaveLength(1);
    expect(screen.getByRole("button", { name: "重试 MP4 检查" })).toBeEnabled();
  });

  it("rejects stale checks throughout subtitle version save and its receipt", async () => {
    const { checks, saves } = setup({ snapshot: { ...snapshot, file_layout: {} } });
    await waitFor(() => expect(checks).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "保存字幕版本" }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(checks[0].options.signal.aborted).toBe(true);
    await act(async () => saves[0].resolve({ folder: "exports", files: [] }));
    await act(async () => checks[0].resolve(results()));
    expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument();
    expect(checks).toHaveLength(2);
    await act(async () => checks[1].resolve(results()));
    expect(screen.getByRole("button", { name: "下载草稿 MP4" })).toBeEnabled();
  });

  it.each([{ project_id: "other" }, { selected_id: "sample-1" }, { revision: "r2" }])("rejects mismatched result identity %j", async mismatch => {
    const { checks } = setup();
    await waitFor(() => expect(checks).toHaveLength(1));
    await act(async () => checks[0].resolve(results(mismatch)));
    expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试 MP4 检查" })).toBeEnabled();
  });

  it("does not launch checks for legacy previews without an explicit pending flag", async () => {
    const { checks } = setup({ data: { video_results_pending: undefined, draft_video: draft } });
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeEnabled();
    expect(checks).toHaveLength(0);
  });
});
