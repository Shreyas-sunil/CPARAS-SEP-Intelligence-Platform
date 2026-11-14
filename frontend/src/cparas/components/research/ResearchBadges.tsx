/**
 * Shared small components for rendering research/paper-style outputs
 * (Requirements 2, 5, 9) — predicted-SEP badges, verification badges,
 * "Matched because: ..." explanation bullets, and the
 * "TS X vA → vB → vC" version-evolution trail.
 */

import type {
  PredictedSepStatus, VerificationStatus, VersionEvolution,
} from '../../types';

/** Coloured badge for Confirmed / Likely / Potential SEP / Insufficient Evidence. */
export function PredictedStatusBadge({ status }: { status: PredictedSepStatus | string }) {
  const cls =
    status === 'Confirmed SEP' ? 's-hi' :
    status === 'Likely SEP' ? 's-ac' :
    status === 'Potential SEP' ? 's-md' : 's-mu';
  return <span className={`sep-s ${cls}`}>{status}</span>;
}

/** Coloured badge for a version-verification status. */
export function VerificationBadge({ status }: { status: VerificationStatus | string }) {
  const cls =
    status === 'Verified - forward alignment' ? 's-hi' :
    status === 'Predicted' ? 's-ac' :
    status === 'Declining relevance' ? 's-lo' : 's-mu';
  return <span className={`sep-s ${cls}`}>{status}</span>;
}

/** Similarity / probability 0-1 as a bar with percentage. */
export function SimBar({ score, label }: { score?: number | null; label?: string }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const color = score >= 0.6 ? 'var(--green)' : score >= 0.5 ? 'var(--accent)' : score >= 0.35 ? 'var(--yellow)' : 'var(--red)';
  return (
    <div className="conf-bar-wrap">
      <div className="conf-bar-track"><div className="conf-bar-fill" style={{ width: pct + '%', background: color }} /></div>
      <span className="cpill" style={{ background: `${color}1a`, color, border: `1px solid ${color}33` }}>{pct}%</span>
      {label && <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--muted)' }}>{label}</span>}
    </div>
  );
}

/**
 * Requirement 5 — "Matched because: • 84% semantic similarity • Scheduler
 * topic overlap • HARQ keyword overlap • Similar to Patent XXXXXXX"
 */
export function ExplanationBullets({ items }: { items: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <div className="di-label">Matched because</div>
      <ul className="explain-list">
        {items.map((it, i) => <li key={i}>{it}</li>)}
      </ul>
    </div>
  );
}

/**
 * Requirement 9 — "TS 36.331 v14.0.0 → v15.8.0 → v16.4.0 — Predicted
 * essentiality increased"
 */
export function VersionEvolutionTrail({ evolution }: { evolution: VersionEvolution }) {
  if (!evolution?.points?.length) return null;
  const trendColor = evolution.trend === 'increasing' ? 'var(--green)' : evolution.trend === 'decreasing' ? 'var(--red)' : 'var(--muted)';
  const arrow = evolution.trend === 'increasing' ? '↑' : evolution.trend === 'decreasing' ? '↓' : '→';
  return (
    <div>
      <div className="di-label">{evolution.standard} · {evolution.clause} {evolution.clause_title}</div>
      <div className="ver-trail">
        {evolution.points.map((p, i) => (
          <span key={p.version} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {i > 0 && <span className="ver-arrow">→</span>}
            <span className="ver-chip" title={`${p.release} · ${p.year}`}>
              {p.version}<span className="ver-chip-sim">{Math.round(p.similarity * 100)}%</span>
            </span>
          </span>
        ))}
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: trendColor, marginTop: 4 }}>
        {arrow} {evolution.trend_label}
      </div>
    </div>
  );
}
