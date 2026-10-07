import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Preview from "./Preview";

const selections = [
  { id: "sample-1", name: "样片 1", offset_ms: 30000 },
  { id: "sample-2", name: "样片 2", offset_ms: 1680000 },
];
const first = {
  selections,
  selected_id: "sample-1",
  offset_ms: 30000,
  media_url: "/api/media?sample=sample-1",
  media_available: true,
  source_name: "第一段.wav",
  cues: [
    { id: "a", start_ms: 1000, end_ms: 2400, ja: "地図を動かします", zh: "移动地图" },
    { id: "b", start_ms: 3200, end_ms: 4500, ja: "被害を確認します", zh: "查看灾情" },
    { id: "c", start_ms: 6000, end_ms: 7500, ja: "航空写真", zh: "航空照片" },
  ],
  downloads: ["原文_待审核.srt"],
  issues: [],
};
const second = {
  ...first,
  selected_id: "sample-2",
  offset_ms: 1680000,
  media_url: "/api/media?sample=sample-2",
  source_name: "第二段.wav",
  cues: [{ id: "d", start_ms: 2000, end_ms: 4000, ja: "次の場所です", zh: "下一个地点" }],
};
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

function setup(custom = {}, overrides = {}) {
  const api = {
    request: vi.fn(async (path) => path.includes("sample-2") ? second : first),
    download: vi.fn(async () => {}),
    ...custom,
  };
  const props = {
    api,
    snapshot: { source: "film.mp4", campaign: "project-a", job: { busy: false }, actions: {}, file_layout: {} },
    connection: "connected",
    onError: vi.fn(),
    open: vi.fn(),
    run: vi.fn(),
    toWorkspace: vi.fn(),
    disabled: false,
    ...overrides,
  };
  return { ...render(<Preview {...props} />), api, props, user: userEvent.setup() };
}

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});

