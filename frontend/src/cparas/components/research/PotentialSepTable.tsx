/**
 * Potential SEP Table (Requirement 6):
 * Patent | Topic | Reason | Missing Standard Section | Predicted Future Version
 */
import type { PotentialSepRow } from '../../types';

/** Table of potential-SEP candidates; renders nothing when there are none. */
export default function PotentialSepTable({ rows }: { rows: PotentialSepRow[] }) {
  if (!rows?.length) {
    return (
      <div className="note" style={{ padding: '4px 0' }}>
        No potential-SEP candidates found at the current similarity threshold —
        every analyzed patent's strongest match was already among its declared standards.
      </div>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Application No.</th>
            <th>Topic</th>
            <th>Reason</th>
            <th>Missing Standard Section</th>
            <th>Predicted Future Version</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="td-appno">{r.patent}</td>
              <td><span className="domain-chip">{r.topic}</span></td>
              <td className="td-title" style={{ maxWidth: 340 }}>{r.reason}</td>
              <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{r.missing_standard_section}</td>
              <td><span className="spec-chip">{r.predicted_future_version}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
