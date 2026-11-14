/**
 * Standards Corpus page (/corpus): crawl and index 3GPP series into the Standards Corpus vector
 * store and follow jobs and batches. Viewing needs reference.view (Data
 * Entry); starting/cancelling crawls needs reference.manage (Super Admin).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Loading } from '../components/common';
import * as scraper from '../scraperApi';
import type { SeriesInfo, CrawlJob } from '../scraperApi';
import { Banner, Kpi, PageHeader } from '../../platform/ui';
import { useAuth } from '../../platform/auth/AuthContext';

interface ActivityLine { id: string; time: string; text: string; kind: 'ok' | 'progress' | 'warn' | 'error'; }

const KIND_ICON: Record<ActivityLine['kind'], string> = { ok: '✓', progress: '→', warn: '⚠', error: '✕' };
const KIND_COLOR: Record<ActivityLine['kind'], string> = {
  ok: 'var(--g-essential)', progress: 'var(--g-blue)', warn: '#b2740f', error: 'var(--g-nonessential)',
};

const STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  pending: { label: 'Queued', color: 'var(--g-blue)', bg: 'rgba(37,99,235,.08)' },
  crawling: { label: 'Crawling', color: 'var(--g-blue)', bg: 'rgba(37,99,235,.08)' },
  indexing: { label: 'Indexing', color: '#7c3aed', bg: 'rgba(124,58,237,.08)' },
  done: { label: 'Completed', color: 'var(--g-essential)', bg: 'var(--g-essential-bg)' },
  failed: { label: 'Failed', color: 'var(--g-nonessential)', bg: 'var(--g-nonessential-bg)' },
  cancelled: { label: 'Cancelled', color: 'var(--g-muted)', bg: 'rgba(100,116,139,.08)' },
};

/** Crawl job status badge. */
function StatusBadge({ status }: { status: string }) {
  const m = STATUS_META[status] || { label: status, color: 'var(--g-muted)', bg: 'rgba(100,116,139,.08)' };
  return <span className="g-badge" style={{ background: m.bg, color: m.color }}>{m.label}</span>;
}

/** Determinate or indeterminate progress bar (indeterminate until the file total is known). */
function ProgressBar({ percent, indeterminate }: { percent: number | null; indeterminate?: boolean }) {
  if (indeterminate || percent == null) {
    return (
      <div style={{ height: 8, borderRadius: 4, background: 'var(--g-border)', overflow: 'hidden', position: 'relative' }}>
        <div className="g-indeterminate-bar" style={{ position: 'absolute', top: 0, bottom: 0, width: '35%', background: 'var(--g-blue)', borderRadius: 4 }} />
      </div>
    );
  }
  return (
    <div style={{ height: 8, borderRadius: 4, background: 'var(--g-border)', overflow: 'hidden' }}>
      <div style={{ width: `${Math.min(percent, 100)}%`, height: '100%', background: 'var(--g-blue)' }} />
    </div>
  );
}

const FILE_STATUS_META: Record<string, { icon: string; label: string; color: string }> = {
  queued: { icon: '○', label: 'Queued', color: 'var(--g-muted)' },
  downloading: { icon: '↓', label: 'Downloading', color: 'var(--g-blue)' },
  downloaded: { icon: '✓', label: 'Downloaded', color: 'var(--g-essential)' },
  skipped: { icon: '✓', label: 'Skipped (already had it)', color: 'var(--g-muted)' },
  failed: { icon: '✕', label: 'Failed', color: 'var(--g-nonessential)' },
};

