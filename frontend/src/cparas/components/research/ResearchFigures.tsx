/**
 * Renders the publication-style figures (Requirements 7 & 10) as an image
 * gallery. Figures are pre-rendered server-side (matplotlib, Agg backend)
 * and delivered as base64 PNGs — no charting library needed client-side.
 */
import type { ResearchFigure } from '../../types';

const FIGURE_ORDER = [
  'patent_similarity_scatter',
  'standard_clustering',
  'patent_topic_heatmap',
  'similarity_matrix',
  'patent_standard_network',
  'standards_timeline',
  'version_evolution',
  'confidence_distribution',
  'standard_frequency',
  'version_distribution',
];

/** Grid of the backend-rendered research figures (PNG data URIs) with captions. */
export default function ResearchFigures({ figures }: { figures: Record<string, ResearchFigure> | null }) {
  if (!figures) return null;
  const keys = [...FIGURE_ORDER.filter(k => k in figures), ...Object.keys(figures).filter(k => !FIGURE_ORDER.includes(k))];
  const valid = keys.filter(k => figures[k]?.image);
  if (!valid.length) return null;

  return (
    <div className="fig-grid">
      {valid.map(k => {
        const fig = figures[k];
        return (
          <div key={k} className="fig-card">
            <div className="fig-title">{fig.title}</div>
            <img className="fig-img" src={fig.image} alt={fig.title} loading="lazy" />
            {fig.caption && <div className="fig-caption">{fig.caption}</div>}
          </div>
        );
      })}
    </div>
  );
}
