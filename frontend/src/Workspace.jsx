import {
  ArrowRight,
  ArrowUpRight,
  AudioLines,
  Check,
  CheckCheck,
  ChevronRight,
  CircleCheck,
  Clapperboard,
  Cloud,
  Cpu,
  FileAudio,
  FileText,
  FolderOpen,
  ListChecks,
  LoaderCircle,
  Play,
  Settings2,
  Square,
  Terminal,
  Timer,
  WandSparkles,
} from "lucide-react";
import {
  Badge,
  Button,
  Empty,
  Notice,
  SectionHeading,
  fileName,
  statusLabel,
} from "./ui";
import "./file-layout.css";

function FileLocations({ snapshot, paths, isDraft, disabled, open }) {
  const layout = isDraft ? null : snapshot?.file_layout;
  const created = layout?.created_at ? new Date(layout.created_at) : null;
  const hasCreated = created && Number.isFinite(created.getTime());
  const rows = [
    ["输入素材", paths.source, "source-folder", "打开素材目录"],
    ["任务与进度", layout?.project_path || paths.campaign, "project", "打开任务目录"],
    ["字幕版本", layout?.exports_path, null, null],
    ["已导出成片", layout?.video_path, "video-folder", "打开成片位置"],
  ].filter(([, value]) => value);
  return <section className="file-locations" aria-label="文件位置">
    <details className="file-location-details">
    <summary className="file-locations-heading"><span><FolderOpen size={14} /><strong>文件位置与输出</strong><ChevronRight size={14} className="details-chevron" /></span>{hasCreated && <span>创建于 <time aria-label="任务创建时间" dateTime={layout.created_at}>{created.toLocaleString("zh-CN", { hour12: false })}</time></span>}</summary>
    {rows.length > 0 && <dl>{rows.map(([label, value, target, action]) => <div className="file-location-row" key={label}>
      <dt>{label}</dt><dd title={value}>{value}</dd>
      {target && !isDraft && (target !== "source-folder" || layout) && <Button icon={FolderOpen} disabled={disabled} onClick={() => open(target, snapshot.project_id)} aria-label={action}>打开</Button>}
    </div>)}</dl>}
    </details>
    {(isDraft || !paths.campaign) && <p>{snapshot?.file_layout ? "项目目录留空时，新任务按日期与创建时间自动命名。素材保留在原位置。" : "项目目录留空时，自动创建独立任务。素材保留在原位置。"}</p>}
  </section>;
}

const engines = [
  {
    id: "local",
    title: "本地识别",
    model: "Whisper · small",
    detail: "离线转写，效果和速度受本机模型限制",
    tag: "本机运行",
    icon: Cpu,
  },
  {
    id: "siliconflow",
    title: "硅基流动样片",
    model: "Qwen3 ASR · 1.7B",
    detail: "仅试听文字，比较识别效果",
    tag: "样片试听",
    icon: AudioLines,
  },
  {
    id: "bailian",
    title: "百炼完整流程",
    model: "Qwen ASR + DeepSeek",
    detail: "识别、翻译与人工验收逐步完成",
    tag: "推荐 · 双语字幕",
    icon: Cloud,
  },
];

const incompleteStatuses = ["asr_incomplete", "translation_incomplete", "samples_incomplete", "full_incomplete"];
const interruptedStatuses = ["needs_attention", "failed", "error", "cancelled", "stopped", ...incompleteStatuses];
const resumeLabels = {
  local: "继续本地任务",
  prepare: "继续准备样片",
  samples: "继续生成双语样片",
  "siliconflow-pilot": "继续样片试听",
  full: "继续处理整片",
};

