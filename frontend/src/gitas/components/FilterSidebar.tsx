/** CPARAS Bulk dashboard filter panel (see the component comment below). */

import { useState } from 'react';
import { SlidersHorizontal, X } from 'lucide-react';
import type { Filters } from '../api';

/** Option lists returned by GET /api/sep/filters. */
export interface FilterOptions {
  technology: string[];
  release: string[];
  company: string[];
  country: string[];
}

type ListKey = 'technology' | 'release' | 'company' | 'country' | 'essentiality';

const RELEASE_ORDER = ['Rel-13', 'Rel-14', 'Rel-15', 'Rel-16', 'Rel-17', 'Rel-18'];

/** One collapsible checklist of filter values. */
function Group({ title, values, selected, onToggle, ordered }: {
  title: string; values: string[]; selected: string[]; onToggle: (v: string) => void; ordered?: string[];
}) {
  const [query, setQuery] = useState('');
  const list = ordered
    ? [...ordered.filter(v => values.includes(v)), ...values.filter(v => !ordered.includes(v))]
    : values;
  const filtered = query ? list.filter(v => v.toLowerCase().includes(query.toLowerCase())) : list;
  const needsSearch = values.length > 6;
  return (
    <details className="filter-group" open>
      <summary>
        <span>{title}</span>
        {selected.length > 0 && <span className="count">{selected.length}</span>}
      </summary>
      <div className="filter-options">
        {needsSearch && (
          <input className="input" placeholder={`Search ${title.toLowerCase()}…`} value={query} onChange={e => setQuery(e.target.value)} aria-label={`Search ${title}`} />
        )}
        {filtered.length === 0 && <span className="muted small">No values</span>}
        {filtered.map(v => (
          <label key={v} className="check" title={v}>
            <input type="checkbox" checked={selected.includes(v)} onChange={() => onToggle(v)} />
            <span>{v}</span>
          </label>
        ))}
      </div>
    </details>
  );
}

/** Portfolio dashboard filter panel (CPARAS Bulk). Changes are staged in
 * `draft` and only take effect on Apply, so several filters can be set
 * before the dashboard reloads. */
export default function FilterSidebar({
  options, draft, onChange, onApply, onReset, onClose, dirty,
}: {
  options: FilterOptions;
  draft: Filters;
  onChange: (f: Filters) => void;
  onApply: () => void;
  onReset: () => void;
  onClose?: () => void;
  dirty?: boolean;
}) {
  const toggle = (key: ListKey, value: string) => {
    const cur = (draft[key] as string[] | undefined) ?? [];
    const next = cur.includes(value) ? cur.filter(v => v !== value) : [...cur, value];
    onChange({ ...draft, [key]: next });
  };

  return (
    <aside className="card filter-panel" aria-label="Dashboard filters">
      <div className="card-header">
        <div className="card-title"><SlidersHorizontal size={14} /> Filters</div>
        {onClose && (
          <button className="btn btn-sm btn-ghost btn-icon" onClick={onClose} title="Hide filters" aria-label="Hide filters"><X size={14} /></button>
        )}
      </div>
      <div className="card-body">
        <Group title="Technology" values={options.technology} selected={draft.technology ?? []} onToggle={v => toggle('technology', v)} />
        <Group title="Release" values={options.release} selected={draft.release ?? []} onToggle={v => toggle('release', v)} ordered={RELEASE_ORDER} />
        <Group title="Declaring company" values={options.company} selected={draft.company ?? []} onToggle={v => toggle('company', v)} />
        <Group title="Country" values={options.country} selected={draft.country ?? []} onToggle={v => toggle('country', v)} />
        <Group title="Essentiality" values={['Essential', 'Potential', 'Non-Essential']} selected={draft.essentiality ?? []} onToggle={v => toggle('essentiality', v)} />
      </div>
      <div className="card-footer">
        <button className="btn btn-sm btn-primary" style={{ flex: 1 }} onClick={onApply} disabled={dirty === false}>Apply</button>
        <button className="btn btn-sm" onClick={onReset}>Reset</button>
      </div>
    </aside>
  );
}