describe("字幕预览的核对流程", () => {
  it("displays language-neutral cues and actual English Japanese labels", async () => {
    setup({request: vi.fn(async()=>({...first,source_language:'en',target_language:'ja',
      cues:[{id:'en-ja',start_ms:0,end_ms:1000,source_text:'Good morning',target_text:'おはよう'}]}))});
    expect(await screen.findByText('Good morning', {selector: '.cue-original'})).toBeInTheDocument();
    expect(screen.getByText('英语 → 日语')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('搜索原文或译文…')).toBeInTheDocument();
  });
  it("saves a local subtitle version for the displayed project and sample", async () => {
    const folder = "D:\\任务\\导出\\2026-10-03_203500_样片2";
    const request = vi.fn(async (path) => path === "/api/save-subtitles"
      ? { folder, files: [{ name: "影片_原文.srt" }], saved_at: "2026-10-03T20:35:00+08:00", review_status: "draft" }
      : { ...(path.includes("sample-2") ? second : first), project_id: "project-a" });
    const { props, user } = setup({ request });
    await user.selectOptions(await screen.findByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    await user.click(screen.getByRole("button", { name: "保存字幕版本" }));
    expect(request).toHaveBeenCalledWith("/api/save-subtitles", { project_id: "project-a", sample: "sample-2" });
    expect(await screen.findByText(folder)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "打开导出目录" }));
    expect(props.open).toHaveBeenCalledWith("exports", "project-a");
    expect(props.run).not.toHaveBeenCalled();
  });

  it("does not save versions while a task is running", async () => {
    setup({}, { snapshot: { source: "film.mp4", campaign: "project-a", job: { busy: true }, file_layout: {} } });
    expect(await screen.findByRole("button", { name: "保存字幕版本" })).toBeDisabled();
  });

  it("disables version saving for an older backend without blocking existing downloads", async () => {
    const { api, user } = setup({}, { snapshot: { source: "film.mp4", campaign: "project-a", job: { busy: false } } });
    const saveButton = await screen.findByRole("button", { name: "保存字幕版本" });
    expect(saveButton).toBeDisabled();
    expect(screen.getByText("重启字幕工坊后可使用版本保存")).toBeInTheDocument();
    await user.click(saveButton);
    expect(api.request.mock.calls.some(([path]) => path === "/api/save-subtitles")).toBe(false);
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(api.download).toHaveBeenCalledWith("原文_待审核.srt", "sample-1", undefined);
  });

  it("discards a late local save receipt after changing samples", async () => {
    const pending = deferred();
    const request = vi.fn((path) => path === "/api/save-subtitles" ? pending.promise : Promise.resolve(path.includes("sample-2") ? second : first));
    const { user } = setup({ request });
    await user.click(await screen.findByRole("button", { name: "保存字幕版本" }));
    expect(screen.getByRole("button", { name: "保存字幕版本" })).toBeDisabled();
    await user.selectOptions(screen.getByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    await act(async () => pending.resolve({ folder: "旧片段导出目录", files: [] }));
    expect(screen.queryByText("旧片段导出目录")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开导出目录" })).not.toBeInTheDocument();
  });

  it("displays the actual timestamped browser download name", async () => {
    const name = "影片_日语_20261003_203500.srt";
    const { user } = setup({ download: vi.fn().mockResolvedValue(name) });
    await user.click(await screen.findByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(await screen.findByRole("status")).toHaveTextContent(name);
  });

  it("restores the same project's sample, position, search and speed across unmounts without playing", async () => {
    const sessionMemory = { current: null };
    const { props, unmount, user } = setup({}, { sessionMemory, variant: "workbench" });
    await user.selectOptions(await screen.findByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    await user.type(screen.getByRole("searchbox", { name: "搜索字幕" }), "下一个");
    await user.selectOptions(screen.getByRole("combobox", { name: "播放速度" }), "1.25");
    const originalMedia = screen.getByLabelText("源素材预览");
    originalMedia.currentTime = 3.125;
    fireEvent.timeUpdate(originalMedia);
    fireEvent.play(originalMedia);
    unmount();
    render(<Preview {...props} variant="review" />);
    await within(screen.getByLabelText("字幕时间轴")).findByText("下一个地点");
    const restoredMedia = screen.getByLabelText("源素材预览");
    fireEvent.loadedMetadata(restoredMedia);
    expect(screen.getByRole("combobox", { name: "预览片段" })).toHaveValue("sample-2");
    expect(screen.getByRole("searchbox", { name: "搜索字幕" })).toHaveValue("下一个");
    expect(screen.getByRole("combobox", { name: "播放速度" })).toHaveValue("1.25");
    expect(restoredMedia.playbackRate).toBe(1.25);
    expect(restoredMedia.currentTime).toBe(3.125);
    expect(screen.getByRole("button", { name: "播放" })).toBeInTheDocument();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "刷新结果" }));
    const refreshedMedia = await screen.findByLabelText("源素材预览");
    fireEvent.loadedMetadata(refreshedMedia);
    expect(refreshedMedia.currentTime).toBe(0);
    expect(screen.getByRole("searchbox", { name: "搜索字幕" })).toHaveValue("下一个");
  });

  it("discards saved preview state when a remount has a different project identity", async () => {
    const sessionMemory = { current: null };
    const { props, unmount, user } = setup({}, { sessionMemory });
    await user.selectOptions(await screen.findByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    await user.type(screen.getByRole("searchbox", { name: "搜索字幕" }), "下一个");
    await user.selectOptions(screen.getByRole("combobox", { name: "播放速度" }), "1.5");
    const media = screen.getByLabelText("源素材预览");
    media.currentTime = 3.5;
    fireEvent.timeUpdate(media);
    unmount();
    render(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "new-project" }} />);
    await screen.findByText("移动地图");
    const newMedia = screen.getByLabelText("源素材预览");
    fireEvent.loadedMetadata(newMedia);
    expect(screen.getByRole("combobox", { name: "预览片段" })).toHaveValue("sample-1");
    expect(screen.getByRole("searchbox", { name: "搜索字幕" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "播放速度" })).toHaveValue("1");
    expect(newMedia.currentTime).toBe(0);
    expect(newMedia.playbackRate).toBe(1);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("follows active subtitles only inside the cue list without scrolling the page", async () => {
    const { user } = setup();
    await screen.findByText("移动地图");
    const list = screen.getByLabelText("字幕时间轴");
    const seek = screen.getByRole("button", { name: /跳转到 .*被害を確認します/ });
    const row = seek.closest(".cue-row");
    const scrollIntoView = vi.fn();
    row.scrollIntoView = scrollIntoView;
    vi.spyOn(list, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 200 });
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ top: 180, bottom: 274 });
    list.scrollTop = 20;
    await user.click(seek);
    expect(list.scrollTop).toBe(94);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("reveals the beginning of a subtitle taller than the timeline viewport", async () => {
    const { user } = setup();
    const list = await screen.findByLabelText("字幕时间轴");
    const seek = await screen.findByRole("button", { name: /跳转到 .*被害を確認します/ });
    const row = seek.closest(".cue-row");
    vi.spyOn(list, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 200, height: 100 });
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ top: 130, bottom: 470, height: 340 });
    list.scrollTop = 60;
    await user.click(seek);
    expect(list.scrollTop).toBe(90);
  });

  it("keeps full review actions by default and opens compact workbench review only on request", async () => {
    const request = vi.fn().mockResolvedValue({
      ...first,
      issues: ["请检查第二句"],
      auditions: [{ name: "试听样本", text: "尚未对齐的试听文本" }],
      exported_video: { name: "已完成的成片.mp4" },
    });
    const toReview = vi.fn();
    const { props, rerender, user } = setup({ request }, { toReview });
    await screen.findByText("移动地图");
    expect(screen.getByRole("region", { name: "字幕预览工作区" })).toHaveClass("preview-review");
    expect(screen.getByRole("button", { name: "播放成片" })).toBeInTheDocument();
    expect(screen.getByText("尚未对齐的试听文本")).toBeInTheDocument();
    rerender(<Preview {...props} variant="workbench" />);
    expect(screen.getByRole("region", { name: "字幕预览工作区" })).toHaveClass("preview-workbench");
    expect(screen.queryByRole("button", { name: "导出带字幕 MP4" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "播放成片" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开样片对照" })).not.toBeInTheDocument();
    expect(screen.queryByText("尚未对齐的试听文本")).not.toBeInTheDocument();
    expect(toReview).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "进入校对台" }));
    expect(toReview).toHaveBeenCalledTimes(1);
    expect(props.run).not.toHaveBeenCalled();
    expect(props.open).not.toHaveBeenCalled();
  });

  it("keeps search, cue positioning and subtitle downloads in the workbench", async () => {
    const { api, user } = setup({}, { variant: "workbench", toReview: vi.fn() });
    await screen.findByText("移动地图");
    await user.type(screen.getByRole("searchbox", { name: "搜索字幕" }), "灾情");
    expect(screen.queryByText("移动地图")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /跳转到 .*被害を確認します/ }));
    expect(screen.getByLabelText("源素材预览").currentTime).toBe(3.2);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(api.download).toHaveBeenCalledWith("原文_待审核.srt", "sample-1", undefined);
  });

  it("shows real empty workbench state with disabled playback when media is missing", async () => {
    setup({ request: vi.fn().mockResolvedValue({ cues: [], downloads: [], media_available: false }) }, { variant: "workbench", toReview: vi.fn(), disabled: true });
    await screen.findByText("字幕还没有生成");
    expect(screen.getByText("还没有可播放的素材")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "播放" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "进入校对台" })).toBeDisabled();
    expect(screen.getByRole("searchbox", { name: "搜索字幕" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "导出带字幕 MP4" })).not.toBeInTheDocument();
  });

  it("switches sample media and subtitles together, and downloads the selected sample", async () => {
    const pending = deferred();
    const { api, user } = setup({ request: vi.fn((path) => path.includes("sample-2") ? pending.promise : Promise.resolve(first)) });
    const selector = await screen.findByRole("combobox", { name: "预览片段" });
    expect(selector).toHaveValue("sample-1");
    expect(screen.getByLabelText("源素材预览")).toHaveAttribute("src", "/api/media?sample=sample-1");
    await user.selectOptions(selector, "sample-2");
    expect(screen.queryByLabelText("源素材预览")).not.toBeInTheDocument();
    expect(screen.queryByText("移动地图")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("正在读取");
    await act(async () => pending.resolve(second));
    expect(screen.getByLabelText("源素材预览")).toHaveAttribute("src", "/api/media?sample=sample-2");
    expect(screen.getByText("下一个地点")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(api.download).toHaveBeenCalledWith("原文_待审核.srt", "sample-2", undefined);
  });

  it("labels original-video times but seeks within the selected sample", async () => {
    const { user } = setup();
    await screen.findByText("移动地图");
    await user.click(screen.getByRole("button", { name: /跳转到 00:00:31\.000 地図/ }));
    expect(screen.getByLabelText("源素材预览").currentTime).toBe(1);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("searches both languages without renumbering matching cues and shows a clear empty state", async () => {
    const { user } = setup();
    const input = await screen.findByRole("searchbox", { name: "搜索字幕" });
    await user.type(input, "灾情");
    const timeline = screen.getByLabelText("字幕时间轴");
    expect(within(timeline).getByText("02")).toBeInTheDocument();
    expect(within(timeline).getByText("查看灾情")).toBeInTheDocument();
    expect(within(timeline).queryByText("移动地图")).not.toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "航空写真");
    expect(within(timeline).getByText("03")).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "没有这样的字幕");
    expect(within(timeline).getByText("没有找到匹配的字幕")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(within(timeline).getByText("移动地图")).toBeInTheDocument();
  });

  it("replays only the requested cue while ordinary playback crosses cue boundaries", async () => {
    const { user } = setup();
    await screen.findByText("移动地图");
    const media = screen.getByLabelText("源素材预览");
    await user.click(screen.getByRole("button", { name: "播放第 2 句" }));
    expect(media.currentTime).toBe(3.2);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    const pauses = HTMLMediaElement.prototype.pause.mock.calls.length;
    media.currentTime = 4.55;
    fireEvent.timeUpdate(media);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses + 1);
    expect(media.currentTime).toBe(4.5);
    fireEvent.play(media);
    media.currentTime = 7.6;
    fireEvent.timeUpdate(media);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses + 1);
  });

  it("moves between cues and preserves the selected playback rate", async () => {
    const { user } = setup();
    await screen.findByText("移动地图");
    const media = screen.getByLabelText("源素材预览");
    expect(screen.getByRole("button", { name: "上一句" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "下一句" }));
    expect(media.currentTime).toBe(1);
    await user.click(screen.getByRole("button", { name: "下一句" }));
    expect(media.currentTime).toBe(3.2);
    await user.click(screen.getByRole("button", { name: "上一句" }));
    expect(media.currentTime).toBe(1);
    await user.selectOptions(screen.getByRole("combobox", { name: "播放速度" }), "0.75");
    expect(media.playbackRate).toBe(0.75);
  });

  it("discards an outstanding sample response when the project changes", async () => {
    const pending = deferred();
    const { props, rerender, user, api } = setup({ request: vi.fn()
      .mockResolvedValueOnce(first)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({ ...second, source_name: "新项目.wav", cues: [{ ...second.cues[0], zh: "新的项目字幕" }] }) });
    await user.selectOptions(await screen.findByRole("combobox", { name: "预览片段" }), "sample-2");
    const signal = api.request.mock.calls[1][2]?.signal;
    rerender(<Preview {...props} snapshot={{ ...props.snapshot, campaign: "project-b", source: "new.mp4" }} />);
    await screen.findByText("新的项目字幕");
    await act(async () => pending.resolve(second));
    expect(screen.queryByText("下一个地点")).not.toBeInTheDocument();
    expect(screen.getByText("新的项目字幕")).toBeInTheDocument();
    expect(signal?.aborted).toBe(true);
    expect(api.request.mock.calls[2][0]).toBe("/api/preview");
  });

  it("shows loading failure without keeping stale subtitles and permits retry", async () => {
    const { user, props } = setup({ request: vi.fn().mockResolvedValueOnce(first).mockRejectedValueOnce(new Error("读取失败")).mockResolvedValueOnce(first) });
    await screen.findByText("移动地图");
    await user.click(screen.getByRole("button", { name: "刷新结果" }));
    await waitFor(() => expect(props.onError).toHaveBeenCalledWith("读取失败"));
    expect(screen.queryByText("移动地图")).not.toBeInTheDocument();
    expect(screen.getByText("字幕读取失败")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "刷新结果" }));
    expect(await screen.findByText("移动地图")).toBeInTheDocument();
  });

  it("keeps MP4 export behind review and starts only after an explicit click", async () => {
    const { props, rerender, user } = setup();
    await screen.findByText("移动地图");
    expect(screen.getByRole("button", { name: "导出带字幕 MP4" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "返回任务完成审核" }));
    expect(props.toWorkspace).toHaveBeenCalledTimes(1);
    rerender(<Preview {...props} snapshot={{ ...props.snapshot, actions: { export: true } }} />);
    expect(props.run).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "导出带字幕 MP4" }));
    expect(props.run).toHaveBeenCalledWith("export");
  });

  it.each(["review", "workbench"])("starts one draft export without acceptance in the %s layout", async variant => {
    const { props, user } = setup({}, { variant, snapshot: { source: "film.mp4", campaign: "project-a", job: { busy: false }, actions: { export: false, "export-draft": true } } });
    const button = await screen.findByRole("button", { name: "直接导出草稿 MP4" });
    expect(button).toBeEnabled();
    expect(screen.getByText("可直接导出当前字幕；未经人工审核，原视频保留。")).toBeInTheDocument();
    expect(props.run).not.toHaveBeenCalled();
    await user.click(button);
    expect(props.run).toHaveBeenCalledExactlyOnceWith("export-draft");
    if (variant === "review") expect(screen.getByRole("button", { name: "导出带字幕 MP4" })).toBeDisabled();
  });

  it.each([
    { busy: true, allowed: true, action: "export-draft" },
    { busy: true, allowed: true, action: "full" },
    { busy: false, allowed: false, action: "" },
  ])("disables draft export when busy=$busy and available=$allowed", async ({ busy, allowed, action }) => {
    const { props, user } = setup({}, { snapshot: { source: "film.mp4", campaign: "project-a", job: { busy, action }, actions: { export: false, "export-draft": allowed } } });
    const button = await screen.findByRole("button", { name: "直接导出草稿 MP4" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(props.run).not.toHaveBeenCalled();
  });

  it("keeps unreviewed draft and reviewed output names and opening actions separate", async () => {
    const data = { ...first, project_id: "outputs-project", draft_video: { name: "影片_未审核草稿.mp4", review_status: "unreviewed_draft", media_url: "/api/draft-video", download_url: "/api/download?name=draft-video" }, exported_video: { name: "影片_审核版.mp4" } };
    const { props, user } = setup({ request: vi.fn(async () => data) });
    await screen.findByText("影片_未审核草稿.mp4");
    const draft = screen.getByRole("region", { name: "草稿成片导出" });
    const reviewed = screen.getByRole("region", { name: "成片导出" });
    expect(within(draft).getByText("未审核草稿")).toBeInTheDocument();
    expect(within(draft).queryByText("影片_审核版.mp4")).not.toBeInTheDocument();
    expect(within(reviewed).getByText("影片_审核版.mp4")).toBeInTheDocument();
    expect(within(reviewed).queryByText("影片_未审核草稿.mp4")).not.toBeInTheDocument();
    await user.click(within(draft).getByRole("button", { name: "播放草稿" }));
    expect(props.open).toHaveBeenLastCalledWith("draft-video", "outputs-project");
    await user.click(within(draft).getByRole("button", { name: "打开草稿目录" }));
    expect(props.open).toHaveBeenLastCalledWith("draft-video-folder", "outputs-project");
    await user.click(within(reviewed).getByRole("button", { name: "播放成片" }));
    expect(props.open).toHaveBeenLastCalledWith("video", "outputs-project");
  });

  it("offers the verified exported video and its containing folder", async () => {
    const { props, user } = setup({ request: vi.fn().mockResolvedValue({ ...first, exported_video: { name: "影片_中文字幕_修订版.mp4", media_url: "/api/output-video", download_url: "/api/download?name=video" } }) });
    await screen.findByText("影片_中文字幕_修订版.mp4");
    await user.click(screen.getByRole("button", { name: "播放成片" }));
    expect(props.open).toHaveBeenCalledWith("video", undefined);
    await user.click(screen.getByRole("button", { name: "打开成片目录" }));
    expect(props.open).toHaveBeenCalledWith("video-folder", undefined);
  });

  it("binds preview, sample downloads and exported-video actions to the displayed project", async () => {
    const projectId = "project-a";
    const request = vi.fn(async (path) => ({
      ...(path.includes("sample-2") ? second : first),
      project_id: projectId,
      media_url: `/api/media?sample=${path.includes("sample-2") ? "sample-2" : "sample-1"}&project=${projectId}`,
      exported_video: { name: "已绑定的成片.mp4" },
    }));
    const { api, props, user } = setup({ request }, { snapshot: { source: "film.mp4", campaign: "campaign-a", project_id: projectId, job: { busy: false }, actions: {} } });
    await screen.findByText("移动地图");
    expect(new URL(request.mock.calls[0][0], "http://localhost").searchParams.get("project")).toBe(projectId);
    expect(screen.getByLabelText("源素材预览")).toHaveAttribute("src", "/api/media?sample=sample-1&project=project-a");
    await user.selectOptions(screen.getByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    const sampleQuery = new URL(request.mock.calls[1][0], "http://localhost").searchParams;
    expect(sampleQuery.get("project")).toBe(projectId);
    expect(sampleQuery.get("sample")).toBe("sample-2");
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(api.download).toHaveBeenCalledWith("原文_待审核.srt", "sample-2", projectId);
    await user.click(screen.getByRole("button", { name: "播放成片" }));
    expect(props.open).toHaveBeenCalledWith("video", projectId);
    await user.click(screen.getByRole("button", { name: "打开成片目录" }));
    expect(props.open).toHaveBeenCalledWith("video-folder", projectId);
  });

  it("clears an outstanding preview when only the project identity changes", async () => {
    const pending = deferred();
    const request = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValueOnce({ ...second, project_id: "new-identity" });
    const { props, rerender } = setup({ request }, { snapshot: { source: "film.mp4", campaign: "campaign-a", project_id: "old-identity", job: { busy: false } } });
    const oldSignal = request.mock.calls[0][2].signal;
    rerender(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "new-identity" }} />);
    expect(await screen.findByText("下一个地点")).toBeInTheDocument();
    await act(async () => pending.resolve({ ...first, project_id: "old-identity" }));
    expect(screen.queryByText("移动地图")).not.toBeInTheDocument();
    expect(oldSignal.aborted).toBe(true);
  });

  it("preserves millisecond cue boundaries while displaying the original-video offset", async () => {
    setup();
    await screen.findByText("移动地图");
    const timeline = screen.getByLabelText("字幕时间轴");
    expect(within(timeline).getByText("00:00:31.000")).toBeInTheDocument();
    expect(within(timeline).getByText("→ 00:00:32.400")).toBeInTheDocument();
    expect(within(timeline).getByText("00:00:33.200")).toBeInTheDocument();
  });

  it("provides explicit transport and replays the recently located cue after its end", async () => {
    const { user } = setup();
    await screen.findByText("移动地图");
    const media = screen.getByLabelText("源素材预览");
    expect(screen.getByRole("button", { name: "重播当前句" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "播放" }));
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    Object.defineProperty(media, "paused", { configurable: true, value: false });
    fireEvent.play(media);
    const pauses = HTMLMediaElement.prototype.pause.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "暂停" }));
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses + 1);
    fireEvent.pause(media);
    expect(screen.getByRole("button", { name: "播放" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /跳转到 .*被害を確認します/ }));
    await user.click(screen.getByRole("button", { name: "重播当前句" }));
    expect(media.currentTime).toBe(3.2);
    media.currentTime = 4.6;
    fireEvent.timeUpdate(media);
    await user.click(screen.getByRole("button", { name: "重播当前句" }));
    expect(media.currentTime).toBe(3.2);
  });

  it("supports scoped playback and cue shortcuts without trapping page-level keyboard input", async () => {
    setup();
    await screen.findByText("移动地图");
    const region = screen.getByRole("region", { name: "字幕预览工作区" });
    const media = screen.getByLabelText("源素材预览");
    region.focus();
    expect(fireEvent.keyDown(region, { key: "]" })).toBe(false);
    expect(media.currentTime).toBe(1);
    fireEvent.keyDown(region, { key: "]" });
    expect(media.currentTime).toBe(3.2);
    fireEvent.keyDown(region, { key: "r" });
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    media.currentTime = 4.7;
    fireEvent.timeUpdate(media);
    fireEvent.keyDown(region, { key: "R" });
    expect(media.currentTime).toBe(3.2);
    fireEvent.keyDown(region, { key: "[" });
    expect(media.currentTime).toBe(1);
    fireEvent.keyDown(region, { key: " " });
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(3);
    Object.defineProperty(media, "paused", { configurable: true, value: false });
    const pauses = HTMLMediaElement.prototype.pause.mock.calls.length;
    fireEvent.keyDown(region, { key: " " });
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauses + 1);
    fireEvent.keyDown(document.body, { key: "]" });
    expect(media.currentTime).toBe(1);
  });

  it("leaves shortcuts to focused controls, editable content, native media and modified or repeated keys", async () => {
    setup();
    await screen.findByText("移动地图");
    const region = screen.getByRole("region", { name: "字幕预览工作区" });
    const media = screen.getByLabelText("源素材预览");
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    region.appendChild(editable);
    const controls = [
      screen.getByRole("searchbox", { name: "搜索字幕" }),
      screen.getByRole("combobox", { name: "预览片段" }),
      screen.getByRole("button", { name: "下一句" }),
      media,
      editable,
    ];
    for (const control of controls) {
      control.focus();
      for (const key of [" ", "[", "]", "r"]) {
        expect(fireEvent.keyDown(control, { key })).toBe(true);
      }
    }
    region.focus();
    for (const options of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }, { isComposing: true }]) {
      expect(fireEvent.keyDown(region, { key: "]", ...options })).toBe(true);
      expect(fireEvent.keyDown(region, { key: " ", ...options })).toBe(true);
    }
    expect(media.currentTime).toBe(0);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    editable.remove();
  });

  it("reports the exact subtitle handed to the browser and clears the receipt on sample or project switch", async () => {
    const { user, props, rerender } = setup();
    await screen.findByText("移动地图");
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(screen.getByRole("status")).toHaveTextContent("原文_待审核.srt");
    expect(screen.getByRole("status")).toHaveTextContent("已交给浏览器");
    await user.selectOptions(screen.getByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    expect(screen.queryByText(/已交给浏览器/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    expect(screen.getByRole("status")).toHaveTextContent("已交给浏览器");
    rerender(<Preview {...props} snapshot={{ ...props.snapshot, campaign: "new-project" }} />);
    await screen.findByText("移动地图");
    expect(screen.queryByText(/已交给浏览器/)).not.toBeInTheDocument();
  });

  it("does not show a late download receipt after switching to another sample", async () => {
    const pending = deferred();
    const { user } = setup({ download: vi.fn(() => pending.promise) });
    await screen.findByText("移动地图");
    await user.click(screen.getByRole("button", { name: "下载 原文_待审核.srt SRT" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "预览片段" }), "sample-2");
    await screen.findByText("下一个地点");
    await act(async () => pending.resolve());
    expect(screen.queryByText(/已交给浏览器/)).not.toBeInTheDocument();
  });
});

describe("播放错误恢复与连续字幕重播", () => {
  const reviewable = data => ({ ...data, project_id: "project-a", source_language: "ja", target_language: "zh",
    manual_review: { supported: true, revision: "r1", summary: { total: data.cues.length } } });
  const contiguous = { ...first, cues: [{ ...first.cues[0], end_ms: 3200 }, first.cues[1], first.cues[2]] };
  const finishFirst = async user => {
    const media = screen.getByLabelText("源素材预览");
    await user.click(screen.getByRole("button", { name: "播放第 1 句" }));
    media.currentTime = 3.25;
    fireEvent.timeUpdate(media);
    fireEvent.pause(media);
    // Clamping to the requested end also produces a native seeking event.
    fireEvent.seeking(media);
    expect(media.currentTime).toBe(3.2);
    return media;
  };

  it("explicitly reloads failed video paused at its last position without losing dirty review or view state", async () => {
    const { user, api } = setup({ request: vi.fn(async () => reviewable(first)) });
    await user.click(await screen.findByRole("button", { name: "编辑第 1 句" }));
    fireEvent.change(screen.getByLabelText("译文（中文）"), { target: { value: "保留未保存译文" } });
    await user.click(screen.getByRole("button", { name: "跟随播放" }));
    await user.type(screen.getByLabelText("搜索字幕"), "地図");
    await user.selectOptions(screen.getByLabelText("播放速度"), "1.25");
    const oldMedia = screen.getByLabelText("源素材预览");
    oldMedia.currentTime = 1.789;
    fireEvent.timeUpdate(oldMedia);
    fireEvent.play(oldMedia);
    fireEvent.error(oldMedia);
    expect(screen.queryByLabelText("源素材预览")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新加载视频" }));
    const media = screen.getByLabelText("源素材预览");
    expect(media).not.toBe(oldMedia);
    Object.defineProperty(media, "duration", { configurable: true, value: 10 });
    fireEvent.loadedMetadata(media);
    expect(media.currentTime).toBe(1.789);
    expect(media.playbackRate).toBe(1.25);
    expect(screen.getByRole("button", { name: "播放" })).toBeEnabled();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("保留未保存译文");
    expect(screen.getByLabelText("搜索字幕")).toHaveValue("地図");
    expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
    expect(api.request).toHaveBeenCalledTimes(1);
  });

  it("keeps the requested restore position through another media failure before metadata loads", async () => {
    const { user, api } = setup();
    const original = await screen.findByLabelText("源素材预览");
    original.currentTime = 4.321;
    fireEvent.timeUpdate(original);
    fireEvent.error(original);
    await user.click(screen.getByRole("button", { name: "重新加载视频" }));
    fireEvent.error(screen.getByLabelText("源素材预览"));
    await user.click(screen.getByRole("button", { name: "重新加载视频" }));
    const media = screen.getByLabelText("源素材预览");
    Object.defineProperty(media, "duration", { configurable: true, value: 10 });
    fireEvent.loadedMetadata(media);
    expect(media.currentTime).toBe(4.321);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(api.request).toHaveBeenCalledTimes(1);
  });

  it.each(["button", "R"])("replays the just-finished sentence at a contiguous boundary using %s", async control => {
    const { user } = setup({ request: vi.fn(async () => contiguous) });
    await screen.findByText("移动地图");
    const media = await finishFirst(user);
    if (control === "button") await user.click(screen.getByRole("button", { name: "重播当前句" }));
    else fireEvent.keyDown(screen.getByRole("region", { name: "字幕预览工作区" }), { key: "R" });
    expect(media.currentTime).toBe(1);
  });

  it("retains the finished sentence by ID when a review refresh inserts an earlier cue", async () => {
    const inserted = { ...contiguous, cues: [{ ...first.cues[0], id: "new", start_ms: 0, end_ms: 500 }, ...contiguous.cues] };
    const request = vi.fn().mockResolvedValueOnce(reviewable(contiguous)).mockResolvedValue(reviewable(inserted));
    const { user } = setup({ request });
    await screen.findByText("移动地图");
    const media = await finishFirst(user);
    await user.click(screen.getByRole("button", { name: "刷新核对状态" }));
    await user.click(screen.getByRole("button", { name: "重播当前句" }));
    expect(media.currentTime).toBe(1);
  });

  it.each(["cue seek", "native seek", "play button", "native play"])("clears the finished replay target for %s", async control => {
    const { user } = setup({ request: vi.fn(async () => contiguous) });
    await screen.findByText("移动地图");
    const media = await finishFirst(user);
    if (control === "cue seek") await user.click(screen.getByRole("button", { name: /跳转到 .*被害を確認します/ }));
    else if (control === "native seek") { media.currentTime = 3.5; fireEvent.seeking(media); fireEvent.timeUpdate(media); }
    else if (control === "play button") await user.click(screen.getByRole("button", { name: "播放" }));
    else fireEvent.play(media);
    await user.click(screen.getByRole("button", { name: "重播当前句" }));
    expect(media.currentTime).toBe(3.2);
  });

  it("clears the finished replay target on a sample switch even if the next sample reuses its cue ID", async () => {
    const other = { ...second, cues: [{ ...second.cues[0], id: "a" }] };
    const { user } = setup({ request: vi.fn(async path => path.includes("sample-2") ? other : contiguous) });
    await screen.findByText("移动地图");
    await finishFirst(user);
    await user.selectOptions(screen.getByLabelText("预览片段"), "sample-2");
    await screen.findByText("下一个地点");
    expect(screen.getByRole("button", { name: "重播当前句" })).toBeDisabled();
    expect(screen.getByLabelText("源素材预览").currentTime).toBe(0);
  });
});

describe("长字幕列表的滚动与定位", () => {
  const longPreview = {
    ...first,
    downloads: [],
    cues: Array.from({ length: 4102 }, (_, index) => ({
      id: `long-${index + 1}`,
      start_ms: index * 2000 + 1000,
      end_ms: index * 2000 + 2500,
      ja: `長編の第${index + 1}句`,
      zh: index === 4101 ? "终点检索词4102" : `长片译文${index + 1}`,
    })),
  };

  async function readyLongList(custom = {}, overrides = {}) {
    const result = setup({ request: vi.fn().mockResolvedValue(longPreview), ...custom }, overrides);
    await waitFor(() => expect(result.container.querySelector(".cue-row")).not.toBeNull());
    return { ...result, list: result.container.querySelector(".cue-list") };
  }

  it("4102 条字幕仅挂载少量行，滚到尾部仍可访问最后一条", async () => {
    const { list } = await readyLongList({}, { variant: "workbench" });
    expect(list.querySelectorAll(".cue-row").length).toBeLessThan(80);
    expect(within(list).getByText("長編の第1句")).toBeInTheDocument();
    expect(within(list).queryByText("長編の第4102句")).not.toBeInTheDocument();

    // jsdom does not lay out rows; use the documented estimate only to reach the end.
    fireEvent.scroll(list, { target: { scrollTop: 4102 * 104 - 455 } });
    expect(await within(list).findByText("長編の第4102句")).toBeInTheDocument();
    expect(list.querySelectorAll(".cue-row").length).toBeLessThan(80);
    expect(within(list).queryByText("長編の第1句")).not.toBeInTheDocument();
  }, 20000);

  it("搜索覆盖未挂载字幕并保留原序号、原片时码和样片内跳转", async () => {
    const { list, user } = await readyLongList();
    const input = screen.getByRole("searchbox", { name: "搜索字幕" });
    fireEvent.change(input, { target: { value: "终点检索词4102" } });
    const row = (await within(list).findByText("终点检索词4102")).closest(".cue-row");
    expect(list.querySelectorAll(".cue-row")).toHaveLength(1);
    expect(row.querySelector(".cue-index")).toHaveTextContent("4102");
    const seek = within(row).getByRole("button", { name: "跳转到 02:17:13.000 長編の第4102句" });
    await user.click(seek);
    expect(screen.getByLabelText("源素材预览").currentTime).toBe(8203);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "没有任何字幕包含这个关键词" } });
    expect(list.querySelectorAll(".cue-row")).toHaveLength(0);
    expect(within(list).getByText("没有找到匹配的字幕")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "" } });
    expect(await within(list).findByText("長編の第4102句")).toBeInTheDocument();
    expect(list.querySelectorAll(".cue-row").length).toBeLessThan(80);
    expect(within(list).queryByText("没有找到匹配的字幕")).not.toBeInTheDocument();
  }, 20000);

  it("播放位置跳到远端时呈现当前行并保持原序号", async () => {
    const { list } = await readyLongList();
    const media = screen.getByLabelText("源素材预览");
    media.currentTime = 7999.5;
    fireEvent.timeUpdate(media);
    const row = (await within(list).findByText("長編の第4000句")).closest(".cue-row");
    expect(row).toHaveClass("active-cue");
    expect(row.querySelector(".cue-index")).toHaveTextContent("4000");
    expect(row.querySelector(".cue-seek")).toHaveAttribute("aria-current", "true");
    expect(list.querySelectorAll(".cue-row").length).toBeLessThan(80);
  }, 20000);

  it("从长列表尾部切换样片时清除旧行并正确显示短列表", async () => {
    const pending = deferred();
    const { list } = await readyLongList({
      request: vi.fn((path) => path.includes("sample-2") ? pending.promise : Promise.resolve(longPreview)),
    });
    fireEvent.scroll(list, { target: { scrollTop: 4102 * 104 - 455 } });
    await within(list).findByText("長編の第4102句");
    fireEvent.change(screen.getByRole("combobox", { name: "预览片段" }), { target: { value: "sample-2" } });
    expect(screen.getByLabelText("字幕时间轴").querySelectorAll(".cue-row")).toHaveLength(0);
    await act(async () => pending.resolve(second));
    const newList = screen.getByLabelText("字幕时间轴");
    expect(await within(newList).findByText("下一个地点")).toBeInTheDocument();
    expect(newList.querySelectorAll(".cue-row")).toHaveLength(1);
    expect(newList.querySelector(".cue-index")).toHaveTextContent("01");
    expect(within(newList).queryByText("長編の第4102句")).not.toBeInTheDocument();
    expect(screen.getByLabelText("源素材预览")).toHaveAttribute("src", "/api/media?sample=sample-2");
  }, 20000);

  it("视口已在顶部时 Home 立即聚焦已挂载首句", async () => {
    setup();
    const firstCue = await screen.findByRole("button", { name: /跳转到 .*地図を動かします/ });
    const secondCue = screen.getByRole("button", { name: /跳转到 .*被害を確認します/ });
    const list = screen.getByLabelText("字幕时间轴");
    list.scrollTop = 0;
    secondCue.focus();
    fireEvent.keyDown(secondCue, { key: "Home" });
    expect(firstCue).toHaveFocus();
    expect(list.scrollTop).toBe(0);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  describe("手动跟随与列表位置恢复", () => {
    function browseAway(list) {
      fireEvent.wheel(list, { deltaY: 600 });
      fireEvent.scroll(list, { target: { scrollTop: 250 * 104 + 26 } });
    }

    it("字幕按钮上的空格保留原生激活，不关闭自动跟随", async () => {
      const { user } = setup();
      const seek = await screen.findByRole("button", { name: /跳转到 .*地図を動かします/ });
      seek.focus();
      await user.keyboard(" ");
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByLabelText("源素材预览").currentTime).toBe(1);
      expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    });

    it("手动浏览后新活动字幕仍更新，但不会抢回滚动位置", async () => {
      const { list } = await readyLongList();
      browseAway(list);
      const follow = screen.getByRole("button", { name: "跟随播放" });
      expect(follow).toHaveAttribute("aria-pressed", "false");
      expect(follow).toHaveTextContent("恢复跟随");
      const position = list.scrollTop;
      const media = screen.getByLabelText("源素材预览");
      media.currentTime = 1.5;
      fireEvent.timeUpdate(media);
      media.currentTime = 3.5;
      fireEvent.timeUpdate(media);
      expect(list.scrollTop).toBe(position);
      expect(within(list).queryByText("長編の第2句")).not.toBeInTheDocument();
      expect(document.querySelector(".subtitle-overlay")).toHaveTextContent("長編の第2句");
    });

    it("恢复跟随清空搜索并定位当前句，但不跳转或启动视频", async () => {
      const { list, user } = await readyLongList();
      const media = screen.getByLabelText("源素材预览");
      media.currentTime = 1.5;
      fireEvent.timeUpdate(media);
      browseAway(list);
      const input = screen.getByRole("searchbox", { name: "搜索字幕" });
      fireEvent.change(input, { target: { value: "无匹配的搜索内容" } });
      expect(within(list).getByText("没有找到匹配的字幕")).toBeInTheDocument();
      fireEvent.change(input, { target: { value: "" } });
      expect(within(list).getByText("長編の第1句")).toBeInTheDocument();
      expect(list.scrollTop).toBe(0);
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
      fireEvent.change(input, { target: { value: "终点检索词4102" } });
      await within(list).findByText("终点检索词4102");
      const pauseCount = HTMLMediaElement.prototype.pause.mock.calls.length;
      const playCount = HTMLMediaElement.prototype.play.mock.calls.length;
      await user.click(screen.getByRole("button", { name: "跟随播放" }));
      expect(input).toHaveValue("");
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "true");
      expect((await within(list).findByText("長編の第1句")).closest(".cue-row")).toHaveClass("active-cue");
      expect(media.currentTime).toBe(1.5);
      expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(pauseCount);
      expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(playCount);
    });

    it("暂停跟随后显式下一句只定位一次，不重新开启自动跟随", async () => {
      const { list, user } = await readyLongList();
      const media = screen.getByLabelText("源素材预览");
      media.currentTime = 1.5;
      fireEvent.timeUpdate(media);
      browseAway(list);
      await user.click(screen.getByRole("button", { name: "下一句" }));
      expect(media.currentTime).toBe(3);
      expect((await within(list).findByText("長編の第2句")).closest(".cue-row")).toHaveClass("active-cue");
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
      const position = list.scrollTop;
      media.currentTime = 7999.5;
      fireEvent.timeUpdate(media);
      expect(list.scrollTop).toBe(position);
      expect(within(list).queryByText("長編の第4000句")).not.toBeInTheDocument();
      expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    });

    it("同项目同样片跨工作台和校对台保留手动状态及列表锚点", async () => {
      const sessionMemory = { current: null };
      const { list, props, unmount } = await readyLongList({}, { sessionMemory, variant: "workbench" });
      const media = screen.getByLabelText("源素材预览");
      media.currentTime = 1.5;
      fireEvent.timeUpdate(media);
      browseAway(list);
      const anchor = (await within(list).findByText("長編の第251句")).closest(".cue-row");
      const relativeTop = Number.parseFloat(anchor.style.top) - list.scrollTop;
      unmount();
      render(<Preview {...props} variant="review" />);
      const newList = screen.getByLabelText("字幕时间轴");
      const restoredAnchor = (await within(newList).findByText("長編の第251句")).closest(".cue-row");
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
      expect(Number.parseFloat(restoredAnchor.style.top) - newList.scrollTop).toBeCloseTo(relativeTop, 0);
      fireEvent.loadedMetadata(screen.getByLabelText("源素材预览"));
      expect(Number.parseFloat(restoredAnchor.style.top) - newList.scrollTop).toBeCloseTo(relativeTop, 0);
      expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    });

    it("换样片或新项目不继承旧列表锚点与暂停跟随状态", async () => {
      const { list, props, rerender } = await readyLongList({
        request: vi.fn(async (path) => path.includes("sample-2") ? second : longPreview),
      }, { sessionMemory: { current: null } });
      browseAway(list);
      fireEvent.change(screen.getByRole("combobox", { name: "预览片段" }), { target: { value: "sample-2" } });
      await within(screen.getByLabelText("字幕时间轴")).findByText("下一个地点");
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByLabelText("字幕时间轴").scrollTop).toBe(0);
      fireEvent.change(screen.getByRole("combobox", { name: "预览片段" }), { target: { value: "sample-1" } });
      await within(screen.getByLabelText("字幕时间轴")).findByText("長編の第1句");
      browseAway(screen.getByLabelText("字幕时间轴"));
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
      rerender(<Preview {...props} snapshot={{ ...props.snapshot, project_id: "unrelated-project" }} />);
      const newList = screen.getByLabelText("字幕时间轴");
      await within(newList).findByText("長編の第1句");
      expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "true");
      expect(newList.scrollTop).toBe(0);
      expect(within(newList).queryByText("長編の第251句")).not.toBeInTheDocument();
    });
  });
});
