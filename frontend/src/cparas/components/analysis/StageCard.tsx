/** One pipeline stage row in the ISLD progress tracker (done / active / pending / failed). */

import type { StageMeta } from '../../lib/pipelineStages';

export type StageStatus = 'done' | 'active' | 'pending' | 'failed';

/** Stage label, status icon, live description and an optional inline sub-progress bar. */
export default function StageCard({
  stage,
  status,
  description,
  subProgress,
}: {
  stage: StageMeta;
  status: StageStatus;
  description: string;
  /** Optional {current, total} for an inline mini progress bar (e.g. chunk 2/5). */
  subProgress?: { current: number; total: number; unit?: string } | null;
}) {
  const icon = status === 'done' ? '✓' : status === 'failed' ? '✖' : status === 'active' ? '' : '○';
  return (
    <div className={`stage-card stage-card--${status}`}>
      <div className={`stage-icon stage-icon--${status}`}>
        {status === 'active' ? <span className="stage-spinner" /> : icon}
      </div>
      <div className="stage-body">
        <div className="stage-label">{stage.label}</div>
        {(status === 'active' || status === 'failed') && (
          <div className="stage-desc">{description}</div>
        )}
        {status === 'active' && subProgress && subProgress.total > 1 && (
          <div className="stage-subprogress">
            <div className="bar-track" style={{ height: 5 }}>
              <div
                className="bar-fill"
                style={{ width: `${Math.round((subProgress.current / subProgress.total) * 100)}%` }}
              />
            </div>
            <span className="stage-subprogress-count">
              {subProgress.current}/{subProgress.total} {subProgress.unit || ''}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
