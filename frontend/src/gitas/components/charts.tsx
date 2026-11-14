/**
 * Recharts charts of the CPARAS Bulk dashboard (and its PDF report snapshots). Colours come from
 * the platform tokens so light and dark themes both work.
 */

import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  LineChart, Line, PieChart, Pie, Cell, Legend,
} from 'recharts';
import { Link } from 'react-router-dom';
import { CountryFlag } from './common';
import { useGitasTheme } from '../context/ThemeContext';

// Recharts renders raw SVG fill/stroke attributes, which — unlike regular
// CSS properties — don't resolve var(--g-*) custom properties at render
// time. So chart palettes are plain hex, picked per-theme in JS via
// useGitasTheme() rather than living in gitas.css with the rest of the UI.
interface ChartPalette {
  essential: string; potential: string; nonEssential: string; blue: string;
  grid: string; axisLine: string; tick: string; tooltipBg: string;
  tooltipBorder: string; tooltipText: string; tooltipShadow: string;
  cursorFill: string; companyTick: string; donutStroke: string; mutedText: string;
}
const PALETTE: Record<'light' | 'dark', ChartPalette> = {
  light: {
    essential: '#128a5b', potential: '#c47a0a', nonEssential: '#c93a30',
    blue: '#1d5bd0', grid: '#eceff3', axisLine: '#e0e4ea', tick: '#5d697b',
    tooltipBg: '#ffffff', tooltipBorder: '#e0e4ea', tooltipText: '#111b2a',
    tooltipShadow: '0 8px 24px rgba(14,27,45,.12)', cursorFill: 'rgba(29,91,208,.05)',
    companyTick: '#111b2a', donutStroke: '#ffffff', mutedText: '#5d697b',
  },
  dark: {
    essential: '#3fcf90', potential: '#f1b44c', nonEssential: '#f2786e',
    blue: '#5b8def', grid: '#1f2a3a', axisLine: '#2a3748', tick: '#8f9bb0',
    tooltipBg: '#121a25', tooltipBorder: '#243042', tooltipText: '#e7ecf4',
    tooltipShadow: '0 8px 24px rgba(0,0,0,.45)', cursorFill: 'rgba(91,141,239,.08)',
    companyTick: '#e7ecf4', donutStroke: '#121a25', mutedText: '#8f9bb0',
  },
};

function usePalette(): ChartPalette {
  const { theme } = useGitasTheme();
  return PALETTE[theme];
}

function tooltipStyle(p: ChartPalette) {
  return {
    fontSize: 12, borderRadius: 10, border: `1px solid ${p.tooltipBorder}`,
    boxShadow: p.tooltipShadow, padding: '8px 12px', background: p.tooltipBg, color: p.tooltipText,
  };
}

// Truncates a long label to fit a fixed axis width instead of letting it
// overflow into whatever sits above/beside the chart. Full name is still
// available on hover via the native <title> tooltip on the SVG text node.
function truncate(label: string, max = 20) {
  if (label.length <= max) return label;
  return label.slice(0, max - 1).trimEnd() + '…';
}

/** Truncated company name axis tick with the full name as tooltip. */
function CompanyTick({ x, y, payload, fill }: any) {
  const full = payload.value as string;
  const short = truncate(full, 20);
  return (
    <text x={x} y={y} dy={4} textAnchor="end" fontSize={12} fill={fill}>
      <title>{full}</title>
      {short}
    </text>
  );
}

