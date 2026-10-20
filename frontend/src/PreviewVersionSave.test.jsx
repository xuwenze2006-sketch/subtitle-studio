import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";

const base = {
  project_id: "project-a", selected_id: "main",
  selections: [{ id: "main", name: "整片" }, { id: "sample-1", name: "样片" }],
  source_language: "en", target_language: "zh", media_available: false,
  cues: [{ id: 1, start_ms: 1000, end_ms: 2000, source_text: "Synthetic source", target_text: "合成测试" }],
  downloads: ["译文.srt"], issues: [],
};
const pendingOperations = [];
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  const operation = { promise, resolve, reject };
  pendingOperations.push(operation);
  return operation;
}
const receipt = name => ({ folder: `D:/synthetic/exports/${name}`, files: [], saved_at: "2026-10-20T21:00:00+08:00" });
const writes = props => props.api.request.mock.calls.filter(([path]) => path === "/api/save-subtitles");
function previewFor(path) {
  const query = new URL(path, "http://localhost").searchParams;
  return { ...base, project_id: query.get("project") || "project-a", selected_id: query.get("sample") || "main" };
}
function setup(write, read = path => Promise.resolve(previewFor(path))) {
  const request = vi.fn((path, body) => path === "/api/save-subtitles" ? write(body) : read(path));
  const props = {
    api: { request, download: vi.fn(async () => "synthetic.srt") },
    snapshot: { project_id: "project-a", source: "synthetic.mp4", job: { busy: false }, file_layout: {} },
    connection: "connected", onError: vi.fn(), open: vi.fn(), sessionMemory: { current: null },
  };
  return { ...render(<Preview {...props} />), props, user: userEvent.setup() };
}
const saveButton = () => screen.getByRole("button", { name: "保存字幕版本" });
const downloadButton = () => screen.getByRole("button", { name: "下载 译文.srt SRT" });
const beforeUnload = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};
beforeEach(() => vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {}));
afterEach(async () => {
  await act(async () => pendingOperations.splice(0).forEach(operation => operation.resolve(receipt("cleanup"))));
});

