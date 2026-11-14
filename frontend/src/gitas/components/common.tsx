/** Small shared building blocks of the CPARAS Bulk pages. */

import type { ReactNode } from 'react';
import { LoadingState } from '../../platform/ui';

/** Essential / Potential / Non-Essential badge. */
export function EssentialityBadge({ status }: { status?: string | null }) {
  if (!status) return <span className="g-badge" style={{ color: 'var(--c-muted)' }}>Not scored</span>;
  const cls = status === 'Essential' ? 'essential' : status === 'Non-Essential' ? 'nonessential' : 'potential';
  return <span className={`g-badge ${cls}`}>{status}</span>;
}

/** Theme colour token for an essentiality class. */
export function essColor(status?: string | null) {
  if (status === 'Essential') return 'var(--g-essential)';
  if (status === 'Non-Essential') return 'var(--g-nonessential)';
  return 'var(--g-potential)';
}

/** Inline loading indicator. */
export function Loading({ label = 'Loading…' }: { label?: string }) {
  return <LoadingState label={label} />;
}

/** Empty-state block with optional action. */
export function EmptyState({ icon, title, body, action }: { icon?: ReactNode; title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="g-empty">
      {icon}
      <h3>{title}</h3>
      {body && <p style={{ maxWidth: 420, margin: '0 auto' }}>{body}</p>}
      {action && <div style={{ marginTop: 16 }}>{action}</div>}
    </div>
  );
}

/** Labelled 0-1 score bar with an optional tooltip (score breakdown). */
export function ScoreBar({ label, value, tip }: { label: string; value: number; tip?: string }) {
  const color = value >= 75 ? 'var(--g-essential)' : value >= 50 ? 'var(--g-blue)' : 'var(--g-potential)';
  return (
    <div className="g-score-row" title={tip}>
      <div className="g-score-label"><span>{label}</span><b>{Math.round(value)}%</b></div>
      <div className="g-score-track"><div className="g-score-fill" style={{ width: `${Math.min(100, Math.max(0, value))}%`, background: color }} /></div>
    </div>
  );
}

// Country flags were previously shown as emoji here. Every call site
// already renders the country code/name as plain text right next to this,
// so dropping the emoji glyph loses no information — this is kept as a
// no-op rather than removed so existing imports don't need touching.
export function CountryFlag(_props: { code?: string | null }) {
  return null;
}

/** Text with the matched key terms wrapped in <mark> (React nodes, no HTML injection). */
export function highlightTerms(text: string | undefined | null, terms: string[] = []) {
  if (!text) return null;
  if (!terms.length) return text;
  const escaped = terms.filter(Boolean).map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!escaped.length) return text;
  const re = new RegExp(`(${escaped.join('|')})`, 'ig');
  const parts = text.split(re);
  return parts.map((part, i) =>
    terms.some(t => t.toLowerCase() === part.toLowerCase())
      ? <span key={i} className="g-term-hl">{part}</span>
      : <span key={i}>{part}</span>
  );
}
