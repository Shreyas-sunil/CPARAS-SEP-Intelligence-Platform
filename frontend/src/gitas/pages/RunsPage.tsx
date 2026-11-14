/** CPARAS Bulk Uploads & Runs page (/bulk/runs). */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { FileSpreadsheet, FileWarning, LayoutDashboard, Loader2, Play, Pause, RefreshCw, Table2, UploadCloud, ShieldCheck } from 'lucide-react';
import * as sep from '../api';
import type { SepRun } from '../api';
import UploadPanel, { RunStatusPill } from '../components/UploadPanel';
import { PRODUCTS } from '../../platform/products';
import { Banner, Card, EmptyState, LoadingState, PageHeader, ProgressBar, formatDateTime } from '../../platform/ui';

const product = PRODUCTS.bulk;

/** CPARAS Bulk — upload a bulk export and manage every scoring run:
 * progress, pause/continue, enriched Excel and failed-row downloads. */
export default function RunsPage() {
  const [runs, setRuns] = useState<SepRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const refresh = useCallback(() => {
    sep.getRuns().then(d => { setRuns(d.runs); setError(null); }).catch(() => setError('The run list could not be loaded.'));
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Keep processing runs' progress live.
  const anyProcessing = !!runs?.some(r => r.status === 'processing');
  useEffect(() => {
    if (!anyProcessing) return;
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, [anyProcessing, refresh]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(b => ({ ...b, [key]: true }));
    try {
      await fn();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'That action failed. Please try again.');
    } finally {
      setBusy(b => ({ ...b, [key]: false }));
      refresh();
    }
  };

  return (
    <div className="page">
      <PageHeader
        product={product}
        title="Uploads & Runs"
        description="Upload an ETSI bulk IPR-declaration export. Each upload becomes a run that is scored in the background; re-uploading a file creates a new, complete run and never alters earlier ones."
      />

      <div className="stack">
        <div className="grid-main-side" style={{ gridTemplateColumns: 'minmax(0, 1.4fr) minmax(0, 1fr)' }}>
          <Card title="New upload" icon={<UploadCloud size={15} />}>
            <UploadPanel onUploaded={refresh} />
          </Card>
          <Card title="Row preservation" icon={<ShieldCheck size={15} />}>
            <ul className="prose" style={{ fontSize: 12.5 }}>
              <li>Every non-empty source row becomes one record, in the original order.</li>
              <li>Rows that repeat an application number, publication number, title or company — even fully identical rows — are all kept.</li>
              <li>The enriched Excel returns your original sheet unchanged with CPARAS_Bulk_* columns appended to each row.</li>
            </ul>
          </Card>
        </div>

        <Card title={<>Runs {runs && <span className="card-sub num">· {runs.length}</span>}</>}
          actions={<button className="btn btn-sm btn-ghost" onClick={refresh} title="Refresh"><RefreshCw size={13} /> Refresh</button>} flush>
          {error && <div style={{ padding: 14 }}><Banner tone="err">{error}</Banner></div>}
          {runs === null && !error && <LoadingState label="Loading runs…" />}
          {runs && runs.length === 0 && (
            <EmptyState icon={<FileSpreadsheet size={20} />} title="No runs yet" body="Upload a bulk export above to create the first run." />
          )}
          {runs && runs.length > 0 && (
            <div className="table-scroll">
              <table className="dtable">
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Uploaded</th>
                    <th>Status</th>
                    <th style={{ minWidth: 150 }}>Progress</th>
                    <th className="t-num">Rows</th>
                    <th className="t-num">Failed</th>
                    <th className="t-num">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map(r => {
                    const resumable = (r.status === 'paused' || r.status === 'failed') && r.processed_rows < r.total_rows;
                    return (
                      <tr key={r.id}>
                        <td className="t-clip" style={{ maxWidth: 260 }} title={r.filename}>
                          <div style={{ fontWeight: 500 }} className="clip">{r.filename}</div>
                          <div className="small muted">Run #{r.id}{r.sheet ? ` · sheet ${r.sheet}` : ''}</div>
                        </td>
                        <td className="nowrap t-muted">{formatDateTime(r.created_at)}</td>
                        <td><RunStatusPill status={r.status} /></td>
                        <td>
                          <ProgressBar value={r.processed_rows} max={r.total_rows || 1} indeterminate={r.status === 'processing' && r.processed_rows === 0} />
                          <div className="small muted num" style={{ marginTop: 3 }}>{r.processed_rows.toLocaleString()} / {r.total_rows.toLocaleString()}</div>
                          {r.detail && <div className="small muted" style={{ marginTop: 2 }}>{r.detail}</div>}
                        </td>
                        <td className="t-num">{r.total_rows.toLocaleString()}</td>
                        <td className="t-num" style={{ color: r.failed_rows ? 'var(--c-err)' : undefined }}>{r.failed_rows.toLocaleString()}</td>
                        <td>
                          <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'nowrap', gap: 4 }}>
                            {r.status === 'processing' && (
                              <button className="btn btn-sm" disabled={busy[`p${r.id}`]} onClick={() => act(`p${r.id}`, () => sep.pauseRun(r.id))} title="Pause after the current batch">
                                <Pause size={13} /> Pause
                              </button>
                            )}
                            {resumable && (
                              <button className="btn btn-sm btn-primary" disabled={busy[`c${r.id}`]} onClick={() => act(`c${r.id}`, () => sep.continueRun(r.id))} title="Continue from the last stored row">
                                {busy[`c${r.id}`] ? <Loader2 size={13} className="spin" /> : <Play size={13} />} Continue
                              </button>
                            )}
                            <Link className="btn btn-sm btn-icon" to={`/bulk?run=${r.id}`} title="Open in dashboard" aria-label="Open in dashboard"><LayoutDashboard size={14} /></Link>
                            <Link className="btn btn-sm btn-icon" to={`/bulk/registry?run=${r.id}`} title="Open registry (source row order)" aria-label="Open registry"><Table2 size={14} /></Link>
                            <button className="btn btn-sm btn-icon" disabled={busy[`x${r.id}`] || r.processed_rows === 0}
                              onClick={() => act(`x${r.id}`, () => sep.downloadRunExcel(r.id))}
                              title="Download enriched Excel (original rows + CPARAS_Bulk_* columns)" aria-label="Download enriched Excel">
                              {busy[`x${r.id}`] ? <Loader2 size={14} className="spin" /> : <FileSpreadsheet size={14} />}
                            </button>
                            <button className="btn btn-sm btn-icon" disabled={busy[`f${r.id}`] || !r.failed_rows}
                              onClick={() => act(`f${r.id}`, () => sep.downloadRunFailedRows(r.id))}
                              title={r.failed_rows ? 'Download failed-row report (CSV)' : 'No failed rows'} aria-label="Download failed-row report">
                              <FileWarning size={14} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
