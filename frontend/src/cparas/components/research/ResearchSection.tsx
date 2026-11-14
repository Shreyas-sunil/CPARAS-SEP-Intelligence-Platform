/**
 * Top-level "Research-Style Analysis" section for one declaration.
 * Composes the metrics panel, the four paper-style tables (Requirement 6),
 * and the figure gallery (Requirements 7 & 10). Renders nothing if the
 * declaration has no `research_outputs` (e.g. analyses from before this
 * feature, until recomputed via POST /analysis/history/{id}/research).
 */
import type { ResearchOutputs } from '../../types';
import PatentClassificationTable from './PatentClassificationTable';
import StandardMappingTable from './StandardMappingTable';
import PotentialSepTable from './PotentialSepTable';
import VerificationTable from './VerificationTable';
import ResearchMetricsPanel from './ResearchMetricsPanel';
import ResearchFigures from './ResearchFigures';

/**
 * All research outputs of a declaration: metrics, tables (classification, standard mapping with
 * matched phrases, potential SEPs, verification) and figures.
 */
export default function ResearchSection({ ro }: { ro?: ResearchOutputs | null }) {
  if (!ro || !ro.records?.length) return null;
  const { meta, tables, metrics, aggregates, figures } = ro;

  return (
    <>
      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-header">
          <div className="ph-left"><div className="panel-dot" style={{ background: 'var(--green)' }} /><h2>Research-Style Analysis</h2></div>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)' }}>
            {meta.embedder_mode === 'tfidf-fallback' ? 'TF-IDF fallback' : meta.embedder_mode} · corpus {meta.corpus_size} refs · threshold {Math.round(meta.similarity_threshold * 100)}%
          </span>
        </div>
        <div className="panel-body">
          <div className="note" style={{ marginTop: 0, marginBottom: 16 }}>{meta.disclaimer}</div>
          <ResearchMetricsPanel metrics={metrics} aggregates={aggregates} />
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-header"><div className="ph-left"><div className="panel-dot" style={{ background: 'var(--accent)' }} /><h2>Patent Classification</h2></div></div>
        <div className="panel-body"><PatentClassificationTable rows={tables.patent_classification} /></div>
      </div>

      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-header"><div className="ph-left"><div className="panel-dot" style={{ background: 'var(--accent2)' }} /><h2>Standard Mapping</h2></div></div>
        <div className="panel-body"><StandardMappingTable rows={tables.standard_mapping} /></div>
      </div>

      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-header"><div className="ph-left"><div className="panel-dot" style={{ background: 'var(--yellow)' }} /><h2>Potential SEPs ({tables.potential_seps.length})</h2></div></div>
        <div className="panel-body"><PotentialSepTable rows={tables.potential_seps} /></div>
      </div>

      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-header"><div className="ph-left"><div className="panel-dot" style={{ background: 'var(--green)' }} /><h2>Version Verification</h2></div></div>
        <div className="panel-body"><VerificationTable rows={tables.verification} /></div>
      </div>

      {figures && Object.keys(figures).length > 0 && (
        <div className="panel" style={{ marginBottom: 12 }}>
          <div className="panel-header"><div className="ph-left"><div className="panel-dot" /><h2>Figures</h2></div></div>
          <div className="panel-body"><ResearchFigures figures={figures} /></div>
        </div>
      )}
    </>
  );
}
