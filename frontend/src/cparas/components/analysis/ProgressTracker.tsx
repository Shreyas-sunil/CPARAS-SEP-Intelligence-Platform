/**
 * Progress view of a running ISLD analysis: stage cards, overall progress with elapsed/remaining
 * time, per-patent progress, the activity log and Pause / Stop controls. Driven entirely by GET
 * /api/analysis/progress/{job_id}.
 */

import { useEffect, useRef, useState } from 'react';
import { Monitor, Pause, Square } from 'lucide-react';
import type { AnalysisProgress } from '../../services/api';
import {
  stagesForMode, backendStageToUi, stageIndex, describeStage, overallFraction,
} from '../../lib/pipelineStages';
import StageCard from './StageCard';
import type { StageStatus } from './StageCard';
import ModelBadge from './ModelBadge';
import Timer from './Timer';
import ActivityLog from './ActivityLog';
import PatentProgress from './PatentProgress';

/** A failure that ended the run, shown with a Retry action. */
export interface FatalError {
  stage?: string;
  message: string;
}

/**
 * Renders the tracker for the latest progress snapshot; stage mapping and fractions come from
 * lib/pipelineStages.
 */
export default function ProgressTracker({
  progress,
  mode,
  startedAt,
  uploadMessage,
  fatalError,
  onRetry,
  onPause,
  onStop,
  jobControlBusy,
}: {
  progress: AnalysisProgress | null;
  mode: 'deep' | 'quick';
  startedAt: number;
  uploadMessage?: string;
  fatalError?: FatalError | null;
  onRetry?: () => void;
  // v0.6 start/stop/pause/continue: cooperatively stops the in-flight
  // /analyze call between files (see analysis.py's "start / stop / pause
  // / continue" section) -- both optional, since ProgressTracker is also
  // used to show a past run's failure with no job in flight to control.
  onPause?: () => void;
  onStop?: () => void;
  jobControlBusy?: boolean;
}) {
  const stages = stagesForMode(mode);
  const currentUi = backendStageToUi(progress?.stage);
  // When a file fails, the backend sets stage='error' and moves the stage
  // that was actually in flight to `failed_stage`. Reading only
  // fatalError.stage therefore always resolved to null (because 'error' is
  // not a UI stage), so no card was ever marked failed and the Retry button
  // never rendered. Prefer the preserved failed_stage, in either source.
  const failedUi = fatalError
    ? backendStageToUi(fatalError.stage) ?? backendStageToUi(progress?.failed_stage)
    : null;
  const currentIdx = currentUi ? stageIndex(stages, currentUi) : -1;
  const failedIdx = failedUi ? stageIndex(stages, failedUi) : -1;

  // Computed once per render — this was previously evaluated twice (once for
  // the bar, once for the Timer), which is wasted work and, more importantly,
  // risked the bar and the ETA disagreeing if the inputs ever diverged.
  const fraction = overallFraction(stages, progress);

  // The bar must never travel backwards inside a single run. The fraction is
  // a weighted heuristic over discrete stages, so a late-arriving or
  // out-of-order poll response can legitimately produce a slightly lower
  // value; showing that as a rewind looks like a fault. Reset per run.
  const [maxFraction, setMaxFraction] = useState(0);
  const runKeyRef = useRef(startedAt);
  useEffect(() => {
    if (runKeyRef.current !== startedAt) {
      runKeyRef.current = startedAt;
      setMaxFraction(0);
    }
  }, [startedAt]);
  useEffect(() => {
    setMaxFraction(prev => (fraction > prev ? fraction : prev));
  }, [fraction]);

  const displayFraction = Math.max(fraction, maxFraction);
  const overallPct = Math.round(displayFraction * 100);
  const isDone = progress?.stage === 'done' || progress?.done === true;

  function statusFor(idx: number): StageStatus {
    if (failedIdx >= 0) {
      if (idx < failedIdx) return 'done';
      if (idx === failedIdx) return 'failed';
      return 'pending';
    }
    // The upload itself happens client-side (axios), before the backend has
    // written any progress entry at all — treat "no backend progress yet"
    // as still-uploading rather than everything-pending.
    if (stages[idx].id === 'upload') {
      return currentIdx >= 0 ? 'done' : 'active';
    }
    if (currentIdx < 0) return 'pending';
    if (idx < currentIdx) return 'done';
    if (idx === currentIdx) return 'active';
    return 'pending';
  }

  const multiFile = (progress?.total_files ?? 0) > 1;

  return (
    <div className="progress-tracker">
      <div className="progress-header">
        <div>
          <div className="progress-title">
            Analysis Progress
            {multiFile && (
              <span className="progress-file-count">
                File {(progress?.current_file_index ?? 0) + 1}/{progress?.total_files}
                {progress?.current_filename ? `: ${progress.current_filename}` : ''}
              </span>
            )}
          </div>
        </div>
        {progress?.model && (
          <div className="progress-model-chip">
            <span className="progress-model-status">
              {fatalError ? '✖ Failed' : progress.stage === 'done' ? '✓ Done' : '⟳ Running'}
            </span>
            <ModelBadge model={progress.model} backend={progress.backend || 'Ollama'} />
          </div>
        )}
        {!fatalError && !isDone && (onPause || onStop) && (
          <div className="progress-controls">
            {onPause && (
              <button
                type="button"
                className="progress-ctrl-btn"
                disabled={jobControlBusy}
                onClick={onPause}
                title="Pause after the current file finishes"
              >
                <Pause size={12} /> Pause
              </button>
            )}
            {onStop && (
              <button
                type="button"
                className="progress-ctrl-btn is-stop"
                disabled={jobControlBusy}
                onClick={onStop}
                title="Stop after the current file finishes"
              >
                <Square size={12} /> Stop
              </button>
            )}
          </div>
        )}
      </div>

      <div className="stage-list">
        {stages.map((s, idx) => (
          <StageCard
            key={s.id}
            stage={s}
            status={statusFor(idx)}
            description={
              statusFor(idx) === 'failed'
                ? (fatalError?.message || 'Something went wrong on this stage.')
                : s.id === 'upload' && statusFor(idx) === 'active' && uploadMessage
                  ? uploadMessage
                  : describeStage(s, progress)
            }
            subProgress={
              idx === currentIdx && progress?.total_chunks && progress.total_chunks > 1
                ? { current: progress.chunk_index ?? 0, total: progress.total_chunks, unit: progress.stage === 'deep_enrichment' ? 'batches' : 'chunks' }
                : null
            }
          />
        ))}
      </div>

      {failedIdx >= 0 && onRetry && (
        <button className="analyze-btn progress-retry-btn" onClick={onRetry}>
          ⟳ Retry
        </button>
      )}

      {!fatalError && (
        <>
          <div className="overall-progress">
            <div className="overall-progress-label">
              <span>Overall Progress</span>
              <span>{overallPct}%</span>
            </div>
            <div
              className="bar-track"
              style={{ height: 8 }}
              role="progressbar"
              aria-valuenow={overallPct}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="Overall analysis progress"
            >
              <div className="bar-fill overall-progress-fill" style={{ width: `${overallPct}%` }} />
            </div>
          </div>

          <Timer startedAt={startedAt} fraction={displayFraction} isDone={isDone} />

          {progress?.stage === 'deep_enrichment' && !!progress.total_patents && (
            <PatentProgress
              current={progress.current_patent}
              completed={progress.patents_completed ?? 0}
              total={progress.total_patents}
            />
          )}

          <div className="activity-section">
            <div className="activity-title">Activity</div>
            <ActivityLog entries={progress?.activity} />
          </div>
        </>
      )}

      <div className="progress-footnote" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 5 }}><Monitor size={11} />Inference running on your machine · No data sent to the cloud</div>
    </div>
  );
}
