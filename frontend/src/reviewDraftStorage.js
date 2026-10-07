export const REVIEW_DRAFT_STORAGE_KEY = "subtitle-studio:review-drafts:v1";

const VERSION = 1;
const DEFAULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const DEFAULT_MAX_ENTRIES = 30;
const DEFAULT_MAX_BYTES = 1024 * 1024;
const textFields = ["start", "end", "source_text", "target_text", "note", "review_status"];
const statuses = new Set(["unchecked", "checked", "issue"]);
const object = value => !!value && typeof value === "object" && !Array.isArray(value);
const identity = value => typeof value === "string" && value.length > 0;
const cueIdentity = value => (Number.isSafeInteger(value) && value > 0) || identity(value);
const validTime = value => Number.isSafeInteger(value) && value >= 0;
const positiveLimit = (value, fallback) => Number.isSafeInteger(value) && value > 0 ? value : fallback;
const result = error => ({ ok: !error, error: error || null });
const emptyRead = error => ({ ...result(error), draft: null, savedAt: null });
const encode = entries => JSON.stringify({ version: VERSION, entries });

function reviewFields(value) {
  if (!object(value) || textFields.some(key => typeof value[key] !== "string") ||
      !statuses.has(value.review_status) || typeof value.translation_confirmed !== "boolean") return null;
  return { ...Object.fromEntries(textFields.map(key => [key, value[key]])), translation_confirmed: value.translation_confirmed };
}

function reviewRecord(projectKey, draft, savedAt) {
  if (!identity(projectKey) || !object(draft) || !identity(draft.sample) || !cueIdentity(draft.cueId) ||
      !identity(draft.revision) || !validTime(savedAt)) return null;
  const fields = reviewFields(draft.fields), original = reviewFields(draft.initial);
  return fields && original ? {
    projectKey, sample: draft.sample, cueId: draft.cueId, revision: draft.revision, fields, original, savedAt,
  } : null;
}

