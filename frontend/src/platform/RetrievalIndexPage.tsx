/**
 * 3GPP Retrieval Index page (/retrieval-index), Data Entry and Super Admin. Read-only: shows status
 * and contents and offers a keyword search; the index is never modified from the UI.
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { CheckCircle2, Copy, Database, FolderOpen, RefreshCw, Search, ShieldCheck, XCircle } from 'lucide-react';
import { Banner, Card, EmptyState, LoadingState, PageHeader, Pill, formatDateTime } from './ui';
import {
  errorMessage, formatBytes, getRetrievalIndexStatus, searchRetrievalIndex,
  type IndexHit, type RetrievalIndexStatus,
} from './retrievalApi';

/**
 * 3GPP Retrieval Index — where the backend expects the tgpp-sqlite-retrieval
 * output (dist/retrieval_data), whether every file is there, and a quick
 * keyword search to prove the index answers.
 */

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn btn-sm" type="button" title="Copy to clipboard"
      onClick={() => { navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }).catch(() => {}); }}>
      <Copy size={13} /> {done ? 'Copied' : 'Copy'}
    </button>
  );
}

/** Which index files are present in the folder the backend reads. */
function FileChecklist({ st }: { st: RetrievalIndexStatus }) {
  return (
    <div className="table-scroll">
      <table className="dtable">
        <thead><tr><th>File</th><th>Needed</th><th>Found</th><th className="t-num">Size</th></tr></thead>
        <tbody>
          {st.files.map(f => (
            <tr key={f.name}>
              <td className="t-mono">{f.name}</td>
              <td className="t-muted">{f.required ? 'Required' : 'Optional'}</td>
              <td>{f.present
                ? <Pill tone="ok" dot>Present</Pill>
                : <Pill tone={f.required ? 'err' : 'muted'} dot>Missing</Pill>}</td>
              <td className="t-num">{formatBytes(f.size_bytes)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Index status, location and problems with Refresh / Deep check actions. */
function StatusCard({ st, onRefresh, onDeep, busy }: { st: RetrievalIndexStatus; onRefresh: () => void; onDeep: () => void; busy: boolean }) {
  const folder = st.found_dir ?? st.expected_dir;
  const pill = st.ok ? <Pill tone="ok" dot>Ready</Pill> : st.configured ? <Pill tone="warn" dot>Not usable</Pill> : <Pill tone="muted" dot>Not installed</Pill>;
  return (
    <Card title="Index location" icon={<FolderOpen size={15} />}
      actions={<div className="row" style={{ gap: 6 }}>
        {pill}
        <button className="btn btn-sm" onClick={onRefresh} disabled={busy}><RefreshCw size={13} className={busy ? 'spin' : undefined} /> Refresh</button>
      </div>}>
      <div className="stack-sm">
        <div className="muted small">
          {st.using_default_folder
            ? <>The backend reads the index from this project folder (default). Copy every file of <span className="mono">retrieval_data</span> here:</>
            : <>The backend reads the folder set by <span className="mono">{st.env_var}</span> in <span className="mono">backend/.env</span>:</>}
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <code style={{ padding: '6px 10px', background: 'var(--c-surface-2)', borderRadius: 6, wordBreak: 'break-all', flex: '1 1 320px' }}>{folder}</code>
          <CopyButton text={folder} />
        </div>
        {!st.using_default_folder && (
          <div className="muted small">Default folder (used when {st.env_var} is empty): <span className="mono">{st.default_dir}</span></div>
        )}
        {st.problems.map(p => (
          <Banner key={p.code + p.message} tone={st.configured ? 'warn' : 'info'} title={p.code === 'incomplete_copy' ? 'Copy not finished' : p.code === 'not_configured' ? 'No index in that folder yet' : 'Index problem'}>
            {p.message}
          </Banner>
        ))}
        <FileChecklist st={st} />
        {st.ok && (
          <div className="row" style={{ gap: 8, alignItems: 'center' }}>
            <button className="btn btn-sm" onClick={onDeep} disabled={busy} title="SHA-256 of the whole database + SQLite quick_check. On a 20 GB index this takes many minutes.">
              <ShieldCheck size={13} /> Deep check (slow)
            </button>
            {st.deep_check && (st.deep_check.compatible && !st.deep_check.problems.length
              ? <Pill tone="ok" dot>Checksum and integrity OK</Pill>
              : <Pill tone="err" dot>{st.deep_check.problems.join('; ') || 'Deep check failed'}</Pill>)}
          </div>
        )}
      </div>
    </Card>
  );
}

/** What the index contains (documents, clauses, versions, content types, build info). */
function ContentsCard({ st }: { st: RetrievalIndexStatus }) {
  const n = (v?: number | null) => (v == null ? '—' : v.toLocaleString());
  const types = Object.entries(st.content_types ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <Card title="What the index contains" icon={<Database size={15} />}
      subtitle={st.counts_from === 'manifest' ? 'Counts from manifest.json' : 'Counts from the database'}>
      <div className="kv-rows">
        <div><span>Documents</span><span>{n(st.documents)}</span></div>
        <div><span>Specification versions</span><span>{n(st.specification_versions)}</span></div>
        <div><span>Distinct specifications</span><span>{n(st.distinct_spec_ids)}</span></div>
        <div><span>Clauses</span><span>{n(st.clauses)}</span></div>
        <div><span>Search rows</span><span>{n(st.search_rows)}</span></div>
        <div><span>Original full-text rows</span><span>{n(st.full_text_rows)}</span></div>
        <div><span>Database size</span><span>{formatBytes(st.database_size_bytes)}</span></div>
        <div><span>Built</span><span>{formatDateTime(st.build_timestamp)} {st.builder_version ? `· builder ${st.builder_version}` : ''}</span></div>
        <div><span>Schema</span><span>{st.schema_version ?? '—'}</span></div>
      </div>
      {types.length > 0 && (
        <div className="stack-sm" style={{ marginTop: 10 }}>
          <div className="muted small">Rows by content type (only <span className="mono">full_text</span> is original specification text; the rest are model-generated summaries from the dataset):</div>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            {types.map(([k, v]) => <Pill key={k} tone={k === 'full_text' ? 'info' : 'outline'}>{k} · {v.toLocaleString()}</Pill>)}
          </div>
        </div>
      )}
    </Card>
  );
}

/** Keyword search box over the index (no embedder needed). */
function SearchCard() {
  const [q, setQ] = useState('');
  const [spec, setSpec] = useState('');
  const [mode, setMode] = useState<'all' | 'any' | 'phrase'>('all');
  const [hits, setHits] = useState<IndexHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    if (q.trim().length < 2) return;
    setBusy(true); setErr(null);
    try { setHits((await searchRetrievalIndex(q.trim(), { spec: spec.trim() || undefined, mode })).results); }
    catch (ex) { setErr(errorMessage(ex)); setHits(null); }
    finally { setBusy(false); }
  };

  return (
    <Card title="Try a search" icon={<Search size={15} />} subtitle="Keyword (BM25) search straight against the index — no embedder involved.">
      <form onSubmit={run} className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
        <input className="input" style={{ flex: '2 1 260px' }} placeholder="e.g. registration procedure" value={q} onChange={e => setQ(e.target.value)} />
        <input className="input" style={{ flex: '1 1 120px' }} placeholder="Spec (e.g. 23.501)" value={spec} onChange={e => setSpec(e.target.value)} />
        <select className="select" value={mode} onChange={e => setMode(e.target.value as typeof mode)}>
          <option value="all">All words</option><option value="any">Any word</option><option value="phrase">Exact phrase</option>
        </select>
        <button className="btn btn-primary" type="submit" disabled={busy || q.trim().length < 2}><Search size={13} /> Search</button>
      </form>
      {err && <Banner tone="err">{err}</Banner>}
      {busy && <LoadingState label="Searching…" />}
      {!busy && hits && hits.length === 0 && <div className="muted small">No matches.</div>}
      {!busy && hits && hits.length > 0 && (
        <div className="table-scroll is-tall">
          <table className="dtable">
            <thead><tr><th>Specification</th><th>Version</th><th>Clause</th><th>Excerpt</th><th>Type</th><th className="t-num">Score</th></tr></thead>
            <tbody>
              {hits.map(h => (
                <tr key={h.record_id}>
                  <td><div className="t-mono">{h.doc_type ? `${h.doc_type} ` : ''}{h.spec_id ?? '—'}</div><div className="t-muted small clip" style={{ maxWidth: 220 }} title={h.spec_title ?? ''}>{h.spec_title}</div></td>
                  <td className="t-mono">{h.version ?? '—'}{h.release ? <div className="t-muted">Rel-{h.release}</div> : null}</td>
                  <td><div className="t-mono">{h.clause_id ?? '—'}</div><div className="t-muted small">{h.clause_title}</div></td>
                  <td className="t-wrap">{h.excerpt}</td>
                  <td><Pill tone={h.content_type === 'full_text' ? 'info' : 'outline'}>{h.content_type}</Pill></td>
                  <td className="t-num">{h.score.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/** 3GPP Retrieval Index page. */
export default function RetrievalIndexPage() {
  const [st, setSt] = useState<RetrievalIndexStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (deep = false) => {
    setBusy(true); setErr(null);
    try { setSt(await getRetrievalIndexStatus(deep)); }
    catch (e) { setErr(errorMessage(e)); }
    finally { setBusy(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="page">
      <PageHeader
        eyebrow="Shared reference data"
        title="3GPP Retrieval Index"
        description="The SQLite / FTS5 index built by tgpp-sqlite-retrieval. When it is present, CPARAS ISLD evidence, Text Matching and the CPARAS Bulk corpus read standards text from it (read-only)."
      />
      {err && <Banner tone="err" title="Cannot read the index status">{err}</Banner>}
      {!st && !err && <LoadingState label="Opening the index (the first check of a large index can take a few seconds)…" />}
      {st && (
        <div className="stack-sm" style={{ gap: 14 }}>
          <div className="grid-2">
            <StatusCard st={st} busy={busy} onRefresh={() => load(false)} onDeep={() => load(true)} />
            {st.ok ? <ContentsCard st={st} /> : (
              <Card title="How to install it" icon={st.configured ? <XCircle size={15} /> : <CheckCircle2 size={15} />}>
                <ol className="small" style={{ paddingLeft: 18, lineHeight: 1.8, margin: 0 }}>
                  <li>Stop the app if it is running.</li>
                  <li>Copy <b>all files</b> from <span className="mono">tgpp-sqlite-retrieval\dist\retrieval_data\</span> into the folder shown on the left.</li>
                  <li>Wait for the copy of <span className="mono">search_index.sqlite</span> to finish (it is about 20 GB).</li>
                  <li>Start the app again and press <b>Refresh</b>. Leave <span className="mono">{st.env_var}</span> empty in <span className="mono">backend/.env</span>.</li>
                </ol>
                <div className="muted small" style={{ marginTop: 10 }}>PowerShell, from the project folder:</div>
                <code style={{ display: 'block', padding: '6px 10px', background: 'var(--c-surface-2)', borderRadius: 6, marginTop: 4, wordBreak: 'break-all' }}>
                  robocopy D:\3gpp\tgpp-sqlite-retrieval\dist\retrieval_data .\dist\retrieval_data /E
                </code>
              </Card>
            )}
          </div>
          {st.ok ? <SearchCard /> : (
            <Card><EmptyState icon={<Database size={20} />} title="Search becomes available once the index is installed"
              body="Until then the app keeps using the Standards Corpus vector store and its built-in reference clauses exactly as before." /></Card>
          )}
        </div>
      )}
    </div>
  );
}
