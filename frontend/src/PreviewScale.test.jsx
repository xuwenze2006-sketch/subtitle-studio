import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import Preview from "./Preview";
import { createReviewDraft } from "./ManualReview";

const cue = index => ({ id: index, start_ms: index * 2000, end_ms: index * 2000 + 1000,
  source_text: `source ${index}`, target_text: `target ${index}`, review_status: "checked", note: "", warnings: [] });
const dataFor = cues => ({ project_id: "scale-project", selected_id: "main", selections: [{ id: "main", name: "整片" }],
  source_language: "en", target_language: "zh", media_url: "/api/media", media_available: true, downloads: [], cues,
  manual_review: { supported: true, revision: "r1", summary: { total: cues.length, checked: cues.length, can_accept: true } } });

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

describe("large subtitle playback metadata", () => {
  it("does not rescan the whole subtitle set for a playback tick or draft field edit", async () => {
    const size = 20000, cues = Array.from({ length: size }, (_, index) => cue(index));
    const preview = dataFor(cues);
    const sessionMemory = { current: { projectKey: '["film.mp4","campaign","scale-project"]', selection: "main",
      reviewDraft: createReviewDraft(cues.at(-1), "r1", "main") } };
    const props = { api: { request: vi.fn(async () => preview) }, snapshot: { source: "film.mp4", campaign: "campaign",
      project_id: "scale-project", job: { busy: false } }, connection: "connected", onError: vi.fn(), open: vi.fn(), sessionMemory };
    await act(async () => render(<Preview {...props} />));
    const media = screen.getByLabelText("源素材预览");
    let visits = 0;
    const some = Array.prototype.some, findIndex = Array.prototype.findIndex;
    const count = (method, original) => vi.spyOn(Array.prototype, method).mockImplementation(function (predicate, receiver) {
      return original.call(this, (item, index, array) => {
        if (this.length === size) visits += 1;
        return predicate.call(receiver, item, index, array);
      });
    });
    const counters = [count("some", some), count("findIndex", findIndex)];
    try {
      media.currentTime = 0.25;
      fireEvent.timeUpdate(media);
      fireEvent.change(screen.getByLabelText("核对备注"), { target: { value: "keep the selected cue" } });
      expect(visits).toBe(0);
    } finally { counters.forEach(counter => counter.mockRestore()); }
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("target 19999");
    expect(screen.getByRole("button", { name: "下一条未检查" })).toBeDisabled();
  });

  it("recomputes the selected cue and unchecked availability when refreshed subtitle data changes", async () => {
    let preview = dataFor([cue(0), cue(1)]);
    const props = { api: { request: vi.fn(async () => preview) }, snapshot: { source: "film.mp4", campaign: "campaign",
      project_id: "scale-project", job: { busy: false } }, connection: "connected", onError: vi.fn(), open: vi.fn(),
      sessionMemory: { current: { projectKey: '["film.mp4","campaign","scale-project"]', selection: "main",
        reviewDraft: createReviewDraft(preview.cues[1], "r1", "main") } } };
    await act(async () => render(<Preview {...props} />));
    expect(screen.getByRole("button", { name: "下一条未检查" })).toBeDisabled();
    preview = { ...preview, cues: [{ ...cue(0), review_status: "unchecked" }, { ...cue(1), target_text: "fresh target" }],
      manual_review: { ...preview.manual_review, revision: "r2" } };
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "刷新核对状态" })));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("fresh target");
    expect(screen.getByRole("button", { name: "下一条未检查" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "下一条未检查" }));
    expect(screen.getByLabelText("译文（中文）")).toHaveValue("target 0");
  });
});
