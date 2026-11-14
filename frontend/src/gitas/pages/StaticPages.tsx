import { Link } from 'react-router-dom';
import {
  MessagesSquare, BarChart3, ExternalLink, Database, Layers, ScanText,
  Boxes, FileSearch, FileSpreadsheet, FileDown,
} from 'lucide-react';
import { PRODUCTS } from '../../platform/products';
import { Card, EmptyState, PageHeader } from '../../platform/ui';
import { Gate, useAuth } from '../../platform/auth/AuthContext';
import type { Permission } from '../../platform/auth/authApi';

/*
 * Help & information pages (About, Resources, FAQs, Facilitation Center,
 * Reports, Assistant). Static content, open to every signed-in role; since
 * v0.5.0 anything that points into CPARAS Bulk or the reference-data pages is
 * shown only to roles that can open it (<Gate>), so a Guest sees ISLD only.
 */

function Page({ eyebrow, title, lead, children, narrow = true }: { eyebrow?: string; title: string; lead: string; children: React.ReactNode; narrow?: boolean }) {
  return (
    <div className={narrow ? 'page page--narrow' : 'page'}>
      <PageHeader eyebrow={eyebrow ?? 'Help & information'} title={title} description={lead} />
      <div className="stack">{children}</div>
    </div>
  );
}

/** About CPARAS (Bulk content only for roles with Bulk access). */
export function AboutPage() {
  return (
    <Page title="About CPARAS" lead="The C-DOT SEP Intelligence Platform — portfolio-scale and declaration-level analysis of declared Standard Essential Patents, fully offline.">
      <Card>
        <div className="prose">
          <h2>What the platform does</h2>
          <p>
            CPARAS gives licensing teams, examiners and researchers an evidence-based view of declared Standard
            Essential Patents (SEPs): who declared what, against which 3GPP specification and clause, and how
            strong the essentiality evidence is. All analysis — embeddings, scoring and the local LLM — runs on
            your own infrastructure; no data leaves the machine.
          </p>
        </div>
      </Card>
      <div className="grid-2">
        <Gate permission="bulk.use">
        <Card title={PRODUCTS.bulk.name} icon={<Boxes size={15} />} className={PRODUCTS.bulk.className}>
          <div className="prose">
            <p><b>Bulk / portfolio workflow.</b> Upload a multi-company ETSI bulk declaration export and explore it at
              scale — company rankings, release and domain coverage, the full patent registry, and an enriched Excel
              export that returns every source row with the analysis appended.</p>
            <p style={{ marginTop: 10 }}><Link className="link" to="/bulk">Open CPARAS Bulk →</Link></p>
          </div>
        </Card>
        </Gate>
        <Card title={PRODUCTS.isld.name} icon={<FileSearch size={15} />} className={PRODUCTS.isld.className}>
          <div className="prose">
            <p><b>Single declaration deep analysis.</b> Upload individual IPR declarations (PDF, CSV, Excel or HTML) for
              LLM-graded claim-to-standard mapping, verification, research-style evidence and reports. Its Text
              Matching tool matches claim text clause-by-clause against the indexed specification corpus.</p>
            <p style={{ marginTop: 10 }}><Link className="link" to="/isld">Open CPARAS ISLD →</Link></p>
          </div>
        </Card>
      </div>
      <Card title="Standards text">
        <div className="prose">
          <p>
            Standards mapping and Text Matching compare patents with real specification text: the installed
            <b> 3GPP Retrieval Index</b> (a read-only full-text index of 3GPP specifications), or the
            <b> Standards Corpus</b> built by importing an ETSI dataset or crawling 3GPP series from the <Gate permission="reference.view" fallback={<>Standards Corpus</>}><Link className="link" to="/corpus">Standards
            Corpus</Link></Gate> page. Everything stays on this machine.
          </p>
        </div>
      </Card>
      <Card title="Data & disclaimers">
        <div className="prose">
          <p>All essentiality determinations are produced by NLP/ML analysis over a reference corpus and are not a legal
            opinion. Verify against live ETSI/3GPP specification text and the original IPR declaration before relying on
            them for FRAND, licensing or litigation purposes.</p>
        </div>
      </Card>
    </Page>
  );
}

interface ResourceLink {
  title: string;
  body: string;
  href?: string;
  internal?: string;
  icon: React.ReactNode;
  className?: string;
}

