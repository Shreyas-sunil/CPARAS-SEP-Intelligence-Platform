/**
 * cPARAS PDF report generator.
 *
 * Produces a standalone, multipage PDF that covers everything the HTML
 * report does — and more: the full per-declaration portfolio-analysis
 * narrative (dominant domains, release focus, cross-spec patterns,
 * geographic filing strategy, FRAND/implementation-risk assessment) is
 * surfaced as a structured panel rather than a one-line summary, and each
 * patent gets a second "Technical & Licensing Detail" table pulling in
 * fields (implementation dependency, device/network impact, jurisdiction
 * strength, enforcement significance, regional strategy) that the HTML
 * report doesn't show at all. Research-style figures are already base64
 * PNGs from the backend, so they're embedded directly with jsPDF's
 * getImageProperties — no screenshotting needed.
 */
import jsPDF from 'jspdf';
import autoTable from 'jspdf-autotable';
import { CDOT_LOGO_DATA_URI } from '../assets/cdotLogoBase64';
import type { AnalysisResponse, Declaration, PatentDisclosure, ResearchOutputs, ResearchRecord } from '../types';
import type { TextMatchResponse } from '../services/api';

const PAGE_W = 595.28; // A4 pt
const PAGE_H = 841.89;
const MARGIN = 40;
const CONTENT_W = PAGE_W - MARGIN * 2;
const NAVY: [number, number, number] = [15, 47, 82];
const MUTED: [number, number, number] = [100, 116, 139];
const BLUE: [number, number, number] = [0, 119, 204];
const PURPLE: [number, number, number] = [106, 63, 203];
const GREEN: [number, number, number] = [10, 138, 74];
const AMBER: [number, number, number] = [176, 112, 0];
const RED: [number, number, number] = [204, 26, 53];
const BAR_COLORS: [number, number, number][] = [BLUE, PURPLE, GREEN, AMBER, RED, [139, 92, 246]];

const SAFE_IMAGE_DATA_URI = /^data:image\/(png|jpeg|jpg|webp);base64,[A-Za-z0-9+/=\s]+$/;

function esc(s: unknown): string {
  return String(s ?? '');
}

function fmtPct(n: number | null | undefined): string {
  return n == null ? '—' : `${Math.round(n * 100)}%`;
}

function join(arr: (string | null | undefined)[] | null | undefined, sep = '; '): string {
  return (arr ?? []).filter(Boolean).join(sep) || '—';
}

/** Adds the repeating letterhead (logo + running title) to the current page. */
function drawLetterhead(doc: jsPDF, pageLabel: string) {
  doc.setFillColor(...NAVY);
  doc.rect(0, 0, PAGE_W, 46, 'F');
  try {
    const h = 26;
    const w = h * (249 / 437);
    const pad = 4;
    doc.setFillColor(255, 255, 255);
    doc.roundedRect(MARGIN - pad, 10 - pad, w + pad * 2, h + pad * 2, 3, 3, 'F');
    doc.addImage(CDOT_LOGO_DATA_URI, 'PNG', MARGIN, 10, w, h);
    doc.setTextColor(255, 255, 255);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(10);
    doc.text('CPARAS ISLD — SEP Analysis Report', MARGIN + w + pad * 2 + 12, 27);
  } catch {
    doc.setTextColor(255, 255, 255);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(10);
    doc.text('CPARAS ISLD — SEP Analysis Report', MARGIN, 27);
  }
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(8.5);
  doc.setTextColor(207, 220, 242);
  doc.text(pageLabel, PAGE_W - MARGIN, 27, { align: 'right' });
}

function drawFooter(doc: jsPDF, pageNum: number, pageCount: number) {
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(8);
  doc.setTextColor(...MUTED);
  doc.text('CPARAS ISLD — C-DOT SEP Intelligence Platform · Fully Offline', MARGIN, PAGE_H - 20);
  doc.text(`Page ${pageNum} of ${pageCount}`, PAGE_W - MARGIN, PAGE_H - 20, { align: 'right' });
}

function sectionTitle(doc: jsPDF, title: string, y: number, size = 13) {
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(size);
  doc.setTextColor(...NAVY);
  doc.text(title, MARGIN, y);
  doc.setDrawColor(226, 230, 238);
  doc.setLineWidth(1);
  doc.line(MARGIN, y + 6, PAGE_W - MARGIN, y + 6);
  return y + 24;
}

function subheading(doc: jsPDF, title: string, y: number) {
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(10.5);
  doc.setTextColor(...NAVY);
  doc.text(title, MARGIN, y);
  return y + 15;
}

/** Ensures at least `needed` pt of vertical room remains before the footer; starts a fresh page (with letterhead) if not. */
function ensureRoom(doc: jsPDF, y: number, needed: number, pageLabel: string): number {
  if (y + needed > PAGE_H - 50) {
    doc.addPage();
    drawLetterhead(doc, pageLabel);
    return 80;
  }
  return y;
}

