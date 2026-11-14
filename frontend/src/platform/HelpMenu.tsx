/**
 * 'How it works' popover in the top bar: one explainer per product plus the reference data,
 * filtered to what the signed-in role can use.
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { HelpCircle, ChevronDown, Boxes, FileSearch, Layers } from 'lucide-react';
import { PRODUCTS } from './products';
import { Gate, useAuth } from './auth/AuthContext';
import type { Permission } from './auth/authApi';

// "How it works" — one always-reachable explainer for the whole platform,
// so a first-time visitor immediately understands the two products and
// where the standards reference data comes from.

interface GuideSection {
  id: string;
  title: string;
  icon: React.ReactNode;
  className: string;
  body: React.ReactNode;
  /** v0.5.0: section only shown to roles with this permission. */
  permission?: Permission;
}

const SECTIONS: GuideSection[] = [
  {
    id: 'bulk',
    permission: 'bulk.use',
    title: PRODUCTS.bulk.name,
    icon: <Boxes size={14} />,
    className: 'p-bulk',
    body: (
      <>
        <p>
          <b>Bulk / portfolio workflow.</b> Upload an ETSI bulk IPR-declaration export (.xlsx, .xls or .csv)
          and every row is scored for essentiality in the background. Explore the result on the Portfolio
          Dashboard, browse the Patent Registry, and drill into any patent or company.
        </p>
        <p>
          Every source row is kept — rows that repeat an application or publication number are never merged
          or dropped. The enriched Excel export returns your original sheet, in its original order, with the
          CPARAS_Bulk_* columns appended to each row.
        </p>
        <p>Runs are processed in batches and can be paused, stopped and continued from <Link className="link" to="/bulk/runs">Uploads &amp; Runs</Link>.</p>
      </>
    ),
  },
  {
    id: 'isld',
    title: PRODUCTS.isld.name,
    icon: <FileSearch size={14} />,
    className: 'p-isld',
    body: (
      <>
        <p>
          <b>Single declaration deep analysis.</b> Upload one or more individual IPR declarations — PDF
          (with OCR), CSV, Excel or HTML — and a local LLM extracts every disclosure, maps it to 3GPP
          standards, grades SEP strength and produces research-style evidence and reports.
        </p>
        <p>
          <b>Text Matching</b> matches pasted claim text, or an uploaded ISLD document, clause-by-clause
          against the indexed standards corpus. Everything runs locally — nothing leaves your machine.
        </p>
      </>
    ),
  },
  {
    id: 'data',
    title: 'Standards reference data',
    icon: <Layers size={14} />,
    className: 'p-platform',
    body: (
      <>
        <p>
          Standards mapping and Text Matching compare patents against indexed specification text. The
          platform ships with a curated reference set so it works offline immediately; a full ETSI dataset
          can be imported on the backend (<code>python -m etsi_corpus.cli import</code>).
        </p>
        <p>
          The <Gate permission="reference.view" fallback={<>Standards Corpus page</>}><Link className="link" to="/corpus">Standards Corpus</Link> page</Gate> can
          additionally crawl and index 3GPP series, the <Gate permission="reference.view" fallback={<>3GPP Retrieval Index</>}><Link className="link" to="/retrieval-index">3GPP
          Retrieval Index</Link></Gate> is the installed full-text index of 3GPP specifications.
        </p>
      </>
    ),
  },
];

/** Top-bar help popover. */
export default function HelpMenu() {
  const [open, setOpen] = useState(false);
  const { can } = useAuth();
  const sections = SECTIONS.filter(s => !s.permission || can(s.permission));
  const [activeId, setActiveId] = useState(sections[0]?.id ?? SECTIONS[0].id);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('mousedown', onClick); document.removeEventListener('keydown', onKey); };
  }, []);

  const active = sections.find(s => s.id === activeId) ?? sections[0];

  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <button className="btn btn-sm btn-ghost" onClick={() => setOpen(o => !o)} aria-expanded={open} title="How this platform works">
        <HelpCircle size={15} />
        <span className="hide-sm">How it works</span>
        <ChevronDown size={13} style={{ transform: open ? 'rotate(180deg)' : undefined, transition: 'transform .15s' }} />
      </button>
      {open && (
        <div className="popover help-popover" onClick={e => { if ((e.target as HTMLElement).closest('a')) setOpen(false); }}>
          <div className="help-tabs">
            {sections.map(s => (
              <button key={s.id} className={`help-tab ${s.className}${s.id === activeId ? ' active' : ''}`} onClick={() => setActiveId(s.id)}>
                <span className="help-tab-icon">{s.icon}</span>{s.title}
              </button>
            ))}
          </div>
          <div className={`help-content ${active.className}`}>
            <div className="eyebrow">{active.title}</div>
            {active.body}
          </div>
        </div>
      )}
    </div>
  );
}
