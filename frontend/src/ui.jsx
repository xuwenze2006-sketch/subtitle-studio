import { LoaderCircle, Check, CircleAlert, Info } from "lucide-react";

export function Button({
  children,
  variant = "secondary",
  busy = false,
  icon: Icon,
  className = "",
  ...props
}) {
  return (
    <button className={`button button-${variant} ${className}`} {...props}>
      {busy ? (
        <LoaderCircle size={16} className="spin" aria-hidden="true" />
      ) : Icon ? (
        <Icon size={16} aria-hidden="true" />
      ) : null}
      {children}
    </button>
  );
}

export function Badge({ children, tone = "neutral" }) {
  return (
    <span className={`badge badge-${tone}`}>
      <span className="badge-dot" />
      {children}
    </span>
  );
}

export function Notice({ children, tone = "info", title, action }) {
  const Icon =
    tone === "error" ? CircleAlert : tone === "success" ? Check : Info;
  return (
    <div
      className={`notice notice-${tone}`}
      role={tone === "error" ? "alert" : undefined}
    >
      <Icon size={18} aria-hidden="true" />
      <div>
        {title && <strong>{title}</strong>}
        <div>{children}</div>
      </div>
      {action}
    </div>
  );
}

export function SectionHeading({ number, title, children, extra }) {
  return (
    <div className="section-heading">
      <div className="section-title-line">
        {number && <span className="section-number">{number}</span>}
        <div>
          <h2>{title}</h2>
          {children && <p>{children}</p>}
        </div>
      </div>
      {extra}
    </div>
  );
}

export function Empty({ icon: Icon, title, children, action }) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon size={28} strokeWidth={1.5} aria-hidden="true" />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}

export const statusLabels = {
  idle: "等待开始",
  prepared: "样片已准备",
  preparing: "准备样片中",
  running: "处理中",
  asr_incomplete: "识别未完成",
  translation_incomplete: "翻译未完成",
  samples_ready: "样片待验收",
  samples_incomplete: "样片未完成",
  approved: "样片已验收",
  full_ready: "整片待抽检",
  full_incomplete: "整片未完成",
  final_reviewed: "最终抽检通过",
  exported: "已导出",
  draft_exported: "草稿已导出（未审核）",
  unreviewed_draft: "未审核草稿",
  complete: "已完成",
  completed: "已完成",
  done: "已完成",
  cancelled: "已停止",
  stopped: "已停止",
  needs_attention: "需要处理",
  failed: "任务出错",
  error: "任务出错",
};
export const statusLabel = (value) =>
  statusLabels[value] || value || "尚未开始";
export const fileName = (path) =>
  path?.split(/[\\/]/).filter(Boolean).pop() || "";
export function updatedLabel(value) {
  if (!value) return "时间未知";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(date);
}
export function timeLabel(milliseconds = 0) {
  const seconds = Math.max(0, Math.floor(Number(milliseconds) / 1000));
  return [
    Math.floor(seconds / 3600),
    Math.floor(seconds / 60) % 60,
    seconds % 60,
  ]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}
