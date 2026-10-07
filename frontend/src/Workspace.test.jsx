import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import Workspace from "./Workspace";

function setup(overrides = {}) {
  const snapshot = { source: "D:\\电影.mp4", campaign: "D:\\任务", campaign_status: "full_ready", project_config: { engine: "bailian" },
    job: { busy: false, status: "draft_exported", action: "export-draft", total: 40, recognized: 40, translated: 40, logs: [] },
    accounts: { bailian: { ready: true }, deepseek: { ready: true } }, actions: { export: false, "export-draft": true }, ...overrides.snapshot };
  return render(<Workspace engine={overrides.engine || "bailian"} snapshot={snapshot} paths={{ source: snapshot.source, campaign: snapshot.campaign, baseline: "", ...overrides.paths }}
    parameters={{ language: "ja", target: "zh-CN", translate: true, workers: 2, chunk_seconds: 120 }} reviewValues={{ reviewed: "", timing: "", contentPassed: false, finalPassed: false }}
    disabled={false} busy={overrides.busy || false} pending="" setEngine={vi.fn()} setPaths={vi.fn()} setParameters={vi.fn()} pick={vi.fn()} run={overrides.run || vi.fn()} open={vi.fn()}
    loadProject={vi.fn()} stop={vi.fn()} toSettings={vi.fn()} toPreview={overrides.toPreview || vi.fn()} updateReview={vi.fn()} />);
}

describe("中断任务的明确继续入口", () => {
  it.each([
    ["samples_incomplete", "samples", "继续生成双语样片"],
    ["full_incomplete", "full", "继续处理整片"],
  ])("%s 按允许的动作继续，不会回到不适用的阶段", (stage, action, label) => {
    const run = vi.fn();
    setup({ run, snapshot: { campaign_status: stage, approval_exists: action === "full",
      actions: { prepare: true, [action]: true },
      job: { busy: false, status: stage, action, logs: ["还有片段未完成"] } } });
    const next = screen.getByRole("region", { name: "下一步建议" });
    expect(within(next).getByRole("heading", { name: label })).toBeInTheDocument();
    expect(run).not.toHaveBeenCalled();
    fireEvent.click(within(next).getByRole("button", { name: label }));
    expect(run).toHaveBeenCalledExactlyOnceWith(action, {});
  });

  it("仅有项目阶段的不完整记录也能恢复正确的整片动作", () => {
    const run = vi.fn();
    setup({ run, snapshot: { campaign_status: "full_incomplete", approval_exists: true,
      actions: { full: true }, job: { busy: false, logs: [] } } });
    fireEvent.click(within(screen.getByRole("region", { name: "下一步建议" }))
      .getByRole("button", { name: "继续处理整片" }));
    expect(run).toHaveBeenCalledExactlyOnceWith("full", {});
    expect(screen.getByText("整片未完成")).toBeInTheDocument();
  });

  it("硅基流动只继续允许的试听动作，不替换成百炼调用", () => {
    const run = vi.fn();
    setup({ engine: "siliconflow", run, snapshot: { campaign_status: "samples_incomplete",
      accounts: { siliconflow: { ready: true } }, actions: { "siliconflow-pilot": true, samples: true },
      job: { busy: false, status: "cancelled", action: "siliconflow-pilot", logs: [] } } });
    fireEvent.click(within(screen.getByRole("region", { name: "下一步建议" }))
      .getByRole("button", { name: "继续样片试听" }));
    expect(run).toHaveBeenCalledExactlyOnceWith("siliconflow-pilot", {});
  });

  it.each([
    ["asr_incomplete", "识别未完成"],
    ["translation_incomplete", "翻译未完成"],
  ])("本地 %s 使用中文状态、展开日志并沿原参数继续", (status, label) => {
    const run = vi.fn();
    setup({ engine: "local", run, snapshot: { campaign_status: "", local_available: true,
      project_config: { engine: "local", model: "small" }, actions: { local: true },
      job: { busy: false, status, action: "local", logs: ["未完成片段的原因"] } } });
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(status)).not.toBeInTheDocument();
    expect(screen.getByRole("log").closest("details")).toHaveAttribute("open");
    expect(run).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole("region", { name: "下一步建议" }))
      .getByRole("button", { name: "继续本地任务" }));
    expect(run).toHaveBeenCalledExactlyOnceWith("local", {
      language: "ja", target: "zh-CN", translate: true, workers: 2, chunk_seconds: 120,
    });
  });

  it("草稿导出停止后进入实际草稿所在预览，不引导至禁用的审核版", () => {
    const run = vi.fn(), toPreview = vi.fn();
    setup({ run, toPreview, snapshot: { actions: { prepare: true, review: true, export: false, "export-draft": true },
      job: { busy: false, status: "cancelled", action: "export-draft", logs: [] } } });
    const next = screen.getByRole("region", { name: "下一步建议" });
    expect(within(next).getByRole("heading", { name: "继续导出草稿 MP4" })).toBeInTheDocument();
    fireEvent.click(within(next).getByRole("button", { name: "前往结果预览" }));
    expect(toPreview).toHaveBeenCalledOnce();
    expect(run).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "导出带字幕 MP4" })).toBeDisabled();
  });

  it.each([undefined, null, {}, { full: false }])("缺少可用继续动作时只引导查看日志：%j", (actions) => {
    const run = vi.fn();
    setup({ run, snapshot: { campaign_status: "full_incomplete", actions,
      job: { busy: false, status: "full_incomplete", action: "full", logs: ["尚不满足继续条件"] } } });
    const next = screen.getByRole("region", { name: "下一步建议" });
    expect(within(next).queryByRole("button", { name: "继续处理整片" })).not.toBeInTheDocument();
    const details = screen.getByRole("log").closest("details");
    details.open = false;
    details.scrollIntoView = vi.fn();
    fireEvent.click(within(next).getByRole("button", { name: "查看运行日志" }));
    expect(details.open).toBe(true);
    expect(details.scrollIntoView).toHaveBeenCalledOnce();
    expect(run).not.toHaveBeenCalled();
  });
});

