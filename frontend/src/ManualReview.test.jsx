import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";

const cue = { id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "你好", review_status: "unchecked", note: "", warnings: [] };
const base = { project_id: "review-project", selected_id: "main", selections: [{ id: "main", name: "整片" }, { id: "sample-1", name: "样片 1" }],
  source_language: "en", target_language: "zh", media_available: true, media_url: "/api/media", cues: [cue, { ...cue, id: 2, start_ms: 3000, end_ms: 4500, source_text: "World", target_text: "世界" }],
  manual_review: { supported: true, revision: "r1", summary: { total: 2, checked: 0, issues: 0, pending_translation: 0, required_checks: 2, can_accept: false } } };
const updated = (changes = {}, summary = {}) => ({ ...base, cues: [{ ...cue, ...changes }, base.cues[1]], manual_review: { ...base.manual_review, revision: "r2", summary: { ...base.manual_review.summary, ...summary } } });
function setup(request = vi.fn(async () => base), extra = {}) {
  const props = { api: { request }, snapshot: { project_id: "review-project", source: "film.mp4", job: { busy: false } }, connection: "connected", onError: vi.fn(), open: vi.fn(), onReviewChanged: vi.fn(), ...extra };
  return { ...render(<Preview {...props} />), props, request, user: userEvent.setup() };
}
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

