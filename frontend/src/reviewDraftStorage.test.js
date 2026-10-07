import { beforeEach, describe, expect, it, vi } from "vitest";
import { createReviewDraftStorage, REVIEW_DRAFT_STORAGE_KEY } from "./reviewDraftStorage";

const project = '["film.mp4","campaign","project-1"]';
const original = {
  start: "00:00:01.000", end: "00:00:02.500", source_text: "Hello", target_text: "你好",
  note: "", review_status: "unchecked", translation_confirmed: false,
};
const draft = (changes = {}) => ({
  cueId: 1, sample: "main", revision: "r1", initial: { ...original },
  fields: { ...original, target_text: "您好" }, ...changes,
});

beforeEach(() => localStorage.clear());

describe("local review draft recovery", () => {
  it("recovers unsaved fields and the original revision after a new storage session", () => {
    const first = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    const input = draft();
    expect(first.write(project, input).ok).toBe(true);
    input.fields.target_text = "之后的输入";
    const recovered = createReviewDraftStorage({ storage: localStorage, now: () => 2000 }).read(project, "main");
    expect(recovered).toEqual({
      ok: true, error: null, savedAt: 1000,
      draft: { cueId: 1, sample: "main", revision: "r1", initial: original,
        fields: { ...original, target_text: "您好" } },
    });
    recovered.draft.fields.target_text = "恢复后继续输入";
    expect(first.read(project, "main").draft.fields.target_text).toBe("您好");
  });

  it("isolates projects and segments, including names that would collide with delimiter keys", () => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    store.write(project, draft());
    store.write(project, draft({ sample: "sample-1", cueId: 2, revision: "r2" }));
    store.write("another-project", draft({ cueId: 3 }));
    store.write("a|b", draft({ sample: "c", cueId: 4 }));
    store.write("a", draft({ sample: "b|c", cueId: 5 }));
    expect(store.read(project, "main").draft.cueId).toBe(1);
    expect(store.read(project, "sample-1").draft.cueId).toBe(2);
    expect(store.read("another-project", "main").draft.cueId).toBe(3);
    expect(store.read("a|b", "c").draft.cueId).toBe(4);
    expect(store.read("a", "b|c").draft.cueId).toBe(5);
    expect(store.read("missing", "main")).toEqual({ ok: true, error: null, draft: null, savedAt: null });
  });

  it("clears only an explicitly saved or discarded segment and removes the empty container", () => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    store.write(project, draft());
    store.write(project, draft({ sample: "sample-1", cueId: 2 }));
    expect(store.clear(project, "main")).toEqual({ ok: true, error: null });
    expect(store.read(project, "main").draft).toBeNull();
    expect(store.read(project, "sample-1").draft.cueId).toBe(2);
    expect(store.clear(project, "sample-1").ok).toBe(true);
    expect(localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY)).toBeNull();
  });

  it("persists only allowed review fields and excludes credentials and unrelated state", () => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    store.write(project, draft({ token: "secret-token", apiKey: "secret-key", playbackTime: 50,
      fields: { ...original, note: "校对备注", token: "secret-field-token" },
      initial: { ...original, api_key: "secret-original-key" } }));
    const raw = localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY);
    expect(raw).not.toContain("secret-");
    expect(raw).not.toContain("playbackTime");
    const recovered = store.read(project, "main").draft;
    expect(recovered.initial).toEqual(original);
    expect(recovered.fields).toEqual({ ...original, note: "校对备注" });
  });

  it.each([
    null,
    draft({ revision: "" }),
    draft({ cueId: 0 }),
    draft({ sample: "" }),
    draft({ fields: { ...original, target_text: { text: "bad" } } }),
    draft({ initial: { ...original, translation_confirmed: "false" } }),
    draft({ fields: { ...original, review_status: "submitted" } }),
  ])("rejects malformed drafts without clearing prior unsaved work: %#", input => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    store.write(project, draft());
    expect(store.write(project, input)).toEqual({ ok: false, error: "invalid-draft" });
    expect(store.read(project, "main").draft.fields.target_text).toBe("您好");
  });

  it("expires a draft at the retention boundary while preserving a newer segment", () => {
    let time = 1000;
    const store = createReviewDraftStorage({ storage: localStorage, now: () => time, retentionMs: 1000 });
    store.write(project, draft());
    time = 1999;
    expect(store.read(project, "main").draft.revision).toBe("r1");
    time = 2000;
    expect(store.read(project, "main").draft).toBeNull();
    expect(localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY)).toBeNull();
    time = 2500;
    store.write(project, draft());
    time = 3000;
    store.write(project, draft({ sample: "sample-1" }));
    time = 3500;
    expect(store.read(project, "main").draft).toBeNull();
    expect(store.read(project, "sample-1").draft.revision).toBe("r1");
    expect(JSON.parse(localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY)).entries).toHaveLength(1);
  });

  it("evicts the oldest segment at the count limit and treats updates as replacements", () => {
    let time = 1000;
    const store = createReviewDraftStorage({ storage: localStorage, now: () => time, maxEntries: 2 });
    store.write(project, draft());
    time = 2000;
    store.write(project, draft({ sample: "sample-1" }));
    time = 3000;
    store.write(project, draft({ revision: "r3" }));
    time = 4000;
    store.write(project, draft({ sample: "sample-2" }));
    expect(store.read(project, "main").draft.revision).toBe("r3");
    expect(store.read(project, "sample-1").draft).toBeNull();
    expect(store.read(project, "sample-2").draft.cueId).toBe(1);
  });

  it("keeps the stored payload within its size budget and retains the newest draft", () => {
    let time = 1000;
    const store = createReviewDraftStorage({ storage: localStorage, now: () => time, maxBytes: 2600 });
    expect(store.write(project, draft()).ok).toBe(true);
    time = 2000;
    expect(store.write(project, draft({ sample: "sample-1" })).ok).toBe(true);
    expect(store.read(project, "main").draft.fields.target_text).toBe("您好");
    time = 3000;
    expect(store.write(project, draft({ sample: "sample-2" })).ok).toBe(true);
    expect(store.read(project, "main").draft).toBeNull();
    expect(store.read(project, "sample-1").draft.fields.target_text).toBe("您好");
    expect(store.read(project, "sample-2").draft.fields.target_text).toBe("您好");
    expect(localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY).length * 2).toBeLessThanOrEqual(2600);
  });

  it("rejects a single oversized draft without evicting existing recoverable work", () => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000, maxBytes: 1800 });
    store.write(project, draft());
    expect(store.write(project, draft({ fields: { ...original, note: "长".repeat(2000) } })))
      .toEqual({ ok: false, error: "capacity-exceeded" });
    expect(store.read(project, "main").draft.fields.target_text).toBe("您好");
  });

  it("reports corrupted JSON and can persist a subsequent valid draft", () => {
    localStorage.setItem(REVIEW_DRAFT_STORAGE_KEY, "{broken");
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    expect(store.read(project, "main")).toEqual({ ok: false, error: "invalid-data", draft: null, savedAt: null });
    expect(store.write(project, draft()).ok).toBe(true);
    expect(store.read(project, "main").draft.fields.target_text).toBe("您好");
  });

  it("filters malformed records while recovering intact records", () => {
    localStorage.setItem(REVIEW_DRAFT_STORAGE_KEY, JSON.stringify({ version: 1, entries: [
      { projectKey: project, sample: "main", cueId: 1, revision: "r1", savedAt: 1000,
        fields: { ...original, target_text: "您好" }, original },
      { projectKey: project, sample: "sample-1", cueId: 2, revision: "r1", savedAt: 1000,
        fields: { ...original, note: 42 }, original },
    ] }));
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 2000 });
    expect(store.read(project, "main").draft.fields.target_text).toBe("您好");
    expect(store.read(project, "sample-1").draft).toBeNull();
    expect(JSON.parse(localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY)).entries).toHaveLength(1);
  });

  it("preserves unsupported storage versions rather than rewriting unknown data", () => {
    const future = '{"version":99,"entries":[{"future":"work"}]}';
    localStorage.setItem(REVIEW_DRAFT_STORAGE_KEY, future);
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    expect(store.read(project, "main")).toEqual({ ok: false, error: "unsupported-version", draft: null, savedAt: null });
    expect(store.write(project, draft())).toEqual({ ok: false, error: "unsupported-version" });
    expect(store.clear(project, "main")).toEqual({ ok: false, error: "unsupported-version" });
    expect(localStorage.getItem(REVIEW_DRAFT_STORAGE_KEY)).toBe(future);
  });

  it("reports blocked storage reads without throwing into the editor", () => {
    const store = createReviewDraftStorage({ storage: { getItem() { throw new DOMException("Denied", "SecurityError"); } } });
    expect(store.read(project, "main")).toEqual({ ok: false, error: "storage-unavailable", draft: null, savedAt: null });
    expect(store.write(project, draft())).toEqual({ ok: false, error: "storage-unavailable" });
    expect(store.clear(project, "main")).toEqual({ ok: false, error: "storage-unavailable" });
  });

  it("reports write quota failures and preserves the previous persisted draft", () => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    store.write(project, draft());
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Full", "QuotaExceededError"); });
    expect(store.write(project, draft({ revision: "r2" }))).toEqual({ ok: false, error: "storage-unavailable" });
    expect(store.read(project, "main").draft.revision).toBe("r1");
  });

  it("reports failed explicit removal and leaves the recoverable draft in storage", () => {
    const store = createReviewDraftStorage({ storage: localStorage, now: () => 1000 });
    store.write(project, draft());
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new DOMException("Denied", "SecurityError"); });
    expect(store.clear(project, "main")).toEqual({ ok: false, error: "storage-unavailable" });
    expect(store.read(project, "main").draft.revision).toBe("r1");
  });

  it("protects another window's changed draft against stale writes and removals", () => {
    let time = 1000;
    const first = createReviewDraftStorage({ storage: localStorage, now: () => time });
    const second = createReviewDraftStorage({ storage: localStorage, now: () => time });
    first.write(project, draft());
    second.read(project, "main");
    time = 2000;
    const later = draft({ fields: { ...original, target_text: "另一个窗口输入" } });
    expect(second.write(project, later).ok).toBe(true);
    expect(first.write(project, draft({ fields: { ...original, target_text: "旧窗口继续输入" } })))
      .toEqual({ ok: false, error: "draft-conflict" });
    expect(first.clear(project, "main")).toEqual({ ok: false, error: "draft-conflict" });
    expect(second.read(project, "main").draft.fields.target_text).toBe("另一个窗口输入");
    expect(second.clear(project, "main").ok).toBe(true);
    expect(first.write(project, draft()).ok).toBe(true);
  });

  it("requires a fresh window to read an existing draft before replacing its contents", () => {
    let time = 1000;
    const first = createReviewDraftStorage({ storage: localStorage, now: () => time });
    const second = createReviewDraftStorage({ storage: localStorage, now: () => time });
    first.write(project, draft());
    time = 2000;
    const input = draft({ fields: { ...original, target_text: "刷新后的新输入" } });
    expect(second.write(project, input)).toEqual({ ok: false, error: "draft-conflict" });
    second.read(project, "main");
    expect(second.write(project, input).ok).toBe(true);
    expect(second.read(project, "main").draft.fields.target_text).toBe("刷新后的新输入");
  });

  it("can inspect persisted content without accepting another window's new baseline", () => {
    let time = 1000;
    const first = createReviewDraftStorage({ storage: localStorage, now: () => time });
    const second = createReviewDraftStorage({ storage: localStorage, now: () => time });
    first.write(project, draft());
    second.read(project, "main");
    time = 2000;
    second.write(project, draft({ fields: { ...original, target_text: "另一窗口的草稿" } }));
    expect(first.read(project, "main", { observe: false }).draft.fields.target_text).toBe("另一窗口的草稿");
    expect(first.clear(project, "main")).toEqual({ ok: false, error: "draft-conflict" });
  });
});
