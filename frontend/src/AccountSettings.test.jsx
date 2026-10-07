import React from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AccountSettings from "./AccountSettings";

function setup(save = vi.fn().mockResolvedValue(true)) {
  render(
    <AccountSettings
      snapshot={{
        accounts: {
          siliconflow: {
            price_per_second: "0.000220",
            pricing_reference: "账户模型报价页",
          },
        },
        settings: {
          asr_endpoint: "https://dashscope.aliyuncs.com/api/v1",
          asr_input_rate: "0.8",
          asr_output_rate: "2.7",
          deepseek_input_rate: "2",
          deepseek_output_rate: "8",
          verified_on: "2026-10-03",
          pricing_reference: "当前账户报价",
        },
      }}
      disabled={false}
      pending=""
      save={save}
    />,
  );
  return { save, user: userEvent.setup() };
}

const providers = [
  ["硅基流动", "siliconflow", "保存硅基流动 Key"],
  ["阿里云百炼", "bailian", "保存百炼 Key"],
  ["DeepSeek", "deepseek", "保存DeepSeek Key"],
];

describe("API Key 保存回执", () => {
  it.each(providers)("%s 在对应卡片反馈成功，修改后清除且不显示密钥", async (name, id, buttonName) => {
    const { user, save } = setup();
    const input = screen.getByLabelText(`${name} API Key`);
    const form = input.closest("form");
    await user.type(input, "test-secret-not-for-display");
    await user.click(screen.getByRole("button", { name: buttonName }));

    const status = within(form).getByRole("status", { name: `${name} Key 保存结果` });
    expect(status).toHaveTextContent("API Key 已保存");
    expect(status).toHaveClass("form-save-status");
    expect(form).not.toHaveTextContent("test-secret-not-for-display");
    expect(input).toHaveValue("");
    expect(save).toHaveBeenCalledWith(`key-${id}`, "/api/credentials", {
      provider: id, key: "test-secret-not-for-display",
    }, expect.any(String));
    expect(document.querySelectorAll(".form-save-status")).toHaveLength(1);

    await user.type(input, "replacement-key");
    expect(within(form).queryByRole("status", { name: `${name} Key 保存结果` })).not.toBeInTheDocument();
  });

  it("保存失败时保留输入并且没有成功回执", async () => {
    const { user } = setup(vi.fn().mockResolvedValue(false));
    const input = screen.getByLabelText("DeepSeek API Key");
    await user.type(input, "unsaved-key");
    await user.click(screen.getByRole("button", { name: "保存DeepSeek Key" }));
    expect(input).toHaveValue("unsaved-key");
    expect(screen.queryByRole("status", { name: "DeepSeek Key 保存结果" })).not.toBeInTheDocument();
  });

  it("较早请求返回成功时不会清掉后来输入的 Key 或为它显示已保存", async () => {
    let resolve;
    const save = vi.fn(() => new Promise((done) => { resolve = done; }));
    const { user } = setup(save);
    const input = screen.getByLabelText("DeepSeek API Key");
    await user.type(input, "first-key");
    await user.click(screen.getByRole("button", { name: "保存DeepSeek Key" }));
    fireEvent.change(input, { target: { value: "later-key" } });
    await act(async () => { resolve(true); });
    expect(input).toHaveValue("later-key");
    expect(screen.queryByRole("status", { name: "DeepSeek Key 保存结果" })).not.toBeInTheDocument();
  });
});

const pricingForms = [
  {
    name: "硅基流动价格保存结果",
    button: "保存硅基流动价格",
    checkbox: "我已核实当前硅基流动账户的模型价格",
    field: "硅基流动报价来源",
    action: "siliconflow-prices",
    url: "/api/siliconflow-settings",
    payload: { price_per_second: 0.00022, pricing_reference: "账户模型报价页", confirmed: true },
  },
  {
    name: "完整流程价格保存结果",
    button: "保存价格配置",
    checkbox: "我已核实当前资源地区与服务价格",
    field: "报价来源说明",
    action: "prices",
    url: "/api/settings",
    payload: {
      asr_endpoint: "https://dashscope.aliyuncs.com/api/v1",
      asr_input_rate: "0.8", asr_output_rate: "2.7",
      deepseek_input_rate: "2", deepseek_output_rate: "8",
      pricing_reference: "当前账户报价", confirmed: true,
    },
  },
];

describe("价格配置保存回执", () => {
  it.each(pricingForms)("$name 在本表单反馈成功并在字段修改后清除", async (formInfo) => {
    const { user, save } = setup();
    const button = screen.getByRole("button", { name: formInfo.button });
    const form = button.closest("form");
    expect(button).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: formInfo.checkbox }));
    await user.click(button);
    const status = within(form).getByRole("status", { name: formInfo.name });
    expect(status).toHaveTextContent("已保存");
    expect(status).toHaveTextContent("不会调用模型");
    expect(status).toHaveClass("form-save-status");
    expect(save).toHaveBeenCalledWith(formInfo.action, formInfo.url, formInfo.payload, expect.any(String));
    expect(screen.getByRole("checkbox", { name: formInfo.checkbox })).not.toBeChecked();
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText(formInfo.field), "补充");
    expect(within(form).queryByRole("status", { name: formInfo.name })).not.toBeInTheDocument();
  });

  it.each(pricingForms)("$name 重新确认时清除旧回执，后续失败不显示成功", async (formInfo) => {
    const { user } = setup(vi.fn().mockResolvedValueOnce(true).mockResolvedValue(false));
    const button = screen.getByRole("button", { name: formInfo.button });
    const checkbox = screen.getByRole("checkbox", { name: formInfo.checkbox });
    await user.click(checkbox);
    await user.click(button);
    expect(screen.getByRole("status", { name: formInfo.name })).toBeInTheDocument();
    await user.click(checkbox);
    expect(screen.queryByRole("status", { name: formInfo.name })).not.toBeInTheDocument();
    await user.click(button);
    expect(screen.queryByRole("status", { name: formInfo.name })).not.toBeInTheDocument();
    expect(checkbox).toBeChecked();
  });
});
