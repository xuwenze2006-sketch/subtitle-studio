import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";
import { createReviewDraft } from "./ManualReview";
import { writeReviewDraft } from "./reviewDraftStorage";
import { getReviewSubmissions } from "./reviewSubmissions";

const cue = { id: 1, start_ms: 1000, end_ms: 2000, source_text: "Hello", target_text: "", translation_stale: false, review_status: "unchecked", note: "", warnings: [] };
const source = { project_id: "source-video", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  source_language: "en", target_language: "zh", source_export_supported: true, media_available: false, cues: [cue], downloads: ["原文"],
  manual_review: { supported: true, mode: "source_only", acceptance_supported: false, revision: "r1", summary: { total: 1, checked: 0, issues: 0, pending_translation: 0, can_accept: false } } };
const snapshot = { project_id: "source-video", source: "film.mp4", campaign: "", job: { busy: false }, actions: { "export-draft": true, export: false } };
const projectKey = '["film.mp4","","source-video"]';
const oldVideo = { name: "影片_原文_未审核草稿_r1.mp4", review_status: "unreviewed_draft", media_url: "/api/draft-video", download_url: "/api/download?name=draft-video" };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function setup(data = source, extra = {}) {
  const props = { api: { request: vi.fn(async () => data) }, snapshot,
    connection: "connected", onError: vi.fn(), onReviewChanged: vi.fn(), open: vi.fn(), run: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, user: userEvent.setup() };
}
const exportButton = () => screen.getByRole("button", { name: "直接导出草稿 MP4" });
const findExport = async () => {
  await screen.findByRole("button", { name: "编辑第 1 句" });
  return screen.findByRole("button", { name: "直接导出草稿 MP4" });
};
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("original subtitle video export", () => {
  it.each(["review", "workbench"])("exposes an explicit unreviewed original MP4 action in the %s layout", async variant => {
    const { props, user } = setup(source, { variant });
    expect(await findExport()).toBeEnabled();
    const card = screen.getByRole("region", { name: "草稿成片导出" });
    expect(within(card).getByText("未审核草稿")).toBeInTheDocument();
    expect(within(card).getByText(/当前已保存的原文字幕/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "完成整片验收" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "导出带字幕 MP4" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("已核对修改后的译文")).not.toBeInTheDocument();
    expect(props.run).not.toHaveBeenCalled();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    await user.click(exportButton());
    expect(props.run).toHaveBeenCalledExactlyOnceWith("export-draft");
    if (variant === "review") expect(screen.getByText(/逐句核对并保存后.*MP4/)).toBeInTheDocument();
  });

  it.each([undefined, false, "true", 1])("keeps legacy source responses hidden unless capability is exactly true: %s", async capability => {
    setup({ ...source, source_export_supported: capability, draft_video: oldVideo });
    await screen.findByRole("button", { name: "编辑第 1 句" });
    expect(screen.queryByRole("region", { name: "草稿成片导出" })).not.toBeInTheDocument();
    expect(screen.queryByText(/MP4/)).not.toBeInTheDocument();
  });

  it("opens the current original draft and folder only after the respective click", async () => {
    const { props, user } = setup({ ...source, draft_video: oldVideo });
    await screen.findByText(oldVideo.name);
    expect(props.open).not.toHaveBeenCalled();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "播放草稿" }));
    expect(props.open).toHaveBeenLastCalledWith("draft-video", "source-video");
    await user.click(screen.getByRole("button", { name: "打开草稿目录" }));
    expect(props.open).toHaveBeenLastCalledWith("draft-video-folder", "source-video");
    expect(props.run).not.toHaveBeenCalled();
  });

  it.each([
    ["disabled", { disabled: true }],
    ["unavailable", { snapshot: { ...snapshot, actions: { "export-draft": false } } }],
    ["missing action", { snapshot: { ...snapshot, actions: {} } }],
    ["missing runner", { run: undefined }],
    ["encoding", { snapshot: { ...snapshot, job: { busy: true, action: "export-draft" } } }],
    ["other task", { snapshot: { ...snapshot, job: { busy: true, action: "local" } } }],
  ])("prevents new export while %s", async (_, extra) => {
    const { props, user } = setup(source, extra);
    expect(await findExport()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run || vi.fn()).not.toHaveBeenCalled();
  });

  it("removes the export action when disconnected", async () => {
    const { props, rerender } = setup();
    await findExport();
    rerender(<Preview {...props} connection="disconnected" />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "直接导出草稿 MP4" })).not.toBeInTheDocument());
    expect(props.run).not.toHaveBeenCalled();
  });

  it("prevents exporting previous disk subtitles while the editor has unsaved input", async () => {
    const { props, user } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "Uncommitted correction" } });
    expect(exportButton()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
  });

  it.each(["orphan", "bilingual", "revision"])("blocks a recovered %s draft", async kind => {
    const draft = createReviewDraft({ ...cue, id: kind === "orphan" ? 99 : 1 }, kind === "revision" ? "r0" : "r1", "main");
    writeReviewDraft(projectKey, kind === "bilingual" ? { ...draft, fields: { ...draft.fields, target_text: "旧译文" } } : draft);
    const { props, user } = setup();
    expect(await findExport()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
  });

  it("blocks a project conflict even without an active editor", async () => {
    const { props, user } = setup({ ...source, manual_review: { ...source.manual_review, conflict: "项目底稿已变化" } });
    expect(await findExport()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
  });

  it("blocks pending review writes owned by a detached sample store", async () => {
    const sessionMemory = { current: null }, saving = deferred();
    const submissions = getReviewSubmissions(sessionMemory, projectKey);
    submissions.submit("sample-1", createReviewDraft(cue, "r1", "sample-1"), () => saving.promise);
    const { props, user } = setup(source, { sessionMemory });
    expect(await findExport()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
    await act(async () => saving.resolve({}));
  });

  it("keeps a pending review write blocked after moving to the other layout", async () => {
    const saving = deferred();
    const request = vi.fn(path => path === "/api/review-cue" ? saving.promise : Promise.resolve(source));
    const { props, user, unmount } = setup(source, { api: { request } });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    unmount();
    render(<Preview {...props} variant="workbench" />);
    expect(await findExport()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
    await act(async () => saving.resolve({ kind: "review-cue", project_id: "source-video", selected_id: "main", base_revision: "r1",
      cue: { ...cue, review_status: "checked" }, manual_review: { ...source.manual_review, revision: "r2" }, source_export_supported: true }));
    expect(exportButton()).toBeEnabled();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
  });

  it("blocks video export while a subtitle version save is pending", async () => {
    const saving = deferred();
    const request = vi.fn(path => path === "/api/save-subtitles" ? saving.promise : Promise.resolve(source));
    const { props, user } = setup(source, { api: { request }, snapshot: { ...snapshot, file_layout: {} } });
    await findExport();
    await user.click(screen.getByRole("button", { name: "保存字幕版本" }));
    expect(exportButton()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
    await act(async () => saving.resolve({ folder: "导出/字幕版本", files: [{ name: "原文.srt" }], saved_at: "2026-10-23T11:00:00+08:00", review_status: "draft" }));
    expect(exportButton()).toBeEnabled();
  });

  it("keeps export disabled after a save conflict until the conflict is refreshed", async () => {
    const request = vi.fn(async path => { if (path === "/api/review-cue") throw new Error("409 revision conflict"); return source; });
    const { props, user } = setup(source, { api: { request } });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    await screen.findByText("409 revision conflict");
    expect(exportButton()).toBeDisabled();
    await user.click(exportButton());
    expect(props.run).not.toHaveBeenCalled();
  });

  it.each(["review", "workbench"])("uses the saved revision capability and invalidates the previous video in %s", async variant => {
    const saving = deferred();
    let reads = 0;
    const request = vi.fn(path => { if (path === "/api/review-cue") return saving.promise; reads += 1; return Promise.resolve({ ...source, draft_video: oldVideo }); });
    const { props, user } = setup(source, { variant, api: { request } });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    expect(exportButton()).toBeDisabled();
    const savedCue = { ...cue, review_status: "checked" };
    const manual_review = { ...source.manual_review, revision: "r2" };
    await act(async () => saving.resolve({ kind: "review-cue", project_id: "source-video", selected_id: "main", base_revision: "r1", cue: savedCue, manual_review, source_export_supported: true }));
    expect(screen.queryByText(oldVideo.name)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "播放草稿" })).not.toBeInTheDocument();
    expect(exportButton()).toBeEnabled();
    expect(reads).toBe(1);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(props.run).not.toHaveBeenCalled();
    await user.click(exportButton());
    expect(props.run).toHaveBeenCalledExactlyOnceWith("export-draft");
  });

  it.each([undefined, false])("withdraws source export on an unsupported saved revision until explicit refresh: %s", async capability => {
    let reads = 0;
    const manual_review = { ...source.manual_review, revision: "r2" };
    const request = vi.fn(async path => {
      if (path === "/api/review-cue") return { kind: "review-cue", project_id: "source-video", selected_id: "main", base_revision: "r1", cue: { ...cue, review_status: "checked" }, manual_review, source_export_supported: capability };
      reads += 1;
      return { ...source, manual_review: reads > 1 ? manual_review : source.manual_review };
    });
    const { props, user } = setup(source, { api: { request } });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    await screen.findByText("本句修改与核对状态已保存。");
    expect(screen.queryByRole("button", { name: "直接导出草稿 MP4" })).not.toBeInTheDocument();
    expect(reads).toBe(1);
    expect(props.run).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    expect(exportButton()).toBeEnabled();
  });

  it("retains the bilingual draft action without requiring the source capability", async () => {
    const { props, user } = setup({ ...source, source_export_supported: undefined, manual_review: { ...source.manual_review, mode: undefined } });
    expect(await findExport()).toBeEnabled();
    await user.click(exportButton());
    expect(props.run).toHaveBeenCalledExactlyOnceWith("export-draft");
  });
});
