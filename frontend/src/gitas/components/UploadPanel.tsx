/**
 * CPARAS Bulk upload panel: file picker, upload progress, live scoring progress with a rate-based
 * ETA, and pause/stop/continue for the newest run.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { UploadCloud, FileSpreadsheet, Pause, Square, Play, Loader2 } from 'lucide-react';
import { Banner, Pill, ProgressBar } from '../../platform/ui';
import { uploadWorkbook, getRunStatus, pauseRun, stopRun, continueRun } from '../api';
import type { SepRunStatus } from '../api';

// ── ETA (v0.7.2 rework, REQUIRED LARGE-DATASET ARCHITECTURE item 9) ────────
// The old estimator averaged the rate over the ENTIRE run since started_at.
// That's fine once a job has been running a while, but it's exactly wrong
// early on (a slow first batch — cold TF-IDF corpus embed, cold DB
// connection pool — permanently drags the average down for the rest of a
// 50k+ row run) and it reacts far too slowly to a genuine rate change
// (pause/continue, GC pause, another job starting). A ring buffer of
// recent (timestamp, processed) samples fixes both: the estimate is based
// on the processing rate over a recent WINDOW, not the whole run, so it
// converges quickly and adapts if the rate changes, while still requiring
// enough samples/elapsed time before showing a number (avoids a wild
// first-tick estimate).
const ETA_WINDOW_MS = 20_000; // rolling window: last 20s of samples
const ETA_MAX_SAMPLES = 40;   // bounded ring buffer regardless of poll rate
const ETA_MIN_ELAPSED_MS = 2000; // don't estimate off <2s of signal
const ETA_MIN_SAMPLES = 2;

/** (time, rows processed) sample for the rolling ETA. */
export interface EtaSample { t: number; processed: number; }

/** Pure function so it's independently testable: given the full sample
 * history (oldest..newest) and the run's remaining count, returns a
 * formatted "processed / total · ~Xm Ys remaining" string, or a state
 * label for initialization/zero-progress/completed/failed/cancelled. */
export function computeEtaLabel(
  samples: EtaSample[],
  processed: number,
  total: number,
  status: SepRunStatus['status'],
): string {
  const processedStr = `${processed.toLocaleString()} / ${total.toLocaleString()} processed`;

  if (status === 'completed') return `${processedStr} · done`;
  if (status === 'completed_with_errors') return `${processedStr} · done (with warnings)`;
  if (status === 'failed') return `${processedStr} · failed`;
  if (status === 'paused') return `${processedStr} · paused`;

  const remaining = Math.max(0, total - processed);
  if (remaining === 0) return `${processedStr} · finishing…`;
  if (processed <= 0 || samples.length < ETA_MIN_SAMPLES) return `${processedStr} · estimating…`;

  // Windowed rate: compare the oldest sample still inside ETA_WINDOW_MS to
  // the newest sample. Falls back to the very first sample (whole-run
  // average) only if we don't yet have ETA_WINDOW_MS of history — i.e.
  // early in a run the estimate gracefully widens its window instead of
  // reporting nothing, then narrows to the rolling window once there's
  // enough history for it to be more accurate than the whole-run average.
  const newest = samples[samples.length - 1];
  const cutoff = newest.t - ETA_WINDOW_MS;
  let base = samples[0];
  for (const s of samples) {
    if (s.t >= cutoff) { base = s; break; }
  }
  const dt = newest.t - base.t;
  const dProcessed = newest.processed - base.processed;

  if (dt < ETA_MIN_ELAPSED_MS) return `${processedStr} · estimating…`;
  if (dProcessed <= 0) {
    // Rate has genuinely stalled (0 rows processed across the whole
    // window) — say so rather than showing a stale/misleading ETA.
    return `${processedStr} · rate stalled, re-estimating…`;
  }

  const ratePerMs = dProcessed / dt;
  const remainingMs = remaining / ratePerMs;
  return `${processedStr} · ${formatDuration(remainingMs)} remaining`;
}

function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000));
  if (totalSec < 60) return `~${totalSec}s`;
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  if (min < 60) return `~${min}m ${sec}s`;
  const hr = Math.floor(min / 60);
  return `~${hr}h ${min % 60}m`;
}