/** Card linking to an external resource or an internal page. */
function LinkCard({ r }: { r: ResourceLink }) {
  const inner = (
    <div className={`link-card ${r.className ?? ''}`}>
      <div className="link-card-title">{r.icon}<span>{r.title}</span>{r.href && <ExternalLink size={12} style={{ marginLeft: 'auto', color: 'var(--c-muted-2)' }} />}</div>
      <div className="link-card-body">{r.body}</div>
    </div>
  );
  if (r.href) return <a href={r.href} target="_blank" rel="noreferrer noopener" style={{ display: 'block' }}>{inner}</a>;
  return <Link to={r.internal!} style={{ display: 'block' }}>{inner}</Link>;
}

/** External references plus shortcuts into the parts of CPARAS this role can open. */
export function ResourcesPage() {
  const external: ResourceLink[] = [
    { title: 'ETSI IPR Online Database', body: 'Search declared essential patents and licensing declarations directly with ETSI — the authoritative source.', href: 'https://ipr.etsi.org/', icon: <Database size={15} /> },
    { title: '3GPP Specification Series', body: 'Browse the source technical specifications (TS 38.xxx and related series) referenced across the platform.', href: 'https://www.3gpp.org/specifications-technologies/specifications-by-series', icon: <Layers size={15} /> },
    { title: 'TSDSI IPR Portal', body: 'Indian standards-body declarations relevant to telecom SEPs, from TSDSI.', href: 'https://www.tsdsi.in/', icon: <Database size={15} /> },
    { title: 'WIPO — Standards and Patents', body: 'Background on FRAND licensing principles for standard-essential patent portfolios.', href: 'https://www.wipo.int/patents/en/topics/standards.html', icon: <ExternalLink size={15} /> },
  ];
  const { can } = useAuth();
  type Gated = ResourceLink & { permission?: Permission };
  const all: Gated[] = [
    { title: 'CPARAS Bulk — Uploads & Runs', body: 'Upload a bulk ETSI export; every source row is kept and scored.', internal: '/bulk/runs', icon: <FileSpreadsheet size={15} />, className: 'p-bulk', permission: 'bulk.use' },
    { title: 'CPARAS ISLD — Declaration Analysis', body: 'Deep, LLM-based analysis of individual declarations.', internal: '/isld', icon: <FileSearch size={15} />, className: 'p-isld' },
    { title: 'CPARAS ISLD — Text Matching', body: 'Match claim or disclosure text against the specification corpus clause-by-clause.', internal: '/isld/text-matching', icon: <ScanText size={15} />, className: 'p-isld' },
    { title: 'Standards Corpus', body: 'Index 3GPP specifications into the Standards Corpus for Text Matching and standards mapping.', internal: '/corpus', icon: <Layers size={15} />, permission: 'reference.view' },
  ];
  const internal = all.filter(r => !r.permission || can(r.permission));
  return (
    <Page title="Resources" lead="Reference material for SEP declaration research and FRAND licensing, plus shortcuts into the platform.">
      <Card title="Standards bodies & IPR databases">
        <div className="grid-2">{external.map(r => <LinkCard r={r} key={r.title} />)}</div>
      </Card>
      <Card title="Inside this platform">
        <div className="grid-2">{internal.map(r => <LinkCard r={r} key={r.title} />)}</div>
      </Card>
    </Page>
  );
}

/** `bulk: true` marks questions only relevant to roles with CPARAS Bulk access. */
const FAQS: { q: string; a: string; bulk?: boolean }[] = [
  { q: 'What is the difference between CPARAS Bulk and CPARAS ISLD?', a: 'CPARAS Bulk is the bulk / portfolio workflow: upload a multi-company ETSI export and every row is scored and explorable on dashboards. CPARAS ISLD analyses individual declarations in depth with a local LLM — standards mapping, SEP strength, verification and research-style evidence — and includes clause-level Text Matching.' },
  { q: 'Does CPARAS Bulk remove duplicate rows?', a: 'No. Every non-empty row of the uploaded sheet becomes its own record, in the original order — including rows that repeat an application number, publication number, title or company, and even completely identical rows. Re-uploading a file creates a new, complete run.', bulk: true },
  { q: 'What does the enriched Excel export contain?', a: 'Your original sheet — same rows, same order, same columns and values — with the CPARAS_Bulk_* analysis columns appended to each row, plus a “CPARAS Bulk Summary” sheet of run-level figures.', bulk: true },
  { q: 'What does an "Essential" vs "Potential" vs "Non-Essential" tag mean?', a: 'These reflect the platform’s confidence, from the score breakdown (claim match, semantic similarity, standard coverage), that a patent reads on a mandatory part of the referenced 3GPP specification clause. They are analytical signals, not legal determinations.' },
  { q: 'How is the essentiality score computed?', a: 'An embedding-based similarity match against a 3GPP standards reference corpus, blended with claim-match and coverage signals when full claim text is not available in the bulk export. CPARAS ISLD uses the same research engine for single declarations.' },
  { q: 'Can I export a single patent’s analysis?', a: 'Yes — open the patent’s detail page (from the registry or a company profile) and use the Export panel.', bulk: true },
];

