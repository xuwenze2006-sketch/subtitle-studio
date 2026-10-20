import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import Preview from "./Preview";
import { readReviewDraft } from "./reviewDraftStorage";

const cue = { id: 1, start_ms: 1000, end_ms: 2000, source_text: "First", target_text: "第一句", review_status: "unchecked", note: "", warnings: [] };
const base = { project_id: "advance-project", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  source_language: "en", target_language: "zh", media_available: false, downloads: [],
  cues: [cue, { ...cue, id: 2, start_ms: 3000, end_ms: 4000, source_text: "Already checked", review_status: "checked" },
    { ...cue, id: 3, start_ms: 5000, end_ms: 6000, source_text: "Next unchecked", target_text: "下一条" }],
  manual_review: { supported: true, revision: "r1", summary: { total: 3, checked: 1, can_accept: false } } };
const projectKey = '["film.mp4","campaign","advance-project"]';
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
function setup(variant = "review", data = base) {
  const pending = deferred();
  let server = data;
  const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(server));
  const props = { api: { request }, snapshot: { project_id: data.project_id, source: "film.mp4", campaign: "campaign", job: { busy: false } },
    connection: "connected", onError: vi.fn(), onReviewChanged: vi.fn(), open: vi.fn(), sessionMemory: { current: null } };
  const view = render(<Preview {...props} variant={variant} />);
  const commit = (changes = {}) => {
    const savedCue = { ...cue, review_status: "checked", ...changes };
    const manual_review = { ...data.manual_review, revision: "r2", summary: { ...data.manual_review.summary, checked: data.manual_review.summary.checked + 1 } };
    server = { ...data, cues: data.cues.map(item => item.id === cue.id ? savedCue : item), manual_review };
    return { kind: "review-cue", project_id: data.project_id, selected_id: "main", base_revision: "r1", cue: savedCue, manual_review };
  };
  const acknowledge = async (changes = {}) => {
    await act(async () => pending.resolve(commit(changes)));
  };
  return { view, props, pending, request, commit, acknowledge };
}
async function selectFirst() {
  fireEvent.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
}
async function submitNext() {
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "核对并下一条" })));
}
function expectOneWrite(request) {
  const writes = request.mock.calls.filter(([path]) => path === "/api/review-cue");
  expect(writes).toHaveLength(1);
  expect(writes[0][1]).toMatchObject({ sample: "main", cue_id: 1, expected_revision: "r1", review_status: "checked", response_mode: "cue" });
}
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("save-and-next intent across preview layouts", () => {
  it.each([
    ["review", "workbench", false, true], ["workbench", "review", false, false],
    ["review", "workbench", true, true], ["workbench", "review", true, true],
  ])("continues %s → %s after a checked-only write (unmounted ack: %s; committed read: %s)", async (from, to, unmountedAck, committedRead) => {
    const { view, props, request, commit, acknowledge } = setup(from);
    await selectFirst();
    await submitNext();
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("First");
    if (committedRead) commit();
    view.unmount();
    if (unmountedAck) await acknowledge();
    render(<Preview {...props} variant={to} />);
    await screen.findByLabelText("原文（英语）");
    if (!unmountedAck) {
      expect(screen.getByLabelText("原文（英语）")).toHaveValue("First");
      expect(screen.getByRole("button", { name: "核对并下一条" })).toBeDisabled();
      await acknowledge();
    }
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Next unchecked");
    expect(screen.getByLabelText("核对状态筛选")).toHaveValue("unchecked");
    expect(props.sessionMemory.current.reviewDraft).toMatchObject({ cueId: 3, sample: "main", revision: "r2" });
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    expect(props.onReviewChanged).toHaveBeenCalledTimes(1);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expectOneWrite(request);
  });

  it("retains the final-cue completion result when the acknowledgement arrives while unmounted", async () => {
    const data = { ...base, cues: [cue], manual_review: { ...base.manual_review, summary: { total: 1, checked: 0, can_accept: false } } };
    const { view, props, request, acknowledge } = setup("workbench", data);
    await selectFirst();
    await submitNext();
    view.unmount();
    await acknowledge();
    render(<Preview {...props} />);
    await screen.findByLabelText("原文（英语）");
    expect(screen.getByText("本句已保存，当前片段没有未检查字幕。")).toBeInTheDocument();
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("First");
    expect(screen.getByLabelText("核对状态筛选")).toHaveValue("all");
    expect(props.sessionMemory.current.reviewDraft).toMatchObject({ cueId: 1, revision: "r2" });
    expectOneWrite(request);
  });

  it("keeps newer restored input on the submitted cue instead of advancing", async () => {
    const { view, props, request, acknowledge } = setup();
    await selectFirst();
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "提交内容" } });
    await submitNext();
    view.unmount();
    const draft = props.sessionMemory.current.reviewDraft;
    props.sessionMemory.current.reviewDraft = { ...draft, fields: { ...draft.fields, target_text: "提交之后新增输入" } };
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("译文（中文）");
    await acknowledge({ target_text: "提交内容" });
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("提交之后新增输入");
    expect(screen.getByLabelText("核对状态筛选")).toHaveValue("all");
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    expect(props.sessionMemory.current.reviewDraft).toMatchObject({ cueId: 1, revision: "r2", initial: { target_text: "提交内容" } });
    expect(readReviewDraft(projectKey, "main").draft).toMatchObject({ revision: "r2", fields: { target_text: "提交之后新增输入" } });
    expectOneWrite(request);
  });

  it("does not turn an ordinary checked save into save-and-next after remounting", async () => {
    const { view, props, request, acknowledge } = setup();
    await selectFirst();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "已核对本句" })));
    view.unmount();
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("原文（英语）");
    await acknowledge();
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("First");
    expect(screen.getByLabelText("核对状态筛选")).toHaveValue("all");
    expect(screen.getByText("本句修改与核对状态已保存。")).toBeInTheDocument();
    expectOneWrite(request);
  });

  it("keeps the edited cue after an unmounted failure without advancing or retrying", async () => {
    const { view, props, pending, request } = setup();
    await selectFirst();
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "失败时保留" } });
    await submitNext();
    view.unmount();
    await act(async () => pending.reject(new Error("磁盘写入失败")));
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("磁盘写入失败");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("失败时保留");
    expect(screen.getByLabelText("核对状态筛选")).toHaveValue("all");
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    expect(props.sessionMemory.current.reviewDraft).toMatchObject({ cueId: 1, revision: "r1" });
    expect(props.onReviewChanged).not.toHaveBeenCalled();
    expectOneWrite(request);
  });
});
