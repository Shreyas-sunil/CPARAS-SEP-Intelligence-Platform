/**
 * Remaining-time estimator for the analysis progress UI.
 *
 * WHY THIS EXISTS
 * ---------------
 * The previous estimate was computed inline in Timer.tsx as:
 *
 *     remaining = elapsed / fraction - elapsed        // == elapsed * (1-f)/f
 *
 * That formula is only stable while `fraction` is advancing. Whenever the
 * backend fraction is flat — which is most of the time, because the LLM
 * stages only report sub-progress when a chunk/batch *finishes* — `elapsed`
 * keeps growing every tick while `f` stays constant, so the "remaining"
 * number grows by (1-f)/f seconds for every real second that passes. At
 * f=0.08 (a typical value during extraction/analysis) the countdown ran
 * *backwards* at roughly 11x real time. That is the "estimated time keeps
 * going up" bug.
 *
 * WHAT THIS DOES INSTEAD
 * ----------------------
 * 1. Estimates the *rate* of progress (Δfraction/Δtime) over a sliding
 *    window, rather than assuming the average rate so far holds forever.
 * 2. Blends the recent rate with the whole-run average rate, weighted
 *    toward the recent rate as evidence accumulates, and floors the rate
 *    so a temporary stall can't make the estimate explode.
 * 3. Smooths the result with an EMA so it doesn't jitter between polls.
 * 4. Renders a *monotonically non-increasing* countdown: the displayed
 *    value ticks down in real time and is only ever revised downward.
 *    It never counts up.
 *
 * Honesty note: because the display can't count up, it can reach zero
 * before the work is done. That case is reported explicitly as
 * `kind: 'finishing'` so the UI can say "Finishing up…" rather than show
 * a number it knows is wrong. Genuine restarts (fraction going backwards,
 * e.g. moving to the next file) reset the estimator instead of being
 * absorbed as a silent 20-minute jump.
 */

/** Samples older than this are dropped from the rate window. */
const RATE_WINDOW_MS = 90_000;
/**
 * EMA smoothing is asymmetric on purpose. Good news (the job will finish
 * sooner than we thought) is applied quickly; bad news is applied slowly, so
 * a momentary stall nudges the estimate instead of yanking it.
 */
const EMA_ALPHA_DOWN = 0.30;
const EMA_ALPHA_UP = 0.08;
/**
 * The blended rate is never allowed below this multiple of the whole-run
 * average rate. Without it, a stalled stage drives rate→0 and the raw
 * estimate →∞; with it, a stall can inflate the estimate by at most 4x.
 */
const MIN_RATE_FACTOR = 0.25;
/**
 * Hard cap on how fast the *displayed* countdown may rise: 0.35 s of extra
 * estimate per 1 s of real time. The old code effectively allowed ~11 s/s,
 * which is what made the number visibly race upward. Combined with the
 * coarse rounding in `roundEtaMs`, an upward revision changes the rendered
 * value at most once every ~40 s, which reads as "stalled", not "counting up".
 */
const MAX_RISE_RATE = 0.35;
/** Absolute ceiling on any estimate, so a near-zero rate can't render "999:59". */
const MAX_REMAINING_MS = 6 * 60 * 60 * 1000;
/** Fraction must move at least this much to count as real forward progress. */
const PROGRESS_EPSILON = 1e-4;

// ── Gating: refuse to show a number until the evidence supports one ──────────
// An estimate produced in the first few seconds is dominated by the cheap
// setup stages and systematically under-predicts a multi-minute LLM run.
// Showing "00:39" and then having to walk it up is exactly the failure this
// module exists to prevent, so we show "Estimating…" until all three hold.
const MIN_ELAPSED_FOR_ESTIMATE_MS = 20_000;
const MIN_FRACTION_FOR_ESTIMATE = 0.05;
const MIN_WINDOW_FOR_ESTIMATE_MS = 12_000;

/** At or above this fraction we treat the run as effectively complete. */
const DONE_FRACTION = 0.995;

/** One observation of backend progress (time, fraction done). */
export interface EtaSample {
  t: number;
  f: number;
}

/** Rolling estimator state (recent samples and the smoothed rate). */
export interface EtaState {
  /** Sliding window of (time, fraction) observations, oldest first. */
  samples: EtaSample[];
  /** EMA-smoothed raw estimate, or null before the first estimate. */
  smoothedMs: number | null;
  /** The monotonically non-increasing value actually shown, or null. */
  displayedMs: number | null;
  /** Timestamp of the last advance() call, used to tick the countdown down. */
  lastTickAt: number;
  /** Highest fraction seen, used to detect genuine regressions. */
  maxFraction: number;
}

export type EtaDisplay =
  | { kind: 'estimating' }
  | { kind: 'finishing' }
  | { kind: 'done' }
  | { kind: 'eta'; remainingMs: number };

/** Fresh estimator state for a run that started at `now`. */
export function createEtaState(now: number): EtaState {
  return {
    samples: [],
    smoothedMs: null,
    displayedMs: null,
    lastTickAt: now,
    maxFraction: 0,
  };
}

