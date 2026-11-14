/**
 * Research metrics & aggregate statistics panel (Requirement 11):
 * Accuracy / Precision / Recall / F1 / Confusion Matrix / Cohen's kappa,
 * Average Similarity, Average Confidence, Number of Potential SEPs,
 * Top Standards, Top Applicants, Top Versions, Top Domains.
 */
import type { ResearchAggregates, ResearchMetrics } from '../../types';

/** Small ranked list with proportional bars (top standards, domains...). */
function MiniBarList({ title, items }: { title: string; items?: [string, number][] }) {
  if (!items?.length) return null;
  const max = items[0][1] || 1;
  return (
    <div className="p-card">
      <div className="p-card-title">{title}</div>
      <div className="bar-chart">
        {items.map(([label, val]) => (
          <div key={label} className="bar-row">
            <div className="bar-label" title={label}>{label}</div>
            <div className="bar-track">
              <div className="bar-fill" style={{ width: `${Math.round((val / max) * 100)}%`, background: 'var(--accent2)' }}>{val}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** One labelled metric value. */
function MetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat-card" style={{ padding: 12 }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ fontSize: 22 }}>{value}</div>
    </div>
  );
}

/**
 * Evaluation metrics (accuracy, F1, kappa, confusion matrix) and batch aggregates of a research
 * run.
 */
export default function ResearchMetricsPanel({ metrics, aggregates }: { metrics: ResearchMetrics; aggregates: ResearchAggregates }) {
  const cm = metrics?.confusion_matrix;

  return (
    <div>
      {/* Top-line aggregates */}
      <div className="stats-strip" style={{ marginBottom: 16 }}>
        <div className="stat-card">
          <div className="stat-label">Patents Analyzed</div>
          <div className="stat-value" style={{ color: 'var(--accent)' }}>{aggregates?.n_patents ?? 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg. Similarity</div>
          <div className="stat-value" style={{ color: 'var(--accent2)' }}>{Math.round((aggregates?.average_similarity ?? 0) * 100)}%</div>
          <div className="stat-sub">to recommended clause</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Avg. Confidence</div>
          <div className="stat-value" style={{ color: 'var(--green)' }}>{Math.round((aggregates?.average_confidence ?? 0) * 100)}%</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Potential SEPs</div>
          <div className="stat-value" style={{ color: 'var(--yellow)' }}>{aggregates?.n_potential_seps ?? 0}</div>
          <div className="stat-sub">flagged for review</div>
        </div>
      </div>

      {/* Classification performance */}
      {metrics?.available ? (
        <div style={{ marginBottom: 16 }}>
          <div className="sec-title">Classification Performance</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(110px,1fr))', gap: 10, marginBottom: 12 }}>
            <MetricCard label="Accuracy" value={`${Math.round((metrics.accuracy ?? 0) * 100)}%`} />
            <MetricCard label="Precision" value={`${Math.round((metrics.precision ?? 0) * 100)}%`} />
            <MetricCard label="Recall" value={`${Math.round((metrics.recall ?? 0) * 100)}%`} />
            <MetricCard label="F1 Score" value={`${Math.round((metrics.f1 ?? 0) * 100)}%`} />
            <MetricCard label="Cohen's κ" value={(metrics.cohens_kappa ?? 0).toFixed(2)} />
          </div>

          {cm && (
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
              <table className="cm-table">
                <thead>
                  <tr><th /><th colSpan={2} style={{ textAlign: 'center' }}>Predicted</th></tr>
                  <tr><th /><th>No</th><th>Yes</th></tr>
                </thead>
                <tbody>
                  <tr>
                    <th className="cm-row-label">Actual: {cm.labels[0]}</th>
                    <td className="cm-cell cm-good">{cm.tn}</td>
                    <td className="cm-cell cm-bad">{cm.fp}</td>
                  </tr>
                  <tr>
                    <th className="cm-row-label">Actual: {cm.labels[1]}</th>
                    <td className="cm-cell cm-bad">{cm.fn}</td>
                    <td className="cm-cell cm-good">{cm.tp}</td>
                  </tr>
                </tbody>
              </table>
              <div className="note" style={{ flex: 1, minWidth: 240, marginTop: 0 }}>
                {metrics.methodology}
                {metrics.n_pairs != null && (
                  <div style={{ marginTop: 6 }}>
                    Evaluated on {metrics.n_pairs} declared/control pairs at a similarity threshold of {Math.round((metrics.threshold ?? 0) * 100)}%.
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="note" style={{ marginBottom: 16 }}>
          {metrics?.reason || 'Classification metrics unavailable for this batch.'}
        </div>
      )}

      {/* Top lists */}
      <div className="p-grid">
        <MiniBarList title="Top Recommended Standards" items={aggregates?.top_standards} />
        <MiniBarList title="Top Recommended Versions" items={aggregates?.top_versions} />
        <MiniBarList title="Top Applicants" items={aggregates?.top_applicants} />
        <MiniBarList title="Top Technical Domains" items={aggregates?.top_domains} />
      </div>
    </div>
  );
}
