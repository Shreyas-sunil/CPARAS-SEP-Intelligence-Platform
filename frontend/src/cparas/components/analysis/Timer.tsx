/**
 * Elapsed / estimated-remaining clock for a running ISLD analysis (see the component comment
 * below).
 */

import { useEffect, useRef, useState } from 'react';
import { formatDuration } from '../../lib/pipelineStages';
import { createEtaState, advanceEta, etaDisplay } from '../../lib/etaEstimator';
import type { EtaState } from '../../lib/etaEstimator';

/**
 * Elapsed / remaining readout for a running analysis.
 *
 * `startedAt` is the real wall-clock timestamp the run began, so elapsed time
 * is always derived from `Date.now() - startedAt` rather than accumulated per
 * tick — a dropped or delayed interval callback (background tab, busy main
 * thread) therefore cannot make the elapsed clock drift.
 *
 * The remaining-time estimate is delegated to lib/etaEstimator, which tracks
 * the *rate* of real backend progress. The previous inline formula
 * (elapsed/f - elapsed) grew by (1-f)/f seconds for every second that passed
 * whenever the backend fraction was flat, which made the "Estimated
 * Remaining" value count upward for most of a run.
 */
export default function Timer({
  startedAt,
  fraction,
  isDone = false,
}: {
  startedAt: number;
  fraction: number;
  isDone?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const etaRef = useRef<EtaState>(createEtaState(Date.now()));
  const [eta, setEta] = useState(() => etaDisplay(etaRef.current, 0, false));

  // Reset the estimator whenever a new run starts, so a previous run's
  // samples can't seed (and skew) the next one's estimate.
  useEffect(() => {
    etaRef.current = createEtaState(Date.now());
    setEta(etaDisplay(etaRef.current, 0, false));
  }, [startedAt]);

  // Stop the interval once the run is finished — previously it kept firing
  // for the lifetime of the mounted component.
  useEffect(() => {
    if (isDone) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [isDone]);

  // Fold the newest observation into the estimator on every tick *and* on
  // every fraction change, so a fresh poll result is reflected immediately
  // rather than waiting for the next second boundary.
  useEffect(() => {
    const t = Date.now();
    etaRef.current = advanceEta(etaRef.current, t, fraction, startedAt);
    setEta(etaDisplay(etaRef.current, fraction, isDone));
  }, [now, fraction, startedAt, isDone]);

  const elapsedMs = Math.max(0, now - startedAt);

  let remainingText: string;
  let remainingTitle: string;
  switch (eta.kind) {
    case 'estimating':
      remainingText = 'Estimating…';
      remainingTitle = 'Waiting for enough progress data to estimate remaining time.';
      break;
    case 'finishing':
      remainingText = 'Finishing up…';
      remainingTitle = 'Almost done — the remaining work is shorter than the estimate resolution.';
      break;
    case 'done':
      remainingText = '—';
      remainingTitle = 'Complete.';
      break;
    default:
      remainingText = `~${formatDuration(eta.remainingMs)}`;
      remainingTitle = 'Estimated from the rate of real pipeline progress; refines as the run proceeds.';
  }

  return (
    <div className="timer-row">
      <div className="timer-block">
        <div className="timer-label" id="timer-elapsed-label">Elapsed Time</div>
        <div className="timer-value" aria-labelledby="timer-elapsed-label" aria-live="off">
          {formatDuration(elapsedMs)}
        </div>
      </div>
      <div className="timer-divider" />
      <div className="timer-block">
        <div className="timer-label" id="timer-remaining-label">Estimated Remaining</div>
        <div
          className="timer-value timer-value--muted"
          aria-labelledby="timer-remaining-label"
          aria-live="off"
          title={remainingTitle}
        >
          {remainingText}
        </div>
      </div>
    </div>
  );
}
