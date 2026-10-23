import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";
import { createReviewDraft } from "./ManualReview";
import { getReviewSubmissions } from "./reviewSubmissions";
import { writeReviewDraft } from "./reviewDraftStorage";

const cue = { id: 1, start_ms: 1000, end_ms: 2000, source_text: "Hello", target_text: "你好", review_status: "unchecked", note: "", warnings: [] };
const draft = { name: "草稿_r1.mp4", artifact_id: "draft-proof-r1" };
const approved = { name: "审核版_r1.mp4", artifact_id: "approved-proof-r1" };
const base = { project_id: "video-download", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue], downloads: ["原文"],
  draft_video: draft, exported_video: approved,
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, issues: 0, can_accept: false } } };
const snapshot = { project_id: "video-download", source: "film.mp4", campaign: "", job: { busy: false }, actions: { "export-draft": true, export: true } };
const notice = "已交给浏览器下载，请在浏览器下载列表中查看进度。";
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const draftButton = () => screen.getByRole("button", { name: "下载草稿 MP4" });
const approvedButton = () => screen.getByRole("button", { name: "下载审核版 MP4" });
function setup(data = base, extra = {}) {
  const props = { api: { request: vi.fn(async () => data), download: vi.fn(), downloadVideo: vi.fn(async () => draft.name) }, snapshot,
    connection: "connected", onError: vi.fn(), onReviewChanged: vi.fn(), open: vi.fn(), run: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, user: userEvent.setup() };
}
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("direct video download controls", () => {
  it.each(["review", "workbench"])("downloads the current original draft in the %s layout", async variant => {
    const data = { ...base, exported_video: null, source_export_supported: true, cues: [{ ...cue, target_text: "" }],
      manual_review: { ...base.manual_review, mode: "source_only", acceptance_supported: false } };
    const { props, user } = setup(data, { variant });
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeEnabled();
    expect(props.api.downloadVideo).not.toHaveBeenCalled();
    await user.click(draftButton());
    expect(props.api.downloadVideo).toHaveBeenCalledExactlyOnceWith("draft-video", "video-download", draft,
      expect.objectContaining({ signal: expect.any(AbortSignal), onError: expect.any(Function) }));
    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(screen.queryByText(/下载完成/)).not.toBeInTheDocument();
    expect(props.api.download).not.toHaveBeenCalled();
  });

  it.each([["草稿", "draft-video", draft], ["审核版", "video", approved]])("downloads the current %s file without regenerating it", async (label, kind, descriptor) => {
    const { props, user } = setup();
    await user.click(await screen.findByRole("button", { name: `下载${label} MP4` }));
    expect(props.api.downloadVideo).toHaveBeenCalledExactlyOnceWith(kind, "video-download", descriptor, expect.any(Object));
    expect(props.run).not.toHaveBeenCalled();
    expect(props.open).not.toHaveBeenCalled();
  });

  it.each([undefined, ""])("disables legacy descriptors without an artifact proof: %s", async artifact_id => {
    setup({ ...base, draft_video: { ...draft, artifact_id }, exported_video: { ...approved, artifact_id } });
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
  });

  it.each([
    ["disabled", { disabled: true }],
    ["job busy", { snapshot: { ...snapshot, job: { busy: true, action: "local" } } }],
    ["different project", { snapshot: { ...snapshot, project_id: "another-project" } }],
  ])("blocks downloads while %s", async (_, extra) => {
    const { props, user } = setup(base, extra);
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
    await user.click(draftButton());
    expect(props.api.downloadVideo).not.toHaveBeenCalled();
  });

  it("blocks both cards while the editor contains unsaved text", async () => {
    const { props, user } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "Unsaved" } });
    expect(draftButton()).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
    expect(props.api.downloadVideo).not.toHaveBeenCalled();
  });

  it("blocks downloads while another segment has a pending review save", async () => {
    const sessionMemory = { current: null }, saving = deferred();
    getReviewSubmissions(sessionMemory, '["film.mp4","","video-download"]').submit("sample-1", createReviewDraft(cue, "r1", "sample-1"), () => saving.promise);
    setup(base, { sessionMemory });
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
    await act(async () => saving.resolve({}));
  });

  it("blocks both downloads while a subtitle version is being saved", async () => {
    const saving = deferred();
    const request = vi.fn(path => path === "/api/save-subtitles" ? saving.promise : Promise.resolve(base));
    const { user } = setup(base, { api: { request, downloadVideo: vi.fn() }, snapshot: { ...snapshot, file_layout: {} } });
    await screen.findByRole("button", { name: "下载草稿 MP4" });
    await user.click(screen.getByRole("button", { name: "保存字幕版本" }));
    expect(draftButton()).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
    await act(async () => saving.resolve({ folder: "exports", files: [], saved_at: "2026-10-23" }));
    expect(draftButton()).toBeEnabled();
  });

  it("blocks downloads when the preview reports a project conflict", async () => {
    setup({ ...base, manual_review: { ...base.manual_review, conflict: "底稿已变化" } });
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
  });

  it("blocks downloads while an orphaned local draft needs recovery", async () => {
    writeReviewDraft('["film.mp4","","video-download"]', createReviewDraft({ ...cue, id: 99 }, "r1", "main"));
    setup();
    expect(await screen.findByRole("button", { name: "下载草稿 MP4" })).toBeDisabled();
    expect(approvedButton()).toBeDisabled();
  });

  it("removes the previous download controls after disconnecting", async () => {
    const { props, rerender } = setup();
    await screen.findByRole("button", { name: "下载草稿 MP4" });
    rerender(<Preview {...props} connection="disconnected" />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "下载草稿 MP4" })).not.toBeInTheDocument());
    expect(props.api.downloadVideo).not.toHaveBeenCalled();
  });

  it("prevents duplicate clicks and overlapping subtitle downloads until the browser handoff", async () => {
    const pending = deferred();
    const downloadVideo = vi.fn(() => pending.promise), api = { request: vi.fn(async () => base), download: vi.fn(), downloadVideo };
    const { user } = setup(base, { api });
    const button = await screen.findByRole("button", { name: "下载草稿 MP4" });
    act(() => { fireEvent.click(button); fireEvent.click(button); });
    expect(downloadVideo).toHaveBeenCalledTimes(1);
    expect(approvedButton()).toBeDisabled();
    expect(screen.getByRole("button", { name: "直接导出草稿 MP4" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "导出带字幕 MP4" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存字幕版本" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下载 原文 SRT" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "下载 原文 SRT" }));
    expect(api.download).not.toHaveBeenCalled();
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
    await act(async () => pending.resolve(draft.name));
    expect(draftButton()).toBeEnabled();
    expect(screen.getByText(notice)).toBeInTheDocument();
  });

  it("reports a preflight failure and permits retry without a success notice", async () => {
    const downloadVideo = vi.fn().mockRejectedValueOnce(new Error("成片已过期")).mockResolvedValueOnce(draft.name);
    const { props, user } = setup(base, { api: { request: vi.fn(async () => base), downloadVideo } });
    await user.click(await screen.findByRole("button", { name: "下载草稿 MP4" }));
    await waitFor(() => expect(props.onError).toHaveBeenCalledExactlyOnceWith("成片已过期"));
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
    expect(draftButton()).toBeEnabled();
    await user.click(draftButton());
    expect(await screen.findByText(notice)).toBeInTheDocument();
  });

  it("aborts a pending preflight when editing makes the displayed revision dirty", async () => {
    const pending = deferred(), downloadVideo = vi.fn(() => pending.promise);
    const { props, user } = setup(base, { api: { request: vi.fn(async () => base), downloadVideo } });
    await user.click(await screen.findByRole("button", { name: "下载草稿 MP4" }));
    const options = downloadVideo.mock.calls[0][3];
    await user.click(screen.getByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "Unsaved while preparing download" } });
    expect(options.signal.aborted).toBe(true);
    await act(async () => { options.onError(new Error("old download")); pending.resolve(draft.name); });
    expect(props.onError).not.toHaveBeenCalled();
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
  });

  it("reports a delayed browser response error and clears the handoff notice", async () => {
    const { props, user } = setup();
    await user.click(await screen.findByRole("button", { name: "下载草稿 MP4" }));
    await screen.findByText(notice);
    act(() => props.api.downloadVideo.mock.calls[0][3].onError(new Error("文件已被替换")));
    expect(props.onError).toHaveBeenCalledExactlyOnceWith("文件已被替换");
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
  });

  it.each(["project", "refresh", "unmount"])("aborts pending preflight on %s and suppresses old completion and callbacks", async transition => {
    const pending = deferred(), downloadVideo = vi.fn(() => pending.promise);
    const { props, user, rerender, unmount } = setup(base, { api: { request: vi.fn(async () => base), downloadVideo } });
    await user.click(await screen.findByRole("button", { name: "下载草稿 MP4" }));
    const options = downloadVideo.mock.calls[0][3];
    if (transition === "project") rerender(<Preview {...props} snapshot={{ ...snapshot, source: "next.mp4", project_id: "next" }} />);
    else if (transition === "refresh") await user.click(screen.getByRole("button", { name: "刷新结果" }));
    else unmount();
    expect(options.signal.aborted).toBe(true);
    await act(async () => { options.onError(new Error("old download")); pending.resolve(draft.name); });
    expect(props.onError).not.toHaveBeenCalled();
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
  });

  it("ignores callbacks from an earlier handoff after a newer download starts", async () => {
    const { props, user } = setup();
    await user.click(await screen.findByRole("button", { name: "下载草稿 MP4" }));
    await screen.findByText(notice);
    const first = props.api.downloadVideo.mock.calls[0][3];
    await user.click(approvedButton());
    act(() => first.onError(new Error("old download")));
    expect(props.onError).not.toHaveBeenCalled();
    expect(screen.getByText(notice)).toBeInTheDocument();
  });
});