function formatBytes(n: number | null): string {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Paged, filterable list of the files of one crawl job. */
function FileList({ jobId }: { jobId: number }) {
  const [data, setData] = useState<scraper.JobFilesResponse | null>(null);
  const [status, setStatus] = useState('');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('filename');
  const [page, setPage] = useState(0);
  const pageSize = 25;

  const load = useCallback(() => {
    scraper.listJobFiles(jobId, { status: status || undefined, search: search || undefined, sort, page, pageSize })
      .then(setData).catch(() => {});
  }, [jobId, status, search, sort, page]);

  useEffect(() => { load(); }, [load]);
  // Poll while this file list is open -- files can still be transitioning
  // (downloading/failed) even after the job-level card stops re-rendering
  // every second, since job-level polling and this component's polling are
  // independent. Capped at a single interval per open FileList, cleared on
  // unmount (closing the section) -- no duplicate timers.
  useEffect(() => {
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  if (!data) return <div style={{ fontSize: 11.5, color: 'var(--g-muted)', padding: '8px 0' }}>Loading files…</div>;

  const totalPages = Math.max(1, Math.ceil(data.total / pageSize));
  const statusTabs = ['', 'downloading', 'downloaded', 'queued', 'skipped', 'failed'];

  return (
    <div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 10, alignItems: 'center' }}>
        {statusTabs.map((s) => (
          <button
            key={s || 'all'}
            className="g-btn-ghost"
            style={{
              fontSize: 10.5, padding: '3px 8px',
              background: status === s ? 'var(--g-blue)' : undefined,
              color: status === s ? '#fff' : undefined,
            }}
            onClick={() => { setStatus(s); setPage(0); }}
          >
            {s ? (FILE_STATUS_META[s]?.label || s) : 'All'} {s ? `(${data.counts_by_status[s] || 0})` : `(${data.total})`}
          </button>
        ))}
        <input
          placeholder="Search filename…"
          defaultValue={search}
          style={{ fontSize: 11, padding: '4px 8px', borderRadius: 5, border: '1px solid var(--g-border)', width: 140 }}
          onKeyDown={(e) => { if (e.key === 'Enter') { setSearch((e.target as HTMLInputElement).value); setPage(0); } }}
        />
        <select className="g-btn-ghost" style={{ fontSize: 10.5, padding: '3px 6px' }} value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="filename">Sort: Filename</option>
          <option value="status">Sort: Status</option>
          <option value="newest">Sort: Newest</option>
          <option value="failed_first">Sort: Failed first</option>
        </select>
      </div>

      {data.files.length === 0 ? (
        <div style={{ fontSize: 11.5, color: 'var(--g-muted)' }}>No files match this filter.</div>
      ) : (
        <div style={{ maxHeight: 320, overflowY: 'auto' }}>
          {data.files.map((f) => {
            const meta = FILE_STATUS_META[f.status] || { icon: '?', label: f.status, color: 'var(--g-muted)' };
            return (
              <div key={f.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', borderBottom: '1px solid var(--g-border)', fontSize: 11.5 }}>
                <span style={{ color: meta.color, width: 14, textAlign: 'center' }}>{meta.icon}</span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={f.relative_path}>{f.filename}</span>
                <span style={{ color: meta.color, minWidth: 90, textAlign: 'right' }}>{meta.label}</span>
                {f.file_size != null && <span style={{ color: 'var(--g-muted)', minWidth: 60, textAlign: 'right' }}>{formatBytes(f.file_size)}</span>}
                {f.retry_count > 0 && <span style={{ color: 'var(--g-muted)', fontSize: 10 }}>({f.retry_count} retr{f.retry_count === 1 ? 'y' : 'ies'})</span>}
              </div>
            );
          })}
        </div>
      )}

      {totalPages > 1 && (
        <div style={{ display: 'flex', justifyContent: 'center', gap: 6, marginTop: 10 }}>
          <button className="g-btn-ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Previous</button>
          <span style={{ fontSize: 11, color: 'var(--g-muted)', padding: '5px 8px' }}>Page {page + 1} of {totalPages}</span>
          <button className="g-btn-ghost" disabled={page >= totalPages - 1} onClick={() => setPage((p) => p + 1)}>Next</button>
        </div>
      )}
    </div>
  );
}

/** One crawl job. `readOnly` hides cancel/retry for roles without reference.manage (v0.5.0). */
function JobCard({ job, onRetry, onCancel, readOnly = false }: { job: CrawlJob; onRetry: (id: number) => void; onCancel: (id: number) => void; readOnly?: boolean }) {
  const active = ['pending', 'crawling', 'indexing'].includes(job.status);
  const [showTech, setShowTech] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  return (
    <div className="g-card" style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <strong>{job.series}</strong>
          <StatusBadge status={job.status} />
          {job.current_operation && active && (
            <span style={{ fontSize: 11.5, color: 'var(--g-muted)' }}>&middot; {job.current_operation}</span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {active && !readOnly && (
            <button
              className="g-btn-ghost"
              disabled={cancelling}
              onClick={() => { setCancelling(true); onCancel(job.id); }}
              aria-label={`Cancel crawl for ${job.series}`}
            >
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
          {job.status === 'failed' && !readOnly && (
            <button className="g-btn-ghost" onClick={() => onRetry(job.id)} aria-label={`Retry crawl for ${job.series}`}>Retry</button>
          )}
          {job.status === 'cancelled' && !readOnly && (
            <button className="g-btn-ghost" onClick={() => onRetry(job.id)} aria-label={`Resume crawl for ${job.series}`}>Resume</button>
          )}
        </div>
      </div>

      {active && (
        <div style={{ marginBottom: 8 }}>
          <ProgressBar percent={job.percent} indeterminate={job.percent == null} />
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, fontSize: 11.5, color: 'var(--g-muted)' }}>
        <div><b style={{ color: 'inherit' }}>{job.files_downloaded}</b><br />Downloaded</div>
        <div><b style={{ color: 'inherit' }}>{job.files_skipped}</b><br />Skipped</div>
        <div><b style={{ color: job.files_failed > 0 ? 'var(--g-nonessential)' : 'inherit' }}>{job.files_failed}</b><br />Failed</div>
        <div><b style={{ color: 'inherit' }}>{job.files_discovered}</b><br />Discovered</div>
        <div><b style={{ color: 'inherit' }}>{job.chunks_indexed.toLocaleString()}</b><br />Chunks indexed</div>
      </div>

      {job.current_path && active && (
        <div style={{ marginTop: 8, fontSize: 11, fontFamily: 'monospace', color: 'var(--g-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          Current: {job.current_path}
        </div>
      )}

      {job.status === 'failed' && job.error && (
        <div style={{ marginTop: 10, padding: '8px 10px', borderRadius: 6, background: 'var(--g-nonessential-bg)', fontSize: 11.5 }}>
          <div style={{ color: 'var(--g-nonessential)', fontWeight: 600, marginBottom: 2 }}>
            {job.error_code === 'INDEXING_FAILED' ? 'Download succeeded, but indexing failed.' : 'Crawl failed.'}
          </div>
          <div>{job.retryable ? 'This is likely transient — retry should resolve it.' : 'This may need manual attention.'}</div>
          <button
            className="g-btn-ghost"
            style={{ marginTop: 6, fontSize: 10.5, padding: '2px 8px' }}
            onClick={() => setShowTech(v => !v)}
          >
            {showTech ? 'Hide' : 'Show'} technical details
          </button>
          {showTech && (
            <pre style={{ marginTop: 6, fontSize: 10, whiteSpace: 'pre-wrap', background: 'rgba(0,0,0,.04)', padding: 6, borderRadius: 4 }}>{job.error}</pre>
          )}
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <button className="g-btn-ghost" style={{ fontSize: 10.5, padding: '3px 8px' }} onClick={() => setShowFiles((v) => !v)} aria-expanded={showFiles}>
          {showFiles ? 'Hide' : 'Show'} specification files
        </button>
        {showFiles && (
          <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--g-border)' }}>
            <FileList jobId={job.id} />
          </div>
        )}
      </div>
    </div>
  );
}

/** Progress of one multi-series batch (actions hidden when read-only). */
function BatchPanel({ batch, onCancel, onRetry, readOnly = false }: { batch: scraper.CorpusBatch; onCancel: () => void; onRetry: () => void; readOnly?: boolean }) {
  const active = batch.status === 'queued' || batch.status === 'running';
  const seriesPct = batch.series_total > 0 ? (batch.series_completed / batch.series_total) * 100 : 0;
  const filesPct = batch.files_total ? Math.min((batch.files_processed / batch.files_total) * 100, 100) : null;

  const statusMeta: Record<string, { label: string; color: string }> = {
    queued: { label: 'Queued', color: 'var(--g-blue)' },
    running: { label: 'Running', color: 'var(--g-blue)' },
    completed: { label: 'Completed', color: 'var(--g-essential)' },
    completed_with_errors: { label: 'Completed with errors', color: '#b2740f' },
    failed: { label: 'Failed', color: 'var(--g-nonessential)' },
    cancelled: { label: 'Cancelled', color: 'var(--g-muted)' },
  };
  const meta = statusMeta[batch.status] || { label: batch.status, color: 'var(--g-muted)' };

  const childMeta: Record<string, { icon: string; color: string; label: string }> = {
    done: { icon: '✓', color: 'var(--g-essential)', label: 'Completed' },
    failed: { icon: '✕', color: 'var(--g-nonessential)', label: 'Failed' },
    cancelled: { icon: '○', color: 'var(--g-muted)', label: 'Cancelled' },
    pending: { icon: '○', color: 'var(--g-muted)', label: 'Queued' },
    crawling: { icon: '●', color: 'var(--g-blue)', label: 'Downloading' },
    indexing: { icon: '●', color: '#7c3aed', label: 'Indexing' },
  };

  return (
    <div className="g-card" style={{ marginBottom: 16, borderColor: active ? 'var(--g-blue)' : undefined }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div className="g-card-title" style={{ margin: 0 }}>
          {active ? 'Active Corpus Operation' : 'Corpus Operation'} — Batch #{batch.id}
        </div>
        <span style={{ fontFamily: 'monospace', fontSize: 11, fontWeight: 700, color: meta.color }}>{meta.label}</span>
      </div>

      <div style={{ fontSize: 12.5, marginBottom: 6 }}>
        {batch.series_completed} / {batch.series_total} series completed
      </div>
      <ProgressBar percent={seriesPct} />

      <div style={{ display: 'flex', gap: 20, marginTop: 12, marginBottom: 4, fontSize: 12 }}>
        <div>
          <div style={{ color: 'var(--g-muted)' }}>Files</div>
          <div style={{ fontWeight: 700 }}>
            {batch.files_processed}{batch.files_total != null ? ` / ${batch.files_total}` : ''} processed
          </div>
        </div>
        <div>
          <div style={{ color: 'var(--g-muted)' }}>Chunks indexed</div>
          <div style={{ fontWeight: 700 }}>{batch.chunks_indexed.toLocaleString()}</div>
        </div>
        {batch.series_failed > 0 && (
          <div>
            <div style={{ color: 'var(--g-nonessential)' }}>Series failed</div>
            <div style={{ fontWeight: 700, color: 'var(--g-nonessential)' }}>{batch.series_failed}</div>
          </div>
        )}
      </div>
      {filesPct != null && <ProgressBar percent={filesPct} />}

      <div style={{ marginTop: 14 }}>
        {batch.children.map((c) => {
          const cm = childMeta[c.status] || { icon: '?', color: 'var(--g-muted)', label: c.status };
          return (
            <div key={c.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '5px 0', borderBottom: '1px solid var(--g-border)', fontSize: 12 }}>
              <span style={{ color: cm.color, width: 14, textAlign: 'center' }}>{cm.icon}</span>
              <strong style={{ minWidth: 90 }}>{c.series}</strong>
              <span style={{ color: 'var(--g-muted)', flex: 1 }}>
                {c.files_downloaded + c.files_skipped}{c.total_files != null ? ` / ${c.total_files}` : ''} files
                {c.chunks_indexed > 0 && ` · ${c.chunks_indexed.toLocaleString()} chunks`}
              </span>
              <span style={{ color: cm.color, fontSize: 11 }}>{cm.label}</span>
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 12, display: readOnly ? 'none' : 'flex', gap: 8 }}>
        {active && <button className="g-btn-ghost" onClick={onCancel}>Cancel Batch</button>}
        {(batch.status === 'failed' || batch.status === 'cancelled' || batch.status === 'completed_with_errors') && (
          <button className="g-btn-ghost" onClick={onRetry}>Resume Batch</button>
        )}
      </div>
    </div>
  );
}

/** Standards Corpus page. */
export default function CorpusPage() {
  const [series, setSeries] = useState<SeriesInfo[]>([]);
  const [jobs, setJobs] = useState<CrawlJob[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [browseAll, setBrowseAll] = useState(false);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activity, setActivity] = useState<ActivityLine[]>([]);
  const [showTechLog, setShowTechLog] = useState(false);
  const [batches, setBatches] = useState<scraper.CorpusBatch[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // v0.5.0: starting/cancelling crawls changes shared reference data -> Super Admin only.
  const canManage = useAuth().can('reference.manage');
  const readOnly = !canManage;
  const batchPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevJobsRef = useRef<Map<number, CrawlJob>>(new Map());

  const refreshBatches = useCallback(() => {
    scraper.listBatches().then((r) => setBatches(r.batches)).catch(() => {});
  }, []);

  useEffect(() => { refreshBatches(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const hasActiveBatch = batches.some((b) => b.status === 'queued' || b.status === 'running');
    if (batchPollRef.current) { clearInterval(batchPollRef.current); batchPollRef.current = null; }
    if (hasActiveBatch) batchPollRef.current = setInterval(refreshBatches, 2000);
    return () => { if (batchPollRef.current) { clearInterval(batchPollRef.current); batchPollRef.current = null; } };
  }, [batches, refreshBatches]);

  // Derives a human-readable activity feed purely from real, observed
  // changes in backend job state across polls (file counts increasing,
  // current_path changing, status transitions) -- never a simulated
  // timer, per the "progress must reflect real backend state" requirement.
  const deriveActivity = useCallback((newJobs: CrawlJob[]) => {
    const lines: ActivityLine[] = [];
    const now = new Date().toLocaleTimeString([], { hour12: false });
    for (const j of newJobs) {
      const prev = prevJobsRef.current.get(j.id);
      if (!prev) {
        if (['pending', 'crawling'].includes(j.status)) {
          lines.push({ id: `${j.id}-start-${now}`, time: now, text: `Started crawl for ${j.series}`, kind: 'progress' });
        }
        continue;
      }
      if (j.files_downloaded > prev.files_downloaded) {
        const delta = j.files_downloaded - prev.files_downloaded;
        lines.push({ id: `${j.id}-dl-${now}-${j.files_downloaded}`, time: now, text: `Downloaded ${delta} file(s) for ${j.series}${j.current_path ? ` — ${j.current_path.split(/[\\/]/).pop()}` : ''}`, kind: 'ok' });
      }
      if (j.files_failed > prev.files_failed) {
        lines.push({ id: `${j.id}-fail-${now}-${j.files_failed}`, time: now, text: `Retry required for a file in ${j.series}`, kind: 'warn' });
      }
      if (j.status !== prev.status) {
        if (j.status === 'indexing') lines.push({ id: `${j.id}-idx-${now}`, time: now, text: `Indexing extracted specifications for ${j.series}`, kind: 'progress' });
        if (j.status === 'done') lines.push({ id: `${j.id}-done-${now}`, time: now, text: `${j.series} completed — ${j.chunks_indexed.toLocaleString()} chunks indexed`, kind: 'ok' });
        if (j.status === 'failed') lines.push({ id: `${j.id}-failed-${now}`, time: now, text: `${j.series} failed: ${j.error || 'unknown error'}`, kind: 'error' });
      }
    }
    if (lines.length) setActivity((prev) => [...lines, ...prev].slice(0, 40));
    prevJobsRef.current = new Map(newJobs.map((j) => [j.id, j]));
  }, []);

  const refreshJobs = useCallback(() => {
    scraper.listCrawlJobs().then((r) => { deriveActivity(r.jobs); setJobs(r.jobs); }).catch(() => {});
  }, [deriveActivity]);

  useEffect(() => {
    Promise.all([scraper.listSeries(), scraper.listCrawlJobs()])
      .then(([s, j]) => {
        setSeries(s);
        setJobs(j.jobs);
        prevJobsRef.current = new Map(j.jobs.map((job) => [job.id, job]));
      })
      .catch(() => setError('Could not load corpus data from the backend.'))
      .finally(() => setLoading(false));
  }, []);

  // Exactly one polling interval, only while something is active; cleaned
  // up on unmount or when nothing is active anymore -- see v0.3 spec's
  // "no duplicate polling" requirement.
  useEffect(() => {
    const hasActive = jobs.some((j) => ['pending', 'crawling', 'indexing'].includes(j.status));
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (hasActive) {
      pollRef.current = setInterval(refreshJobs, 2000);
    }
    return () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  }, [jobs, refreshJobs]);

  const recommended = series.find((s) => s.recommended);
  const activeJobs = jobs.filter((j) => ['pending', 'crawling', 'indexing'].includes(j.status));
  const historyJobs = jobs.filter((j) => !['pending', 'crawling', 'indexing'].includes(j.status));

  const activeSeriesSet = new Set(activeJobs.map((j) => j.series));

  const toggle = (s: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s); else next.add(s);
      return next;
    });
  };

  const startSeries = async (seriesList: string[]) => {
    if (seriesList.length === 0) return;
    setStarting(true);
    setError(null);
    try {
      if (seriesList.length > 1) {
        // Multiple series -> one trackable batch operation (v0.3 Part C),
        // not N independent fire-and-forget crawls.
        await scraper.startBatch(seriesList);
        refreshBatches();
      } else {
        await scraper.startCrawl(seriesList);
      }
      refreshJobs();
    } catch (e: any) {
      setError(e?.response?.data?.detail?.message || e?.response?.data?.detail || 'Failed to start crawl.');
    } finally {
      setStarting(false);
    }
  };

  const totalIndexedChunks = jobs.reduce((sum, j) => sum + (j.chunks_indexed || 0), 0);
  const totalFilesDownloaded = jobs.reduce((sum, j) => sum + (j.files_downloaded || 0), 0);
  const indexedSeriesCount = new Set(jobs.filter((j) => j.status === 'done').map((j) => j.series)).size;

  const header = (
    <PageHeader
      eyebrow="Reference data · shared by CPARAS Bulk and CPARAS ISLD"
      title="Standards Corpus"
      description="Download and index real 3GPP specifications for Text Matching, standards mapping and RAG enrichment."
    />
  );

  if (loading) return <div className="page">{header}<Loading label="Loading standards corpus…" /></div>;

  return (
    <div className="page">
      {header}

      {error && <Banner tone="err" style={{ marginBottom: 14 }}>{error}</Banner>}
      {readOnly && (
        <Banner tone="info" style={{ marginBottom: 14 }} title="Read-only">
          Downloading, indexing and cancelling 3GPP crawls is reserved for Super Admins. You can follow progress here.
        </Banner>
      )}

      <div className="kpi-grid kpi-grid--5" style={{ marginBottom: 14 }}>
        <Kpi label="Available series" value={series.length} color="var(--c-muted)" />
        <Kpi label="Files downloaded" value={totalFilesDownloaded.toLocaleString()} color="var(--c-bulk)" />
        <Kpi label="Chunks indexed" value={totalIndexedChunks.toLocaleString()} color="var(--c-ok)" />
        <Kpi label="Active jobs" value={activeJobs.length} color={activeJobs.length ? 'var(--c-accent)' : 'var(--c-muted-2)'} />
        <Kpi label="Series indexed" value={indexedSeriesCount} color="var(--c-isld)" />
      </div>

      {batches.filter((b) => b.status === 'queued' || b.status === 'running').map((b) => (
        <BatchPanel
          key={b.id}
          batch={b}
          onCancel={() => scraper.cancelBatch(b.id).then(refreshBatches)}
          onRetry={() => scraper.retryBatch(b.id).then(refreshBatches)}
          readOnly={readOnly}
        />
      ))}

      {recommended && (
        <div className="g-card" style={{ marginBottom: 16 }}>
          <div className="g-card-title">Quick start</div>
          <p style={{ fontSize: 12.5, marginBottom: 12 }}>
            Recommended: <strong>{recommended.series}</strong> — {recommended.label}
          </p>
          {activeSeriesSet.has(recommended.series) ? (
            <div style={{ fontSize: 12.5, color: 'var(--g-blue)' }}>Crawl already running for this series — see Active Jobs below.</div>
          ) : (
            <button className="g-btn-primary" disabled={starting || readOnly} onClick={() => startSeries([recommended.series])}>
              {starting ? 'Starting…' : `Download & Index ${recommended.series}`}
            </button>
          )}
          <div style={{ marginTop: 10 }}>
            <button className="g-btn-ghost" onClick={() => setBrowseAll((v) => !v)} aria-expanded={browseAll}>
              {browseAll ? 'Hide' : 'Browse'} all 3GPP series
            </button>
          </div>
        </div>
      )}

      {browseAll && (
        <div className="g-card" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, flexWrap: 'wrap', gap: 8 }}>
            <div className="g-card-title" style={{ margin: 0 }}>Series selector</div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="g-btn-ghost" onClick={() => setSelected(new Set(series.map((s) => s.series)))}>Select all</button>
              <button className="g-btn-ghost" onClick={() => setSelected(new Set())}>Clear selection</button>
              <button className="g-btn-primary" disabled={starting || selected.size === 0 || readOnly} onClick={() => startSeries(Array.from(selected))}>
                {starting ? 'Starting…' : `Download & Index Selected (${selected.size})`}
              </button>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 8 }}>
            {series.map((s) => {
              const isActive = activeSeriesSet.has(s.series);
              const lastDone = jobs.find((j) => j.series === s.series && j.status === 'done');
              return (
                <label
                  key={s.series}
                  style={{
                    display: 'flex', gap: 8, alignItems: 'flex-start', padding: 10, borderRadius: 8,
                    border: '1px solid var(--g-border)', fontSize: 12, cursor: isActive ? 'default' : 'pointer',
                    opacity: isActive ? 0.7 : 1,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(s.series)}
                    disabled={isActive}
                    onChange={() => toggle(s.series)}
                    aria-label={`Select ${s.series}`}
                  />
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <strong>{s.series}</strong>
                      {s.recommended && <span className="g-badge" style={{ background: 'var(--g-essential-bg)', color: 'var(--g-essential)' }}>Recommended</span>}
                      {isActive && <span className="g-badge" style={{ background: 'rgba(37,99,235,.08)', color: 'var(--g-blue)' }}>Crawling</span>}
                    </div>
                    <div style={{ color: 'var(--g-muted)', marginTop: 2 }}>{s.label}</div>
                    {lastDone && <div style={{ color: 'var(--g-muted)', marginTop: 2, fontSize: 10.5 }}>Last run: {lastDone.files_downloaded} files, {lastDone.chunks_indexed.toLocaleString()} chunks</div>}
                  </div>
                </label>
              );
            })}
          </div>
        </div>
      )}

      <div className="g-card" style={{ marginBottom: 16 }}>
        <div className="g-card-title">Active jobs</div>
        {activeJobs.length === 0 ? (
          <div style={{ fontSize: 12.5, color: 'var(--g-muted)' }}>No crawl is currently running.</div>
        ) : (
          <>
            {activeJobs.length > 1 && (
              <div style={{ fontSize: 12.5, color: 'var(--g-blue)', marginBottom: 10, fontWeight: 600 }}>
                {activeJobs.length} series currently processing
              </div>
            )}
            {activeJobs.map((j) => <JobCard key={j.id} job={j} readOnly={readOnly} onRetry={(id) => scraper.retryJob(id).then(refreshJobs)} onCancel={(id) => scraper.cancelJob(id).then(refreshJobs)} />)}
          </>
        )}
      </div>

      {activity.length > 0 && (
        <div className="g-card" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <div className="g-card-title" style={{ margin: 0 }}>Live activity</div>
            <button className="g-btn-ghost" onClick={() => setShowTechLog((v) => !v)}>{showTechLog ? 'Hide' : 'View'} technical log</button>
          </div>
          <div style={{ maxHeight: 220, overflowY: 'auto', fontFamily: 'monospace', fontSize: 11 }}>
            {activity.slice(0, showTechLog ? 40 : 10).map((a) => (
              <div key={a.id} style={{ display: 'flex', gap: 8, padding: '3px 0', color: KIND_COLOR[a.kind] }}>
                <span style={{ color: 'var(--g-muted)', minWidth: 66 }}>{a.time}</span>
                <span>{KIND_ICON[a.kind]}</span>
                <span style={{ color: a.kind === 'ok' || a.kind === 'error' ? KIND_COLOR[a.kind] : 'var(--g-text, inherit)' }}>{a.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {historyJobs.length > 0 && (
        <div className="g-card">
          <div className="g-card-title">Previous runs</div>
          {historyJobs.slice(0, 15).map((j) => <JobCard key={j.id} job={j} readOnly={readOnly} onRetry={(id) => scraper.retryJob(id).then(refreshJobs)} onCancel={(id) => scraper.cancelJob(id).then(refreshJobs)} />)}
        </div>
      )}

      {batches.filter((b) => !['queued', 'running'].includes(b.status)).length > 0 && (
        <div className="g-card" style={{ marginBottom: 16 }}>
          <div className="g-card-title">Corpus History</div>
          {batches.filter((b) => !['queued', 'running'].includes(b.status)).slice(0, 10).map((b) => {
            const statusLabels: Record<string, string> = {
              completed: 'Completed', completed_with_errors: 'Completed with errors',
              failed: 'Failed', cancelled: 'Cancelled',
            };
            return (
              <div key={b.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0', borderBottom: '1px solid var(--g-border)', fontSize: 12 }}>
                <span>Batch #{b.id} — {b.series_requested.join(', ')}</span>
                <span style={{ color: 'var(--g-muted)' }}>
                  {statusLabels[b.status] || b.status} &middot; {b.completed_at ? new Date(b.completed_at).toLocaleString() : ''}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {jobs.length === 0 && (
        <div className="g-card" style={{ textAlign: 'center', padding: '32px 20px' }}>
          <div style={{ fontFamily: 'monospace', fontSize: 11, letterSpacing: 1, color: 'var(--g-muted)', marginBottom: 8, textTransform: 'uppercase' }}>Standards corpus not indexed</div>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 8 }}>No specification corpus has been indexed yet.</div>
          {recommended && (
            <>
              <p style={{ fontSize: 12.5, color: 'var(--g-muted)', marginBottom: 14 }}>
                Recommended first step: download and index <strong>{recommended.series}</strong> — {recommended.label}.
              </p>
              <button className="g-btn-primary" disabled={starting || readOnly} onClick={() => startSeries([recommended.series])}>
                {starting ? 'Starting…' : `Download ${recommended.series}`}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
