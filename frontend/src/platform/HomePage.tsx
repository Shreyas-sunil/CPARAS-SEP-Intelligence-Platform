/**
 * Platform home ("/") — shown to roles with CPARAS Bulk access (Data Entry,
 * Super Admin); Guests are redirected to ISLD by <HomeRoute> (App.tsx).
 *
 * Two product cards plus three status cards: Bulk uploads, the 3GPP
 * Retrieval Index (v0.5.0 — replaces the former "Standards knowledge base"
 * card; it shows the real document count and health from
 * GET /api/retrieval-index/status, read-only, never rebuilding anything) and
 * the latest Bulk activity.
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, CheckCircle2, Database, FileStack, History, UploadCloud } from 'lucide-react';
import { PRODUCTS, PLATFORM_NAME, PLATFORM_SUBTITLE, type Product } from './products';
import { Card, PageHeader, Pill, formatDateTime } from './ui';
import * as sep from '../gitas/api';
import { formatBytes, getRetrievalIndexStatus, type RetrievalIndexStatus } from './retrievalApi';
import { useAuth } from './auth/AuthContext';

/** Status card for the installed tgpp-sqlite-retrieval index. */
function RetrievalIndexCard({ ri }: { ri: RetrievalIndexStatus | null | undefined }) {
  return (
    <Card title="3GPP Retrieval Index" icon={<Database size={15} />} actions={<Link className="btn btn-sm" to="/retrieval-index">Open</Link>}>
      {ri === undefined ? <div className="skeleton" style={{ height: 38 }} /> : !ri ? (
        <div className="stack-sm">
          <Pill tone="warn" dot>Status unavailable</Pill>
          <div className="muted small">The backend could not report the index status.</div>
        </div>
      ) : (
        <div className="stack-sm">
          <div>
            {ri.ok ? <Pill tone="ok" dot>Ready</Pill>
              : ri.configured ? <Pill tone="warn" dot>Installed but not usable</Pill>
              : <Pill tone="warn" dot>Not installed</Pill>}
          </div>
          {ri.ok ? (
            <div className="kv-rows">
              <div><span>Documents</span><span>{(ri.documents ?? 0).toLocaleString()}</span></div>
              {ri.clauses != null && <div><span>Clauses</span><span>{ri.clauses.toLocaleString()}</span></div>}
              {ri.specification_versions != null && <div><span>Specification versions</span><span>{ri.specification_versions.toLocaleString()}</span></div>}
              {ri.database_size_bytes != null && <div><span>Index size</span><span>{formatBytes(ri.database_size_bytes)}</span></div>}
            </div>
          ) : (
            <div className="muted small">{ri.problems?.[0]?.message ?? `Copy the retrieval_data folder to ${ri.expected_dir}.`}</div>
          )}
          <div className="muted small">Used by ISLD standards mapping, Bulk clause scoring and Text Matching.</div>
        </div>
      )}
    </Card>
  );
}

/** Product summary card with its primary and secondary actions. */
function ProductCard({ product, cta, secondary }: { product: Product; cta: { to: string; label: string }; secondary?: { to: string; label: string } }) {
  const Icon = product.icon;
  return (
    <div className={`product-card ${product.className}`}>
      <div className="product-card-head">
        <span className="product-card-icon"><Icon size={20} /></span>
        <div style={{ minWidth: 0 }}>
          <div className="product-card-name">{product.name}</div>
          <div className="product-card-tag">{product.tagline}</div>
        </div>
      </div>
      <p style={{ fontSize: 12.8, color: 'var(--c-text-2)', lineHeight: 1.6 }}>{product.description}</p>
      <ul>
        {product.capabilities.map(c => <li key={c}><CheckCircle2 size={14} />{c}</li>)}
      </ul>
      <div className="row" style={{ marginTop: 'auto', paddingTop: 4 }}>
        <Link to={cta.to} className="btn btn-product">{cta.label} <ArrowRight size={14} /></Link>
        {secondary && <Link to={secondary.to} className="btn">{secondary.label}</Link>}
      </div>
    </div>
  );
}

/** Platform home page. */
export default function HomePage() {
  const { can } = useAuth();
  const [runs, setRuns] = useState<any[] | null>(null);
  // undefined = loading, null = the status call failed
  const [ri, setRi] = useState<RetrievalIndexStatus | null | undefined>(undefined);

  useEffect(() => {
    sep.getRuns().then(d => setRuns(d.runs)).catch(() => setRuns([]));
    if (can('reference.view')) getRetrievalIndexStatus().then(setRi).catch(() => setRi(null));
  }, [can]);

  const latest = runs?.[0];
  const totalRows = runs?.reduce((a, r) => a + (r.total_rows || 0), 0) ?? 0;

  return (
    <div className="page">
      <PageHeader
        eyebrow="C-DOT · Standard Essential Patents"
        title={`${PLATFORM_NAME} — ${PLATFORM_SUBTITLE}`}
        description="Two products on one offline analysis engine and one shared 3GPP Retrieval Index. Use CPARAS Bulk for whole exports and portfolios; use CPARAS ISLD to analyse individual declarations in depth."
      />

      <div className="grid-2" style={{ marginBottom: 14 }}>
        <ProductCard product={PRODUCTS.bulk} cta={{ to: '/bulk', label: 'Open CPARAS Bulk' }} secondary={{ to: '/bulk/runs', label: 'Upload export' }} />
        <ProductCard product={PRODUCTS.isld} cta={{ to: '/isld', label: 'Open CPARAS ISLD' }} secondary={{ to: '/isld/text-matching', label: 'Text Matching' }} />
      </div>

      <div className="grid-3">
        <Card title="Bulk uploads" icon={<UploadCloud size={15} />} actions={<Link className="btn btn-sm" to="/bulk/runs">Manage</Link>}>
          {runs === null ? <div className="skeleton" style={{ height: 38 }} /> : runs.length === 0 ? (
            <div className="muted small">No bulk exports uploaded yet.</div>
          ) : (
            <div className="kv-rows">
              <div><span>Runs</span><span>{runs.length.toLocaleString()}</span></div>
              <div><span>Source rows (all runs)</span><span>{totalRows.toLocaleString()}</span></div>
              <div><span>Latest</span><span className="clip" style={{ maxWidth: 180 }} title={latest?.filename}>{latest?.filename}</span></div>
            </div>
          )}
        </Card>
        {can('reference.view') && <RetrievalIndexCard ri={ri} />}
        <Card title="Latest activity" icon={<History size={15} />}>
          {latest ? (
            <div className="kv-rows">
              <div><span>Last upload</span><span>{formatDateTime(latest.created_at)}</span></div>
              <div><span>Status</span><span>{latest.status}</span></div>
              <div><span>Rows</span><span>{(latest.total_rows ?? 0).toLocaleString()}</span></div>
            </div>
          ) : (
            <div className="stack-sm">
              <div className="muted small">Start with a bulk export, or analyse a single declaration.</div>
              <div className="row">
                <Link className="btn btn-sm" to="/bulk/runs"><UploadCloud size={13} /> Bulk upload</Link>
                <Link className="btn btn-sm" to="/isld"><FileStack size={13} /> ISLD analysis</Link>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