/** Top companies as stacked Essential / Potential / Non-Essential bars. */
export function CompanyStackedBar({ data }: { data: { company: string; essential: number; non_essential: number; potential: number; total: number }[] }) {
  const p = usePalette();
  if (!data.length) return null;
  return (
    <ResponsiveContainer width="100%" height={Math.max(220, data.length * 44)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 24, left: 8, bottom: 4 }} barCategoryGap="28%">
        <CartesianGrid strokeDasharray="3 3" stroke={p.grid} horizontal={false} />
        <XAxis type="number" tick={{ fontSize: 11, fill: p.tick }} axisLine={{ stroke: p.axisLine }} tickLine={false} />
        <YAxis type="category" dataKey="company" width={148} tick={<CompanyTick fill={p.companyTick} />} axisLine={{ stroke: p.axisLine }} tickLine={false} interval={0} />
        <Tooltip
          cursor={{ fill: p.cursorFill }}
          contentStyle={tooltipStyle(p)}
        />
        <Legend verticalAlign="top" align="right" iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11.5, top: -2, color: p.mutedText }} />
        <Bar dataKey="essential" stackId="a" fill={p.essential} name="Essential" radius={[0, 0, 0, 0]} maxBarSize={22} />
        <Bar dataKey="potential" stackId="a" fill={p.potential} name="Potential" maxBarSize={22} />
        <Bar dataKey="non_essential" stackId="a" fill={p.nonEssential} name="Non-Essential" radius={[0, 4, 4, 0]} maxBarSize={22} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Declarations per year with the cumulative total. */
export function TimelineLine({ data }: { data: { year: number; count: number; cumulative: number }[] }) {
  const p = usePalette();
  if (!data.length) return null;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 6, right: 16, left: -14, bottom: 0 }}>
        <defs>
          <linearGradient id="timelineFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={p.blue} stopOpacity={0.16} />
            <stop offset="100%" stopColor={p.blue} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={p.grid} />
        <XAxis dataKey="year" tick={{ fontSize: 11, fill: p.tick }} axisLine={{ stroke: p.axisLine }} tickLine={false} minTickGap={14} />
        <YAxis tick={{ fontSize: 11, fill: p.tick }} axisLine={{ stroke: p.axisLine }} tickLine={false} />
        <Tooltip contentStyle={tooltipStyle(p)} />
        <Line type="monotone" dataKey="cumulative" stroke={p.blue} strokeWidth={2.5} dot={{ r: 3, strokeWidth: 0, fill: p.blue }} activeDot={{ r: 5 }} name="Cumulative SEPs" fill="url(#timelineFill)" />
      </LineChart>
    </ResponsiveContainer>
  );
}

const DONUT_COLORS = {
  light: ['#1d5bd0', '#128a5b', '#c47a0a', '#6a3fcb', '#0e8fa3', '#c93a30', '#5d697b'],
  dark: ['#5b8def', '#3fcf90', '#f1b44c', '#a488f2', '#3cc3d6', '#f2786e', '#8f9bb0'],
} as const;

/** Share of records per country. */
export function CountryDonut({ data, total }: { data: { country: string; count: number; pct: number }[]; total: number }) {
  const p = usePalette();
  const { theme } = useGitasTheme();
  const colors = DONUT_COLORS[theme];
  if (!data.length) return null;
  return (
    <div style={{ position: 'relative' }}>
      <ResponsiveContainer width="100%" height={230}>
        <PieChart>
          <Pie data={data} dataKey="count" nameKey="country" innerRadius={62} outerRadius={92} paddingAngle={2} stroke={p.donutStroke} strokeWidth={2}>
            {data.map((_, i) => <Cell key={i} fill={colors[i % colors.length]} />)}
          </Pie>
          <Tooltip contentStyle={tooltipStyle(p)} />
        </PieChart>
      </ResponsiveContainer>
      <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-58%)', textAlign: 'center', pointerEvents: 'none' }}>
        <div style={{ fontSize: 22, fontWeight: 600, color: 'var(--g-text)' }}>{total.toLocaleString()}</div>
        <div style={{ fontSize: 10.5, color: p.mutedText }}>Records</div>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', marginTop: 8, fontSize: 12, color: 'var(--g-text)' }}>
        {data.map((d, i) => (
          <span key={d.country} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: colors[i % colors.length], display: 'inline-block' }} />
            <CountryFlag code={d.country} />{d.country} — {d.pct}%
          </span>
        ))}
      </div>
    </div>
  );
}

