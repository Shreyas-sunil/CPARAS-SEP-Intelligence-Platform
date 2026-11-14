/**
 * API Service
 * All HTTP calls to the local FastAPI backend.
 * No cloud APIs ever called from here.
 */

import axios from 'axios';
import { installAuthInterceptor } from '../../platform/auth/http';
import type { AnalysisResponse, BackendStatus, SearchResult, RecommendedModelResponse } from '../types';
import { normalizeLlmHealth } from '../../shared/llmHealth';

const BASE = '/api';

const api = axios.create({
  baseURL: BASE,
  // No client-side timeout: the backend's own per-chunk timeout is 1 hour
  // (OLLAMA_TIMEOUT), and a large multi-chunk declaration can legitimately
  // take longer than that in total. Use /progress/{job_id} polling (see
  // getProgress below) to show the user it's still working instead of
  // capping the request client-side.
  timeout: 0,
});
installAuthInterceptor(api); // v0.5.0: an expired session returns the user to the sign-in page

// ── Health ─────────────────────────────────────────────────────────────────────

// GET /health (backend/api/routes/shared/health.py) reports READY /
// DEGRADED / UNAVAILABLE for the backend as a whole, plus per-component
// detail. READY and DEGRADED both mean the FastAPI process and its database
// are usable ('ok' here); DEGRADED only says an optional component (usually
// the local LLM) isn't ready, which the UI reports separately and in detail
// from the `llm` object — the shared health contract (shared/llmHealth.ts).
export async function checkHealth(): Promise<BackendStatus> {
  const { data } = await api.get('/health', { timeout: 15000 });
  const raw = data?.status;
  const status: BackendStatus['status'] =
    raw === 'READY' || raw === 'DEGRADED' ? 'ok' : 'error';
  return {
    status,
    overall: raw,
    backend: data?.llm?.backend,
    llm: data?.llm ? normalizeLlmHealth(data.llm) : undefined,
  };
}

// ── Analysis ───────────────────────────────────────────────────────────────────

export interface ActivityEntry {
  text: string;
  ts: number;
}

/**
 * GET /api/analysis/progress/{job_id} payload: stage, fractions, current patent, activity log,
 * chunk/batch counters.
 */
export interface AnalysisProgress {
  job_id: string;
  stage?: 'starting' | 'extracting_text' | 'analyzing' | 'deep_enrichment' | 'verifying' | 'research' | 'persisting' | 'done' | 'error' | 'unknown';
  failed_stage?: string;
  total_files?: number;
  current_file_index?: number;
  current_filename?: string;
  chunk_index?: number | null;
  total_chunks?: number | null;
  message?: string;
  done?: boolean;
  updated_at?: number;
  // Added for the richer multi-stage progress UI — all derived from real
  // pipeline events (see _set_progress call sites in analysis.py), never
  // simulated client-side.
  model?: string;
  backend?: string;
  total_pages?: number;
  total_patents?: number;
  patents_completed?: number;
  current_patent?: string;
  activity?: ActivityEntry[];
}

