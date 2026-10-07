import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";
import { createApi } from "./api";
import { getReviewSubmissions } from "./reviewSubmissions";

const cue = { id: 1, start_ms: 1000, end_ms: 2500, source_text: "Hello", target_text: "原始译文", review_status: "unchecked", note: "", warnings: [] };
const base = { project_id: "review-project", selected_id: "main", selections: [{ id: "main", name: "整片" }, { id: "sample-1", name: "样片" }],
  source_language: "en", target_language: "zh", media_available: false, cues: [cue], downloads: ["译文.srt"],
  manual_review: { supported: true, revision: "r1", summary: { total: 1, checked: 0, can_accept: false } } };
const updated = { ...base, cues: [{ ...cue, target_text: "已提交的新译文" }], manual_review: { ...base.manual_review, revision: "r2" } };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
function setup(request, extra = {}) {
  const props = { api: { request, download: vi.fn() }, snapshot: { project_id: "review-project", source: "film.mp4", job: { busy: false }, file_layout: {} },
    connection: "connected", onError: vi.fn(), open: vi.fn(), onReviewChanged: vi.fn(), sessionMemory: { current: null }, ...extra };
  return { ...render(<Preview {...props} />), props, user: userEvent.setup() };
}
async function editAndSave(user) {
  await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
  fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "已提交的新译文" } });
  await user.click(screen.getByRole("button", { name: "保存修改" }));
}
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("连续人工核对", () => {
  const sequence = { ...base, cues: [cue,
    {...cue, id:2, start_ms:3000, end_ms:4000, source_text:'Already', review_status:'checked'},
    {...cue, id:3, start_ms:5000, end_ms:6000, source_text:'Next', target_text:'下一条'}] };
  const acknowledged = {...sequence, cues:[{...cue, review_status:'checked'}, ...sequence.cues.slice(1)],
    manual_review:{...base.manual_review, revision:'r2'}};

  it("advances to the next unchecked cue only after one successful save acknowledgement", async () => {
    const pending = deferred();
    const request = vi.fn(path => path === '/api/review-cue' ? pending.promise : Promise.resolve(sequence));
    const {user, props} = setup(request);
    await user.click(await screen.findByRole('button', {name:'编辑第 1 句'}));
    await user.dblClick(screen.getByRole('button', {name:'核对并下一条'}));
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Hello');
    expect(request.mock.calls.filter(([path])=>path==='/api/review-cue')).toHaveLength(1);
    await act(async()=>pending.resolve(acknowledged));
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Next');
    expect(props.sessionMemory.current.reviewDraft.revision).toBe('r2');
    expect(screen.getByLabelText('核对状态筛选')).toHaveValue('unchecked');
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("does not check the next cue when a double click receives an immediate first acknowledgement", async () => {
    let saved=sequence;
    const request=vi.fn(async (path, body) => {
      if(path === '/api/review-cue') saved={...saved,
        cues:saved.cues.map(item=>item.id===body.cue_id ? {...item,review_status:'checked'} : item),
        manual_review:{...saved.manual_review,revision:`r${body.cue_id+1}`}};
      return saved;
    });
    const {user}=setup(request);
    await user.click(await screen.findByRole('button', {name:'编辑第 1 句'}));
    await user.dblClick(screen.getByRole('button', {name:'核对并下一条'}));
    expect(request.mock.calls.filter(([path])=>path==='/api/review-cue')).toHaveLength(1);
    expect(saved.cues[2].review_status).toBe('unchecked');
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Next');
  });

  it.each(['磁盘写入失败','版本冲突，请刷新'])("keeps the current edited cue after %s without retrying", async message => {
    const request = vi.fn(async path => {if(path === '/api/review-cue') throw new Error(message); return sequence;});
    const {user} = setup(request);
    await user.click(await screen.findByRole('button', {name:'编辑第 1 句'}));
    fireEvent.change(screen.getByLabelText('译文（中文）'), {target:{value:'保留这次修改'}});
    await user.click(screen.getByRole('button', {name:'核对并下一条'}));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.getByLabelText('译文（中文）')).toHaveValue('保留这次修改');
    expect(request.mock.calls.filter(([path])=>path==='/api/review-cue')).toHaveLength(1);
  });

  it("stays on the final cue and explains when no unchecked cues remain", async () => {
    const done = {...base, cues:[{...cue, review_status:'checked'}], manual_review:{...base.manual_review,revision:'r2'}};
    const {user} = setup(vi.fn(async path=>path === '/api/review-cue' ? done : base));
    await user.click(await screen.findByRole('button', {name:'编辑第 1 句'}));
    await user.click(screen.getByRole('button', {name:'核对并下一条'}));
    expect(await screen.findByText('本句已保存，当前片段没有未检查字幕。')).toBeInTheDocument();
    expect(screen.getByLabelText('原文（英语）')).toHaveValue('Hello');
  });

  it("does not move a restored draft changed after submission", async () => {
    const pending=deferred();
    const request=vi.fn(path=>path === '/api/review-cue' ? pending.promise : Promise.resolve(sequence));
    const {user,props,unmount}=setup(request);
    await user.click(await screen.findByRole('button', {name:'编辑第 1 句'}));
    await user.click(screen.getByRole('button', {name:'核对并下一条'}));
    unmount();
    const memory=props.sessionMemory.current;
    memory.reviewDraft={...memory.reviewDraft, fields:{...memory.reviewDraft.fields,target_text:'之后新增的修改'}};
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText('译文（中文）');
    await act(async()=>pending.resolve(acknowledged));
    expect(screen.getByLabelText('译文（中文）')).toHaveValue('之后新增的修改');
    expect(screen.getByText('有未保存修改')).toBeInTheDocument();
  });
});

describe("预览读取超时后的恢复", () => {
  it.each(["initial", "manual"])("releases a hanging %s read so the user can retry without leaving the page", async stage => {
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(() => ++reads === (stage === "initial" ? 1 : 2)
      ? new Promise(() => {}) : Promise.resolve(new Response(JSON.stringify(base)))));
    vi.useFakeTimers();
    try {
      let view;
      await act(async () => { view = setup(createApi().request); });
      if (stage === "manual") {
        await act(async () => {
          fireEvent.click(screen.getByRole("button", { name: "编辑第 1 句" }));
        });
        fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "尚未保存的译文" } });
        await act(async () => { fireEvent.click(screen.getByRole("button", { name: "刷新核对状态" })); });
        expect(screen.getByLabelText("译文（中文）")).toBeDisabled();
      } else expect(screen.getByRole("button", { name: "刷新结果" })).toBeDisabled();

      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(screen.getByRole("button", { name: "刷新结果" })).toBeEnabled();
      expect(reads).toBe(stage === "initial" ? 1 : 2);
      if (stage === "manual") {
        expect(screen.getByLabelText("译文（中文）")).toBeEnabled();
        expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
        expect(screen.getByRole("alert")).toHaveTextContent(/超时/);
      } else expect(view.props.onError).toHaveBeenCalledWith(expect.stringMatching(/超时/));

      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "刷新结果" })); });
      expect(reads).toBe(stage === "initial" ? 2 : 3);
      expect(screen.getByRole("button", { name: "编辑第 1 句" })).toBeEnabled();
      if (stage === "manual") expect(screen.getByLabelText("译文（中文）")).toHaveValue("尚未保存的译文");
    } finally { vi.useRealTimers(); }
  });
});

