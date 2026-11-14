/**
 * Verification Table (Requirement 6):
 * Patent | Current Version | Future Version | Verification | Status
 */
import type { VerificationRow } from '../../types';
import { VerificationBadge } from './ResearchBadges';

/** Current vs later version alignment per patent. */
export default function VerificationTable({ rows }: { rows: VerificationRow[] }) {
  if (!rows?.length) return null;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Application No.</th>
            <th>Current Version</th>
            <th>Future Version</th>
            <th>Verification</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="td-appno">{r.patent}</td>
              <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{r.current_version}</td>
              <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{r.future_version || '—'}</td>
              <td className="td-title" style={{ maxWidth: 320 }}>{r.verification}</td>
              <td><VerificationBadge status={r.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
