/**
 * Regression tests for the progress fraction and remaining-time estimator.
 *
 * Runnable with any of vitest / jest / `node --experimental-strip-types --test`.
 * The imports below use node:test + node:assert so they work with zero extra
 * dependencies in this offline repo.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { stagesForMode, overallFraction } from '../pipelineStages.ts';
import { createEtaState, advanceEta, etaDisplay, roundEtaMs } from '../etaEstimator.ts';
import type { AnalysisProgress } from '../../services/api.ts';

const deep = stagesForMode('deep');
const p = (o: Partial<AnalysisProgress>): AnalysisProgress =>
  ({ job_id: 'test', ...o }) as AnalysisProgress;

// ── overallFraction ─────────────────────────────────────────────────────────

test('fraction is 0 before any stage and 1 when done', () => {
  assert.equal(overallFraction(deep, null), 0);
  assert.equal(overallFraction(deep, p({ stage: 'done' })), 1);
});

test('fraction never leaves [0,1] for malformed progress', () => {
  const nasty: Partial<AnalysisProgress>[] = [
    { stage: 'analyzing', chunk_index: 99, total_chunks: 1 },
    { stage: 'analyzing', chunk_index: -5, total_chunks: 4 },
    { stage: 'analyzing', chunk_index: 1, total_chunks: 0 },
    { stage: 'deep_enrichment', patents_completed: 500, total_patents: 3 },
    { stage: 'analyzing', total_files: 0, current_file_index: 7 },
    { stage: 'analyzing', chunk_index: NaN as unknown as number, total_chunks: 4 },
  ];
  for (const n of nasty) {
    const f = overallFraction(deep, p(n));
    assert.ok(f >= 0 && f <= 1, `fraction out of range for ${JSON.stringify(n)}: ${f}`);
  }
});

test('fraction advances monotonically through a normal deep run', () => {
  const seq: Partial<AnalysisProgress>[] = [
    { stage: 'starting' },
    { stage: 'extracting_text' },
    { stage: 'analyzing', chunk_index: 0, total_chunks: 4 },
    { stage: 'analyzing', chunk_index: 2, total_chunks: 4 },
    { stage: 'analyzing', chunk_index: 4, total_chunks: 4 },
    { stage: 'deep_enrichment', patents_completed: 0, total_patents: 30 },
    { stage: 'deep_enrichment', patents_completed: 18, total_patents: 30 },
    { stage: 'deep_enrichment', patents_completed: 30, total_patents: 30 },
    { stage: 'verifying' },
    { stage: 'research' },
    { stage: 'persisting' },
    { stage: 'done' },
  ];
  let prev = -1;
  for (const step of seq) {
    const f = overallFraction(deep, p(step));
    assert.ok(f >= prev, `fraction went backwards at ${JSON.stringify(step)}: ${prev} -> ${f}`);
    prev = f;
  }
});

test('stale chunk counters do not leak into non-chunked stages', () => {
  // total_chunks lingers in the backend progress entry; a later stage must
  // ignore it rather than deriving its own position from it.
  const withStale = overallFraction(deep, p({ stage: 'verifying', chunk_index: 0, total_chunks: 4 }));
  const withoutStale = overallFraction(deep, p({ stage: 'verifying' }));
  assert.equal(withStale, withoutStale);
});

test('multi-file runs do not reset the overall bar per file', () => {
  const file1End = overallFraction(
    deep,
    p({ stage: 'persisting', total_files: 3, current_file_index: 0 }),
  );
  const file2Start = overallFraction(
    deep,
    p({ stage: 'starting', total_files: 3, current_file_index: 1 }),
  );
  assert.ok(file2Start >= file1End, `bar rewound between files: ${file1End} -> ${file2Start}`);
  assert.ok(file1End < 0.4, 'one of three files should not be most of the bar');
});

// ── ETA estimator ───────────────────────────────────────────────────────────

/** Drives the estimator over a fraction-vs-time function, 1 Hz. */
function runEta(durationS: number, fractionAt: (s: number) => number) {
  let st = createEtaState(0);
  const shown: (number | null)[] = [];
  for (let s = 0; s <= durationS; s++) {
    const f = fractionAt(s);
    st = advanceEta(st, s * 1000, f, 0);
    const d = etaDisplay(st, f, false);
    shown.push(d.kind === 'eta' ? d.remainingMs : null);
  }
  return shown;
}