describe("工作台的未审核草稿导出状态", () => {
  it("shows completed draft export separately from the unchanged review stage", () => {
    setup();
    expect(screen.getByText("草稿已导出（未审核）")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "下一步建议" })).getByRole("heading", { name: "查看未审核草稿" })).toBeInTheDocument();
  });

  it("describes draft video encoding without recognition progress while exporting", () => {
    setup({ busy: true, snapshot: { job: { busy: true, action: "export-draft", status: "running", total: 40, recognized: 40, translated: 40, logs: [] } } });
    expect(within(screen.getByRole("region", { name: "下一步建议" })).getByRole("heading", { name: "正在导出草稿 MP4" })).toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: "已识别片段" })).not.toBeInTheDocument();
    expect(screen.getByText("正在压入字幕并编码视频，原视频保留。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导出带字幕 MP4" })).toBeDisabled();
  });

  it("offers direct draft export as the next step for completed unreviewed subtitles", () => {
    setup({ snapshot: { job: { busy: false, status: "full_ready", action: "", logs: [] } } });
    expect(within(screen.getByRole("region", { name: "下一步建议" })).getByText("可直接导出当前字幕；未经人工审核，原视频保留。")).toBeInTheDocument();
  });

  it("presents manual checking as optional when direct draft export is available", () => {
    setup({ snapshot: { manual_review: { supported: true } } });
    expect(screen.getByRole("heading", { name: "可选：整片人工核对" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "整片字幕等待最终抽检" })).not.toBeInTheDocument();
  });
});

const encodingProgress = {
  phase: "encoding", percent: 37.5, encoded_seconds: 375, duration_seconds: 1000,
  speed: 12, eta_seconds: 125, elapsed_seconds: 31.25,
};