function PathField({
  label,
  kind,
  value,
  placeholder,
  disabled,
  onChange,
  pick,
  aside,
}) {
  return (
    <div className="path-field">
      <label htmlFor={`path-${kind}`}>
        {label}
        {aside && <span>{aside}</span>}
      </label>
      <div className="input-with-button">
        <input
          id={`path-${kind}`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          spellCheck="false"
        />
        <Button
          icon={FolderOpen}
          disabled={disabled}
          onClick={() => pick(kind)}
          aria-label={`选择${label}`}
        >
          选择
        </Button>
      </div>
    </div>
  );
}

const exportPhases = {
  preparing: { label: "准备视频编码", detail: "正在准备字幕与视频编码。" },
  encoding: { label: "视频编码中", detail: "正在压入字幕并编码视频，原视频保留。" },
  validating: { label: "校验音轨与视频", detail: "编码已结束，正在校验输出视频和音轨。" },
  publishing: { label: "保存成片", detail: "校验已通过，正在保存带字幕的成片。" },
  done: { label: "正在完成导出", detail: "成片已保存，等待后台确认任务完成。" },
};

function exportPhaseDetails(progress) {
  return typeof progress?.phase === "string" && Object.hasOwn(exportPhases, progress.phase)
    ? exportPhases[progress.phase] : null;
}

function formatRemainingTime(seconds) {
  const rounded = Math.ceil(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  return [hours > 0 && `${hours} 小时`, minutes > 0 && `${minutes} 分`, (remainder > 0 || rounded === 0) && `${remainder} 秒`].filter(Boolean).join(" ");
}

function ExportProgress({ progress }) {
  const phase = exportPhaseDetails(progress);
  if (!phase || progress.phase !== "encoding") {
    return <div className="pending-progress"><LoaderCircle size={14} className="spin" />{phase?.label || "视频编码中，请稍候"}</div>;
  }
  const percent = Number.isFinite(progress.percent) && progress.percent >= 0 && progress.percent <= 100 ? progress.percent : null;
  const speed = Number.isFinite(progress.speed) && progress.speed > 0 ? progress.speed : null;
  const eta = Number.isFinite(progress.eta_seconds) && progress.eta_seconds >= 0 ? progress.eta_seconds : null;
  return <div className="real-progress">
    <div><span>{phase.label}</span><strong>{percent === null ? "等待编码进度" : `${Number(percent.toFixed(1))}%`}</strong></div>
    <progress aria-label="视频编码进度" value={percent ?? undefined} max={100} />
    {(speed !== null || eta !== null) && <div>
      <span>{speed !== null && <>编码速度 <strong>{speed.toFixed(1)}×</strong></>}</span>
      {eta !== null && <span>预计剩余 {formatRemainingTime(eta)}</span>}
    </div>}
  </div>;
}

function ProgressPanel({ job = {}, status, busy, toPreview, translate, budget, stage, disabled, pending, stop, stopTitle, engine, nextStep, previewRecommended }) {
  const total = Number(job.total) || 0;
  const recognized = Number(job.recognized) || 0;
  const translated = Number(job.translated) || 0;
  const logs = Array.isArray(job.logs) ? job.logs : [];
  const waitingReview = ["samples_ready", "full_ready"].includes(status);
  const exporting = busy && job.busy === true && ["export", "export-draft"].includes(job.action);
  return (
    <aside className={`task-side ${busy ? 'is-busy' : ''}`}>
      <section className="card progress-card">
        <div className="section-heading">
          <h2>制作进度</h2>
          <Badge
            tone={busy ? "primary" : waitingReview ? "warning" : "neutral"}
          >
            {busy ? "正在运行" : statusLabel(status)}
          </Badge>
        </div>
        {stage && stage !== status && <p className="stage-context">项目阶段：{statusLabel(stage)}</p>}
        {nextStep || <><h3>
          {busy
            ? job.message || "正在处理任务"
            : waitingReview
              ? "轮到你来检查了"
              : job.message || "准备好了，就开始吧"}
        </h3>
        <p>
          {busy
            ? "进度来自正在运行的后台任务"
            : waitingReview
              ? "机器生成完成后，人工验收让字幕更可靠。"
              : "选择素材和识别方式，开始制作字幕。"}
        </p></>}
        {total > 0 && !exporting && (
          <div className="real-progress">
            <div>
              <span>已识别片段</span>
              <strong>
                {recognized} <small>/ {total}</small>
              </strong>
            </div>
            <progress aria-label="已识别片段" value={recognized} max={total} />
            {translate ? <><div>
              <span>已翻译片段</span>
              <strong>
                {translated} <small>/ {total}</small>
              </strong>
            </div>
            <progress aria-label="已翻译片段" value={translated} max={total} />
            </> : <p className="options-hint">未启用翻译</p>}
          </div>
        )}
        {exporting && <ExportProgress progress={job.export_progress} />}
        {busy && total === 0 && !exporting && (
          <div className="pending-progress">
            <LoaderCircle size={14} className="spin" />
            等待后台报告分段进度
          </div>
        )}
        {(busy || !previewRecommended) && <div className="progress-bottom">
          {busy && <Button className="full-width" icon={Square} variant="danger-quiet" disabled={disabled} busy={pending === 'stop'} onClick={stop} title={stopTitle}>停止任务</Button>}
          {!previewRecommended && <Button
            className="full-width"
            icon={Clapperboard}
            disabled={disabled || !toPreview}
            title={!toPreview ? "后台正在处理另一个任务；结束后请载入该任务查看字幕结果。" : undefined}
            onClick={toPreview}
          >
            查看字幕结果 <ArrowUpRight size={14} />
          </Button>}
        </div>}
        {!exporting && <div className="provider-summary"><Settings2 size={14} /><span>{engines.find(item => item.id === engine)?.title}<small>{engine === 'bailian' ? 'Qwen ASR · DeepSeek 翻译' : engine === 'siliconflow' ? 'Qwen3 ASR · 仅试听文字' : 'Whisper · 本机运行'}</small></span></div>}
        {budget && <details className="budget-details"><summary><span>费用与预算</span><strong>¥{Number(budget.spent_cny).toFixed(4)} <small>/ ¥{Number(budget.budget_cny).toFixed(2)}</small></strong><ChevronRight size={14} className="details-chevron" /></summary><div className="budget-summary" aria-label="费用估算">
          <div><span>{budget.scope === 'shared' ? '共享账本累计' : '本任务累计'}</span><strong>¥{Number(budget.spent_cny).toFixed(4)}</strong></div>
          <div><span>在途预留</span><span>¥{Number(budget.reserved_cny).toFixed(4)}</span></div>
          <div><span>预算上限</span><span>¥{Number(budget.budget_cny).toFixed(2)}</span></div>
          <small>按用量估算，达到 ¥{Number(budget.stop_cny).toFixed(2)} 停止追加。{budget.scope === 'shared' && '含使用同一账本的样片与其他验证。'}</small>
        </div></details>}
      </section>
      <details className="card logs-card" id="task-logs" key={status} open={['needs_attention','failed','error', ...incompleteStatuses].includes(status)}>
        <summary>
          <span><Terminal size={16} /> 运行日志</span>
          <span className="live-label">
            {busy && <span className="connection-dot connected" />}{busy ? '实时更新' : `${logs.length} 条记录`}
          </span>
        </summary>
        <div
          className="log-lines"
          role="log"
          aria-label="后台运行日志"
          aria-live="polite"
        >
          {logs.length ? (
            logs
              .slice(-100)
              .map((entry, index) => (
                <p key={index}>
                  {typeof entry === "string"
                    ? entry
                    : entry.message || JSON.stringify(entry)}
                </p>
              ))
          ) : (
            <div className="logs-empty">
              <span>还没有运行记录</span>
              <p>
                任务开始后，会在这里显示
                <br />
                每一个处理步骤。
              </p>
            </div>
          )}
        </div>
      </details>
      <div className="workflow-note">
        <CircleCheck size={17} />
        <p>
          已完成的片段会保留。
          <br />
          中途停止后，可以继续原任务。
        </p>
      </div>
    </aside>
  );
}

export default function Workspace({
  engine,
  setEngine,
  snapshot,
  paths,
  setPaths,
  parameters,
  setParameters,
  disabled,
  pending,
  busy,
  pick,
  run,
  open,
  loadProject,
  stop,
  toSettings,
  toPreview,
  reviewValues,
  updateReview,
  preview,
}) {
  const { reviewed, timing, contentPassed, finalPassed } = reviewValues;
  const setReviewed = value => updateReview('reviewed', value);
  const setTiming = value => updateReview('timing', value);
  const setContentPassed = value => updateReview('contentPassed', value);
  const setFinalPassed = value => updateReview('finalPassed', value);
  const isDraft = paths.source !== (snapshot?.source || '') || paths.campaign !== (snapshot?.campaign || '');
  const stage = isDraft ? '' : snapshot?.campaign_status || '';
  const runStatus = isDraft ? 'idle' : snapshot?.job?.status;
  const interruptedStatus = interruptedStatuses.includes(runStatus) ? runStatus : incompleteStatuses.includes(stage) ? stage : null;
  const interrupted = !!interruptedStatus;
  const status = busy ? runStatus || 'running' : interruptedStatus || (runStatus === 'draft_exported' ? runStatus : stage || runStatus || 'idle');
  const actions = snapshot?.actions || {};
  const locked = disabled || busy;
  const updatePath = (kind) => (value) =>
    setPaths((previous) => ({ ...previous, [kind]: value }));
  const updateParameter = (name, value) =>
    setParameters((previous) => {
      const next={...previous,[name]:value};
      if(name==='language' && (previous.target==='zh-CN'?'zh':previous.target)===value) next.target=value==='zh'?'en':'zh-CN';
      return next;
    });
  const handleEngineKeys = (event) => {
    if (!['ArrowRight','ArrowDown','ArrowLeft','ArrowUp','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const options = Array.from(event.currentTarget.parentElement.querySelectorAll('[role="radio"]:not(:disabled)'));
    if (!options.length) return;
    const current = options.indexOf(event.currentTarget);
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
      : (current + (['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1) + options.length) % options.length;
    options[index].focus();
    options[index].click();
  };
  const savedEngine = !isDraft && (snapshot?.project_config?.engine || (snapshot?.project_config?.asr_provider === 'qwen_asr' ? 'bailian' : snapshot?.project_config?.asr_provider === 'whisper_cpp' ? 'local' : ''));
  const isSavedLocal = !isDraft && !!snapshot?.project_config?.model;
  const sourceReady = !!paths.source.trim();
  const can = (action) => !locked && sourceReady && (isDraft ? action === 'prepare' : actions[action] === true);
  const reviewCountValid = reviewed !== '' && Number.isSafeInteger(Number(reviewed)) && Number(reviewed) >= 20;
  const timingCountValid = timing !== '' && Number.isSafeInteger(Number(timing)) && Number(timing) >= 0 && Number(timing) <= Number(reviewed);
  const timingRate = reviewCountValid && timingCountValid ? Number(timing) / Number(reviewed) * 100 : null;
  const validReview = reviewCountValid && timingCountValid && Number(timing) * 10 >= Number(reviewed) * 9 && contentPassed;
  const reviewHint = !reviewCountValid ? '还需填写实际抽检句数（至少 20 句）'
    : !timingCountValid ? '时间合格句数须为 0 到抽检总数之间的整数'
    : Number(timing) * 10 < Number(reviewed) * 9 ? `时间合格率 ${timingRate.toFixed(1)}%，未达到 90%`
    : !contentPassed ? '时间检查已达标，请听看确认内容后勾选通过'
    : '填写已达标，点击下方按钮才会记录验收';

  if (!snapshot)
    return (
      <section className="card">
        <Empty icon={MonitorIcon} title="等待本地工作台连接">
          连接成功后，将读取你的素材、任务进度和账户状态。
        </Empty>
      </section>
    );

  const setupMissing = engine === 'siliconflow'
    ? !snapshot.accounts?.siliconflow?.ready
    : engine === 'bailian' && !(snapshot.accounts?.bailian?.ready && snapshot.accounts?.deepseek?.ready);
  const exporting = !isDraft && busy && snapshot.job?.busy === true && ['export','export-draft'].includes(snapshot.job?.action);
  const exportPhase = exporting ? exportPhaseDetails(snapshot.job?.export_progress) : null;
  let continuation = null;
  if (interrupted && !isDraft) {
    const previousAction = snapshot.job?.action;
    if (['export', 'export-draft'].includes(previousAction) && actions[previousAction] === true) {
      continuation = [previousAction === 'export-draft' ? '继续导出草稿 MP4' : '继续导出审核版 MP4',
        '到结果预览继续导出，已保存的字幕和原视频保留。', 'preview'];
    } else {
      const action = engine === 'local' ? 'local'
        : [runStatus, stage].includes('full_incomplete') ? engine === 'bailian' ? 'full' : null
        : [runStatus, stage].includes('samples_incomplete') ? engine === 'siliconflow' ? 'siliconflow-pilot' : 'samples'
        : previousAction === 'prepare' ? 'prepare'
        : previousAction === 'full' && engine === 'bailian' ? 'full'
        : ['samples', 'siliconflow-pilot'].includes(previousAction) ? engine === 'siliconflow' ? 'siliconflow-pilot' : 'samples'
        : null;
      if (action && actions[action] === true && (action !== 'local' || snapshot.local_available)) {
        continuation = [resumeLabels[action], action === 'local'
          ? '沿用已保存的模型与参数，继续未完成片段，已有结果保留。'
          : action === 'prepare' ? '继续本机样片准备，不调用收费服务，已有结果保留。'
          : '继续未完成的处理，已有结果保留；云端调用仍按量计费。', 'resume', action];
      }
    }
  }
  const next = busy
    ? [exporting ? snapshot.job?.action === 'export-draft' ? '正在导出草稿 MP4' : '正在导出审核版 MP4' : '正在处理', exportPhase?.detail || snapshot.job?.message || (exporting ? '正在压入字幕并编码视频，原视频保留。' : '正在保存识别和翻译结果，可随时停止并保留进度。'), null]
    : !sourceReady ? ['选择视频或音频', '先选素材，输出目录会自动建立。', 'source']
    : interrupted && !isDraft ? continuation || ['先处理本次任务的问题', snapshot.job?.message || '当前还不能继续，请查看日志中的原因；已完成结果保留。', 'logs']
    : !isDraft && runStatus === 'draft_exported' ? ['查看未审核草稿', '带字幕的草稿视频已保存，原视频保留；可在结果页播放或打开目录。', 'preview']
    : !isDraft && actions['export-draft'] && !actions.export ? ['直接导出草稿 MP4', '可直接导出当前字幕；未经人工审核，原视频保留。', 'preview']
    : engine === 'local' && ['complete','completed','done'].includes(runStatus) ? ['查看生成的字幕', '识别结果已保存。到预览页检查文字和时间轴，下载需要的字幕。', 'preview']
    : engine === 'local' ? [isSavedLocal ? '继续已有本地任务' : '开始本地识别', parameters.translate ? '生成原文、译文与双语草稿，完成后到结果预览核对。' : '当前仅生成原文字幕；需要双语时可开启翻译。', 'actions']
    : stage === 'samples_ready' && snapshot.approval_exists ? ['处理整片', '样片验收已记录。点击处理整片才会继续计费。', 'actions']
    : ['samples_ready','full_ready'].includes(stage) ? ['查看结果并听看审核', stage === 'samples_ready' ? '样片已生成。先逐句重播检查，再记录实际抽检结果。' : '整片已生成。请核对疑点，再记录最终抽检。', 'preview']
    : stage === 'exported' ? ['查看或保存成片', '字幕和带字幕视频已导出，可以在结果页打开。', 'preview']
    : stage === 'final_reviewed' ? ['导出带字幕视频', '最终抽检已记录，可以导出 MP4。', 'preview']
    : stage === 'approved' ? ['处理整片', '样片验收已记录。点击处理整片才会继续计费。', 'actions']
    : !stage || isDraft ? ['准备样片', '本机提取音频，不调用收费服务。短片整段、长片抽样，合计最多 5 分钟。', 'actions']
    : setupMissing ? ['补齐账户与计价信息', '保存 API Key 后，还需要确认当前账户的模型价格。', 'settings']
    : ['生成双语样片', '点击生成按钮才会上传音频并计费，完成后可以直接在结果页试听。', 'actions'];
  const handleNextStep = () => {
    if (next[2] === 'source') return pick('source');
    if (next[2] === 'preview') return toPreview();
    if (next[2] === 'settings') return toSettings();
    if (next[2] === 'resume') return run(next[3], next[3] === 'local' ? parameters : {});
    const target = document.getElementById(next[2] === 'logs' ? 'task-logs' : 'task-actions');
    if (next[2] === 'logs' && target) target.open = true;
    target?.scrollIntoView({behavior:'smooth',block:'start'});
  };
  const nextStep = <section className={`next-step ${interrupted ? 'next-step-warning' : ''}`} aria-label="下一步建议">
        <div><span className="next-step-label">{busy ? '当前进度' : '下一步'}</span><h2>{next[0]}</h2><p>{next[1]}</p></div>
        {next[2] && <Button icon={next[2] === 'preview' ? Clapperboard : next[2] === 'resume' ? Play : ArrowRight} disabled={disabled}
          onClick={handleNextStep}>
          {next[2] === 'source' ? '选择文件开始' : next[2] === 'preview' ? '前往结果预览' : next[2] === 'settings' ? '补齐配置' : next[2] === 'resume' ? next[0] : next[2] === 'logs' ? '查看运行日志' : '前往操作'}
        </Button>}
      </section>;

  return (
    <>
      <div className="workspace-layout">
        <div className="workspace-main">
          <section className="card material-card">
            <div className={`source-area ${paths.source ? "has-source" : ""}`}>
              <div className="source-symbol">
                <FileAudio size={28} strokeWidth={1.5} />
              </div>
              <div className="source-description">
                <h3 title={fileName(paths.source)}>{fileName(paths.source) || "选择本机视频或音频"}</h3>
                <p>
                  {paths.source
                    ? "已选择本机素材 · 原文件保留"
                    : "支持常见视频与音频格式，选择本机文件即可开始"}
                </p>
              </div>
              <Button
                variant={paths.source ? "secondary" : "primary"}
                disabled={locked}
                onClick={() => pick("source")}
              >
                {paths.source ? "更换素材" : "选择素材"}
                <ArrowUpRight size={15} />
              </Button>
            </div>
            {isDraft && <p className="draft-hint">新素材将创建独立任务，旧项目与结果保留。</p>}
            <FileLocations snapshot={snapshot} paths={paths} isDraft={isDraft} disabled={disabled} open={open} />
            <details className="advanced-paths">
              <summary>路径与对照字幕 <span>手动输入路径、指定目录或载入已有任务</span></summary>
            <PathField
              label="素材路径"
              kind="source"
              value={paths.source}
              placeholder="选择文件，或在这里粘贴完整路径"
              disabled={locked}
              onChange={updatePath("source")}
              pick={pick}
            />
            <div className="project-path-row">
              <PathField
                label="项目目录"
                kind="campaign"
                value={paths.campaign}
                placeholder="留空自动创建，也可选择已有任务"
                disabled={locked}
                onChange={updatePath("campaign")}
                pick={pick}
                aside="保存进度和输出"
              />
              <Button
                className="load-project"
                disabled={locked || !paths.campaign.trim()}
                onClick={() => loadProject()}
                icon={FolderOpen}
              >
                载入
              </Button>
            </div>
            {engine !== "local" && (
              <PathField
                label="对照字幕"
                kind="baseline"
                value={paths.baseline}
                placeholder="选择用于样片对照的原字幕 .srt"
                disabled={locked}
                onChange={updatePath("baseline")}
                pick={pick}
                aside="可选"
              />
            )}
            {engine !== 'local' && <p className="options-hint">旧字幕只作对照，没有也可以准备样片。</p>}
            </details>
          </section>

          <details className="card language-card" aria-label="字幕语言组合" key={`language-${paths.source}-${paths.campaign}`} open={!savedEngine}>
            <summary className="language-summary"><span><strong>字幕语言</strong><small>{savedEngine ? '已随任务保存' : '设置原文与译文'}</small></span><span><Badge>{({ja:'日语',en:'英语',zh:'中文',auto:'自动检测'})[parameters.language]}{engine === 'local' && !parameters.translate ? ' · 原文字幕' : ` → ${({ja:'日语',en:'英语','zh-CN':'中文'})[parameters.target || 'zh-CN']}`}</Badge><ChevronRight size={16} className="details-chevron" /></span></summary>
            <div className="local-options">
              <div className="option-field"><label htmlFor="language">音频语言</label>
                <select id="language" value={parameters.language} disabled={locked || !!savedEngine || (engine!=='local' && !snapshot.language_options)} onChange={e=>updateParameter('language',e.target.value)}>
                  <option value="ja">日语</option><option value="en">英语</option><option value="zh">中文</option>
                  {engine==='local' && <option value="auto">自动检测</option>}
                </select>
              </div>
              <div className="option-field"><label htmlFor="translation-target">翻译目标</label>
                <select id="translation-target" value={parameters.target || 'zh-CN'} disabled={locked || !!savedEngine || !snapshot.language_options} onChange={e=>updateParameter('target',e.target.value)}>
                  <option value="zh-CN" disabled={parameters.language==='zh'}>中文</option><option value="en" disabled={parameters.language==='en'}>英语</option><option value="ja" disabled={parameters.language==='ja'}>日语</option>
                </select>
              </div>
            </div>
            <p className="options-hint">{!snapshot.language_options ? '重启字幕工坊后可使用多语言组合。' : savedEngine ? '语言组合已随任务保存；更换语言请新建任务，已有结果仍可续跑。' : engine==='siliconflow' ? '试听服务自动检测原音语言；翻译目标用于后续完整流程。' : engine==='local' && !parameters.translate ? '当前只制作原文字幕；在识别设置中开启翻译后，将同时生成译文和双语字幕。' : '原音与译文分别保存，并合成为双语字幕。'}</p>
          </details>
          {preview}
          <details className="card engine-section" key={JSON.stringify([paths.source,paths.campaign])} open={!['samples_ready','full_ready','approved','final_reviewed','exported'].includes(stage) && !['complete','completed','done','draft_exported'].includes(runStatus)}>
            <summary className="engine-summary"><span><Settings2 size={17} /><strong>识别与翻译设置</strong></span><span>{engines.find(item=>item.id===engine)?.title}<ChevronRight size={16} /></span></summary>
            <p className="engine-summary-hint">选择识别方式与处理参数，已有任务沿用保存的配置。</p>
            <div
              className="engine-grid"
              role="radiogroup"
              aria-label="识别方式"
            >
              {engines.map(({ id, title, model, detail, tag, icon: Icon }) => (
                <button
                  key={id}
                  role="radio"
                  aria-checked={engine === id}
                  tabIndex={engine === id ? 0 : -1}
                  className={`engine-card ${engine === id ? "selected" : ""}`}
                  disabled={locked || (!!savedEngine && ((savedEngine === 'local') !== (id === 'local')))}
                  onClick={() => {setEngine(id); if(id!=='local' && parameters.language==='auto') updateParameter('language','ja');}}
                  onKeyDown={handleEngineKeys}
                >
                  <div className="engine-top">
                    <span className={`engine-icon engine-${id}`}>
                      <Icon size={22} strokeWidth={1.7} />
                    </span>
                    <span className="radio-mark">
                      {engine === id && <Check size={11} strokeWidth={3} />}
                    </span>
                  </div>
                  <h3>{title}</h3>
                  <div className="model-name">
                    {id === "local" && isSavedLocal
                      ? `Whisper · ${snapshot.project_config.model}`
                      : model}
                  </div>
                  <p>{detail}</p>
                  <span className="engine-tag">{tag}</span>
                </button>
              ))}
            </div>
            {savedEngine && <p className="options-hint">已载入已有任务。云端样片可切换试听服务；本地与云端互换时，请点击“新建任务”。</p>}
            {engine === "local" ? (
              <div className="local-options">
                <div className="option-field">
                  <label htmlFor="chunk">分段长度</label>
                  <select
                    id="chunk"
                    value={parameters.chunk_seconds}
                    disabled={locked || isSavedLocal}
                    onChange={(e) =>
                      updateParameter("chunk_seconds", Number(e.target.value))
                    }
                  >
                    {[...new Set([60, 120, 300, parameters.chunk_seconds])]
                      .sort((a, b) => a - b)
                      .map((value) => (
                        <option key={value} value={value}>
                          {value} 秒
                        </option>
                      ))}
                  </select>
                </div>
                <div className="option-field">
                  <label htmlFor="workers">并发任务</label>
                  <select
                    id="workers"
                    value={parameters.workers}
                    disabled={locked || isSavedLocal}
                    onChange={(e) =>
                      updateParameter("workers", Number(e.target.value))
                    }
                  >
                    {[
                      ...new Set([
                        1,
                        2,
                        ...(isSavedLocal ? [parameters.workers] : []),
                      ]),
                    ]
                      .sort((a, b) => a - b)
                      .map((value) => (
                        <option key={value} value={value}>
                          {value} 个
                        </option>
                      ))}
                  </select>
                </div>
                <label className="checkbox-option">
                  <input
                    type="checkbox"
                    checked={!!parameters.translate}
                    disabled={locked || isSavedLocal}
                    onChange={(e) =>
                      updateParameter("translate", e.target.checked)
                    }
                  />
                  <span>生成译文与双语字幕</span>
                </label>
                {isSavedLocal && (
                  <p className="options-hint">
                    已载入任务：继续使用该任务保存的模型与参数。
                  </p>
                )}
                {!snapshot.local_available && (
                  <p className="options-hint warning-text">
                    本地识别环境尚未就绪，请检查 whisper.cpp 与 small 模型。
                  </p>
                )}
              </div>
            ) : (
              <div className="engine-context">
                <div className="context-icon">
                  {engine === "siliconflow" ? (
                    <AudioLines size={19} />
                  ) : (
                    <ShieldIcon />
                  )}
                </div>
                <div>
                  <strong>
                    {engine === "siliconflow"
                      ? "先听样片，再比较识别效果"
                      : "从样片到整片，每一步都有检查"}
                  </strong>
                  <p>
                    {engine === "siliconflow"
                      ? "试听会上传音频并按量计费。请先核实账户价格；输出文字不含可靠时间轴。"
                      : "样片通过人工验收后，才可处理整片。总预算 ¥20，追加停止线 ¥18。"}
                  </p>
                </div>
                <button className="text-button" onClick={toSettings}>
                  账户设置
                  <ArrowUpRight size={14} />
                </button>
              </div>
            )}
          </details>

          <section className="card action-section" id="task-actions">
            <SectionHeading
              title="生成与验收"
              extra={
                engine !== "local" && <Badge tone="warning">逐步确认</Badge>
              }
            >
              {engine === "local"
                ? "按当前设置开始，或继续已有任务"
                : "完成一个阶段，再决定下一步"}
            </SectionHeading>
            {engine === "local" ? (
              <div className="local-run">
                <div>
                  <strong>本地 Whisper 识别</strong>
                  <p>
                    {parameters.translate
                      ? "识别完成后按所选语言生成译文与双语草稿，请人工核对翻译。"
                      : "按音频语言生成原文字幕，进度自动保存在项目目录。"}
                  </p>
                </div>
                <Button
                  icon={Play}
                  variant="primary"
                  busy={pending === "local"}
                  disabled={
                    locked ||
                    !sourceReady ||
                    !snapshot.local_available ||
                    (!isDraft && actions.local === false)
                  }
                  onClick={() => run("local", parameters)}
                >
                  开始本地识别
                </Button>
              </div>
            ) : (
              <>
                <div className="cloud-stages">
                  <div className="cloud-action-row">
                    <span className="stage-dot">1</span>
                    <div>
                      <strong>准备代表性样片</strong>
                      <p>短片整段、长片抽样，合计最多 5 分钟 · 本机处理</p>
                    </div>
                    <Button
                      icon={FileAudio}
                      disabled={!can("prepare")}
                      busy={pending === "prepare"}
                      onClick={() => run("prepare", snapshot.language_options ? {language:parameters.language,target:parameters.target || 'zh-CN'} : {})}
                    >
                      准备样片
                    </Button>
                  </div>
                  <div className="cloud-action-row">
                    <span className="stage-dot">2</span>
                    <div>
                      <strong>
                        {engine === "siliconflow"
                          ? "试听模型识别效果"
                          : "生成双语样片"}
                      </strong>
                      <p>
                        {engine === "siliconflow"
                          ? "上传音频至硅基流动 · 按量计费"
                          : "百炼识别 + DeepSeek 翻译 · 按量计费"}
                      </p>
                    </div>
                    <Button
                      variant="primary"
                      icon={Play}
                      disabled={
                        !can(
                          engine === "siliconflow"
                            ? "siliconflow-pilot"
                            : "samples",
                        )
                      }
                      onClick={() =>
                        run(
                          engine === "siliconflow"
                            ? "siliconflow-pilot"
                            : "samples",
                        )
                      }
                    >
                      {engine === "siliconflow"
                        ? "开始样片试听（计费）"
                        : "生成付费样片"}
                    </Button>
                  </div>
                  {engine === "bailian" && (
                    <div className="cloud-action-row">
                      <span className="stage-dot">3</span>
                      <div>
                        <strong>继续完整字幕流程</strong>
                        <p>
                          {snapshot.approval_exists
                            ? "样片已验收，可手动启动整片识别与翻译"
                            : "等待样片验收通过 · 按量计费"}
                        </p>
                      </div>
                      <Button
                        icon={ArrowRight}
                        disabled={!can("full")}
                        onClick={() => run("full")}
                      >
                        处理整片
                      </Button>
                    </div>
                  )}
                </div>
                {engine === "siliconflow" && (
                  <Button
                    className="full-width audition-link"
                    icon={FileText}
                    disabled={disabled || !snapshot.siliconflow_result_exists}
                    onClick={() => open("siliconflow-review")}
                  >
                    打开硅基流动试听文字
                  </Button>
                )}
                {engine === "bailian" && (
                  <div className="review-toolbar">
                    <Button
                      icon={ListChecks}
                      disabled={disabled || !actions.review}
                      onClick={() => open("review")}
                    >
                      打开样片对照
                    </Button>
                    <Button
                      icon={Clapperboard}
                      disabled={!can("export")}
                      onClick={() => run("export")}
                    >
                      导出带字幕 MP4
                    </Button>
                  </div>
                )}
                {engine === "bailian" &&
                  stage === "samples_ready" &&
                  !snapshot.approval_exists && (
                    <div className="review-form">
                      <div className="review-heading">
                        <ListChecks size={19} />
                        <div>
                          <h3>样片等待人工验收</h3>
                          <p>至少抽检 20 句，时间合格率需达到 90%。</p>
                          <p>逐句核对清晰对白，起止偏差约 0.5 秒以内计为合格；听不清的句子保留为疑点。</p>
                        </div>
                      </div>
                      <div id="review-feedback" className={`review-feedback ${validReview ? 'review-feedback-ready' : ''}`} role="status" aria-live="polite">
                        <strong>{reviewHint}</strong>
                        {timingRate !== null && <span>时间合格 {timing} / {reviewed} 句 · {timingRate.toFixed(1)}%</span>}
                        <small>填写内容会在本次打开期间保留；切换任务或重新生成结果后清空，不会自动提交。</small>
                      </div>
                      <div className="review-counts">
                        <label>
                          实际抽检句数
                          <input
                            aria-label="实际抽检句数"
                            aria-describedby="review-feedback"
                            type="number"
                            min="20"
                            step="1"
                            value={reviewed}
                            onChange={(e) => setReviewed(e.target.value)}
                            placeholder="至少 20"
                            disabled={locked}
                          />
                        </label>
                        <label>
                          时间合格句数
                          <input
                            aria-label="时间合格句数"
                            aria-describedby="review-feedback"
                            type="number"
                            min="0"
                            step="1"
                            value={timing}
                            onChange={(e) => setTiming(e.target.value)}
                            placeholder="填写实际数量"
                            disabled={locked}
                          />
                        </label>
                      </div>
                      <label className="checkbox-option">
                        <input
                          type="checkbox"
                          checked={contentPassed}
                          onChange={(e) => setContentPassed(e.target.checked)}
                          disabled={locked}
                        />
                        我已检查内容，确认样片通过
                      </label>
                      <Button
                        variant="primary"
                        icon={CheckCheck}
                        disabled={!can("approve") || !validReview}
                        onClick={() =>
                          run("approve", {
                            reviewed: Number(reviewed),
                            timing_passed: Number(timing),
                            content_passed: true,
                          })
                        }
                      >
                        记录样片验收
                      </Button>
                    </div>
                  )}
                {engine === "bailian" && stage === "full_ready" && (
                  <div className="review-form">
                    <h3>{actions['export-draft'] ? '可选：整片人工核对' : '整片字幕等待最终抽检'}</h3>
                    {snapshot.manual_review?.supported ? <>
                      <p>{actions['export-draft'] ? '可直接导出草稿；需要审核版时，再在校对页逐句编辑、标记疑点和已检查项。' : '在校对页逐句编辑、标记疑点和已检查项。直接生成的整片草稿也可在完成实际抽检后验收。'}</p>
                      {snapshot.manual_review.summary && <p>已检查 {snapshot.manual_review.summary.checked} / {snapshot.manual_review.summary.total} 句 · 疑点 {snapshot.manual_review.summary.issues} · 译文待确认 {snapshot.manual_review.summary.pending_translation}</p>}
                      <Button icon={ListChecks} variant="primary" disabled={locked} onClick={toPreview}>前往校对并验收</Button>
                    </> : <>
                    <p>核对疑点与实际播放效果后，再记录最终确认。</p>
                    <label className="checkbox-option">
                      <input
                        type="checkbox"
                        checked={finalPassed}
                        disabled={locked}
                        onChange={(e) => setFinalPassed(e.target.checked)}
                      />
                      我已完成整片抽检，确认内容通过
                    </label>
                    <Button
                      icon={CheckCheck}
                      variant="primary"
                      disabled={!can("accept-final") || !finalPassed}
                      onClick={() =>
                        run("accept-final", { content_passed: true })
                      }
                    >
                      记录最终抽检
                    </Button>
                    </>}
                  </div>
                )}
                {!(engine === "siliconflow"
                  ? snapshot.accounts?.siliconflow?.ready
                  : snapshot.accounts?.bailian?.ready &&
                    snapshot.accounts?.deepseek?.ready) && (
                  <div className="setup-reminder">
                    <Settings2 size={15} />
                    <span>云端识别前，请在 API 设置中完成账户与价格确认。</span>
                    <button className="text-button" onClick={toSettings}>
                      去设置
                      <ArrowRight size={13} />
                    </button>
                  </div>
                )}
              </>
            )}
            <div className="stop-row">
              <span>
                <Timer size={14} />
                中断后保留已完成结果
              </span>
            </div>
          </section>
        </div>
        <ProgressPanel
          job={isDraft ? {} : snapshot.job}
          status={status}
          stage={stage}
          translate={engine !== 'local' || parameters.translate}
          budget={isDraft ? null : snapshot.budget}
          busy={busy}
          toPreview={toPreview}
          disabled={disabled}
          pending={pending}
          stop={stop}
          stopTitle={snapshot.source ? `停止当前正在处理的素材：${snapshot.source}` : undefined}
          engine={engine}
          nextStep={nextStep}
          previewRecommended={next[2] === 'preview'}
        />
      </div>
    </>
  );
}

function MonitorIcon(props) {
  return <Cpu {...props} />;
}
function ShieldIcon() {
  return <CircleCheck size={19} />;
}
