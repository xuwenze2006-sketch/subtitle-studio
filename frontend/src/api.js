// The launch token and entered credentials live only in memory. The server owns
// the HttpOnly session cookie and encrypted credential storage.
function downloadName(disposition, fallback) {
  const encoded = disposition?.match(/(?:^|;)\s*filename\*\s*=\s*UTF-8'[^']*'([^;]+)/i)?.[1];
  if (!encoded) return fallback;
  try {
    const name = decodeURIComponent(encoded.trim());
    return name && name !== "." && name !== ".." && !/[<>:"/\\|?*\x00-\x1f\x7f]/.test(name) ? name : fallback;
  } catch {
    return fallback;
  }
}

export function createApi() {
  const token =
    new URLSearchParams(window.location.hash.slice(1)).get("token") || "";
  let session;

  async function request(path, body, options = {}) {
    const { blob = false, signal } = options;
    const endpoint = path.split("?")[0];
    const timedRead = body === undefined && !blob && ["/api/state", "/api/preview", "/api/environment", "/api/progress"].includes(endpoint);
    // Session setup only establishes a local cookie. Other POSTs can already
    // have saved work or started a paid task and must keep their receipt alive.
    const timedSession = body !== undefined && !blob && endpoint === "/api/session";
    if (!timedRead && !timedSession) return sendRequest(path, body, options);
    const abortReason = () => signal?.reason ?? new DOMException("The operation was aborted.", "AbortError");
    if (signal?.aborted) throw abortReason();

    const controller = new AbortController();
    let rejectRead;
    const interrupted = new Promise((_, reject) => { rejectRead = reject; });
    const interrupt = error => {
      // Reject independently: transport cancellation may not settle a stalled read.
      rejectRead(error);
      controller.abort(error);
    };
    const onAbort = () => interrupt(abortReason());
    signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => interrupt(new Error(timedSession
      ? "连接本地服务超时，请确认字幕工坊仍在运行，然后重新连接。"
      : "读取本地服务数据超时，请稍后重试。")), endpoint==='/api/environment' ? 30000 : 15000);
    try {
      return await Promise.race([
        sendRequest(path, body, { ...options, signal: controller.signal }),
        interrupted,
      ]);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }
  }

  async function sendRequest(path, body, { blob = false, downloadMetadata = false, signal } = {}) {
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin",
        cache: "no-store",
        signal,
        headers: {
          "Content-Type": "application/json",
          "X-Subtitle-Token": token,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new Error(
        "无法连接本地服务。请确认字幕工坊仍在运行，然后重新连接。",
      );
    }
    if (!response.ok) {
      let detail;
      try {
        detail = await response.json();
      } catch (error) {
        if (error?.name === "AbortError") throw error;
        /* Use the status fallback. */
      }
      let message =
        typeof detail?.error === "string"
          ? detail.error
          : `操作未完成（HTTP ${response.status}），请稍后重试。`;
      if (body?.key) message = message.split(body.key).join("[已隐藏]");
      throw new Error(message);
    }
    try {
      const data = await (blob ? response.blob() : response.json());
      return blob && downloadMetadata ? { data, disposition: response.headers.get("Content-Disposition") } : data;
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new Error(error?.name === "SyntaxError"
        ? "本地服务返回的数据格式不正确，请刷新后重试。"
        : "本地服务响应读取中断，请确认字幕工坊仍在运行，然后重试。");
    }
  }

  return {
    request,
    connect() {
      if (!session) {
        session = request("/api/session", {})
          .then(() => {
            if (token)
              window.history.replaceState(
                null,
                "",
                window.location.pathname + window.location.search,
              );
          })
          .catch((error) => {
            session = undefined;
            throw error;
          });
      }
      return session;
    },
    async download(name, sample, project) {
      const query = new URLSearchParams({ name });
      if (sample) query.set("sample", sample);
      if (project) query.set("project", project);
      const { data, disposition } = await request(
        `/api/download?${query}`,
        undefined,
        { blob: true, downloadMetadata: true },
      );
      const filename = downloadName(disposition, name);
      const url = URL.createObjectURL(data);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      return filename;
    },
  };
}