/** Maintains the rolling sample buffer across polls for one run. Resets
 * whenever the run_id changes (a new upload) so a previous run's history
 * never leaks into a new one's estimate. */
function useEtaSamples(run: SepRunStatus | null): EtaSample[] {
  const bufRef = useRef<EtaSample[]>([]);
  const runIdRef = useRef<number | null>(null);

  if (run && runIdRef.current !== run.run_id) {
    bufRef.current = [];
    runIdRef.current = run.run_id;
  }

  if (run && run.status === 'processing') {
    const buf = bufRef.current;
    const last = buf[buf.length - 1];
    // Only append on genuine progress ticks (avoids padding the buffer
    // with duplicate samples between poll intervals when nothing changed,
    // which would otherwise bias the windowed rate toward "no progress").
    if (!last || last.processed !== run.processed) {
      buf.push({ t: Date.now(), processed: run.processed });
      const cutoffIdx = buf.findIndex(s => s.t >= Date.now() - ETA_WINDOW_MS * 3);
      if (cutoffIdx > 0) buf.splice(0, cutoffIdx);
      if (buf.length > ETA_MAX_SAMPLES) buf.splice(0, buf.length - ETA_MAX_SAMPLES);
    }
  }
  return bufRef.current;
}

const STATUS_TONE: Record<SepRunStatus['status'], 'info' | 'ok' | 'warn' | 'err' | 'muted'> = {
  processing: 'info', paused: 'warn', completed: 'ok', completed_with_errors: 'warn', failed: 'err',
};
const STATUS_LABEL: Record<SepRunStatus['status'], string> = {
  processing: 'Processing', paused: 'Paused', completed: 'Completed', completed_with_errors: 'Completed with warnings', failed: 'Failed',
};

/** Coloured pill for a run status (processing, paused, completed, failed...). */
export function RunStatusPill({ status }: { status: SepRunStatus['status'] | string }) {
  const s = status as SepRunStatus['status'];
  return <Pill tone={STATUS_TONE[s] ?? 'muted'} dot>{STATUS_LABEL[s] ?? status}</Pill>;
}

/**
 * CPARAS Bulk upload: drop an ETSI bulk export, then follow its background
 * scoring run live (processed / failed / remaining, ETA) with
 * Pause / Stop / Continue. Every source row of the file becomes one record.
 */