describe("跨布局的字幕提交回执", () => {
  it("keeps an exit warning for a checked-only write through layout remount until acknowledgement", async () => {
    const pending = deferred();
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(base));
    const { user, props, unmount } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    const pendingExit = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(pendingExit);
    expect(pendingExit.defaultPrevented).toBe(true);
    unmount();
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("译文（中文）");
    const remountedExit = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(remountedExit);
    expect(remountedExit.defaultPrevented).toBe(true);
    await act(async () => pending.resolve({ ...base, cues: [{ ...cue, review_status: "checked" }], manual_review: { ...base.manual_review, revision: "r2" } }));
    const savedExit = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(savedExit);
    expect(savedExit.defaultPrevented).toBe(false);
  });

  it("checks all pending submissions owned by the session, including other projects and samples", async () => {
    const first = deferred(), second = deferred();
    const { props } = setup(vi.fn(async () => base));
    await screen.findByRole("button", { name: "编辑第 1 句" });
    const draft = { cueId: 1, revision: "r1", fields: {} };
    getReviewSubmissions(props.sessionMemory, "other-project").submit("main", draft, () => first.promise);
    getReviewSubmissions(props.sessionMemory, "another-project").submit("sample-2", draft, () => second.promise);
    const warn = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(warn()).toBe(true);
    await act(async () => first.resolve(base));
    expect(warn()).toBe(true);
    await act(async () => second.reject(new Error("写入未确认")));
    expect(warn()).toBe(false);
  });

  it("保存中切换布局仍等待同一提交，收到回执后清除原草稿并使用新版本", async () => {
    const pending = deferred();
    let server = base;
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(server));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    server = updated; // 已落盘，HTTP 保存回执尚未抵达。
    unmount();
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByLabelText("译文（中文）")).toBeDisabled();
    await user.dblClick(screen.getByRole("button", { name: "保存修改" }));
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
    await act(async () => pending.resolve(updated));
    expect(screen.getByLabelText("译文（中文）")).toBeEnabled();
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    expect(screen.getByText("本句修改与核对状态已保存。")).toBeInTheDocument();
    expect(props.sessionMemory.current.reviewDraft.revision).toBe("r2");
    expect(props.onReviewChanged).toHaveBeenCalledTimes(1);
  });

  it("在页面卸载期间收到成功回执，返回时也能确认已保存", async () => {
    const pending = deferred();
    let server = base;
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(server));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    unmount();
    await act(async () => { server = updated; pending.resolve(updated); });
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("译文（中文）");
    expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
    expect(props.sessionMemory.current.reviewDraft.revision).toBe("r2");
  });

  it("跨布局提交失败会保留输入并显示失败，不自动重试", async () => {
    const pending = deferred();
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(base));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    unmount();
    await act(async () => pending.reject(new Error("磁盘写入失败")));
    render(<Preview {...props} variant="workbench" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("磁盘写入失败");
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("已提交的新译文");
    expect(screen.getByRole("button", { name: "保存修改" })).toBeEnabled();
    expect(request.mock.calls.filter(([path]) => path === "/api/review-cue")).toHaveLength(1);
  });

  it("回执只确认提交时的字段，恢复出的后来修改仍是未保存草稿", async () => {
    const pending = deferred();
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(base));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    unmount();
    const memory = props.sessionMemory.current;
    memory.reviewDraft = { ...memory.reviewDraft, fields: { ...memory.reviewDraft.fields, target_text: "后来的修改" } };
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("译文（中文）");
    await act(async () => pending.resolve(updated));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("后来的修改");
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    expect(within(screen.getByLabelText("字幕时间轴")).getByText("已提交的新译文")).toBeInTheDocument();
    expect(props.sessionMemory.current.reviewDraft.revision).toBe("r2");
  });

  it("成功回执比旧读取先抵达时，迟到读取不能把已保存文字换回旧内容", async () => {
    const pending = deferred(), read = deferred();
    let reads = 0;
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : (++reads === 1 ? Promise.resolve(base) : read.promise));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    unmount();
    render(<Preview {...props} variant="workbench" />);
    await act(async () => pending.resolve(updated));
    expect(await within(screen.getByLabelText("字幕时间轴")).findByText("已提交的新译文")).toBeInTheDocument();
    await act(async () => read.resolve(base));
    expect(within(screen.getByLabelText("字幕时间轴")).queryByText("原始译文")).not.toBeInTheDocument();
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("已提交的新译文");
  });

  it.each(["项目", "样片"])("旧回执不能覆盖另一个%s里新输入的草稿", async kind => {
    const pending = deferred();
    const other = { ...base, project_id: kind === "项目" ? "other-project" : base.project_id,
      selected_id: kind === "样片" ? "sample-1" : "main", cues: [{ ...cue, id: 9, target_text: "另一段译文" }] };
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise
      : Promise.resolve(path.includes(kind === "项目" ? "other-project" : "sample-1") ? other : base));
    const { user, props, rerender } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    if (kind === "项目") rerender(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "other-project" }} />);
    else await user.selectOptions(screen.getByLabelText("预览片段"), "sample-1");
    await within(screen.getByLabelText("字幕时间轴")).findByText("另一段译文");
    await user.click(screen.getByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "新的当前草稿" } });
    await act(async () => pending.resolve(updated));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("新的当前草稿");
    expect(props.sessionMemory.current.reviewDraft.cueId).toBe(9);
    expect(props.onReviewChanged).not.toHaveBeenCalled();
    expect(screen.queryByText("本句修改与核对状态已保存。")).not.toBeInTheDocument();
  });

  it("同样片内恢复了另一句草稿时，也不能用旧句回执清除它", async () => {
    const pending = deferred();
    const nextCue = { ...cue, id: 2, source_text: "World", target_text: "第二句" };
    const data = { ...base, cues: [cue, nextCue] };
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(data));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    unmount();
    const current = props.sessionMemory.current.reviewDraft;
    props.sessionMemory.current.reviewDraft = { ...current, cueId: 2, fields: { ...current.fields, source_text: "World", target_text: "第二句的新输入" } };
    render(<Preview {...props} variant="workbench" />);
    await screen.findByLabelText("译文（中文）");
    await act(async () => pending.resolve({ ...updated, cues: [...updated.cues, nextCue] }));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("第二句的新输入");
    expect(props.sessionMemory.current.reviewDraft.cueId).toBe(2);
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
  });

  it("已处理的回执不会在下一次布局切换时重放并覆盖更新的内容", async () => {
    const pending = deferred();
    let server = base;
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(server));
    const { user, props, unmount } = setup(request);
    await editAndSave(user);
    await act(async () => { server = updated; pending.resolve(updated); });
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "下一轮输入" } });
    unmount();
    server = { ...updated, cues: [{ ...updated.cues[0], target_text: "最新磁盘译文" }], manual_review: { ...updated.manual_review, revision: "r3" } };
    render(<Preview {...props} variant="workbench" />);
    expect(await within(screen.getByLabelText("字幕时间轴")).findByText("最新磁盘译文")).toBeInTheDocument();
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("下一轮输入");
    expect(props.onReviewChanged).toHaveBeenCalledTimes(1);
  });
});

