import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApi } from "./api";

describe("字幕文件下载名", () => {
  async function download(disposition) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("subtitle", {
      headers: disposition ? { "Content-Disposition": disposition } : {},
    })));
    URL.createObjectURL = vi.fn().mockReturnValue("blob:subtitle");
    URL.revokeObjectURL = vi.fn();
    let chosenName;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () {
      chosenName = this.download;
    });
    const result = await createApi().download("原文.srt", "sample-2", "project-a");
    const query = new URL(fetch.mock.calls[0][0], "http://localhost").searchParams;
    expect(query.get("sample")).toBe("sample-2");
    expect(query.get("project")).toBe("project-a");
    return { result, chosenName };
  }

  it("uses the server's UTF-8 timestamped filename in the browser and returns it", async () => {
    const name = "影片_样片2_日语_20261003_203500.srt";
    const result = await download(`attachment; filename*=UTF-8''${encodeURIComponent(name)}`);
    expect(result).toEqual({ result: name, chosenName: name });
  });

  it.each([
    undefined,
    "attachment; filename*=UTF-8''broken%ZZ.srt",
    "attachment; filename*=UTF-8''..%2Funsafe.srt",
    "attachment; filename*=UTF-8''unsafe%0Aname.srt",
  ])("falls back to the known subtitle name for missing or unsafe metadata", async (header) => {
    expect(await download(header)).toEqual({ result: "原文.srt", chosenName: "原文.srt" });
  });
});

function interruptedResponse(error, status = 200) {
  return new Response(new ReadableStream({ start(controller) { controller.error(error); } }), { status });
}

describe("本地服务响应正文异常", () => {
  it("explains an interrupted JSON body without exposing the transport error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(interruptedResponse(new TypeError("terminated: private-response-content"))));
    const error = await createApi().request("/api/preview").catch(failure => failure);

    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/响应.*中断/);
    expect(error.message).not.toMatch(/格式|terminated|private-response-content/);
  });

  it("does not create a download or object URL when its body is interrupted", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(interruptedResponse(new TypeError("terminated"))));
    URL.createObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const error = await createApi().download("原文.srt", "main", "project-a").catch(failure => failure);

    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/响应.*中断/);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
    expect(document.querySelector("a[download]")).toBeNull();
  });

  it("distinguishes malformed JSON from a disconnected response without exposing its content", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"private-response-content":')));
    const error = await createApi().request("/api/state").catch(failure => failure);

    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/数据格式.*不正确/);
    expect(error.message).not.toMatch(/中断|private-response-content/);
  });

  it.each(["fetch", "json-body", "blob-body", "http-error-body"])("preserves cancellation from %s", async stage => {
    const aborted = new DOMException("The operation was aborted.", "AbortError");
    const fetch = vi.fn();
    if (stage === "fetch") fetch.mockRejectedValue(aborted);
    else fetch.mockResolvedValue(interruptedResponse(aborted, stage === "http-error-body" ? 503 : 200));
    vi.stubGlobal("fetch", fetch);

    await expect(createApi().request("/api/preview", undefined, { blob: stage === "blob-body" })).rejects.toBe(aborted);
  });

  it("preserves useful HTTP error details while masking every occurrence of the entered key", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "凭据 test-private-key 无效：test-private-key" }), { status: 403 })));

    await expect(createApi().request("/api/credentials", { key: "test-private-key" })).rejects.toThrow("凭据 [已隐藏] 无效：[已隐藏]");
  });

  it.each([
    ["malformed JSON", () => new Response("not-json-private-content", { status: 503 })],
    ["non-string error detail", () => new Response(JSON.stringify({ error: { private: "hidden" } }), { status: 503 })],
    ["interrupted error body", () => interruptedResponse(new TypeError("terminated: private-content"), 503)],
  ])("keeps the HTTP status fallback for %s", async (_, response) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response()));

    await expect(createApi().request("/api/state")).rejects.toThrow("操作未完成（HTTP 503），请稍后重试。");
  });
});

