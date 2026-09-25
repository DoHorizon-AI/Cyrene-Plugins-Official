// -----------------------------------------------------------------------------
// Module: src/components.tsx
// Role: Shared presentation primitives for the Navigator operations console.
// -----------------------------------------------------------------------------
// 中文：// 中文：模块职责：为 Navigator 运维控制台提供共享展示组件。

import type { ButtonHTMLAttributes, ReactNode } from "react";

export interface PageHeaderProps {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}

/**
 * Page-level title block with a single clear action slot.
 * 提供页面级标题区，并预留一个明确的操作按钮位置。
 */
export function PageHeader({ eyebrow, title, description, action }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
      {action ? <div className="page-header__action">{action}</div> : null}
    </header>
  );
}

export interface PanelProps {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * Bordered work surface used to keep related API data together.
 * 使用带边框的工作区将相关 API 数据归在一起。
 */
export function Panel({ title, meta, children, className = "" }: PanelProps) {
  return (
    <section className={`panel ${className}`.trim()}>
      <div className="panel__heading">
        <h2>{title}</h2>
        {meta ? <div className="panel__meta">{meta}</div> : null}
      </div>
      {children}
    </section>
  );
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  tone?: "primary" | "quiet" | "danger";
}

/**
 * Consistent keyboard-focusable button surface.
 * 提供统一且可通过键盘聚焦的按钮外观。
 */
export function Button({ tone = "quiet", className = "", children, ...props }: ButtonProps) {
  return (
    <button className={`button button--${tone} ${className}`.trim()} {...props}>
      {children}
    </button>
  );
}

/**
 * Small state marker that uses domain wording rather than color alone.
 * 使用领域术语呈现小型状态标记，不单靠颜色表达状态。
 */
export function StatusPill({ value }: { value: string | undefined }) {
  const label = value || "UNKNOWN";
  const normalized = label.toLowerCase();
  const tone =
    normalized.includes("ready") ||
    normalized.includes("active") ||
    normalized.includes("complete") ||
    normalized.includes("published") ||
    normalized === "ok"
      ? "good"
      : normalized.includes("fail") ||
          normalized.includes("error") ||
          normalized.includes("revoked") ||
          normalized.includes("unhealthy")
        ? "bad"
        : normalized.includes("running") || normalized.includes("start") || normalized.includes("validat")
          ? "live"
          : "muted";
  return <span className={`status-pill status-pill--${tone}`}>{label}</span>;
}

export interface MetricCardProps {
  label: string;
  value: string | number;
  detail: string;
  accent?: "lime" | "orange" | "blue" | "gray";
}

/**
 * Compact metric with an editorial label/detail hierarchy.
 * 采用清晰的标题与详情层级展示紧凑指标。
 */
export function MetricCard({ label, value, detail, accent = "lime" }: MetricCardProps) {
  return (
    <article className={`metric-card metric-card--${accent}`}>
      <p className="metric-card__label">{label}</p>
      <strong>{value}</strong>
      <p className="metric-card__detail">{detail}</p>
    </article>
  );
}

export interface StateBlockProps {
  kind: "loading" | "error" | "empty";
  title: string;
  detail: string;
  action?: ReactNode;
}

/**
 * Honest loading, failure, and empty states shared by every resource page.
 * 为所有资源页面提供一致的加载、失败和空状态。
 */
export function StateBlock({ kind, title, detail, action }: StateBlockProps) {
  return (
    <div className={`state-block state-block--${kind}`} role={kind === "error" ? "alert" : undefined}>
      <span className="state-block__mark" aria-hidden="true">
        {kind === "loading" ? "..." : kind === "error" ? "!" : "0"}
      </span>
      <div>
        <h3>{title}</h3>
        <p>{detail}</p>
        {action ? <div className="state-block__action">{action}</div> : null}
      </div>
    </div>
  );
}

export interface TableColumn<T> {
  label: string;
  className?: string;
  render: (row: T) => ReactNode;
}

export interface ResourceTableProps<T> {
  rows: readonly T[];
  columns: readonly TableColumn<T>[];
  rowKey: (row: T, index: number) => string;
  caption: string;
}

/**
 * Accessible horizontal table with a mobile scroll boundary.
 * 提供可访问的水平表格，并在移动端设置滚动边界。
 */
export function ResourceTable<T>({ rows, columns, rowKey, caption }: ResourceTableProps<T>) {
  return (
    <div className="table-scroll">
      <table className="resource-table">
        <caption>{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th className={column.className} key={column.label} scope="col">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
              {columns.map((column) => (
                <td className={column.className} key={column.label}>
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Form label wrapper that keeps the control name adjacent to its input.
 * 提供表单标签包装组件，使控件名称紧邻对应输入框。
 */
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

/**
 * Format an optional API timestamp for the operator's local timezone.
 * 按操作人员本地时区格式化可选的 API 时间戳。
 */
export function formatDate(value: unknown): string {
  if (typeof value !== "string" || !value) {
    return "No timestamp";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/**
 * Narrow an unknown Product field to a render-safe string.
 * 将未知 Product 字段收窄为可安全渲染的字符串。
 */
export function text(value: unknown, fallback = "Not reported"): string {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

/**
 * Format a count without hiding the unavailable state behind zero.
 * 格式化计数，同时不把不可用状态伪装成零。
 */
export function count(value: number | null): string {
  return value === null ? "--" : new Intl.NumberFormat().format(value);
}
