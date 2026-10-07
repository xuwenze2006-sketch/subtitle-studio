import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";
import { createReviewDraft } from "./ManualReview";
import { readReviewDraft, writeReviewDraft, REVIEW_DRAFT_STORAGE_KEY } from "./reviewDraftStorage";

const cue = { id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "原译文", review_status: "unchecked", note: "", warnings: [] };
const base = { project_id: "draft-project", selected_id: "main", selections: [{ id: "main", name: "整片" }, { id: "sample-1", name: "样片" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue], downloads: [],
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, can_accept: false } } };
const saved = { ...base, cues: [{ ...cue, target_text: "提交内容" }], manual_review: { ...base.manual_review, revision: "r2" } };
const projectKey = '["film.mp4","campaign","draft-project"]';
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
function seed(sample = "main", changes = {}, key = projectKey) {
  const draft = createReviewDraft(cue, "r1", sample);
  return writeReviewDraft(key, { ...draft, fields: { ...draft.fields, target_text: "待恢复的译文", note: "听不清末尾", ...changes } });
}
function setup(request = vi.fn(async () => base), extra = {}) {
  const props = { api: { request }, snapshot: { project_id: "draft-project", source: "film.mp4", campaign: "campaign", job: { busy: false } },
    connection: "connected", onError: vi.fn(), open: vi.fn(), onReviewChanged: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, request, user: userEvent.setup() };
}
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("persisted review drafts in the preview", () => {
  it("writes the last edit immediately and restores it in a fresh editor session without submitting", async () => {
    const { user, props, request, unmount } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "刷新前最后一次输入" } });
    expect(readReviewDraft(projectKey, "main").draft?.fields.target_text).toBe("刷新前最后一次输入");
    unmount();
    render(<Preview {...props} sessionMemory={{ current: null }} />);
    expect(await screen.findByLabelText("译文（中文）")).toHaveValue("刷新前最后一次输入");
    expect(screen.getByText(/已恢复本机未保存草稿/)).toBeInTheDocument();
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    const exit = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(exit);
    expect(exit.defaultPrevented).toBe(true);
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(0);
  });

  it("persists a manual return to the original fields instead of restoring an older intermediate edit", async () => {
    const { user, props, unmount } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "中间一次输入" } });
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "原译文" } });
    unmount();
    render(<Preview {...props} sessionMemory={{ current: null }} />);
    expect(await screen.findByLabelText("译文（中文）")).toHaveValue("原译文");
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
  });

  it("retains a recovered revision conflict until the user explicitly refreshes the saved state", async () => {
    seed();
    const latest = { ...saved, cues: [{ ...saved.cues[0], target_text: "另一个窗口已保存" }] };
    const { user, props, request } = setup(vi.fn(async () => latest));
    expect(await screen.findByLabelText("译文（中文）")).toHaveValue("待恢复的译文");
    expect(screen.getByLabelText("核对备注")).toHaveValue("听不清末尾");
    expect(screen.getByText(/草稿.*版本.*变化/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(props.sessionMemory.current.reviewDraft.revision).toBe("r1");
    expect(readReviewDraft(projectKey, "main").draft.revision).toBe("r1");
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("待恢复的译文");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    expect(readReviewDraft(projectKey, "main").draft.revision).toBe("r2");
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(0);
  });

  it("keeps an orphaned cue's text visible and recoverable until explicit discard", async () => {
    const draft = createReviewDraft({ ...cue, id: 99 }, "r1", "main");
    writeReviewDraft(projectKey, { ...draft, fields: { ...draft.fields, target_text: "原句消失后的草稿", note: "需要保留" } });
    const { user, request } = setup();
    expect(await screen.findByLabelText("草稿译文")).toHaveValue("原句消失后的草稿");
    expect(screen.getByLabelText("草稿核对备注")).toHaveValue("需要保留");
    expect(screen.getByText(/草稿.*原句.*不存在/)).toBeInTheDocument();
    expect(readReviewDraft(projectKey, "main").draft.cueId).toBe(99);
    await user.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    expect(readReviewDraft(projectKey, "main").draft).toBeNull();
    expect(screen.queryByLabelText("草稿译文")).not.toBeInTheDocument();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(0);
  });

  it("restores the selected segment's draft and isolates another project's matching cue ID", async () => {
    seed("sample-1", { target_text: "样片待恢复" });
    seed("main", { target_text: "其他项目私有草稿" }, '["film.mp4","campaign","other-project"]');
    const request = vi.fn(async path => path.includes("sample=sample-1") ? { ...base, selected_id: "sample-1" } : base);
    const { user } = setup(request);
    await screen.findByRole("button", { name: "编辑第 1 句" });
    expect(screen.queryByLabelText("译文（中文）")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("预览片段"), "sample-1");
    expect(await screen.findByLabelText("译文（中文）")).toHaveValue("样片待恢复");
    expect(screen.queryByText("其他项目私有草稿")).not.toBeInTheDocument();
    expect(readReviewDraft('["film.mp4","campaign","other-project"]', "main").draft.fields.target_text).toBe("其他项目私有草稿");
  });

  it.each(["save", "discard"])("clears the persisted draft only after a confirmed %s", async action => {
    const request = vi.fn(async path => path === "/api/review-cue" ? saved : base);
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "提交内容" } });
    expect(readReviewDraft(projectKey, "main").draft?.fields.target_text).toBe("提交内容");
    await user.click(screen.getByRole("button", { name: action === "save" ? "保存修改" : "放弃未保存修改" }));
    await waitFor(() => expect(readReviewDraft(projectKey, "main").draft).toBeNull());
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
  });

  it("retains newer input after a late receipt and persists its updated base revision", async () => {
    const pending = deferred();
    let server = base;
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(server));
    const { user, props, unmount } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "提交内容" } });
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    unmount();
    const draft = props.sessionMemory.current.reviewDraft;
    props.sessionMemory.current.reviewDraft = { ...draft, fields: { ...draft.fields, target_text: "提交之后新增输入" } };
    const remounted = render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("译文（中文）");
    await act(async () => { server = saved; pending.resolve(saved); });
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("提交之后新增输入");
    const persisted = readReviewDraft(projectKey, "main").draft;
    expect(persisted?.revision).toBe("r2");
    expect(persisted?.initial.target_text).toBe("提交内容");
    expect(persisted?.fields.target_text).toBe("提交之后新增输入");
    remounted.unmount();
    render(<Preview {...props} sessionMemory={{ current: null }} />);
    expect(await screen.findByLabelText("译文（中文）")).toHaveValue("提交之后新增输入");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
  });

  it("does not clear another window's newer persisted draft when the active submission is acknowledged", async () => {
    const pending = deferred();
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(base));
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "提交内容" } });
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    seed("main", { target_text: "另一窗口后来的输入" });
    await act(async () => pending.resolve(saved));
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    expect(readReviewDraft(projectKey, "main").draft?.fields.target_text).toBe("另一窗口后来的输入");
  });

  it("reports damaged browser data while allowing the next edit to repair recovery", async () => {
    localStorage.setItem(REVIEW_DRAFT_STORAGE_KEY, "{damaged");
    const { user } = setup();
    expect(await screen.findByText(/本机草稿.*读取失败/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "新的校对内容" } });
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    expect(readReviewDraft(projectKey, "main").draft.fields.target_text).toBe("新的校对内容");
  });

  it("shows unavailable storage without blocking editing or a confirmed server save", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Full", "QuotaExceededError"); });
    const request = vi.fn(async path => path === "/api/review-cue" ? saved : base);
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "提交内容" } });
    expect(screen.getByText(/本机草稿.*无法保存/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByText("本句修改与核对状态已保存。")).toBeInTheDocument();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
  });
});