function exportingJob(overrides = {}) {
  return { busy: true, action: "export-draft", status: "running", total: 40, recognized: 40,
    translated: 40, logs: [], export_progress: encodingProgress, ...overrides };
}

describe("工作台的视频导出进度", () => {
  it.each(["export-draft", "export"])("shows actual encoding progress for %s", (action) => {
    setup({ busy: true, snapshot: { job: exportingJob({ action }) } });

    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).toHaveAttribute("value", "37.5");
    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).toHaveAttribute("max", "100");
    expect(screen.getByText("37.5%")).toBeInTheDocument();
    expect(screen.getByText("12.0×")).toBeInTheDocument();
    expect(screen.getByText("预计剩余 2 分 5 秒")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: "已识别片段" })).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: "已翻译片段" })).not.toBeInTheDocument();
  });

  it.each([
    ["preparing", "准备视频编码"],
    ["validating", "校验音轨与视频"],
    ["publishing", "保存成片"],
    ["done", "正在完成导出"],
  ])("shows the %s phase without prematurely claiming export completion", (phase, label) => {
    setup({ busy: true, snapshot: { job: exportingJob({ message: "视频编码中，请稍候",
      export_progress: { ...encodingProgress, phase, percent: 100 } }) } });

    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText("视频编码中，请稍候")).not.toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: "视频编码进度" })).not.toBeInTheDocument();
    expect(screen.queryByText("草稿已导出（未审核）")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止任务" })).toBeInTheDocument();
  });

  it("keeps encoding completion separate from successful export", () => {
    setup({ busy: true, snapshot: { job: exportingJob({ export_progress: { ...encodingProgress, percent: 100 } }) } });

    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).toHaveAttribute("value", "100");
    expect(screen.queryByText("草稿已导出（未审核）")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "正在导出草稿 MP4" })).toBeInTheDocument();
  });

  it.each([
    { percent: null, speed: null, eta_seconds: null },
    { percent: NaN, speed: Infinity, eta_seconds: NaN },
    { percent: -1, speed: -3, eta_seconds: -10 },
    { percent: 101, speed: "12", eta_seconds: "125" },
  ])("ignores unavailable or malformed numeric fields: %j", (fields) => {
    setup({ busy: true, snapshot: { job: exportingJob({ export_progress: { ...encodingProgress, ...fields } }) } });

    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).not.toHaveAttribute("value");
    expect(screen.queryByText(/预计剩余/)).not.toBeInTheDocument();
    expect(screen.queryByText(/×/)).not.toBeInTheDocument();
    expect(screen.queryByText(/NaN|Infinity|undefined/)).not.toBeInTheDocument();
  });

  it("shows zero percent and an approximate subminute remaining time", () => {
    setup({ busy: true, snapshot: { job: exportingJob({ export_progress: { ...encodingProgress, percent: 0, speed: 0, eta_seconds: 0.2 } }) } });

    expect(screen.getByRole("progressbar", { name: "视频编码进度" })).toHaveAttribute("value", "0");
    expect(screen.getByText("0%")).toBeInTheDocument();
    expect(screen.queryByText(/×/)).not.toBeInTheDocument();
    expect(screen.getByText("预计剩余 1 秒")).toBeInTheDocument();
  });

  it.each([
    { busy: false, job: { busy: false, status: "draft_exported" } },
    { busy: true, job: { busy: true, action: "full" } },
    { busy: true, job: { busy: false } },
    { busy: true, job: {}, paths: { source: "D:\\另一部电影.mp4" } },
  ])("ignores stale progress outside the current active export: %j", ({ busy, job, paths }) => {
    setup({ busy, paths, snapshot: { job: exportingJob(job) } });

    expect(screen.queryByRole("progressbar", { name: "视频编码进度" })).not.toBeInTheDocument();
    expect(screen.queryByText("12.0×")).not.toBeInTheDocument();
    expect(screen.queryByText(/预计剩余/)).not.toBeInTheDocument();
  });
});