function newJobId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `job-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Poll live progress for an in-flight analyzeDeclarations() call. */
export async function getProgress(jobId: string): Promise<AnalysisProgress> {
  const { data } = await api.get<AnalysisProgress>(`/analysis/progress/${jobId}`);
  return data;
}

/**
 * POST /api/analysis/analyze - upload declaration files and run the analysis (quick or deep mode,
 * optional model override). Resolves when the whole analysis is done; poll getProgress(jobId)
 * meanwhile.
 */
export async function analyzeDeclarations(
  files: File[],
  mode: 'deep' | 'quick',
  useRag: boolean,
  onProgress?: (msg: string) => void,
  jobId?: string,
  model?: string,
): Promise<AnalysisResponse> {
  const form = new FormData();
  files.forEach((f) => form.append('files', f));
  form.append('mode', mode);
  form.append('use_rag', String(useRag));
  if (jobId) form.append('job_id', jobId);
  if (model) form.append('model', model);

  onProgress?.(`Sending ${files.length} file(s) to local AI engine…`);

  const { data } = await api.post<AnalysisResponse>('/analysis/analyze', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (evt) => {
      if (evt.total) {
        const pct = Math.round((evt.loaded / evt.total) * 100);
        onProgress?.(`Uploading… ${pct}%`);
      }
    },
  });

  return data;
}

export { newJobId };

// ── Start / stop / pause / continue ──────────────────────────────────────────
// While POST /analysis/analyze is in flight for a given job_id, these
// cooperatively signal it to stop between files (see analysis.py's
// "start / stop / pause / continue" section) — the in-flight request then
// returns early with `stopped: true` and a `skipped_files` list rather
// than an error. There's no separate resume-from-checkpoint call: CPARAS
// doesn't persist partial-file state, so "continue" is re-submitting
// analyzeDeclarations() with the same jobId and only the files named in
// `skipped_files`.
export async function pauseAnalysis(jobId: string): Promise<{ job_id: string; status: string }> {
  const { data } = await api.post(`/analysis/jobs/${jobId}/pause`);
  return data;
}

/** POST /api/analysis/jobs/{id}/stop - stop after the chunk/batch in flight. */
export async function stopAnalysis(jobId: string): Promise<{ job_id: string; status: string }> {
  const { data } = await api.post(`/analysis/jobs/${jobId}/stop`);
  return data;
}

/** POST /api/analysis/jobs/{id}/continue - whether a paused job can be continued. */
export async function continueAnalysisReady(jobId: string): Promise<{ job_id: string; status: string; message: string }> {
  const { data } = await api.post(`/analysis/jobs/${jobId}/continue`);
  return data;
}

// ── History ────────────────────────────────────────────────────────────────────

export async function getHistory() {
  const { data } = await api.get('/analysis/history');
  return data;
}

/** GET /api/analysis/history/{id} - a stored declaration's full result. */
export async function getDeclaration(id: number) {
  const { data } = await api.get(`/analysis/history/${id}`);
  return data;
}

// ── Models ────────────────────────────────────────────────────────────────────

export async function getModelRecommendations() {
  const { data } = await api.get('/models/recommendations');
  return data;
}

/** GET /api/models/recommended - the model recommended for this machine's GPU. */
export async function getRecommendedModel(): Promise<RecommendedModelResponse> {
  const { data } = await api.get('/models/recommended');
  return data;
}

// Installed models are read from GET /models/status (the full health
// contract, see shared/llmHealth.ts getLlmStatus) — one source of truth.

export async function pullModel(modelId: string) {
  const { data } = await api.post('/models/pull', { model_id: modelId });
  return data;
}

/** Progress of an `ollama pull` started from the UI. */
export interface PullStatus {
  model: string;
  status: 'pulling' | 'done' | 'error' | 'unknown' | 'already_pulling';
  detail?: string | null;
  error?: string | null;
  started_at?: number;
}

/** GET /api/models/pull/status/{model}. */
export async function getPullStatus(modelId: string): Promise<PullStatus> {
  const { data } = await api.get(`/models/pull/status/${encodeURIComponent(modelId)}`);
  return data;
}

// ── Search ────────────────────────────────────────────────────────────────────

export async function semanticSearch(
  query: string,
  topK = 10,
  sourceType?: string,
): Promise<{ results: SearchResult[]; total: number }> {
  const { data } = await api.post('/search/semantic', { query, top_k: topK, source_type: sourceType });
  return data;
}

/** Standards Corpus vector-store statistics (GET /api/search/stats). */
export async function getStandardsCorpusStats() {
  const { data } = await api.get('/search/stats');
  return data;
}

// ── Datasets ─────────────────────────────────────────────────────────────────

export async function getIngestionSources() {
  const { data } = await api.get('/datasets/sources');
  return data;
}

/** POST /api/datasets/start (Super Admin) - legacy public-source ingestion. */
export async function startIngestion(sources: string[], maxRecords = 500) {
  const { data } = await api.post('/datasets/start', { sources, max_records: maxRecords });
  return data;
}

/** GET /api/datasets/jobs. */
export async function getIngestionJobs() {
  const { data } = await api.get('/datasets/jobs');
  return data;
}

// ── Text Matching ────────────────────────────────────────────────────────────

export interface TextMatchHit {
  content: string;
  score: number;
  metadata: Record<string, unknown>;
}

/** Matches for one query unit (record or text segment). */
export interface TextMatchSegmentResult {
  segment: string;
  matches: TextMatchHit[];
  // v0.4: per-record context for structured (CSV/XLSX/XLS) uploads.
  label?: string | null;
  declared_standards?: string[] | null;
  query_basis?: string | null;
}

/** Complete Text Matching result. */
export interface TextMatchResponse {
  segments: TextMatchSegmentResult[];
  corpus_indexed_chunks: number;
  warning: string | null;
  source_filename?: string | null;
  extraction_method?: string | null;
  low_confidence_pages?: number[] | null;
  // v0.3: populated for non-PDF uploads (CSV/XLSX/XLS/DOCX/TXT) -- see
  // backend api/routes/text_match.py's extract_matchable_text().
  file_format?: string | null;
  records_extracted?: number | null;
  text_source?: string | null;
  extraction_preview?: string | null;
}

/** GET /api/text-match/status. `ready` = the 3GPP Retrieval Index OR the
 * Standards Corpus vector store has content (retrieval uses whichever exists). */
export interface TextMatchStatus {
  /** Chunks in the Standards Corpus vector store. */
  indexed_chunks?: number;
  /** v0.5.0: the installed 3GPP Retrieval Index (counts only). */
  retrieval_index?: { ok: boolean; configured: boolean; documents: number | null };
  ready: boolean;
  backend?: string;
  error?: string;
}

/**
 * GET /api/text-match/status - which standards text is available (3GPP Retrieval Index documents,
 * Standards Corpus chunks).
 */
export async function getTextMatchStatus(): Promise<TextMatchStatus> {
  const { data } = await api.get('/text-match/status');
  return data;
}

/** POST /api/text-match/match - one-shot matching of pasted text. */
export async function matchText(
  text: string,
  opts: { topK?: number; minScore?: number; sourceType?: string | null } = {},
): Promise<TextMatchResponse> {
  const { data } = await api.post('/text-match/match', {
    text,
    top_k: opts.topK ?? 5,
    min_score: opts.minScore ?? 0.3,
    source_type: opts.sourceType === undefined ? '3gpp_spec_text' : opts.sourceType,
  });
  return data;
}

/** POST /api/text-match/match-file - one-shot matching of an uploaded file. */
export async function matchFile(file: File): Promise<TextMatchResponse> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await api.post('/text-match/match-file', form);
  return data;
}

// ── Text Matching jobs (progress + pause / resume / stop) ───────────────────
// Matching runs as a backend job: start it, poll getTextMatchJob() for
// progress and partial results, and pause / resume / stop it. Control is
// applied between records/segments, and Stop keeps the results so far.

export type TextMatchJobStatus = 'queued' | 'extracting' | 'running' | 'paused' | 'stopped' | 'completed' | 'failed';

/** A pausable Text Matching job snapshot (status, progress, partial result). */
export interface TextMatchJob {
  job_id: string;
  status: TextMatchJobStatus;
  total: number;
  done: number;
  error: string | null;
  elapsed_s: number;
  result: TextMatchResponse;
}

/** POST /api/text-match/jobs/text - start matching pasted text as a job. */
export async function startTextMatchJob(text: string): Promise<TextMatchJob> {
  // source_type null = shared retrieval: the 3GPP Retrieval Index when installed, else the Standards Corpus.
  const { data } = await api.post('/text-match/jobs/text', { text, top_k: 5, min_score: 0.3, source_type: null });
  return data;
}

/** POST /api/text-match/jobs/file - start matching an uploaded file as a job. */
export async function startFileMatchJob(file: File): Promise<TextMatchJob> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await api.post('/text-match/jobs/file', form);
  return data;
}

/** GET /api/text-match/jobs/{id} - progress and results so far. */
export async function getTextMatchJob(jobId: string): Promise<TextMatchJob> {
  const { data } = await api.get(`/text-match/jobs/${jobId}`);
  return data;
}

/** POST /api/text-match/jobs/{id}/{pause|resume|stop}. */
export async function controlTextMatchJob(jobId: string, action: 'pause' | 'resume' | 'stop'): Promise<{ status: TextMatchJobStatus }> {
  const { data } = await api.post(`/text-match/jobs/${jobId}/${action}`);
  return data;
}

// ── Scraper ──────────────────────────────────────────────────────────────────

export interface SeriesInfo {
  series: string;
  label: string;
  recommended: boolean;
}

/** A 3GPP crawl job (Standards Corpus page). */
export interface CrawlJob {
  id: number;
  series: string;
  status: 'pending' | 'crawling' | 'indexing' | 'done' | 'failed';
  files_downloaded: number;
  files_skipped: number;
  files_failed: number;
  bytes_downloaded: number;
  chunks_indexed: number;
  current_path: string | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

/** GET /api/scrape/series. */
export async function listSeries(): Promise<SeriesInfo[]> {
  const { data } = await api.get('/scrape/series');
  return data;
}

/** POST /api/scrape/start (Super Admin). */
export async function startCrawl(series: string[], maxFiles?: number): Promise<{ jobs: { id: number; series: string }[] }> {
  const { data } = await api.post('/scrape/start', { series, max_files: maxFiles ?? null });
  return data;
}

/** GET /api/scrape/jobs. */
export async function listCrawlJobs(): Promise<{ jobs: CrawlJob[] }> {
  const { data } = await api.get('/scrape/jobs');
  return data;
}
