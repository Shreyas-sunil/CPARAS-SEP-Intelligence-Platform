/**
 * Live activity log of a running ISLD analysis (entries come only from the backend's progress
 * feed).
 */

import type { ActivityEntry } from '../../services/api';

/** Every entry here comes straight from the backend's progress `activity`
 * log (real pipeline events — file uploaded, pages processed, patents
 * extracted, batch N/M analyzed, etc). Nothing is invented client-side. */
export default function ActivityLog({ entries }: { entries?: ActivityEntry[] }) {
  const items = (entries || []).slice(0, 12);
  if (!items.length) {
    return <div className="activity-empty">Waiting for the pipeline to start…</div>;
  }
  return (
    <ul className="activity-list">
      {items.map((e, i) => {
        const failed = /^failed/i.test(e.text);
        return (
          <li key={`${e.ts}-${i}`} className={`activity-item${i === 0 ? ' activity-item--new' : ''}`}>
            <span className={`activity-icon ${failed ? 'activity-icon--err' : ''}`}>{failed ? '✖' : '✓'}</span>
            <span className="activity-text">{e.text}</span>
          </li>
        );
      })}
    </ul>
  );
}
