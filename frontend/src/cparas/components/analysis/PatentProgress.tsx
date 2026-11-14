/** Current patent label and completed / total patents (renders nothing until the total is known). */
export default function PatentProgress({
  current,
  completed,
  total,
}: {
  current?: string | null;
  completed: number;
  total: number;
}) {
  if (!total) return null;
  const pct = Math.min(100, Math.round((completed / total) * 100));
  return (
    <div className="patent-progress">
      <div className="patent-progress-title">Patent Analysis</div>
      <div className="patent-progress-body">
        <div className="patent-progress-info">
          <div className="patent-progress-label">Patent</div>
          <div className="patent-progress-current">{current || '—'}</div>
        </div>
        <div className="patent-progress-count">{completed} / {total}</div>
      </div>
      <div className="bar-track" style={{ height: 8 }}>
        <div className="bar-fill patent-progress-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
