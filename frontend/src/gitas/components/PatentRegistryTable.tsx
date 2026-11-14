/** CPARAS Bulk patent registry table component. */

import { Link } from 'react-router-dom';
import { EssentialityBadge } from './common';
import type { Patent } from '../api';

/** Patent registry table (CPARAS Bulk). One row per stored record — i.e.
 * per uploaded source row; repeated identifiers appear as separate rows. */
export default function PatentRegistryTable({ patents, showSourceRow = true }: { patents: Patent[]; showSourceRow?: boolean }) {
  if (!patents.length) return null;
  return (
    <div className="table-scroll">
      <table className="dtable">
        <thead>
          <tr>
            {showSourceRow && <th className="t-num" title="Row number in the uploaded sheet">Src row</th>}
            <th>Patent / application</th>
            <th>Essentiality</th>
            <th>Title</th>
            <th>Declaring company</th>
            <th>Declared standard</th>
            <th>Release</th>
            <th>ISLD reference</th>
            <th className="t-num">Claim match</th>
          </tr>
        </thead>
        <tbody>
          {patents.map(p => (
            <tr key={p.id}>
              {showSourceRow && <td className="t-num t-muted">{p.source_row_number ?? '—'}</td>}
              <td className="t-mono">
                <Link className="link" to={`/bulk/patent/${p.id}`}>{p.application_number || p.publication_number || p.disclosure_number || `#${p.id}`}</Link>
              </td>
              <td>
                {p.row_status === 'FAILED'
                  ? <span className="pill pill-err pill-dot" title={p.row_error || undefined}>Scoring failed</span>
                  : <EssentialityBadge status={p.essentiality} />}
              </td>
              <td className="t-wrap">{p.title || <span className="t-muted">—</span>}</td>
              <td className="t-clip" title={p.company || undefined}>{p.company || '—'}</td>
              <td className="t-clip" title={p.standard || undefined}>{p.standard || '—'}</td>
              <td className="nowrap">{p.release || '—'}</td>
              <td className="t-mono t-muted nowrap">{p.iprd_reference || '—'}</td>
              <td className="t-num">{p.claim_match != null ? `${Math.round(p.claim_match * 100)}%` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
