// The app session owns a snapshot write and its last confirmed receipt, even
// while no preview is mounted. These records do not survive a browser reload.
const sessions = new WeakMap();

export function getSubtitleVersions(owner, projectKey) {
  if (!sessions.has(owner)) sessions.set(owner, new Map());
  const projects = sessions.get(owner);
  if (projects.has(projectKey)) return projects.get(projectKey);
  const operations = new Map(), listeners = new Set();
  let version = 0;
  const publish = () => { version += 1; listeners.forEach(listener => listener()); };
  const hasPending = () => [...operations.values()].some(operation => operation.pending);
  const preventPendingLoss = event => {
    if (!hasPending()) return;
    event.preventDefault();
    event.returnValue = "";
  };
  const store = {
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    getSnapshot: () => version,
    pending: sample => !!operations.get(sample)?.pending,
    receipt: sample => operations.get(sample)?.receipt,
    takeError(sample) {
      const operation = operations.get(sample);
      if (!operation?.error) return null;
      const error = operation.error;
      operation.error = null;
      publish();
      return error;
    },
    submit(sample, projectId, request) {
      if (store.pending(sample)) return false;
      const operation = { pending: true, receipt: store.receipt(sample), error: null };
      operations.set(sample, operation);
      window.addEventListener("beforeunload", preventPendingLoss);
      publish();
      // Do not cancel a write when its view disappears: the server may already
      // have published the version. Retain both outcomes for the next layout.
      Promise.resolve().then(request).then(data => {
        operation.receipt = { data, projectId };
      }, error => {
        operation.error = error;
      }).then(() => {
        operation.pending = false;
        if (!hasPending()) window.removeEventListener("beforeunload", preventPendingLoss);
        publish();
      });
      return true;
    },
  };
  projects.set(projectKey, store);
  return store;
}