function upwardTicks(vals: (number | null)[]) {
  let n = 0;
  for (let i = 1; i < vals.length; i++) {
    const a = vals[i - 1], b = vals[i];
    if (a != null && b != null && b > a) n++;
  }
  return n;
}

test('THE BUG: a long flat plateau must not make the ETA climb every tick', () => {
  // Fraction pinned at 0.19 for five straight minutes — the exact condition
  // that made the old `elapsed/f - elapsed` formula count upward at ~4x real
  // time. Under the old code this produced ~300 upward ticks.
  const shown = runEta(400, s => (s < 20 ? 0.05 + s * 0.007 : 0.19));
  const ups = upwardTicks(shown);
  assert.ok(ups <= 10, `ETA climbed on ${ups} ticks during a flat plateau`);
});

test('ETA never rises faster than the rate limit', () => {
  const shown = runEta(600, s => (s < 20 ? 0.05 + s * 0.005 : 0.15));
  for (let i = 1; i < shown.length; i++) {
    const a = shown[i - 1], b = shown[i];
    if (a == null || b == null) continue;
    // 1s tick, 0.35 rise rate, plus one 30s rounding bucket of slack.
    assert.ok(b - a <= 30_500, `ETA jumped ${(b - a) / 1000}s in one tick`);
  }
});

test('ETA counts down while real progress is being made', () => {
  const shown = runEta(300, s => Math.min(0.99, s / 300));
  const first = shown.find(v => v != null)!;
  const last = [...shown].reverse().find(v => v != null)!;
  assert.ok(last < first, `ETA did not decrease over a steady run: ${first} -> ${last}`);
  assert.ok(upwardTicks(shown) === 0, 'steady progress should never revise upward');
});

test('estimate is withheld until there is evidence for one', () => {
  const shown = runEta(60, s => Math.min(0.99, s / 300));
  assert.equal(shown[0], null, 'showed an estimate at t=0');
  assert.equal(shown[10], null, 'showed an estimate at t=10s with no rate history');
  assert.ok(shown[40] != null, 'never produced an estimate at all');
});

test('a fraction regression resets rather than corrupting the estimate', () => {
  let st = createEtaState(0);
  for (let s = 0; s <= 200; s++) st = advanceEta(st, s * 1000, Math.min(0.9, s / 200), 0);
  const before = st.displayedMs;
  assert.ok(before != null);
  // Next file starts: fraction drops hard.
  st = advanceEta(st, 201_000, 0.05, 0);
  assert.equal(st.displayedMs, null, 'stale estimate survived a regression');
  assert.equal(st.samples.length, 1, 'stale rate samples survived a regression');
});

test('estimate is finite and bounded even when progress stalls at once', () => {
  const shown = runEta(1200, s => (s < 25 ? 0.06 : 0.06));
  for (const v of shown) {
    if (v == null) continue;
    assert.ok(Number.isFinite(v), 'non-finite estimate');
    assert.ok(v <= 6 * 60 * 60 * 1000, 'estimate exceeded the 6h ceiling');
  }
});

test('roundEtaMs uses coarser buckets for larger estimates', () => {
  assert.equal(roundEtaMs(62_000), 60_000);       // 15s bucket
  assert.equal(roundEtaMs(9 * 60_000 + 20_000), 9 * 60_000 + 30_000); // 30s bucket
  assert.equal(roundEtaMs(40 * 60_000 + 20_000), 40 * 60_000);        // 60s bucket
});

test('etaDisplay reports done rather than a stale number', () => {
  let st = createEtaState(0);
  for (let s = 0; s <= 100; s++) st = advanceEta(st, s * 1000, Math.min(0.9, s / 100), 0);
  assert.equal(etaDisplay(st, 1, true).kind, 'done');
  assert.equal(etaDisplay(st, 0.999, false).kind, 'done');
});
