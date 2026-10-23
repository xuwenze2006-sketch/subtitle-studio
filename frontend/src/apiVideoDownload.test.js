import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApi } from "./api";

const artifact = "a".repeat(64);
const video = { name: "影片_原文草稿.mp4", artifact_id: artifact };
const headers = () => new Headers({
  "Content-Type": "video/mp4", "Content-Length": "5368709120",
  "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(video.name)}`,
  "X-Subtitle-Artifact": artifact,
});
let head;
beforeEach(() => {
  head = { ok: true, status: 200, headers: headers(), blob: vi.fn(), arrayBuffer: vi.fn(), json: vi.fn() };
  vi.stubGlobal("fetch", vi.fn().mockImplementation((_, options) => Promise.resolve(
    options.method === "HEAD" ? head : new Response("{}", { status: 200 }))));
  URL.createObjectURL = vi.fn();
});
afterEach(() => {
  document.querySelectorAll("iframe").forEach(frame => frame.remove());
  vi.useRealTimers();
});

describe("MP4 browser download handoff", () => {
  it.each(["draft-video", "video"])("hands %s to an authenticated native navigation without reading the body", async kind => {
    const result = await createApi().downloadVideo(kind, "project-a", video);
    expect(result).toBe(video.name);
    expect(fetch.mock.calls.map(([, options]) => options.method)).toEqual(["POST", "HEAD"]);
    const [path, options] = fetch.mock.calls[1];
    const query = new URL(path, location.href).searchParams;
    expect(Object.fromEntries(query)).toEqual({ name: kind, project: "project-a", artifact });
    expect(options.credentials).toBe("same-origin");
    expect(document.querySelector("iframe").getAttribute("src")).toBe(path);
    expect(document.querySelector("a[download]")).toBeNull();
    expect(head.blob).not.toHaveBeenCalled();
    expect(head.arrayBuffer).not.toHaveBeenCalled();
    expect(head.json).not.toHaveBeenCalled();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it.each([[403, /连接|会话/], [409, /变化|更新/], [410, /失效|重新导出/], [503, /退出|服务/]])("explains HEAD failure %s without creating a download", async (status, message) => {
    head.ok = false; head.status = status;
    await expect(createApi().downloadVideo("video", "project-a", video)).rejects.toThrow(message);
    expect(head.json).not.toHaveBeenCalled();
    expect(document.querySelector("iframe")).toBeNull();
  });

  it.each(["Content-Type", "Content-Disposition", "Content-Length", "X-Subtitle-Artifact"])("rejects missing %s before navigation", async name => {
    head.headers.delete(name);
    await expect(createApi().downloadVideo("video", "project-a", video)).rejects.toThrow(/下载|成片/);
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("does not trust a supplied URL or accept a mismatched output identity", async () => {
    head.headers.set("X-Subtitle-Artifact", "b".repeat(64));
    await expect(createApi().downloadVideo("video", "project-a", { ...video, download_url: "https://evil.example/" })).rejects.toThrow(/变化|下载|成片/);
    expect(fetch.mock.calls[1][0]).toMatch(/^\/api\/download\?/);
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("rejects missing identity without requests", async () => {
    await expect(createApi().downloadVideo("video", "", video)).rejects.toThrow(/刷新/);
    await expect(createApi().downloadVideo("video", "project-a", { name: video.name })).rejects.toThrow(/刷新/);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("stops a hung preflight after 15 seconds even when transport ignores abort", async () => {
    vi.useFakeTimers();
    fetch.mockImplementation((_, options) => options.method === "HEAD" ? new Promise(() => {}) : Promise.resolve(new Response("{}")));
    const promise = createApi().downloadVideo("video", "project-a", video);
    const check = expect(promise).rejects.toThrow(/超时/);
    await vi.advanceTimersByTimeAsync(15000);
    await check;
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("aborts a stale preflight without handing its result to the browser", async () => {
    let resolveHead;
    fetch.mockImplementation((_, options) => options.method === "HEAD" ? new Promise(resolve => { resolveHead = resolve; }) : Promise.resolve(new Response("{}")));
    const controller = new AbortController();
    const promise = createApi().downloadVideo("video", "project-a", video, { signal: controller.signal });
    const check = expect(promise).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(resolveHead).toBeTypeOf("function"));
    controller.abort(); resolveHead(head);
    await check;
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("keeps at most one native target per kind and does not time out large transfers", async () => {
    vi.useFakeTimers();
    const api = createApi();
    await api.downloadVideo("video", "project-a", video);
    await api.downloadVideo("video", "project-a", video);
    await api.downloadVideo("draft-video", "project-a", video);
    expect(document.querySelectorAll("iframe")).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(3600000);
    expect(document.querySelectorAll("iframe")).toHaveLength(2);
  });

  it("reports a JSON error if the file changes after HEAD and keeps the application page", async () => {
    const onError = vi.fn();
    await createApi().downloadVideo("video", "project-a", video, { onError });
    const frame = document.querySelector("iframe");
    Object.defineProperty(frame, "contentDocument", { value: { URL: frame.src,
      body: { textContent: JSON.stringify({ error: "成片版本已变化，请刷新结果。" }) } } });
    frame.dispatchEvent(new Event("load"));
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0].message).toContain("成片版本已变化");
    expect(document.querySelector("iframe")).toBeNull();
    expect(location.pathname).toBe("/");
  });

  it("suppresses old errors after leaving the project without removing an active browser transfer", async () => {
    const onError = vi.fn();
    const controller = new AbortController();
    await createApi().downloadVideo("video", "project-a", video, { signal: controller.signal, onError });
    const frame = document.querySelector("iframe");
    controller.abort();
    Object.defineProperty(frame, "contentDocument", { value: { URL: frame.src,
      body: { textContent: JSON.stringify({ error: "旧项目错误" }) } } });
    frame.dispatchEvent(new Event("load"));
    expect(onError).not.toHaveBeenCalled();
    expect(frame.isConnected).toBe(true);
  });
});