/** Essentiality split per 3GPP release. */
export function ReleaseBars({ data }: { data: { release: string; count: number; Essential: number; Potential: number; 'Non-Essential': number }[] }) {
  const p = usePalette();
  if (!data.length) return null;
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={data} margin={{ top: 6, right: 10, left: -18, bottom: 0 }} barCategoryGap="32%">
        <CartesianGrid strokeDasharray="3 3" stroke={p.grid} />
        <XAxis dataKey="release" tick={{ fontSize: 11.5, fill: p.tick }} axisLine={{ stroke: p.axisLine }} tickLine={false} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: p.tick }} axisLine={{ stroke: p.axisLine }} tickLine={false} />
        <Tooltip contentStyle={tooltipStyle(p)} cursor={{ fill: p.cursorFill }} />
        <Legend verticalAlign="top" align="right" iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11.5, top: -2, color: p.mutedText }} />
        <Bar dataKey="Essential" stackId="r" fill={p.essential} maxBarSize={44} />
        <Bar dataKey="Potential" stackId="r" fill={p.potential} maxBarSize={44} />
        <Bar dataKey="Non-Essential" stackId="r" fill={p.nonEssential} radius={[4, 4, 0, 0]} maxBarSize={44} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Overall Essential / Potential / Non-Essential split. */
export function EssentialityDonut({ essential, potential, nonEssential }: { essential: number; potential: number; nonEssential: number }) {
  const p = usePalette();
  const total = essential + potential + nonEssential;
  const data = [
    { name: 'Essential', value: essential, color: p.essential },
    { name: 'Potential SEP', value: potential, color: p.potential },
    { name: 'Non-Essential', value: nonEssential, color: p.nonEssential },
  ].filter(d => d.value > 0);
  return (
    <div style={{ position: 'relative' }}>
      <ResponsiveContainer width="100%" height={230}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" innerRadius={62} outerRadius={92} paddingAngle={2} stroke={p.donutStroke} strokeWidth={2}>
            {data.map((d, i) => <Cell key={i} fill={d.color} />)}
          </Pie>
          <Tooltip contentStyle={tooltipStyle(p)} />
        </PieChart>
      </ResponsiveContainer>
      <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-58%)', textAlign: 'center', pointerEvents: 'none' }}>
        <div style={{ fontSize: 22, fontWeight: 600, color: 'var(--g-text)' }}>{total.toLocaleString()}</div>
        <div style={{ fontSize: 10.5, color: p.mutedText }}>Records</div>
      </div>
      <div style={{ marginTop: 8, fontSize: 12, color: 'var(--g-text)' }}>
        {data.map(d => (
          <div key={d.name} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ width: 9, height: 9, borderRadius: 3, background: d.color, display: 'inline-block' }} />{d.name}
            </span>
            <b>{d.value}</b>
          </div>
        ))}
      </div>
    </div>
  );
}

function heatColor(value: number, max: number, theme: 'light' | 'dark') {
  if (!value) return null;
  const t = max > 0 ? value / max : 0;
  const stops: [number, [number, number, number]][] = theme === 'dark'
    ? [[0, [30, 58, 95]], [0.5, [58, 110, 200]], [1, [125, 176, 255]]]
    : [[0, [196, 214, 245]], [0.5, [59, 118, 222]], [1, [11, 37, 69]]];
  let lo = stops[0], hi = stops[stops.length - 1];
  for (let i = 0; i < stops.length - 1; i++) {
    if (t >= stops[i][0] && t <= stops[i + 1][0]) { lo = stops[i]; hi = stops[i + 1]; break; }
  }
  const span = hi[0] - lo[0] || 1;
  const localT = (t - lo[0]) / span;
  const rgb = lo[1].map((c, i) => Math.round(c + (hi[1][i] - c) * localT));
  return `rgb(${rgb.join(',')})`;
}

