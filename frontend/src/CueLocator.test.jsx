import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CueLocator from "./CueLocator";

const cues = Object.freeze([
  Object.freeze({ id: 91, start_ms: 1000, end_ms: 2000 }),
  Object.freeze({ id: 7, start_ms: 3500, end_ms: 5000 }),
  Object.freeze({ id: 91, start_ms: 4500, end_ms: 6500 }),
]);

function setup(extra = {}) {
  const props = { cues, onLocate: vi.fn(), ...extra };
  return { ...render(<CueLocator {...props} />), props, user: userEvent.setup() };
}

async function enterTime(user, value) {
  await user.selectOptions(screen.getByRole("combobox", { name: "定位方式" }), "time");
  fireEvent.change(screen.getByRole("textbox", { name: "原片时间" }), { target: { value } });
}

function submit() {
  fireEvent.submit(screen.getByRole("form", { name: "字幕定位" }));
}

describe("字幕定位", () => {
  it("locates an original array position with a button, independent of duplicate cue ids", async () => {
    const { user, props } = setup();
    await user.type(screen.getByRole("textbox", { name: "字幕序号" }), "3");
    await user.click(screen.getByRole("button", { name: "定位字幕" }));
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[2], 2);
    expect(screen.getByRole("status")).toHaveTextContent("已定位第 3 条字幕");
    expect(cues[2]).toEqual({ id: 91, start_ms: 4500, end_ms: 6500 });
  });

  it("uses the same submission through Enter and supports a large original array", async () => {
    const many = Array.from({ length: 4500 }, (_, index) => ({ id: 1, start_ms: index * 2000, end_ms: index * 2000 + 1000 }));
    const { user, props } = setup({ cues: many });
    await user.type(screen.getByRole("textbox", { name: "字幕序号" }), "4500{Enter}");
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(many[4499], 4499);
    expect(screen.getByRole("status")).toHaveTextContent("第 4500 条");
  });

  it.each(["", "0", "-1", "1.5", "1e0", "+1", "4", "9007199254740992", "9".repeat(33)])(
    "rejects an invalid or out-of-range cue number %j without calling the parent", value => {
      const { props } = setup();
      const input = screen.getByRole("textbox", { name: "字幕序号" });
      fireEvent.change(input, { target: { value } });
      submit();
      expect(props.onLocate).not.toHaveBeenCalled();
      expect(screen.getByRole("alert")).toHaveTextContent(/序号|长度/);
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(input).toHaveAccessibleDescription(/序号|长度/);
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    },
  );

  it.each(["00:01:01.500", "61.5"])("maps original time %s through the sample offset", async value => {
    const { user, props } = setup({ offset: 60000 });
    await enterTime(user, value);
    await user.click(screen.getByRole("button", { name: "定位字幕" }));
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[0], 0);
    expect(screen.getByRole("status")).toHaveTextContent("已定位第 1 条字幕");
  });

  it.each(["2", "2.5"])("locates the following cue at an exclusive end or gap at %s seconds", async value => {
    const { user, props } = setup();
    await enterTime(user, value);
    submit();
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[1], 1);
    expect(screen.getByRole("status")).toHaveTextContent(/空隙.*后续.*第 2 条/);
  });

  it("uses the first original entry whose end exceeds time even when intervals overlap", async () => {
    const { user, props } = setup();
    await enterTime(user, "4.75");
    submit();
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[1], 1);
  });

  it("locates a cue starting exactly at the requested time without a gap claim", async () => {
    const { user, props } = setup();
    await enterTime(user, "3.5");
    submit();
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[1], 1);
    expect(screen.getByRole("status")).not.toHaveTextContent("空隙");
  });

  it("explains the opening gap instead of pretending that it contains a cue", async () => {
    const { user, props } = setup();
    await enterTime(user, "0");
    submit();
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[0], 0);
    expect(screen.getByRole("status")).toHaveTextContent(/空隙.*后续.*第 1 条/);
  });

  it.each(["6.5", "7"])("rejects tail time %s instead of silently locating the last cue", async value => {
    const { user, props } = setup();
    await enterTime(user, value);
    submit();
    expect(props.onLocate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/末尾/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("rejects original time before a sample begins", async () => {
    const { user, props } = setup({ offset: 60000 });
    await enterTime(user, "59.999");
    submit();
    expect(props.onLocate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/早于.*起点/);
  });

  it.each(["", "-1", "1e2", "00:60:00.000", "1.0001", "9007199254740.992", "9999999999:00:00.000", "0".repeat(33)])(
    "rejects invalid, unsafe, or overly long time %j before locating", async value => {
      const { user, props } = setup();
      await enterTime(user, value);
      submit();
      expect(props.onLocate).not.toHaveBeenCalled();
      expect(screen.getByRole("alert")).toHaveTextContent(/时间|长度/);
      expect(screen.getByRole("textbox", { name: "原片时间" })).toHaveAttribute("aria-invalid", "true");
    },
  );

  it("reports a parent block and preserves input without claiming success", async () => {
    const { user, props } = setup({ onLocate: vi.fn(() => "请先保存本句修改。") });
    const input = screen.getByRole("textbox", { name: "字幕序号" });
    await user.type(input, "2");
    await user.click(screen.getByRole("button", { name: "定位字幕" }));
    expect(props.onLocate).toHaveBeenCalledExactlyOnceWith(cues[1], 1);
    expect(screen.getByRole("alert")).toHaveTextContent("请先保存本句修改。");
    expect(input).toHaveValue("2");
    expect(input).toHaveAttribute("aria-invalid", "false");
    expect(input).toHaveAccessibleDescription(/请先保存本句修改/);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("rejects an overlong paste intact instead of truncating it into a different valid time", async () => {
    const { user, props } = setup();
    await enterTime(user, "");
    const input = screen.getByRole("textbox", { name: "原片时间" });
    const pasted = "0".repeat(32) + "1";
    await user.click(input);
    await user.paste(pasted);
    await user.click(screen.getByRole("button", { name: "定位字幕" }));
    expect(props.onLocate).not.toHaveBeenCalled();
    expect(input).toHaveValue(pasted);
    expect(screen.getByRole("alert")).toHaveTextContent("长度");
  });

  it("never claims success if the synchronous parent callback throws", () => {
    setup({ onLocate: () => { throw new Error("定位未完成"); } });
    fireEvent.change(screen.getByRole("textbox", { name: "字幕序号" }), { target: { value: "1" } });
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent("定位未完成");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it.each(["disabled", "empty"])("blocks both controls and direct submit while %s", mode => {
    const { props } = setup(mode === "empty" ? { cues: [] } : { disabled: true });
    const input = screen.getByRole("textbox", { name: "字幕序号" });
    expect(input).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "定位方式" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "定位字幕" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "1" } });
    submit();
    expect(props.onLocate).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("clears obsolete feedback on new input, mode, and dataset while keeping unique accessible ids", async () => {
    const { user, props, rerender } = setup();
    const input = screen.getByRole("textbox", { name: "字幕序号" });
    await user.type(input, "1");
    submit();
    expect(screen.getByRole("status")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "2" } });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "0" } });
    submit();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "定位方式" }), "time");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "原片时间" }), { target: { value: "1" } });
    submit();
    expect(screen.getByRole("status")).toBeInTheDocument();
    rerender(<CueLocator {...props} offset={60000} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    rerender(<><CueLocator {...props} /><CueLocator {...props} /></>);
    const inputs = screen.getAllByRole("textbox");
    expect(inputs[0].id).not.toBe(inputs[1].id);
    inputs.forEach(item => expect(item).toHaveAccessibleDescription());
  });
});
