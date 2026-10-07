import { useCallback, useEffect, useRef, useState } from "react";
import {
  Captions,
  Clapperboard,
  History,
  PanelsTopLeft,
  Settings2,
  FolderOpen,
  ArrowUpRight,
  RefreshCw,
  Monitor,
  ShieldCheck,
  AudioLines,
  CircleHelp,
  Copy,
  Plus,
  Search,
  X,
} from "lucide-react";
import { createApi } from "./api";
import {
  Badge,
  Button,
  Empty,
  Notice,
  fileName,
  statusLabel,
  updatedLabel,
} from "./ui";
import Workspace from "./Workspace";
import AccountSettings from "./AccountSettings";
import Preview from "./Preview";
import RuntimeSettings from './RuntimeSettings';
import "./action-feedback.css";
import { isReviewDirty } from "./ManualReview";
import { hasPendingReviewSubmissions } from "./reviewSubmissions";

const navigation = [
  {
    id: "workspace",
    label: "字幕任务",
    display: "工作台",
    icon: PanelsTopLeft,
    heading: "新建字幕任务",
    description: "选择视频，先看样片，再继续整片和导出。",
  },
  {
    id: "history",
    label: "任务记录",
    icon: History,
    heading: "接着上次，继续创作。",
    description: "载入已有任务，复用已经保存的识别与翻译结果。",
  },
  {
    id: "preview",
    label: "结果预览",
    display: "字幕校对",
    icon: Clapperboard,
    heading: "字幕校对",
    description: "听原声，对照双语字幕；按句定位、重播与检查。",
  },
  {
    id: "settings",
    label: "API 设置",
    icon: Settings2,
    heading: "连接你熟悉的模型。",
    description: "在本机保存账户凭据，按需启用云端识别和翻译。",
  },
];

const emptyReview = { reviewed: "", timing: "", contentPassed: false, finalPassed: false };
const previewProjectKey = data => JSON.stringify([data?.source, data?.campaign, data?.project_id]);
const pendingReviewMessage = "校对提交仍在等待回执，请等待结果后再切换任务或开始处理。";

function projectEngine(data) {
  const provider = data?.project_config?.asr_provider;
  return data?.project_config?.engine || (provider === "qwen_asr" ? "bailian" : provider === "whisper_cpp" ? "local" : null);
}

function projectParameters(data) {
  return {
    language: "ja", target: "zh-CN", chunk_seconds: 120, workers: 1, translate: false,
    ...Object.fromEntries(Object.entries(data?.project_config || {}).filter(([key]) =>
      ["language", "target", "chunk_seconds", "workers", "translate"].includes(key),
    )),
  };
}

function rememberedReviewProject(memory, paths) {
  if (!memory?.reviewDraft) return null;
  try {
    const identity = JSON.parse(memory.projectKey);
    if (!Array.isArray(identity) || identity.length !== 3 || identity.some(value => typeof value !== "string" || !value)) return null;
    const [source, campaign, project_id] = identity;
    return source === paths.source && campaign === paths.campaign ? { source, campaign, project_id } : null;
  } catch { return null; }
}

