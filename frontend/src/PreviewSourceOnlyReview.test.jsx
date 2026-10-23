import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";
import { createReviewDraft } from "./ManualReview";
import { readReviewDraft, writeReviewDraft } from "./reviewDraftStorage";

const cue = { id: 1, start_ms: 1000, end_ms: 2000, source_text: "Hello", target_text: "", translation_stale: false, review_status: "unchecked", note: "", warnings: [] };
const source = { project_id: "source-review", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue, { ...cue, id: 2, start_ms: 3000, end_ms: 4000, source_text: "Next" }], downloads: ["原文"],
  manual_review: { supported: true, mode: "source_only", acceptance_supported: false, revision: "r1", summary: { total: 2, checked: 0, issues: 0, pending_translation: 0, can_accept: false } } };
const projectKey = '["film.mp4","campaign","source-review"]';
function setup(request = vi.fn(async () => source), extra = {}) {
  const props = { api: { request }, snapshot: { project_id: "source-review", source: "film.mp4", campaign: "campaign", job: { busy: false }, actions: { "export-draft": true, export: true } },
    connection: "connected", onError: vi.fn(), onReviewChanged: vi.fn(), open: vi.fn(), run: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, request, user: userEvent.setup() };
}
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("source-only review", () => {
  it("checks an original correction with edited time and note without a translation confirmation", async () => {
    const request = vi.fn(async (path, body) => path === "/api/review-cue" ? { ...source, cues: [{ ...cue, ...body }, source.cues[1]], manual_review: { ...source.manual_review, revision: "r2" } } : source);
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "Hello again" } });
    fireEvent.change(screen.getByLabelText("开始时间"), { target: { value: "0.800" } });
    fireEvent.change(screen.getByLabelText("核对备注"), { target: { value: "已听清" } });
    expect(screen.getByRole("button", { name: "已核对本句" })).toBeEnabled();
    await user.selectOptions(screen.getByLabelText("本句核对状态"), "checked");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(request).toHaveBeenCalledWith("/api/review-cue", expect.objectContaining({ source_text: "Hello again", start_ms: 800, end_ms: 2000, note: "已听清", target_text: "", translation_confirmed: false, review_status: "checked", expected_revision: "r1" }));
    expect(screen.queryByText(/译文需要重新核对/)).not.toBeInTheDocument();
  });

  it("keeps source mode and direct checking after compact save-and-next receipts and a layout remount", async () => {
    let current = source;
    const request = vi.fn(async (path, body) => {
      if (path !== "/api/review-cue") return current;
      const savedCue = { ...current.cues.find(item => item.id === body.cue_id), ...body, id: body.cue_id };
      const baseRevision = current.manual_review.revision;
      current = { ...current, cues: current.cues.map(item => item.id === body.cue_id ? savedCue : item), manual_review: { ...current.manual_review, revision: `r${Number(baseRevision.slice(1)) + 1}` } };
      return { kind: "review-cue", project_id: "source-review", selected_id: "main", base_revision: baseRevision, cue: savedCue, manual_review: current.manual_review };
    });
    const { user, props, unmount } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "First corrected" } });
    await user.click(screen.getByRole("button", { name: "核对并下一条" }));
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Next");
    expect(screen.getByText("英语 · 原文字幕")).toBeInTheDocument();
    unmount();
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByLabelText("原文（英语）")).toHaveValue("Next");
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "Second corrected" } });
    await user.click(screen.getByRole("button", { name: "核对并下一条" }));
    const bodies = request.mock.calls.filter(([path]) => path === "/api/review-cue").map(([, body]) => body);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(expect.objectContaining({ expected_revision: "r2", cue_id: 2, review_status: "checked", target_text: "", translation_confirmed: false }));
    expect(screen.queryByLabelText("译文（中文）")).not.toBeInTheDocument();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it.each(["review", "workbench"])("hides translation, acceptance and MP4 actions in the %s layout", async variant => {
    const { user } = setup(undefined, { variant });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    expect(screen.getByText("英语 · 原文字幕")).toBeInTheDocument();
    expect(screen.queryByLabelText("译文（中文）")).not.toBeInTheDocument();
    expect(screen.queryByText(/译文待核对/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "完成整片验收" })).not.toBeInTheDocument();
    expect(screen.queryByText(/整片抽检|正式验收|MP4/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开样片对照" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载 原文 SRT" })).toBeEnabled();
  });

  it.each([
    { target_text: "旧双语草稿译文", translation_confirmed: false },
    { target_text: "", translation_confirmed: true },
  ])("preserves incompatible bilingual drafts in the recovery area and blocks writes even after refresh: %j", async oldFields => {
    const draft = createReviewDraft(cue, "r1", "main");
    writeReviewDraft(projectKey, { ...draft, fields: { ...draft.fields, ...oldFields, note: "保留此稿" } });
    const { user, request } = setup();
    const recovered = await screen.findByLabelText("恢复的草稿内容");
    expect(within(recovered).getByLabelText("草稿译文")).toHaveValue(oldFields.target_text);
    expect(within(recovered).getByLabelText("草稿核对备注")).toHaveValue("保留此稿");
    expect(within(recovered).getByText(/草稿翻译确认/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    expect(readReviewDraft(projectKey, "main").draft.fields).toEqual(expect.objectContaining(oldFields));
    expect(screen.getByLabelText("草稿译文")).toHaveValue(oldFields.target_text);
    fireEvent.keyDown(screen.getByLabelText("人工核对"), { key: "s", ctrlKey: true });
    await user.click(screen.getByRole("button", { name: "编辑第 2 句" }));
    expect(screen.getByLabelText("草稿核对备注")).toHaveValue("保留此稿");
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    expect(screen.queryByLabelText("恢复的草稿内容")).not.toBeInTheDocument();
    expect(readReviewDraft(projectKey, "main").draft).toBeNull();
  });

  it("retains bilingual editing and confirmation when mode is absent even if acceptance is unsupported", async () => {
    const bilingual = { ...source, cues: [{ ...cue, target_text: "你好" }], manual_review: { ...source.manual_review, mode: undefined, acceptance_supported: false } };
    const { user } = setup(vi.fn(async () => bilingual));
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("你好");
    fireEvent.change(screen.getByLabelText("原文（英语）"), { target: { value: "New source" } });
    expect(screen.getByRole("button", { name: "已核对本句" })).toBeDisabled();
    await user.click(screen.getByLabelText("已核对修改后的译文"));
    expect(screen.getByRole("button", { name: "已核对本句" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "完成整片验收" })).not.toBeInTheDocument();
  });
});
