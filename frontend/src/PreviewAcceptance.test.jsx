import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import Preview from "./Preview";
import App from "./App";

const cue = { id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "你好",
  review_status: "checked", note: "", warnings: [] };
const base = { project_id: "accept-project", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue], downloads: [],
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 1, can_accept: true } } };
const snapshot = { source: "film.mp4", campaign: "campaign", baseline: "", project_id: "accept-project", project_config: {},
  recent: [], local_available: false, job: { busy: false, action: "", status: "idle", message: "", logs: [] },
  accounts: {}, settings: {}, actions: {}, campaign_status: "prepared" };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
const respond = body => new Response(JSON.stringify(body), { status: 200 });
const unload = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};
async function accept() {
  fireEvent.click(await screen.findByLabelText("我已听看抽检并确认疑点已处理"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "完成整片验收" })));
}
function setup(pending, { failPreview = false, onReviewChanged = vi.fn() } = {}) {
  let reads = 0;
  const request = vi.fn(path => {
    if (path === "/api/review-accept") return pending.promise;
    if (failPreview && ++reads > 1) return Promise.reject(new Error("核对读取超时"));
    return Promise.resolve(base);
  });
  const props = { api: { request }, snapshot, connection: "connected", onError: vi.fn(), open: vi.fn(),
    onReviewChanged, sessionMemory: { current: null } };
  return { ...render(<Preview {...props} />), props, request };
}
beforeEach(() => vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {}));

describe("session-owned final acceptance receipts", () => {
  it("keeps acceptance pending across layouts and prevents a second write", async () => {
    const pending = deferred();
    const { props, request, unmount } = setup(pending);
    await accept();
    unmount();
    render(<Preview {...props} variant="workbench" />);
    const confirm = await screen.findByLabelText("我已听看抽检并确认疑点已处理");
    expect(confirm).toBeDisabled();
    expect(screen.getByRole("button", { name: "完成整片验收" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "完成整片验收" }));
    expect(request.mock.calls.filter(([path]) => path === "/api/review-accept")).toHaveLength(1);
    await act(async () => pending.resolve({ accepted: true }));
    expect(await screen.findByText(/整片验收已记录/)).toBeInTheDocument();
  });

  it.each(["success", "conflict", "error"])("retains an acceptance %s received while no layout is mounted", async outcome => {
    const pending = deferred();
    const { props, request, unmount } = setup(pending);
    await accept();
    unmount();
    await act(async () => outcome === "success" ? pending.resolve({ accepted: true })
      : pending.reject(new Error(outcome === "conflict" ? "校对版本冲突" : "服务暂时不可用")));
    render(<Preview {...props} />);
    if (outcome === "success") expect(await screen.findByText(/整片验收已记录/)).toBeInTheDocument();
    else {
      expect(await screen.findByRole("alert")).toHaveTextContent(outcome === "conflict" ? "校对版本冲突" : "服务暂时不可用");
      if (outcome === "conflict") expect(screen.getByRole("button", { name: "完成整片验收" })).toBeDisabled();
    }
    expect(request.mock.calls.filter(([path]) => path === "/api/review-accept")).toHaveLength(1);
  });

  it("retains a successful acceptance when the following preview read fails, without repeating the write", async () => {
    const pending = deferred();
    const { props, request, unmount } = setup(pending, { failPreview: true });
    await accept();
    unmount();
    await act(async () => pending.resolve({ accepted: true }));
    // Remount reads are healthy again; only the post-acceptance read failed.
    request.mockImplementation(path => path === "/api/review-accept" ? pending.promise : Promise.resolve(base));
    render(<Preview {...props} />);
    expect(await screen.findByText("整片验收已记录，可继续导出带字幕视频。")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(/验收已记录.*刷新失败.*核对读取超时/);
    expect(request.mock.calls.filter(([path]) => path === "/api/review-accept")).toHaveLength(1);
  });

  it("does not replace a newer remount read with acceptance follow-up data from the previous layout", async () => {
    const pending = deferred(), stateRead = deferred();
    const { props, request, unmount } = setup(pending, { onReviewChanged: () => stateRead.promise });
    await accept();
    unmount();
    await act(async () => pending.resolve({ accepted: true }));
    const latest = { ...base, cues: [{ ...cue, target_text: "newer target" }],
      manual_review: { ...base.manual_review, revision: "r2" } };
    request.mockImplementation(path => path === "/api/review-accept" ? pending.promise : Promise.resolve(latest));
    render(<Preview {...props} />);
    await screen.findByText("newer target", { selector: ".cue-translation" });
    await act(async () => stateRead.resolve());
    expect(screen.getByText("newer target", { selector: ".cue-translation" })).toBeInTheDocument();
    expect(screen.getByText(/验收已记录.*字幕版本已变化/)).toBeInTheDocument();
  });

  it("protects unload and project changes while acceptance is pending on settings", async () => {
    const pending = deferred(), calls = [];
    vi.stubGlobal("fetch", vi.fn(async url => {
      calls.push(url);
      if (url === "/api/review-accept") return pending.promise;
      return respond(url === "/api/state" ? snapshot : url.startsWith("/api/preview") ? base
        : url === "/api/environment" ? { checks: {}, export_ready: false } : {});
    }));
    await act(async () => render(<App />));
    await accept();
    fireEvent.click(screen.getByRole("button", { name: "API 设置", exact: true }));
    expect(unload()).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "新建任务", exact: true }));
    expect(await screen.findByText(/校对提交仍在等待回执/)).toBeInTheDocument();
    expect(calls.filter(path => path === "/api/project")).toHaveLength(0);
    await act(async () => pending.resolve(respond({ accepted: true })));
    expect(await screen.findByText(/整片验收已记录/)).toBeInTheDocument();
    expect(unload()).toBe(false);
  });
});