describe("本地状态与预览读取截止时间", () => {
  const pending = () => new Promise(() => {});
  const observe = promise => {
    const result = { settled: false };
    promise.then(value => { Object.assign(result, { settled: true, value }); }, error => { Object.assign(result, { settled: true, error }); });
    return result;
  };
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each([
    ["/api/state", "headers"], ["/api/state?project=a", "headers"],
    ["/api/preview", "headers"], ["/api/preview?sample=main&project=a", "headers"],
    ["/api/state", "body"], ["/api/preview?sample=main&project=a", "body"],
    ["/api/state", "http-error-body"],
  ])("rejects %s after 15 seconds when %s hangs, even if transport ignores abort", async (path, stage) => {
    const caller = new AbortController();
    const removed = vi.spyOn(caller.signal, "removeEventListener");
    vi.stubGlobal("fetch", vi.fn(() => stage === "headers" ? pending()
      : Promise.resolve(new Response(new ReadableStream({ start() {} }), { status: stage === "http-error-body" ? 503 : 200 }))));
    const result = observe(createApi().request(path, undefined, { signal: caller.signal }));
    await vi.advanceTimersByTimeAsync(14999);
    expect(result.settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);

    expect(result.settled).toBe(true);
    expect(result.error?.message).toMatch(/本地.*读取.*超时|读取.*本地.*超时/);
    expect(result.error?.message).not.toMatch(/操作未完成|未提交|无法连接|响应读取中断/);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    expect(caller.signal.aborted).toBe(false);
    expect(removed).toHaveBeenCalledWith("abort", expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("uses one deadline across response headers and JSON body", async () => {
    let headers;
    vi.stubGlobal("fetch", vi.fn(() => new Promise(resolve => { headers = resolve; })));
    const result = observe(createApi().request("/api/state"));
    await vi.advanceTimersByTimeAsync(10000);
    headers(new Response(new ReadableStream({ start() {} })));
    await vi.advanceTimersByTimeAsync(5000);
    expect(result.error?.message).toMatch(/超时/);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  });

  it.each(["success", "network-error", "malformed-json", "http-error"])("cleans the deadline and caller listener after %s", async outcome => {
    const caller = new AbortController();
    const removed = vi.spyOn(caller.signal, "removeEventListener");
    vi.stubGlobal("fetch", vi.fn(() => outcome === "network-error" ? Promise.reject(new TypeError("failed"))
      : Promise.resolve(new Response(outcome === "malformed-json" ? "{" : JSON.stringify({ ok: true }), { status: outcome === "http-error" ? 503 : 200 }))));
    const result = observe(createApi().request("/api/state", undefined, { signal: caller.signal }));
    await vi.advanceTimersByTimeAsync(0);
    expect(result.settled).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(removed).toHaveBeenCalledWith("abort", expect.any(Function));
    caller.abort();
    await vi.advanceTimersByTimeAsync(15000);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(false);
    if (outcome === "success") expect(result.value).toEqual({ ok: true });
    else expect(result.error?.message).not.toMatch(/超时/);
  });

  it.each(["before-request", "headers", "body"])("preserves caller AbortError during %s and removes its deadline/listener", async stage => {
    const caller = new AbortController();
    const aborted = new DOMException("caller cancelled", "AbortError");
    const added = vi.spyOn(caller.signal, "addEventListener");
    const removed = vi.spyOn(caller.signal, "removeEventListener");
    vi.stubGlobal("fetch", vi.fn(() => stage === "body" ? Promise.resolve(new Response(new ReadableStream({ start() {} }))) : pending()));
    if (stage === "before-request") caller.abort(aborted);
    const result = observe(createApi().request("/api/preview", undefined, { signal: caller.signal }));
    await vi.advanceTimersByTimeAsync(1);
    if (stage !== "before-request") caller.abort(aborted);
    await vi.advanceTimersByTimeAsync(0);

    expect(result.error).toBe(aborted);
    expect(vi.getTimerCount()).toBe(0);
    if (stage === "before-request") {
      expect(fetch).not.toHaveBeenCalled();
      expect(added).not.toHaveBeenCalled();
    } else {
      expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
      expect(removed).toHaveBeenCalledWith("abort", added.mock.calls[0][1]);
    }
    await vi.advanceTimersByTimeAsync(15000);
    expect(result.error).toBe(aborted);
  });

  it.each([
    ["/api/state", {}], ["/api/preview?project=a", {}], ["/api/run", { action: "full" }],
    ["/api/state", undefined, true], ["/api/preview?project=a", undefined, true],
    ["/api/download?name=video.mp4", undefined, true], ["/api/other-read", undefined],
  ])("does not time out or retry excluded request %s (body %j, blob %s)", async (path, body, blob = false) => {
    const caller = new AbortController();
    vi.stubGlobal("fetch", vi.fn(pending));
    const result = observe(createApi().request(path, body, { blob, signal: caller.signal }));
    await vi.advanceTimersByTimeAsync(60000);
    expect(result.settled).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].signal).toBe(caller.signal);
    expect(caller.signal.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("首次连接服务的恢复", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    window.history.replaceState(null, "", "/");
  });

  it.each(["headers", "body"])("allows explicit reconnect when session %s never arrives", async stage => {
    window.history.replaceState(null, "", "/#token=local-test-token");
    let resolveHeaders;
    const fetch = vi.fn(() => stage === "headers"
      ? new Promise(resolve => { resolveHeaders = resolve; })
      : Promise.resolve(new Response(new ReadableStream({ start() {} }))));
    vi.stubGlobal("fetch", fetch);
    const api = createApi();
    const first = api.connect();
    expect(api.connect()).toBe(first);
    let failure;
    first.catch(error => { failure = error; });
    await vi.advanceTimersByTimeAsync(15000);

    expect(failure?.message).toMatch(/连接.*超时/);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    expect(window.location.hash).toBe("#token=local-test-token");
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetch).toHaveBeenCalledTimes(1);

    // A late old receipt cannot consume the entry token or auto-connect.
    resolveHeaders?.(new Response(JSON.stringify({ connected: true })));
    await vi.advanceTimersByTimeAsync(0);
    expect(window.location.hash).toBe("#token=local-test-token");
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ connected: true })));
    await api.connect();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(window.location.hash).toBe("");
    expect(vi.getTimerCount()).toBe(0);
  });
});