describe("人工字幕核对", () => {
  it("saves corrected cues with CAS and preserves player, search, speed and follow state", async () => {
    const request = vi.fn(async (path) => path === "/api/review-cue" ? updated({ target_text: "您好" }) : base);
    const { user, props } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    const media = screen.getByLabelText("源素材预览");
    media.currentTime = 1.234;
    fireEvent.timeUpdate(media);
    await user.selectOptions(screen.getByLabelText("播放速度"), "1.25");
    await user.click(screen.getByRole("button", { name: "跟随播放" }));
    await user.type(screen.getByLabelText("搜索字幕"), "Hello");
    await user.clear(screen.getByLabelText("译文（中文）"));
    await user.type(screen.getByLabelText("译文（中文）"), "您好");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await within(screen.getByLabelText("字幕时间轴")).findByText("您好")).toBeInTheDocument();
    expect(screen.getByLabelText("源素材预览")).toBe(media);
    expect(media.currentTime).toBe(1.234);
    expect(media.playbackRate).toBe(1.25);
    expect(screen.getByLabelText("搜索字幕")).toHaveValue("Hello");
    expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
    expect(request).toHaveBeenCalledWith("/api/review-cue", expect.objectContaining({ project_id: "review-project", sample: "main", expected_revision: "r1", cue_id: 1, target_text: "您好", review_status: "unchecked" }));
    expect(props.onReviewChanged).toHaveBeenCalled();
  });

  it("retains conflicting input until an explicit refresh and does not retry writes", async () => {
    const request = vi.fn(async (path) => { if (path === "/api/review-cue") throw new Error("版本冲突，请刷新"); return base; });
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.type(screen.getByLabelText("译文（中文）"), "！");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("版本冲突");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("你好！");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("你好！");
    expect(await screen.findByRole("button", { name: "保存修改" })).toBeEnabled();
  });

  it("requires explicit translation confirmation after a source edit before marking checked", async () => {
    const request = vi.fn(async (path) => path === "/api/review-cue" ? updated({ source_text: "Hello again", review_status: "checked" }, { checked: 1 }) : base);
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.type(screen.getByLabelText("原文（英语）"), " again");
    expect(screen.getByRole("button", { name: "已核对本句" })).toBeDisabled();
    expect(screen.getByText(/译文需要重新核对/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("已核对修改后的译文"));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    expect(await screen.findByText("已检查 1 / 2")).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("/api/review-cue", expect.objectContaining({ source_text: "Hello again", review_status: "checked", translation_confirmed: true }));
  });

  it("saves an unconfirmed source correction as unchecked and exposes the stale translation", async () => {
    const request = vi.fn(async path => path === "/api/review-cue" ? updated({ source_text: "Corrected", translation_stale: true }) : updated({ review_status: "checked" }, { checked: 1 }));
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.clear(screen.getByLabelText("原文（英语）"));
    await user.type(screen.getByLabelText("原文（英语）"), "Corrected");
    expect(screen.getByLabelText("本句核对状态")).toHaveValue("unchecked");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(await screen.findByLabelText("已核对修改后的译文")).not.toBeChecked();
    expect(request).toHaveBeenCalledWith("/api/review-cue", expect.objectContaining({ review_status: "unchecked", translation_confirmed: false }));
  });

  it("preserves issue notes, displays automatic warnings, and never sends an issue without a note", async () => {
    const request = vi.fn(async path => path === "/api/review-cue" ? updated({ review_status: "issue", note: "音节听不清" }, { issues: 1 }) : updated({ warnings: ["字幕时长过短"] }));
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    expect(screen.getByText("字幕时长过短")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "标记疑点" }));
    expect(screen.getByRole("alert")).toHaveTextContent("核对备注");
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(0);
    await user.type(screen.getByLabelText("核对备注"), "音节听不清");
    await user.click(screen.getByRole("button", { name: "标记疑点" }));
    expect(await screen.findByText("疑点 1")).toBeInTheDocument();
    expect(screen.getByLabelText("核对备注")).toHaveValue("音节听不清");
  });

  it.each([["译文（中文）", "新译文"], ["开始时间", "0.800"], ["结束时间", "2.800"]])("requires checking again after editing %s", async (label, value) => {
    const { user } = setup(vi.fn(async () => updated({ review_status: "checked" }, { checked: 1 })));
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    expect(screen.getByLabelText("本句核对状态")).toHaveValue("unchecked");
  });

  it("filters automatic warning clues without treating them as manual issues", async () => {
    setup(vi.fn(async () => updated({ warnings: ["字幕过短"] })));
    await screen.findByRole("button", { name: "编辑第 1 句" });
    fireEvent.change(screen.getByLabelText("核对状态筛选"), { target: { value: "warnings" } });
    expect(screen.getByRole("button", { name: "编辑第 1 句" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑第 2 句" })).not.toBeInTheDocument();
    expect(screen.getByText("疑点 0")).toBeInTheDocument();
  });

  it("blocks accidental cue and sample switches until dirty text is explicitly discarded", async () => {
    const { user } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.type(screen.getByLabelText("译文（中文）"), "！");
    await user.click(screen.getByRole("button", { name: "编辑第 2 句" }));
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Hello");
    await user.selectOptions(screen.getByLabelText("预览片段"), "sample-1");
    expect(screen.getByLabelText("预览片段")).toHaveValue("main");
    await user.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    await user.click(screen.getByRole("button", { name: "编辑第 2 句" }));
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("World");
  });

  it("validates time ranges and disables all writes while a background job is busy", async () => {
    const { user, request, props, rerender } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.clear(screen.getByLabelText("结束时间"));
    await user.type(screen.getByLabelText("结束时间"), "0.5");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(screen.getByRole("alert")).toHaveTextContent("结束时间必须晚于开始时间");
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(0);
    rerender(<Preview {...props} snapshot={{ ...props.snapshot, job: { busy: true } }} />);
    expect(await screen.findByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "已核对本句" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "标记疑点" })).toBeDisabled();
  });

  it("restores persisted checked states and filters without renumbering", async () => {
    setup(vi.fn(async () => updated({ review_status: "checked" }, { checked: 1 })));
    expect(await screen.findByText("已检查 1 / 2")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("核对状态筛选"), { target: { value: "unchecked" } });
    expect(within(screen.getByLabelText("字幕时间轴")).getByText("02")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑第 1 句" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一条未检查" }));
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("World");
  });

  it("keeps unsaved drafts when navigating between workspace and review views", async () => {
    const sessionMemory = { current: null };
    const { user, props, unmount } = setup(undefined, { sessionMemory });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.type(screen.getByLabelText("译文（中文）"), "！");
    unmount();
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByLabelText("译文（中文）")).toHaveValue("你好！");
  });

  it("warns before browser unload only while the active draft is dirty", async () => {
    const { user, unmount } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "未保存" } });
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
    await user.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    const discarded = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(discarded);
    expect(discarded.defaultPrevented).toBe(false);
    unmount();
    const afterUnmount = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(afterUnmount);
    expect(afterUnmount.defaultPrevented).toBe(false);
  });

  it("ignores an old sample save after switching projects", async () => {
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const request = vi.fn((path) => path === "/api/review-cue" ? pending : Promise.resolve(base));
    const { user, props, rerender } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    rerender(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "other-project" }} />);
    await screen.findByRole("button", { name: "编辑第 1 句" });
    await act(async () => finish(updated({ target_text: "旧项目写回" })));
    expect(screen.queryByText("旧项目写回")).not.toBeInTheDocument();
    expect(props.onReviewChanged).not.toHaveBeenCalled();
  });

  it("ignores a late mark-checked response after switching samples", async () => {
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const other = { ...base, selected_id: "sample-1", cues: [{ ...cue, id: 9, source_text: "Sample", target_text: "样片内容" }] };
    const request = vi.fn(path => path === "/api/review-cue" ? pending : Promise.resolve(path.includes("sample-1") ? other : base));
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    await user.selectOptions(screen.getByLabelText("预览片段"), "sample-1");
    await screen.findByText("样片内容");
    await act(async () => finish(updated({ target_text: "旧片段响应" })));
    expect(screen.queryByText("旧片段响应")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "完成整片验收" })).not.toBeInTheDocument();
  });

  it("requires explicit acceptance and the server's actual checked count", async () => {
    const accepted = { ...base, manual_review: { ...base.manual_review, summary: { ...base.manual_review.summary, checked: 2, can_accept: true } } };
    const request = vi.fn(async path => path === "/api/review-accept" ? { accepted: true, reviewed_cues: 2 } : accepted);
    const { user } = setup(request);
    const button = await screen.findByRole("button", { name: "完成整片验收" });
    expect(button).toBeDisabled();
    await user.click(screen.getByLabelText("我已听看抽检并确认疑点已处理"));
    await user.click(button);
    expect(await screen.findByText(/整片验收已记录/)).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("/api/review-accept", { project_id: "review-project", sample: "main", expected_revision: "r1", content_passed: true });
  });

  it("does not accept insufficient checked cues even after the user confirms", async () => {
    const { user, request } = setup();
    await user.click(await screen.findByLabelText("我已听看抽检并确认疑点已处理"));
    expect(screen.getByRole("button", { name: "完成整片验收" })).toBeDisabled();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-accept")).toHaveLength(0);
  });

  it("leaves older servers in playback-only mode", async () => {
    setup(vi.fn(async () => ({ ...base, manual_review: undefined })));
    expect(await screen.findByRole("button", { name: "播放第 1 句" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "编辑第 1 句" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "完成整片验收" })).not.toBeInTheDocument();
  });
});