describe("字幕版本保存跨布局回执", () => {
  it("keeps the pending save and download exclusion when the layout remounts", async () => {
    const pending = deferred();
    const { props, user, unmount } = setup(() => pending.promise);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    unmount();
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByRole("button", { name: "保存字幕版本" })).toBeDisabled();
    expect(downloadButton()).toBeDisabled();
    await user.click(saveButton());
    await user.click(downloadButton());
    expect(writes(props)).toHaveLength(1);
    expect(props.api.download).not.toHaveBeenCalled();
    await act(async () => pending.resolve(receipt("original")));
    expect(screen.getByText("D:/synthetic/exports/original")).toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
    expect(downloadButton()).toBeEnabled();
  });

  it("restores a successful receipt delivered while every layout was unmounted", async () => {
    const pending = deferred();
    const { props, user, unmount } = setup(() => pending.promise);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    unmount();
    await act(async () => pending.resolve(receipt("unmounted")));
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByText("D:/synthetic/exports/unmounted")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "打开导出目录" }));
    expect(props.open).toHaveBeenCalledWith("exports", "project-a");
    expect(writes(props)).toHaveLength(1);
  });

  it("reports an unmounted failure on return and retries only on an explicit save", async () => {
    const pending = deferred();
    let attempts = 0;
    const { props, user, unmount } = setup(() => ++attempts === 1 ? pending.promise : Promise.resolve(receipt("retried")));
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    unmount();
    await act(async () => pending.reject(new Error("磁盘写入失败")));
    expect(beforeUnload()).toBe(false);
    expect(props.onError).not.toHaveBeenCalled();
    render(<Preview {...props} />);
    await waitFor(() => expect(props.onError).toHaveBeenCalledWith("磁盘写入失败"));
    expect(writes(props)).toHaveLength(1);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    expect(await screen.findByText("D:/synthetic/exports/retried")).toBeInTheDocument();
    expect(writes(props)).toHaveLength(2);
  });

  it("keeps the previous confirmed folder during a later save and after its failure", async () => {
    const pending = deferred();
    let attempts = 0;
    const { props, user } = setup(() => ++attempts === 1 ? Promise.resolve(receipt("confirmed")) : pending.promise);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    await screen.findByText("D:/synthetic/exports/confirmed");
    await user.click(saveButton());
    expect(screen.getByText("D:/synthetic/exports/confirmed")).toBeInTheDocument();
    await act(async () => pending.reject(new Error("没有可用空间")));
    expect(props.onError).toHaveBeenCalledWith("没有可用空间");
    expect(screen.getByText("D:/synthetic/exports/confirmed")).toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
  });

  it("does not clear an in-flight save when connection and preview reads restart", async () => {
    const pending = deferred();
    const { props, user, rerender } = setup(() => pending.promise);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    rerender(<Preview {...props} connection="disconnected" />);
    rerender(<Preview {...props} />);
    expect(await screen.findByRole("button", { name: "保存字幕版本" })).toBeDisabled();
    await user.click(saveButton());
    expect(writes(props)).toHaveLength(1);
    await act(async () => pending.resolve(receipt("reconnected")));
    expect(screen.getByText("D:/synthetic/exports/reconnected")).toBeInTheDocument();
  });

  it("keeps a confirmed folder visible when a later preview read fails", async () => {
    let reads = 0;
    const { props, user } = setup(() => Promise.resolve(receipt("confirmed")), path => ++reads === 1
      ? Promise.resolve(previewFor(path)) : Promise.reject(new Error("字幕读取失败")));
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    await screen.findByText("D:/synthetic/exports/confirmed");
    await user.click(screen.getByRole("button", { name: "刷新结果" }));
    await waitFor(() => expect(props.onError).toHaveBeenCalledWith("字幕读取失败"));
    expect(screen.getByText("D:/synthetic/exports/confirmed")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "打开导出目录" }));
    expect(props.open).toHaveBeenCalledWith("exports", "project-a");
    expect(writes(props)).toHaveLength(1);
  });

  it("shows a save receipt completed before a replacement preview GET settles", async () => {
    const saving = deferred(), reading = deferred();
    let reads = 0;
    const { props, user, unmount } = setup(() => saving.promise, path => ++reads === 1 ? Promise.resolve(previewFor(path)) : reading.promise);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    unmount();
    render(<Preview {...props} />);
    await act(async () => saving.resolve(receipt("newer-than-read")));
    expect(screen.getByText("D:/synthetic/exports/newer-than-read")).toBeInTheDocument();
    await act(async () => reading.resolve(base));
    expect(screen.getByText("D:/synthetic/exports/newer-than-read")).toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
    expect(writes(props)).toHaveLength(1);
  });

  it.each(["sample", "project"])("isolates pending saves and receipts by exact %s and restores them on return", async context => {
    const original = deferred(), current = deferred();
    let attempts = 0;
    const { props, user, rerender } = setup(() => ++attempts === 1 ? original.promise : current.promise);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    if (context === "sample") await user.selectOptions(screen.getByLabelText("预览片段"), "sample-1");
    else rerender(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "project-b" }} />);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    expect(beforeUnload()).toBe(true);
    await act(async () => original.resolve(receipt("original-context")));
    expect(beforeUnload()).toBe(true);
    expect(screen.queryByText("D:/synthetic/exports/original-context")).not.toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    await act(async () => current.resolve(receipt("current-context")));
    expect(beforeUnload()).toBe(false);
    expect(screen.getByText("D:/synthetic/exports/current-context")).toBeInTheDocument();
    if (context === "sample") await user.selectOptions(screen.getByLabelText("预览片段"), "main");
    else rerender(<Preview {...props} />);
    expect(await screen.findByText("D:/synthetic/exports/original-context")).toBeInTheDocument();
    expect(screen.queryByText("D:/synthetic/exports/current-context")).not.toBeInTheDocument();
    expect(writes(props).map(([, body]) => body)).toEqual(context === "sample"
      ? [{ project_id: "project-a", sample: "main" }, { project_id: "project-a", sample: "sample-1" }]
      : [{ project_id: "project-a", sample: "main" }, { project_id: "project-b", sample: "main" }]);
  });

  it("uses one synchronous save owner even if two layouts have already rendered an enabled button", async () => {
    const pending = deferred();
    const first = setup(() => pending.promise);
    await screen.findByRole("button", { name: "保存字幕版本" });
    const second = render(<Preview {...first.props} variant="workbench" />);
    await within(second.container).findByRole("button", { name: "保存字幕版本" });
    await act(async () => {
      within(first.container).getByRole("button", { name: "保存字幕版本" }).click();
      within(second.container).getByRole("button", { name: "保存字幕版本" }).click();
    });
    expect(writes(first.props)).toHaveLength(1);
    await act(async () => pending.resolve(receipt("shared")));
    expect(within(first.container).getByText("D:/synthetic/exports/shared")).toBeInTheDocument();
    expect(within(second.container).getByText("D:/synthetic/exports/shared")).toBeInTheDocument();
  });

  it("guards browser unload during a save even after the preview unmounts and releases it on completion", async () => {
    const pending = deferred();
    const { user, unmount } = setup(() => pending.promise);
    expect(beforeUnload()).toBe(false);
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    expect(beforeUnload()).toBe(true);
    unmount();
    expect(beforeUnload()).toBe(true);
    await act(async () => pending.resolve(receipt("unload-safe")));
    expect(beforeUnload()).toBe(false);
  });

  it("ignores the second mouse click of a fast double click but accepts later explicit and keyboard clicks", async () => {
    let attempts = 0;
    const { props, user } = setup(async () => receipt(`version-${++attempts}`));
    await user.dblClick(await screen.findByRole("button", { name: "保存字幕版本" }));
    expect(writes(props)).toHaveLength(1);
    expect(screen.getByText("D:/synthetic/exports/version-1")).toBeInTheDocument();
    await user.click(saveButton());
    expect(writes(props)).toHaveLength(2);
    saveButton().focus();
    await user.keyboard("{Enter}");
    expect(writes(props)).toHaveLength(3);
    expect(screen.getByText("D:/synthetic/exports/version-3")).toBeInTheDocument();
  });

  it("keeps download then version-save exclusion for the current view", async () => {
    const pending = deferred();
    const { props, user } = setup(async () => receipt("after-download"));
    props.api.download.mockReturnValueOnce(pending.promise);
    await user.click(await screen.findByRole("button", { name: "下载 译文.srt SRT" }));
    expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton());
    expect(writes(props)).toHaveLength(0);
    await act(async () => pending.resolve("downloaded.srt"));
    await user.click(saveButton());
    expect(screen.getByText("D:/synthetic/exports/after-download")).toBeInTheDocument();
  });
});
