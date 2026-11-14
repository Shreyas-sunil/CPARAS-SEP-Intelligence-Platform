/**
 * CPARAS Bulk dashboard PDF report generator.
 *
 * Produces a standalone, multipage PDF covering everything the Dashboard
 * page can show: KPI summary, the six charts (rendered as raster snapshots
 * via html2canvas so the output matches what's on screen pixel-for-pixel),
 * and the full underlying data as paginated tables (company portfolio,
 * release/domain heatmap, and the complete patent registry — not just the
 * page of rows visible in the UI).
 *
 * Charts are captured from a set of DOM refs that the caller keeps mounted
 * (see DashboardPage's off-screen "pdf capture" block) so the report always
 * contains every chart regardless of which tab the user currently has open.
 */
import jsPDF from 'jspdf';
import autoTable from 'jspdf-autotable';
import html2canvas from 'html2canvas';
import { CDOT_LOGO_DATA_URI } from '../assets/cdotLogoBase64';
import type { Patent } from '../api';

const PAGE_W = 595.28; // A4 pt
const PAGE_H = 841.89;
const MARGIN = 40;
const CONTENT_W = PAGE_W - MARGIN * 2;
const NAVY: [number, number, number] = [15, 47, 82];
const MUTED: [number, number, number] = [100, 116, 139];
const ESS_GREEN: [number, number, number] = [18, 153, 106];
const ESS_AMBER: [number, number, number] = [178, 116, 15];
const ESS_RED: [number, number, number] = [208, 51, 47];
const BLUE: [number, number, number] = [37, 99, 235];
const PURPLE: [number, number, number] = [124, 58, 237];

export interface ReportOverview {
  total_seps: number;
  essential: number;
  potential: number;
  non_essential: number;
  top_release?: string | null;
  specs_covered: number;
  avg_essentiality_score: number;
}

export interface ReportPortfolioRow {
  company: string;
  country?: string;
  total: number;
  essential: number;
  potential: number;
  non_essential: number;
}

export interface ReportByRelease {
  release: string;
  count: number;
  Essential: number;
  Potential: number;
  'Non-Essential': number;
}

export interface ReportCountry {
  country: string;
  count: number;
  pct: number;
}

export interface ReportHeatmap {
  releases: string[];
  domains: string[];
  cells: { domain: string; release: string; value: number }[];
}

export interface ChartRefs {
  company?: HTMLElement | null;
  timeline?: HTMLElement | null;
  country?: HTMLElement | null;
  release?: HTMLElement | null;
  essentiality?: HTMLElement | null;
  heatmap?: HTMLElement | null;
}

/** Slim shape of the /entity/company/{name} response, used for the
 * company deep-dive appendix. */
export interface CompanyProfileLite {
  company: string;
  country?: string | null;
  total_seps: number;
  essential: number;
  potential: number;
  non_essential: number;
  by_release?: Record<string, number>;
  by_domain?: Record<string, number>;
}

export interface GenerateReportParams {
  overview: ReportOverview;
  portfolio: ReportPortfolioRow[];
  byRelease: ReportByRelease[];
  countries: ReportCountry[];
  heatmap: ReportHeatmap;
  patents: Patent[];
  patentsTotal: number;
  runLabel: string;
  filtersSummary: string;
  chartRefs: ChartRefs;
  companyProfiles?: CompanyProfileLite[];
  onProgress?: (label: string) => void;
}

function fmt(n: number | null | undefined) {
  return (n ?? 0).toLocaleString();
}

/** Adds the repeating letterhead (logo + running title) to the current page. */
function drawLetterhead(doc: jsPDF, pageLabel: string) {
  doc.setFillColor(...NAVY);
  doc.rect(0, 0, PAGE_W, 46, 'F');
  try {
    // Logo aspect ratio is 249:437 (w:h) — keep it intact at a small nav-bar size.
    // The logo art itself is navy-on-transparent, so it needs a light card
    // behind it to stay legible against the navy letterhead bar.
    const h = 26;
    const w = h * (249 / 437);
    const pad = 4;
    doc.setFillColor(255, 255, 255);
    doc.roundedRect(MARGIN - pad, 10 - pad, w + pad * 2, h + pad * 2, 3, 3, 'F');
    doc.addImage(CDOT_LOGO_DATA_URI, 'PNG', MARGIN, 10, w, h);
    doc.setTextColor(255, 255, 255);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(11);
    doc.text('CPARAS Bulk — SEP Portfolio Report', MARGIN + w + pad * 2 + 12, 27);
  } catch {
    doc.setTextColor(255, 255, 255);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(11);
    doc.text('CPARAS Bulk — SEP Portfolio Report', MARGIN, 27);
  }
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(9);
  doc.setTextColor(207, 220, 242);
  doc.text(pageLabel, PAGE_W - MARGIN, 27, { align: 'right' });
}

