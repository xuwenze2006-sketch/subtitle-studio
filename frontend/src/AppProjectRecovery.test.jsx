import React from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import App from "./App";

const projectError = "当前项目配置无法读取，请保留原目录并检查；可载入其他任务或新建任务。";
const healthyPath = "D:\\正常任务";
function service({ busy = false } = {}) {
  let snapshot = {
    source: "D:\\损坏项目.mp4", campaign: "D:\\损坏任务", baseline: "", project_id: "damaged-project",
    project_config: {}, project_error: projectError, campaign_status: "", local_available: true,
    job: { busy, action: busy ? "local" : "", status: busy ? "running" : "idle", message: "", logs: [] },
    accounts: { siliconflow: {}, bailian: {}, deepseek: {} }, settings: {}, file_layout: {},
    recent: [{ path: healthyPath, title: "正常任务", status: "prepared" }],
    actions: { local: false, prepare: false, samples: false, full: false, approve: false,
      review: false, export: false, "export-draft": false, "siliconflow-pilot": false, "accept-final": false, stop: busy },
  };
  const calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, body });
    let data = {};
    if (url === "/api/project" && body.campaign === healthyPath) {
      snapshot = { ...snapshot, source: "D:\\正常项目.mp4", campaign: healthyPath, project_id: "healthy-project",
        project_error: "", campaign_status: "prepared", actions: { ...snapshot.actions, local: true } };
      data = snapshot;
    } else if (url === "/api/stop") {
      snapshot = { ...snapshot, job: { ...snapshot.job, busy: false, status: "cancelled" } };
      data = { stopped: true };
    } else if (url === "/api/state" || url === "/api/progress") data = snapshot;
    else if (url === "/api/environment") data = { version: 1, checks: {}, export_ready: false };
    else if (url.startsWith("/api/preview")) data = {
      project_id: snapshot.project_id, selected_id: "main", cues: [], selections: [], downloads: [], media_available: false,
    };
    return new Response(JSON.stringify(data), { status: 200 });
  }));
  return calls;
}
const click = name => act(async () => { fireEvent.click(screen.getByRole("button", { name, exact: true })); });

describe("recoverable project configuration errors", () => {
  it("shows the project problem while connected and clears it after loading a healthy task", async () => {
    const calls = service();
    await act(async () => { render(<App />); });
    expect(screen.getByText("本地服务已连接")).toBeVisible();
    expect(screen.queryByText(projectError)).not.toBeNull();
    expect(screen.getByText("当前项目需要检查")).toBeVisible();
    expect(screen.getByRole("button", { name: "新建任务", exact: true })).toBeEnabled();
    expect(screen.getByRole("button", { name: "载入任务 正常任务" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "开始本地识别", exact: true })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "重新连接", exact: true })).not.toBeInTheDocument();

    await click("载入任务 正常任务");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("正常项目.mp4");
    expect(screen.queryByText(projectError)).not.toBeInTheDocument();
    expect(screen.queryByText("当前项目需要检查")).not.toBeInTheDocument();
    expect(screen.getByText("本地服务已连接")).toBeVisible();
    expect(calls.filter(call => call.url === "/api/project")).toEqual([
      { url: "/api/project", body: { campaign: healthyPath } },
    ]);
    expect(calls.some(call => call.url === "/api/run")).toBe(false);
  });

  it("keeps stopping available while a damaged project's task is running", async () => {
    const calls = service({ busy: true });
    await act(async () => { render(<App />); });
    expect(screen.getByText("本地服务已连接")).toBeVisible();
    expect(screen.queryByText(projectError)).not.toBeNull();
    expect(screen.getByRole("button", { name: "新建任务", exact: true })).toBeDisabled();
    expect(screen.getByRole("button", { name: "载入任务 正常任务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止任务", exact: true })).toBeEnabled();
    await click("停止任务");
    expect(calls.filter(call => call.url === "/api/stop")).toEqual([{ url: "/api/stop", body: {} }]);
    expect(screen.getByText(projectError)).toBeVisible();
    expect(screen.getByRole("button", { name: "新建任务", exact: true })).toBeEnabled();
    expect(calls.some(call => call.url === "/api/run")).toBe(false);
  });
});
