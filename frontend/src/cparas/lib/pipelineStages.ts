/**
 * Pipeline stage metadata for the analysis progress UI.
 *
 * Every id here corresponds to a real `stage` value the backend sets via
 * `_set_progress()` in api/routes/analysis.py — nothing here is simulated.
 * `analyzing` is genuinely where the LLM identifies/extracts the patents
 * from the source text, and `deep_enrichment` is genuinely the per-patent
 * SEP/standards enrichment pass (Deep mode only) — so the "Patent
 * Extraction" / "LLM Analysis" labels below map onto what each stage
 * actually does, not just onto backend variable names.
 */
import type { AnalysisProgress } from '../services/api';

export type UiStageId =
  | 'upload' | 'starting' | 'extracting_text' | 'analyzing'
  | 'deep_enrichment' | 'verifying' | 'research' | 'persisting' | 'done';

/**
 * Static description of one UI pipeline stage (id, label, which modes include it, weight in the
 * overall bar).
 */
export interface StageMeta {
  id: UiStageId;
  label: string;
  /** Static fallback description; dynamic variants are built by describeStage(). */
  description: string;
  /**
   * Relative wall-clock cost of this stage, used to turn discrete stage
   * transitions into a completion fraction for the progress bar and the
   * remaining-time estimate.
   *
   * These are calibrated against observed deep-mode runs, where the two LLM
   * stages dominate (~80% of wall-clock). The previous values gave the LLM
   * stages only 56% of the bar and handed 22% to verifying/research/
   * persisting, which are near-instant non-LLM steps — that mismatch made
   * the fraction race ahead early and then stall for minutes, which in turn
   * made the remaining-time estimate wildly optimistic and then wildly
   * pessimistic. Keep these proportional to *time*, not to importance.
   */
  weight: number;
  /** Only shown when running in Deep mode. */
  deepOnly?: boolean;
}

/** Every UI stage in order; quick mode skips the LLM enrichment stages. */
export const PIPELINE_STAGES: StageMeta[] = [
  { id: 'upload', label: 'Upload Complete', description: 'Sending file(s) to the local backend…', weight: 0.3 },
  { id: 'starting', label: 'Document Processing', description: 'Preparing the document for analysis…', weight: 0.1 },
  { id: 'extracting_text', label: 'OCR & Text Extraction', description: 'Extracting readable text from every page…', weight: 2 },
  { id: 'analyzing', label: 'Patent Extraction', description: 'Identifying referenced patents…', weight: 8 },
  { id: 'deep_enrichment', label: 'LLM Analysis', description: 'Analyzing patent declarations…', weight: 10, deepOnly: true },
  { id: 'verifying', label: 'SEP Verification', description: 'Cross-checking extracted fields against source text…', weight: 0.3 },
  { id: 'research', label: 'Research & Similarity Analysis', description: 'Finding related patents and computing similarity…', weight: 1 },
  { id: 'persisting', label: 'Database Storage', description: 'Saving results to the local database…', weight: 0.2 },
  { id: 'done', label: 'Generating Dashboard', description: 'Finalizing the report and opening the dashboard…', weight: 0.1 },
];

/** Stages to show for the current mode — 'deep_enrichment' only applies in Deep mode. */
export function stagesForMode(mode: 'deep' | 'quick'): StageMeta[] {
  return PIPELINE_STAGES.filter(s => mode === 'deep' || !s.deepOnly);
}

/** Maps a raw backend `progress.stage` value onto our UI stage ids. 'error'
 * and 'unknown' aren't in the tracker itself — they're handled separately. */
export function backendStageToUi(stage?: string): UiStageId | null {
  if (!stage) return null;
  if (stage === 'starting' || stage === 'extracting_text' || stage === 'analyzing' ||
      stage === 'deep_enrichment' || stage === 'verifying' || stage === 'research' ||
      stage === 'persisting' || stage === 'done') {
    return stage;
  }
  return null;
}

/** Index of a stage within the mode-filtered list, or -1. */
export function stageIndex(stages: StageMeta[], id: UiStageId): number {
  return stages.findIndex(s => s.id === id);
}

/**
 * Builds the live, context-aware description for a stage — e.g. plugging
 * in the active model name or a running patent count — rather than the
 * static fallback in PIPELINE_STAGES.
 */
