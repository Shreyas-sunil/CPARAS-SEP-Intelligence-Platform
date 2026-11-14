/**
 * Standard Mapping Table (Requirement 6):
 * Patent | Matched Standard | Matched Clause | Exact Phrases Matched | Similarity | Reason
 */
import type { StandardMappingRow } from '../../types';
import { SimBar } from './ResearchBadges';

/** Matched standard, clause, exact phrases matched and similarity per patent. */
export default function StandardMappingTable({ rows }: { rows: StandardMappingRow[] }) {
  if (!rows?.length) return null;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Application No.</th>
            <th>Matched Standard</th>
            <th>Matched Clause</th>
            <th>Exact Phrases Matched</th>
            <th>Similarity</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="td-appno">{r.patent}</td>
              <td><span className="spec-chip">{r.matched_standard}</span></td>
              <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                {r.matched_clause}
                {r.matched_clause_title && <div style={{ color: 'var(--muted)', fontSize: 10 }}>{r.matched_clause_title}</div>}
              </td>
              <td style={{ maxWidth: 300 }}>
                {r.matched_phrases?.length
                  ? <div className="phrase-list">{r.matched_phrases.map((ph, j) => <span key={j} className="phrase-chip">“{ph}”</span>)}</div>
                  : <span style={{ color: 'var(--muted)', fontSize: 11 }}>{r.matched_phrases ? 'None (semantic match)' : '—'}</span>}
                {r.matched_terms?.length ? (
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)', marginTop: 3 }}>
                    Terms: {r.matched_terms.join(', ')}
                  </div>
                ) : null}
              </td>
              <td style={{ minWidth: 110 }}><SimBar score={r.similarity} /></td>
              <td className="td-title" style={{ maxWidth: 320 }}>{r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
