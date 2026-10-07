import React, { useRef, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import CueTimeline from "./CueTimeline";

const ROW_HEIGHT = 104;
const VIEW_HEIGHT = 312;
const ANCHOR = 80;
const VIEW_KEY = "project-a/sample-a/all";

function makeEntries() {
  return Array.from({ length: 300 }, (_, index) => ({ index, cue: {
    id: `cue-${index}`, start_ms: index * 2000 + 1000, end_ms: index * 2000 + 2500,
    ja: `第 ${index + 1} 句`, zh: `译文 ${index + 1}`,
  } }));
}

function TimelineHarness({ entries, active = 0, viewKey = VIEW_KEY }) {
  const memory = useRef(null);
  const [following, setFollowing] = useState(true);
  return <>
    <button aria-label="跟随播放" aria-pressed={following}>{following ? "跟随播放" : "恢复跟随"}</button>
    <CueTimeline entries={entries} active={active} offset={0} canPlay seek={() => {}} replay={() => {}}
      following={following} onManualBrowse={() => setFollowing(false)} viewMemory={memory} viewKey={viewKey} />
  </>;
}

beforeEach(() => {
  const original = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
    if (!this.matches(".cue-list, .cue-row")) return original.call(this);
    const top = this.matches(".cue-row") ? Number.parseFloat(this.style.top) - this.closest(".cue-list").scrollTop : 0;
    const height = this.matches(".cue-row") ? ROW_HEIGHT : VIEW_HEIGHT;
    return { x: 0, y: top, top, bottom: top + height, left: 0, right: 800, width: 800, height };
  });
});

function browseToAnchor(entries) {
  const result = render(<TimelineHarness entries={entries} />);
  const list = screen.getByLabelText("字幕时间轴");
  Object.defineProperties(list, {
    clientHeight: { configurable: true, value: VIEW_HEIGHT },
    scrollHeight: { configurable: true, value: entries.length * ROW_HEIGHT },
  });
  fireEvent.wheel(list, { deltaY: 600 });
  fireEvent.scroll(list, { target: { scrollTop: ANCHOR * ROW_HEIGHT + 26 } });
  expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
  expect(within(list).getByText(`第 ${ANCHOR + 1} 句`)).toBeInTheDocument();
  return { ...result, list };
}

function anchorTop(list) {
  return within(list).getByText(`第 ${ANCHOR + 1} 句`).closest(".cue-row").getBoundingClientRect().top;
}

describe("字幕时间轴的稳定滚动锚点", () => {
  it.each(["cue-80", 0])("修改顶部可见句的时间后仍停留在同一句，保持暂停跟随（ID=%s）", (id) => {
    const entries = makeEntries();
    entries[ANCHOR].cue.id = id;
    const { list, rerender } = browseToAnchor(entries);
    const beforeTop = anchorTop(list), beforeScroll = list.scrollTop;
    const edited = entries.map(entry => ({ ...entry, cue: { ...entry.cue,
      start_ms: entry.cue.start_ms + (entry.index === ANCHOR ? 125 : 0),
    } }));
    rerender(<TimelineHarness entries={edited} />);
    expect(list.scrollTop).toBe(beforeScroll);
    expect(anchorTop(list)).toBe(beforeTop);
    expect(within(list).getByText(`第 ${ANCHOR + 1} 句`).closest(".cue-row").querySelector("time")).toHaveTextContent(".125");
    expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
    rerender(<TimelineHarness entries={edited} active={2} />);
    expect(list.scrollTop).toBe(beforeScroll);
  });

  it("同一句移到新序号时仍按稳定 ID 保留它在视口中的位置", () => {
    const entries = makeEntries();
    const { list, rerender } = browseToAnchor(entries);
    const beforeTop = anchorTop(list);
    const inserted = [{ index: 0, cue: { id: "inserted", start_ms: 0, end_ms: 500, ja: "新增首句" } },
      ...entries.map(entry => ({ index: entry.index + 1, cue: { ...entry.cue } }))];
    rerender(<TimelineHarness entries={inserted} />);
    expect(anchorTop(list)).toBe(beforeTop);
    expect(list.scrollTop).toBe((ANCHOR + 1) * ROW_HEIGHT + 26);
    expect(screen.getByRole("button", { name: "跟随播放" })).toHaveAttribute("aria-pressed", "false");
  });

  it("另一组字幕不能因旧序号与时间相同而继承滚动锚点", () => {
    const entries = makeEntries();
    const { list, rerender } = browseToAnchor(entries);
    const replacement = entries.map(entry => ({ ...entry, cue: { ...entry.cue, id: `other-${entry.index}` } }));
    rerender(<TimelineHarness entries={replacement} />);
    expect(list.scrollTop).toBe(0);
    expect(within(list).getByText("第 1 句")).toBeInTheDocument();
    expect(within(list).queryByText(`第 ${ANCHOR + 1} 句`)).not.toBeInTheDocument();
  });

  it.each(["project-b/sample-a/all", "project-a/sample-b/all", "project-a/sample-a/filtered"])(
    "视图范围改变时清除旧锚点：%s", (viewKey) => {
      const entries = makeEntries();
      const { list, rerender } = browseToAnchor(entries);
      rerender(<TimelineHarness entries={entries} viewKey={viewKey} />);
      expect(list.scrollTop).toBe(0);
      expect(within(list).getByText("第 1 句")).toBeInTheDocument();
    });

  it("旧格式没有 ID 时仅在序号和时间仍匹配的情况下恢复", () => {
    const entries = makeEntries().map(({ index, cue: { id, ...cue } }) => ({ index, cue }));
    const { list, rerender } = browseToAnchor(entries);
    const beforeTop = anchorTop(list), beforeScroll = list.scrollTop;
    const revised = entries.map(entry => ({ ...entry, cue: { ...entry.cue, zh: `${entry.cue.zh}已核对` } }));
    rerender(<TimelineHarness entries={revised} />);
    expect(list.scrollTop).toBe(beforeScroll);
    expect(anchorTop(list)).toBe(beforeTop);
    const unrelated = revised.map(entry => ({ ...entry, cue: { ...entry.cue,
      start_ms: entry.cue.start_ms + (entry.index === ANCHOR ? 125 : 0),
    } }));
    rerender(<TimelineHarness entries={unrelated} />);
    expect(list.scrollTop).toBe(0);
  });
});
