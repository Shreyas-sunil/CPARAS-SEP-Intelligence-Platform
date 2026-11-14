/**
 * CPARAS Bulk Portfolio Dashboard (/bulk): KPI tiles, company / timeline / country / release /
 * heatmap charts and the portfolio table for the filtered records, plus the portfolio PDF report.
 */

import { useEffect, useMemo, useState, useCallback, useRef } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  LayoutGrid, Cpu, Briefcase, Scale, UploadCloud, Database, FileDown, FileSpreadsheet, Loader2,
  SlidersHorizontal, Table2, X, RefreshCw,
} from 'lucide-react';
import FilterSidebar, { type FilterOptions } from '../components/FilterSidebar';
import { CompanyStackedBar, TimelineLine, CountryDonut, ReleaseBars, EssentialityDonut, Heatmap, PortfolioTable } from '../components/charts';
import PatentRegistryTable from '../components/PatentRegistryTable';
import UploadPanel from '../components/UploadPanel';
import * as sep from '../api';
import type { Filters, Patent, SepRun } from '../api';
import { generateGitasReport } from '../utils/pdfReport';
import { GitasForcedTheme } from '../context/ThemeContext';
import { PRODUCTS } from '../../platform/products';
import { Banner, Card, EmptyState, Kpi, KpiSkeleton, LoadingState, PageHeader, Pill, Tabs } from '../../platform/ui';

// Cap on how many patent rows the PDF report will embed — the registry can
// legitimately run into the tens of thousands, and jsPDF/autotable slow
// down noticeably well before that. The report notes when the cap is hit.
const REPORT_PATENT_ROW_CAP = 3000;
// The backend caps a single /patents request at 500 rows.
const BACKEND_PATENTS_PAGE_SIZE = 500;
// Largest declarants that get a full profile block in the report appendix.
const COMPANY_DEEP_DIVE_COUNT = 6;

type View = 'overview' | 'technical' | 'portfolio' | 'legal';

const TABS: { id: View; label: string; icon: React.ReactNode }[] = [
  { id: 'overview', label: 'Overview', icon: <LayoutGrid size={13} /> },
  { id: 'technical', label: 'Technical', icon: <Cpu size={13} /> },
  { id: 'portfolio', label: 'Portfolio', icon: <Briefcase size={13} /> },
  { id: 'legal', label: 'Legal', icon: <Scale size={13} /> },
];

const product = PRODUCTS.bulk;

const pct = (n: number, total: number) => (total ? `${((n / total) * 100).toFixed(1)}% of records` : '—');