function lastTableY(doc: jsPDF, fallback: number): number {
  // @ts-expect-error jspdf-autotable augments doc with lastAutoTable at runtime
  return (doc.lastAutoTable?.finalY ?? fallback) + 26;
}

/** A label/value fact panel — used for the portfolio-analysis narrative. */
function factPanel(doc: jsPDF, y: number, facts: { label: string; value: string; color?: [number, number, number] }[]): number {
  const rowH = 15;
  facts.forEach((f, i) => {
    const ty = y + i * rowH;
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(8.5);
    doc.setTextColor(...(f.color ?? MUTED));
    doc.text(f.label.toUpperCase(), MARGIN, ty);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9.5);
    doc.setTextColor(30, 41, 59);
    const lines = doc.splitTextToSize(f.value, CONTENT_W - 160);
    doc.text(lines, MARGIN + 155, ty);
  });
  return y + facts.length * rowH + 12;
}

function calloutBox(doc: jsPDF, y: number, label: string, text: string, color: [number, number, number], bg: [number, number, number]): number {
  const lines = doc.splitTextToSize(text, CONTENT_W - 24);
  const h = 22 + lines.length * 12;
  doc.setFillColor(...bg);
  doc.setDrawColor(...color);
  doc.roundedRect(MARGIN, y, CONTENT_W, h, 4, 4, 'FD');
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(8.5);
  doc.setTextColor(...color);
  doc.text(label.toUpperCase(), MARGIN + 10, y + 14);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(9);
  doc.setTextColor(60, 50, 20);
  doc.text(lines, MARGIN + 10, y + 27);
  return y + h + 12;
}

/** Draws a horizontal ranked bar list (mirrors the HTML report's `bars()` helper). */
function drawBarList(doc: jsPDF, items: [string, number][], y: number, maxVal: number): number {
  const rowH = 20;
  const labelW = 130;
  const barX = MARGIN + labelW;
  const barW = CONTENT_W - labelW - 40;
  items.forEach(([label, val], i) => {
    const ty = y + i * rowH;
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    doc.setTextColor(...MUTED);
    const shortLabel = label.length > 26 ? label.slice(0, 25) + '…' : label;
    doc.text(shortLabel, MARGIN + labelW - 6, ty + 9, { align: 'right' });
    const pct = maxVal > 0 ? val / maxVal : 0;
    const w = Math.max(4, barW * pct);
    const color = BAR_COLORS[i % BAR_COLORS.length];
    doc.setFillColor(241, 245, 249);
    doc.roundedRect(barX, ty, barW, 14, 3, 3, 'F');
    doc.setFillColor(...color);
    doc.roundedRect(barX, ty, w, 14, 3, 3, 'F');
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(8);
    doc.setTextColor(255, 255, 255);
    if (w > 18) doc.text(String(val), barX + 6, ty + 10);
    else { doc.setTextColor(30, 41, 59); doc.text(String(val), barX + w + 4, ty + 10); }
  });
  return y + items.length * rowH + 10;
}

function kpiTiles(doc: jsPDF, y: number, tiles: { label: string; value: string; color: [number, number, number] }[], perRow = 4): number {
  const tileW = (CONTENT_W - 10 * (perRow - 1)) / perRow;
  const tileH = 58;
  tiles.forEach((t, i) => {
    const col = i % perRow;
    const row = Math.floor(i / perRow);
    const x = MARGIN + col * (tileW + 10);
    const ty = y + row * (tileH + 10);
    doc.setFillColor(248, 250, 252);
    doc.setDrawColor(226, 230, 238);
    doc.roundedRect(x, ty, tileW, tileH, 5, 5, 'FD');
    doc.setFillColor(...t.color);
    doc.rect(x, ty, 4, tileH, 'F');
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(15);
    doc.setTextColor(...t.color);
    doc.text(t.value, x + 12, ty + 28);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8);
    doc.setTextColor(...MUTED);
    doc.text(t.label, x + 12, ty + 43);
  });
  return y + Math.ceil(tiles.length / perRow) * (tileH + 10) + 14;
}

/**
 * Adds a base64 research figure image, fit to content width, with title +
 * caption. Prefers a `prefH`-tall render but shrinks to whatever room is
 * actually left on the page rather than always claiming the full preferred
 * height — a fixed height meant a figure that didn't quite fit got bumped
 * to a fresh page wholesale, leaving most of the previous page blank. Only
 * forces a new page when the remaining room drops below a usable minimum.
 * Returns new y.
 */