describe("编辑定位、草稿试听与保存快捷键", () => {
  const long = { ...base, cues: Array.from({ length: 600 }, (_, index) => ({ ...cue,
    id: `long-${index + 1}`, start_ms: index * 2000 + 1000, end_ms: index * 2000 + 2500,
    source_text: `Sentence${index + 1}`, target_text: `译文${index + 1}` })) };

  it("pauses and locates the newly edited cue, clearing the previous completed replay target", async () => {
    const { user } = setup();
    const media = await screen.findByLabelText("源素材预览");
    await user.click(screen.getByRole("button", { name: "播放第 1 句" }));
    media.currentTime = 2.6;
    fireEvent.timeUpdate(media);
    fireEvent.pause(media);
    const plays = HTMLMediaElement.prototype.play.mock.calls.length;
    const pauses = HTMLMediaElement.prototype.pause.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "编辑第 2 句" }));
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("World");
    expect(media.currentTime).toBe(3);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses + 1);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(plays);
    fireEvent.keyDown(screen.getByRole("region", { name: "字幕预览工作区" }), { key: "R" });
    expect(media.currentTime).toBe(3);
  });

  it.each(["dirty", "pending"])("does not move or pause the player when %s review protects another edit", async mode => {
    const request = vi.fn(path => path === "/api/review-cue" ? new Promise(() => {}) : Promise.resolve(base));
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    if (mode === "dirty") fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "保留草稿" } });
    else await user.click(screen.getByRole("button", { name: "已核对本句" }));
    const media = screen.getByLabelText("源素材预览");
    media.currentTime = 1.75;
    fireEvent.timeUpdate(media);
    const pauses = HTMLMediaElement.prototype.pause.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "编辑第 2 句" }));
    expect(media.currentTime).toBe(1.75);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses);
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Hello");
    expect(screen.getByRole("status").textContent).toMatch(/保存|等待/);
  });

  it("reveals the edited far-away cue when clearing search without seeking or discarding its draft", async () => {
    const { user, request } = setup(vi.fn(async () => long));
    await screen.findByLabelText("源素材预览");
    await user.click(screen.getByRole("button", { name: "跟随播放" }));
    fireEvent.change(screen.getByLabelText("搜索字幕"), { target: { value: "Sentence500" } });
    await user.click(screen.getByRole("button", { name: "编辑第 500 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "远处未保存的草稿" } });
    const media = screen.getByLabelText("源素材预览");
    media.currentTime = 1.25;
    fireEvent.timeUpdate(media);
    await user.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(screen.getByRole("button", { name: "编辑第 500 句" })).toBeInTheDocument();
    expect(screen.getByLabelText("字幕时间轴").scrollTop).toBeGreaterThan(0);
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("远处未保存的草稿");
    expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
    expect(media.currentTime).toBe(1.25);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("auditions the edited time range and stops at its end without saving or changing draft fields", async () => {
    const { user, request } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.selectOptions(screen.getByLabelText("播放速度"), "1.25");
    fireEvent.change(screen.getByLabelText("搜索字幕"), { target: { value: "Hello" } });
    for (const [label, value] of [["开始时间", "0.700"], ["结束时间", "00:00:01.900"],
      ["原文（英语）", "Edited source"], ["译文（中文）", "试听中的译文"], ["核对备注", "要听清这句"]])
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    await user.selectOptions(screen.getByLabelText("本句核对状态"), "issue");
    await user.click(screen.getByRole("button", { name: "试听本句" }));
    const media = screen.getByLabelText("源素材预览");
    expect(media.currentTime).toBe(0.7);
    expect(media.playbackRate).toBe(1.25);
    const pauses = HTMLMediaElement.prototype.pause.mock.calls.length;
    media.currentTime = 2;
    fireEvent.timeUpdate(media);
    expect(media.currentTime).toBe(1.9);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses + 1);
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Edited source");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("试听中的译文");
    expect(screen.getByLabelText("开始时间")).toHaveValue("0.700");
    expect(screen.getByLabelText("结束时间")).toHaveValue("00:00:01.900");
    expect(screen.getByLabelText("本句核对状态")).toHaveValue("issue");
    expect(screen.getByLabelText("搜索字幕")).toHaveValue("Hello");
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("locates a copied audition cue by stable ID even when its edited range is elsewhere", async () => {
    const { user } = setup(vi.fn(async () => long));
    await screen.findByLabelText("源素材预览");
    await user.click(screen.getByRole("button", { name: "跟随播放" }));
    fireEvent.change(screen.getByLabelText("搜索字幕"), { target: { value: "Sentence500" } });
    await user.click(screen.getByRole("button", { name: "编辑第 500 句" }));
    await user.click(screen.getByRole("button", { name: "清空搜索" }));
    fireEvent.scroll(screen.getByLabelText("字幕时间轴"), { target: { scrollTop: 0 } });
    fireEvent.change(screen.getByLabelText("开始时间"), { target: { value: "0.400" } });
    fireEvent.change(screen.getByLabelText("结束时间"), { target: { value: "0.800" } });
    await user.click(screen.getByRole("button", { name: "试听本句" }));
    expect(screen.getByLabelText("源素材预览").currentTime).toBe(0.4);
    expect(screen.getByRole("button", { name: "编辑第 500 句" })).toBeInTheDocument();
  });

  it.each([["bad", "2", /有效时间/], ["-1", "2", /有效时间/],
    ["2", "2", /结束时间必须晚于/], ["3", "2", /结束时间必须晚于/]])(
    "rejects audition range %s–%s locally", async (start, end, message) => {
      const { user, request } = setup();
      await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
      fireEvent.change(screen.getByLabelText("开始时间"), { target: { value: start } });
      fireEvent.change(screen.getByLabelText("结束时间"), { target: { value: end } });
      const media = screen.getByLabelText("源素材预览");
      const before = media.currentTime;
      await user.click(screen.getByRole("button", { name: "试听本句" }));
      expect(screen.getByRole("alert").textContent).toMatch(message);
      expect(media.currentTime).toBe(before);
      expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
      expect(request).toHaveBeenCalledTimes(1);
    });

  it.each(["unavailable", "failed", "pending", "disabled"])("disables audition when media/review is %s", async mode => {
    const data = mode === "unavailable" ? { ...base, media_available: false } : base;
    const request = vi.fn(path => path === "/api/review-cue" ? new Promise(() => {}) : Promise.resolve(data));
    const { user, props, rerender } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    if (mode === "failed") fireEvent.error(screen.getByLabelText("源素材预览"));
    if (mode === "pending") await user.click(screen.getByRole("button", { name: "已核对本句" }));
    if (mode === "disabled") rerender(<Preview {...props} disabled />);
    expect(screen.getByRole("button", { name: "试听本句" })).toBeDisabled();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it.each(["unchecked", "issue"])("Ctrl+S saves the current %s status without checking or advancing", async status => {
    const request = vi.fn(async (path, body) => path === "/api/review-cue"
      ? updated({ target_text: body.target_text, note: body.note, review_status: body.review_status }) : base);
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "快捷保存" } });
    fireEvent.change(screen.getByLabelText("核对备注"), { target: { value: "保留疑点" } });
    await user.selectOptions(screen.getByLabelText("本句核对状态"), status);
    await act(async () => { expect(fireEvent.keyDown(screen.getByLabelText("译文（中文）"), { key: "s", ctrlKey: true })).toBe(false); });
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
    expect(request).toHaveBeenCalledWith("/api/review-cue", expect.objectContaining({ cue_id: 1,
      expected_revision: "r1", target_text: "快捷保存", review_status: status, note: "保留疑点" }));
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Hello");
    expect(screen.getByLabelText("本句核对状态")).toHaveValue(status);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("ignores save shortcut composition, repeated keys and extra modifiers", async () => {
    const { user, request } = setup();
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    const input = screen.getByLabelText("译文（中文）");
    for (const options of [{ ctrlKey: false }, { metaKey: true }, { altKey: true }, { shiftKey: true },
      { repeat: true }, { isComposing: true }, { keyCode: 229 }]) {
      expect(fireEvent.keyDown(input, { key: "s", ctrlKey: true, ...options })).toBe(true);
    }
    expect(request).toHaveBeenCalledTimes(1);
  });

  it.each(["disabled", "pending", "conflict", "background job"])("does not submit Ctrl+S while %s", async mode => {
    const request = vi.fn(path => path !== "/api/review-cue" ? Promise.resolve(base)
      : mode === "conflict" ? Promise.reject(new Error("版本冲突，请刷新")) : new Promise(() => {}));
    const { user, props, rerender } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    if (mode === "disabled") rerender(<Preview {...props} disabled />);
    if (mode === "background job") {
      rerender(<Preview {...props} snapshot={{ ...props.snapshot, job: { busy: true } }} />);
      await screen.findByLabelText("译文（中文）");
    }
    if (mode === "pending" || mode === "conflict") {
      await user.click(screen.getByRole("button", { name: "保存修改" }));
      if (mode === "conflict") await screen.findByRole("alert");
    }
    const submitted = request.mock.calls.filter(([path]) => path === "/api/review-cue").length;
    await act(async () => { fireEvent.keyDown(screen.getByLabelText("人工核对"), { key: "s", ctrlKey: true }); });
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(submitted);
    expect(screen.getByLabelText("原文（英语）")).toHaveValue("Hello");
  });
});