/** Portfolio dashboard page. */
export default function DashboardPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [runs, setRuns] = useState<SepRun[] | null>(null);
  const [runsError, setRunsError] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<number | 'all' | null>(null);
  const [view, setView] = useState<View>('overview');
  const [options, setOptions] = useState<FilterOptions>({ technology: [], release: [], company: [], country: [] });
  const [draft, setDraft] = useState<Filters>({});
  const [applied, setApplied] = useState<Filters>({});
  const [showFilters, setShowFilters] = useState(() => typeof window === 'undefined' || window.innerWidth > 1000);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [overview, setOverview] = useState<any>(null);
  const [companies, setCompanies] = useState<any[]>([]);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [countries, setCountries] = useState<{ total: number; countries: any[] }>({ total: 0, countries: [] });
  const [byRelease, setByRelease] = useState<any[]>([]);
  const [portfolio, setPortfolio] = useState<any[]>([]);
  const [heatmap, setHeatmap] = useState<{ releases: string[]; domains: string[]; cells: any[] }>({ releases: [], domains: [], cells: [] });
  const [patents, setPatents] = useState<Patent[]>([]);
  const [patentsTotal, setPatentsTotal] = useState(0);

  const [reportStatus, setReportStatus] = useState<string | null>(null);
  const [excelStatus, setExcelStatus] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // Off-screen chart instances used purely as html2canvas capture sources
  // for the PDF report — so the report always contains every chart, not
  // just whichever tab is visible.
  const companyChartRef = useRef<HTMLDivElement>(null);
  const timelineChartRef = useRef<HTMLDivElement>(null);
  const countryChartRef = useRef<HTMLDivElement>(null);
  const releaseChartRef = useRef<HTMLDivElement>(null);
  const essentialityChartRef = useRef<HTMLDivElement>(null);
  const heatmapChartRef = useRef<HTMLDivElement>(null);

  const refreshRuns = useCallback(() => {
    setRunsError(false);
    sep.getRuns().then(d => setRuns(d.runs)).catch(() => { setRuns([]); setRunsError(true); });
  }, []);

  useEffect(() => { refreshRuns(); }, [refreshRuns]);

  // Pick the run: ?run=<id> from the URL (links from Uploads & Runs), else
  // the most recent upload. Multiple runs are never merged silently.
  useEffect(() => {
    if (!runs || runs.length === 0) return;
    const fromUrl = searchParams.get('run');
    if (fromUrl === 'all') { setSelectedRunId('all'); return; }
    const id = fromUrl ? Number(fromUrl) : NaN;
    setSelectedRunId(runs.some(r => r.id === id) ? id : runs[0].id);
  }, [runs]); // eslint-disable-line react-hooks/exhaustive-deps

  const chooseRun = (v: number | 'all') => {
    setSelectedRunId(v);
    const next = new URLSearchParams(searchParams);
    next.set('run', String(v));
    setSearchParams(next, { replace: true });
  };

  useEffect(() => {
    if (!runs || runs.length === 0 || selectedRunId == null) return;
    sep.getFilters(selectedRunId === 'all' ? undefined : selectedRunId).then(setOptions).catch(() => {});
  }, [runs, selectedRunId]);

  const effectiveFilters = useMemo<Filters>(() => ({
    ...applied,
    run_id: selectedRunId === 'all' || selectedRunId == null ? undefined : selectedRunId,
  }), [applied, selectedRunId]);

  const loadAll = useCallback((f: Filters) => {
    setLoading(true);
    setLoadError(null);
    Promise.all([
      sep.getOverview(f),
      sep.getCompanies(f, 10),
      sep.getTimeline(f),
      sep.getCountries(f),
      sep.getByRelease(f),
      sep.getPortfolio(f),
      sep.getHeatmap(f),
      sep.getPatents(f, 50, 0),
    ]).then(([ov, comp, tl, ctry, rel, port, heat, pats]) => {
      setOverview(ov);
      setCompanies(comp.companies);
      setTimeline(tl.timeline);
      setCountries(ctry);
      setByRelease(rel.by_release.map((r: any) => ({ ...r, 'Non-Essential': r.non_essential ?? r['Non-Essential'] })));
      setPortfolio(port.portfolio);
      setHeatmap(heat);
      setPatents(pats.patents);
      setPatentsTotal(pats.total);
    }).catch(() => setLoadError('The dashboard data could not be loaded. Check that the backend is running and try again.'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!runs || runs.length === 0 || selectedRunId == null) return;
    loadAll(effectiveFilters);
  }, [runs, effectiveFilters, loadAll, selectedRunId, reloadKey]);

  const selectedRun = useMemo(() => (typeof selectedRunId === 'number' ? runs?.find(r => r.id === selectedRunId) : undefined), [runs, selectedRunId]);

  // While the selected run is still scoring, poll its status so the banner
  // (and, when it finishes, the figures) stay current.
  const [liveStatus, setLiveStatus] = useState<sep.SepRunStatus | null>(null);
  useEffect(() => {
    setLiveStatus(null);
    if (!selectedRun || selectedRun.status !== 'processing') return;
    let alive = true;
    const tick = async () => {
      try {
        const s = await sep.getRunStatus(selectedRun.id);
        if (!alive) return;
        setLiveStatus(s);
        if (s.status !== 'processing') { refreshRuns(); setReloadKey(k => k + 1); }
      } catch { /* transient */ }
    };
    tick();
    const t = setInterval(tick, 2500);
    return () => { alive = false; clearInterval(t); };
  }, [selectedRun?.id, selectedRun?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const activeFilterCount = (['technology', 'release', 'company', 'country', 'essentiality'] as const)
    .reduce((n, k) => n + ((applied[k] as string[] | undefined)?.length ?? 0), 0);
  const draftDirty = JSON.stringify(draft) !== JSON.stringify(applied);

  const resetFilters = () => { setDraft({}); setApplied({}); };
  const removeFilter = (key: keyof Filters, value: string) => {
    const next = { ...applied, [key]: ((applied[key] as string[]) ?? []).filter(v => v !== value) };
    setApplied(next);
    setDraft(next);
  };

  const heatmapCaptureWidth = useMemo(() => {
    const domainColWidth = 190;
    const perReleaseWidth = 94;
    const padding = 32;
    return Math.max(760, domainColWidth + heatmap.releases.length * perReleaseWidth + padding);
  }, [heatmap.releases.length]);

  const runLabel = useMemo(() => {
    if (!runs) return '—';
    if (selectedRunId === 'all') return `All runs (${runs.length} upload${runs.length === 1 ? '' : 's'})`;
    const r = runs.find(x => x.id === selectedRunId);
    return r ? `${r.filename} · ${r.total_rows.toLocaleString()} rows` : `Run #${selectedRunId}`;
  }, [runs, selectedRunId]);

  const filtersSummaryText = useMemo(() => {
    const parts: string[] = [];
    (applied.technology ?? []).length && parts.push(`Technology: ${applied.technology!.join(', ')}`);
    (applied.release ?? []).length && parts.push(`Release: ${applied.release!.join(', ')}`);
    (applied.company ?? []).length && parts.push(`Company: ${applied.company!.join(', ')}`);
    (applied.country ?? []).length && parts.push(`Country: ${applied.country!.join(', ')}`);
    (applied.essentiality ?? []).length && parts.push(`Essentiality: ${applied.essentiality!.join(', ')}`);
    return parts.length ? parts.join(' | ') : 'None applied';
  }, [applied]);

  const handleDownloadReport = useCallback(async () => {
    if (!overview) return;
    setReportStatus('Fetching registry…');
    try {
      let allPatents: Patent[] = [];
      let total = 0;
      let offset = 0;
      while (true) {
        const page = await sep.getPatents(effectiveFilters, BACKEND_PATENTS_PAGE_SIZE, offset);
        total = page.total;
        allPatents = allPatents.concat(page.patents);
        offset += page.patents.length;
        if (page.patents.length === 0) break;
        if (offset >= total) break;
        if (offset >= REPORT_PATENT_ROW_CAP) break;
        setReportStatus(`Fetching registry… ${offset.toLocaleString()} / ${total.toLocaleString()}`);
      }
      if (allPatents.length > REPORT_PATENT_ROW_CAP) allPatents = allPatents.slice(0, REPORT_PATENT_ROW_CAP);

      setReportStatus('Fetching company profiles…');
      const topCompanyNames = [...portfolio].sort((a, b) => b.total - a.total).slice(0, COMPANY_DEEP_DIVE_COUNT).map(r => r.company);
      const companyProfiles = (await Promise.all(
        topCompanyNames.map(name => sep.getCompanyProfile(name).catch(() => null))
      )).filter((p): p is NonNullable<typeof p> => p != null);

      await generateGitasReport({
        overview,
        portfolio,
        byRelease,
        countries: countries.countries,
        heatmap,
        patents: allPatents,
        patentsTotal: total,
        runLabel,
        filtersSummary: filtersSummaryText,
        chartRefs: {
          company: companyChartRef.current,
          timeline: timelineChartRef.current,
          country: countryChartRef.current,
          release: releaseChartRef.current,
          essentiality: essentialityChartRef.current,
          heatmap: heatmapChartRef.current,
        },
        companyProfiles,
        onProgress: setReportStatus,
      });
    } catch (err) {
      console.error('Failed to generate the CPARAS Bulk report', err);
      setReportStatus('Report failed — try again');
      setTimeout(() => setReportStatus(null), 3000);
      return;
    }
    setReportStatus(null);
  }, [overview, portfolio, byRelease, countries, heatmap, effectiveFilters, runLabel, filtersSummaryText]);

  // Enriched Excel is per run; "All runs" targets the most recent upload.
  const excelRunId = selectedRunId === 'all' ? runs?.[0]?.id : (selectedRunId ?? undefined);
  const handleDownloadExcel = useCallback(async () => {
    if (excelRunId == null) return;
    setExcelStatus('Preparing workbook…');
    try {
      await sep.downloadRunExcel(excelRunId);
    } catch (err) {
      console.error('Failed to download CPARAS Bulk Excel export', err);
      setExcelStatus('Export failed — try again');
      setTimeout(() => setExcelStatus(null), 3000);
      return;
    }
    setExcelStatus(null);
  }, [excelRunId]);

  // ── First-run / error states ──────────────────────────────────────────
  if (runs === null) {
    return <div className="page"><PageHeader product={product} title="Portfolio Dashboard" /><KpiSkeleton /></div>;
  }

  if (runs.length === 0) {
    return (
      <div className="page page--narrow">
        <PageHeader product={product} title="Portfolio Dashboard"
          description="Upload an ETSI bulk IPR-declaration export to populate the dashboard — company breakdowns, essentiality, release and domain coverage, and the full patent registry." />
        {runsError && <Banner tone="err" title="Could not reach the backend" style={{ marginBottom: 14 }}>The list of uploads could not be loaded. Showing the upload form anyway.</Banner>}
        <Card>
          <EmptyState icon={<Database size={22} />} title="No bulk export uploaded yet"
            body="Every row of the file you upload is kept and scored — repeated application or publication numbers are never merged or dropped." />
          <UploadPanel onUploaded={refreshRuns} />
        </Card>
      </div>
    );
  }

  const total = overview?.total_seps ?? 0;

  return (
    <div className="page">
      <PageHeader
        product={product}
        title="Portfolio Dashboard"
        description={<>{runLabel}{overview ? <> · <b className="num">{total.toLocaleString()}</b> matching record{total === 1 ? '' : 's'}</> : null}</>}
        actions={
          <>
            <select className="select" aria-label="Upload run" value={String(selectedRunId ?? '')}
              onChange={e => chooseRun(e.target.value === 'all' ? 'all' : Number(e.target.value))} style={{ maxWidth: 280 }}>
              {runs.length > 1 && <option value="all">All runs ({runs.length})</option>}
              {runs.map(r => <option key={r.id} value={r.id}>{r.filename} · {r.total_rows.toLocaleString()} rows</option>)}
            </select>
            <button className="btn" onClick={handleDownloadExcel} disabled={!overview || !!excelStatus || excelRunId == null}
              title="Your original sheet — every source row, same order and columns — with the CPARAS_Bulk_* enrichment columns appended, plus a CPARAS Bulk Summary sheet">
              {excelStatus ? <Loader2 size={14} className="spin" /> : <FileSpreadsheet size={14} />}
              {excelStatus ?? 'Enriched Excel'}
            </button>
            <button className="btn" onClick={handleDownloadReport} disabled={!overview || !!reportStatus} title="Portfolio PDF report with every chart and the patent registry">
              {reportStatus ? <Loader2 size={14} className="spin" /> : <FileDown size={14} />}
              {reportStatus ?? 'PDF report'}
            </button>
            <Link className="btn btn-product" to="/bulk/runs"><UploadCloud size={14} /> Upload</Link>
          </>
        }
      />

      {selectedRun && selectedRun.status === 'processing' && (
        <Banner tone="info" style={{ marginBottom: 14 }} icon={<Loader2 size={16} className="spin" />}
          title="This run is still being scored"
          actions={<Link className="btn btn-sm" to="/bulk/runs">View progress</Link>}>
          {liveStatus
            ? <>{liveStatus.processed.toLocaleString()} of {liveStatus.parsed.toLocaleString()} rows processed. Figures below include only rows scored so far and refresh when the run finishes.</>
            : 'Figures below include only rows scored so far.'}
        </Banner>
      )}
      {selectedRun && (selectedRun.status === 'paused' || selectedRun.status === 'failed') && (
        <Banner tone="warn" style={{ marginBottom: 14 }} title={`This run is ${selectedRun.status}`}
          actions={<Link className="btn btn-sm" to="/bulk/runs">Resume in Uploads &amp; Runs</Link>}>
          {selectedRun.processed_rows.toLocaleString()} of {selectedRun.total_rows.toLocaleString()} rows were scored before it stopped.
        </Banner>
      )}

      <div className="dash-toolbar">
        <Tabs value={view} onChange={setView} items={TABS} />
        <div className="row">
          {activeFilterCount > 0 && (
            <div className="active-filters">
              {(['technology', 'release', 'company', 'country', 'essentiality'] as const).flatMap(k =>
                ((applied[k] as string[] | undefined) ?? []).map(v => (
                  <button key={`${k}:${v}`} className="pill pill-info" style={{ cursor: 'pointer', border: 'none' }} onClick={() => removeFilter(k, v)} title="Remove filter">
                    {v} <X size={11} />
                  </button>
                )))}
              <button className="btn btn-sm btn-ghost" onClick={resetFilters}>Clear all</button>
            </div>
          )}
          <button className="btn btn-sm" onClick={() => setShowFilters(s => !s)} aria-pressed={showFilters}>
            <SlidersHorizontal size={13} /> {showFilters ? 'Hide filters' : 'Filters'}{activeFilterCount ? ` (${activeFilterCount})` : ''}
          </button>
        </div>
      </div>

      <div className={`bulk-layout${showFilters ? '' : ' filters-hidden'}`}>
        {showFilters && (
          <FilterSidebar options={options} draft={draft} onChange={setDraft} dirty={draftDirty}
            onApply={() => setApplied(draft)} onReset={resetFilters} onClose={() => setShowFilters(false)} />
        )}

        <div className="stack" style={{ minWidth: 0 }}>
          {loadError && (
            <Banner tone="err" title="Dashboard unavailable" actions={<button className="btn btn-sm" onClick={() => setReloadKey(k => k + 1)}><RefreshCw size={13} /> Retry</button>}>{loadError}</Banner>
          )}

          {loading && !overview && <><KpiSkeleton /><LoadingState label="Loading portfolio data…" /></>}

          {overview && (
            <div style={{ opacity: loading ? 0.55 : 1, transition: 'opacity .15s' }} className="stack">
              {view === 'overview' && (
                <>
                  <div className="kpi-grid">
                    <Kpi label="Declared records" value={total.toLocaleString()} note="one per uploaded source row" color="var(--c-bulk)" />
                    <Kpi label="Essential" value={overview.essential.toLocaleString()} note={pct(overview.essential, total)} color="var(--c-ok)" />
                    <Kpi label="Potential SEP" value={overview.potential.toLocaleString()} note={pct(overview.potential, total)} color="var(--c-warn)" />
                    <Kpi label="Non-essential" value={overview.non_essential.toLocaleString()} note={pct(overview.non_essential, total)} color="var(--c-err)" />
                  </div>
                  <Card title="Top declaring companies" subtitle="Records per company, stacked by essentiality">
                    {companies.length ? <CompanyStackedBar data={companies} /> : <EmptyState title="No company data" />}
                  </Card>
                  <div className="grid-2">
                    <Card title="Declarations over time" subtitle="Cumulative, by signature year">
                      {timeline.length ? <TimelineLine data={timeline} /> : <EmptyState title="No signature dates in this data" />}
                    </Card>
                    <Card title="Records by country of registration">
                      {countries.countries.length ? <CountryDonut data={countries.countries} total={countries.total} /> : <EmptyState title="No country data" />}
                    </Card>
                  </div>
                  <Card title="Company portfolio" subtitle={`${portfolio.length.toLocaleString()} companies`} flush>
                    <PortfolioTable rows={portfolio} />
                  </Card>
                </>
              )}

              {view === 'technical' && (
                <>
                  <div className="kpi-grid">
                    <Kpi label="Declared records" value={total.toLocaleString()} color="var(--c-bulk)" />
                    <Kpi label="Essential" value={overview.essential.toLocaleString()} note={pct(overview.essential, total)} color="var(--c-ok)" />
                    <Kpi label="Top release" value={overview.top_release || '—'} note="most declarations" color="var(--c-isld)" />
                    <Kpi label="Specifications covered" value={overview.specs_covered.toLocaleString()} color="var(--c-warn)" />
                  </div>
                  <div className="grid-2">
                    <Card title="Records by 3GPP release" subtitle="Stacked by essentiality">
                      {byRelease.length ? <ReleaseBars data={byRelease} /> : <EmptyState title="No release data" />}
                    </Card>
                    <Card title="Essentiality breakdown">
                      <EssentialityDonut essential={overview.essential} potential={overview.potential} nonEssential={overview.non_essential} />
                    </Card>
                  </div>
                  <Card title="Coverage heatmap" subtitle="Release × technical domain">
                    {heatmap.releases.length ? <Heatmap releases={heatmap.releases} domains={heatmap.domains} cells={heatmap.cells} /> : <EmptyState title="No release/domain data" />}
                  </Card>
                </>
              )}

              {view === 'portfolio' && (
                <Card title="Company portfolio" subtitle={`${portfolio.length.toLocaleString()} companies · click a company for its full profile`} flush>
                  <PortfolioTable rows={portfolio} />
                </Card>
              )}

              {view === 'legal' && (
                <>
                  <div className="kpi-grid">
                    <Kpi label="Declared records" value={total.toLocaleString()} color="var(--c-bulk)" />
                    <Kpi label="Avg. essentiality score" value={`${overview.avg_essentiality_score}%`} color="var(--c-ok)" />
                    <Kpi label="Essential" value={overview.essential.toLocaleString()} color="var(--c-isld)" />
                    <Kpi label="Non-essential" value={overview.non_essential.toLocaleString()} color="var(--c-err)" />
                  </div>
                  <Card title="Patent registry — legal view"
                    subtitle={`First ${patents.length.toLocaleString()} of ${patentsTotal.toLocaleString()} records matching the current filters`}
                    actions={<Link className="btn btn-sm" to={`/bulk/registry${typeof selectedRunId === 'number' ? `?run=${selectedRunId}` : ''}`}><Table2 size={13} /> Open full registry</Link>}
                    flush>
                    {patents.length ? <PatentRegistryTable patents={patents} /> : <EmptyState title="No records match these filters" />}
                  </Card>
                </>
              )}
            </div>
          )}

          {!loading && overview && total === 0 && (
            <Banner tone="info" title="No records match">
              {activeFilterCount ? 'Try removing some filters.' : 'This run has no scored records yet.'}
              {selectedRun?.status === 'processing' ? <Pill tone="info">Still processing</Pill> : null}
            </Banner>
          )}
        </div>
      </div>

      {/* Off-screen chart instances kept mounted purely so the PDF report
          can snapshot every chart regardless of the visible tab. Pinned to
          the light theme since the PDF always renders on white. */}
      {!loading && overview && (
        <GitasForcedTheme theme="light">
          <div aria-hidden className="app-root g-pdf-capture" data-theme="light" style={{ position: 'absolute', top: 0, left: -99999, width: 760, pointerEvents: 'none' }}>
            <div ref={companyChartRef} style={{ background: '#fff', padding: 16, width: 760 }}><CompanyStackedBar data={companies} /></div>
            <div ref={timelineChartRef} style={{ background: '#fff', padding: 16, width: 760 }}><TimelineLine data={timeline} /></div>
            <div ref={countryChartRef} style={{ background: '#fff', padding: 16, width: 760, color: '#111b2a' }}><CountryDonut data={countries.countries} total={countries.total} /></div>
            <div ref={releaseChartRef} style={{ background: '#fff', padding: 16, width: 760 }}><ReleaseBars data={byRelease} /></div>
            <div ref={essentialityChartRef} style={{ background: '#fff', padding: 16, width: 760, color: '#111b2a' }}>
              <EssentialityDonut essential={overview.essential} potential={overview.potential} nonEssential={overview.non_essential} />
            </div>
            <div ref={heatmapChartRef} style={{ background: '#fff', padding: 16, width: heatmapCaptureWidth }}>
              <Heatmap releases={heatmap.releases} domains={heatmap.domains} cells={heatmap.cells} />
            </div>
          </div>
        </GitasForcedTheme>
      )}
    </div>
  );
}
