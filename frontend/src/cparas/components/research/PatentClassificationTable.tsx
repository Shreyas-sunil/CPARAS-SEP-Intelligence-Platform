/**
 * Patent Classification Table (Requirement 6):
 * Patent | Probability | Predicted SEP | Recommended Standard | Version | Confidence
 */
import type { PatentClassificationRow } from '../../types';
import { PredictedStatusBadge, SimBar } from './ResearchBadges';

/** Table of research classification rows (one per patent). */
export default function PatentClassificationTable({ rows }: { rows: PatentClassificationRow[] }) {
  if (!rows?.length) return null;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Application No.</th>
            <th>Probability</th>
            <th>Predicted SEP</th>
            <th>Recommended Standard</th>
            <th>Version</th>
            <th>Confidence</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="td-appno">{r.patent}</td>
              <td style={{ minWidth: 110 }}><SimBar score={r.probability} /></td>
              <td><PredictedStatusBadge status={r.predicted_sep} /></td>
              <td><span className="spec-chip">{r.recommended_standard}</span></td>
              <td style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>{r.version}</td>
              <td style={{ minWidth: 110 }}><SimBar score={r.confidence} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