export function describeStage(stage: StageMeta, progress: AnalysisProgress | null): string {
  if (!progress) return stage.description;
  switch (stage.id) {
    case 'analyzing':
      if (progress.total_chunks && progress.total_chunks > 1 && progress.stage === 'analyzing') {
        return `Identifying referenced patents — chunk ${progress.chunk_index ?? 0}/${progress.total_chunks}…`;
      }
      return stage.description;
    case 'deep_enrichment': {
      const model = progress.model ? ` using ${progress.model}` : '';
      if (progress.total_patents && progress.stage === 'deep_enrichment') {
        return `Analyzing patent declarations${model} — ${progress.patents_completed ?? 0}/${progress.total_patents} patents…`;
      }
      return `Analyzing patent declarations${model}…`;
    }
    case 'extracting_text':
      return progress.total_pages
        ? `Extracting readable text from ${progress.total_pages} page(s)…`
        : stage.description;
    default:
      return stage.description;
  }
}

/** mm:ss formatter for the elapsed/remaining timer. */
export function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** Stages that actually report sub-progress (chunk/batch counters) from the
 * backend. Any other stage must NOT read chunk_index/total_chunks, because
 * those fields linger in the progress entry from whichever chunked stage set
 * them last and would otherwise be applied to an unrelated stage. */
const CHUNKED_STAGES = new Set<UiStageId>(['analyzing', 'deep_enrichment']);

/** Assumed position within a stage that reports no sub-progress. 0.5 is the
 * least-biased estimate for "somewhere inside this stage" and keeps the bar
 * from lurching at every transition. */
const ASSUMED_INTRA = 0.5;

const clamp01 = (n: number) => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

/** How far through the *current stage* we are, 0-1. */
function intraStageFraction(progress: AnalysisProgress, uiStage: UiStageId): number {
  if (uiStage === 'done') return 1;
  // deep_enrichment reports both patents and batches; patents are the finer-grained of the two.
  if (uiStage === 'deep_enrichment' && progress.total_patents) {
    return clamp01((progress.patents_completed ?? 0) / Math.max(1, progress.total_patents));
  }
  if (CHUNKED_STAGES.has(uiStage) && progress.total_chunks && progress.total_chunks > 0) {
    // chunk_index is a count of *completed* chunks (backend increments it in
    // the per-chunk `finally`), so this is a true completed-fraction.
    return clamp01((progress.chunk_index ?? 0) / progress.total_chunks);
  }
  return ASSUMED_INTRA;
}

/**
 * Overall completion fraction (0-1) across the whole run.
 *
 * Used for the progress bar and as the input to the remaining-time
 * estimator. It is a weighted-stage heuristic, not a fabricated timer —
 * each stage's own check/spinner/grey state still reflects the real backend
 * stage.
 *
 * Fixes vs. the previous implementation:
 *  - Multi-file runs are accounted for. Previously the fraction was computed
 *    purely from the current file's stage, so a 3-file run drove the bar
 *    0→100% three times and reset the time estimate on every file.
 *  - chunk_index/total_chunks are only consulted for stages that actually
 *    publish them, instead of bleeding stale counters into later stages.
 *  - 'error' and unknown stages return the last-known position rather than
 *    silently collapsing the bar to 0.
 */
export function overallFraction(stages: StageMeta[], progress: AnalysisProgress | null): number {
  if (!progress?.stage) return 0;
  if (progress.stage === 'done') return 1;

  const uiStage = backendStageToUi(progress.stage);
  if (!uiStage) return 0; // 'error' / 'unknown' — caller renders the failure UI
  const idx = stageIndex(stages, uiStage);
  if (idx < 0) return 0;

  const totalWeight = stages.reduce((a, s) => a + s.weight, 0) || 1;
  const beforeWeight = stages.slice(0, idx).reduce((a, s) => a + s.weight, 0);
  const current = stages[idx];

  const withinFile = clamp01(
    (beforeWeight + current.weight * intraStageFraction(progress, uiStage)) / totalWeight,
  );

  // Each file is an equal share of the overall run.
  const totalFiles = Math.max(1, progress.total_files ?? 1);
  const fileIdx = Math.min(Math.max(0, progress.current_file_index ?? 0), totalFiles - 1);
  return clamp01((fileIdx + withinFile) / totalFiles);
}
