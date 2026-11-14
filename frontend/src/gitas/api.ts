/**
 * CPARAS Bulk API client — talks to the /api/sep/* bulk-dashboard backend.
 * Kept separate from cparas/services/api.ts (CPARAS ISLD, /api/analysis/*)
 * so the two products never share request state.
 */
import axios from 'axios';
import { installAuthInterceptor } from '../platform/auth/http';

const api = axios.create({ baseURL: '/api/sep', timeout: 60000 });
installAuthInterceptor(api); // v0.5.0: an expired session returns the user to the sign-in page

/**
 * Dashboard/registry filters; each maps to a query parameter of the /api/sep endpoints (see backend
 * Filters).
 */
export interface Filters {
  run_id?: number;
  technology?: string[];
  release?: string[];
  company?: string[];
  country?: string[];
  essentiality?: string[];
  /** Free-text search over title / reference / application / publication / DIPG id. */
  q?: string;
}

function toParams(f?: Filters) {
  const params = new URLSearchParams();
  if (!f) return params;
  if (f.run_id != null) params.set('run_id', String(f.run_id));
  (f.technology ?? []).forEach(v => params.append('technology', v));
  (f.release ?? []).forEach(v => params.append('release', v));
  (f.company ?? []).forEach(v => params.append('company', v));
  (f.country ?? []).forEach(v => params.append('country', v));
  (f.essentiality ?? []).forEach(v => params.append('essentiality', v));
  if (f.q && f.q.trim()) params.set('q', f.q.trim());
  return params;
}

/** One Bulk record as listed in the registry. */
export interface Patent {
  id: number;
  run_id: number;
  /** 0-based position of the source row among the uploaded sheet's data rows. */
  source_row_index?: number | null;
  /** Spreadsheet row number of the source row (header = row 1). */
  source_row_number?: number | null;
  dipg_id?: string | null;
  row_status?: 'OK' | 'FAILED' | string | null;
  row_error?: string | null;
  project?: string | null;
  iprd_reference?: string | null;
  signature_date?: string | null;
  company?: string | null;
  disclosure_number?: string | null;
  family_id?: string | null;
  standard?: string | null;
  spec_number?: string | null;
  spec_version?: string | null;
  release?: string | null;
  technology?: string | null;
  domain?: string | null;
  patent_type?: string | null;
  application_number?: string | null;
  publication_number?: string | null;
  title?: string | null;
  illustrative_part?: string | null;
  explicit_disclosure?: boolean;
  country?: string | null;
  country_code?: string | null;
  filing_date?: string | null;
  legal_status?: string | null;
  generations?: Record<string, boolean>;
  essentiality?: 'Essential' | 'Potential' | 'Non-Essential' | null;
  confidence?: number | null;
  claim_match?: number | null;
  semantic_similarity?: number | null;
  standard_coverage?: number | null;
  matched_standard?: string | null;
  matched_clause?: string | null;
  matched_clause_title?: string | null;
  matched_domain?: string | null;
  matched_key_terms?: string[];
  matched_key_terms_count?: number;
  total_key_terms?: number;
}

/** A record with its full essentiality determination (score breakdown, matched clause, evidence). */
export interface PatentDetail extends Patent {
  family_count: number;
  spec_context: {
    spec_number?: string;
    spec_version?: string;
    total_seps?: number;
    essential?: number;
    potential?: number;
    non_essential?: number;
    families?: number | null;
  };
  essentiality_determination: {
    status: string;
    overall_confidence: number;
    score_breakdown: { claim_match: number; semantic_similarity: number; standard_coverage: number };
    disclaimer: string;
  };
  claim_to_standard_mapping: {
    patent_claim_simplified?: string;
    matched_standard_section?: string;
    match_confidence: number;
    matched_key_terms: string[];
    matched_key_terms_count: number;
    total_key_terms: number;
    summary: string;
  };
  declaration: { isld_reference?: string; note: string };
  related_patents: Patent[];
}

/** GET /api/sep/runs - every upload run, newest first. */
export const getRuns = (): Promise<{ runs: SepRun[] }> => api.get('/runs').then(r => r.data);

// v0.5+: scoring/persistence runs as a background task (see
// api/routes/gitas/dashboard.py's _process_bulk_run) -- POST /upload
// itself only parses the file and returns a run_id almost immediately.
// The generous timeout below is really just headroom for parsing a very
// large workbook client-side upload; poll getRunStatus() for scoring
// progress rather than waiting on this call.
const UPLOAD_TIMEOUT_MS = 15 * 60 * 1000;

// v0.6 start/stop/pause/continue: cooperative job control over a running
// bulk scoring run -- see _process_bulk_run's "start / stop / pause /
// continue" docstring section.
export interface SepRunStatus {
  run_id: number;
  status: 'processing' | 'paused' | 'completed' | 'completed_with_errors' | 'failed';
  filename: string;
  parsed: number;
  queued: number;
  processing: boolean;
  processed: number;
  completed: number | null;
  failed: number;
  remaining: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  run_meta: Record<string, unknown> | null;
  stoppable: boolean;
  resumable: boolean;
}

/** GET /api/sep/runs/{id}/status - live progress of a scoring run. */
export const getRunStatus = (runId: number): Promise<SepRunStatus> =>
  api.get(`/runs/${runId}/status`).then(r => r.data);
