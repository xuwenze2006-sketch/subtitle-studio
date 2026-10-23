import React from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import App from "./App";

function service({ rejectWrite = false, refreshGate } = {}) {
  let committed = false, failRefresh = true;
  const calls = [];
  const snapshot = {
    source: "film.mp4", campaign: "campaign", project_id: "project-a", project_config: {}, recent: [],
    job: { busy: false, action: "", status: "idle", message: "", logs: [] },
    accounts: { siliconflow: { configured: false, price_per_second: "0.000220", pricing_reference: "账户报价" },
      bailian: {}, deepseek: {} },
    settings: { asr_endpoint: "https://example.test/api/v1", asr_input_rate: "1", asr_output_rate: "2",
      deepseek_input_rate: "3", deepseek_output_rate: "4", pricing_reference: "账户报价" },
    actions: {},
  };
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    calls.push({ url, body: options.body && JSON.parse(options.body) });
    let data = {}, status = 200;
    if (url === "/api/state?include_video=false") {
      if (committed && refreshGate) await refreshGate;
      if (committed && failRefresh) { data = { error: "任务状态暂时无法读取" }; status = 503; }
      else data = snapshot;
    } else if (["/api/credentials", "/api/settings", "/api/siliconflow-settings"].includes(url)) {
      if (rejectWrite) { data = { error: "配置写入失败" }; status = 503; }
      else { committed = true; data = { saved: true }; }
    } else if (url === "/api/environment") data = { version: 1, checks: {}, export_ready: false };
    else if (url.startsWith("/api/preview")) data = { cues: [], selections: [], downloads: [] };
    return new Response(JSON.stringify(data), { status });
  }));
  return { calls, recover: () => { failRefresh = false; } };
}

const click = name => act(async () => { fireEvent.click(screen.getByRole("button", { name, exact: true })); });
async function settings() {
  await act(async () => { render(<App />); });
  await click("API 设置");
}

describe("committed settings receipts when task state cannot be refreshed", () => {
  it("clears the acknowledged key while its follow-up read is still pending", async () => {
    let release;
    const refreshGate = new Promise(resolve => { release = resolve; });
    service({ refreshGate });
    await settings();
    const input = screen.getByLabelText("硅基流动 API Key");
    fireEvent.change(input, { target: { value: "fake-test-key-only" } });
    await click("保存硅基流动 Key");
    try {
      expect(input).toHaveValue("");
      expect(input).toBeDisabled();
      expect(screen.getByRole("status", { name: "硅基流动 Key 保存结果" })).toHaveTextContent("API Key 已保存");
      expect(screen.getByRole("button", { name: "保存硅基流动 Key" })).toBeDisabled();
    } finally {
      await act(async () => { release(); });
    }
  });

  it("clears a committed key and retries only the failed state read", async () => {
    const backend = service();
    await settings();
    const input = screen.getByLabelText("硅基流动 API Key");
    fireEvent.change(input, { target: { value: "fake-test-key-only" } });
    await click("保存硅基流动 Key");

    expect(input).toHaveValue("");
    expect(screen.getByRole("status", { name: "硅基流动 Key 保存结果" })).toHaveTextContent("API Key 已保存");
    expect(screen.getByText(/操作已完成，任务状态暂未更新/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    backend.recover();
    await click("刷新任务状态");
    expect(screen.queryByText(/操作已完成，任务状态暂未更新/)).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "硅基流动 Key 保存结果" })).toBeInTheDocument();
    expect(backend.calls.filter(call => call.url === "/api/credentials")).toHaveLength(1);
  });

  it.each([
    ["我已核实当前硅基流动账户的模型价格", "保存硅基流动价格", "硅基流动价格保存结果"],
    ["我已核实当前资源地区与服务价格", "保存价格配置", "完整流程价格保存结果"],
  ])("retains the committed price receipt and resets its confirmation: %s", async (label, button, receipt) => {
    service();
    await settings();
    fireEvent.click(screen.getByLabelText(label));
    await click(button);
    expect(screen.getByLabelText(label)).not.toBeChecked();
    expect(screen.getByRole("status", { name: receipt })).toHaveTextContent("已保存");
    expect(screen.getByText(/操作已完成，任务状态暂未更新/)).toBeInTheDocument();
  });

  it("keeps the entered key and shows failure when the write itself is rejected", async () => {
    service({ rejectWrite: true });
    await settings();
    const input = screen.getByLabelText("硅基流动 API Key");
    fireEvent.change(input, { target: { value: "fake-test-key-only" } });
    await click("保存硅基流动 Key");
    expect(input).toHaveValue("fake-test-key-only");
    expect(screen.getByRole("alert")).toHaveTextContent("配置写入失败");
    expect(screen.queryByRole("status", { name: "硅基流动 Key 保存结果" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新任务状态" })).not.toBeInTheDocument();
  });
});
