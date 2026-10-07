import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

function state(overrides = {}) {
  return {
    source: "D:\\电影.mp4",
    campaign: "D:\\字幕任务",
    baseline: "",
    job: {
      busy: false,
      action: "",
      status: "idle",
      message: "",
      total: 0,
      recognized: 0,
      translated: 0,
      logs: [],
    },
    accounts: {
      siliconflow: {
        configured: false,
        storage: "none",
        ready: false,
        verified_on: "",
        price_per_second: null,
        pricing_reference: "",
      },
      bailian: { configured: false, storage: "none", ready: false },
      deepseek: { configured: false, storage: "none", ready: false },
    },
    settings: {
      asr_endpoint: "https://dashscope.aliyuncs.com/api/v1",
      asr_input_rate: "",
      asr_output_rate: "",
      deepseek_input_rate: "",
      deepseek_output_rate: "",
      verified_on: "",
      pricing_reference: "",
    },
    recent: [],
    local_available: true,
    project_config: {},
    language_options: { sources: ['ja','en','zh'], targets: ['zh-CN','en','ja'] },
    campaign_status: "prepared",
    approval_exists: false,
    siliconflow_result_exists: false,
    actions: {
      local: true,
      prepare: true,
      samples: false,
      full: false,
      approve: false,
      export: false,
      "siliconflow-pilot": false,
      "accept-final": false,
    },
    ...overrides,
  };
}

function server(initial = state(), custom) {
  let snapshot = initial;
  const requests = [];
  const respond = (body, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, options, body });
      if (url === '/api/environment') return respond({version:1,checks:{},export_ready:false,recommended_encoder:null});
      const result = await custom?.(url, body, snapshot);
      if (result) return respond(result.body, result.status);
      if (url === "/api/state") return respond(snapshot);
      if (url === "/api/credentials") {
        snapshot = {
          ...snapshot,
          accounts: {
            ...snapshot.accounts,
            [body.provider]: {
              ...snapshot.accounts[body.provider],
              configured: true,
              storage: "encrypted",
              ready: false,
            },
          },
        };
        return respond({ ok: true });
      }
      if (url === "/api/siliconflow-settings") {
        snapshot = {
          ...snapshot,
          accounts: {
            ...snapshot.accounts,
            siliconflow: {
              ...snapshot.accounts.siliconflow,
              ...body,
              verified_on: "2026-10-02",
              ready: !!snapshot.accounts.siliconflow.configured,
            },
          },
        };
        return respond({ saved: true });
      }
      if (url === "/api/run") {
        snapshot = {
          ...snapshot,
          job: {
            ...snapshot.job,
            busy: true,
            action: body.action,
            status: "running",
            message: "正在提取音频",
          },
        };
        return respond({ ok: true });
      }
      if (url === "/api/preview")
        return respond({
          cues: [],
          media_available: false,
          source_name: "",
          issues: [],
          downloads: [],
          auditions: [],
        });
      return respond({ ok: true });
    }),
  );
  return requests;
}

beforeEach(() => {
  window.history.replaceState({}, "", "/#token=test-launch-token");
});

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