export default function UploadPanel({ onUploaded }: { onUploaded: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<{ ok: boolean; message: string; warnings?: string[] } | null>(null);

  // Scoring runs as a background job (api/routes/gitas/dashboard.py's
  // _process_bulk_run) — POST /upload returns almost immediately with a
  // run_id, then this polls GET /runs/{run_id}/status for real progress.
  const [run, setRun] = useState<SepRunStatus | null>(null);
  const [jobActionBusy, setJobActionBusy] = useState(false);

  useEffect(() => {
    if (!run || (run.status !== 'processing')) return;
    const t = setInterval(async () => {
      try {
        const s = await getRunStatus(run.run_id);
        setRun(s);
        if (s.status !== 'processing') onUploaded();
      } catch {
        // transient poll failure — try again next tick
      }
    }, 1200);
    return () => clearInterval(t);
  }, [run?.run_id, run?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const doUpload = async (file: File) => {
    setBusy(true);
    setResult(null);
    setProgress(0);
    setRun(null);
    try {
      const data = await uploadWorkbook(file, (pct) => setProgress(pct));
      setResult({
        ok: true,
        message: `Read ${Number(data.total_rows).toLocaleString()} source row${data.total_rows === 1 ? '' : 's'} from “${data.filename}” (sheet ${data.sheet}). Every row is kept and scored in the background.`,
        warnings: data.warnings,
      });
      setRun({
        run_id: data.run_id, status: data.status || 'processing', filename: data.filename,
        parsed: data.total_rows, queued: data.total_rows, processing: true, processed: 0,
        completed: null, failed: 0, remaining: data.total_rows, error_message: null,
        started_at: null, completed_at: null, run_meta: null, stoppable: true, resumable: false,
      });
      onUploaded();
    } catch (err: any) {
      const isTimeout = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '');
      const detail = err?.response?.data?.detail;
      const msg = isTimeout
        ? 'The upload/parse is taking longer than expected for a file this large. It may still be finishing on the server — check the runs list in a minute before re-uploading.'
        : typeof detail === 'string' ? detail : detail?.message || 'Upload failed — check the file format.';
      setResult({ ok: false, message: msg, warnings: typeof detail === 'object' ? detail?.warnings : undefined });
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  const onFiles = (files: FileList | null) => {
    if (!files || !files.length || busy) return;
    doUpload(files[0]);
  };

  const runAction = async (action: (runId: number) => Promise<unknown>) => {
    if (!run) return;
    setJobActionBusy(true);
    try {
      await action(run.run_id);
      setRun(await getRunStatus(run.run_id));
    } catch (err: any) {
      setResult({ ok: false, message: err?.response?.data?.detail || 'That action failed. Please try again.' });
    } finally {
      setJobActionBusy(false);
    }
  };

  const etaSamples = useEtaSamples(run);
  const etaLabel = useMemo(
    () => run ? computeEtaLabel(etaSamples, run.processed, run.parsed, run.status) : '',
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [run?.processed, run?.parsed, run?.status, run?.run_id, etaSamples.length],
  );

  return (
    <div className="stack p-bulk">
      <div
        className={`dropzone${drag ? ' is-drag' : ''}${busy ? ' is-busy' : ''}`}
        role="button"
        tabIndex={0}
        aria-label="Upload an ETSI bulk declaration export"
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inputRef.current?.click(); } }}
        onDragOver={e => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={e => { e.preventDefault(); setDrag(false); onFiles(e.dataTransfer.files); }}
        onClick={() => !busy && inputRef.current?.click()}
      >
        <input ref={inputRef} type="file" accept=".xlsx,.xls,.csv" style={{ display: 'none' }} onChange={e => onFiles(e.target.files)} />
        <div className="dropzone-icon">{busy ? <Loader2 size={20} className="spin" /> : <UploadCloud size={20} />}</div>
        <div className="dropzone-title">{busy ? `Uploading… ${progress}%` : 'Drop an ETSI bulk declaration export here'}</div>
        <div className="dropzone-sub">{busy ? 'Reading the workbook — large files can take a little while.' : 'or click to browse · one row per patent family member per declared standard'}</div>
        {busy ? <ProgressBar value={progress} style={{ width: 260, marginTop: 8 }} /> : (
          <div className="dropzone-formats">
            <span className="chip">.xlsx</span><span className="chip">.xls</span><span className="chip">.csv</span>
          </div>
        )}
      </div>

      {result && (
        <Banner tone={result.ok ? 'ok' : 'err'} title={result.ok ? 'Upload received' : 'Upload failed'}>
          {result.message}
          {!!result.warnings?.length && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          )}
        </Banner>
      )}

      {run && (
        <div className="card">
          <div className="card-header">
            <div className="card-title" style={{ minWidth: 0 }}>
              <FileSpreadsheet size={15} />
              <span className="clip" title={run.filename}>{run.filename}</span>
            </div>
            <div className="card-actions">
              <RunStatusPill status={run.status} />
              {run.stoppable && run.status === 'processing' && (
                <>
                  <button className="btn btn-sm" disabled={jobActionBusy} onClick={() => runAction(pauseRun)}><Pause size={13} /> Pause</button>
                  <button className="btn btn-sm btn-danger" disabled={jobActionBusy} onClick={() => runAction(stopRun)}><Square size={13} /> Stop</button>
                </>
              )}
              {run.resumable && (
                <button className="btn btn-sm btn-primary" disabled={jobActionBusy} onClick={() => runAction(continueRun)}>
                  {jobActionBusy ? <Loader2 size={13} className="spin" /> : <Play size={13} />} Continue
                </button>
              )}
            </div>
          </div>
          <div className="card-body stack-sm">
            <ProgressBar value={run.processed} max={run.parsed || 1} indeterminate={run.status === 'processing' && run.processed === 0} />
            <div className="run-progress-meta">
              <span>{etaLabel}</span>
              <span>
                <b>{run.failed.toLocaleString()}</b> failed · <b>{Math.max(0, run.parsed - run.processed).toLocaleString()}</b> remaining
              </span>
            </div>
            {run.status === 'failed' && run.error_message && <Banner tone="err">{run.error_message}</Banner>}
          </div>
        </div>
      )}
    </div>
  );
}
