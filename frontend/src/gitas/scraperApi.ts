/**
 * 3GPP crawler API client (/api/scrape/*) used by the Standards Corpus page. Read calls need the
 * Data Entry role; starting, cancelling and retrying crawls/batches are Super Admin only (the
 * backend returns 403 otherwise).
 */

import axios from 'axios';
import { installAuthInterceptor } from '../platform/auth/http';

// Calls the SAME backend endpoints the (now-removed) cPARAS Scraper tab
// used -- /api/scrape/* -- per the v0.3 architectural rule against a
// second scraper implementation. This file only adds a GITAS-side client
// for those existing endpoints, plus the v0.3 retry endpoint.
const api = axios.create({ baseURL: '/api/scrape', timeout: 60000 });
installAuthInterceptor(api); // v0.5.0: an expired session returns the user to the sign-in page

/** A crawlable 3GPP series. */
export interface SeriesInfo {
  series: string;
  label: string;
  recommended: boolean;
}

/** One series crawl job with live counters (totals stay null until known). */
export interface CrawlJob {
  id: number;
  series: string;
  status: 'pending' | 'crawling' | 'indexing' | 'done' | 'failed' | 'cancelled';
  current_operation: string | null;
  files_downloaded: number;
  files_skipped: number;
  files_failed: number;
  files_discovered: number;
  total_files: number | null;
  percent: number | null;
  bytes_downloaded: number;
  chunks_indexed: number;
  current_path: string | null;
  error: string | null;
  error_code: string | null;
  retryable: boolean;
  started_at: string | null;
  completed_at: string | null;
}

/** GET /api/scrape/series. */
export async function listSeries(): Promise<SeriesInfo[]> {
  const { data } = await api.get('/series');
  return data;
}

/**
 * POST /api/scrape/start - crawl (and index) series; already-running series are reported, not
 * duplicated.
 */
export async function startCrawl(series: string[], maxFiles?: number): Promise<{
  message: string;
  jobs: { id: number; series: string }[];
  already_running: { id: number; series: string; status: string }[];
}> {
  const { data } = await api.post('/start', { series, max_files: maxFiles ?? null });
  return data;
}

/** GET /api/scrape/jobs. */
export async function listCrawlJobs(): Promise<{ jobs: CrawlJob[] }> {
  const { data } = await api.get('/jobs');
  return data;
}

/** GET /api/scrape/jobs/{id}. */
export async function getCrawlJob(id: number): Promise<CrawlJob> {
  const { data } = await api.get(`/jobs/${id}`);
  return data;
}

/** POST /api/scrape/jobs/{id}/retry - resume a failed/cancelled crawl. */
export async function retryJob(id: number): Promise<{ message: string; job_id?: number; existing_job_id?: number; series: string }> {
  const { data } = await api.post(`/jobs/${id}/retry`);
  return data;
}

/** POST /api/scrape/jobs/{id}/cancel. */
export async function cancelJob(id: number): Promise<{ message: string; job_id: number }> {
  const { data } = await api.post(`/jobs/${id}/cancel`);
  return data;
}

/** One crawled file of a job. */
export interface CrawlFile {
  id: number;
  filename: string;
  relative_path: string;
  series: string;
  status: 'queued' | 'downloading' | 'downloaded' | 'skipped' | 'failed';
  bytes_downloaded: number | null;
  file_size: number | null;
  retry_count: number;
  error_code: string | null;
  error_message: string | null;
  checksum: string | null;
  indexed: boolean;
  chunks_indexed: number;
  started_at: string | null;
  completed_at: string | null;
}

/** A page of crawled files with status counts. */
export interface JobFilesResponse {
  total: number;
  page: number;
  page_size: number;
  counts_by_status: Record<string, number>;
  files: CrawlFile[];
}

/** GET /api/scrape/jobs/{id}/files - filtered, sorted, paged file list. */
export async function listJobFiles(
  jobId: number,
  opts: { status?: string; search?: string; sort?: string; page?: number; pageSize?: number } = {},
): Promise<JobFilesResponse> {
  const { data } = await api.get(`/jobs/${jobId}/files`, {
    params: {
      status: opts.status || undefined,
      search: opts.search || undefined,
      sort: opts.sort || 'filename',
      page: opts.page ?? 0,
      page_size: opts.pageSize ?? 25,
    },
  });
  return data;
}

// ── Batches (v0.3 Part C) ────────────────────────────────────────────────────

export interface BatchChild extends CrawlJob {}

/** A multi-series crawl batch with aggregated progress. */
export interface CorpusBatch {
  id: number;
  status: 'queued' | 'running' | 'completed' | 'completed_with_errors' | 'failed' | 'cancelled';
  series_requested: string[];
  max_concurrent: number;
  series_completed: number;
  series_total: number;
  series_failed: number;
  files_processed: number;
  files_total: number | null;
  chunks_indexed: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  children: BatchChild[];
}

/** POST /api/scrape/batches. */
export async function startBatch(series: string[], maxConcurrent?: number): Promise<{ batch_id: number; child_job_ids: number[] }> {
  const { data } = await api.post('/batches', { series, max_concurrent: maxConcurrent ?? null });
  return data;
}

/** GET /api/scrape/batches. */
export async function listBatches(): Promise<{ batches: CorpusBatch[] }> {
  const { data } = await api.get('/batches');
  return data;
}

/** GET /api/scrape/batches/{id}. */
export async function getBatch(id: number): Promise<CorpusBatch> {
  const { data } = await api.get(`/batches/${id}`);
  return data;
}

/** POST /api/scrape/batches/{id}/cancel. */
export async function cancelBatch(id: number): Promise<{ message: string; batch_id: number }> {
  const { data } = await api.post(`/batches/${id}/cancel`);
  return data;
}

/** POST /api/scrape/batches/{id}/retry. */
export async function retryBatch(id: number): Promise<{ message: string; batch_id: number }> {
  const { data } = await api.post(`/batches/${id}/retry`);
  return data;
}
