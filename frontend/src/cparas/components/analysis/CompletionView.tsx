/**
 * Final summary card of an ISLD analysis run: every pipeline stage ticked, how many files
 * succeeded, which model/backend was used.
 */

import type { StageMeta } from '../../lib/pipelineStages';
import ModelBadge from './ModelBadge';

/** Completion summary for `succeeded` of `total` files. */
export default function CompletionView({
  stages,
  model,
  backend,
  succeeded,
  total,
}: {
  stages: StageMeta[];
  model?: string;
  backend?: string;
  succeeded: number;
  total: number;
}) {
  return (
    <div className="completion-view">
      <div className="completion-title">Analysis Complete</div>
      <ul className="completion-list">
        {stages.map(s => (
          <li key={s.id} className="completion-item">
            <span className="completion-check">✓</span> {s.label} Complete
          </li>
        ))}
      </ul>
      {model && (
        <div className="completion-model">
          <ModelBadge model={model} backend={backend} size="sm" />
        </div>
      )}
      <div className="completion-sub">
        {succeeded}/{total} file(s) succeeded
      </div>
      <div className="completion-opening">
        <span className="stage-spinner" /> Opening Dashboard…
      </div>
    </div>
  );
}