function addFigure(doc: jsPDF, y: number, pageLabel: string, title: string, image: string, caption?: string): number {
  const src = SAFE_IMAGE_DATA_URI.test(image.trim()) ? image.trim() : null;
  if (!src) return y;
  let props: { width: number; height: number };
  try {
    props = doc.getImageProperties(src);
  } catch {
    return y;
  }
  const maxW = CONTENT_W;
  const prefH = 240;
  const minH = 120;
  const captionLines = caption ? doc.splitTextToSize(caption, CONTENT_W) : [];
  const capH = captionLines.length ? captionLines.length * 10 + 6 : 0;
  const fixedH = 18 + 10 + capH + 16; // chrome around the image itself

  let targetH = prefH;
  const room = PAGE_H - 50 - y;
  if (fixedH + prefH > room) {
    const availH = room - fixedH;
    if (availH >= minH) {
      targetH = availH;
    } else {
      doc.addPage();
      drawLetterhead(doc, pageLabel);
      y = 80;
      targetH = prefH;
    }
  }
  const ratio = Math.min(maxW / props.width, targetH / props.height);
  const w = props.width * ratio;
  const h = props.height * ratio;

  doc.setFont('helvetica', 'bold');
  doc.setFontSize(9.5);
  doc.setTextColor(...NAVY);
  doc.text(title, MARGIN, y);
  y += 12;
  doc.setDrawColor(226, 230, 238);
  doc.setFillColor(248, 250, 252);
  doc.roundedRect(MARGIN, y, CONTENT_W, h + 10, 4, 4, 'FD');
  doc.addImage(src, MARGIN + (CONTENT_W - w) / 2, y + 5, w, h);
  y += h + 14;
  if (captionLines.length) {
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8);
    doc.setTextColor(...MUTED);
    doc.text(captionLines, MARGIN, y);
    y += captionLines.length * 10 + 6;
  }
  return y + 8;
}

// ── Research-style analysis section ─────────────────────────────────────────

function researchSection(doc: jsPDF, ro: ResearchOutputs, pageLabel: string): void {
  doc.addPage();
  drawLetterhead(doc, pageLabel);
  let y = 80;
  y = sectionTitle(doc, 'Research-Style Analysis', y);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(8.5);
  doc.setTextColor(...MUTED);
  doc.text(`${esc(ro.meta.embedder_mode)} · corpus ${ro.meta.corpus_size} references · similarity threshold ${Math.round(ro.meta.similarity_threshold * 100)}%`, MARGIN, y);
  y += 14;
  const disclaimerLines = doc.splitTextToSize(esc(ro.meta.disclaimer), CONTENT_W);
  doc.text(disclaimerLines, MARGIN, y);
  y += disclaimerLines.length * 10 + 12;

  const ag = ro.aggregates;
  const m = ro.metrics;
  if (ag) {
    const tiles: { label: string; value: string; color: [number, number, number] }[] = [
      { label: 'Patents', value: String(ag.n_patents ?? 0), color: BLUE },
      { label: 'Avg. Similarity', value: fmtPct(ag.average_similarity), color: PURPLE },
      { label: 'Avg. Confidence', value: fmtPct(ag.average_confidence), color: GREEN },
      { label: 'Potential SEPs', value: String(ag.n_potential_seps ?? 0), color: AMBER },
    ];
    if (m?.available) {
      tiles.push(
        { label: 'Accuracy', value: fmtPct(m.accuracy), color: GREEN },
        { label: 'F1 Score', value: fmtPct(m.f1), color: GREEN },
        { label: "Cohen's κ", value: (m.cohens_kappa ?? 0).toFixed(2), color: PURPLE },
      );
    }
    y = kpiTiles(doc, y, tiles);
    if (ag.status_distribution) {
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(8.5);
      doc.setTextColor(...MUTED);
      const dist = Object.entries(ag.status_distribution).map(([s, n]) => `${n}× ${s}`).join('   |   ');
      doc.text(dist, MARGIN, y);
      y += 18;
    }
  }

  // Patent classification table
  const cls = ro.tables?.patent_classification ?? [];
  if (cls.length) {
    y = ensureRoom(doc, y, 60, pageLabel);
    y = subheading(doc, 'Patent Classification', y);
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Application No.', 'Probability', 'Predicted SEP', 'Recommended Standard', 'Version', 'Confidence']],
      body: cls.map(r => [r.patent, fmtPct(r.probability), r.predicted_sep, r.recommended_standard, r.version, fmtPct(r.confidence)]),
      styles: { fontSize: 7.5, cellPadding: 4 },
      headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [248, 250, 252] },
      theme: 'grid',
      didParseCell: (data) => {
        if (data.section === 'body' && data.column.index === 2) {
          const v = String(data.cell.raw ?? '');
          data.cell.styles.textColor = v === 'Confirmed SEP' ? GREEN : v === 'Likely SEP' ? BLUE : v === 'Potential SEP' ? AMBER : MUTED;
          data.cell.styles.fontStyle = 'bold';
        }
      },
    });
    y = lastTableY(doc, y);
  }

  // Standard mapping table
  const mapping = ro.tables?.standard_mapping ?? [];
  if (mapping.length) {
    y = ensureRoom(doc, y, 60, pageLabel);
    y = subheading(doc, 'Standard Mapping', y);
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Application No.', 'Matched Standard', 'Matched Clause', 'Exact Phrases Matched', 'Similarity', 'Reason']],
      body: mapping.map(r => [
        r.patent, r.matched_standard,
        r.matched_clause_title ? `${r.matched_clause}\n${r.matched_clause_title}` : r.matched_clause,
        r.matched_phrases?.length
          ? r.matched_phrases.map(ph => `\u201c${ph}\u201d`).join('\n')
          : (r.matched_phrases ? 'None (semantic match)' : '\u2014'),
        fmtPct(r.similarity), r.reason,
      ]),
      styles: { fontSize: 7.5, cellPadding: 4, overflow: 'linebreak' },
      columnStyles: { 3: { cellWidth: 130 }, 5: { cellWidth: 140 } },
      headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [248, 250, 252] },
      theme: 'grid',
    });
    y = lastTableY(doc, y);
  }

  // Potential SEPs table
  const potential = ro.tables?.potential_seps ?? [];
  y = ensureRoom(doc, y, 60, pageLabel);
  y = subheading(doc, 'Potential SEPs — Candidates to Validate', y);
  if (potential.length) {
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Application No.', 'Topic', 'Reason', 'Missing Standard Section', 'Predicted Future Version']],
      body: potential.map(r => [r.patent, r.topic, r.reason, r.missing_standard_section, r.predicted_future_version]),
      styles: { fontSize: 7.5, cellPadding: 4, overflow: 'linebreak' },
      columnStyles: { 2: { cellWidth: 160 } },
      headStyles: { fillColor: AMBER, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [255, 251, 235] },
      theme: 'grid',
    });
    y = lastTableY(doc, y);
  } else {
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    doc.setTextColor(...MUTED);
    doc.text('No potential-SEP candidates found — all patents mapped to declared standards.', MARGIN, y);
    y += 20;
  }

  // Verification table
  const verif = ro.tables?.verification ?? [];
  if (verif.length) {
    y = ensureRoom(doc, y, 60, pageLabel);
    y = subheading(doc, 'Version Verification — SEPs in Updated Standards', y);
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Application No.', 'Current Version', 'Future Version', 'Verification', 'Status']],
      body: verif.map(r => [r.patent, r.current_version, r.future_version || '—', r.verification, r.status]),
      styles: { fontSize: 7.5, cellPadding: 4, overflow: 'linebreak' },
      columnStyles: { 3: { cellWidth: 160 } },
      headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [248, 250, 252] },
      theme: 'grid',
      didParseCell: (data) => {
        if (data.section === 'body' && data.column.index === 4) {
          const v = String(data.cell.raw ?? '');
          data.cell.styles.textColor = v.includes('forward') ? GREEN : v === 'Predicted' ? BLUE : v === 'Declining relevance' ? RED : MUTED;
          data.cell.styles.fontStyle = 'bold';
        }
      },
    });
    y = lastTableY(doc, y);
  }

  // Figures — already-rendered base64 PNGs from the backend.
  if (ro.figures) {
    const ORDER = ['patent_similarity_scatter', 'standard_clustering', 'patent_topic_heatmap', 'similarity_matrix',
      'patent_standard_network', 'standards_timeline', 'version_evolution', 'confidence_distribution',
      'standard_frequency', 'version_distribution'];
    const keys = Object.keys(ro.figures).filter(k => ro.figures![k]?.image);
    const sorted = [...ORDER.filter(k => keys.includes(k)), ...keys.filter(k => !ORDER.includes(k))];
    if (sorted.length) {
      y = ensureRoom(doc, y, 40, pageLabel);
      y = subheading(doc, 'Publication-Style Figures', y);
      for (const k of sorted) {
        const fig = ro.figures[k];
        y = addFigure(doc, y, pageLabel, fig.title, fig.image, fig.caption);
      }
    }
  }
}

