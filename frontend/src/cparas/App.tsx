/**
 * CPARAS ISLD — single declaration deep analysis (+ Text Matching).
 * Part of the CPARAS — C-DOT SEP Intelligence Platform. All AI calls go to
 * the local FastAPI backend. No cloud dependencies. Zero telemetry.
 *
 * Mounted at /isld/* (see src/App.tsx): /isld is Declaration Analysis and
 * /isld/text-matching is Text Matching. Both are rendered by this one
 * component so an analysis in progress (and its results) survives switching
 * between the two pages.
 */

import { Fragment, useState, useCallback, useRef, useEffect } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { UploadCloud, FileText, FileSpreadsheet, X, Download, Info, AlertTriangle, SearchCode, BookOpen, History, RefreshCw, Cpu, Database, Settings2, Play, ScanText, Boxes, Pause, Square } from 'lucide-react';
import type {
  AnalysisResponse, AnalysisMode, FileItem, BackendStatus, Declaration, RecommendedModelResponse
} from './types';
import { OCR_EXTRACTION_METHODS } from './types';
import { analyzeDeclarations, checkHealth, getProgress, newJobId, getRecommendedModel, pullModel, getPullStatus, getTextMatchStatus, startTextMatchJob, startFileMatchJob, getTextMatchJob, controlTextMatchJob, pauseAnalysis, stopAnalysis } from './services/api';
import type { AnalysisProgress, TextMatchResponse, TextMatchStatus, TextMatchJob } from './services/api';
import { generateReport, exportJSON, exportCSV } from './utils/reportGenerator';
import { generateCparasPdfReport, generateTextMatchPdfReport } from './utils/pdfReportGenerator';
import ResearchSection from './components/research/ResearchSection';
import ProgressTracker from './components/analysis/ProgressTracker';
import CompletionView from './components/analysis/CompletionView';
import ModelBadge from './components/analysis/ModelBadge';
import { stagesForMode } from './lib/pipelineStages';
import LlmStatusPanel from '../shared/LlmStatusPanel';
import { getLlmStatus, apiErrorMessage } from '../shared/llmHealth';
import type { LlmHealth } from '../shared/llmHealth';
import { useTheme } from '../platform/theme';
import { PRODUCTS } from '../platform/products';
import { Banner, Card, EmptyState, PageHeader, Pill } from '../platform/ui';
import { Gate } from '../platform/auth/AuthContext';

// ── App identity ─────────────────────────────────────────────────────────────
export const APP_NAME = 'CPARAS ISLD';
/** Product subtitle shown in the ISLD header and reports. */
export const APP_SUBTITLE = 'Single declaration deep analysis';
/** Full product name for report headers. */
export const APP_FULL_NAME = 'CPARAS ISLD — C-DOT SEP Intelligence Platform';

// ── Version history (Requirement 3) ────────────────────────────────────────────
interface VersionEntry { version: string; date: string; changes: string[] }
const VERSION_HISTORY: VersionEntry[] = [
  {
    version: 'v0.5.0',
    date: '2026-10-17',
    changes: [
      'Sign-in with three roles: Guest (CPARAS ISLD only), Data Entry (ISLD + Bulk) and Super Admin (everything + user management)',
      'Every API call is checked on the server against the signed-in role; sessions expire after inactivity and on sign-out',
      'Super Admins manage accounts, roles and passwords on the new User Management page',
      '"Standards text" panel now shows the 3GPP Retrieval Index (documents) next to the Standards Corpus (chunks)',
      'Exact phrases matched and Bulk essentiality confidence (%) from v0.4.9 included',
    ],
  },
  {
    version: 'v0.4.4',
    date: '2026-10-09',
    changes: [
      'Bulk uploads: a header below a title block is now found, and semicolon/tab/pipe CSVs and cp1252/UTF-16 files are accepted instead of rejected as an unrecognized format',
      '"Essential" now requires the declared standard family to be confirmed \u2014 a strong match to a different family reads as "Potential", so the essentiality column and the evidence level agree',
      'Corpus coverage reports whether the reference corpus actually contains the declared standard (new DECLARED_FAMILY_NOT_IN_CORPUS reason code)',
      'Country values like "UNITED STATES" and "EUROPEAN PATENT OFFICE" resolve to real country codes in the dashboard instead of "UNI"/"EUR"',
      'Oversized uploads are rejected with a clear message instead of exhausting backend memory',
    ],
  },
  {
    version: 'v0.4.2',
    date: '2026-10-07',
    changes: [
      'GPU used again: the context window is sized per model so it fits in VRAM, and the GPU panel shows Ollama\'s real GPU/CPU split',
      'phi3.5 no longer fails with HTTP 500 (its context no longer exceeds the card\'s memory)',
      'Same file gives the same result on every run (fixed seed, stable model loading); the Test button loads the model exactly as an analysis does',
      'GPU detection finds nvidia-smi outside PATH and external GPUs; embeddings use the GPU (EMBEDDING_DEVICE=auto)',
      'Local 3GPP archive folders (3gpp_*.json, .docx, .zip) can be indexed with index_3gpp.cmd',
    ],
  },
  {
    version: 'v0.4',
    date: '2026-09-26',
    changes: [
      'Text Matching matches each declared record separately, handles standard-only rows and several standards in one cell',
      'Text Matching can be paused, resumed or stopped, with a live progress bar; Stop keeps the results so far',
      'Analysis setup panel scrolls, so every option and the Analyze button stay reachable',
      'Page no longer runs past the right edge during an analysis; sidebar can be collapsed to icons',
      'Windows start script works from folders with spaces or brackets in the path',
      'Local LLM status now shows server, installed models, selected model and generation separately — a running Ollama is never reported as offline',
      'Only installed text models are offered; vision models (e.g. moondream) are kept for OCR only, and a missing model is reported instead of replaced',
      'Publication numbers with kind codes (e.g. B2, A1) and the declared standard are now kept from CSV/Excel inputs',
      'Windows start/setup scripts that reuse a running Ollama and wait for real readiness',
      'Renamed to CPARAS ISLD inside the unified CPARAS platform (alongside CPARAS Bulk)',
      'Redesigned interface: guided analysis setup, cleaner results and tables',
      'Extracted disclosure rows are no longer de-duplicated — every declared row is kept',
    ],
  },
  {
    version: 'v0.2',
    date: '2026-08-01',
    changes: [
      'Added CSV/spreadsheet ingestion as an alternative to PDF upload',
      'Improved extraction accuracy across the analysis pipeline',
    ],
  },
  {
    version: 'v0.1',
    date: '2026-07-15',
    changes: [
      'Rebranded with the C-DOT logo',
      'Added version history panel',
      'Added Application No. / Patent (Publication) No. columns to disclosure tables',
      'Added roadmap placeholder for upcoming text-matching module',
      'Renamed the downloaded report to "SEP Analysis Report"',
    ],
  },
];
const CURRENT_VERSION = VERSION_HISTORY[0].version;

// ── Text matching roadmap (Requirement 5 — steps for a future capability) ─────
const TEXT_MATCHING_STEPS: { title: string; desc: string }[] = [
  { title: 'Ingest source text', desc: 'Extract patent claim / disclosure text and target standard-specification text.' },
  { title: 'Normalize & segment', desc: 'Clean, tokenize, and split both texts into comparable clause-level segments.' },
  { title: 'Candidate retrieval', desc: 'Shortlist likely-matching clauses using embedding / keyword similarity search.' },
  { title: 'Fine-grained comparison', desc: 'Score each candidate pair for semantic and lexical overlap.' },
  { title: 'Ranking & thresholding', desc: 'Rank matches by confidence and filter out low-quality pairs.' },
  { title: 'Review & display', desc: 'Present ranked matches with highlighted overlaps for analyst review.' },
];

// ── Helpers ────────────────────────────────────────────────────────────────────
const fmt = (b: number) => b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(1) + ' KB' : (b / 1048576).toFixed(1) + ' MB';
const topN = (obj: Record<string, number>, n: number) => Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, n);
const COUNTRY_MAP: [string, string][] = [
  ['PATENT COOPERATION', 'PCT'], ['UNITED STATES', 'US'], ['UNITED KINGDOM', 'GB'],
  ['FINLAND', 'FI'], ['GERMANY', 'DE'], ['INDIA', 'IN'], ['JAPAN', 'JP'], ['CHINA', 'CN'], ['KOREA', 'KR'],
];
const shortC = (c: string) => {
  const u = c.toUpperCase();
  for (const [k, v] of COUNTRY_MAP) if (u.includes(k)) return v;
  return c.slice(0, 8);
};
const BAR_COLORS = ['var(--c-isld)', 'var(--c-accent)', 'var(--c-ok)', 'var(--c-warn)', '#0e8fa3', 'var(--c-err)'];

// ── Small Components ───────────────────────────────────────────────────────────

const STATUS_CLASS: Record<string, string> = {
  'Confirmed SEP': 's-hi', 'Likely SEP': 's-ac', 'Potential SEP': 's-md', 'Insufficient Evidence': 's-mu',
};

/** Confidence 0-1 as a coloured percentage pill. */
function ConfPill({ score }: { score?: number | null }) {
  if (score == null) return null;
  const cls = score >= 0.8 ? 'c-hi' : score >= 0.6 ? 'c-md' : 'c-lo';
  return <span className={`cpill ${cls}`}>{Math.round(score * 100)}%</span>;
}