/** Release x technical domain record counts. */
export function Heatmap({ releases, domains, cells }: { releases: string[]; domains: string[]; cells: { domain: string; release: string; value: number }[] }) {
  const { theme } = useGitasTheme();
  if (!releases.length || !domains.length) return null;
  const lookup = new Map(cells.map(c => [`${c.domain}|${c.release}`, c.value]));
  const max = Math.max(1, ...cells.map(c => c.value));
  const legendColors = theme === 'dark'
    ? ['rgb(30,58,95)', 'rgb(58,110,200)', 'rgb(125,176,255)']
    : ['rgb(196,214,245)', 'rgb(59,118,222)', 'rgb(11,37,69)'];
  return (
    <div className="g-heatmap-scroll">
      <table className="g-heatmap">
        <thead>
          <tr>
            <th className="domain-head">Domain / Release</th>
            {releases.map(r => <th key={r}>{r}</th>)}
          </tr>
        </thead>
        <tbody>
          {domains.map(d => (
            <tr key={d}>
              <td className="domain-label" title={d}>{truncate(d, 22)}</td>
              {releases.map(r => {
                const v = lookup.get(`${d}|${r}`) ?? 0;
                const bg = heatColor(v, max, theme);
                return (
                  <td key={r}>
                    <div className={`g-heat-cell${bg ? '' : ' empty'}`} style={bg ? { background: bg } : undefined} title={`${d} × ${r}: ${v} SEPs`}>
                      {v || '—'}
                    </div>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="g-heat-legend" style={{ marginTop: 10 }}>
        Low
        <span className="g-heat-legend-swatch" style={{ background: legendColors[0] }} />
        <span className="g-heat-legend-swatch" style={{ background: legendColors[1] }} />
        <span className="g-heat-legend-swatch" style={{ background: legendColors[2] }} />
        High
      </div>
    </div>
  );
}

/** Per-company portfolio table (totals and essentiality counts). */
export function PortfolioTable({ rows }: { rows: { company: string; country?: string; total: number; essential: number; potential: number; non_essential: number }[] }) {
  const p = usePalette();
  if (!rows.length) return <div className="empty"><div className="empty-body">No companies match the current filters.</div></div>;
  const max = Math.max(1, ...rows.map(r => r.total));
  return (
    <div className="table-scroll is-tall">
      <table className="dtable">
        <thead>
          <tr>
            <th>Company</th><th>Country</th><th className="t-num">Records</th><th>Split</th>
            <th className="t-num">Essential</th><th className="t-num">Potential</th><th className="t-num">Non-essential</th><th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.company}>
              <td className="t-clip" style={{ fontWeight: 500 }} title={r.company}>{r.company}</td>
              <td className="t-muted"><CountryFlag code={r.country} />{r.country || '—'}</td>
              <td className="t-num">{r.total.toLocaleString()}</td>
              <td>
                <div className="g-mini-bar" style={{ width: Math.max(24, (r.total / max) * 110) }} title={`${r.essential} essential · ${r.potential} potential · ${r.non_essential} non-essential`}>
                  <span style={{ width: `${(r.essential / (r.total || 1)) * 100}%`, background: p.essential }} />
                  <span style={{ width: `${(r.potential / (r.total || 1)) * 100}%`, background: p.potential }} />
                  <span style={{ width: `${(r.non_essential / (r.total || 1)) * 100}%`, background: p.nonEssential }} />
                </div>
              </td>
              <td className="t-num" style={{ color: p.essential, fontWeight: 600 }}>{r.essential.toLocaleString()}</td>
              <td className="t-num" style={{ color: p.potential, fontWeight: 600 }}>{r.potential.toLocaleString()}</td>
              <td className="t-num" style={{ color: p.nonEssential, fontWeight: 600 }}>{r.non_essential.toLocaleString()}</td>
              <td className="nowrap"><Link className="link" to={`/bulk/company/${encodeURIComponent(r.company)}`}>Profile →</Link></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