describe("导出前的核对草稿保护", () => {
  it("有未保存输入时禁用下载和保存版本，并提示先保存本句", async () => {
    const request = vi.fn(async () => base);
    const { user, props } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "尚未保存" } });
    const download = screen.getByRole("button", { name: "下载 译文.srt SRT" });
    const save = screen.getByRole("button", { name: "保存字幕版本" });
    expect(download).toBeDisabled();
    expect(save).toBeDisabled();
    expect(screen.getByText(/请先保存本句修改/)).toBeInTheDocument();
    await user.click(download);
    await user.click(save);
    expect(props.api.download).not.toHaveBeenCalled();
    expect(request.mock.calls.filter(([path]) => path === "/api/save-subtitles")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "放弃未保存修改" }));
    expect(download).toBeEnabled();
    expect(save).toBeEnabled();
  });

  it("只标记核对状态的请求尚未完成时也不允许导出旧版本", async () => {
    const pending = deferred();
    const request = vi.fn(path => path === "/api/review-cue" ? pending.promise : Promise.resolve(base));
    const { user } = setup(request);
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    await user.click(screen.getByRole("button", { name: "已核对本句" }));
    expect(screen.getByRole("button", { name: "保存字幕版本" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下载 译文.srt SRT" })).toBeDisabled();
    await act(async () => pending.resolve(updated));
    expect(screen.getByRole("button", { name: "保存字幕版本" })).toBeEnabled();
  });
});
