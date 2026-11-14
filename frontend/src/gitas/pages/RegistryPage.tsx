/** CPARAS Bulk Patent Registry page (/bulk/registry). */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Search, Table2, UploadCloud, RefreshCw } from 'lucide-react';
import * as sep from '../api';
import type { Patent, PatentOrder, SepRun } from '../api';
import PatentRegistryTable from '../components/PatentRegistryTable';
import { PRODUCTS } from '../../platform/products';
import { Banner, Card, EmptyState, LoadingState, PageHeader, Pager } from '../../platform/ui';

const product = PRODUCTS.bulk;
const PAGE_SIZES = [50, 100, 200, 500];

/**
 * Full, paginated patent registry for CPARAS Bulk. One row per stored
 * record — i.e. per uploaded source row — so a file with repeated
 * application numbers shows every one of those rows. "Source order" lists
 * records exactly as they appear in the uploaded sheet.
 */
export default function RegistryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [runs, setRuns] = useState<SepRun[] | null>(null);
  const [runId, setRunId] = useState<number | 'all'>('all');
  const [q, setQ] = useState(searchParams.get('q') ?? '');
  const [query, setQuery] = useState(searchParams.get('q') ?? '');
  const [essentiality, setEssentiality] = useState('');
  const [order, setOrder] = useState<PatentOrder>((searchParams.get('order') as PatentOrder) || 'source');
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; patents: Patent[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    sep.getRuns().then(d => {
      setRuns(d.runs);
      const fromUrl = Number(searchParams.get('run'));
      if (d.runs.some(r => r.id === fromUrl)) setRunId(fromUrl);
      else if (d.runs.length) setRunId(d.runs[0].id);
    }).catch(() => setRuns([]));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const filters = useMemo(() => ({
    run_id: runId === 'all' ? undefined : runId,
    q: query || undefined,
    essentiality: essentiality ? [essentiality] : undefined,
  }), [runId, query, essentiality]);

  const load = useCallback(() => {
    if (runs === null || runs.length === 0) return;
    setLoading(true);
    setError(null);
    sep.getPatents(filters, limit, offset, order)
      .then(d => setData({ total: d.total, patents: d.patents }))
      .catch(() => setError('The registry could not be loaded.'))
      .finally(() => setLoading(false));
  }, [filters, limit, offset, order, runs]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { setOffset(0); }, [filters, limit, order]);

  useEffect(() => {
    const next = new URLSearchParams();
    if (runId !== 'all') next.set('run', String(runId));
    if (query) next.set('q', query);
    if (order !== 'source') next.set('order', order);
    setSearchParams(next, { replace: true });
  }, [runId, query, order, setSearchParams]);

  const run = typeof runId === 'number' ? runs?.find(r => r.id === runId) : undefined;

  if (runs !== null && runs.length === 0) {
    return (
      <div className="page page--narrow">
        <PageHeader product={product} title="Patent Registry" />
        <Card>
          <EmptyState icon={<Table2 size={22} />} title="No records yet" body="Upload an ETSI bulk export to populate the registry."
            action={<Link className="btn btn-product p-bulk" to="/bulk/runs"><UploadCloud size={14} /> Upload export</Link>} />
        </Card>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        product={product}
        title="Patent Registry"
        description={run
          ? <>Every stored record of <b>{run.filename}</b> — one per source row ({run.total_rows.toLocaleString()} rows uploaded). Repeated identifiers appear as separate rows.</>
          : 'Every stored record across all uploads — one per source row.'}
      />

      <Card flush
        title={<>Records {data && <span className="card-sub num">· {data.total.toLocaleString()}</span>}</>}
        actions={
          <form className="row" onSubmit={e => { e.preventDefault(); setQuery(q.trim()); }}>
            <select className="select" aria-label="Upload run" value={String(runId)} onChange={e => setRunId(e.target.value === 'all' ? 'all' : Number(e.target.value))} style={{ maxWidth: 240 }}>
              <option value="all">All runs</option>
              {(runs ?? []).map(r => <option key={r.id} value={r.id}>{r.filename}</option>)}
            </select>
            <div className="input-icon" style={{ width: 260 }}>
              <Search size={14} />
              <input className="input" placeholder="Title, application / publication no., ISLD, DIPG…" value={q} onChange={e => setQ(e.target.value)} aria-label="Search the registry" />
            </div>
            <select className="select" aria-label="Essentiality" value={essentiality} onChange={e => setEssentiality(e.target.value)}>
              <option value="">All essentiality</option>
              <option>Essential</option><option>Potential</option><option>Non-Essential</option>
            </select>
            <select className="select" aria-label="Sort order" value={order} onChange={e => setOrder(e.target.value as PatentOrder)}>
              <option value="source">Source row order</option>
              <option value="company">By company</option>
            </select>
            <button className="btn btn-sm btn-primary" type="submit">Search</button>
          </form>
        }
        footer={data && data.total > 0 ? (
          <>
            <Pager offset={offset} limit={limit} total={data.total} onChange={setOffset} disabled={loading} />
            <label className="row small">
              Rows per page
              <select className="select" value={limit} onChange={e => setLimit(Number(e.target.value))} style={{ height: 27 }}>
                {PAGE_SIZES.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
          </>
        ) : undefined}
      >
        {error && <div style={{ padding: 14 }}><Banner tone="err" actions={<button className="btn btn-sm" onClick={load}><RefreshCw size={13} /> Retry</button>}>{error}</Banner></div>}
        {!data && loading && <LoadingState label="Loading registry…" />}
        {data && data.patents.length === 0 && !loading && (
          <EmptyState icon={<Search size={20} />} title="No records match" body={query ? `Nothing matches “${query}”.` : 'Try a different run or filter.'} />
        )}
        {data && data.patents.length > 0 && (
          <div style={{ opacity: loading ? .55 : 1, transition: 'opacity .15s' }}>
            <PatentRegistryTable patents={data.patents} />
          </div>
        )}
      </Card>
    </div>
  );
}
