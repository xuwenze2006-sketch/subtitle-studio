// A layout owns the editor; the shared preview session owns an in-flight write.
// Weak ownership releases these records when the app session is discarded.
const sessions = new WeakMap();

export function hasPendingReviewSubmissions(owner) {
  for (const store of sessions.get(owner)?.values() || []) {
    if (store.hasPending()) return true;
  }
  return false;
}

export function getReviewSubmissions(owner, projectKey) {
  if (!sessions.has(owner)) sessions.set(owner, new Map());
  const projects = sessions.get(owner);
  if (projects.has(projectKey)) return projects.get(projectKey);
  const operations = new Map(), listeners = new Set();
  let version = 0;
  const publish = () => { version += 1; listeners.forEach(listener => listener()); };
  const store = {
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    getSnapshot: () => version,
    pending: sample => operations.get(sample)?.status === "pending",
    hasPending: () => [...operations.values()].some(operation => operation.status === "pending"),
    peekResult: sample => {
      const operation=operations.get(sample);
      return operation?.status !== 'pending' ? operation : null;
    },
    submit(sample, draft, request) {
      if (store.pending(sample)) return false;
      const operation = { sample, cueId: draft.cueId, revision: draft.revision, fields: { ...draft.fields }, status: "pending" };
      operations.set(sample, operation);
      publish();
      // Keep both success and failure even when no layout is mounted.
      Promise.resolve().then(request).then(data => {
        operation.status = "success";
        operation.data = data;
      }, error => {
        operation.status = "error";
        operation.error = error;
      }).then(publish);
      return true;
    },
    takeResult(sample) {
      const operation = operations.get(sample);
      if (!operation || operation.status === "pending") return null;
      operations.delete(sample);
      publish();
      return operation;
    },
  };
  projects.set(projectKey, store);
  return store;
}

export const matchesSubmittedDraft = (draft, submission) => !!draft && draft.sample === submission.sample &&
  draft.cueId === submission.cueId && draft.revision === submission.revision;

export const matchesSubmittedFields = (draft, submission) => matchesSubmittedDraft(draft, submission) &&
  Object.keys(submission.fields).every(key => draft.fields[key] === submission.fields[key]);