const clamp01 = (n: number) => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

/**
 * Fold one observation into the estimator and return the next state.
 * Pure: does not mutate `state`.
 */
export function advanceEta(
  state: EtaState,
  now: number,
  fraction: number,
  startedAt: number,
): EtaState {
  const f = clamp01(fraction);
  const elapsed = Math.max(0, now - startedAt);

  // A genuine regression (next file started, or the backend corrected
  // itself) invalidates every rate sample we have. Rebuild from here
  // rather than letting a stale, much-higher fraction distort the rate.
  if (f < state.maxFraction - PROGRESS_EPSILON) {
    return {
      samples: [{ t: now, f }],
      smoothedMs: null,
      displayedMs: null,
      lastTickAt: now,
      maxFraction: f,
    };
  }

  const samples = [...state.samples, { t: now, f }].filter(
    (sample, i, arr) => sample.t >= now - RATE_WINDOW_MS || i === arr.length - 1,
  );

  const dt = Math.max(0, now - state.lastTickAt);
  const next: EtaState = {
    samples,
    smoothedMs: state.smoothedMs,
    displayedMs: state.displayedMs,
    lastTickAt: now,
    maxFraction: Math.max(state.maxFraction, f),
  };

  const oldest = samples[0];
  const windowDt = now - oldest.t;

  const ready =
    elapsed >= MIN_ELAPSED_FOR_ESTIMATE_MS &&
    f >= MIN_FRACTION_FOR_ESTIMATE &&
    windowDt >= MIN_WINDOW_FOR_ESTIMATE_MS &&
    f < 1;

  if (!ready) {
    // Keep an already-established countdown ticking, but don't manufacture
    // a first estimate out of noise.
    if (next.displayedMs != null) next.displayedMs = Math.max(0, next.displayedMs - dt);
    return next;
  }

  // Whole-run average rate — stable, but slow to react.
  const globalRate = elapsed > 0 ? f / elapsed : 0;
  // Recent rate over the sliding window — reactive, but noisy, and zero
  // during a stall.
  const recentRate = windowDt > 0 ? (f - oldest.f) / windowDt : 0;

  // Trust the recent rate more as the window fills up.
  const w = clamp01(windowDt / RATE_WINDOW_MS);
  let rate = w * recentRate + (1 - w) * globalRate;
  // Floor the rate so a stall inflates the estimate by a bounded amount
  // instead of sending it to infinity.
  rate = Math.max(rate, globalRate * MIN_RATE_FACTOR);

  if (!(rate > 0)) {
    if (next.displayedMs != null) next.displayedMs = Math.max(0, next.displayedMs - dt);
    return next;
  }

  const rawMs = Math.min(MAX_REMAINING_MS, Math.max(0, (1 - f) / rate));

  if (state.smoothedMs == null) {
    next.smoothedMs = rawMs;
  } else {
    const alpha = rawMs < state.smoothedMs ? EMA_ALPHA_DOWN : EMA_ALPHA_UP;
    next.smoothedMs = state.smoothedMs + alpha * (rawMs - state.smoothedMs);
  }

  const smoothed = next.smoothedMs;

  if (state.displayedMs == null) {
    next.displayedMs = smoothed;
    return next;
  }

  const tickedDown = Math.max(0, state.displayedMs - dt);
  if (smoothed <= tickedDown) {
    // Good news arrived faster than the clock — snap down to it.
    next.displayedMs = smoothed;
  } else if (smoothed <= state.displayedMs) {
    // Estimate is broadly consistent with the countdown — just count down.
    next.displayedMs = tickedDown;
  } else {
    // The job is running longer than predicted. Let the estimate grow, but
    // strictly rate-limited so it never reads as a countdown running backwards.
    next.displayedMs = Math.min(smoothed, state.displayedMs + MAX_RISE_RATE * dt);
  }

  return next;
}

/**
 * Coarse rounding for display. Second-by-second precision on an estimate
 * this uncertain is false precision, and it is what makes small revisions
 * look like churn. Buckets widen with the magnitude of the estimate.
 */
export function roundEtaMs(ms: number): number {
  const bucket = ms < 5 * 60_000 ? 15_000 : ms < 15 * 60_000 ? 30_000 : 60_000;
  return Math.round(ms / bucket) * bucket;
}

/**
 * Maps estimator state onto what the UI should actually render.
 *
 * `finishing` is reported rather than showing "00:00" while work is still
 * running — the countdown bottoming out means the estimate was short, and
 * saying so is more honest than displaying a zero that never resolves.
 */
export function etaDisplay(state: EtaState, fraction: number, isDone: boolean): EtaDisplay {
  if (isDone || fraction >= DONE_FRACTION) return { kind: 'done' };
  if (state.displayedMs == null) return { kind: 'estimating' };
  const rounded = roundEtaMs(state.displayedMs);
  if (rounded < 15_000) return { kind: 'finishing' };
  return { kind: 'eta', remainingMs: rounded };
}