export default function App() {
  const [api] = useState(createApi);
  const [page, setPage] = useState("workspace");
  const [historyQuery, setHistoryQuery] = useState("");
  const [engine, setEngine] = useState("local");
  const [exportEncoder,setExportEncoder]=useState('auto');
  const [snapshot, setSnapshot] = useState(null);
  const [paths, setPaths] = useState({
    source: "",
    campaign: "",
    baseline: "",
  });
  const [parameters, setParameters] = useState({
    language: "ja",
    target: "zh-CN",
    chunk_seconds: 120,
    workers: 1,
    translate: false,
  });
  const [connection, setConnection] = useState("connecting");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [pending, setPending] = useState("");
  const [openReceipt, setOpenReceipt] = useState(null);
  const [freshTask, setFreshTask] = useState(false);
  const [completion, setCompletion] = useState(null);
  const observedJob = useRef(null);
  const previewMemory = useRef(null);
  useEffect(() => {
    const preventDraftLoss = event => {
      if (!isReviewDirty(previewMemory.current?.reviewDraft) && !hasPendingReviewSubmissions(previewMemory)) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventDraftLoss);
    return () => window.removeEventListener("beforeunload", preventDraftLoss);
  }, []);
  const [reviewDraft, setReviewDraft] = useState({key: "", values: emptyReview});
  const reviewKey = JSON.stringify([paths.source, paths.campaign, snapshot?.project_id, snapshot?.campaign_status, !!snapshot?.job?.busy]);
  useEffect(() => { setReviewDraft({key:reviewKey, values:emptyReview}); }, [reviewKey]);
  const reviewValues = reviewDraft.key === reviewKey ? reviewDraft.values : emptyReview;
  const updateReview = (field, value) => setReviewDraft(previous => ({
    key: reviewKey,
    values: {...(previous.key === reviewKey ? previous.values : emptyReview), [field]:value},
  }));
  const initialized = useRef(false);
  const mounted = useRef(true);
  const lock = useRef(false);
  const stateReads = useRef({ issued: 0, applied: 0, epoch: 0, data: null });
  const lastDetailRead=useRef(0);

  const applyProject = useCallback((data, { restoreEngine = true } = {}) => {
    setPaths({
      source: data.source || "",
      campaign: data.campaign || "",
      baseline: data.baseline || "",
    });
    const savedEngine = projectEngine(data);
    if (restoreEngine && savedEngine) setEngine(savedEngine);
    else if (restoreEngine && !initialized.current) setEngine(data.accounts?.bailian?.ready && data.accounts?.deepseek?.ready ? "bailian" : "local");
    setParameters(projectParameters(data));
  }, []);

  const refresh = useCallback(async ({progress=false}={}) => {
    const reads = stateReads.current;
    const request = ++reads.issued;
    const epoch = reads.epoch;
    const current = () => mounted.current && epoch === reads.epoch && request > reads.applied;
    let data;
    try {
      data = await api.request(progress ? '/api/progress' : '/api/state');
    } catch (failure) {
      if (!current()) return reads.data;
      throw failure;
    }
    if (!current()) return reads.data;
    if(progress) {
      if(!data.project_id || data.project_id!==reads.data?.project_id || !data.job?.busy ||
        data.job.action!==reads.data?.job?.action || performance.now()-lastDetailRead.current>=4500) {
        return refresh();
      }
      data={...reads.data,job:data.job,persistence_warning:data.persistence_warning};
    } else lastDetailRead.current=performance.now();
    // Failed reads never advance this boundary: an older valid read may still help.
    reads.applied = request;
    reads.data = data;
    const identity = data.project_id || JSON.stringify([data.source,data.campaign]);
    const previous = observedJob.current;
    if (previous?.identity !== identity || data.job?.busy) setCompletion(null);
    else if (previous.busy && !data.job?.busy) {
      const problem = ['needs_attention','failed','error','cancelled','stopped','asr_incomplete','translation_incomplete','samples_incomplete','full_incomplete'].includes(data.job?.status);
      setCompletion({identity, problem, message:data.job?.message || statusLabel(data.job?.status)});
    }
    observedJob.current = {identity, busy:!!data.job?.busy};
    setSnapshot(data);
    setConnection("connected");
    if (!initialized.current) {
      applyProject(data);
      initialized.current = true;
    }
    return data;
  }, [api, applyProject]);

  const connect = useCallback(async () => {
    setConnection("connecting");
    setError("");
    try {
      await api.connect();
      await refresh();
    } catch (failure) {
      if (mounted.current) {
        setError(failure.message);
        setConnection("offline");
      }
    }
  }, [api, refresh]);

  useEffect(() => {
    mounted.current = true;
    connect();
    return () => {
      mounted.current = false;
    };
  }, [connect]);

  useEffect(() => {
    if (connection !== "connected") return undefined;
    let cancelled = false;
    let timer;
    const poll = async () => {
      try {
        await refresh({progress:!!snapshot?.job?.busy});
      } catch (failure) {
        if (!cancelled) {
          setConnection("offline");
          setError(failure.message);
        }
      }
      if (!cancelled)
        timer = setTimeout(poll, snapshot?.job?.busy ? 1100 : 4500);
    };
    timer = setTimeout(poll, snapshot?.job?.busy ? 1100 : 4500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [connection, refresh, snapshot?.job?.busy]);

  const perform = async (name, operation, message = "", {refreshAfter = true} = {}) => {
    if (lock.current) return false;
    lock.current = true;
    setPending(name);
    setError("");
    setSuccess("");
    try {
      await operation();
      if (refreshAfter) await refresh();
      setSuccess(message);
      return true;
    } catch (failure) {
      setError(failure.message);
      return false;
    } finally {
      lock.current = false;
      setPending("");
    }
  };

  const reviewSaved = () => {
    if (hasPendingReviewSubmissions(previewMemory)) {
      setError(pendingReviewMessage);
      setPage('preview');
      return false;
    }
    if (!isReviewDirty(previewMemory.current?.reviewDraft)) return true;
    setError('校对内容尚未保存，请先保存或放弃修改，再切换任务或开始处理。');
    setPage('preview');
    return false;
  };

  const run = (action, values = {}) =>
    reviewSaved() && perform(action, async () => {
      setCompletion(null);
      const project = await api.request("/api/project", {...paths,...(freshTask ? {new_task:true} : {})});
      stateReads.current.epoch += 1;
      if (project?.source !== undefined) applyProject(project, { restoreEngine: false });
      if (action === 'prepare' || action === 'local') setParameters(previous => ({...previous,...values}));
      setFreshTask(false);
      await api.request("/api/run", { action, ...values,
        ...(['export','export-draft'].includes(action) ? {encoder:exportEncoder} : {}),
        project_id: project?.project_id || snapshot?.project_id });
      stateReads.current.epoch += 1;
      if (action === 'export' || action === 'export-draft') setPage('workspace');
    });

  const pick = (kind) =>
    perform(`pick-${kind}`, async () => {
      const result = await api.request("/api/pick", { kind });
      if (result.path)
        changePaths((previous) => ({ ...previous, [kind]: result.path }));
    });

  const changePaths = (update) => {
    const next = typeof update === "function" ? update(paths) : update;
    if ((next.source !== paths.source || next.campaign !== paths.campaign) && !reviewSaved()) return;
    if (next.source !== paths.source) {
      previewMemory.current = null;
      setFreshTask(true);
      setPaths({ ...next, campaign: "", baseline: "" });
    } else setPaths(next);
  };

  const newTask = () => {
    if (!reviewSaved()) return;
    previewMemory.current = null;
    setCompletion(null);
    setOpenReceipt(null);
    setPaths({source:"",campaign:"",baseline:""});
    setFreshTask(true);
    setParameters({language:"ja",target:"zh-CN",chunk_seconds:120,workers:1,translate:false});
    setEngine(snapshot?.accounts?.bailian?.ready && snapshot?.accounts?.deepseek?.ready ? "bailian" : "local");
    setError("");setSuccess("");setPage("workspace");
  };

  const restoreProject = async (path) => {
    if (!reviewSaved()) throw new Error(hasPendingReviewSubmissions(previewMemory)
      ? pendingReviewMessage : '校对内容尚未保存，请先保存或放弃修改，再切换任务。');
    await api.request("/api/project", { campaign: path });
    stateReads.current.epoch += 1;
    applyProject(await refresh());
    setFreshTask(false);
    setPage("workspace");
  };

  const recoverReview = () => perform("recover-review", async () => {
    const project = rememberedReviewProject(previewMemory.current, paths);
    if (!project) throw new Error("原校对任务与当前窗口路径不一致，已保留校对内容。");
    if (hasPendingReviewSubmissions(previewMemory)) throw new Error(pendingReviewMessage);
    if (snapshot?.job?.busy) throw new Error("请等待当前服务任务停止后，再恢复原校对任务。");
    const expected = previewProjectKey(project);
    const selected = await api.request("/api/project", { campaign: project.campaign });
    stateReads.current.epoch += 1;
    if (previewProjectKey(selected) !== expected) throw new Error("恢复返回的项目与原校对任务不一致，校对内容已保留，请重试。");
    const data = await refresh();
    if (previewProjectKey(data) !== expected) throw new Error("恢复期间服务项目发生变化，校对内容已保留，请重试。");
    applyProject(data);
    setFreshTask(false);
    setPage("preview");
  }, "已恢复原校对任务。", { refreshAfter: false });

  const loadProject = (path = paths.campaign) =>
    perform(
      "load",
      () => restoreProject(path),
      "任务已载入，可以继续制作。",
    );

  const pickProject = () => perform("pick-load", async () => {
    const result = await api.request("/api/pick", { kind: "campaign" });
    if (result.path) await restoreProject(result.path);
  });

  const openContext = JSON.stringify([snapshot?.project_id, paths.source, paths.campaign]);
  const shownOpenReceipt = openReceipt?.context === openContext ? openReceipt : null;
  const open = (target, project = snapshot?.project_id) =>
    perform(`open-${target}`, async () => {
      setCompletion(null);
      const fallback = target === 'project' && project === snapshot?.project_id ? snapshot?.campaign || '' : '';
      const receipt = {context:openContext, target, path:fallback, status:'pending', copyMessage:''};
      setOpenReceipt(receipt);
      const controller = new AbortController();
      let timer;
      let timedOut = false;
      try {
        const timeout = new Promise((_, reject) => {
          timer = setTimeout(() => {
            timedOut = true;
            reject(new Error('打开请求等待超过 15 秒，结果未知。请先检查任务栏；程序不会自动重试。'));
            controller.abort();
          }, 15000);
        });
        const result = await Promise.race([
          api.request('/api/open', {target, ...(project ? {project_id:project} : {})}, {signal:controller.signal}),
          timeout,
        ]);
        if (result?.opened === false) throw new Error('打开请求未完成，请检查目录或文件是否仍然存在。');
        const path = typeof result?.path === 'string' && result.path.trim() ? result.path : fallback;
        setOpenReceipt({...receipt, path, status:'success'});
      } catch (failure) {
        setOpenReceipt({...receipt, status:timedOut ? 'unknown' : 'error'});
        throw failure;
      } finally {
        clearTimeout(timer);
      }
    }, '', {refreshAfter:false});
  const copyOpenPath = async () => {
    const receipt = shownOpenReceipt;
    if (!receipt?.path) return;
    let copyMessage;
    try {
      await navigator.clipboard.writeText(receipt.path);
      copyMessage = '路径已复制';
    } catch {
      copyMessage = '无法访问剪贴板，请选中路径后按 Ctrl+C。';
    }
    setOpenReceipt(previous => previous === receipt ? {...previous, copyMessage} : previous);
  };
  const active = navigation.find((item) => item.id === page);
  const disabled = !!pending || connection !== "connected";
  const busy = !!snapshot?.job?.busy;
  const draft = paths.source !== (snapshot?.source || "") || paths.campaign !== (snapshot?.campaign || "");
  const reviewPending = hasPendingReviewSubmissions(previewMemory);
  const recoveryProject = rememberedReviewProject(previewMemory.current, paths);
  const detachedReview = !!snapshot && !!previewMemory.current?.reviewDraft &&
    previewMemory.current.projectKey !== previewProjectKey(snapshot) &&
    (!!recoveryProject || isReviewDirty(previewMemory.current.reviewDraft) || reviewPending);
  // Only change the displayed task; preserve this window's paths and drafts.
  const showRunningTask = busy && (draft || detachedReview);
  const workspacePaths = showRunningTask
    ? { source: snapshot.source, campaign: snapshot.campaign, baseline: snapshot.baseline || "" } : paths;
  const workspaceEngine = showRunningTask ? projectEngine(snapshot) || engine : engine;
  const workspaceParameters = showRunningTask ? projectParameters(snapshot) : parameters;
  const isReview = page === "preview";
  const projectTitle = fileName(page === "workspace" ? workspacePaths.source : paths.source);
  const normalizedHistoryQuery = historyQuery.trim().normalize("NFKC").toLocaleLowerCase();
  const recentProjects = snapshot?.recent || [];
  const filteredProjects = recentProjects.filter(item =>
    `${item.title || ""}\n${item.path || ""}`.normalize("NFKC").toLocaleLowerCase().includes(normalizedHistoryQuery),
  );
  const previewProps = {api, snapshot, connection, onError:setError, open, disabled, run, sessionMemory:previewMemory, onReviewChanged:refresh, toWorkspace:()=>setPage("workspace")};

  return (
    <div className={`app-shell studio-layout ${isReview ? "review-shell" : "workbench-shell"}`}>
      <aside className="sidebar">
        <a
          href="#"
          className="brand"
          onClick={(event) => {
            event.preventDefault();
            setPage("workspace");
          }}
          aria-label="字幕工坊首页"
        >
          <span className="brand-icon">
            <Captions size={26} strokeWidth={1.9} />
          </span>
          <span>
            <strong>字幕工坊</strong>
            <small>SUBTITLE STUDIO</small>
          </span>
        </a>
        <Button className="sidebar-create" variant="primary" icon={Plus} disabled={disabled || busy} onClick={newTask}>新建任务</Button>
        <nav aria-label="主导航">
          {navigation.map(({ id, label, display, icon: Icon }) => (
            <button
              key={id}
              aria-label={label}
              title={label}
              className={`nav-item ${id === page ? "is-active" : ""}`}
              aria-current={id === page ? "page" : undefined}
              onClick={() => {
                setPage(id);
                setSuccess("");
              }}
            >
              <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
              <span>{display || label}</span>
              {id === page && <span className="nav-indicator" />}
            </button>
          ))}
        </nav>
        <section className="sidebar-projects" aria-label="最近项目">
          <div className="nav-caption">最近项目 <span>{snapshot?.recent?.length || 0}</span></div>
          {snapshot?.recent?.length ? snapshot.recent.slice(0, 4).map((item, index) => (
            <button key={`${item.path}-${index}`} className={`sidebar-project ${!draft && paths.campaign === item.path ? 'is-selected' : ''}`}
              aria-label={`载入任务 ${item.title || fileName(item.path)}`} title={item.path}
              disabled={disabled || busy || item.missing} onClick={() => loadProject(item.path)}>
              <FolderOpen size={16} /><span><strong>{item.title || fileName(item.path)}</strong><small>{item.missing ? '目录不存在' : statusLabel(item.status)}</small></span>
            </button>
          )) : <p className="sidebar-empty">创建任务后，可从这里继续。</p>}
        </section>
        <div className="sidebar-bottom">
          <div className="local-note">
            <ShieldCheck size={20} />
            <strong>进度保存在本机</strong>
            <p>
              云端识别上传音频，
              <br />
              云端翻译发送字幕文字。
            </p>
          </div>
          <div className="desktop-label">
            <Monitor size={14} /> 桌面工作台 <span>v1.0</span>
          </div>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            工作空间 <span>/</span> <strong>{active.display || active.label}</strong>
          </div>
          <div className="connection">
            <span className={`connection-dot ${connection}`} />
            {connection === "connected"
              ? "本地服务已连接"
              : connection === "connecting"
                ? "连接本地服务…"
                : "本地服务未连接"}
            {connection === "offline" && <Button icon={RefreshCw} onClick={connect}>重新连接</Button>}
          </div>
        </header>
        <main className="main-content">
          <div className="page-heading">
            <div>
              <h1 title={projectTitle}>{["workspace", "preview"].includes(page) && projectTitle ? projectTitle : active.heading}</h1>
              <p>{page === "workspace" && projectTitle && (!draft || showRunningTask) ? "查看制作进度，校对字幕并保存结果。" : active.description}</p>
            </div>
            {page !== "settings" && (<div className="heading-actions">
              {isReview && <Button icon={PanelsTopLeft} onClick={()=>setPage('workspace')}>返回工作台</Button>}
              <Button
                icon={FolderOpen}
                busy={pending === 'open-project'}
                aria-busy={pending === 'open-project'}
                disabled={disabled || (draft && !showRunningTask) || !snapshot?.campaign}
                title={showRunningTask ? `打开正在处理的项目目录：${snapshot.campaign}` : undefined}
                onClick={() => open("project")}
              >
                {pending === 'open-project' ? '正在打开…' : '打开项目目录'}
              </Button>
            </div>)}
          </div>

          {shownOpenReceipt && (
            <section className="open-location-receipt" role="status" aria-label="打开位置反馈">
              <div className="open-location-summary">
                <strong>{shownOpenReceipt.status === 'pending' ? '正在请求打开…'
                  : shownOpenReceipt.status === 'success'
                    ? ['project','exports','source-folder','video-folder'].includes(shownOpenReceipt.target)
                      ? '已请求在文件资源管理器中打开。若未看到窗口，请检查任务栏。'
                      : '已请求在默认应用中打开。若未看到窗口，请检查任务栏。'
                    : shownOpenReceipt.status === 'unknown' ? '打开结果尚未确认，可复制路径自行查看。'
                      : '打开请求未完成，可复制路径自行查看。'}</strong>
                <button className="notice-dismiss" aria-label="关闭打开位置反馈" onClick={() => setOpenReceipt(null)}>×</button>
              </div>
              {shownOpenReceipt.path && <div className="open-location-path">
                <input aria-label="打开位置路径" readOnly value={shownOpenReceipt.path} onFocus={event => event.target.select()} />
                <Button icon={Copy} onClick={copyOpenPath}>复制路径</Button>
              </div>}
              {shownOpenReceipt.copyMessage && <small>{shownOpenReceipt.copyMessage}</small>}
            </section>
          )}
          {(error || success || (completion && !draft)) && <section className="app-action-feedback" aria-label="操作反馈">
          {error && (
            <Notice
              tone="error"
              action={
                  <button
                    className="notice-dismiss"
                    aria-label="关闭错误提示"
                    onClick={() => setError("")}
                  >
                    ×
                  </button>
              }
            >
              {error}
            </Notice>
          )}
          {success && <div role="status"><Notice tone="success" action={<button className="notice-dismiss" aria-label="关闭成功提示" onClick={() => setSuccess("")}>×</button>}>{success}</Notice></div>}
          {completion && !draft && <div role="status"><Notice tone={completion.problem ? 'warning' : 'success'} title={completion.problem ? '本次任务需要处理' : '本次任务已结束'} action={<div className="completion-actions"><Button onClick={()=>setPage(completion.problem ? 'workspace' : 'preview')}>{completion.problem ? '查看任务与日志' : '查看本次结果'}</Button><button className="text-button" onClick={()=>setCompletion(null)} aria-label="关闭任务结束提示">关闭</button></div>}>
            {completion.message}
          </Notice></div>}
          </section>}
          {showRunningTask && !detachedReview && <Notice tone="warning" title="正在显示后台运行的任务">
            当前服务正在处理 {fileName(snapshot.source)}；本窗口准备的素材与参数已保留，任务结束后会恢复显示。
          </Notice>}
          {detachedReview && <Notice tone="warning" title="服务当前任务已变化，原校对内容已保留。" action={
            <div className="completion-actions">
              {recoveryProject && <Button disabled={disabled || busy || reviewPending} busy={pending === "recover-review"} onClick={recoverReview}>恢复原校对任务</Button>}
              {busy && page !== "workspace" && <Button onClick={() => setPage("workspace")}>查看当前任务进度</Button>}
            </div>
          }>
            {busy ? `当前服务正在处理 ${fileName(snapshot.source)}；停止后可恢复原校对任务。`
              : "恢复原校对任务后，可以继续保存或放弃修改。"}
          </Notice>}
          {snapshot?.persistence_warning && <Notice tone="warning">{snapshot.persistence_warning}</Notice>}
          {connection === "connecting" && !snapshot && (
            <div className="connecting-state">
              <RefreshCw className="spin" size={16} />{" "}
              正在读取本机任务与账户配置…
            </div>
          )}

          <RuntimeSettings api={api} connection={connection} encoder={exportEncoder}
            onEncoderChange={setExportEncoder} disabled={disabled || busy}/>
          {page === "workspace" && (
            <Workspace
              engine={workspaceEngine}
              setEngine={setEngine}
              snapshot={snapshot}
              paths={workspacePaths}
              setPaths={changePaths}
              parameters={workspaceParameters}
              setParameters={setParameters}
              disabled={disabled}
              pending={pending}
              busy={busy}
              pick={pick}
              run={run}
              open={open}
              loadProject={loadProject}
              stop={() =>
                perform(
                  "stop",
                  () => api.request("/api/stop", {}),
                  "已发送停止请求，当前片段结束后将保留已完成结果。",
                )
              }
              toSettings={() => setPage("settings")}
              toPreview={showRunningTask ? undefined : () => setPage("preview")}
              reviewValues={reviewValues}
              updateReview={updateReview}
              preview={!draft && !detachedReview && snapshot?.source ? <Preview {...previewProps} variant="workbench" toReview={()=>setPage('preview')} /> : null}
            />
          )}

          {page === "settings" && (
            <AccountSettings
              snapshot={snapshot}
              disabled={disabled || busy}
              pending={pending}
              save={(name, path, body, message) =>
                perform(name, () => api.request(path, body), message)
              }
            />
          )}

          {page === "history" && (
            <section className="card history-card">
              <div className="section-heading">
                <div>
                  <h2>最近的任务</h2>
                  <p>任务进度与结果随项目保存</p>
                </div>
                <Badge>{snapshot?.recent?.length || 0} 个任务</Badge>
              </div>
              {recentProjects.length > 0 && <div className="history-tools">
                <label className="history-search"><Search size={17} aria-hidden="true" />
                  <input type="search" aria-label="搜索任务" placeholder="搜索任务名称或文件夹路径" value={historyQuery} onChange={event=>setHistoryQuery(event.target.value)} />
                  {historyQuery && <button type="button" aria-label="清空任务搜索" onClick={()=>setHistoryQuery("")}><X size={16} /></button>}
                </label>
                <span role="status" aria-live="polite">显示 {filteredProjects.length} / {recentProjects.length} 个任务</span>
              </div>}
              {filteredProjects.length ? (
                <div className="history-list">
                  {filteredProjects.map((item, index) => (
                    <div className="history-row" key={`${item.path}-${index}`}>
                      <div className="file-icon">
                        <Captions size={23} />
                      </div>
                      <div className="history-title">
                        <h3 title={item.title || fileName(item.path)}>{item.title || fileName(item.path)}</h3>
                        <p title={item.path}>{item.path}</p>
                        <small>{updatedLabel(item.updated)}</small>
                      </div>
                      <Badge
                        tone={
                          item.missing || item.status?.includes("ready") ? "warning" : "neutral"
                        }
                      >
                        {item.missing ? '目录已移动或不存在' : statusLabel(item.status)}
                      </Badge>
                      <Button
                        icon={ArrowUpRight}
                        disabled={disabled || busy || item.missing}
                        onClick={() => loadProject(item.path)}
                      >
                        载入任务
                      </Button>
                    </div>
                  ))}
                </div>
              ) : recentProjects.length > 0 ? (
                <Empty icon={Search} title="没有匹配的任务">试试素材名称或项目路径中的关键词。</Empty>
              ) : (
                <Empty
                  icon={History}
                  title="还没有任务记录"
                  action={
                    <Button
                      variant="primary"
                      onClick={() => setPage("workspace")}
                    >
                      创建第一个字幕任务
                    </Button>
                  }
                >
                  选择素材并开始制作后，任务会出现在这里。
                </Empty>
              )}
              <div className="history-load">
                <FolderOpen size={20} />
                <div>
                  <strong>已经有项目文件夹？</strong>
                  <p>从本机选择目录，继续以前的字幕任务。</p>
                </div>
                <Button
                  disabled={disabled || busy}
                  busy={pending === "pick-load"}
                  onClick={pickProject}
                >
                  选择并载入
                </Button>
              </div>
            </section>
          )}

          {page === "preview" && draft && !detachedReview && <section className="card"><Empty icon={Clapperboard} title="新任务尚未创建" action={<Button onClick={()=>setPage('workspace')}>返回任务准备</Button>}>先准备样片或开始本地识别，再预览新素材的结果。</Empty></section>}
          {page === "preview" && !draft && !detachedReview && (
            <Preview {...previewProps} />
          )}

          <footer className="page-footer">
            <span>
              <AudioLines size={14} /> 字幕工坊 · 本地工作空间
            </span>
            <span>
              <CircleHelp size={14} /> 生成结果需要人工核对
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}