describe("桌面工作台的真实操作边界", () => {
  it("keeps action errors in a dedicated feedback region with a working dismiss action", async () => {
    server(state(), url => url === '/api/open' ? {status:503, body:{error:'模拟目录打开失败'}} : null);
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', {name:'打开项目目录', exact:true}));
    const feedback = await screen.findByRole('region', {name:'操作反馈'});
    expect(feedback).toContainElement(screen.getByRole('alert'));
    expect(feedback).toHaveTextContent('模拟目录打开失败');
    await user.click(screen.getByRole('button', {name:'关闭错误提示'}));
    expect(screen.queryByRole('region', {name:'操作反馈'})).not.toBeInTheDocument();
  });

  it("announces task loading success in visible action feedback and allows dismissing it", async () => {
    server(state({recent:[{path:'D:\\继续项目', title:'继续项目', status:'prepared'}]}));
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', {name:'载入任务 继续项目'}));
    const feedback = await screen.findByRole('region', {name:'操作反馈'});
    expect(feedback).toHaveTextContent('任务已载入，可以继续制作。');
    expect(feedback.querySelector('[role="status"]')).not.toBeNull();
    await user.click(screen.getByRole('button', {name:'关闭成功提示'}));
    expect(screen.queryByRole('region', {name:'操作反馈'})).not.toBeInTheDocument();
  });

  it("allows dismissing an offline error while keeping an explicit reconnect action", async () => {
    const requests=server(state(), url => url === '/api/state' ? {status:503,body:{error:'模拟服务未连接'}} : null);
    const user=userEvent.setup(); render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('模拟服务未连接');
    const reads=requests.length;
    await user.click(screen.getByRole('button',{name:'关闭错误提示'}));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(requests).toHaveLength(reads);
    expect(screen.getByRole('button',{name:'重新连接',exact:true})).toBeEnabled();
    expect(screen.getByText('本地服务未连接')).toBeInTheDocument();
  });

  it("keeps unsaved cue edits when starting or loading another project", async () => {
    const preview = { project_id: 'review-project', selected_id: 'main', selections: [{ id: 'main', name: '整片' }],
      source_language: 'ja', target_language: 'zh-CN', cues: [{ id: 1, start_ms: 1000, end_ms: 2000,
        source_text: 'こんにちは', target_text: '你好', review_status: 'unchecked', note: '', warnings: [] }],
      manual_review: { supported: true, revision: 'r1', summary: { total: 1, checked: 0, issues: 0,
        pending_translation: 0, required_checks: 1, can_accept: false } } };
    const requests = server(state({ project_id: 'review-project', recent: [{ path: 'D:\\旅行项目', title: '京都旅行', status: 'full_ready' }] }),
      url => url.startsWith('/api/preview') ? { body: preview } : null);
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: '编辑第 1 句' }));
    await user.type(screen.getByLabelText('译文（中文）'), '，尚未保存');
    await user.click(screen.getByRole('button', { name: '新建任务', exact: true }));
    expect(await screen.findByText(/校对内容尚未保存，请先保存或放弃修改/)).toBeInTheDocument();
    expect(screen.getByLabelText('译文（中文）')).toHaveValue('你好，尚未保存');
    await user.click(screen.getByRole('button', { name: '载入任务 京都旅行' }));
    expect(requests.some(request => request.url === '/api/project')).toBe(false);
    expect(screen.getByLabelText('译文（中文）')).toHaveValue('你好，尚未保存');
    await user.click(screen.getByRole('button', { name: '放弃未保存修改' }));
    await user.click(screen.getByRole('button', { name: '新建任务', exact: true }));
    expect(screen.queryByLabelText('译文（中文）')).not.toBeInTheDocument();
  });

  it.each([
    ['API 设置', 'save'],
    ['任务记录', 'discard'],
    ['API 设置', 'revert'],
  ])("protects unsaved review text on %s and allows unload after %s", async (page, resolution) => {
    let preview = { project_id: 'review-project', selected_id: 'main', selections: [{ id: 'main', name: '整片' }],
      source_language: 'ja', target_language: 'zh-CN', cues: [{ id: 1, start_ms: 1000, end_ms: 2000,
        source_text: 'こんにちは', target_text: '你好', review_status: 'unchecked', note: '', warnings: [] }],
      manual_review: { supported: true, revision: 'r1', summary: { total: 1, checked: 0, issues: 0,
        pending_translation: 0, required_checks: 1, can_accept: false } } };
    server(state({ project_id: 'review-project' }), (url, body) => {
      if (url === '/api/review-cue') {
        preview = { ...preview, cues: [{ ...preview.cues[0], target_text: body.target_text }],
          manual_review: { ...preview.manual_review, revision: 'r2' } };
        return { body: preview };
      }
      return url.startsWith('/api/preview') ? { body: preview } : null;
    });
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', { name: '编辑第 1 句' }));
    const clean = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    await user.type(screen.getByLabelText('译文（中文）'), '，尚未保存');
    await user.click(screen.getByRole('button', { name: page, exact: true }));
    expect(screen.getByRole('button', { name: page, exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByLabelText('译文（中文）')).not.toBeInTheDocument();
    const dirty = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);

    await user.click(screen.getByRole('button', { name: '结果预览', exact: true }));
    expect(await screen.findByLabelText('译文（中文）')).toHaveValue('你好，尚未保存');
    if (resolution === 'save') {
      await user.click(screen.getByRole('button', { name: '保存修改' }));
      await screen.findByText('本句修改与核对状态已保存。');
    } else if (resolution === 'discard') {
      await user.click(screen.getByRole('button', { name: '放弃未保存修改' }));
    } else {
      await user.clear(screen.getByLabelText('译文（中文）'));
      await user.type(screen.getByLabelText('译文（中文）'), '你好');
    }
    await user.click(screen.getByRole('button', { name: page, exact: true }));
    const resolved = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(resolved);
    expect(resolved.defaultPrevented).toBe(false);
  });

  it.each([['en','ja'],['zh','en'],['ja','en']])("prepares cloud audio %s with chosen translation %s", async (source,target) => {
    const requests=server(state()); const user=userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('radio',{name:/百炼完整流程/}));
    await user.selectOptions(screen.getByLabelText('音频语言'),source);
    await user.selectOptions(screen.getByLabelText('翻译目标'),target);
    await user.click(screen.getByRole('button',{name:'准备样片'}));
    await waitFor(()=>expect(requests.find(r=>r.url==='/api/run')?.body).toMatchObject({action:'prepare',language:source,target}));
    expect(screen.getByLabelText('音频语言')).toHaveValue(source);
    expect(screen.getByLabelText('翻译目标')).toHaveValue(target);
  });

  it("restores and locks a saved Chinese English language pair", async () => {
    server(state({project_config:{engine:'bailian',asr_provider:'qwen_asr',language:'zh',target:'en'}}));
    render(<App />);
    expect(await screen.findByLabelText('音频语言')).toHaveValue('zh');
    expect(screen.getByLabelText('翻译目标')).toHaveValue('en');
    expect(screen.getByLabelText('翻译目标')).toBeDisabled();
  });
  it("shows persisted file locations and creation time without exposing them on a new draft", async () => {
    const fileLayout = {
      source_path: "D:\\电影.mp4", project_path: "D:\\字幕任务",
      exports_path: "D:\\字幕任务\\导出", created_at: "2026-10-03T20:35:00+08:00", video_path: "",
    };
    const requests = server(state({ project_id: "project-a", file_layout: fileLayout }));
    const user = userEvent.setup();
    render(<App />);
    const created = await screen.findByLabelText("任务创建时间");
    expect(created).toHaveAttribute("datetime", fileLayout.created_at);
    expect(screen.getByText(fileLayout.exports_path)).toBeInTheDocument();
    await user.click(screen.getByText("文件位置与输出"));
    await user.click(screen.getByRole("button", { name: "打开素材目录" }));
    expect(requests.some(request => request.url === "/api/open" && request.body.target === "source-folder" && request.body.project_id === "project-a")).toBe(true);
    await user.click(screen.getByRole("button", { name: "新建任务", exact: true }));
    expect(screen.queryByLabelText("任务创建时间")).not.toBeInTheDocument();
    expect(screen.queryByText(fileLayout.exports_path)).not.toBeInTheDocument();
    expect(screen.getByText(/按日期与创建时间自动命名/)).toBeInTheDocument();
  });

  it("does not invent a creation timestamp for legacy tasks", async () => {
    server(state());
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("region", { name: "文件位置" });
    await user.click(screen.getByText("文件位置与输出"));
    expect(screen.queryByLabelText("任务创建时间")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开素材目录" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开任务目录" })).toBeEnabled();
  });

  it("previews the persisted project on the workbench and opens dedicated review without starting a job", async () => {
    const requests = server();
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByRole('region', {name:'字幕预览工作区'})).toBeInTheDocument();
    await user.click(screen.getByRole('button', {name:'进入校对台'}));
    expect(screen.getByRole('button', {name:'结果预览', exact:true})).toHaveAttribute('aria-current','page');
    expect(screen.getByRole('region', {name:'字幕预览工作区'})).toBeInTheDocument();
    expect(requests.some(request => request.url === '/api/run')).toBe(false);
    await user.click(screen.getByRole('button', {name:'新建任务', exact:true}));
    expect(screen.queryByRole('region', {name:'字幕预览工作区'})).not.toBeInTheDocument();
  });

  it.each([
    ['export-draft', '直接导出草稿 MP4'],
    ['export', '导出带字幕 MP4'],
  ])("opens visible workbench progress after starting %s from review", async (action, label) => {
    const base = state({ project_id: 'export-project', campaign_status: 'full_ready', project_config: { engine: 'bailian' } });
    base.actions[action] = true;
    server(base, (url, body, snapshot) => {
      if (url === '/api/run') snapshot.job.export_progress = { phase: 'encoding', percent: 42,
        encoded_seconds: 420, duration_seconds: 1000, speed: 12, eta_seconds: 49, elapsed_seconds: 35 };
      return null;
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: '进入校对台' }));
    const exportButton = await screen.findByRole('button', { name: label });
    await waitFor(() => expect(exportButton).toBeEnabled());
    await user.click(exportButton);

    expect(await screen.findByRole('progressbar', { name: '视频编码进度' })).toHaveAttribute('value', '42');
    expect(screen.getByRole('button', { name: '字幕任务', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('region', { name: '字幕预览工作区' })).toBeInTheDocument();
  });

  it("stays in review when starting a draft export is rejected", async () => {
    const base = state({ campaign_status: 'full_ready', project_config: { engine: 'bailian' } });
    base.actions['export-draft'] = true;
    server(base, url => url === '/api/run' ? { status: 409, body: { error: '导出未启动，请检查素材。' } } : null);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: '进入校对台' }));
    const exportButton = await screen.findByRole('button', { name: '直接导出草稿 MP4' });
    await waitFor(() => expect(exportButton).toBeEnabled());
    await user.click(exportButton);

    expect(await screen.findByRole('alert')).toHaveTextContent('导出未启动，请检查素材。');
    expect(screen.getByRole('button', { name: '结果预览', exact: true })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('heading', { name: '制作进度' })).not.toBeInTheDocument();
  });

  it("loads a recent project from the sidebar without submitting a run", async () => {
    const requests = server(state({recent:[{path:'D:\\旅行项目', title:'京都旅行', status:'full_ready'}]}));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', {name:'载入任务 京都旅行'}));
    expect(requests.some(request => request.url === '/api/project' && request.body.campaign === 'D:\\旅行项目')).toBe(true);
    expect(requests.some(request => request.url === '/api/run')).toBe(false);
  });

  it("retains listening position and search through actual workbench to review navigation", async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
    const requests = server(state(), url => url.startsWith('/api/preview') ? {body:{
      cues:[{id:1,start_ms:0,end_ms:9000,ja:'京都',zh:'京都旅行'}],
      media_available:true,source_name:'京都',media_url:'/fixture.mp4',
      selections:[{id:'main',name:'整片'}],selected_id:'main',downloads:[],issues:[],auditions:[],
    }} : null);
    const user = userEvent.setup();
    render(<App />);
    const original = await screen.findByLabelText('源素材预览');
    fireEvent.loadedMetadata(original);
    original.currentTime = 4.25;
    fireEvent.timeUpdate(original);
    await user.selectOptions(screen.getByLabelText('播放速度'),'1.25');
    await user.type(screen.getByLabelText('搜索字幕'),'旅行');
    await user.click(screen.getByRole('button',{name:'进入校对台'}));
    const restored = await screen.findByLabelText('源素材预览');
    expect(restored).not.toBe(original);
    fireEvent.loadedMetadata(restored);
    expect(restored.currentTime).toBe(4.25);
    expect(restored.playbackRate).toBe(1.25);
    expect(screen.getByLabelText('搜索字幕')).toHaveValue('旅行');
    expect(restored.paused).toBe(true);
    expect(requests.some(request=>request.url==='/api/run')).toBe(false);
  });

  it("recommends preview for a completed local task without announcing an old completion", async () => {
    server(state({campaign_status:'',project_config:{engine:'local',model:'small'},job:{busy:false,action:'local',status:'complete',message:'已保存字幕',logs:[]}}));
    render(<App />);
    expect(await screen.findByRole('heading',{name:'查看生成的字幕'})).toBeInTheDocument();
    expect(screen.queryByText('本次任务已结束')).not.toBeInTheDocument();
  });

  it("announces a background completion without navigating away from the current page", async () => {
    const base=state({project_id:'same-project',campaign_status:'',job:{busy:true,action:'local',status:'running',message:'处理中',logs:[]}});
    let polls=0;
    server(base,url=>url==='/api/state'?{body:++polls===2?{...base,job:{...base.job,busy:false,status:'complete',message:'已保存字幕'}}:base}:null);
    const user=userEvent.setup();render(<App />);
    await screen.findByText('正在处理');
    await user.click(screen.getByRole('button',{name:'任务记录',exact:true}));
    expect(await screen.findByText('本次任务已结束',{}, {timeout:3000})).toBeInTheDocument();
    expect(screen.getByRole('heading',{name:'最近的任务'})).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'查看本次结果'})).toBeInTheDocument();
    await user.click(screen.getByRole('button',{name:'打开项目目录',exact:true}));
    await waitFor(()=>expect(screen.queryByText('本次任务已结束')).not.toBeInTheDocument());
  });

  it.each(['initial', 'background'])("offers reconnect after a hanging %s state read and resumes polling after retry", async stage => {
    let reads = 0;
    const requests = server(state(), url => url === '/api/state' && ++reads === (stage === 'initial' ? 1 : 2) ? new Promise(() => {}) : null);
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      if (stage === 'background') await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(screen.getByRole('alert')).toHaveTextContent(/读取.*本地.*超时|本地.*读取.*超时/);
      expect(screen.getByRole('button', { name: '重新连接' })).toBeEnabled();
      const stalledRead = requests.filter(request => request.url === '/api/state').at(-1);
      expect(stalledRead.options.signal.aborted).toBe(true);
      expect(reads).toBe(stage === 'initial' ? 1 : 2);

      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新连接' })); });
      expect(screen.getByText('本地服务已连接')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '新建任务', exact: true })).toBeEnabled();
      const afterRetry = reads;
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      expect(reads).toBe(afterRetry + 1);
    } finally { vi.useRealTimers(); }
  });

  it("releases the project selection lock when its state read hangs despite newer healthy polls", async () => {
    const original = state({ project_id: 'project-a', recent: [{ path: 'D:\\项目B', title: '项目 B', status: 'prepared' }] });
    const selected = { ...original, project_id: 'project-b', source: 'D:\\项目B.mp4', campaign: 'D:\\项目B' };
    let reads = 0, selectedProject = false;
    const requests = server(original, url => {
      if (url === '/api/project') { selectedProject = true; return { body: selected }; }
      if (url !== '/api/state') return null;
      if (++reads === 2) return new Promise(() => {});
      return { body: selectedProject ? selected : original };
    });
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '载入任务 项目 B' })); });
      expect(screen.getByRole('button', { name: '新建任务', exact: true })).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(reads).toBeGreaterThan(2);
      expect(screen.getByRole('button', { name: '新建任务', exact: true })).toBeEnabled();
      expect(screen.getByRole('button', { name: '载入任务 项目 B' })).toBeEnabled();
      expect(screen.getByRole('heading', { name: '项目B.mp4', level: 1 })).toBeInTheDocument();
      expect(requests.filter(request => request.url === '/api/project')).toHaveLength(1);
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it.each([['pending', 'response'], ['applied', 'response'], ['pending', 'failure']])("ignores an old project poll when the selected project's read is %s and the old poll returns a %s", async (phase, outcome) => {
    const oldPoll = deferred(), selectedRead = deferred();
    const original = state({ project_id: 'project-a', recent: [{ path: 'D:\\项目B', title: '项目 B', status: 'prepared' }] });
    const selected = { ...original, project_id: 'project-b', source: 'D:\\项目B.mp4', campaign: 'D:\\项目B' };
    let reads = 0, selectionRequested = false;
    server(original, (url, body) => {
      if (url === '/api/project') { selectionRequested = true; return { body: selected }; }
      if (url !== '/api/state') return null;
      reads += 1;
      if (reads === 2) return oldPoll.promise;
      if (reads === 3) return selectedRead.promise;
      return { body: selectionRequested ? selected : original };
    });
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      expect(reads).toBe(2);
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '载入任务 项目 B' })); });
      if (phase === 'applied') await act(async () => { selectedRead.resolve({ body: selected }); });
      await act(async () => { oldPoll.resolve(outcome === 'failure' ? { status: 503, body: { error: '过期的项目 A 读取失败' } } : { body: { ...original, job: { ...original.job, busy: true, action: 'local', status: 'running', message: '过期的项目 A 任务' } } }); });
      expect(screen.queryByText('过期的项目 A 任务')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '停止任务' })).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      if (phase === 'pending') await act(async () => { selectedRead.resolve({ body: selected }); });
      expect(screen.getByRole('heading', { name: '项目B.mp4', level: 1 })).toBeInTheDocument();
      expect(screen.getByRole('region', { name: '字幕预览工作区' })).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it("ignores a poll started during run submission after the run successfully starts", async () => {
    const submission = deferred(), oldPoll = deferred(), runningRead = deferred();
    const original = state();
    const running = { ...original, job: { ...original.job, busy: true, action: 'local', status: 'running', message: '新任务正在处理' } };
    let reads = 0;
    server(original, url => {
      if (url === '/api/run') return submission.promise;
      if (url !== '/api/state') return null;
      reads += 1;
      return reads === 2 ? oldPoll.promise : reads === 3 ? runningRead.promise : { body: original };
    });
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始本地识别' })); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      await act(async () => { submission.resolve({ body: { ok: true } }); });
      await act(async () => { oldPoll.resolve({ body: { ...original, job: { ...original.job, status: 'needs_attention', message: '启动前的旧任务问题' } } }); });
      expect(screen.queryByText('启动前的旧任务问题')).not.toBeInTheDocument();
      await act(async () => { runningRead.resolve({ body: running }); });
      expect(screen.getByText('新任务正在处理')).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it.each(['response', 'failure'])("ignores an older same-project poll %s after a newer busy response", async outcome => {
    const oldPoll = deferred();
    const original = state();
    const running = { ...original, job: { ...original.job, busy: true, action: 'local', status: 'running', message: '较新的处理状态', total: 10, recognized: 3 } };
    let reads = 0;
    server(original, url => {
      if (url !== '/api/state') return null;
      reads += 1;
      return reads === 2 ? oldPoll.promise : { body: reads > 2 ? running : original };
    });
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      expect(reads).toBe(2);
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '更换素材' })); });
      expect(screen.getByText('较新的处理状态')).toBeInTheDocument();
      await act(async () => { oldPoll.resolve(outcome === 'failure' ? { status: 503, body: { error: '过期的读取失败' } } : { body: original }); });
      expect(screen.getByText('较新的处理状态')).toBeInTheDocument();
      expect(screen.getByRole('progressbar', { name: '已识别片段' })).toHaveAttribute('value', '3');
      expect(screen.queryByText('本次任务已结束')).not.toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it("accepts a pending same-project busy response when a newer state read fails", async () => {
    const oldPoll = deferred();
    const original = state();
    let reads = 0;
    server(original, url => {
      if (url !== '/api/state') return null;
      reads += 1;
      if (reads === 2) return oldPoll.promise;
      return reads > 2 ? { status: 503, body: { error: '本次状态读取失败' } } : { body: original };
    });
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '更换素材' })); });
      expect(screen.getByRole('alert')).toHaveTextContent('本次状态读取失败');
      await act(async () => { oldPoll.resolve({ body: { ...original, job: { ...original.job, busy: true, action: 'local', status: 'running', message: '有效的后台进度', total: 10, recognized: 4 } } }); });
      expect(screen.getByText('有效的后台进度')).toBeInTheDocument();
      expect(screen.getByRole('progressbar', { name: '已识别片段' })).toHaveAttribute('value', '4');
    } finally { vi.useRealTimers(); }
  });

  it("keeps the existing project and its pending poll when project selection fails", async () => {
    const oldPoll = deferred();
    const original = state({ recent: [{ path: 'D:\\缺失项目', title: '缺失项目', status: 'prepared' }] });
    let reads = 0;
    server(original, url => {
      if (url === '/api/project') return { status: 404, body: { error: '项目目录不存在' } };
      if (url === '/api/state' && ++reads === 2) return oldPoll.promise;
      return null;
    });
    vi.useFakeTimers();
    try {
      await act(async () => { render(<App />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '载入任务 缺失项目' })); });
      expect(screen.getByRole('alert')).toHaveTextContent('项目目录不存在');
      await act(async () => { oldPoll.resolve({ body: { ...original, job: { ...original.job, busy: true, action: 'local', status: 'running', message: '原项目继续处理' } } }); });
      expect(screen.getByText('原项目继续处理')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: '电影.mp4', level: 1 })).toBeInTheDocument();
      expect(screen.getByRole('region', { name: '字幕预览工作区' })).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });

  it("keeps review entries when visiting preview but never submits them automatically", async () => {
    const base=state({project_config:{engine:'bailian'},campaign_status:'samples_ready'});
    base.actions.approve=true;
    const requests=server(base);const user=userEvent.setup();render(<App />);
    await user.type(await screen.findByLabelText('实际抽检句数'),'20');
    await user.type(screen.getByLabelText('时间合格句数'),'18');
    await user.click(screen.getByLabelText('我已检查内容，确认样片通过'));
    await user.click(screen.getByRole('button',{name:'结果预览',exact:true}));
    await user.click(screen.getByRole('button',{name:'字幕任务',exact:true}));
    expect(screen.getByLabelText('实际抽检句数')).toHaveValue(20);
    expect(screen.getByLabelText('时间合格句数')).toHaveValue(18);
    expect(screen.getByLabelText('我已检查内容，确认样片通过')).toBeChecked();
    expect(requests.some(r=>r.url==='/api/run')).toBe(false);
    await user.click(screen.getByRole('button',{name:'新建任务',exact:true}));
    expect(screen.queryByLabelText('我已检查内容，确认样片通过')).not.toBeInTheDocument();
  });

  it("explains review eligibility and rejects fractional or insufficient timing counts", async () => {
    const base=state({project_config:{engine:'bailian'},campaign_status:'samples_ready'});base.actions.approve=true;
    server(base);const user=userEvent.setup();render(<App />);
    expect(await screen.findByText('还需填写实际抽检句数（至少 20 句）')).toBeInTheDocument();
    await user.type(screen.getByLabelText('实际抽检句数'),'20');
    await user.type(screen.getByLabelText('时间合格句数'),'17');
    expect(screen.getByText('时间合格率 85.0%，未达到 90%')).toBeInTheDocument();
    await user.clear(screen.getByLabelText('时间合格句数'));
    await user.type(screen.getByLabelText('时间合格句数'),'18.5');
    expect(screen.getByText('时间合格句数须为 0 到抽检总数之间的整数')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'记录样片验收'})).toBeDisabled();
  });

  it("binds the run to the project returned by selection rather than a stale snapshot", async () => {
    const requests=server(state({project_id:'old-project'}),url=>url==='/api/project'?{body:state({project_id:'selected-project'})}:null);
    const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('button',{name:'开始本地识别'}));
    expect(requests.find(r=>r.url==='/api/run').body.project_id).toBe('selected-project');
  });

  it("binds the project folder button to the displayed project", async () => {
    const requests=server(state({project_id:'displayed-project'}));
    const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('button',{name:'打开项目目录'}));
    expect(requests.find(r=>r.url==='/api/open').body).toEqual({target:'project',project_id:'displayed-project'});
  });

  it("acknowledges opening immediately with the returned selectable path and copies it without a state refresh", async () => {
    const requests = server(state(), url => url === '/api/open'
      ? {body:{opened:true,path:'D:\\实际项目\\任务 01'}} : null);
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
    render(<App />);
    const button = await screen.findByRole('button', {name:'打开项目目录'});
    const reads = requests.filter(request => request.url === '/api/state').length;
    await user.click(button);
    const receipt = await screen.findByRole('status', {name:'打开位置反馈'});
    expect(receipt).toHaveTextContent('已请求在文件资源管理器中打开');
    expect(receipt).toHaveTextContent('任务栏');
    const path = screen.getByRole('textbox', {name:'打开位置路径'});
    expect(path).toHaveValue('D:\\实际项目\\任务 01');
    expect(path).toHaveAttribute('readonly');
    expect(button).toBeEnabled();
    expect(requests.filter(request => request.url === '/api/state')).toHaveLength(reads);
    await user.click(screen.getByRole('button', {name:'复制路径'}));
    expect(copy).toHaveBeenCalledWith('D:\\实际项目\\任务 01');
    expect(receipt).toHaveTextContent('路径已复制');
  });

  it("uses the selected project path for an old backend response and clears it on a new task", async () => {
    server(state({campaign:'D:\\旧服务\\项目'}), url => url === '/api/open'
      ? {body:{opened:true}} : null);
    const user = userEvent.setup(); render(<App />);
    await user.click(await screen.findByRole('button', {name:'打开项目目录'}));
    expect(await screen.findByRole('textbox', {name:'打开位置路径'})).toHaveValue('D:\\旧服务\\项目');
    await user.click(screen.getByRole('button', {name:'新建任务',exact:true}));
    expect(screen.queryByRole('status', {name:'打开位置反馈'})).not.toBeInTheDocument();
  });

  it("does not invent the project path when an old backend opens a different location", async () => {
    server(state({file_layout:{source_path:'D:\\电影.mp4',project_path:'D:\\字幕任务'}}), url => url === '/api/open'
      ? {body:{opened:true}} : null);
    const user = userEvent.setup(); render(<App />);
    await screen.findByRole('button', {name:'打开项目目录'});
    await user.click(screen.getByText('文件位置与输出'));
    await user.click(screen.getByRole('button', {name:'打开素材目录'}));
    expect(await screen.findByRole('status', {name:'打开位置反馈'})).toHaveTextContent('已请求');
    expect(screen.queryByRole('textbox', {name:'打开位置路径'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name:'复制路径'})).not.toBeInTheDocument();
  });

  it("shows pending feedback and submits only once while opening is delayed", async () => {
    server(); const baseFetch = globalThis.fetch; let release; let calls = 0;
    vi.stubGlobal('fetch', (url, options) => url === '/api/open'
      ? (calls++, new Promise(resolve => {release=resolve;})) : baseFetch(url, options));
    const user = userEvent.setup(); render(<App />);
    const button = await screen.findByRole('button', {name:'打开项目目录'});
    await user.click(button);
    expect(button).toHaveTextContent('正在打开');
    expect(button).toHaveAttribute('aria-busy','true');
    expect(button).toBeDisabled();
    await user.click(button);
    expect(calls).toBe(1);
    await act(async () => release(new Response(JSON.stringify({opened:true}), {status:200})));
    expect(button).toBeEnabled();
    expect(screen.getByRole('status', {name:'打开位置反馈'})).toHaveTextContent('已请求');
  });

  it("shows an opening error without claiming success and retains a copyable project path", async () => {
    server(state(), url => url === '/api/open'
      ? {status:500,body:{error:'无法打开项目目录'}} : null);
    const user = userEvent.setup(); render(<App />);
    const button = await screen.findByRole('button', {name:'打开项目目录'});
    await user.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('无法打开项目目录');
    expect(screen.queryByText(/已请求在文件资源管理器中打开/)).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', {name:'打开位置路径'})).toHaveValue('D:\\字幕任务');
    expect(screen.getByRole('button', {name:'复制路径'})).toBeEnabled();
    expect(button).toBeEnabled();
  });

  it("unlocks after a 15 second opening timeout, labels the result unknown and never retries automatically", async () => {
    server(); const baseFetch = globalThis.fetch; let calls = 0; let signal;
    vi.stubGlobal('fetch', (url, options) => url === '/api/open'
      ? (calls++, signal=options.signal, new Promise(() => {})) : baseFetch(url, options));
    render(<App />);
    const button = await screen.findByRole('button', {name:'打开项目目录'});
    vi.useFakeTimers();
    try {
      fireEvent.click(button);
      await act(async () => {await vi.advanceTimersByTimeAsync(15000);});
      expect(screen.getByRole('alert')).toHaveTextContent('结果未知');
      expect(screen.getByRole('alert')).toHaveTextContent('不会自动重试');
      expect(signal.aborted).toBe(true);
      expect(button).toBeEnabled();
      expect(screen.getByRole('textbox', {name:'打开位置路径'})).toHaveValue('D:\\字幕任务');
      expect(screen.getByRole('button', {name:'复制路径'})).toBeEnabled();
      await act(async () => {await vi.advanceTimersByTimeAsync(15000);});
      expect(calls).toBe(1);
      expect(screen.queryByText(/已请求在文件资源管理器中打开/)).not.toBeInTheDocument();
    } finally {vi.useRealTimers();}
  });

  it("keeps the chosen audition service after selecting an existing cloud project for a run", async () => {
    const base=state({project_config:{engine:'bailian',asr_provider:'qwen_asr'}});
    base.accounts.siliconflow={...base.accounts.siliconflow,configured:true,ready:true};
    base.actions['siliconflow-pilot']=true;
    const requests=server(base,url=>url==='/api/project'?{body:{...base,project_id:'cloud-project'}}:null);
    const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('radio',{name:/硅基流动样片/}));
    await user.click(screen.getByRole('button',{name:'开始样片试听（计费）'}));
    expect(requests.find(r=>r.url==='/api/run').body).toMatchObject({action:'siliconflow-pilot',project_id:'cloud-project'});
    expect(screen.getByRole('radio',{name:/硅基流动样片/})).toHaveAttribute('aria-checked','true');
  });

  it("restores a cloud project into its own engine without starting requests", async () => {
    const requests = server(state({project_config:{asr_provider:'qwen_asr'},campaign_status:'samples_ready'}));
    render(<App />);
    expect(await screen.findByRole('radio',{name:/百炼完整流程/})).toHaveAttribute('aria-checked','true');
    expect(screen.getByRole('radio',{name:/本地识别/})).toBeDisabled();
    expect(requests.some(r=>r.url==='/api/run')).toBe(false);
  });

  it("changing the input starts a fresh draft without attaching the old project or baseline", async () => {
    const requests=server(state({baseline:'D:\\旧字幕.srt'}),url=>url==='/api/pick'?{body:{path:'D:\\新视频.mp4'}}:null);
    const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('button',{name:'更换素材'}));
    expect(screen.getByLabelText('项目目录',{exact:false,selector:'input'})).toHaveValue('');
    expect(screen.getByText(/新素材将创建独立任务/)).toBeInTheDocument();
    await user.click(screen.getByRole('button',{name:'开始本地识别'}));
    expect(requests.find(r=>r.url==='/api/project').body).toEqual({source:'D:\\新视频.mp4',campaign:'',baseline:'',new_task:true});
  });

  it("keeps a failed run visible above the saved project stage and omits disabled translation progress", async () => {
    server(state({job:{busy:false,action:'local',status:'needs_attention',message:'音频读取失败，已保留结果',total:3,recognized:1,translated:0,logs:[]},campaign_status:'prepared'}));
    render(<App />);
    expect((await screen.findAllByText('需要处理')).length).toBeGreaterThan(0);
    expect(screen.queryByRole('progressbar',{name:'已翻译片段'})).not.toBeInTheDocument();
    expect(screen.getByText('未启用翻译')).toBeInTheDocument();
  });

  it("offers cloud sample preparation without requiring an old subtitle", async () => {
    server(state());const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('radio',{name:/百炼完整流程/}));
    expect(screen.getByRole('button',{name:'准备样片'})).toBeEnabled();
    expect(screen.getByText(/旧字幕只作对照/)).toBeInTheDocument();
  });

  it("does not show the old project's preview after choosing a new input", async () => {
    const requests=server(state(),url=>url==='/api/pick'?{body:{path:'D:\\新视频.mp4'}}:null);
    const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('button',{name:'更换素材'}));
    const checkpoint = requests.length;
    expect(screen.queryByRole('region', {name:'字幕预览工作区'})).not.toBeInTheDocument();
    await user.click(screen.getByRole('button',{name:'结果预览'}));
    expect(screen.getByText('新任务尚未创建')).toBeInTheDocument();
    expect(requests.slice(checkpoint).some(r=>r.url.startsWith('/api/preview'))).toBe(false);
  });

  it("lets a prepared cloud project compare SiliconFlow while preventing local mixing", async () => {
    server(state({project_config:{engine:'bailian',asr_provider:'qwen_asr'}}));
    const user=userEvent.setup();render(<App />);
    await user.click(await screen.findByRole('radio',{name:/硅基流动样片/}));
    expect(screen.getByRole('radio',{name:/硅基流动样片/})).toHaveAttribute('aria-checked','true');
    expect(screen.getByRole('radio',{name:/本地识别/})).toBeDisabled();
  });
  it("launch token authenticates every request and is removed from the URL after establishing the session", async () => {
    const requests = server();
    render(<App />);
    await screen.findByRole("button", { name: "开始本地识别" });
    expect(requests[0].url).toBe("/api/session");
    expect(window.location.hash).toBe("");
    expect(
      requests.every(
        (r) => r.options.headers["X-Subtitle-Token"] === "test-launch-token",
      ),
    ).toBe(true);
  });

  it("stores a key independently of price confirmation and never restores it into the UI or browser storage", async () => {
    const requests = server();
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();
    const first = render(<App />);
    await user.click(screen.getByRole("button", { name: "API 设置" }));
    const field = await screen.findByLabelText("硅基流动 API Key");
    expect(field).toHaveAttribute("type", "password");
    await user.type(field, "sk-fake-private-value");
    await user.click(screen.getByRole("button", { name: "保存硅基流动 Key" }));
    await waitFor(() => expect(field).toHaveValue(""));
    expect(requests.find((r) => r.url === "/api/credentials").body).toEqual({
      provider: "siliconflow",
      key: "sk-fake-private-value",
    });
    expect(storage).not.toHaveBeenCalled();
    expect(document.body).not.toHaveTextContent("sk-fake-private-value");
    first.unmount();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "API 设置" }));
    expect(await screen.findByLabelText("硅基流动 API Key")).toHaveValue("");
    expect(await screen.findByText("已加密保存")).toBeInTheDocument();
  });

  it("shows a failed connection instead of fabricated progress or example subtitles", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/本地服务/);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新连接" })).toBeEnabled();
  });

  it.each([
    { rate: "0.000220", estimate: "¥0.066", expected: 0.00022 },
    { rate: "0", estimate: "¥0.000", expected: 0 },
  ])(
    "requires a quoted source and explicit confirmation before saving SF rate $rate independently of the key",
    async ({ rate, estimate, expected }) => {
      const requests = server();
      const user = userEvent.setup();
      render(<App />);
      await user.click(screen.getByRole("button", { name: "API 设置" }));
      const input = await screen.findByLabelText("硅基流动单价（元 / 秒）");
      expect(input).toHaveValue(0.00022);
      const save = screen.getByRole("button", { name: "保存硅基流动价格" });
      expect(save).toBeDisabled();
      await user.clear(input);
      await user.type(input, rate);
      expect(screen.getByLabelText("五分钟样片预计费用")).toHaveTextContent(
        estimate,
      );
      await user.click(
        screen.getByLabelText("我已核实当前硅基流动账户的模型价格"),
      );
      expect(save).toBeDisabled();
      await user.type(
        screen.getByLabelText("硅基流动报价来源"),
        "当前账户模型页截图",
      );
      await user.click(save);
      await waitFor(() =>
        expect(
          requests.some((r) => r.url === "/api/siliconflow-settings"),
        ).toBe(true),
      );
      expect(
        requests.find((r) => r.url === "/api/siliconflow-settings").body,
      ).toEqual({
        price_per_second: expected,
        pricing_reference: "当前账户模型页截图",
        confirmed: true,
      });
      expect(
        requests.some((r) => ["/api/credentials", "/api/run"].includes(r.url)),
      ).toBe(false);
      await waitFor(() =>
        expect(
          screen.getByLabelText("我已核实当前硅基流动账户的模型价格"),
        ).not.toBeChecked(),
      );
      expect(document.body).not.toHaveTextContent(/免费/);
    },
  );

  it("restores the saved SF quote and labels its explicit upload action as metered billing", async () => {
    const initial = state();
    const requests = server({
      ...initial,
      accounts: {
        ...initial.accounts,
        siliconflow: {
          configured: true,
          storage: "encrypted",
          ready: true,
          verified_on: "2026-10-02",
          price_per_second: 0.001,
          pricing_reference: "我的账户实际报价",
        },
      },
      actions: { ...initial.actions, "siliconflow-pilot": true },
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "API 设置" }));
    await waitFor(() =>
      expect(screen.getByLabelText("硅基流动单价（元 / 秒）")).toHaveValue(
        0.001,
      ),
    );
    expect(screen.getByLabelText("五分钟样片预计费用")).toHaveTextContent(
      "¥0.300",
    );
    expect(screen.getByLabelText("硅基流动报价来源")).toHaveValue(
      "我的账户实际报价",
    );
    await user.click(screen.getByRole("button", { name: "字幕任务" }));
    await user.click(
      await screen.findByRole("radio", { name: /硅基流动样片/ }),
    );
    expect(document.body).not.toHaveTextContent(/免费/);
    expect(
      screen.getByText(/上传音频至硅基流动.*按量计费/),
    ).toBeInTheDocument();
    expect(requests.some((r) => r.url === "/api/run")).toBe(false);
    await user.click(
      screen.getByRole("button", { name: "开始样片试听（计费）" }),
    );
    await waitFor(() =>
      expect(requests.some((r) => r.url === "/api/run")).toBe(true),
    );
    expect(requests.find((r) => r.url === "/api/run").body).toEqual({
      action: "siliconflow-pilot",
    });
  });

  it("sends the selected project before a local run and exposes cooperative stop while running", async () => {
    const requests = server();
    const user = userEvent.setup();
    render(<App />);
    const start = await screen.findByRole("button", { name: "开始本地识别" });
    await user.click(start);
    const stop = await screen.findByRole("button", { name: "停止任务" });
    await waitFor(() => expect(stop).toBeEnabled());
    const projectIndex = requests.findIndex((r) => r.url === "/api/project");
    const runIndex = requests.findIndex((r) => r.url === "/api/run");
    expect(projectIndex).toBeLessThan(runIndex);
    expect(requests[projectIndex].body).toEqual({
      source: "D:\\电影.mp4",
      campaign: "D:\\字幕任务",
      baseline: "",
    });
    expect(requests[runIndex].body).toMatchObject({
      action: "local",
      language: "ja",
      chunk_seconds: 120,
      workers: 1,
      translate: false,
    });
    expect(screen.getAllByText("正在提取音频").length).toBeGreaterThan(0);
    await user.click(stop);
    expect(requests.some((r) => r.url === "/api/stop")).toBe(true);
  });

  it("requires actual review input and never automatically starts the full paid workflow", async () => {
    const requests = server(
      state({
        campaign_status: "samples_ready",
        actions: {
          local: true,
          prepare: true,
          samples: true,
          approve: true,
          full: false,
        },
      }),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      await screen.findByRole("radio", { name: /百炼完整流程/ }),
    );
    expect(await screen.findByText("样片等待人工验收")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "处理整片" })).toBeDisabled();
    const approve = screen.getByRole("button", { name: "记录样片验收" });
    expect(approve).toBeDisabled();
    await user.type(screen.getByLabelText("实际抽检句数"), "20");
    await user.type(screen.getByLabelText("时间合格句数"), "18");
    await user.click(screen.getByLabelText("我已检查内容，确认样片通过"));
    await user.click(approve);
    await waitFor(() =>
      expect(requests.some((r) => r.url === "/api/run")).toBe(true),
    );
    expect(
      requests.filter((r) => r.url === "/api/run").map((r) => r.body),
    ).toEqual([
      {
        action: "approve",
        reviewed: 20,
        timing_passed: 18,
        content_passed: true,
      },
    ]);
  });

  it("keeps server errors visible after a rejected action", async () => {
    server(state(), (url) =>
      url === "/api/run"
        ? { status: 409, body: { error: "已有任务正在运行，请先停止。" } }
        : null,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(
      await screen.findByRole("button", { name: "开始本地识别" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "已有任务正在运行，请先停止。",
    );
  });

  it("filters task history by name or path and restores the list after clearing", async () => {
    const requests = server(state({recent:[
      {path:'D:\\任务\\京都',title:'京都旅行',status:'full_ready'},
      {path:'D:\\English\\Interview',title:'访谈',status:'prepared'},
    ]}));
    const user = userEvent.setup(); render(<App />);
    await screen.findByRole('radio',{name:/本地识别/});
    await user.click(screen.getByRole('button',{name:'任务记录',exact:true}));
    await user.type(screen.getByRole('searchbox',{name:'搜索任务'}),'ｅｎｇｌｉｓｈ');
    expect(screen.getAllByRole('button',{name:'载入任务',exact:true})).toHaveLength(1);
    expect(screen.getByRole('heading',{name:'访谈'})).toBeInTheDocument();
    await user.clear(screen.getByRole('searchbox',{name:'搜索任务'}));
    await user.type(screen.getByRole('searchbox',{name:'搜索任务'}),'不存在');
    expect(screen.getByText('没有匹配的任务')).toBeInTheDocument();
    await user.click(screen.getByRole('button',{name:'清空任务搜索'}));
    expect(screen.getAllByRole('button',{name:'载入任务',exact:true})).toHaveLength(2);
    expect(requests.some(r=>r.url==='/api/run')).toBe(false);
  });

  it("locks the history directory picker until the native dialog is dismissed", async () => {
    server(); const baseFetch = globalThis.fetch; let release;
    const picker = vi.fn(() => new Promise(resolve => { release=resolve; }));
    vi.stubGlobal('fetch',(url,options)=>url==='/api/pick' ? picker() : baseFetch(url,options));
    const user = userEvent.setup(); render(<App />);
    await screen.findByRole('radio',{name:/本地识别/});
    await user.click(screen.getByRole('button',{name:'任务记录',exact:true}));
    const choose=screen.getByRole('button',{name:'选择并载入'});
    await user.click(choose);
    expect(choose).toBeDisabled();
    await user.click(choose);
    expect(picker).toHaveBeenCalledTimes(1);
    release(new Response(JSON.stringify({path:''}),{status:200}));
    await waitFor(()=>expect(choose).toBeEnabled());
    expect(screen.queryByText('任务已载入，可以继续制作。')).not.toBeInTheDocument();
  });

  it("supports arrow-key engine selection without starting a job", async () => {
    const requests = server(); const user=userEvent.setup(); render(<App />);
    const local=await screen.findByRole('radio',{name:/本地识别/});
    const audition=screen.getByRole('radio',{name:/硅基流动样片/});
    const cloud=screen.getByRole('radio',{name:/百炼完整流程/});
    expect(local).toHaveAttribute('tabindex','0');
    expect(audition).toHaveAttribute('tabindex','-1');
    local.focus(); await user.keyboard('{ArrowRight}');
    expect(audition).toHaveFocus(); expect(audition).toHaveAttribute('aria-checked','true');
    await user.keyboard('{End}'); expect(cloud).toHaveFocus();
    await user.keyboard('{ArrowRight}'); expect(local).toHaveFocus();
    expect(requests.some(r=>r.url==='/api/run')).toBe(false);
  });

  it("skips locked engines when selecting a saved cloud task with the keyboard", async () => {
    server(state({project_config:{engine:'bailian'}}));
    const user=userEvent.setup(); render(<App />);
    const cloud=await screen.findByRole('radio',{name:/百炼完整流程/});
    cloud.focus(); await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('radio',{name:/硅基流动样片/})).toHaveFocus();
    expect(screen.getByRole('radio',{name:/本地识别/})).toBeDisabled();
  });

  it("shows siliconflow audition as text and offers no subtitle download when none exists", async () => {
    server(state(), (url) =>
      url === "/api/preview"
        ? {
            body: {
              cues: [],
              media_available: false,
              source_name: "电影.mp4",
              issues: [],
              downloads: [],
              auditions: [
                {
                  name: "片段一",
                  text: "试听识别结果",
                  timing_verified: false,
                },
              ],
            },
          }
        : null,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "结果预览" }));
    expect(await screen.findByText("试听识别结果")).toBeInTheDocument();
    expect(screen.getByText(/试听文字尚未对齐时间轴/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /下载.*SRT/ }),
    ).not.toBeInTheDocument();
  });
});