// localStorage survives a browser restart. Keep a small, expiring collection of
// review text only; never serialize the enclosing preview, connection or app.
export function createReviewDraftStorage({ storage, now = Date.now, retentionMs, maxEntries, maxBytes } = {}) {
  const retention = positiveLimit(retentionMs, DEFAULT_RETENTION_MS);
  const countLimit = positiveLimit(maxEntries, DEFAULT_MAX_ENTRIES);
  // Browser storage accounts for UTF-16 code units; this is a conservative
  // payload budget independent of the browser's remaining shared quota.
  const byteLimit = positiveLimit(maxBytes, DEFAULT_MAX_BYTES);
  const resolveStorage = () => storage === undefined ? globalThis.localStorage : storage;
  const matches = (entry, projectKey, sample) => entry.projectKey === projectKey && entry.sample === sample;
  const scopeKey = (projectKey, sample) => JSON.stringify([projectKey, sample]);
  const fingerprint = record => record ? JSON.stringify([record.cueId, record.revision, record.fields, record.original]) : null;
  const observed = new Map();
  const remember = (key, record) => {
    observed.delete(key);
    observed.set(key, fingerprint(record));
    while (observed.size > countLimit) observed.delete(observed.keys().next().value);
  };
  const bounded = entries => {
    const latest = new Map();
    for (const entry of entries) {
      const key = JSON.stringify([entry.projectKey, entry.sample]);
      if (!latest.has(key) || latest.get(key).savedAt <= entry.savedAt) latest.set(key, entry);
    }
    const kept = [...latest.values()].sort((a, b) => a.savedAt - b.savedAt).slice(-countLimit);
    while (kept.length && encode(kept).length * 2 > byteLimit) kept.shift();
    return kept;
  };
  const load = (target, time) => {
    const raw = target.getItem(REVIEW_DRAFT_STORAGE_KEY);
    if (raw === null) return { entries: [], raw, error: null };
    let value;
    try { value = JSON.parse(raw); }
    catch { return { entries: [], raw, error: "invalid-data" }; }
    if (!object(value) || !Number.isSafeInteger(value.version)) return { entries: [], raw, error: "invalid-data" };
    if (value.version !== VERSION) return { entries: [], raw, error: "unsupported-version" };
    if (!Array.isArray(value.entries)) return { entries: [], raw, error: "invalid-data" };
    const entries = value.entries.map(entry => object(entry)
      ? reviewRecord(entry.projectKey, { ...entry, initial: entry.original }, entry.savedAt) : null)
      .filter(entry => entry && entry.savedAt <= time && time - entry.savedAt < retention);
    return { entries: bounded(entries), raw, error: null };
  };
  const persist = (target, entries) => {
    if (entries.length) target.setItem(REVIEW_DRAFT_STORAGE_KEY, encode(entries));
    else target.removeItem(REVIEW_DRAFT_STORAGE_KEY);
  };

  return {
    read(projectKey, sample, { observe = true } = {}) {
      try {
        const target = resolveStorage(), time = now();
        if (!validTime(time)) return emptyRead("storage-unavailable");
        const loaded = load(target, time);
        if (loaded.error) return emptyRead(loaded.error);
        const record = loaded.entries.find(entry => matches(entry, projectKey, sample));
        if (observe) remember(scopeKey(projectKey, sample), record);
        const recovered = { ok: true, error: null, savedAt: record?.savedAt ?? null,
          draft: record ? { cueId: record.cueId, sample: record.sample, revision: record.revision,
            initial: record.original, fields: record.fields } : null };
        // Expired, invalid and over-budget records should leave browser storage.
        // A failed cleanup must still return any intact draft that was read.
        if (loaded.raw !== null && loaded.raw !== encode(loaded.entries)) {
          try { persist(target, loaded.entries); }
          catch { return { ...recovered, ok: false, error: "storage-unavailable" }; }
        }
        return recovered;
      } catch { return emptyRead("storage-unavailable"); }
    },
    write(projectKey, draft) {
      try {
        const record = reviewRecord(projectKey, draft, now());
        if (!record) return result("invalid-draft");
        if (encode([record]).length * 2 > byteLimit) return result("capacity-exceeded");
        const target = resolveStorage(), loaded = load(target, record.savedAt);
        if (loaded.error === "unsupported-version") return result(loaded.error);
        const key = scopeKey(projectKey, record.sample);
        const previous = loaded.entries.find(entry => matches(entry, projectKey, record.sample));
        // Each browser window owns its own observations. Refuse an overwrite
        // after another window changed the draft since our last read/write.
        if (previous && (observed.has(key) ? observed.get(key) !== fingerprint(previous)
          : fingerprint(record) !== fingerprint(previous))) return result("draft-conflict");
        // Invalid JSON is unrecoverable; a new valid edit can repair the store.
        const entries = loaded.entries.filter(entry => !matches(entry, projectKey, record.sample));
        persist(target, bounded([...entries, record]));
        remember(key, record);
        return result();
      } catch { return result("storage-unavailable"); }
    },
    clear(projectKey, sample) {
      try {
        const target = resolveStorage(), time = now();
        if (!validTime(time)) return result("storage-unavailable");
        const loaded = load(target, time);
        if (loaded.error) return result(loaded.error);
        const key = scopeKey(projectKey, sample);
        const previous = loaded.entries.find(entry => matches(entry, projectKey, sample));
        if (previous && (!observed.has(key) || observed.get(key) !== fingerprint(previous))) return result("draft-conflict");
        const entries = loaded.entries.filter(entry => !matches(entry, projectKey, sample));
        if (loaded.raw !== null && loaded.raw !== encode(entries)) persist(target, entries);
        remember(key, null);
        return result();
      } catch { return result("storage-unavailable"); }
    },
  };
}

const defaultStorage = createReviewDraftStorage();
export const readReviewDraft = (projectKey, sample, options) => defaultStorage.read(projectKey, sample, options);
export const writeReviewDraft = (projectKey, draft) => defaultStorage.write(projectKey, draft);
export const clearReviewDraft = (projectKey, sample) => defaultStorage.clear(projectKey, sample);