/** Confidence 0-1 as a small bar with a percentage. */
function ConfBar({ score }: { score?: number | null }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const color = score >= 0.8 ? 'var(--green)' : score >= 0.6 ? 'var(--yellow)' : 'var(--red)';
  const label = score >= 0.95 ? 'Explicit' : score >= 0.8 ? 'Strong' : score >= 0.6 ? 'Probable' : 'Speculative';
  return (
    <div className="conf-bar-wrap">
      <div className="conf-bar-track">
        <div className="conf-bar-fill" style={{ width: pct + '%', background: color }} />
      </div>
      <ConfPill score={score} />
      <span style={{ fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--muted)' }}>{label}</span>
    </div>
  );
}

/** Country of a disclosure (from the explicit field, else the application-number prefix). */
function CountryBadge({ country, appNumber }: { country?: string; appNumber?: string }) {
  const c = (country || '').toUpperCase(), a = (appNumber || '').toUpperCase();
  if (a.startsWith('PCT/') || c.includes('PCT')) return <span className="cbadge b-pct">PCT</span>;
  if (c.includes('INDIA') || c === 'IN') return <span className="cbadge b-in">IN</span>;
  if (c.includes('UNITED STATES') || c === 'US') return <span className="cbadge b-us">US</span>;
  if (c.includes('UNITED KINGDOM') || c === 'GB') return <span className="cbadge b-gb">GB</span>;
  if (c.includes('GERMANY') || c === 'DE') return <span className="cbadge b-other">DE</span>;
  if (c.includes('JAPAN') || c === 'JP') return <span className="cbadge b-us">JP</span>;
  if (c.includes('CHINA') || c === 'CN') return <span className="cbadge b-in">CN</span>;
  if (c.includes('KOREA') || c === 'KR') return <span className="cbadge b-other">KR</span>;
  return <span className="cbadge b-other">{(country || '?').slice(0, 4).toUpperCase()}</span>;
}

/** LLM-graded SEP strength (high / medium / low) as a coloured label. */
function SepStrength({ strength }: { strength?: string }) {
  if (!strength) return null;
  const s = strength.toLowerCase();
  const cls = s.includes('high') ? 's-hi' : s.includes('low') ? 's-lo' : 's-md';
  return <span className={`sep-s ${cls}`}>{strength}</span>;
}

/** Minimal horizontal bar chart for the portfolio breakdowns. */
function BarChart({ items, max, colorOffset = 0 }: { items: [string, number][]; max: number; colorOffset?: number }) {
  return (
    <div className="bar-chart">
      {items.map(([label, val], i) => (
        <div key={label} className="bar-row">
          <div className="bar-label" title={label}>{label}</div>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${Math.round((val / max) * 100)}%`, background: BAR_COLORS[(i + colorOffset) % BAR_COLORS.length] }}>
              {val}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Header dropdown listing VERSION_HISTORY ('what's new'). */
function VersionDropdown() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);

  return (
    <div className="version-dd" ref={ref}>
      <button className="btn btn-sm" onClick={() => setOpen(o => !o)} aria-expanded={open} title="Version history">
        <History size={13} /> {CURRENT_VERSION}
      </button>
      {open && (
        <div className="version-dd-panel">
          <div className="version-dd-title">Version History</div>
          {VERSION_HISTORY.map(v => (
            <div key={v.version} className="version-dd-entry">
              <div className="version-dd-entry-head">
                <span className="version-dd-tag">{v.version}</span>
                <span className="version-dd-date">{v.date}</span>
              </div>
              <ul className="version-dd-list">
                {v.changes.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** ISLD help dialog: how to run an analysis, quick vs deep mode, reading the results. */
function HelpModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true" aria-label="CPARAS ISLD help">
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <h2>CPARAS ISLD — help &amp; user guide</h2>
          <button className="btn btn-sm btn-ghost btn-icon" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        <div className="modal-body">

          <div className="help-section">
            <div className="help-title">Overview</div>
            <p className="help-text">
              CPARAS ISLD analyzes individual ETSI IPR declarations to identify and classify Standard Essential Patents
              (SEPs). Each disclosed patent is extracted, compared against a corpus of 3GPP specification
              clauses and against other patents already in the database, and the results are compiled into
              the report below. The platform runs entirely offline; no data leaves the local environment at
              any stage of processing.
            </p>
          </div>

          <div className="help-section">
            <div className="help-title">1. Uploading declarations</div>
            <p className="help-text">
              Declarations can be uploaded as PDF, CSV/TSV, Excel (XLSX/XLS) or HTML. All formats are
              supported by the same downstream pipeline — enrichment, verification, and research
              augmentation — and differ only in how patent data is initially read from the file. Every
              declared row is kept: rows that repeat an application or publication number are not merged
              or dropped.
            </p>
            <table className="help-table">
              <thead>
                <tr><th>Format</th><th>Extraction method</th><th>Notes</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td><b>PDF</b></td>
                  <td>Multi-tier OCR followed by extraction with a local LLM</td>
                  <td>Required when the source table structure is not machine-readable (scanned or flattened documents)</td>
                </tr>
                <tr>
                  <td><b>CSV / TSV</b></td>
                  <td>Direct column mapping, with header names matched automatically against common naming variants</td>
                  <td>Faster and deterministic; recommended when a spreadsheet export of the annex is available. Files that cannot be reliably mapped fall back to standard extraction automatically</td>
                </tr>
                <tr>
                  <td><b>Excel (XLSX/XLS)</b></td>
                  <td>Same direct column mapping as CSV, reading workbook cells straight from the first sheet</td>
                  <td>Use this instead of exporting to CSV first; behaves identically to a CSV upload. Only the first worksheet is read</td>
                </tr>
                <tr>
                  <td><b>HTML</b></td>
                  <td>Same direct column mapping as CSV, applied to the best-matching &lt;table&gt; on the page</td>
                  <td>Useful for "export to web page" annex exports or scraped tables. Pages with no usable table fall back to standard extraction over the visible text</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="help-section">
            <div className="help-title">2. Report structure</div>
            <ul className="help-list">
              <li><b>Stats strip</b> — headline counts (Total Patents, Declarations, Specifications, Jurisdictions) across the current analysis run.</li>
              <li><b>Top 3GPP Specs / Jurisdictions / Technical Domains</b> — distribution of the disclosed patents by standard, filing country, and inferred technology area.</li>
              <li><b>Per-declarant panel</b> — one panel per declaration, containing portfolio intelligence and the full disclosure table for that declarant.</li>
              <li><b>Patent disclosures table</b> — one row per patent, expandable to show the full standards/SEP analysis, geographic detail, similarity match, and version-evolution trail.</li>
              <li><b>Coverage warning</b> — a diagnostic, shown for PDF uploads only, comparing the number of patents extracted against the number of application/publication-like numbers detected in the source text. A significant gap is worth verifying manually against the source PDF.</li>
            </ul>
          </div>

          <div className="help-section">
            <div className="help-title">3. SEP classification</div>
            <p className="help-text">
              Each patent is scored against the 3GPP specification corpus using semantic similarity, blended
              with the extraction model's own confidence where available. The resulting score determines the
              classification tier:
            </p>
            <ul className="help-list">
              <li><span className="sep-s s-hi" style={{ marginRight: 6 }}>Confirmed SEP</span> — similarity ≥ 60%, and the patent's declared standard family matches the matched clause's standard.</li>
              <li><span className="sep-s s-ac" style={{ marginRight: 6 }}>Likely SEP</span> — similarity ≥ 50%, without requiring a declared-family match.</li>
              <li><span className="sep-s s-md" style={{ marginRight: 6 }}>Potential SEP</span> — similarity ≥ 35%. A plausible but weaker candidate; recommended for manual review.</li>
              <li><span className="sep-s s-mu" style={{ marginRight: 6 }}>Insufficient Evidence</span> — similarity below 35%. No confident match was found against the standards corpus.</li>
            </ul>
          </div>

          <div className="help-section">
            <div className="help-title">4. Interpreting similarity and confidence scores</div>
            <p className="help-text">
              The percentage shown alongside a patent indicates how closely its text matches the best
              corresponding specification clause or prior patent. It reflects textual similarity, not a legal
              determination of essentiality. As a general guide: <b>80% and above</b> is a strong match,
              <b> 60–79%</b> is probable, and <b>below 60%</b> is speculative and should be verified against
              the source declaration and cited specification before being relied upon.
            </p>
          </div>

          <div className="help-section">
            <div className="help-title">5. Limitations of the local model</div>
            <p className="help-text">
              Extraction and enrichment run on a compact, locally hosted LLM, which trades some accuracy for
              fully offline operation. Application numbers, publication numbers, and standard codes should be
              cross-checked against the source document before being relied upon; the extraction-verification
              flag on each field indicates whether that value was found verbatim, or near-verbatim, in the
              source text. Where a CSV version of the declaration is available, uploading it instead of the
              PDF avoids this limitation, since values are read directly from the file rather than inferred.
            </p>
          </div>

        </div>
      </div>
    </div>
  );
}

/** Backend + local-LLM readiness badge for the ISLD header. */
function BackendStatusBadge({ status, llm, selectedModel }: { status: BackendStatus; llm: LlmHealth | null; selectedModel?: string }) {
  if (status.status === 'checking') return <Pill tone="muted" dot>Connecting…</Pill>;
  if (status.status === 'error') return <Pill tone="err" dot title="The FastAPI backend (/api) is not reachable.">Backend offline</Pill>;
  if (!llm) return <Pill tone="muted" dot>Checking local LLM…</Pill>;
  const detail = [llm.error, llm.hint].filter(Boolean).join(' ');
  if (!llm.reachable) {
    return <Pill tone="warn" dot title={detail || 'The backend is running but cannot reach the local LLM server.'}>Local LLM unreachable</Pill>;
  }
  if (!llm.text_models.length) return <Pill tone="warn" dot title={detail}>No text model installed</Pill>;
  if (!selectedModel || !llm.text_models.includes(selectedModel)) {
    return <Pill tone="warn" dot title="Choose an installed model in the setup panel.">Select a model</Pill>;
  }
  const backend = (llm.backend || 'local').toUpperCase();
  return (
    <Pill tone="ok" dot title={`${backend} at ${llm.base_url ?? '?'} · ${selectedModel}`}>
      <span className="mono" style={{ fontSize: 11 }}>{backend} · {selectedModel.slice(0, 26)}</span>
    </Pill>
  );
}

/**
 * Expanded view of one disclosure: extracted fields, standards analysis, SEP assessment and the
 * research analysis (recommended standard / version / clause, exact matched phrases, explanation,
 * version evolution, verification, potential-SEP flag).
 */
function PatentDetailRow({ disc, researchRecord }: {
  disc: import('./types').PatentDisclosure;
  researchRecord?: import('./types').ResearchRecord | null;
}) {
  const sa = disc.standards_analysis ?? {};
  const sep = disc.sep_analysis ?? {};
  const geo = disc.geographic_analysis ?? {};
  const inf = disc.inferred_fields ?? {};
  const specs = sa.standard_descriptions ?? [];
  const domains = sa.technical_domains ?? [];
  const releases = sa['3gpp_release_mapping'] ?? [];
  const layers = sa.network_layer_mapping ?? [];

  // ── Research-augmented fields (Requirements 2–9) ─────────────────
  const rr = researchRecord;
  const STATUS_COLOR: Record<string, string> = {
    'Confirmed SEP': 'var(--green)', 'Likely SEP': 'var(--accent)',
    'Potential SEP': 'var(--yellow)', 'Insufficient Evidence': 'var(--muted)',
  };
  const VER_COLOR: Record<string, string> = {
    'Verified - forward alignment': 'var(--green)', 'Predicted': 'var(--accent)',
    'Declining relevance': 'var(--red)', 'Stable': 'var(--muted)',
  };

  return (
    <div className="detail-grid">
      {(disc.explicit_data?.application_number || disc.explicit_data?.publication_number) && (
        <div className="detail-card">
          <div className="dc-title">Filing Numbers</div>
          {disc.explicit_data?.application_number && <div className="di"><div className="di-label">Application No.</div><div className="di-val" style={{ fontFamily: 'var(--mono)' }}>{disc.explicit_data.application_number}</div></div>}
          {disc.explicit_data?.publication_number && <div className="di"><div className="di-label">Patent No. (Publication)</div><div className="di-val" style={{ fontFamily: 'var(--mono)' }}>{disc.explicit_data.publication_number}</div></div>}
        </div>
      )}
      {releases.length > 0 && (
        <div className="detail-card">
          <div className="dc-title">3GPP Release Mapping</div>
          {releases.map((r, i) => (
            <div key={i} className="di">
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
                <span className="rtag">{r.release}</span>
                <ConfPill score={r.confidence} />
              </div>
              {r.reasoning && <div className="di-val" style={{ fontSize: 10, color: 'var(--muted)' }}>{r.reasoning}</div>}
            </div>
          ))}
        </div>
      )}
      {domains.length > 0 && (
        <div className="detail-card">
          <div className="dc-title">Technical Domains</div>
          {domains.map((d, i) => (
            <div key={i} className="di">
              <span className="domain-chip">{d.domain}</span>
              <ConfBar score={d.confidence} />
            </div>
          ))}
        </div>
      )}
      {(sa.likely_protocol_components ?? []).length > 0 && (
        <div className="detail-card">
          <div className="dc-title">Protocol Components</div>
          <div className="tag-list">
            {sa.likely_protocol_components!.map((c, i) => <span key={i} className="tag t-a">{c}</span>)}
          </div>
        </div>
      )}
      {(sep.likely_sep_strength || sep.probable_implementation_scope || sep.estimated_licensing_relevance) && (
        <div className="detail-card">
          <div className="dc-title">SEP Analysis</div>
          {sep.likely_sep_strength && <div className="di"><div className="di-label">SEP Strength</div><SepStrength strength={sep.likely_sep_strength} /><ConfBar score={sep.confidence} /></div>}
          {sep.probable_implementation_scope && <div className="di"><div className="di-label">Implementation Scope</div><div className="di-val">{sep.probable_implementation_scope}</div></div>}
          {sep.estimated_licensing_relevance && <div className="di"><div className="di-label">Licensing Relevance</div><div className="di-val">{sep.estimated_licensing_relevance}</div></div>}
          {(sep.probable_device_impact ?? []).length > 0 && (
            <div className="di"><div className="di-label">Device Impact</div>
              <div className="tag-list">{sep.probable_device_impact!.map((x, i) => <span key={i} className="tag" style={{ background: 'var(--c-warn-soft)', color: 'var(--c-warn)' }}>{x}</span>)}</div>
            </div>
          )}
        </div>
      )}
      {(geo.jurisdiction_strength || geo.enforcement_significance) && (
        <div className="detail-card">
          <div className="dc-title">Geographic Analysis</div>
          {geo.jurisdiction_strength && <div className="di"><div className="di-label">Jurisdiction Strength</div><div className="di-val">{geo.jurisdiction_strength}</div></div>}
          {geo.enforcement_significance && <div className="di"><div className="di-label">Enforcement Significance</div><div className="di-val">{geo.enforcement_significance}</div></div>}
          {geo.regional_strategy_inference && <div className="di"><div className="di-label">Strategy Inference</div><div className="di-val">{geo.regional_strategy_inference}</div></div>}
        </div>
      )}
      {(inf.probable_title || inf.probable_technology_focus || inf.probable_claim_scope) && (
        <div className="detail-card detail-full">
          <div className="dc-title">Inferred Intelligence</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(200px,1fr))', gap: 10 }}>
            {inf.probable_title && <div className="di"><div className="di-label">Probable Title</div><div className="di-val">{inf.probable_title}</div></div>}
            {inf.probable_technology_focus && <div className="di"><div className="di-label">Technology Focus</div><div className="di-val">{inf.probable_technology_focus}</div></div>}
            {inf.probable_claim_scope && <div className="di"><div className="di-label">Probable Claim Scope</div><div className="di-val">{inf.probable_claim_scope}</div></div>}
            {(inf.probable_use_cases ?? []).length > 0 && (
              <div className="di"><div className="di-label">Use Cases</div>
                <div className="tag-list">{inf.probable_use_cases!.map((u, i) => <span key={i} className="tag t-g">{u}</span>)}</div>
              </div>
            )}
          </div>
        </div>
      )}
      {specs.length > 0 && (
        <div className="detail-card detail-full">
          <div className="dc-title">Specification Descriptions</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))', gap: 10 }}>
            {specs.map((s, i) => (
              <div key={i} className="di" style={{ background: 'var(--surface2)', borderRadius: 6, padding: '8px 10px' }}>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4 }}>
                  <span className="spec-chip">{s.specification}</span>
                  {s.technology_area && <span className="domain-chip">{s.technology_area}</span>}
                </div>
                {s.description && <div className="di-val" style={{ marginBottom: 3 }}>{s.description}</div>}
                {s.functional_role && <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)' }}>{s.functional_role}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Research / paper-style augmentation (Requirements 2-9) ── */}
      {rr && (
        <div className="detail-card detail-full rr-card">
          <div className="dc-title">Research Analysis</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))', gap: 10 }}>

            {/* Req 2: core prediction fields */}
            <div className="di">
              <div className="di-label">Predicted SEP Status</div>
              <span className={`sep-s ${STATUS_CLASS[rr.predicted_sep_status] ?? 's-mu'}`}>{rr.predicted_sep_status}</span>
            </div>

            {/* Req 3: version-aware recommendation */}
            <div className="di">
              <div className="di-label">Recommended Standard</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                <span className="spec-chip">{rr.recommended_standard}</span>
                <span className="rtag">{rr.recommended_version}</span>
              </div>
            </div>

            {/* Req 4: clause output */}
            <div className="di">
              <div className="di-label">Standard Clause</div>
              <div className="di-val" style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                {rr.recommended_standard}<br />
                <span style={{ color: 'var(--accent2)' }}>{rr.standard_clause}</span>
                {rr.standard_clause_title && (
                  <span style={{ color: 'var(--muted)', fontSize: 10 }}> — {rr.standard_clause_title}</span>
                )}
              </div>
            </div>

            <div className="di">
              <div className="di-label">Standard Similarity</div>
              <div className="conf-bar-wrap">
                <div className="conf-bar-track">
                  <div className="conf-bar-fill" style={{
                    width: `${Math.round(rr.similarity_score * 100)}%`,
                    background: rr.similarity_score >= 0.6 ? 'var(--green)' : rr.similarity_score >= 0.5 ? 'var(--accent)' : 'var(--yellow)',
                  }} />
                </div>
                <span className="cpill c-md">{Math.round(rr.similarity_score * 100)}%</span>
              </div>
            </div>

            <div className="di">
              <div className="di-label">Research Confidence</div>
              <div className="conf-bar-wrap">
                <div className="conf-bar-track">
                  <div className="conf-bar-fill" style={{
                    width: `${Math.round(rr.confidence_score * 100)}%`,
                    background: rr.confidence_score >= 0.7 ? 'var(--green)' : 'var(--yellow)',
                  }} />
                </div>
                <span className="cpill c-md">{Math.round(rr.confidence_score * 100)}%</span>
              </div>
            </div>

            {rr.similar_patent && (
              <div className="di">
                <div className="di-label">Similar Patent</div>
                <div className="di-val">
                  <span className="spec-chip">{rr.similar_patent}</span>
                  {rr.similar_patent_score != null && (
                    <span className="cpill c-md" style={{ marginLeft: 4 }}>{Math.round(rr.similar_patent_score * 100)}%</span>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* v0.4.9: exact wording shared with the matched clause */}
          {rr.predicted_sep_status !== 'Insufficient Evidence' && (rr.matched_phrases !== undefined || rr.matched_terms !== undefined) && (
            <div className="di" style={{ marginTop: 10 }}>
              <div className="di-label">
                Exact phrases matched in {rr.recommended_standard} {rr.recommended_version} · {rr.standard_clause}
              </div>
              {(rr.matched_phrases?.length ?? 0) > 0 ? (
                <div className="phrase-list">
                  {rr.matched_phrases!.map((ph, i) => <span key={i} className="phrase-chip">“{ph}”</span>)}
                </div>
              ) : (
                <div className="di-val" style={{ fontSize: 11, color: 'var(--muted)' }}>
                  No multi-word phrase appears word-for-word in both texts — the match is semantic.
                </div>
              )}
              {(rr.matched_terms?.length ?? 0) > 0 && (
                <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--muted)', marginTop: 4 }}>
                  Shared terms: {rr.matched_terms!.join(', ')}
                </div>
              )}
            </div>
          )}

          {/* Req 5: explanation bullets */}
          {rr.explanation?.length > 0 && (
            <div className="di" style={{ marginTop: 10 }}>
              <div className="di-label">Matched because</div>
              <ul className="explain-list">
                {rr.explanation.map((b, i) => <li key={i}>{b}</li>)}
              </ul>
            </div>
          )}

          {/* Req 9: version evolution trail */}
          {rr.version_evolution?.points?.length > 0 && (
            <div className="di" style={{ marginTop: 10 }}>
              <div className="di-label">
                {rr.version_evolution.standard} · {rr.version_evolution.clause} {rr.version_evolution.clause_title}
              </div>
              <div className="ver-trail">
                {rr.version_evolution.points.map((p, i) => (
                  <span key={p.version} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    {i > 0 && <span className="ver-arrow">→</span>}
                    <span className="ver-chip" title={`${p.release} · ${p.year}`}>
                      {p.version}<span className="ver-chip-sim">{Math.round(p.similarity * 100)}%</span>
                    </span>
                  </span>
                ))}
              </div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 10, marginTop: 4, color:
                rr.version_evolution.trend === 'increasing' ? 'var(--green)' :
                rr.version_evolution.trend === 'decreasing' ? 'var(--red)' : 'var(--muted)' }}>
                {rr.version_evolution.trend === 'increasing' ? '↑' : rr.version_evolution.trend === 'decreasing' ? '↓' : '→'} {rr.version_evolution.trend_label}
              </div>
            </div>
          )}

          {/* Verification */}
          {rr.verification?.current_version && (
            <div className="di" style={{ marginTop: 10 }}>
              <div className="di-label">Version Verification</div>
              <div className="di-val" style={{ fontSize: 11 }}>
                <span style={{ color: 'var(--muted)' }}>Current:</span>{' '}
                <span className="rtag">{rr.verification.current_version}</span>
                {rr.verification.future_version && (
                  <> → <span className="rtag">{rr.verification.future_version}</span></>
                )}
                <div style={{ color: VER_COLOR[rr.verification.status] ?? 'var(--muted)', fontSize: 10, marginTop: 3 }}>
                  {rr.verification.status}
                </div>
                <div style={{ color: 'var(--muted)', fontSize: 10, marginTop: 2, lineHeight: 1.5 }}>
                  {rr.verification.verification}
                </div>
              </div>
            </div>
          )}

          {/* Potential SEP flag */}
          {rr.is_potential_sep && rr.potential_sep && (
            <div className="di" style={{ marginTop: 10, padding: '8px 10px', borderRadius: 6,
              background: 'var(--c-warn-soft)', border: '1px solid color-mix(in srgb, var(--c-warn) 28%, transparent)' }}>
              <div className="di-label" style={{ color: 'var(--yellow)' }}>Potential SEP</div>
              <div className="di-val" style={{ fontSize: 11 }}>
                <span className="domain-chip">{rr.potential_sep.topic}</span>{' '}
                <span className="spec-chip">{rr.potential_sep.standard} {rr.potential_sep.missing_standard_section}</span>
                <div style={{ color: 'var(--muted)', fontSize: 10, marginTop: 4, lineHeight: 1.5 }}>
                  {rr.potential_sep.reason}
                </div>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--yellow)', marginTop: 2 }}>
                  Predicted future version: {rr.potential_sep.predicted_future_version}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main App ───────────────────────────────────────────────────────────────────

const DEEP_FEATS = [
  ['Full 3GPP standards expert analysis'],
  ['Release mapping with confidence scores'],
  ['Protocol component inference'],
  ['SEP strength & FRAND assessment'],
  ['Geographic enforcement strategy'],
  ['Probable title & claim scope inference'],
  ['Portfolio-level intelligence'],
  ['RAG-enhanced with the local 3GPP Retrieval Index'],
  ['Standard + version + clause recommendation'],
  ['Version evolution & verification tracking'],
  ['Similar patent retrieval'],
  ['Potential-SEP vacuum detection'],
  ['Accuracy / F1 / Cohen-κ metrics'],
  ['Publication-style figures (10 charts)'],
];
const QUICK_FEATS = [
  ['Fast extraction — minimal LLM inference'],
  ['Explicit table data only'],
  ['App numbers, jurisdictions, specs'],
  ['Lower latency for quick review'],
];

/**
 * v0.5.0: what ISLD can retrieve standards text from — the installed 3GPP
 * Retrieval Index (documents) and/or the Standards Corpus vector store
 * (chunks). Replaces the old single "Knowledge base" chunk count, which read
 * "Empty" even when the 3GPP Retrieval Index was installed and in use.
 */
function StandardsTextSources({ status, pills = false }: { status: TextMatchStatus; pills?: boolean }) {
  const ri = status.retrieval_index;
  const chunks = status.indexed_chunks ?? 0;
  const items = [
    ri?.ok ? { ok: true, text: `3GPP Retrieval Index: ${(ri.documents ?? 0).toLocaleString()} documents` }
      : { ok: false, text: ri?.configured ? '3GPP Retrieval Index: installed but not usable' : '3GPP Retrieval Index: not installed' },
    { ok: chunks > 0, text: `Standards Corpus: ${chunks.toLocaleString()} chunks` },
  ];
  if (pills) {
    return <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>{items.map(it => <Pill key={it.text} tone={it.ok ? 'ok' : 'muted'} dot>{it.text}</Pill>)}</div>;
  }
  return (
    <div className="note" style={{ marginTop: 0, marginBottom: 8 }}>
      {items.map(it => <div key={it.text} style={{ color: it.ok ? undefined : 'var(--c-muted)' }}>{it.ok ? '✓' : '–'} {it.text}</div>)}
    </div>
  );
}

/**
 * CPARAS ISLD: Declaration Analysis (upload -> extraction -> optional deep LLM enrichment ->
 * research engine -> results and exports) and Text Matching, kept in one mounted component so
 * results survive switching tabs. Talks to /api/analysis/*, /api/text-match/* and /api/models/*.
 */
export default function App() {
  const [files, setFiles] = useState<FileItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadMsg, setLoadMsg] = useState('');
  const [progress, setProgress] = useState<AnalysisProgress | null>(null);
  const progressRef = useRef<AnalysisProgress | null>(null);
  const progressTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const [results, setResults] = useState<AnalysisResponse | null>(null);
  const [error, setError] = useState('');
  // v0.6 start/stop/pause/continue: the job_id of the in-flight /analyze
  // call, so Pause/Stop buttons in ProgressTracker have something to
  // signal (see analysis.py's "start / stop / pause / continue" section).
  const activeJobIdRef = useRef<string | null>(null);
  const [jobControlBusy, setJobControlBusy] = useState(false);
  const [generatingPdf, setGeneratingPdf] = useState(false);
  const [tmGeneratingPdf, setTmGeneratingPdf] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [mode, setMode] = useState<AnalysisMode>(() => (localStorage.getItem('sep_mode') as AnalysisMode) || 'deep');
  const { theme } = useTheme();
  const [useRag, setUseRag] = useState(true);
  const [backendStatus, setBackendStatus] = useState<BackendStatus>({ status: 'checking' });
  const location = useLocation();
  const navigate = useNavigate();
  const activeTab: 'analyze' | 'text-matching' = location.pathname.replace(/\/+$/, '').endsWith('/text-matching') ? 'text-matching' : 'analyze';
  const [searchParams] = useSearchParams();
  const [sourceRecord, setSourceRecord] = useState<{
    id: string; title: string; standard: string; company: string; dipgId: string; project: string;
  } | null>(null);

  // A GITAS/C-SARAS record can deep-link here with "Text Match in cPARAS"
  // (see gitas/pages/PatentDetailPage.tsx) -- this reads that context,
  // switches straight to the Text Matching tab, and keeps the source
  // record visible as reference context above the input. It never
  // prefills the input itself with anything from the dataset (e.g.
  // Normalized_Patent, which is a Yes/No/Error flag, not patent text --
  // see backend/etsi_bulk/schema.py) -- the user pastes the actual
  // claim/disclosure text themselves.
  useEffect(() => {
    const refId = searchParams.get('ref_id');
    if (!refId) return;
    setSourceRecord({
      id: refId,
      title: searchParams.get('ref_title') || '',
      standard: searchParams.get('ref_standard') || '',
      company: searchParams.get('ref_company') || '',
      dipgId: searchParams.get('ref_dipg_id') || '',
      project: searchParams.get('ref_project') || '',
    });
    if (activeTab !== 'text-matching') navigate(`/isld/text-matching?${searchParams.toString()}`, { replace: true });
  }, [searchParams]); // eslint-disable-line react-hooks/exhaustive-deps
  const [tmInput, setTmInput] = useState('');
  const [tmMode, setTmMode] = useState<'paste' | 'upload'>('paste');
  const [tmFile, setTmFile] = useState<File | null>(null);
  const [tmResult, setTmResult] = useState<TextMatchResponse | null>(null);
  const [tmStatus, setTmStatus] = useState<TextMatchStatus | null>(null);
  const [tmLoading, setTmLoading] = useState(false);
  const [tmError, setTmError] = useState<string | null>(null);

  useEffect(() => {
    // Fetched once on mount (not just Text Matching) -- the Analyze tab's
    // Configuration sidebar shows the same status, since it's exactly the
    // signal that tells a person whether "RAG Enhancement" / Deep Mode's
    // RAG retrieval (3GPP Retrieval Index / Standards Corpus) will actually find anything, or is quietly a
    // no-op with nothing indexed yet. Re-fetched on switching TO Text
    // Matching too, in case an import finished while on another tab.
    getTextMatchStatus().then(setTmStatus).catch(() => setTmStatus({ ready: false, indexed_chunks: 0 }));
  }, []);
  useEffect(() => {
    if (activeTab !== 'text-matching') return;
    getTextMatchStatus().then(setTmStatus).catch(() => setTmStatus({ ready: false, indexed_chunks: 0 }));
  }, [activeTab]);

  // v0.4: matching runs as a backend job so it can report progress and be
  // paused / resumed / stopped (control applies between records/segments;
  // Stop keeps every result produced so far).
  const [tmJob, setTmJob] = useState<TextMatchJob | null>(null);
  const [tmControlBusy, setTmControlBusy] = useState(false);
  const tmPollRef = useRef<number | null>(null);
  const stopTmPolling = useCallback(() => {
    if (tmPollRef.current !== null) { window.clearTimeout(tmPollRef.current); tmPollRef.current = null; }
  }, []);
  useEffect(() => stopTmPolling, [stopTmPolling]);

  const tmErrorMessage = (e: any, fallback: string) => {
    // Backend errors come back as a structured {message, supported_formats}
    // detail for file-format issues, or a plain string otherwise.
    const detail = e?.response?.data?.detail;
    return typeof detail === 'string' ? detail
      : detail?.message ? detail.message
      : e instanceof Error ? e.message : fallback;
  };

  const pollTmJob = useCallback((jobId: string) => {
    let failures = 0;
    const tick = async () => {
      try {
        const job = await getTextMatchJob(jobId);
        failures = 0;
        setTmJob(job);
        setTmResult(job.result);
        if (job.status === 'completed' || job.status === 'stopped' || job.status === 'failed') {
          if (job.status === 'failed') setTmError(job.error || 'Text matching failed.');
          setTmLoading(false);
          tmPollRef.current = null;
          return;
        }
      } catch (e: any) {
        failures += 1;
        if (e?.response?.status === 404 || failures >= 5) {
          setTmError(tmErrorMessage(e, 'Lost contact with the text matching job.'));
          setTmLoading(false);
          tmPollRef.current = null;
          return;
        }
      }
      tmPollRef.current = window.setTimeout(tick, 600);
    };
    tmPollRef.current = window.setTimeout(tick, 250);
  }, []);

  const runTextMatch = useCallback(async () => {
    stopTmPolling();
    setTmLoading(true);
    setTmError(null);
    setTmResult(null);   // never show the previous run's matches under a new run
    setTmJob(null);
    try {
      const job = tmMode === 'upload' && tmFile ? await startFileMatchJob(tmFile) : await startTextMatchJob(tmInput);
      setTmJob(job);
      pollTmJob(job.job_id);
    } catch (e: any) {
      setTmError(tmErrorMessage(e, 'Text matching request failed.'));
      setTmLoading(false);
    }
  }, [tmMode, tmFile, tmInput, pollTmJob, stopTmPolling]);

  const controlTm = useCallback(async (action: 'pause' | 'resume' | 'stop') => {
    if (!tmJob) return;
    setTmControlBusy(true);
    try {
      const r = await controlTextMatchJob(tmJob.job_id, action);
      setTmJob(j => (j ? { ...j, status: r.status } : j));
    } catch (e: any) {
      setTmError(tmErrorMessage(e, `Could not ${action} matching.`));
    } finally {
      setTmControlBusy(false);
    }
  }, [tmJob]);
  const [showHelp, setShowHelp] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Progress UI state ─────────────────────────────────────────────────────
  // analysisStartedAt anchors the real elapsed-time timer. showCompletion /
  // completionInfo drive the brief "Analysis Complete" screen once the real
  // pipeline work is done, before switching to the results dashboard.
  // fatalError captures which real stage was in-flight if the /analyze
  // request itself fails outright (vs. a single file's soft per-file error,
  // which is still reported via `error` below as before).
  const [analysisStartedAt, setAnalysisStartedAt] = useState<number | null>(null);
  const [showCompletion, setShowCompletion] = useState(false);
  const [completionInfo, setCompletionInfo] = useState<{ model?: string; backend?: string; succeeded: number; total: number } | null>(null);
  const [fatalError, setFatalError] = useState<{ stage?: string; message: string } | null>(null);
  const [lastRunElapsedMs, setLastRunElapsedMs] = useState<number | null>(null);

  // ── LLM model selection ──────────────────────────────────────────────────
  // Populated from Ollama's installed-models list (GET /models/available)
  // rather than a hardcoded list, so any model the user pulls locally
  // (ollama pull ...) shows up here automatically on refresh/reload.
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>(() => localStorage.getItem('sep_model') || '');
  const [llm, setLlm] = useState<LlmHealth | null>(null);
  const selectedModelRef = useRef(selectedModel);
  selectedModelRef.current = selectedModel;
  const staleSelectionRef = useRef(false);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const llmReachable = !!llm?.reachable;

  // One call returns the whole health contract: server reachability, the
  // installed models (classified text / vision / embedding), the server's
  // configured default and whether it is installed. Only TEXT models are
  // offered for analysis — a vision model such as moondream is OCR-only.
  // The selection is never silently swapped for a different model: a saved
  // choice that is no longer installed is reported, and the fallback is the
  // server's configured OLLAMA_MODEL only if that is actually installed.
  const loadModels = useCallback(async (quiet = false) => {
    if (!quiet) setModelsLoading(true);
    try {
      const h = await getLlmStatus();
      setLlm(h);
      const names = h.text_models;
      setAvailableModels(names);
      let msg = '';
      if (!h.reachable) msg = [h.error, h.hint].filter(Boolean).join(' ') || 'The local LLM server is not reachable.';
      else if (!names.length) msg = h.vision_models.length
        ? 'Only vision/OCR models are installed. Install a text model, e.g.: ollama pull qwen2.5:7b'
        : 'No models are installed in Ollama. Install one, e.g.: ollama pull qwen2.5:7b';
      const prev = selectedModelRef.current;
      if (h.reachable && !(prev && names.includes(prev))) {
        if (prev) {
          // The user's chosen model disappeared (ollama rm). Report it and
          // let them choose — never swap in another model behind their back.
          msg = `The previously selected model "${prev}" is no longer installed. Choose one of the installed models.`;
          staleSelectionRef.current = true;
          localStorage.removeItem('sep_model');
          setSelectedModel('');
        } else if (!staleSelectionRef.current) {
          // Nothing chosen yet: start from the server's configured default,
          // but only if it is actually installed.
          const configured = h.configured_model_resolved && names.includes(h.configured_model_resolved) ? h.configured_model_resolved : '';
          if (configured) setSelectedModel(configured);
        }
      }
      setModelsError(msg);
    } catch (e) {
      setLlm(null);
      setModelsError(apiErrorMessage(e, 'Could not check the local LLM'));
    } finally {
      if (!quiet) setModelsLoading(false);
    }
  }, []);

  const chooseModel = useCallback((m: string) => {
    staleSelectionRef.current = false;
    setSelectedModel(m);
    localStorage.setItem('sep_model', m);
    setModelsError('');
  }, []);

  // ── GPU-based model recommendation ───────────────────────────────────────
  // Best-effort: GET /models/recommended detects local VRAM (nvidia-smi, or
  // unified memory on Apple Silicon) and maps it onto a model sized for
  // this specific machine, rather than making the user guess against a
  // static VRAM table. `gpu.detected: false` (no NVIDIA GPU found, or an
  // unsupported platform) still returns a conservative recommendation —
  // the panel just says so instead of hiding itself, since "here's a safe
  // default" is more useful than nothing.
  const [recommendation, setRecommendation] = useState<RecommendedModelResponse | null>(null);
  const [recLoading, setRecLoading] = useState(false);
  const [pullingModel, setPullingModel] = useState<string | null>(null);
  const [pullDetail, setPullDetail] = useState('');
  const [pullError, setPullError] = useState('');
  const pullPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadRecommendation = useCallback(async () => {
    setRecLoading(true);
    try {
      const data = await getRecommendedModel();
      setRecommendation(data);
    } catch {
      // Silent: this is a "nice to have" suggestion, not core functionality
      // — a failed lookup just means the panel doesn't render, same as if
      // the endpoint simply had nothing to recommend.
      setRecommendation(null);
    } finally {
      setRecLoading(false);
    }
  }, []);

  // Plain derived values (not computed inline in JSX) so the nullability
  // of `recommendation.recommended` is resolved once, here, rather than
  // needing TypeScript to narrow it through a closure at render time.
  const recommendedModelInfo = recommendation?.recommended ?? null;
  const recommendedGpuInfo = recommendation?.gpu ?? null;

  const stopPullPolling = useCallback(() => {
    if (pullPollRef.current) {
      clearInterval(pullPollRef.current);
      pullPollRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPullPolling(), [stopPullPolling]);

  const useRecommendedModel = useCallback(async (modelId: string) => {
    // Already installed — just select it, no download needed.
    if (availableModels.includes(modelId)) {
      chooseModel(modelId);
      return;
    }
    setPullError('');
    setPullDetail('Starting download…');
    setPullingModel(modelId);
    try {
      await pullModel(modelId);
    } catch (err) {
      setPullError(apiErrorMessage(err, `Could not start pulling ${modelId}`));
      setPullingModel(null);
      return;
    }
    stopPullPolling();
    pullPollRef.current = setInterval(async () => {
      try {
        const status = await getPullStatus(modelId);
        if (status.status === 'done') {
          stopPullPolling();
          setPullingModel(null);
          setPullDetail('');
          await loadModels();
          chooseModel(modelId);
        } else if (status.status === 'error') {
          stopPullPolling();
          setPullingModel(null);
          setPullError(status.error || `Failed to pull ${modelId}.`);
        } else {
          setPullDetail(status.detail || 'Downloading…');
        }
      } catch {
        // A transient poll failure isn't fatal — keep polling until the
        // interval itself is cleared, rather than aborting on one blip.
      }
    }, 2000);
  }, [availableModels, chooseModel, loadModels, stopPullPolling]);

  // Backend + LLM health on mount, then re-checked every 20 s so the
  // indicators follow reality (Ollama started/stopped, model pulled)
  // without a page reload. Polls are cheap: /api/tags only, never a
  // generation.
  useEffect(() => {
    const refresh = (quiet: boolean) => {
      checkHealth()
        .then(s => setBackendStatus(s))
        .catch(() => setBackendStatus({ status: 'error' }));
      loadModels(quiet);
    };
    refresh(false);
    loadRecommendation();
    const t = setInterval(() => refresh(true), 20_000);
    return () => clearInterval(t);
  }, [loadModels, loadRecommendation]);


  const saveMode = useCallback((m: AnalysisMode) => { setMode(m); localStorage.setItem('sep_mode', m); }, []);
  const toggleExpand = useCallback((id: string) => setExpanded(p => ({ ...p, [id]: !p[id] })), []);

  const addFiles = useCallback((newFiles: FileList | null) => {
    if (!newFiles) return;
    // Matched on extension rather than MIME type: browsers report CSV/XLSX
    // inconsistently (text/csv, application/vnd.ms-excel, or empty
    // depending on OS and whether Excel is installed), so a MIME check
    // would silently reject valid files. The backend re-checks content
    // regardless.
    const accepted = Array.from(newFiles).filter(f => {
      const name = f.name.toLowerCase();
      return f.type === 'application/pdf' || name.endsWith('.pdf') ||
             name.endsWith('.csv') || name.endsWith('.tsv') ||
             name.endsWith('.xlsx') || name.endsWith('.xlsm') || name.endsWith('.xls') ||
             name.endsWith('.html') || name.endsWith('.htm');
    });
    setFiles(prev => {
      const seen = new Set(prev.map(f => f.file.name));
      return [...prev, ...accepted.filter(f => !seen.has(f.name)).map(f => ({ file: f, id: Math.random().toString(36).slice(2) }))];
    });
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }, [addFiles]);

  const stopProgressPolling = useCallback(() => {
    if (progressTimer.current) {
      clearInterval(progressTimer.current);
      progressTimer.current = null;
    }
  }, []);

  const analyze = useCallback(async () => {
    if (!files.length) return;
    if (!llmReachable) {
      setError(`The local LLM is not reachable. ${[llm?.error, llm?.hint].filter(Boolean).join(' ')}`.trim());
      return;
    }
    if (!selectedModel || !availableModels.includes(selectedModel)) {
      setError('Select an installed text model first (the list refreshes automatically; use the refresh button after `ollama pull`).');
      return;
    }
    // Defensive: never start a second poller without stopping the first.
    // The Analyze button is disabled while `loading`, but the Retry path
    // calls analyze() directly and a stray interval here would poll a dead
    // job id forever, overwriting live state from the real run.
    stopProgressPolling();
    setError('');
    setFatalError(null);
    setLoading(true);
    setResults(null);
    setExpanded({});
    setProgress(null);
    progressRef.current = null as AnalysisProgress | null;
    setShowCompletion(false);
    setCompletionInfo(null);
    const startedAt = Date.now();
    setAnalysisStartedAt(startedAt);

    const jobId = newJobId();
    activeJobIdRef.current = jobId;

    // Poll live progress (current file, pipeline stage, chunk/patent N/M,
    // model, live activity log) while the analyze request is in flight —
    // large multi-chunk declarations can legitimately take a long time now
    // that the artificial timeout was removed, so this is what shows the
    // user it's still working, and what it's actually doing right now.
    progressTimer.current = setInterval(async () => {
      try {
        const p = await getProgress(jobId);
        progressRef.current = p;
        setProgress(p);
      } catch {
        // transient — ignore, next poll will retry
      }
    }, 1200);

    try {
      const result = await analyzeDeclarations(
        files.map(f => f.file),
        mode,
        useRag,
        (msg) => setLoadMsg(msg),
        jobId,
        selectedModel,
      );
      // Real pipeline work is done at this point — stop polling and show a
      // brief, honest completion screen (not a fake extra "stage") before
      // handing off to the results dashboard.
      stopProgressPolling();
      setLastRunElapsedMs(Date.now() - startedAt);
      setCompletionInfo({
        model: result.declarations[0]?._model,
        backend: result.backend,
        succeeded: result.succeeded,
        total: result.total_files,
      });
      setShowCompletion(true);
      await new Promise(resolve => setTimeout(resolve, 1500));
      setResults(result);
      if (result.errors.length) {
        setError(result.errors.map(e => `${e.filename}: ${e.error}`).join('\n'));
      }
      if (result.stopped && result.skipped_files?.length) {
        const skippedMsg = `Stopped by request — ${result.succeeded}/${result.total_files} file(s) completed. `
          + `Not yet started: ${result.skipped_files.join(', ')}. Re-select and re-analyze those files to continue.`;
        setError(prev => (prev ? `${prev}\n${skippedMsg}` : skippedMsg));
      }
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail
        || (err as { message?: string })?.message
        || 'Unknown error';
      // Highlight whichever real stage was in-flight when this broke,
      // without losing any of the stages already completed before it.
      // Prefer failed_stage: when a file fails the backend sets stage='error'
      // and preserves the stage that was actually in flight under
      // failed_stage. Using `stage` alone resolved to 'error', which is not a
      // UI stage, so no card was ever highlighted as the failure point.
      setFatalError({
        stage: progressRef.current?.failed_stage ?? progressRef.current?.stage,
        message: msg,
      });
      if (msg.includes('connect') || msg.includes('ECONNREFUSED')) {
        setError('Cannot reach the local backend. Is it running?\n\nStart the app with start.cmd / start.ps1 (Windows) or ./start.sh (Linux/macOS).');
      } else {
        setError(msg);
      }
    } finally {
      stopProgressPolling();
      setLoading(false);
      setLoadMsg('');
      setShowCompletion(false);
      activeJobIdRef.current = null;
    }
  }, [files, mode, useRag, stopProgressPolling, llmReachable, llm, selectedModel, availableModels, loadModels]);

  // v0.6 start/stop/pause/continue: cooperatively stop the in-flight
  // analysis between files (see ProgressTracker's onPause/onStop props
  // and analysis.py's matching endpoints). Both share the same backend
  // mechanism -- Pause and Stop are offered as distinct buttons only
  // because that's what people expect, not because they behave
  // differently underneath.
  const handlePauseAnalysis = useCallback(async () => {
    const jobId = activeJobIdRef.current;
    if (!jobId) return;
    setJobControlBusy(true);
    try {
      await pauseAnalysis(jobId);
    } catch {
      // best-effort — if the job already finished, there's nothing to pause
    } finally {
      setJobControlBusy(false);
    }
  }, []);

  const handleStopAnalysis = useCallback(async () => {
    const jobId = activeJobIdRef.current;
    if (!jobId) return;
    setJobControlBusy(true);
    try {
      await stopAnalysis(jobId);
    } catch {
      // best-effort — if the job already finished, there's nothing to stop
    } finally {
      setJobControlBusy(false);
    }
  }, []);

  // Safety net: stop polling if the component unmounts mid-analysis.
  useEffect(() => stopProgressPolling, [stopProgressPolling]);

  // Derived stats
  const allD = results?.declarations.flatMap(d => d.patent_disclosures ?? []) ?? [];
  const specC: Record<string, number> = {};
  const countryC: Record<string, number> = {};
  const domainC: Record<string, number> = {};
  allD.forEach(p => {
    (p.explicit_data?.standards ?? []).forEach(s => { specC[s] = (specC[s] ?? 0) + 1; });
    const c = p.explicit_data?.country || 'Unknown';
    countryC[c] = (countryC[c] ?? 0) + 1;
    (p.standards_analysis?.technical_domains ?? []).forEach(d => { if (d.domain) domainC[d.domain] = (domainC[d.domain] ?? 0) + 1; });
  });
  const tSpecs = topN(specC, 6) as [string, number][];
  const tCountries = topN(countryC, 5) as [string, number][];
  const tDomains = topN(domainC, 6) as [string, number][];
  const mxS = tSpecs[0]?.[1] || 1, mxC = tCountries[0]?.[1] || 1, mxD = tDomains[0]?.[1] || 1;

  const inferenceTime = results?.declarations.reduce((acc, d) => acc + (d._inference_time_s ?? 0), 0);

  const product = PRODUCTS.isld;
  const isTM = activeTab === 'text-matching';
  const modeLabel = mode === 'deep' ? 'Deep enrichment' : 'Quick extraction';
  const canAnalyze = !!files.length && !loading && backendStatus.status === 'ok' && llmReachable && !!selectedModel && availableModels.includes(selectedModel);
  const analyzeBlocker = !files.length ? 'Add at least one declaration file'
    : backendStatus.status !== 'ok' ? 'Backend is offline'
    : !llmReachable ? 'The local LLM (Ollama) is not reachable — see the Local model panel'
    : !availableModels.length ? 'No text model is installed in Ollama'
    : !selectedModel ? 'Select an installed model' : null;

  return (
    <div className="page isld-scope">
      <PageHeader
        product={product}
        title={isTM ? 'Text Matching' : 'Declaration Analysis'}
        description={isTM
          ? 'Paste patent claim / disclosure text, or upload an ISLD document, and find the closest-matching clauses in the indexed standards corpus.'
          : 'Upload one or more individual IPR declarations (PDF, CSV, Excel or HTML). A local LLM extracts every disclosure and maps it to 3GPP standards, SEP strength and research-style evidence.'}
        actions={
          <>
            <BackendStatusBadge status={backendStatus} llm={llm} selectedModel={selectedModel} />
            <VersionDropdown />
            <button className="btn btn-sm" onClick={() => setShowHelp(true)} title="Help / user guide"><BookOpen size={13} /> Guide</button>
          </>
        }
      />

      {showHelp && <HelpModal onClose={() => setShowHelp(false)} />}

      {isTM ? (
        <div className="stack">
          <div className="grid-main-side">
            <Card title="Query" icon={<ScanText size={15} />}
              actions={
                <div className="tabs" role="tablist" aria-label="Input type">
                  <button role="tab" aria-selected={tmMode === 'paste'} className={`tab${tmMode === 'paste' ? ' active' : ''}`} onClick={() => setTmMode('paste')}>Paste text</button>
                  <button role="tab" aria-selected={tmMode === 'upload'} className={`tab${tmMode === 'upload' ? ' active' : ''}`} onClick={() => setTmMode('upload')}>Upload ISLD document</button>
                </div>
              }>
              <div className="stack">
                {sourceRecord && (
                  <div className="source-record p-bulk">
                    <div className="eyebrow" style={{ marginBottom: 6 }}>Source record from CPARAS Bulk — reference context, not the matching input</div>
                    <div className="kv" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))' }}>
                      {sourceRecord.title && <div className="kv-item" style={{ gridColumn: '1 / -1' }}><div className="kv-label">Title</div><div className="kv-value">{sourceRecord.title}</div></div>}
                      {sourceRecord.standard && <div className="kv-item"><div className="kv-label">Standard</div><div className="kv-value">{sourceRecord.standard}</div></div>}
                      {sourceRecord.company && <div className="kv-item"><div className="kv-label">Company</div><div className="kv-value">{sourceRecord.company}</div></div>}
                      {sourceRecord.project && <div className="kv-item"><div className="kv-label">Project</div><div className="kv-value">{sourceRecord.project}</div></div>}
                      {sourceRecord.dipgId && <div className="kv-item"><div className="kv-label">DIPG_ID</div><div className="kv-value mono">{sourceRecord.dipgId}</div></div>}
                    </div>
                    <div className="row" style={{ marginTop: 8 }}>
                      <button className="btn btn-sm" onClick={() => sourceRecord.title && setTmInput(sourceRecord.title)} disabled={!sourceRecord.title}
                        title="Title-only demo query — not patent claim text">Use title as demo query</button>
                      <span className="small muted">Title-only demo query — not patent claim text</span>
                      {/* v0.5.0: the Bulk record itself is only reachable for roles with CPARAS Bulk access */}
                      <Gate permission="bulk.use">
                        <Link className="btn btn-sm btn-ghost" to={`/bulk/patent/${sourceRecord.id}`} style={{ marginLeft: 'auto' }}><Boxes size={13} /> Open record</Link>
                      </Gate>
                    </div>
                  </div>
                )}

                {tmMode === 'paste' ? (
                  <textarea className="textarea" value={tmInput} onChange={(e) => setTmInput(e.target.value)}
                    placeholder="Paste patent claim or disclosure text here…" rows={7} aria-label="Claim or disclosure text" />
                ) : (
                  <div className="stack-sm">
                    <label className="drop" style={{ cursor: 'pointer' }}>
                      <input type="file" style={{ display: 'none' }}
                        accept=".pdf,.csv,.xlsx,.xls,.docx,.txt,application/pdf,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
                        onChange={(e) => setTmFile(e.target.files?.[0] ?? null)} />
                      <span className="drop-icon"><UploadCloud size={18} /></span>
                      <span className="drop-title">{tmFile ? tmFile.name : 'Choose an ISLD / declaration / claim-disclosure file'}</span>
                      <span className="drop-sub">PDF, CSV, XLSX, XLS, DOCX or TXT</span>
                    </label>
                    <div className="field-hint">
                      Text is extracted automatically (with OCR for scanned PDFs). For CSV/XLSX/XLS, each declared row's title,
                      standard and illustrative/specific-part clause are matched individually.
                    </div>
                  </div>
                )}

                <div className="row">
                  <button className="btn btn-product" onClick={runTextMatch}
                    disabled={tmLoading || (tmMode === 'paste' ? !tmInput.trim() : !tmFile)}>
                    {tmLoading
                      ? (tmJob?.status === 'paused' ? <><Pause size={14} /> Paused</> : <><span className="spinner" /> Matching…</>)
                      : <><SearchCode size={14} /> Find matches</>}
                  </button>
                  {tmLoading && tmJob && (
                    <>
                      {tmJob.status === 'paused' ? (
                        <button className="btn" onClick={() => controlTm('resume')} disabled={tmControlBusy}
                          title="Continue matching from where it paused">
                          <Play size={14} /> Resume
                        </button>
                      ) : (
                        <button className="btn" onClick={() => controlTm('pause')} disabled={tmControlBusy}
                          title="Pause after the record/segment currently being matched">
                          <Pause size={14} /> Pause
                        </button>
                      )}
                      <button className="btn" onClick={() => controlTm('stop')} disabled={tmControlBusy}
                        title="Stop after the current record/segment and keep the results so far"
                        style={{ color: 'var(--c-err)' }}>
                        <Square size={13} /> Stop
                      </button>
                    </>
                  )}
                  {tmResult && !tmLoading && (
                    <button className="btn" disabled={tmGeneratingPdf}
                      onClick={() => {
                        if (!tmResult) return;
                        setTmGeneratingPdf(true);
                        // Let the disabled state paint before the synchronous jsPDF build.
                        setTimeout(() => {
                          try {
                            generateTextMatchPdfReport(tmResult, { queryText: tmMode === 'paste' ? tmInput : undefined, sourceRecord });
                          } finally {
                            setTmGeneratingPdf(false);
                          }
                        }, 30);
                      }}>
                      {tmGeneratingPdf ? <><span className="spinner" /> Generating…</> : <><Download size={14} /> PDF report</>}
                    </button>
                  )}
                </div>
                {tmJob && (tmLoading || tmJob.status === 'stopped') && (() => {
                  const pct = tmJob.total ? Math.round((tmJob.done / tmJob.total) * 100) : 0;
                  const unit = tmResult?.records_extracted ? 'records' : 'segments';
                  const label = tmJob.status === 'queued' || tmJob.status === 'extracting'
                    ? (tmJob.status === 'extracting' ? 'Extracting text…' : 'Starting…')
                    : tmJob.status === 'paused' ? `Paused — ${tmJob.done} of ${tmJob.total} ${unit} matched`
                    : tmJob.status === 'stopped' ? `Stopped — ${tmJob.done} of ${tmJob.total} ${unit} matched (results so far are kept)`
                    : `Matching ${Math.min(tmJob.done + 1, tmJob.total)} of ${tmJob.total} ${unit}…`;
                  return (
                    <div className="stack-sm" aria-live="polite">
                      <div className="row small" style={{ justifyContent: 'space-between' }}>
                        <span className={tmJob.status === 'stopped' ? '' : 'muted'}>{label}</span>
                        {tmJob.total > 0 && <span className="muted mono">{pct}% · {tmJob.elapsed_s.toFixed(0)}s</span>}
                      </div>
                      <div className={`progress${tmJob.total ? '' : ' is-indeterminate'}`}
                        role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
                        <span style={tmJob.total ? { width: `${pct}%`, opacity: tmJob.status === 'paused' ? 0.5 : 1 } : undefined} />
                      </div>
                    </div>
                  );
                })()}
                {tmError && <Banner tone="err" title="Matching failed">{tmError}</Banner>}
              </div>
            </Card>

            <div className="stack">
              <Card title="Standards text" icon={<Database size={15} />}>
                <div className="stack-sm">
                  {tmStatus === null ? <Pill tone="muted" dot>Checking…</Pill>
                    : tmStatus.ready ? <StandardsTextSources status={tmStatus} pills />
                    : <Pill tone="warn" dot>No standards text indexed yet</Pill>}
                  {tmStatus && !tmStatus.ready && (
                    <div className="field-hint">
                      Install the 3GPP Retrieval Index (copy the <code>retrieval_data</code> folder into <code>dist\retrieval_data</code>),
                      import an ETSI dataset on the backend (<code>python -m etsi_corpus.cli import --jsonl etsi_dataset.jsonl</code>),
                      or index 3GPP series on the <Gate permission="reference.view" fallback={<>Standards Corpus page (ask a Data Entry user or Super Admin)</>}><Link className="link" to="/corpus">Standards Corpus</Link> page</Gate>.
                    </div>
                  )}
                </div>
              </Card>
            </div>
          </div>

          {tmResult ? (
            <Card title={tmLoading ? 'Matches (in progress)' : tmJob?.status === 'stopped' ? 'Matches (stopped early)' : 'Matches'} icon={<ScanText size={15} />}
              subtitle={tmResult.source_filename ? `From ${tmResult.source_filename} · extraction: ${tmResult.extraction_method ?? 'unknown'}` : `${tmResult.segments.length} segment${tmResult.segments.length === 1 ? '' : 's'}`}>
              <div className="stack">
                {tmResult.file_format && (
                  <div className="box">
                    <div className="row small" style={{ gap: 16 }}>
                      <span><span className="muted">Format</span> <b>{tmResult.file_format.toUpperCase()}</b></span>
                      {tmResult.text_source && <span><span className="muted">Text source</span> <b>{tmResult.text_source}</b></span>}
                      {typeof tmResult.records_extracted === 'number' && tmResult.records_extracted > 0 && (
                        <span><span className="muted">Records extracted</span> <b style={{ color: 'var(--c-ok)' }}>{tmResult.records_extracted}</b></span>
                      )}
                    </div>
                    {tmResult.extraction_preview && (
                      <div className="small muted" style={{ whiteSpace: 'pre-wrap', maxHeight: 140, overflowY: 'auto', borderTop: '1px solid var(--c-border)', paddingTop: 6, marginTop: 8, fontFamily: 'var(--c-mono)' }}>
                        {tmResult.extraction_preview}{tmResult.extraction_preview.length >= 1000 ? '…' : ''}
                      </div>
                    )}
                  </div>
                )}
                {tmResult.warning && <Banner tone="warn">{tmResult.warning}</Banner>}
                <div>
                  {tmResult.segments.map((seg, i) => (
                    <div key={i} className="tm-segment">
                      <div className="tm-segment-text">
                        <span className="pill pill-muted" style={{ marginRight: 8 }}>{seg.label || `Segment ${i + 1}`}</span>
                        {seg.segment}
                        {seg.query_basis && seg.query_basis.startsWith('declared standard') && (
                          <div className="small muted" style={{ marginTop: 4 }}>
                            No title or clause in the file for this disclosure — matched on the declared standard's scope, limited to that standard.
                          </div>
                        )}
                      </div>
                      {seg.matches.length === 0 ? (
                        <div className="small muted">No matches above threshold.</div>
                      ) : seg.matches.map((m, j) => (
                        <div key={j} className="tm-match">
                          <span className="tm-score" style={{ color: m.score >= 0.6 ? 'var(--c-ok)' : m.score >= 0.4 ? 'var(--c-warn)' : 'var(--c-muted)' }}>{(m.score * 100).toFixed(0)}%</span>
                          <span className="tm-match-body">
                            {m.metadata?.declared_standard_match === true && <span className="rtag" title="This clause is from a standard the record declares" style={{ color: 'var(--c-ok)', marginRight: 4 }}>declared</span>}
                            {(m.metadata?.spec as string) && <span className="spec-chip">{m.metadata?.spec as string}</span>}
                            {(m.metadata?.clause as string) && <span className="rtag">{m.metadata?.clause as string}</span>}
                            {' '}{m.content}
                          </span>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          ) : !tmLoading && (
            <Card title="How matching works">
              <div className="tm-steps">
                {TEXT_MATCHING_STEPS.map((st, i) => (
                  <div key={st.title} className="tm-step">
                    <div className="tm-step-num">{i + 1}</div>
                    <div>
                      <div className="tm-step-title">{st.title}</div>
                      <div className="tm-step-desc">{st.desc}</div>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      ) : (
      <div className="isld-layout">
        {/* ── Setup column ── */}
        <aside className="panel isld-setup" aria-label="Analysis setup">
          <div className="panel-header">
            <div className="ph-left"><Settings2 size={15} style={{ color: 'var(--c-isld)' }} /><h2>Analysis setup</h2></div>
            <Pill tone="product" className="p-isld">{modeLabel}</Pill>
          </div>

          {/* Scrolls independently of the page so every setup row (model status,
              Analyze button, capabilities) stays reachable on short screens. */}
          <div className="isld-setup-body" tabIndex={0} aria-label="Analysis setup options">

          <div className="setup-section">
            <div className="setup-title"><span>1 · Declarations</span>{files.length > 0 && <span className="pill pill-muted">{files.length} file{files.length === 1 ? '' : 's'}</span>}</div>
            <div
              className={`drop${dragging ? ' drag' : ''}`}
              role="button" tabIndex={0} aria-label="Add declaration files"
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInputRef.current?.click(); } }}
              onDragOver={e => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <span className="drop-icon"><UploadCloud size={18} /></span>
              <span className="drop-title">Drop ETSI IPR declarations here</span>
              <span className="drop-sub">PDF, CSV, TSV, Excel (XLSX/XLS) or HTML · multiple files supported</span>
              <input ref={fileInputRef} type="file" accept=".pdf,.csv,.tsv,.xlsx,.xlsm,.xls,.html,.htm,application/pdf,text/csv,text/tab-separated-values,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,text/html" multiple style={{ display: 'none' }} onChange={e => addFiles(e.target.files)} />
            </div>
            {files.length > 0 && (
              <div className="file-list">
                {files.map(({ file, id }) => (
                  <div key={id} className="file-item">
                    <span style={{ display: 'flex', alignItems: 'center', flexShrink: 0, color: 'var(--c-muted)' }}>
                      {/\.(csv|tsv|xlsx|xlsm|xls)$/i.test(file.name) ? <FileSpreadsheet size={16} /> : <FileText size={16} />}
                    </span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="file-name" title={file.name}>{file.name}</div>
                      <div className="file-size">{fmt(file.size)}</div>
                    </div>
                    <button className="rm-btn" onClick={() => setFiles(p => p.filter(f => f.id !== id))} aria-label={`Remove ${file.name}`}><X size={14} /></button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="setup-section">
            <div className="setup-title">2 · Analysis mode</div>
            <div className="mode-opts">
              {([['quick', 'Quick', 'Explicit table data only. Fast.'],
                ['deep', 'Deep', 'Full 3GPP enrichment, SEP strength, portfolio.']] as const).map(([v, n, d]) => (
                <label key={v} className={`mode-opt${mode === v ? ' active' : ''}`}>
                  <input type="radio" name="mode" checked={mode === v} onChange={() => saveMode(v)} />
                  <div><div className="mode-name">{n}</div><div className="mode-desc">{d}</div></div>
                </label>
              ))}
            </div>
            {mode === 'deep' && (
              <label className="check" style={{ marginTop: 10, alignItems: 'flex-start' }}>
                <input type="checkbox" checked={useRag} onChange={e => setUseRag(e.target.checked)} style={{ marginTop: 2 }} />
                <span><b style={{ fontWeight: 600 }}>RAG enhancement</b><br /><span className="small muted">Retrieve relevant telecom knowledge from the local vector store.</span></span>
              </label>
            )}
          </div>

          <div className="setup-section">
            <div className="setup-title"><span>3 · Local model</span>
              <button className="btn btn-sm btn-ghost btn-icon" onClick={() => loadModels(false)} disabled={modelsLoading} title="Re-check Ollama and refresh installed models" aria-label="Re-check Ollama and refresh installed models">
                {modelsLoading ? <span className="spinner" /> : <RefreshCw size={13} />}
              </button>
            </div>
            <div className="stack-sm">
              <select className="select" value={selectedModel} onChange={e => chooseModel(e.target.value)} disabled={modelsLoading || !availableModels.length}
                style={{ width: '100%', colorScheme: theme === 'dark' ? 'dark' : 'light' }} aria-label="LLM model">
                {availableModels.length
                  ? <>
                      {!selectedModel && <option value="" disabled>Select an installed text model…</option>}
                      {availableModels.map(m => <option key={m} value={m}>{m}{llm?.configured_model_resolved === m ? ' (server default)' : ''}</option>)}
                    </>
                  : <option value="">{llmReachable ? 'No text models installed' : 'Ollama not reachable'}</option>}
              </select>
              <div className="status-line"><span className="status-dot" style={{ background: backendStatus.status === 'ok' ? 'var(--c-ok)' : backendStatus.status === 'checking' ? 'var(--c-muted-2)' : 'var(--c-err)' }} />
                {backendStatus.status === 'ok'
                  ? <>Backend running · <span className="mono small">{(backendStatus.backend || 'local').toUpperCase()}</span></>
                  : backendStatus.status === 'checking' ? 'Checking backend…'
                  : <>Backend offline — start it with <code>start.ps1</code> (Windows) or <code>./start.sh</code></>}
              </div>
              <LlmStatusPanel llm={llm} selectedModel={selectedModel} loading={modelsLoading} onGenerationChecked={() => loadModels(true)} />
              {modelsError && llmReachable && <div className="note" style={{ color: 'var(--c-warn)' }}>{modelsError}</div>}

              {recLoading && !recommendation && (
                <div className="note"><span className="spinner" style={{ marginRight: 6 }} />Checking your GPU for a recommendation…</div>
              )}
              {recommendedModelInfo && (
                <div className="box">
                  <div className="box-label"><Cpu size={11} style={{ verticalAlign: -1 }} /> Recommended for this machine</div>
                  <div className="small muted" style={{ marginBottom: 5 }}>
                    {recommendedGpuInfo?.detected
                      ? <>{recommendedGpuInfo.name} — {recommendedGpuInfo.vram_gb} GB VRAM <span style={{ opacity: .75 }}>(via {recommendedGpuInfo.source})</span></>
                      : <span title={recommendedGpuInfo?.error ?? undefined}>No GPU detected — safe default for CPU-only systems{recommendedGpuInfo?.error ? <span style={{ opacity: .75 }}> ({recommendedGpuInfo.error})</span> : null}</span>}
                  </div>
                  <div style={{ fontSize: 12.5, marginBottom: 7 }}>
                    <b className="mono" style={{ fontWeight: 500 }}>{recommendedModelInfo.name.replace(' ← RECOMMENDED', '')}</b>
                    {(recommendedModelInfo.speed || recommendedModelInfo.quality) && (
                      <span className="muted"> ({[recommendedModelInfo.speed, recommendedModelInfo.quality].filter(Boolean).join(', ')}, ~{recommendedModelInfo.vram_gb} GB)</span>
                    )}
                  </div>
                  {selectedModel === recommendedModelInfo.id ? (
                    <Pill tone="ok" dot>Currently selected</Pill>
                  ) : pullingModel === recommendedModelInfo.id ? (
                    <div className="small muted"><span className="spinner" style={{ marginRight: 6 }} />{pullDetail || 'Downloading…'}</div>
                  ) : (
                    <button className="btn btn-sm" onClick={() => useRecommendedModel(recommendedModelInfo.id)}>
                      {availableModels.includes(recommendedModelInfo.id) ? 'Use this model' : `Download & use (~${recommendedModelInfo.vram_gb} GB)`}
                    </button>
                  )}
                  {pullError && !pullingModel && <div className="note" style={{ color: 'var(--c-err)' }}>{pullError}</div>}
                </div>
              )}
            </div>
          </div>

          <div className="setup-section">
            <div className="setup-title"><span>Standards text</span>
              {tmStatus === null ? <span className="pill pill-muted">Checking…</span>
                : tmStatus.ready ? <span className="pill pill-ok pill-dot">Available</span>
                : <span className="pill pill-warn pill-dot">Empty</span>}
            </div>
            {tmStatus && <StandardsTextSources status={tmStatus} />}
            {tmStatus && !tmStatus.ready && (
              <div className="note" style={{ marginTop: 0, marginBottom: 8 }}>
                No standards text available yet — RAG enhancement and deep 3GPP enrichment will find little to retrieve.
                Install the 3GPP Retrieval Index or import a dataset into the Standards Corpus.
              </div>
            )}
          </div>

          <div className="setup-section">
            <div className="btn-row" style={{ marginTop: 0 }}>
              <button className="analyze-btn" disabled={!canAnalyze} onClick={analyze} title={analyzeBlocker ?? undefined}>
                {loading ? <><span className="spinner" />Analyzing…</> : <><Play size={15} />Analyze {files.length || ''} declaration{files.length !== 1 ? 's' : ''}</>}
              </button>
            </div>
            {!loading && analyzeBlocker && <div className="note">{analyzeBlocker}.</div>}
            {results && (
              <>
                <div className="section-label" style={{ marginTop: 12 }}>Export results</div>
                <div className="btn-row" style={{ marginTop: 0 }}>
                  <button className="dl-btn" style={{ flex: 1 }} disabled={generatingPdf}
                    onClick={() => {
                      if (!results) return;
                      setGeneratingPdf(true);
                      // Let the disabled state paint before the synchronous jsPDF build.
                      setTimeout(() => {
                        try {
                          generateCparasPdfReport(results);
                        } finally {
                          setGeneratingPdf(false);
                        }
                      }, 30);
                    }}>
                    {generatingPdf ? <><span className="spinner" />PDF…</> : <><Download size={13} />PDF</>}
                  </button>
                  <button className="dl-btn" style={{ flex: 1 }} onClick={() => results && generateReport(results)}><Download size={13} />HTML</button>
                  <button className="dl-btn" style={{ flex: 1 }} onClick={() => results && exportJSON(results)}><Download size={13} />JSON</button>
                  <button className="dl-btn" style={{ flex: 1 }} onClick={() => results && exportCSV(results)}><Download size={13} />CSV</button>
                  <button className="dl-btn" style={{ flex: 1 }}
                    disabled={!results?.declarations.some(d => d._declaration_id)}
                    title="Enriched Excel workbook per declaration (source rows + CPARAS columns), built by the backend from the saved analysis"
                    onClick={() => {
                      const ids = (results?.declarations ?? []).map(d => d._declaration_id).filter((x): x is number => !!x);
                      ids.forEach((id, i) => setTimeout(() => {
                        const a = document.createElement('a');
                        a.href = `/api/analysis/history/${id}/export/excel`;
                        a.rel = 'noopener';
                        document.body.appendChild(a); a.click(); a.remove();
                      }, i * 600));
                    }}><Download size={13} />Excel</button>
                </div>
              </>
            )}
          </div>

          <div className="setup-section">
            <div className="setup-title">{modeLabel} includes</div>
            <div className="cap-list">
              {(mode === 'deep' ? DEEP_FEATS : QUICK_FEATS).map(([txt]) => (
                <div key={txt} className="cap-item">
                  <span className="cap-bullet" aria-hidden="true" />
                  <span className="cap-text">{txt}</span>
                </div>
              ))}
            </div>
          </div>
          </div>
        </aside>

        {/* ── Results column ── */}
        <div className="isld-results">
          {error && <div className="err-box" role="alert" style={{ whiteSpace: 'pre-wrap' }}>{error}</div>}

          {loading && !showCompletion && (
            <ProgressTracker
              progress={progress}
              mode={mode}
              startedAt={analysisStartedAt ?? Date.now()}
              uploadMessage={loadMsg}
              onPause={handlePauseAnalysis}
              onStop={handleStopAnalysis}
              jobControlBusy={jobControlBusy}
            />
          )}

          {loading && showCompletion && (
            <CompletionView
              stages={stagesForMode(mode)}
              model={completionInfo?.model}
              backend={completionInfo?.backend}
              succeeded={completionInfo?.succeeded ?? 0}
              total={completionInfo?.total ?? 0}
            />
          )}

          {!loading && fatalError && (
            <ProgressTracker
              progress={progress}
              mode={mode}
              startedAt={analysisStartedAt ?? Date.now()}
              fatalError={fatalError}
              onRetry={() => { setFatalError(null); analyze(); }}
            />
          )}

          {!results && !loading && !fatalError && (
            <div className="panel">
              <EmptyState
                icon={<SearchCode size={22} />}
                title="No analysis yet"
                body={<>
                  Add declarations in <b>Analysis setup</b>, choose a mode and model, then run the analysis.
                  CSV / Excel / HTML annex exports are mapped straight from their columns — no OCR, no extraction model.
                  All inference runs locally; nothing leaves your machine.
                </>}
              />
              <div style={{ padding: '0 18px 18px' }}>
                <div className="tm-steps">
                  {[
                    ['Upload', 'PDF (OCR if scanned), CSV/TSV, Excel or HTML declarations.'],
                    ['Extract', 'Every declared disclosure row is read and kept — repeated numbers are not merged.'],
                    ['Enrich', 'Quick: explicit table data. Deep: 3GPP releases, domains, SEP strength, portfolio.'],
                    ['Evidence', 'Standard / version / clause recommendation, verification and research figures.'],
                  ].map(([t, d], i) => (
                    <div key={t} className="tm-step"><div className="tm-step-num">{i + 1}</div><div><div className="tm-step-title">{t}</div><div className="tm-step-desc">{d}</div></div></div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {results && (
            <>
              <div className="stats-strip">
                {([
                  ['Disclosures', allD.length, 'declared patent rows', 'var(--c-isld)'],
                  ['Declarations', results.declarations.length, 'IPR filings analysed', 'var(--c-accent)'],
                  ['Specifications', Object.keys(specC).length, '3GPP TSs referenced', 'var(--c-ok)'],
                  ['Jurisdictions', Object.keys(countryC).length, 'filing countries', 'var(--c-warn)'],
                ] as const).map(([label, val, sub, color]) => (
                  <div key={label} className="stat-card" style={{ '--stat-color': color } as React.CSSProperties}>
                    <div className="stat-label">{label}</div>
                    <div className="stat-value">{val}</div>
                    <div className="stat-sub">{sub}</div>
                  </div>
                ))}
              </div>

              <div className="analysis-metadata">
                {results.declarations[0]?._model && (
                  <div className="analysis-metadata-item">
                    <div className="stat-label">Model used</div>
                    <ModelBadge model={results.declarations[0]._model} backend={results.backend} size="sm" />
                  </div>
                )}
                <div className="analysis-metadata-item">
                  <div className="stat-label">Backend</div>
                  <div className="analysis-metadata-value">{(results.backend || 'unknown').toUpperCase()}</div>
                </div>
                {inferenceTime != null && (
                  <div className="analysis-metadata-item">
                    <div className="stat-label">Inference</div>
                    <div className="analysis-metadata-value">{inferenceTime.toFixed(1)}s</div>
                  </div>
                )}
                {lastRunElapsedMs != null && (
                  <div className="analysis-metadata-item">
                    <div className="stat-label">Completed in</div>
                    <div className="analysis-metadata-value">{(lastRunElapsedMs / 1000).toFixed(1)}s</div>
                  </div>
                )}
                <span className="small muted" style={{ marginLeft: 'auto' }}>No cloud APIs used</span>
              </div>

              {(tSpecs.length > 0 || tCountries.length > 0 || tDomains.length > 0) && (
                <div className="charts-row">
                  {tSpecs.length > 0 && (
                    <div className="panel">
                      <div className="panel-header"><div className="ph-left"><div className="panel-dot" /><h2>Top 3GPP specifications</h2></div></div>
                      <div className="panel-body"><BarChart items={tSpecs} max={mxS} /></div>
                    </div>
                  )}
                  {tCountries.length > 0 && (
                    <div className="panel">
                      <div className="panel-header"><div className="ph-left"><div className="panel-dot" style={{ background: 'var(--c-ok)' }} /><h2>Jurisdictions</h2></div></div>
                      <div className="panel-body"><BarChart items={tCountries.map(([c, n]) => [shortC(c), n] as [string, number])} max={mxC} colorOffset={2} /></div>
                    </div>
                  )}
                  {tDomains.length > 0 && (
                    <div className="panel">
                      <div className="panel-header"><div className="ph-left"><div className="panel-dot" style={{ background: 'var(--c-warn)' }} /><h2>Technical domains (AI inferred)</h2></div></div>
                      <div className="panel-body"><BarChart items={tDomains.map(([d, n]) => [d.slice(0, 18), n] as [string, number])} max={mxD} colorOffset={1} /></div>
                    </div>
                  )}
                </div>
              )}

              {results.declarations.map((decl: Declaration, di) => {
                const meta = decl.declaration_metadata ?? {};
                const port = decl.portfolio_level_analysis ?? {};
                const discs = decl.patent_disclosures ?? [];
                const hasPort = Object.values(port).some(v => Array.isArray(v) ? v.length > 0 : !!v);
                const ocrMethod = decl._ocr_quality?.method;
                const isScanned = !!ocrMethod && (OCR_EXTRACTION_METHODS as readonly string[]).includes(ocrMethod);
                const lowConfPages = decl._ocr_quality?.low_confidence_pages ?? [];
                const countDiag = decl._count_diagnostic;
                const src = decl._source;
                const isTabularSrc = src?.kind === 'csv' || src?.kind === 'xlsx' || src?.kind === 'html';
                const csvMeta = isTabularSrc ? src.csv : null;
                const isMappedCsv = isTabularSrc && src.used_extraction_model === false;
                const tabularLabel = src?.kind === 'xlsx' ? 'Excel' : src?.kind === 'html' ? 'HTML table' : 'CSV';
                const srcWarnings = decl._source_warnings ?? [];

                return (
                  <div key={di} className="decl-panel">
                    <div className="panel">
                      <div className="panel-header">
                        <div className="ph-left" style={{ minWidth: 0 }}>
                          <div className="panel-dot" />
                          <h2 className="clip">{meta.declarant || decl.filename || `Declaration ${di + 1}`}</h2>
                          {meta.declarant && decl.filename && <span className="small muted clip" title={decl.filename}>{decl.filename}</span>}
                        </div>
                        <div className="row" style={{ gap: 6 }}>
                          {meta.likely_standard_generation && <span className="tag t-a">{meta.likely_standard_generation}</span>}
                          <ConfPill score={meta.confidence} />
                        </div>
                      </div>
                      <div className="panel-body">
                        {isMappedCsv && (
                          <div className="src-info-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-ok)' }}><Info size={15} /></span>
                            <div>
                              <div className="ocr-warning-text">
                                <b>Read directly from {tabularLabel} columns.</b> {csvMeta?.rows_parsed ?? discs.length} row{(csvMeta?.rows_parsed ?? discs.length) !== 1 ? 's' : ''} mapped without an extraction model.
                              </div>
                              <div className="ocr-warning-sub">
                                Application numbers, publication numbers and standard codes were transcribed from the file rather than inferred, so they are not subject to extraction error.
                                {typeof csvMeta?.rows_skipped === 'number' && csvMeta.rows_skipped > 0 && ` ${csvMeta.rows_skipped} non-data row${csvMeta.rows_skipped !== 1 ? 's were' : ' was'} skipped (blank, repeated header, or annex boilerplate).`}
                                {!!csvMeta?.unmapped_columns?.length && ` ${csvMeta.unmapped_columns.length} column${csvMeta.unmapped_columns.length !== 1 ? 's were' : ' was'} not recognised and ignored: ${csvMeta.unmapped_columns.slice(0, 4).join(', ')}${csvMeta.unmapped_columns.length > 4 ? '…' : ''}.`}
                                {' '}Any analysis, scoring or classification shown below is still model-generated.
                              </div>
                            </div>
                          </div>
                        )}
                        {srcWarnings.length > 0 && (
                          <div className="ocr-warning-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-warn)' }}><AlertTriangle size={15} /></span>
                            <div>
                              <div className="ocr-warning-text"><b>Check the file's structure.</b> The file was read, but not everything could be determined confidently.</div>
                              <div className="ocr-warning-sub">{srcWarnings.join(' ')}</div>
                            </div>
                          </div>
                        )}
                        {isTabularSrc && src.used_extraction_model && (
                          <div className="ocr-warning-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-warn)' }}><AlertTriangle size={15} /></span>
                            <div>
                              <div className="ocr-warning-text"><b>{tabularLabel} columns were not recognised.</b> This file was analysed by the language model instead of being mapped directly.</div>
                              <div className="ocr-warning-sub">Expected a column for application number, publication number or title. Rename the columns to match the ETSI annex headings for a faster, exact result — and verify the rows below against the source file in the meantime.</div>
                            </div>
                          </div>
                        )}
                        {decl._persist_error && (
                          <div className="ocr-warning-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-err)' }}><AlertTriangle size={15} /></span>
                            <div>
                              <div className="ocr-warning-text"><b>Not saved.</b> {decl._persist_error}</div>
                            </div>
                          </div>
                        )}
                        {decl._possibly_incomplete && (
                          <div className="ocr-warning-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-warn)' }}><AlertTriangle size={15} /></span>
                            <div>
                              <div className="ocr-warning-text"><b>Extraction may be incomplete.</b> {decl._incomplete_chunk_count ?? 1} part(s) of this document did not return a complete result from the local model.</div>
                              <div className="ocr-warning-sub">
                                {(decl._failed_chunks ?? []).slice(0, 3).map(f => `Part ${f.part}: ${f.reason}`).join(' · ') || 'The model output was cut off or only partly readable.'}
                                {' '}Re-run the analysis (a larger model or more memory helps), and verify the rows below against the source file.
                              </div>
                            </div>
                          </div>
                        )}
                        {countDiag?.significant_gap && (
                          <div className="ocr-warning-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-warn)' }}><AlertTriangle size={15} /></span>
                            <div>
                              <div className="ocr-warning-text"><b>Some disclosures may be missing.</b> {countDiag.extracted_patents} extracted vs. ~{countDiag.detected_source_numbers} numbers found in the source PDF.</div>
                              <div className="ocr-warning-sub">{countDiag.message}</div>
                            </div>
                          </div>
                        )}
                        {isScanned && (
                          <div className="ocr-warning-banner">
                            <span className="ocr-warning-icon" style={{ color: 'var(--c-warn)' }}><AlertTriangle size={15} /></span>
                            <div>
                              <div className="ocr-warning-text"><b>This document is scanned.</b> OCR is undergoing development and testing. Please check manually.</div>
                              <div className="ocr-warning-sub">
                                No embedded text layer was found in this PDF, so it was read using {ocrMethod === 'tesseract' ? 'Tesseract OCR' : ocrMethod === 'vision_ollama' ? 'a vision-model OCR fallback' : 'a best-effort text extraction fallback'}.
                                {lowConfPages.length > 0 && ` ${lowConfPages.length} page${lowConfPages.length !== 1 ? 's' : ''} scored low OCR confidence (page${lowConfPages.length !== 1 ? 's' : ''} ${lowConfPages.join(', ')}).`}
                                {' '}The extracted data below is shown as-is — verify application numbers, publication numbers, and standard codes against the source PDF before relying on them.
                              </div>
                            </div>
                          </div>
                        )}
                        {meta.portfolio_characterization && (
                          <div className="meta-row">
                            <div className="meta-item">
                              <div className="meta-label">Portfolio characterization</div>
                              <div className="meta-val">{meta.portfolio_characterization}</div>
                            </div>
                            {(meta.overall_technology_domains ?? []).length > 0 && (
                              <div className="meta-item">
                                <div className="meta-label">Technology domains</div>
                                <div className="tag-list" style={{ marginTop: 4 }}>
                                  {meta.overall_technology_domains!.map((d, i) => <span key={i} className="tag t-p">{d}</span>)}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                        {hasPort && (
                          <>
                            <div className="sec-title">Portfolio intelligence</div>
                            <div className="p-grid">
                              {([
                                ['Strength assessment', port.portfolio_strength_assessment],
                                ['Geographic strategy', port.geographic_filing_strategy],
                                ['FRAND implications', port.frand_implications],
                                ['Implementation risk', port.implementation_risk_summary],
                              ] as [string, string | undefined][]).filter(([, v]) => v).map(([t, v]) => (
                                <div key={t} className="p-card"><div className="p-card-title">{t}</div><div className="p-card-body">{v}</div></div>
                              ))}
                              {(port.dominant_technology_domains ?? []).length > 0 && (
                                <div className="p-card"><div className="p-card-title">Dominant domains</div>
                                  <div className="tag-list">{port.dominant_technology_domains!.map((d, i) => <span key={i} className="tag t-p">{d}</span>)}</div>
                                </div>
                              )}
                              {(port.cross_specification_patterns ?? []).length > 0 && (
                                <div className="p-card"><div className="p-card-title">Cross-spec patterns</div>
                                  <div className="tag-list">{port.cross_specification_patterns!.map((p, i) => <span key={i} className="tag t-g">{p}</span>)}</div>
                                </div>
                              )}
                            </div>
                          </>
                        )}
                        {!meta.portfolio_characterization && !hasPort && !isMappedCsv && srcWarnings.length === 0 && !isScanned && !countDiag?.significant_gap && !(isTabularSrc && src.used_extraction_model) && (
                          <div className="small muted">No declaration-level metadata was extracted for this file.</div>
                        )}
                      </div>
                    </div>

                    <div className="panel">
                      <div className="panel-header">
                        <div className="ph-left"><div className="panel-dot" style={{ background: 'var(--c-accent)' }} /><h2>Patent disclosures</h2><span className="pill pill-muted num">{discs.length}</span></div>
                        {decl._inference_time_s ? <span className="small muted num">{decl._inference_time_s.toFixed(1)}s inference</span> : null}
                      </div>
                      <div className="table-wrap">
                        <table>
                          <thead>
                            <tr>
                              <th>#</th><th>3GPP standards</th><th>Application no.</th><th>Patent no. (publication)</th>
                              <th>Jurisdiction</th><th>SEP strength</th><th>Predicted SEP</th><th>Title / inferred</th><th></th>
                            </tr>
                          </thead>
                          <tbody>
                            {discs.map((p, i) => {
                              const rid = `${di}-${i}`;
                              const ed = p.explicit_data ?? {};
                              const inf = p.inferred_fields ?? {};
                              const sep = p.sep_analysis ?? {};
                              const rr = decl.research_outputs?.records?.[i];
                              return (
                                <Fragment key={rid}>
                                  <tr>
                                    <td className="td-num">{p.disclosure_number ?? i + 1}</td>
                                    <td>{(ed.standards ?? []).length ? (ed.standards ?? []).map(s => <span key={s} className="spec-chip">{s}</span>) : <span className="muted">—</span>}</td>
                                    <td className="td-appno">{ed.application_number || '—'}</td>
                                    <td className="td-appno">{ed.publication_number || '—'}</td>
                                    <td><CountryBadge country={ed.country} appNumber={ed.application_number} /></td>
                                    <td className="nowrap"><SepStrength strength={sep.likely_sep_strength} /><ConfPill score={sep.confidence} /></td>
                                    <td>{rr ? <span className={`sep-s ${STATUS_CLASS[rr.predicted_sep_status] ?? 's-mu'}`}>{rr.predicted_sep_status}</span> : <span className="muted">—</span>}</td>
                                    <td className="td-title">{ed.title || inf.probable_title || <span className="muted">—</span>}</td>
                                    <td><button className="expand-btn" aria-expanded={!!expanded[rid]} onClick={() => toggleExpand(rid)}>{expanded[rid] ? 'Hide' : 'Details'}</button></td>
                                  </tr>
                                  {expanded[rid] && (
                                    <tr className="detail-row">
                                      <td colSpan={9}><PatentDetailRow disc={p} researchRecord={rr} /></td>
                                    </tr>
                                  )}
                                </Fragment>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    </div>

                    <ResearchSection ro={decl.research_outputs} />
                  </div>
                );
              })}
            </>
          )}
        </div>
      </div>
      )}
    </div>
  );
}