function drawFooter(doc: jsPDF, pageNum: number, pageCount: number) {
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(8);
  doc.setTextColor(...MUTED);
  doc.text('Centre for Development of Telematics — CPARAS SEP Intelligence Platform', MARGIN, PAGE_H - 20);
  doc.text(`Page ${pageNum} of ${pageCount}`, PAGE_W - MARGIN, PAGE_H - 20, { align: 'right' });
}

function sectionTitle(doc: jsPDF, title: string, y: number) {
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(13);
  doc.setTextColor(...NAVY);
  doc.text(title, MARGIN, y);
  doc.setDrawColor(226, 230, 238);
  doc.setLineWidth(1);
  doc.line(MARGIN, y + 6, PAGE_W - MARGIN, y + 6);
  return y + 26;
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

function subheading(doc: jsPDF, title: string, y: number) {
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(10.5);
  doc.setTextColor(...NAVY);
  doc.text(title, MARGIN, y);
  return y + 15;
}

function lastTableY(doc: jsPDF, fallback: number): number {
  // @ts-expect-error jspdf-autotable augments doc with lastAutoTable at runtime
  return (doc.lastAutoTable?.finalY ?? fallback) + 26;
}

/** Renders one chart ref to a PNG data URL sized to fit the content width. */
async function snapshotChart(el: HTMLElement | null | undefined): Promise<{ dataUrl: string; w: number; h: number } | null> {
  if (!el) return null;
  // Capture the element's actual rendered content box (scrollWidth/Height)
  // rather than trusting a caller-supplied container width. Content like the
  // heatmap table can legitimately render wider than any width estimate we
  // pass in (e.g. a long release label such as "Legacy (R99/Rel-4 era)"
  // pushes columns past a fixed-width guess) — if html2canvas only looks at
  // the element's own box, anything that overflows it gets silently
  // clipped out of the snapshot. Passing explicit width/height + matching
  // windowWidth/windowHeight forces html2canvas to render the full content,
  // so nothing is cut off regardless of how wide the table ends up being.
  const w = Math.ceil(el.scrollWidth);
  const h = Math.ceil(el.scrollHeight);
  const canvas = await html2canvas(el, {
    backgroundColor: '#ffffff',
    scale: 2,
    logging: false,
    useCORS: true,
    width: w,
    height: h,
    windowWidth: w,
    windowHeight: h,
  });
  const dataUrl = canvas.toDataURL('image/png');
  return { dataUrl, w: canvas.width, h: canvas.height };
}

function addChartToPage(doc: jsPDF, snap: { dataUrl: string; w: number; h: number }, x: number, y: number, maxW: number, maxH: number) {
  const ratio = Math.min(maxW / snap.w, maxH / snap.h);
  const w = snap.w * ratio;
  const h = snap.h * ratio;
  doc.addImage(snap.dataUrl, 'PNG', x + (maxW - w) / 2, y, w, h);
  return h;
}

/**
 * Places a chart, preferring `prefH` tall but shrinking to fit whatever
 * room remains on the current page rather than always jumping to a fresh
 * page. A fixed preferred height meant charts either got their own mostly
 * empty page, or forced an unnecessary page break when only a little more
 * room was needed — this adapts to what's actually left before deciding
 * a new page is warranted (i.e. remaining room is below `minH`).
 */
function placeChart(doc: jsPDF, y: number, pageLabel: string, title: string, snap: { dataUrl: string; w: number; h: number } | null, prefH: number): number {
  if (!snap) return y;
  const minH = 130;
  const TITLE_H = 26;
  const GAP = 20;
  const room = PAGE_H - 50 - y;
  let targetH = prefH;
  if (TITLE_H + prefH + GAP > room) {
    const availH = room - TITLE_H - GAP;
    if (availH >= minH) {
      targetH = availH;
    } else {
      doc.addPage();
      drawLetterhead(doc, pageLabel);
      y = 80;
      targetH = prefH;
    }
  }
  y = sectionTitle(doc, title, y);
  const ratio = Math.min(CONTENT_W / snap.w, targetH / snap.h);
  const w = snap.w * ratio;
  const h = snap.h * ratio;
  doc.addImage(snap.dataUrl, 'PNG', MARGIN + (CONTENT_W - w) / 2, y, w, h);
  return y + h + GAP;
}

/**
 * Build and download the CPARAS Bulk portfolio PDF (overview, charts captured from the dashboard,
 * portfolio and release tables, top declarant profiles, capped patent list) for the active filters.
 */
export async function generateGitasReport(params: GenerateReportParams): Promise<void> {
  const { overview, portfolio, byRelease, countries, heatmap, patents, patentsTotal, runLabel, filtersSummary, chartRefs, companyProfiles, onProgress } = params;
  const progress = (l: string) => onProgress?.(l);

  const doc = new jsPDF({ unit: 'pt', format: 'a4' });
  const generatedAt = new Date();

  // ── Cover page ──────────────────────────────────────────────────────
  progress('Building cover page…');
  doc.setFillColor(...NAVY);
  doc.rect(0, 0, PAGE_W, PAGE_H, 'F');
  try {
    // Logo art is navy-on-transparent, so give it a white card to sit on
    // top of the navy cover background — otherwise it's nearly invisible.
    const logoH = 130;
    const logoW = logoH * (249 / 437);
    const pad = 18;
    const cardX = (PAGE_W - logoW) / 2 - pad;
    const cardY = 150 - pad;
    doc.setFillColor(255, 255, 255);
    doc.roundedRect(cardX, cardY, logoW + pad * 2, logoH + pad * 2, 10, 10, 'F');
    doc.addImage(CDOT_LOGO_DATA_URI, 'PNG', (PAGE_W - logoW) / 2, 150, logoW, logoH);
  } catch {
    /* logo optional if it fails to decode */
  }
  doc.setTextColor(255, 255, 255);
  doc.setFont('helvetica', 'bold');
  doc.setFontSize(26);
  doc.text('CPARAS Bulk', PAGE_W / 2, 330, { align: 'center' });
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(12.5);
  doc.setTextColor(207, 220, 242);
  doc.text('Bulk & portfolio declaration analysis', PAGE_W / 2, 352, { align: 'center' });
  doc.setFontSize(16);
  doc.setTextColor(255, 255, 255);
  doc.setFont('helvetica', 'bold');
  doc.text('SEP Portfolio Report', PAGE_W / 2, 400, { align: 'center' });

  doc.setDrawColor(79, 142, 247);
  doc.setLineWidth(1);
  doc.line(PAGE_W / 2 - 60, 415, PAGE_W / 2 + 60, 415);

  doc.setFont('helvetica', 'normal');
  doc.setFontSize(10.5);
  doc.setTextColor(207, 220, 242);
  doc.text(`Data source: ${runLabel}`, PAGE_W / 2, 445, { align: 'center' });
  doc.text(`Filters: ${filtersSummary}`, PAGE_W / 2, 462, { align: 'center' });
  doc.text(`Generated ${generatedAt.toLocaleString()}`, PAGE_W / 2, 479, { align: 'center' });

  doc.setFontSize(9);
  doc.setTextColor(140, 160, 195);
  doc.text('Centre for Development of Telematics — Offline SEP Intelligence Platform', PAGE_W / 2, PAGE_H - 40, { align: 'center' });

  // ── KPI summary page ────────────────────────────────────────────────
  progress('Summarizing KPIs…');
  doc.addPage();
  drawLetterhead(doc, 'Portfolio Summary');
  let y = 80;
  y = sectionTitle(doc, 'Key Metrics', y);

  const kpis: { label: string; value: string; color: [number, number, number] }[] = [
    { label: 'Total SEPs', value: fmt(overview.total_seps), color: BLUE },
    { label: 'Essential', value: fmt(overview.essential), color: ESS_GREEN },
    { label: 'Potential SEP', value: fmt(overview.potential), color: ESS_AMBER },
    { label: 'Non-Essential', value: fmt(overview.non_essential), color: ESS_RED },
    { label: 'Avg. Essentiality Score', value: `${overview.avg_essentiality_score}%`, color: ESS_GREEN },
    { label: 'Top Release', value: overview.top_release || '—', color: [124, 58, 237] },
    { label: 'Specs Covered', value: fmt(overview.specs_covered), color: ESS_AMBER },
  ];
  const tileW = (CONTENT_W - 12 * 2) / 3;
  const tileH = 64;
  kpis.forEach((k, i) => {
    const col = i % 3;
    const row = Math.floor(i / 3);
    const x = MARGIN + col * (tileW + 12);
    const ty = y + row * (tileH + 12);
    doc.setFillColor(248, 250, 252);
    doc.setDrawColor(226, 230, 238);
    doc.roundedRect(x, ty, tileW, tileH, 5, 5, 'FD');
    doc.setFillColor(...k.color);
    doc.rect(x, ty, 4, tileH, 'F');
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(17);
    doc.setTextColor(...k.color);
    doc.text(k.value, x + 14, ty + 32);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9);
    doc.setTextColor(...MUTED);
    doc.text(k.label, x + 14, ty + 48);
  });
  y = y + Math.ceil(kpis.length / 3) * (tileH + 12) + 20;

  y = sectionTitle(doc, 'Portfolio Overview', y);
  autoTable(doc, {
    startY: y,
    margin: { top: 80, left: MARGIN, right: MARGIN },
    didDrawPage: () => drawLetterhead(doc, 'Portfolio Summary'),
    head: [['Company', 'Country', 'Total SEPs', 'Essential', 'Potential', 'Non-Essential']],
    body: portfolio.map(r => [r.company, r.country || '—', fmt(r.total), fmt(r.essential), fmt(r.potential), fmt(r.non_essential)]),
    styles: { fontSize: 8.5, cellPadding: 5 },
    headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold' },
    alternateRowStyles: { fillColor: [248, 250, 252] },
    theme: 'grid',
  });
  y = lastTableY(doc, y);

  // ── Executive insights ───────────────────────────────────────────────
  // Computed narrative — concentration, essentiality rate, most-active
  // release/domain, filing trend — none of which is spelled out anywhere
  // in the on-screen dashboard, so this is genuinely new analysis rather
  // than a re-statement of the KPI tiles above. Flows onto the current
  // page if there's room rather than always starting fresh.
  progress('Building executive insights…');
  y = ensureRoom(doc, y, 40, 'Executive Insights');
  y = sectionTitle(doc, 'Executive Insights', y);

  const essentialityRate = overview.total_seps > 0 ? overview.essential / overview.total_seps : 0;
  const sortedPortfolio = [...portfolio].sort((a, b) => b.total - a.total);
  const topCompany = sortedPortfolio[0];
  const top3Total = sortedPortfolio.slice(0, 3).reduce((s, r) => s + r.total, 0);
  const topCompanyShare = topCompany && overview.total_seps > 0 ? topCompany.total / overview.total_seps : 0;
  const top3Share = overview.total_seps > 0 ? top3Total / overview.total_seps : 0;

  const releaseTotals = new Map<string, number>();
  const domainTotals = new Map<string, number>();
  heatmap.cells.forEach(c => {
    releaseTotals.set(c.release, (releaseTotals.get(c.release) ?? 0) + c.value);
    domainTotals.set(c.domain, (domainTotals.get(c.domain) ?? 0) + c.value);
  });
  const topReleaseByVolume = [...releaseTotals.entries()].sort((a, b) => b[1] - a[1])[0];
  const topDomainByVolume = [...domainTotals.entries()].sort((a, b) => b[1] - a[1])[0];

  const sortedCountries = [...countries].sort((a, b) => b.count - a.count);
  const topCountry = sortedCountries[0];

  const insightCards: { label: string; value: string; note: string; color: [number, number, number] }[] = [
    {
      label: 'Essentiality Rate',
      value: `${Math.round(essentialityRate * 100)}%`,
      note: `${fmt(overview.essential)} of ${fmt(overview.total_seps)} declared SEPs are classified Essential — the remainder split across Potential and Non-Essential.`,
      color: ESS_GREEN,
    },
    {
      label: 'Portfolio Concentration',
      value: topCompany ? `${Math.round(topCompanyShare * 100)}%` : '—',
      note: topCompany
        ? `${topCompany.company} alone accounts for ${Math.round(topCompanyShare * 100)}% of all declared SEPs in this dataset; the top 3 declarants together hold ${Math.round(top3Share * 100)}%.`
        : 'No portfolio data available.',
      color: BLUE,
    },
    {
      label: 'Most-Declared Release',
      value: topReleaseByVolume?.[0] || overview.top_release || '—',
      note: topReleaseByVolume
        ? `${fmt(topReleaseByVolume[1])} SEPs map to ${topReleaseByVolume[0]} across the coverage heatmap — the single most-targeted release in this run.`
        : 'Release-level coverage data not available.',
      color: PURPLE,
    },
    {
      label: 'Most-Declared Domain',
      value: topDomainByVolume?.[0] || '—',
      note: topDomainByVolume
        ? `${fmt(topDomainByVolume[1])} SEPs fall under ${topDomainByVolume[0]}, making it the technical domain with the deepest declared coverage.`
        : 'Domain-level coverage data not available.',
      color: ESS_AMBER,
    },
    {
      label: 'Geographic Concentration',
      value: topCountry ? `${topCountry.country} — ${topCountry.pct}%` : '—',
      note: topCountry
        ? `${sortedCountries.length} distinct jurisdiction${sortedCountries.length === 1 ? '' : 's'} appear in this dataset; ${topCountry.country} is the largest single filing jurisdiction at ${topCountry.pct}% of all SEPs.`
        : 'Country data not available.',
      color: ESS_RED,
    },
  ];

  // Stacked layout: label, then value, then the note wrapped full-width
  // below them. A side-by-side layout previously placed the note at a
  // fixed x-offset while the label/value shared that same row — long
  // labels (e.g. "PORTFOLIO CONCENTRATION") overran that offset and
  // collided with the note text. Stacking avoids that regardless of
  // label length.
  const CARD_PAD = 14;
  insightCards.forEach(card => {
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    const noteLines = doc.splitTextToSize(card.note, CONTENT_W - CARD_PAD * 2);
    const cardH = Math.max(48, 40 + noteLines.length * 11);
    y = ensureRoom(doc, y, cardH + 10, 'Executive Insights');
    doc.setFillColor(248, 250, 252);
    doc.setDrawColor(226, 230, 238);
    doc.roundedRect(MARGIN, y, CONTENT_W, cardH, 5, 5, 'FD');
    doc.setFillColor(...card.color);
    doc.rect(MARGIN, y, 4, cardH, 'F');
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(9);
    doc.setTextColor(...card.color);
    doc.text(card.label.toUpperCase(), MARGIN + CARD_PAD, y + 16);
    doc.setFont('helvetica', 'bold');
    doc.setFontSize(13);
    doc.setTextColor(...NAVY);
    doc.text(card.value, MARGIN + CARD_PAD, y + 32);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8.5);
    doc.setTextColor(...MUTED);
    doc.text(noteLines, MARGIN + CARD_PAD, y + 44);
    y += cardH + 10;
  });

  // ── Charts page(s) ──────────────────────────────────────────────────
  progress('Capturing charts…');
  const companySnap = await snapshotChart(chartRefs.company);
  const timelineSnap = await snapshotChart(chartRefs.timeline);
  const countrySnap = await snapshotChart(chartRefs.country);
  const releaseSnap = await snapshotChart(chartRefs.release);
  const essSnap = await snapshotChart(chartRefs.essentiality);

  // Charts flow one after another, each shrinking to fit remaining room
  // rather than always starting a fresh page — this is what keeps pages
  // from ending in a large empty gap when a chart is shorter than its
  // preferred height, or when only a little more room was needed to fit
  // the next one.
  const chartDefs: { title: string; snap: typeof companySnap; prefH: number; pageLabel: string }[] = [
    { title: 'Top Companies by SEP Count (Stacked by Essentiality)', snap: companySnap, prefH: 300, pageLabel: 'Charts — Companies' },
    { title: 'SEP Declarations Over Time', snap: timelineSnap, prefH: 200, pageLabel: 'Charts — Time & Geography' },
    { title: 'SEPs by Country (Illustrative)', snap: countrySnap, prefH: 230, pageLabel: 'Charts — Time & Geography' },
    { title: 'SEPs by Release', snap: releaseSnap, prefH: 210, pageLabel: 'Charts — Technical View' },
    { title: 'Essentiality Breakdown', snap: essSnap, prefH: 230, pageLabel: 'Charts — Technical View' },
  ];
  chartDefs.forEach(def => {
    if (!def.snap) return;
    y = ensureRoom(doc, y, 60, def.pageLabel);
    y = placeChart(doc, y, def.pageLabel, def.title, def.snap, def.prefH);
  });

  // ── By-release & country data tables ────────────────────────────────
  progress('Building data tables…');
  y = ensureRoom(doc, y, 60, 'Data — Release & Country Breakdown');
  y = sectionTitle(doc, 'SEPs by Release', y);
  autoTable(doc, {
    startY: y,
    margin: { left: MARGIN, right: MARGIN },
    head: [['Release', 'Total', 'Essential', 'Potential', 'Non-Essential']],
    body: byRelease.map(r => [r.release, fmt(r.count), fmt(r.Essential), fmt(r.Potential), fmt(r['Non-Essential'])]),
    styles: { fontSize: 8.5, cellPadding: 5 },
    headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold' },
    alternateRowStyles: { fillColor: [248, 250, 252] },
    theme: 'grid',
  });
  // @ts-expect-error jspdf-autotable augments doc with lastAutoTable at runtime
  y = (doc.lastAutoTable?.finalY ?? y) + 30;
  if (y > PAGE_H - 150) {
    doc.addPage();
    drawLetterhead(doc, 'Data — Release & Country Breakdown');
    y = 80;
  }
  y = sectionTitle(doc, 'SEPs by Country', y);
  autoTable(doc, {
    startY: y,
    margin: { left: MARGIN, right: MARGIN },
    head: [['Country', 'Count', '% of Total']],
    body: countries.map(c => [c.country, fmt(c.count), `${c.pct}%`]),
    styles: { fontSize: 8.5, cellPadding: 5 },
    headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold' },
    alternateRowStyles: { fillColor: [248, 250, 252] },
    theme: 'grid',
  });

  // ── Heatmap data table ──────────────────────────────────────────────
  if (heatmap.releases.length && heatmap.domains.length) {
    y = ensureRoom(doc, y, 60, 'Data — Coverage Heatmap');
    y = sectionTitle(doc, 'SEP Coverage — Release × Domain (counts)', y);
    const lookup = new Map(heatmap.cells.map(c => [`${c.domain}|${c.release}`, c.value]));
    autoTable(doc, {
      startY: y,
      margin: { left: MARGIN, right: MARGIN },
      head: [['Domain', ...heatmap.releases]],
      body: heatmap.domains.map(d => [d, ...heatmap.releases.map(r => fmt(lookup.get(`${d}|${r}`) ?? 0))]),
      styles: { fontSize: 7.5, cellPadding: 4 },
      headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [248, 250, 252] },
      theme: 'grid',
    });
  }

  // ── Legal status & domain distribution ───────────────────────────────
  // Aggregated client-side from the same full patent list used for the
  // registry — two more cuts through the data (legal status, technical
  // domain) that the on-screen dashboard doesn't surface as tables at all.
  progress('Building distribution tables…');
  const legalStatusCounts = new Map<string, number>();
  const domainCounts = new Map<string, number>();
  patents.forEach(p => {
    const ls = p.legal_status || 'Unknown';
    legalStatusCounts.set(ls, (legalStatusCounts.get(ls) ?? 0) + 1);
    const dm = p.domain || 'Unclassified';
    domainCounts.set(dm, (domainCounts.get(dm) ?? 0) + 1);
  });
  const sortedLegalStatus = [...legalStatusCounts.entries()].sort((a, b) => b[1] - a[1]);
  const sortedDomains = [...domainCounts.entries()].sort((a, b) => b[1] - a[1]);

  if (sortedLegalStatus.length || sortedDomains.length) {
    y = ensureRoom(doc, y, 60, 'Data — Legal Status & Domain Distribution');
    if (sortedLegalStatus.length) {
      y = sectionTitle(doc, 'Legal Status Distribution', y);
      autoTable(doc, {
        startY: y,
        margin: { left: MARGIN, right: MARGIN },
        head: [['Legal Status', 'Count', '% of Registry']],
        body: sortedLegalStatus.map(([status, count]) => [status, fmt(count), `${Math.round((count / Math.max(1, patents.length)) * 100)}%`]),
        styles: { fontSize: 8.5, cellPadding: 5 },
        headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold' },
        alternateRowStyles: { fillColor: [248, 250, 252] },
        theme: 'grid',
      });
      y = lastTableY(doc, y);
    }
    if (sortedDomains.length) {
      y = ensureRoom(doc, y, 60, 'Data — Legal Status & Domain Distribution');
      y = sectionTitle(doc, 'Technical Domain Distribution', y);
      autoTable(doc, {
        startY: y,
        margin: { left: MARGIN, right: MARGIN },
        head: [['Domain', 'Count', '% of Registry']],
        body: sortedDomains.map(([domain, count]) => [domain, fmt(count), `${Math.round((count / Math.max(1, patents.length)) * 100)}%`]),
        styles: { fontSize: 8.5, cellPadding: 5 },
        headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold' },
        alternateRowStyles: { fillColor: [248, 250, 252] },
        theme: 'grid',
      });
    }
  }

  // ── Top potential SEPs to watch ───────────────────────────────────────
  // An actionable shortlist — patents already classified "Potential" and
  // ranked by claim-match strength, i.e. the ones most worth a manual
  // essentiality review first.
  const potentialSeps = patents
    .filter(p => p.essentiality === 'Potential')
    .sort((a, b) => (b.claim_match ?? 0) - (a.claim_match ?? 0))
    .slice(0, 25);
  if (potentialSeps.length) {
    y = ensureRoom(doc, y, 90, 'Data — Potential SEPs to Watch');
    y = sectionTitle(doc, 'Top Potential SEPs to Watch', y);
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(9);
    doc.setTextColor(...MUTED);
    doc.text('Patents classified "Potential" with the strongest claim-match signal — the best starting shortlist for manual essentiality review.', MARGIN, y - 12);
    autoTable(doc, {
      startY: y,
      margin: { top: 80, left: MARGIN, right: MARGIN },
      didDrawPage: () => drawLetterhead(doc, 'Data — Potential SEPs to Watch'),
      head: [['Patent ID', 'Company', 'Matched Standard', 'Matched Clause', 'Claim Match', 'Semantic Sim.']],
      body: potentialSeps.map(p => [
        p.application_number || p.publication_number || String(p.id),
        p.company || '—',
        p.matched_standard || '—',
        p.matched_clause_title || p.matched_clause || '—',
        p.claim_match != null ? `${Math.round(p.claim_match * 100)}%` : '—',
        p.semantic_similarity != null ? `${Math.round(p.semantic_similarity * 100)}%` : '—',
      ]),
      styles: { fontSize: 7.5, cellPadding: 4, overflow: 'linebreak' },
      columnStyles: {
        0: { cellWidth: 68 },
        1: { cellWidth: 110 },
        2: { cellWidth: 100 },
        3: { cellWidth: 130 },
        4: { cellWidth: 55 },
        5: { cellWidth: 55 },
      },
      headStyles: { fillColor: ESS_AMBER, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [255, 251, 235] },
      theme: 'grid',
    });
  }

  // ── Company deep-dive appendix ────────────────────────────────────────
  // Per-company profile (essentiality split + top releases/domains) for
  // the largest declarants — pulled from the same /entity/company endpoint
  // the Company Profile page uses, so this is real per-company breadth the
  // portfolio table alone doesn't show.
  if (companyProfiles && companyProfiles.length) {
    progress('Building company profiles…');
    y = ensureRoom(doc, y, 60, 'Company Deep-Dive');
    y = sectionTitle(doc, `Company Deep-Dive — Top ${companyProfiles.length} Declarants`, y);

    companyProfiles.forEach(cp => {
      const topReleases = Object.entries(cp.by_release ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
      const topDomains = Object.entries(cp.by_domain ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
      const blockH = 96;
      y = ensureRoom(doc, y, blockH + 12, 'Company Deep-Dive');

      doc.setFillColor(248, 250, 252);
      doc.setDrawColor(226, 230, 238);
      doc.roundedRect(MARGIN, y, CONTENT_W, blockH, 6, 6, 'FD');
      doc.setFillColor(...NAVY);
      doc.rect(MARGIN, y, CONTENT_W, 22, 'F');
      doc.setFont('helvetica', 'bold');
      doc.setFontSize(10.5);
      doc.setTextColor(255, 255, 255);
      doc.text(cp.company, MARGIN + 10, y + 15);
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(8.5);
      doc.text(`${cp.country || '—'} · ${fmt(cp.total_seps)} declared SEPs`, PAGE_W - MARGIN - 10, y + 15, { align: 'right' });

      const splitY = y + 40;
      const splitTiles: { label: string; value: number; color: [number, number, number] }[] = [
        { label: 'Essential', value: cp.essential, color: ESS_GREEN },
        { label: 'Potential', value: cp.potential, color: ESS_AMBER },
        { label: 'Non-Essential', value: cp.non_essential, color: ESS_RED },
      ];
      const splitTileW = 90;
      splitTiles.forEach((t, i) => {
        const tx = MARGIN + 10 + i * (splitTileW + 6);
        doc.setFont('helvetica', 'bold');
        doc.setFontSize(13);
        doc.setTextColor(...t.color);
        doc.text(fmt(t.value), tx, splitY);
        doc.setFont('helvetica', 'normal');
        doc.setFontSize(7.5);
        doc.setTextColor(...MUTED);
        doc.text(t.label, tx, splitY + 11);
      });

      const listX = MARGIN + 320;
      doc.setFont('helvetica', 'bold');
      doc.setFontSize(7.5);
      doc.setTextColor(...MUTED);
      doc.text('TOP RELEASES', listX, y + 34);
      doc.text('TOP DOMAINS', listX + 130, y + 34);
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(8);
      doc.setTextColor(30, 41, 59);
      topReleases.forEach(([rel, cnt], i) => doc.text(`${rel} (${cnt})`, listX, y + 47 + i * 11));
      topDomains.forEach(([dom, cnt], i) => {
        const label = dom.length > 22 ? dom.slice(0, 21) + '…' : dom;
        doc.text(`${label} (${cnt})`, listX + 130, y + 47 + i * 11);
      });

      y += blockH + 12;
    });
  }

  // ── Full patent registry ────────────────────────────────────────────
  progress('Writing patent registry…');
  y = ensureRoom(doc, y, 90, 'Data — Patent Registry');
  const registryNote = patents.length < patentsTotal
    ? `Showing ${patents.length.toLocaleString()} of ${patentsTotal.toLocaleString()} matching patents (report row cap reached).`
    : `All ${patentsTotal.toLocaleString()} matching patents.`;
  y = sectionTitle(doc, 'Patent Registry — Legal View', y);
  doc.setFont('helvetica', 'normal');
  doc.setFontSize(9);
  doc.setTextColor(...MUTED);
  doc.text(registryNote, MARGIN, y - 12);

  autoTable(doc, {
    startY: y,
    margin: { top: 80, left: MARGIN, right: MARGIN },
    didDrawPage: () => drawLetterhead(doc, 'Data — Patent Registry'),
    head: [['Patent ID', 'Title', 'Company', 'Legal Status', 'ISLD Ref.', 'Claim Match', 'Essentiality']],
    body: patents.map(p => [
      p.application_number || p.publication_number || String(p.id),
      p.title || '—',
      p.company || '—',
      p.legal_status || '—',
      p.iprd_reference || '—',
      p.claim_match != null ? `${Math.round(p.claim_match * 100)}%` : '—',
      p.essentiality || '—',
    ]),
    styles: { fontSize: 7.5, cellPadding: 4, overflow: 'linebreak' },
    columnStyles: {
      0: { cellWidth: 68 },
      1: { cellWidth: 150 },
      2: { cellWidth: 90 },
      3: { cellWidth: 65 },
      4: { cellWidth: 70 },
      5: { cellWidth: 45 },
      6: { cellWidth: 55 },
    },
    headStyles: { fillColor: NAVY, textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
    alternateRowStyles: { fillColor: [248, 250, 252] },
    theme: 'grid',
    didParseCell: (data) => {
      if (data.section === 'body' && data.column.index === 6) {
        const v = String(data.cell.raw ?? '');
        if (v === 'Essential') data.cell.styles.textColor = ESS_GREEN;
        else if (v === 'Potential') data.cell.styles.textColor = ESS_AMBER;
        else if (v === 'Non-Essential') data.cell.styles.textColor = ESS_RED;
      }
    },
  });

  // ── Footer + page numbers on every page except the cover ───────────
  const pageCount = doc.getNumberOfPages();
  for (let p = 2; p <= pageCount; p++) {
    doc.setPage(p);
    drawFooter(doc, p - 1, pageCount - 1);
  }

  progress('Finalizing PDF…');
  const filename = `CPARAS_Bulk_SEP_Portfolio_Report_${generatedAt.toISOString().slice(0, 10)}.pdf`;
  doc.save(filename);
}
