/**
 * 3GPP retrieval index (tgpp-sqlite-retrieval output) — /api/retrieval-index/*.
 * The backend reads <project>/dist/retrieval_data (or CPARAS_RETRIEVAL_DATA); the browser never sends a path.
 */
import axios from 'axios';
import { installAuthInterceptor } from './auth/http';

const api = axios.create({ baseURL: '/api/retrieval-index', timeout: 120_000 });
installAuthInterceptor(api); // v0.5.0: an expired session returns the user to the sign-in page

/** One expected file of the index folder. */
export interface IndexFile { name: string; required: boolean; present: boolean; size_bytes: number | null }
/** A reason the index is not usable. */
export interface IndexProblem { code: string; message: string }

/** GET /api/retrieval-index/status. */
export interface RetrievalIndexStatus {
  configured: boolean;
  ok: boolean;
  problems: IndexProblem[];
  env_var: string;
  explicit_setting: string | null;
  using_default_folder: boolean;
  default_dir: string;
  expected_dir: string;
  found_dir: string | null;
  files: IndexFile[];
  dir?: string;
  schema_version?: string | null;
  search_rows?: number;
  documents?: number;
  specification_versions?: number;
  distinct_spec_ids?: number | null;
  clauses?: number;
  full_text_rows?: number;
  content_types?: Record<string, number> | null;
  counts_from?: 'manifest' | 'database';
  database_size_bytes?: number;
  build_timestamp?: string | null;
  builder_version?: string | null;
  full_specification_text_available?: boolean | null;
  text_files_linked_to_documents?: number | null;
  text_files_unlinked?: number | null;
  deep_check?: { compatible: boolean; checksum_ok: boolean | null; problems: string[] };
}

/** One keyword-search result. */
export interface IndexHit {
  record_id: string;
  spec_id: string | null;
  spec_title: string | null;
  doc_type: string | null;
  version: string | null;
  release: string | null;
  clause_id: string | null;
  clause_title: string | null;
  content_type: string;
  excerpt: string;
  score: number;
  document_id?: string | null;
  source_path?: string | null;
}

/** Index status; `deep` also verifies schema and checksums (slow on large indexes). */
export async function getRetrievalIndexStatus(deep = false): Promise<RetrievalIndexStatus> {
  const r = await api.get('/status', { params: deep ? { deep: true } : {}, timeout: deep ? 3_600_000 : 120_000 });
  return r.data;
}

/** GET /api/retrieval-index/search - keyword search with optional spec/release filters. */
export async function searchRetrievalIndex(q: string, opts: { spec?: string; release?: string; mode?: 'all' | 'any' | 'phrase'; limit?: number } = {}) {
  const params: Record<string, string | number> = { q, mode: opts.mode ?? 'all', limit: opts.limit ?? 20 };
  if (opts.spec) params.spec = opts.spec;
  if (opts.release) params.release = opts.release;
  const r = await api.get('/search', { params });
  return r.data as { query: string; mode: string; count: number; results: IndexHit[] };
}

/** Human-readable byte size. */
export function formatBytes(n?: number | null): string {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

/** Message from a retrieval-index API error (structured {code, message} or string detail). */
export function errorMessage(e: any): string {
  const d = e?.response?.data?.detail;
  if (d && typeof d === 'object' && d.message) return d.message;
  if (typeof d === 'string') return d;
  return e?.message ?? 'Request failed';
}