/** POST /api/sep/runs/{id}/pause. */
export const pauseRun = (runId: number) => api.post(`/runs/${runId}/pause`).then(r => r.data);
/** POST /api/sep/runs/{id}/stop - stop after the batch in flight (results so far are kept). */
export const stopRun = (runId: number) => api.post(`/runs/${runId}/stop`).then(r => r.data);
/** POST /api/sep/runs/{id}/continue - resume a paused/stopped run. */
export const continueRun = (runId: number) => api.post(`/runs/${runId}/continue`).then(r => r.data);

/**
 * POST /api/sep/upload - upload an ETSI bulk export (.xlsx/.xls/.csv); scoring runs in the
 * background.
 */
export const uploadWorkbook = (file: File, onProgress?: (pct: number) => void) => {
  const form = new FormData();
  form.append('file', file);
  return api.post('/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: UPLOAD_TIMEOUT_MS,
    onUploadProgress: (e) => {
      if (onProgress && e.total) onProgress(Math.round((e.loaded / e.total) * 100));
    },
  }).then(r => r.data);
};

/** GET /api/sep/filters - option lists for the filter sidebar (optionally of one run). */
export const getFilters = (runId?: number) =>
  api.get('/filters', { params: runId != null ? { run_id: runId } : {} }).then(r => r.data);

/** GET /api/sep/dashboard/overview - KPI tiles. */
export const getOverview = (f?: Filters) => api.get('/dashboard/overview', { params: toParams(f) }).then(r => r.data);
/** GET /api/sep/dashboard/companies - top declarants by essentiality class. */
export const getCompanies = (f?: Filters, topN = 10) =>
  api.get('/dashboard/companies', { params: (() => { const p = toParams(f); p.set('top_n', String(topN)); return p; })() }).then(r => r.data);
/** GET /api/sep/dashboard/timeline - declarations per year. */
export const getTimeline = (f?: Filters) => api.get('/dashboard/timeline', { params: toParams(f) }).then(r => r.data);
/** GET /api/sep/dashboard/countries. */
export const getCountries = (f?: Filters) => api.get('/dashboard/countries', { params: toParams(f) }).then(r => r.data);
/** GET /api/sep/dashboard/by-release. */
export const getByRelease = (f?: Filters) => api.get('/dashboard/by-release', { params: toParams(f) }).then(r => r.data);
/** GET /api/sep/dashboard/portfolio - per-company portfolio table. */
export const getPortfolio = (f?: Filters) => api.get('/dashboard/portfolio', { params: toParams(f) }).then(r => r.data);
/** GET /api/sep/dashboard/heatmap - release x domain counts. */
export const getHeatmap = (f?: Filters) => api.get('/dashboard/heatmap', { params: toParams(f) }).then(r => r.data);
export type PatentOrder = 'company' | 'source';
/** GET /api/sep/patents - registry page (ordered by company or original row order). */
export const getPatents = (f?: Filters, limit = 50, offset = 0, order: PatentOrder = 'company') =>
  api.get('/patents', { params: (() => { const p = toParams(f); p.set('limit', String(limit)); p.set('offset', String(offset)); p.set('order', order); return p; })() }).then(r => r.data);
/** GET /api/sep/patents/{id} - one record with its full determination. */
export const getPatent = (id: number | string): Promise<PatentDetail> => api.get(`/patents/${id}`).then(r => r.data);
/** GET /api/sep/entity/company/{name}. */
export const getCompanyProfile = (name: string) => api.get(`/entity/company/${encodeURIComponent(name)}`).then(r => r.data);

/** One upload run as listed on Uploads & Runs. */
export interface SepRun {
  id: number;
  filename: string;
  sheet?: string | null;
  total_rows: number;
  embedder_mode?: string | null;
  created_at: string;
  status: SepRunStatus['status'];
  processed_rows: number;
  failed_rows: number;
  detail?: string | null;
}

async function downloadBlob(url: string, fallbackName: string): Promise<void> {
  const res = await api.get(url, { responseType: 'blob' });
  const disposition: string | undefined = res.headers?.['content-disposition'];
  const match = disposition && /filename="?([^"]+)"?/.exec(disposition);
  const filename = match?.[1] || fallbackName;
  const blobUrl = window.URL.createObjectURL(res.data);
  const a = document.createElement('a');
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(blobUrl);
}

// Enriched Excel export — downstream of scoring. Streams the original
// uploaded sheet (every source row, original order and columns) with the
// CPARAS_Bulk_* enrichment columns appended (GET /api/sep/runs/{id}/export/excel).
// Filename comes from the backend's Content-Disposition header
// ("<source>_CPARAS_Bulk_enriched.xlsx").
export function downloadRunExcel(runId: number): Promise<void> {
  return downloadBlob(`/runs/${runId}/export/excel`, `run_${runId}_CPARAS_Bulk_enriched.xlsx`);
}

// Rows whose scoring failed, with their source row number and reason.
export function downloadRunFailedRows(runId: number): Promise<void> {
  return downloadBlob(`/runs/${runId}/export/failed-rows`, `cparas_bulk_run_${runId}_failed_rows.csv`);
}