// ── Main entry point ─────────────────────────────────────────────────────────

export function generateCparasPdfReport(results: AnalysisResponse): void {
  const allDisclosures = results.declarations.flatMap(d => d.patent_disclosures ?? []);

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
  const topN = (obj: Record<string, number>, n: number): [string, number][] =>
    Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, n);
  const tS = topN(specC, 8), tC = topN(countryC, 8), tD = topN(domainC, 8);
  const mS = tS[0]?.[1] || 1, mC = tC[0]?.[1] || 1, mD = tD[0]?.[1] || 1;

  const model = results.declarations[0]?._model || 'Local LLM';
  const generatedAt = new Date();

  const doc = new jsPDF({ unit: 'pt', format: 'a4' });

  // ── Cover page ──────────────────────────────────────────────────────
  doc.setFillColor(...NAVY);
  doc.rect(0, 0, PAGE_W, PAGE_H, 'F');
  try {
    const logoH = 120;
    const logoW = logoH * (249 / 437);
    const pad = 16;
    const cardX = (PAGE_W - logoW) / 2 - pad;
    const cardY = 130 - pad;
    doc.setFillColor(255, 255, 255);
    doc.roundedRect(cardX, cardY, logoW + pad * 2, logoH + pad * 2, 10, 10, 'F');
    doc.addImage(CDOT_LOGO_DATA_URI, 'PNG', (PAGE_W - logoW) / 2, 130, logoW, logoH);
  } catch { /* logo optional */ }
  doc.setTextColor(255, 255, 255);
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(22);
  doc.text('CPARAS ISLD — SEP Analysis Report', PAGE_W / 2, 335, { align: 'center' });
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(11.5);
  doc.setTextColor(207, 220, 242);
  doc.text('CPARAS ISLD — Single declaration deep analysis', PAGE_W / 2, 372, { align: 'center' });

  doc.setDrawColor(79, 142, 247);
  doc.setLineWidth(1);
  doc.line(PAGE_W / 2 - 60, 388, PAGE_W / 2 + 60, 388);

  doc.setFontSize(10.5);
  doc.text(`${allDisclosures.length} patents across ${results.declarations.length} declaration${results.declarations.length === 1 ? '' : 's'}`, PAGE_W / 2, 415, { align: 'center' });
  doc.text(`Model: ${esc(model)} · Fully offline, no cloud APIs`, PAGE_W / 2, 432, { align: 'center' });
  doc.text(`Generated ${generatedAt.toLocaleString()}`, PAGE_W / 2, 449, { align: 'center' });
  if (results.errors?.length) {
    doc.setTextColor(252, 165, 165);
    doc.text(`${results.errors.length} file(s) failed to process — see appendix`, PAGE_W / 2, 470, { align: 'center' });
  }

  doc.setFontSize(9);
  doc.setTextColor(140, 160, 195);
  doc.text('Centre for Development of Telematics — Offline SEP Intelligence Platform', PAGE_W / 2, PAGE_H - 40, { align: 'center' });

  // ── Executive summary ────────────────────────────────────────────────
  doc.addPage();
  drawLetterhead(doc, 'Executive Summary');
  let y = 80;
  y = sectionTitle(doc, 'Executive Summary', y);
  y = kpiTiles(doc, y, [
    { label: 'Total Patents', value: String(allDisclosures.length), color: BLUE },
    { label: 'Declarations', value: String(results.declarations.length), color: PURPLE },
    { label: 'Specifications', value: String(Object.keys(specC).length), color: GREEN },
    { label: 'Jurisdictions', value: String(Object.keys(countryC).length), color: AMBER },
  ]);

  if (tS.length) {
    y = ensureRoom(doc, y, 30 + tS.length * 20, 'Executive Summary');
    y = subheading(doc, 'Top 3GPP Specifications', y);
    y = drawBarList(doc, tS, y, mS);
  }
  if (tD.length) {
    y = ensureRoom(doc, y, 30 + tD.length * 20, 'Executive Summary');
    y = subheading(doc, 'Top Technical Domains', y);
    y = drawBarList(doc, tD, y, mD);
  }
  if (tC.length) {
    y = ensureRoom(doc, y, 30 + tC.length * 20, 'Executive Summary');
    y = subheading(doc, 'Filing Jurisdictions', y);
    y = drawBarList(doc, tC, y, mC);
  }

  // ── Per-declaration sections ──────────────────────────────────────────
  results.declarations.forEach((decl: Declaration, declIdx: number) => {
    const meta = decl.declaration_metadata ?? ({} as Declaration['declaration_metadata']);
    const port = decl.portfolio_level_analysis ?? ({} as Declaration['portfolio_level_analysis']);
    const disclosures = decl.patent_disclosures ?? [];
    const pageLabel = `Declaration ${declIdx + 1} of ${results.declarations.length}`;

    doc.addPage();
    drawLetterhead(doc, pageLabel);
    y = 80;

    doc.setFont('helvetica', 'bold');
    doc.setFontSize(16);
    doc.setTextColor(...NAVY);
    doc.text(esc(meta.declarant || decl.filename), MARGIN, y);
    y += 18;
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9);
    doc.setTextColor(...MUTED);
    doc.text(`${esc(meta.likely_standard_generation || '—')} · ${esc(meta.filing_type || '—')} · ${disclosures.length} patents · confidence ${fmtPct(meta.confidence)}`, MARGIN, y);
    y += 20;

    if (meta.portfolio_characterization) {
      const lines = doc.splitTextToSize(esc(meta.portfolio_characterization), CONTENT_W);
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(9.5);
      doc.setTextColor(51, 65, 85);
      doc.text(lines, MARGIN, y);
      y += lines.length * 12 + 12;
    }

    // Portfolio-level analysis — full narrative, not shown at all in the
    // HTML report beyond the one-line characterization + FRAND callout.
    y = ensureRoom(doc, y, 140, pageLabel);
    y = subheading(doc, 'Portfolio-Level Analysis', y);
    y = factPanel(doc, y, [
      { label: 'Dominant Domains', value: join(port.dominant_technology_domains) },
      { label: 'Release Focus', value: join(port.release_focus) },
      { label: 'Most-Targeted Specs', value: join(port.most_targeted_specs) },
      { label: 'Cross-Spec Patterns', value: join(port.cross_specification_patterns) },
      { label: 'Geographic Strategy', value: esc(port.geographic_filing_strategy) || '—' },
      { label: 'Portfolio Strength', value: esc(port.portfolio_strength_assessment) || '—' },
    ]);
    if (port.frand_implications) {
      y = ensureRoom(doc, y, 60, pageLabel);
      y = calloutBox(doc, y, 'FRAND Implications', esc(port.frand_implications), AMBER, [255, 251, 235]);
    }
    if (port.implementation_risk_summary) {
      y = ensureRoom(doc, y, 60, pageLabel);
      y = calloutBox(doc, y, 'Implementation Risk', esc(port.implementation_risk_summary), RED, [254, 242, 242]);
    }

    // Main disclosures table
    if (disclosures.length) {
      y = ensureRoom(doc, y, 70, pageLabel);
      y = subheading(doc, 'Patent Disclosures', y);
      autoTable(doc, {
        startY: y,
        margin: { left: MARGIN, right: MARGIN },
        head: [['#', 'Standards', 'App No.', 'Pub No.', 'Country', 'SEP Strength', 'Predicted SEP', 'Std + Version', 'Title']],
        body: disclosures.map((p: PatentDisclosure, i: number) => {
          const rr: ResearchRecord | undefined = decl.research_outputs?.records?.[i];
          return [
            String(p.disclosure_number ?? i + 1),
            join(p.explicit_data?.standards, ', '),
            esc(p.explicit_data?.application_number) || '—',
            esc(p.explicit_data?.publication_number) || '—',
            esc(p.explicit_data?.country) || '—',
            esc(p.sep_analysis?.likely_sep_strength) || '—',
            rr?.predicted_sep_status ?? '—',
            rr ? `${rr.recommended_standard} ${rr.recommended_version}` : '—',
            esc(p.explicit_data?.title || p.inferred_fields?.probable_title) || '—',
          ];
        }),
        styles: { fontSize: 7, cellPadding: 3.5, overflow: 'linebreak' },
        columnStyles: {
          0: { cellWidth: 16 },
          1: { cellWidth: 55 },
          2: { cellWidth: 55 },
          3: { cellWidth: 55 },
          4: { cellWidth: 35 },
          5: { cellWidth: 50 },
          6: { cellWidth: 50 },
          7: { cellWidth: 60 },
          8: { cellWidth: 90 },
        },
        headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold', fontSize: 7 },
        alternateRowStyles: { fillColor: [248, 250, 252] },
        theme: 'grid',
        didParseCell: (data) => {
          if (data.section === 'body' && data.column.index === 6) {
            const v = String(data.cell.raw ?? '');
            data.cell.styles.textColor = v === 'Confirmed SEP' ? GREEN : v === 'Likely SEP' ? BLUE : v === 'Potential SEP' ? AMBER : MUTED;
            data.cell.styles.fontStyle = 'bold';
          }
          if (data.section === 'body' && data.column.index === 5) {
            const v = String(data.cell.raw ?? '').toLowerCase();
            data.cell.styles.textColor = v.includes('high') ? GREEN : v.includes('low') ? RED : AMBER;
            data.cell.styles.fontStyle = 'bold';
          }
        },
      });
      y = lastTableY(doc, y);

      // Technical & Licensing Detail — fields not shown anywhere in the
      // HTML report at all (implementation dependency, device/network
      // impact, jurisdiction strength, enforcement significance, regional
      // filing strategy).
      y = ensureRoom(doc, y, 70, pageLabel);
      y = subheading(doc, 'Technical & Licensing Detail', y);
      autoTable(doc, {
        startY: y,
        margin: { left: MARGIN, right: MARGIN },
        head: [['#', 'Implementation Scope', 'Dependency', 'Device Impact', 'Network Impact', 'Licensing Relevance', 'Jurisdiction', 'Enforcement', 'Regional Strategy']],
        body: disclosures.map((p: PatentDisclosure, i: number) => [
          String(p.disclosure_number ?? i + 1),
          esc(p.sep_analysis?.probable_implementation_scope) || '—',
          esc(p.sep_analysis?.implementation_dependency) || '—',
          join(p.sep_analysis?.probable_device_impact, ', '),
          join(p.sep_analysis?.probable_network_impact, ', '),
          esc(p.sep_analysis?.estimated_licensing_relevance) || '—',
          esc(p.geographic_analysis?.jurisdiction_strength) || '—',
          esc(p.geographic_analysis?.enforcement_significance) || '—',
          esc(p.geographic_analysis?.regional_strategy_inference) || '—',
        ]),
        styles: { fontSize: 6.5, cellPadding: 3, overflow: 'linebreak' },
        columnStyles: {
          0: { cellWidth: 14 },
          1: { cellWidth: 58 },
          2: { cellWidth: 50 },
          3: { cellWidth: 55 },
          4: { cellWidth: 55 },
          5: { cellWidth: 55 },
          6: { cellWidth: 45 },
          7: { cellWidth: 50 },
          8: { cellWidth: 55 },
        },
        headStyles: { fillColor: [71, 85, 105], textColor: 255, fontStyle: 'bold', fontSize: 6.5 },
        alternateRowStyles: { fillColor: [248, 250, 252] },
        theme: 'grid',
      });
      y = lastTableY(doc, y);
    }

    // Research-style analysis — own page(s) per declaration.
    if (decl.research_outputs) {
      researchSection(doc, decl.research_outputs, `${pageLabel} — Research`);
    }
  });

  // ── Failed files appendix ────────────────────────────────────────────
  if (results.errors?.length) {
    doc.addPage();
    drawLetterhead(doc, 'Processing Errors');
    y = 80;
    y = sectionTitle(doc, 'Files That Failed to Process', y);
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Filename', 'Error']],
      body: results.errors.map(e => [e.filename, e.error]),
      styles: { fontSize: 8, cellPadding: 5, overflow: 'linebreak' },
      columnStyles: { 1: { cellWidth: 380 } },
      headStyles: { fillColor: RED, textColor: 255, fontStyle: 'bold' },
      alternateRowStyles: { fillColor: [254, 242, 242] },
      theme: 'grid',
    });
  }

  // ── Footer + page numbers on every page except the cover ───────────
  const pageCount = doc.getNumberOfPages();
  for (let p = 2; p <= pageCount; p++) {
    doc.setPage(p);
    drawFooter(doc, p - 1, pageCount - 1);
  }

  const filename = `CPARAS_ISLD_SEP_Analysis_Report_${generatedAt.toISOString().slice(0, 10)}.pdf`;
  doc.save(filename);
}