/** FAQ list (Bulk-only questions hidden from Guests). */
export function FaqsPage() {
  const { can } = useAuth();
  const faqs = FAQS.filter(f => !f.bulk || can('bulk.use'));
  return (
    <Page title="Frequently asked questions" lead="Common questions about the two products, row preservation, scoring and exports.">
      <Card flush>
        {faqs.map(f => (
          <details className="faq-item" key={f.q}>
            <summary>{f.q}</summary>
            <p>{f.a}</p>
          </details>
        ))}
      </Card>
    </Page>
  );
}

/** Support topics and contact route. */
export function FacilitationCenterPage() {
  return (
    <Page title="Facilitation Center" lead="Support for licensors, licensees and researchers working with declared SEP data.">
      <Card title="Get help with">
        <ul className="prose">
          <li>Interpreting an essentiality score or claim-to-standard mapping</li>
          <li>Preparing a bulk declaration export for CPARAS Bulk</li>
          <li>Understanding FRAND licensing terminology used across the platform</li>
          <li>Requesting a deeper single-declaration analysis in CPARAS ISLD</li>
        </ul>
      </Card>
      <Card title="Contact">
        <p className="prose">Reach the Centre for Development of Telematics SEP Repository team through your existing C-DOT liaison channel — this build does not expose a direct contact form.</p>
      </Card>
    </Page>
  );
}

/** Where each product's exports are found. */
export function ReportsPage() {
  return (
    <Page title="Reports" lead="Where to find every export the platform produces.">
      <div className="grid-2">
        <Gate permission="bulk.use">
        <Card title="CPARAS Bulk" icon={<Boxes size={15} />} className="p-bulk">
          <ul className="prose">
            <li><b>Enriched Excel</b> — per run, from the dashboard or Uploads &amp; Runs.</li>
            <li><b>Portfolio PDF report</b> — dashboard, respects the active filters.</li>
            <li><b>Failed-row report (CSV)</b> — Uploads &amp; Runs.</li>
            <li><b>Single record CSV</b> — patent detail page.</li>
          </ul>
          <div className="row" style={{ marginTop: 12 }}>
            <Link className="btn btn-sm" to="/bulk"><BarChart3 size={13} /> Dashboard</Link>
            <Link className="btn btn-sm" to="/bulk/runs"><FileSpreadsheet size={13} /> Uploads &amp; Runs</Link>
          </div>
        </Card>
        </Gate>
        <Card title="CPARAS ISLD" icon={<FileSearch size={15} />} className="p-isld">
          <ul className="prose">
            <li><b>Analysis report</b> — PDF or HTML, after an analysis run.</li>
            <li><b>Structured data</b> — JSON and CSV exports of every disclosure.</li>
            <li><b>Text Matching report</b> — PDF, from the Text Matching page.</li>
          </ul>
          <div className="row" style={{ marginTop: 12 }}>
            <Link className="btn btn-sm" to="/isld"><FileDown size={13} /> Declaration Analysis</Link>
          </div>
        </Card>
      </div>
    </Page>
  );
}

/** Placeholder for the not-yet-available assistant. */
export function ChatbotPage() {
  const { can } = useAuth();
  return (
    <Page title="Assistant" lead="Conversational access to the SEP repository.">
      <Card>
        <EmptyState icon={<MessagesSquare size={22} />} title="Not available in this build"
          body={can('bulk.use')
            ? 'The conversational assistant for querying declarations and essentiality scores isn’t wired up yet. Use the CPARAS Bulk registry and dashboard filters to query the data directly.'
            : 'The conversational assistant for querying declarations and essentiality scores isn’t wired up yet. Use CPARAS ISLD Declaration Analysis to analyse a declaration.'}
          action={can('bulk.use')
            ? <Link className="btn" to="/bulk/registry">Open the patent registry</Link>
            : <Link className="btn" to="/isld">Open Declaration Analysis</Link>} />
      </Card>
    </Page>
  );
}
