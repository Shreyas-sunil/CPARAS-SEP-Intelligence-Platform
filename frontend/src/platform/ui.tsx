/**
 * Shared UI primitives for the CPARAS platform (both products).
 * Styling lives in platform/styles/platform.css.
 */
import type { CSSProperties, ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle, ChevronLeft, ChevronRight } from 'lucide-react';
import type { Product } from './products';

/** Page title block (eyebrow, title, description, actions), coloured by product. */
export function PageHeader({
  product, eyebrow, title, description, actions, children,
}: {
  product?: Product | null;
  eyebrow?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`page-header ${product?.className ?? 'p-platform'}`}>
      <div className="page-header-main">
        {(eyebrow ?? product?.name) && <div className="eyebrow">{eyebrow ?? product?.name}</div>}
        <h1 className="page-title">{title}</h1>
        {description && <p className="page-desc">{description}</p>}
        {children}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

/** Standard content card with optional header, actions and footer. */
export function Card({
  title, subtitle, icon, actions, children, footer, flush, className, style, id,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  flush?: boolean;
  className?: string;
  style?: CSSProperties;
  id?: string;
}) {
  return (
    <section className={`card${className ? ` ${className}` : ''}`} style={style} id={id}>
      {(title || actions) && (
        <header className="card-header">
          <div style={{ minWidth: 0 }}>
            {title && <h2 className="card-title">{icon}{title}</h2>}
            {subtitle && <div className="card-sub">{subtitle}</div>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? 'card-body card-body--flush' : 'card-body'}>{children}</div>
      {footer && <footer className="card-footer">{footer}</footer>}
    </section>
  );
}

/** KPI tile. */
export function Kpi({ label, value, note, color, icon }: {
  label: ReactNode; value: ReactNode; note?: ReactNode; color?: string; icon?: ReactNode;
}) {
  return (
    <div className="kpi" style={color ? ({ '--kpi-color': color } as CSSProperties) : undefined}>
      <div className="kpi-label">{icon}{label}</div>
      <div className="kpi-value" title={typeof value === 'string' ? value : undefined}>{value}</div>
      {note && <div className="kpi-note">{note}</div>}
    </div>
  );
}

/** Loading placeholder row of KPI tiles. */
export function KpiSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className={count === 5 ? 'kpi-grid kpi-grid--5' : 'kpi-grid'}>
      {Array.from({ length: count }).map((_, i) => (
        <div className="kpi" key={i}>
          <div className="skeleton" style={{ height: 10, width: '45%' }} />
          <div className="skeleton" style={{ height: 22, width: '60%', marginTop: 10 }} />
        </div>
      ))}
    </div>
  );
}

/** Empty / no-access state block. */
export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: ReactNode; body?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      {icon && <div className="empty-icon">{icon}</div>}
      <div className="empty-title">{title}</div>
      {body && <div className="empty-body">{body}</div>}
      {action && <div style={{ marginTop: 10 }}>{action}</div>}
    </div>
  );
}

/** Spinner with a label. */
export function LoadingState({ label = 'Loading…' }: { label?: ReactNode }) {
  return (
    <div className="loading" role="status">
      <Loader2 size={16} className="spin" />
      <span>{label}</span>
    </div>
  );
}

type Tone = 'info' | 'ok' | 'warn' | 'err';
const TONE_ICON: Record<Tone, ReactNode> = {
  info: <Info size={16} />,
  ok: <CheckCircle2 size={16} />,
  warn: <AlertTriangle size={16} />,
  err: <XCircle size={16} />,
};

/** Inline message (info / ok / warn / err). */
export function Banner({ tone = 'info', title, children, actions, icon, style }: {
  tone?: Tone; title?: ReactNode; children?: ReactNode; actions?: ReactNode; icon?: ReactNode; style?: CSSProperties;
}) {
  return (
    <div className={`banner banner-${tone}`} role={tone === 'err' ? 'alert' : undefined} style={style}>
      {icon ?? TONE_ICON[tone]}
      <div style={{ minWidth: 0 }}>
        {title && <div className="banner-title">{title}</div>}
        {children && <div className="banner-body">{children}</div>}
      </div>
      {actions && <div className="banner-actions">{actions}</div>}
    </div>
  );
}

/** Small status pill. */
export function Pill({ tone = 'muted', dot, children, title, className }: {
  tone?: 'ok' | 'warn' | 'err' | 'info' | 'muted' | 'outline' | 'product'; dot?: boolean; children: ReactNode; title?: string; className?: string;
}) {
  return <span className={`pill pill-${tone}${dot ? ' pill-dot' : ''}${className ? ` ${className}` : ''}`} title={title}>{children}</span>;
}

/** Determinate or indeterminate progress bar (accessible progressbar role). */
export function ProgressBar({ value, max = 100, indeterminate, style }: { value?: number; max?: number; indeterminate?: boolean; style?: CSSProperties }) {
  const pct = max > 0 && value != null ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className={`progress${indeterminate ? ' is-indeterminate' : ''}`} style={style} role="progressbar"
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={indeterminate ? undefined : Math.round(pct)}>
      <span style={indeterminate ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

/** Previous/next pager with the visible range. */
export function Pager({ offset, limit, total, onChange, disabled }: {
  offset: number; limit: number; total: number; onChange: (offset: number) => void; disabled?: boolean;
}) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="pager">
      <span>
        <span className="num">{from.toLocaleString()}–{to.toLocaleString()}</span> of <span className="num">{total.toLocaleString()}</span>
      </span>
      <span className="row" style={{ gap: 4 }}>
        <button className="btn btn-sm btn-icon" aria-label="Previous page" disabled={disabled || offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          <ChevronLeft size={14} />
        </button>
        <span className="num" style={{ minWidth: 70, textAlign: 'center' }}>Page {page} / {pages}</span>
        <button className="btn btn-sm btn-icon" aria-label="Next page" disabled={disabled || offset + limit >= total} onClick={() => onChange(offset + limit)}>
          <ChevronRight size={14} />
        </button>
      </span>
    </div>
  );
}

/** Accessible tab bar. */
export function Tabs<T extends string>({ value, onChange, items }: {
  value: T; onChange: (v: T) => void; items: { id: T; label: ReactNode; icon?: ReactNode; count?: ReactNode }[];
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map(it => (
        <button key={it.id} role="tab" aria-selected={value === it.id} className={`tab${value === it.id ? ' active' : ''}`} onClick={() => onChange(it.id)}>
          {it.icon}{it.label}{it.count != null && <span className="tab-count">{it.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** Local date/time for an ISO timestamp (UTC assumed when the zone is missing). */
export function formatDateTime(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
