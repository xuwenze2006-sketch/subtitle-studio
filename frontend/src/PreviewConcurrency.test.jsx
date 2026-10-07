import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";

const cue = { id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "原译文", review_status: "unchecked", note: "", warnings: [] };
const base = { project_id: "project-a", selected_id: "main", selections: [{ id: "main", name: "整片" }, { id: "sample-1", name: "样片" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue], downloads: ["译文.srt"],
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, can_accept: true } } };
const saved = { ...base, cues: [{ ...cue, target_text: "新译文" }], manual_review: { ...base.manual_review, revision: "r2" } };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
const exportLabel = mode => mode === "download" ? "下载 译文.srt SRT" : "保存字幕版本";
const exportResult = (mode, label = "当前") => mode === "download" ? `${label}.srt` : { folder: `D:/exports/${label}` };
function setup(mode, operations = [], extra = {}) {
  let next = 0, current = base;
  const api = {
    request: vi.fn(async (path) => {
      if (path === "/api/save-subtitles") return operations[next++].promise;
      if (path === "/api/review-cue") return (current = saved);
      if (path === "/api/review-accept") return { accepted: true };
      return path.includes("sample=sample-1") ? { ...current, selected_id: "sample-1" } : current;
    }),
    download: vi.fn(() => operations[next++].promise),
  };
  const props = { api, snapshot: { project_id: "project-a", source: "film.mp4", job: { busy: false }, file_layout: {} },
    connection: "connected", onError: vi.fn(), open: vi.fn(), onReviewChanged: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, user: userEvent.setup() };
}
async function saveCue(user) {
  await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
  fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "新译文" } });
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  await screen.findByText("本句修改与核对状态已保存。");
}
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

describe("字幕导出与核对保存的并发回执", () => {
  it.each([["download", "success"], ["download", "error"], ["version", "success"], ["version", "error"]])(
    "%s finishes after a cue save without leaving controls locked (%s)", async (mode, outcome) => {
      const pending = deferred();
      const { user, props } = setup(mode, [pending]);
      await user.click(await screen.findByRole("button", { name: exportLabel(mode) }));
      await saveCue(user);
      await act(async () => outcome === "success" ? pending.resolve(exportResult(mode)) : pending.reject(new Error("导出失败")));
      expect(screen.getByRole("button", { name: "下载 译文.srt SRT" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "保存字幕版本" })).toBeEnabled();
      expect(screen.getByLabelText("译文（中文）")).toHaveValue("新译文");
      if (outcome === "success") expect(screen.getByText(mode === "download" ? /已交给浏览器：当前.srt/ : "字幕版本已保存")).toBeInTheDocument();
      else expect(props.onError).toHaveBeenCalledWith("导出失败");
    },
  );

  it.each([["download", "sample"], ["version", "sample"], ["download", "project"], ["version", "project"]])(
    "an old %s acknowledgement cannot unlock a newer request after changing %s", async (mode, context) => {
      const old = deferred(), current = deferred();
      const { user, props, rerender } = setup(mode, [old, current]);
      await user.click(await screen.findByRole("button", { name: exportLabel(mode) }));
      if (context === "sample") await user.selectOptions(screen.getByLabelText("预览片段"), "sample-1");
      else rerender(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "project-b" }} />);
      await user.click(await screen.findByRole("button", { name: exportLabel(mode) }));
      await act(async () => old.resolve(exportResult(mode, "旧的")));
      expect(screen.getByRole("button", { name: exportLabel(mode) })).toBeDisabled();
      expect(screen.queryByText(/旧的/)).not.toBeInTheDocument();
      await act(async () => current.resolve(exportResult(mode)));
      expect(screen.getByRole("button", { name: exportLabel(mode) })).toBeEnabled();
      expect(screen.getByText(mode === "download" ? /已交给浏览器：当前.srt/ : "字幕版本已保存")).toBeInTheDocument();
    },
  );
});

describe("核对成功后的任务状态刷新", () => {
  it.each(["refresh", "save", "accept"])("ignores an old state failure while a newer %s receipt is pending", async operation => {
    const oldState = deferred(), nextReceipt = deferred();
    const onReviewChanged = vi.fn().mockReturnValueOnce(oldState.promise).mockResolvedValue(undefined);
    let props;
    await act(async () => { ({ props } = setup(null, [], { onReviewChanged })); });
    fireEvent.click(screen.getByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "新译文" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "保存修改" })); });
    expect(screen.getByText("本句修改与核对状态已保存。")).toBeInTheDocument();
    props.api.request.mockImplementationOnce(() => nextReceipt.promise);
    if (operation === "refresh") fireEvent.click(screen.getByRole("button", { name: "刷新核对状态" }));
    else if (operation === "save") fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    else {
      fireEvent.click(screen.getByLabelText("我已听看抽检并确认疑点已处理"));
      fireEvent.click(screen.getByRole("button", { name: "完成整片验收" }));
    }
    await act(async () => oldState.reject(new Error("旧状态读取超时")));
    await act(async () => nextReceipt.resolve(operation === "accept" ? { accepted: true } : saved));
    expect(onReviewChanged).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not show a stale refresh failure after a newer manual refresh succeeds", async () => {
    const pending = deferred();
    const onReviewChanged = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(undefined);
    const { user } = setup(null, [], { onReviewChanged });
    await saveCue(user);
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    await act(async () => pending.reject(new Error("旧状态读取超时")));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("已读取最新核对状态。")).toBeInTheDocument();
  });

  it.each(["cue", "accept"])("keeps the successful %s receipt when state refresh fails and supports an explicit retry", async operation => {
    const onReviewChanged = vi.fn().mockRejectedValueOnce(new Error("本地状态读取超时")).mockResolvedValue(undefined);
    const { user, props } = setup(null, [], { onReviewChanged });
    if (operation === "cue") await saveCue(user);
    else {
      await user.click(await screen.findByLabelText("我已听看抽检并确认疑点已处理"));
      await user.click(screen.getByRole("button", { name: "完成整片验收" }));
    }
    expect(await screen.findByRole("alert")).toHaveTextContent(/已保存|已记录/);
    expect(screen.getByRole("alert")).toHaveTextContent(/任务状态刷新失败/);
    expect(screen.getByText(operation === "cue" ? "本句修改与核对状态已保存。" : "整片验收已记录，可继续导出带字幕视频。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(onReviewChanged).toHaveBeenCalledTimes(2);
    expect(props.api.request.mock.calls.filter(([path]) => path === (operation === "cue" ? "/api/review-cue" : "/api/review-accept"))).toHaveLength(1);
  });
});
