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
  const videoTargets = new Map();

  async function request(path, body, options = {}) {
    const { blob = false, head = false, signal } = options;
    const endpoint = path.split("?")[0];
    const timedRead = head || (body === undefined && !blob && ["/api/state", "/api/preview", "/api/environment", "/api/progress"].includes(endpoint));
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

  async function sendRequest(path, body, { blob = false, head = false, downloadMetadata = false, signal } = {}) {
    if (body === undefined && !blob && !head && ["/api/state", "/api/preview"].includes(path.split("?")[0])) {
      // Keep the public request path stable for component mocks. Only the
      // transport selects lightweight reads; MP4 proofs have their own lifetime.
      const [endpoint, query] = path.split("?");
      const params = new URLSearchParams(query);
      params.set("include_video", "false");
      path = `${endpoint}?${params}`;
    }
    let response;
    try {
      response = await fetch(path, {
        method: head ? "HEAD" : body === undefined ? "GET" : "POST",
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
      if (head) throw new Error({
        403: "下载会话失效，请重新连接本地服务后再试。",
        409: "项目或成片版本已变化，请刷新结果后再下载。",
        410: "成片已失效或缺失，请刷新结果并重新导出 MP4。",
        503: "本地服务正在退出，请重新启动后下载。",
      }[response.status] || `无法准备 MP4 下载（HTTP ${response.status}），请刷新结果后重试。`);
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
    if (head) return response.headers;
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
    async downloadVideo(kind, project, video, { signal, onError } = {}) {
      if (!["video", "draft-video"].includes(kind) || !project || !/^[a-f0-9]{64}$/.test(video?.artifact_id || "")) {
        throw new Error("缺少当前成片信息，请刷新结果后再下载。");
      }
      // Re-establish the cookie even if an earlier shared session promise has
      // resolved: a native navigation cannot send our in-memory launch header.
      await request("/api/session", {}, { signal });
      const query = new URLSearchParams({ name: kind, project, artifact: video.artifact_id });
      const path = `/api/download?${query}`;
      const metadata = await request(path, undefined, { head: true, signal });
      const disposition = metadata.get("Content-Disposition");
      const filename = downloadName(disposition, "");
      if (metadata.get("X-Subtitle-Artifact") !== video.artifact_id ||
          metadata.get("Content-Type")?.split(";")[0] !== "video/mp4" ||
          !/^[1-9]\d*$/.test(metadata.get("Content-Length") || "") ||
          !/^attachment\s*;/i.test(disposition || "") || !filename.toLowerCase().endsWith(".mp4")) {
        throw new Error("成片下载信息不完整或已变化，请刷新结果后重试。");
      }
      if (signal?.aborted) throw signal.reason ?? new DOMException("Aborted", "AbortError");
      // Attachment navigation is owned by the browser's download manager. No
      // video body, Blob or object URL crosses into the application heap.
      // Keep bounded targets alive; a time limit must not cancel a large file.
      const previous = videoTargets.get(kind);
      previous?.remove();
      const frame = document.createElement("iframe");
      frame.hidden = true;
      frame.title = "MP4 下载";
      frame.onload = () => {
        if (videoTargets.get(kind) !== frame || signal?.aborted) return;
        try {
          const doc = frame.contentDocument;
          if (!doc || doc.URL === "about:blank") return;
          // Only errors render a document; successful attachments never do.
          // Reading this small JSON does not read the MP4 response body.
          const text = doc.body?.textContent || "";
          const detail = text.length <= 8192 ? JSON.parse(text) : null;
          onError?.(new Error(typeof detail?.error === "string" ? detail.error : "下载未能开始，请刷新结果后重试。"));
        } catch {
          onError?.(new Error("下载未能开始，请确认本地服务仍在运行后重试。"));
        } finally {
          if (frame.contentDocument?.URL !== "about:blank") {
            frame.remove();
            videoTargets.delete(kind);
          }
        }
      };
      videoTargets.set(kind, frame);
      frame.src = path;
      document.body.appendChild(frame);
      return filename;
    },
  };
}