// ── Text Matching PDF report ─────────────────────────────────────────────────

export interface TextMatchReportSourceRecord {
  id?: string;
  title?: string;
  standard?: string;
  company?: string;
  dipgId?: string;
  project?: string;
}

/** Inputs for the Text Matching PDF report. */
export interface TextMatchReportOptions {
  /** The pasted query text (paste mode). Omitted for file uploads. */
  queryText?: string;
  /** CPARAS Bulk record this query was launched from, if any — see App.tsx's sourceRecord. */
  sourceRecord?: TextMatchReportSourceRecord | null;
}

/**
 * Produces a standalone PDF for a single Text Matching run: the query
 * (pasted text or uploaded file + its extraction preview), the corpus
 * context it was matched against, and every segment's ranked matches —
 * mirroring what's shown on screen (App.tsx's Text Matching tab) so the
 * report is a faithful record of that result, not a re-derived summary.
 */
export function generateTextMatchPdfReport(result: TextMatchResponse, opts: TextMatchReportOptions = {}): void {
  const generatedAt = new Date();
  const totalMatches = result.segments.reduce((n, s) => n + s.matches.length, 0);

  const doc = new jsPDF({ unit: 'pt', format: 'a4' });

  // ── Cover page ──────────────────────────────────────────────────────
  doc.setFillColor(...NAVY);
  doc.rect(0, 0, PAGE_W, PAGE_H, 'F');
  try {
    const logoH = 120;
    const logoW = logoH * (249 / 437);
    const pad = 16;
    const cardX = (PAGE_W - logoW) / 2 - pad;
    const cardY = 130 - pad;
    doc.setFillColor(255, 255, 255);
    doc.roundedRect(cardX, cardY, logoW + pad * 2, logoH + pad * 2, 10, 10, 'F');
    doc.addImage(CDOT_LOGO_DATA_URI, 'PNG', (PAGE_W - logoW) / 2, 130, logoW, logoH);
  } catch { /* logo optional */ }
  doc.setTextColor(255, 255, 255);
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(22);
  doc.text('CPARAS ISLD — Text Matching Report', PAGE_W / 2, 335, { align: 'center' });
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(11.5);
  doc.setTextColor(207, 220, 242);
  doc.text('CPARAS ISLD — Single declaration deep analysis', PAGE_W / 2, 372, { align: 'center' });

  doc.setDrawColor(79, 142, 247);
  doc.setLineWidth(1);
  doc.line(PAGE_W / 2 - 60, 388, PAGE_W / 2 + 60, 388);

  doc.setFontSize(10.5);
  doc.text(`${result.segments.length} segment${result.segments.length === 1 ? '' : 's'} · ${totalMatches} matched clause${totalMatches === 1 ? '' : 's'}`, PAGE_W / 2, 415, { align: 'center' });
  doc.text(`Corpus: ${result.corpus_indexed_chunks.toLocaleString()} indexed chunks · Fully offline, no cloud APIs`, PAGE_W / 2, 432, { align: 'center' });
  doc.text(`Generated ${generatedAt.toLocaleString()}`, PAGE_W / 2, 449, { align: 'center' });
  if (result.warning) {
    doc.setTextColor(252, 165, 165);
    const wLines = doc.splitTextToSize(result.warning, PAGE_W - 140);
    doc.text(wLines, PAGE_W / 2, 470, { align: 'center' });
  }

  doc.setFontSize(9);
  doc.setTextColor(140, 160, 195);
  doc.text('Centre for Development of Telematics — Offline SEP Intelligence Platform', PAGE_W / 2, PAGE_H - 40, { align: 'center' });

  // ── Query context ─────────────────────────────────────────────────────
  doc.addPage();
  drawLetterhead(doc, 'Query');
  let y = 80;
  y = sectionTitle(doc, 'Query', y);

  const sr = opts.sourceRecord;
  if (sr && (sr.title || sr.standard || sr.company || sr.project || sr.dipgId)) {
    y = subheading(doc, 'Source Record (reference context — not the matching input)', y);
    y = factPanel(doc, y, [
      sr.title && { label: 'Title', value: esc(sr.title) },
      sr.standard && { label: 'Standard', value: esc(sr.standard) },
      sr.company && { label: 'Company', value: esc(sr.company) },
      sr.project && { label: 'Project', value: esc(sr.project) },
      sr.dipgId && { label: 'DIPG_ID', value: esc(sr.dipgId) },
    ].filter(Boolean) as { label: string; value: string }[]);
  }

  if (result.source_filename) {
    y = subheading(doc, 'Uploaded File', y);
    y = factPanel(doc, y, [
      { label: 'Filename', value: esc(result.source_filename) },
      result.file_format ? { label: 'Format', value: esc(result.file_format).toUpperCase() } : null,
      result.text_source ? { label: 'Text Source', value: esc(result.text_source) } : null,
      result.extraction_method ? { label: 'Extraction Method', value: esc(result.extraction_method) } : null,
      typeof result.records_extracted === 'number' ? { label: 'Records Extracted', value: String(result.records_extracted) } : null,
    ].filter(Boolean) as { label: string; value: string }[]);
    if (result.extraction_preview) {
      y = subheading(doc, 'Extraction Preview', y);
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(8.5);
      doc.setTextColor(60, 70, 85);
      const previewLines = doc.splitTextToSize(esc(result.extraction_preview), CONTENT_W);
      y = ensureRoom(doc, y, previewLines.length * 10 + 10, 'Query');
      doc.text(previewLines, MARGIN, y);
      y += previewLines.length * 10 + 14;
    }
  } else if (opts.queryText) {
    y = subheading(doc, 'Pasted Text', y);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9);
    doc.setTextColor(30, 41, 59);
    const qLines = doc.splitTextToSize(esc(opts.queryText), CONTENT_W);
    y = ensureRoom(doc, y, qLines.length * 11 + 10, 'Query');
    doc.text(qLines, MARGIN, y);
    y += qLines.length * 11 + 14;
  }

  if (result.low_confidence_pages?.length) {
    y = ensureRoom(doc, y, 40, 'Query');
    y = calloutBox(
      doc, y, 'Low-Confidence Extraction',
      `Page(s) ${result.low_confidence_pages.join(', ')} had low-confidence text extraction — verify against the source document.`,
      AMBER, [255, 247, 230],
    );
  }

  // ── Segments & matches ───────────────────────────────────────────────
  result.segments.forEach((seg, i) => {
    y = ensureRoom(doc, y, 60, 'Matches');
    y = subheading(doc, seg.label || `Segment ${i + 1}`, y);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    doc.setTextColor(...MUTED);
    const segLines = doc.splitTextToSize(esc(seg.segment), CONTENT_W);
    y = ensureRoom(doc, y, segLines.length * 10 + 10, 'Matches');
    doc.text(segLines, MARGIN, y);
    y += segLines.length * 10 + 10;

    if (seg.matches.length === 0) {
      doc.setFont('helvetica', 'italic');
      doc.setFontSize(9);
      doc.setTextColor(...MUTED);
      doc.text('No matches above threshold.', MARGIN, y);
      y += 20;
      return;
    }

    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Score', 'Spec', 'Clause', 'Content']],
      body: seg.matches.map(m => [
        `${Math.round(m.score * 100)}%`,
        esc(m.metadata?.spec ?? '—'),
        esc(m.metadata?.clause ?? '—'),
        esc(m.content),
      ]),
      styles: { fontSize: 8, cellPadding: 5, overflow: 'linebreak' },
      columnStyles: { 0: { cellWidth: 45 }, 1: { cellWidth: 70 }, 2: { cellWidth: 70 }, 3: { cellWidth: CONTENT_W - 185 } },
      headStyles: { fillColor: BLUE, textColor: 255, fontStyle: 'bold' },
      alternateRowStyles: { fillColor: [245, 249, 255] },
      theme: 'grid',
      didParseCell: (data) => {
        if (data.section === 'body' && data.column.index === 0) {
          const score = seg.matches[data.row.index]?.score ?? 0;
          data.cell.styles.textColor = score >= 0.6 ? GREEN : score >= 0.4 ? AMBER : MUTED;
          data.cell.styles.fontStyle = 'bold';
        }
      },
    });
    y = lastTableY(doc, y);
  });

  // ── Footer + page numbers on every page except the cover ───────────
  const pageCount = doc.getNumberOfPages();
  for (let p = 2; p <= pageCount; p++) {
    doc.setPage(p);
    drawFooter(doc, p - 1, pageCount - 1);
  }

  const filename = `CPARAS_ISLD_Text_Matching_Report_${generatedAt.toISOString().slice(0, 10)}.pdf`;
  doc.save(filename);
}
