/**
 * CPARAS Bulk patent detail (/bulk/patent/:id): the source record, its essentiality determination
 * with score breakdown and matched clause, and per-record export. Data: GET /api/sep/patents/{id}.
 */

import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ExternalLink, FileText, FileSpreadsheet, Scale, ScanText, ArrowLeft, Gauge } from 'lucide-react';
import * as sep from '../api';
import type { PatentDetail } from '../api';
import { EssentialityBadge, essColor, ScoreBar, highlightTerms, CountryFlag } from '../components/common';
import { PRODUCTS } from '../../platform/products';
import { Banner, Card, LoadingState, PageHeader, Pill } from '../../platform/ui';

const product = PRODUCTS.bulk;

/** Patent detail page. */
export default function PatentDetailPage() {
  const { id } = useParams();
  const [patent, setPatent] = useState<PatentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setPatent(null);
    setError(null);
    sep.getPatent(id).then(setPatent).catch(() => setError('This record could not be found. It may belong to a run that was removed.'));
  }, [id]);

  if (error) {
    return (
      <div className="page page--narrow">
        <PageHeader product={product} title="Patent detail" />
        <Banner tone="err" title="Record not found" actions={<Link className="btn btn-sm" to="/bulk/registry">Back to registry</Link>}>{error}</Banner>
      </div>
    );
  }
  if (!patent) return <div className="page"><LoadingState label="Loading patent…" /></div>;

  const det = patent.essentiality_determination;
  const mapping = patent.claim_to_standard_mapping;
  const sc = patent.spec_context;
  // A record with no declared application/publication number still has a
  // disclosure number to display, so show that instead of a blank id.
  const displayId = patent.application_number || patent.publication_number || patent.disclosure_number || `#${patent.id}`;
  const isldLink = `/isld/text-matching?ref_id=${patent.id}&ref_title=${encodeURIComponent(patent.title || '')}&ref_standard=${encodeURIComponent(patent.standard || '')}&ref_company=${encodeURIComponent(patent.company || '')}&ref_dipg_id=${encodeURIComponent(patent.dipg_id || '')}&ref_project=${encodeURIComponent(patent.project || '')}`;
  const failed = patent.row_status === 'FAILED';

  return (
    <div className="page">
      <PageHeader
        product={product}
        eyebrow={<>CPARAS Bulk · Patent detail</>}
        title={<span style={{ display: 'inline-flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}><span className="mono">{displayId}</span><EssentialityBadge status={patent.essentiality} /></span>}
        description={patent.title || undefined}
        actions={
          <>
            <Link className="btn" to="/bulk/registry"><ArrowLeft size={14} /> Registry</Link>
            <Link className="btn p-isld btn-product" to={isldLink} title="Match this record's claim text against the standards corpus in CPARAS ISLD">
              <ScanText size={14} /> Text match in CPARAS ISLD
            </Link>
            <a className="btn" href={`https://patents.google.com/?q=${encodeURIComponent(patent.application_number || patent.title || '')}`} target="_blank" rel="noreferrer">
              Google Patents <ExternalLink size={13} />
            </a>
          </>
        }
      >
        <div className="row" style={{ marginTop: 10 }}>
          {patent.source_row_number != null && <Pill tone="outline" title="Row in the uploaded sheet">Source row {patent.source_row_number}</Pill>}
          {patent.legal_status && <Pill tone="info">{patent.legal_status}</Pill>}
          {patent.release && <Pill>{patent.release}</Pill>}
          {patent.technology && <Pill>{patent.technology}</Pill>}
          {patent.standard && <Pill tone="outline">{patent.standard}</Pill>}
        </div>
      </PageHeader>

      {failed && (
        <Banner tone="err" title="Scoring failed for this row" style={{ marginBottom: 14 }}>
          {patent.row_error || 'No score could be computed.'} The row is still stored and exported with its original data.
        </Banner>
      )}

      <div className="grid-main-side">
        <div className="stack">
          <Card title="Core metadata">
            <div className="kv">
              <Meta
                label="Patent number"
                value={patent.application_number || patent.publication_number
                  ? <span className="mono">{patent.application_number || '—'}</span>
                  : patent.disclosure_number ? `Not declared (disclosure #${patent.disclosure_number})` : undefined}
              />
              <Meta label="Publication number" value={patent.publication_number ? <span className="mono">{patent.publication_number}</span> : undefined} />
              <Meta label="Declaring company" value={patent.company} link={patent.company ? `/bulk/company/${encodeURIComponent(patent.company)}` : undefined} />
              <Meta label="Country" value={<><CountryFlag code={patent.country_code} />{patent.country || patent.country_code || '—'}</>} />
              <Meta label="Application date" value={patent.filing_date || patent.signature_date} />
              <Meta label="Legal status" value={patent.legal_status || 'Not provided'} />
              <Meta label="Technology" value={patent.technology} />
              <Meta label="Release" value={patent.release} />
              <Meta label="Declared specification" value={patent.standard} />
              <Meta label="Technical domain" value={patent.domain} />
              <Meta label="Declaration (ISLD)" value={patent.iprd_reference ? <span className="mono">{patent.iprd_reference}</span> : undefined} />
              <Meta label="DIPG id" value={patent.dipg_id ? <span className="mono">{patent.dipg_id}</span> : undefined} />
              <Meta label="Family" value={`${patent.family_count} record${patent.family_count === 1 ? '' : 's'} in family`} />
              <Meta label="Tech body" value="3GPP" />
            </div>
          </Card>

          {sc && sc.spec_number && (
            <Card title={<>Specification context · TS {sc.spec_number}</>}
              subtitle={`Declared against TS ${sc.spec_number}${sc.spec_version ? ` v${sc.spec_version}` : ''}${patent.release ? ` (${patent.release})` : ''} — totals across this run`}>
              <div className="kpi-grid">
                <StatBox value={sc.total_seps} label="Records" color="var(--c-bulk)" />
                <StatBox value={sc.essential} label="Essential" color="var(--c-ok)" />
                <StatBox value={sc.potential} label="Potential" color="var(--c-warn)" />
                <StatBox value={sc.families ?? '—'} label="Families" color="var(--c-isld)" />
              </div>
            </Card>
          )}

          <Card title="Claim to standard mapping" icon={<Scale size={14} />}
            footer={<span>Match confidence <b className="num" style={{ color: 'var(--c-text)' }}>{Math.round(mapping.match_confidence)}%</b> · {mapping.summary}</span>}>
            <div className="g-mapping-grid">
              <div className="g-mapping-box claim">
                <div className="g-mapping-box-title">Patent claim (simplified)</div>
                <div>{highlightTerms(mapping.patent_claim_simplified, mapping.matched_key_terms) || <span className="muted">No claim text in the source row.</span>}</div>
              </div>
              <div className="g-mapping-box standard">
                <div className="g-mapping-box-title">Matched standard section</div>
                <div>{highlightTerms(mapping.matched_standard_section, mapping.matched_key_terms)}</div>
              </div>
            </div>
          </Card>

          {patent.related_patents.length > 0 && (
            <Card title={`Related records — same domain${patent.domain ? ` (${patent.domain})` : ''}`} subtitle={`${patent.related_patents.length} shown`}>
              <div className="g-related-grid">
                {patent.related_patents.map(r => (
                  <Link key={r.id} to={`/bulk/patent/${r.id}`} className="g-related-card">
                    <div className="g-related-id">{r.application_number || r.publication_number || r.disclosure_number || `#${r.id}`}</div>
                    <div className="g-related-title">{r.title}</div>
                    <div className="g-related-meta">{r.standard} · {r.company}</div>
                    <EssentialityBadge status={r.essentiality} />
                  </Link>
                ))}
              </div>
            </Card>
          )}
        </div>

        <div className="stack">
          <Card title="Essentiality determination" icon={<Gauge size={14} />} style={{ borderTop: `3px solid ${essColor(patent.essentiality)}` }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <EssentialityBadge status={det.status} />
              <span className="num" style={{ fontSize: 24, fontWeight: 600, color: essColor(patent.essentiality) }}>{Math.round(det.overall_confidence)}%</span>
            </div>
            <div className="g-score-track" style={{ marginBottom: 14 }}><div className="g-score-fill" style={{ width: `${det.overall_confidence}%`, background: essColor(patent.essentiality) }} /></div>
            <div className="section-label">Score breakdown</div>
            <ScoreBar label="Claim match" value={det.score_breakdown.claim_match} />
            <ScoreBar label="Semantic similarity" value={det.score_breakdown.semantic_similarity} />
            <ScoreBar label="Standard coverage" value={det.score_breakdown.standard_coverage} tip="Breadth of specification section covered by this patent" />
            <p className="small muted" style={{ marginTop: 8, lineHeight: 1.55 }}>{det.disclaimer}</p>
          </Card>

          <Card title="Export">
            <div className="stack-sm">
              <button className="btn btn-block" onClick={() => window.print()}><FileText size={14} /> Print / save as PDF</button>
              <button className="btn btn-block" onClick={() => exportRowCsv(patent)}><FileSpreadsheet size={14} /> Export this record (CSV)</button>
              {patent.declaration.isld_reference && (
                <button className="btn btn-block" disabled title="ETSI IPR search URL pattern not yet wired up">
                  <ExternalLink size={14} /> Open ISLD document
                </button>
              )}
            </div>
          </Card>

          <Card title="Quick stats">
            <div className="kv-rows">
              <div><span>Claim match</span><span>{patent.claim_match != null ? `${Math.round(patent.claim_match * 100)}%` : '—'}</span></div>
              <div><span>Semantic score</span><span>{patent.semantic_similarity != null ? `${Math.round(patent.semantic_similarity * 100)}%` : '—'}</span></div>
              <div><span>Family size</span><span>{patent.family_count}</span></div>
              <div><span>Filed</span><span>{(patent.filing_date || '').slice(0, 4) || '—'}</span></div>
              <div><span>Run</span><span><Link className="link" to={`/bulk?run=${patent.run_id}`}>#{patent.run_id}</Link></span></div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

/** Label / value pair (optionally linked). */
function Meta({ label, value, link }: { label: string; value: any; link?: string }) {
  const body = link ? <Link className="link" to={link}>{value}</Link> : (value ?? <span className="muted">—</span>);
  return (
    <div className="kv-item">
      <div className="kv-label">{label}</div>
      <div className="kv-value">{body}</div>
    </div>
  );
}

/** Large number with a caption. */
function StatBox({ value, label, color }: { value: any; label: string; color?: string }) {
  return (
    <div className="kpi" style={{ '--kpi-color': color } as React.CSSProperties}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{typeof value === 'number' ? value.toLocaleString() : value}</div>
    </div>
  );
}

function exportRowCsv(p: PatentDetail) {
  const rows = [
    ['Field', 'Value'],
    ['Source row', p.source_row_number != null ? String(p.source_row_number) : ''],
    ['Patent Number', p.application_number || ''],
    ['Publication Number', p.publication_number || ''],
    ['Title', p.title || ''],
    ['Company', p.company || ''],
    ['Essentiality', p.essentiality || ''],
    ['Overall Confidence %', String(p.essentiality_determination.overall_confidence)],
    ['Claim Match %', String(p.essentiality_determination.score_breakdown.claim_match)],
    ['Semantic Similarity %', String(p.essentiality_determination.score_breakdown.semantic_similarity)],
    ['Standard Coverage %', String(p.essentiality_determination.score_breakdown.standard_coverage)],
    ['Matched Standard', p.matched_standard || ''],
    ['ISLD Reference', p.declaration.isld_reference || ''],
  ];
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `${p.application_number || `cparas_bulk_record_${p.id}`}.csv`; a.click();
  URL.revokeObjectURL(url);
}
