/**
 * Local LLM health — the frontend side of the ONE health contract defined
 * in backend/core/ollama_client.py. Every status indicator in the UI reads
 * this shape; nothing re-derives "is Ollama up?" on its own.
 */
import axios from 'axios';
import { installAuthInterceptor } from '../platform/auth/http';

/** Overall local-LLM state reported by the backend. */
export type LlmState = 'ready' | 'unreachable' | 'not_ollama' | 'no_models' | 'model_missing' | 'error';
export type ModelKind = 'text' | 'vision' | 'embedding';

/** One installed model. */
export interface LlmModelInfo {
  name: string;
  kind: ModelKind;
  size?: number | null;
  parameter_size?: string | null;
  quantization?: string | null;
  family?: string | null;
}

/** Result of the last generation smoke test. */
export interface GenerationState {
  status: 'untested' | 'ready' | 'failed';
  model: string | null;
  checked_at: string | null;
  latency_ms: number | null;
  error: string | null;
  source: 'smoke_test' | 'analysis' | null;
  /** v0.4.2 — returned by POST /models/test */
  num_ctx?: number | null;
  context_plan?: string | null;
  processor?: string | null;
  gpu_percent?: number | null;
}

/** v0.4.2 — GET /models/gpu */
export interface LoadedModelInfo {
  name: string;
  size_gb: number;
  vram_gb: number;
  gpu_percent: number;
  processor: string;
  context_length?: number | null;
}
/** GPU detection result and the context size the backend will use. */
export interface GpuStatus {
  gpu: { detected: boolean; name: string | null; vram_gb: number | null; source: string | null; error?: string | null };
  ollama_runtime: { available: boolean; models: LoadedModelInfo[]; error: string | null } | null;
  plan: { num_ctx?: number; reason?: string; fits_gpu?: boolean | null; error?: string } | null;
}

/** GET /api/models/gpu - GPU, VRAM and fitted context for a model. */
export async function getGpuStatus(model?: string): Promise<GpuStatus> {
  const { data } = await http.get<GpuStatus>('/models/gpu', { params: model ? { model } : {}, timeout: 20000 });
  return data;
}

function normName(n: string | null | undefined): string {
  const s = (n || '').trim().toLowerCase();
  if (!s) return '';
  const last = s.split('/').pop() || s;
  return last.includes(':') ? s : `${s}:latest`;
}
/** Compare model names ignoring a ':latest' suffix and case. */
export function sameModel(a: string | null | undefined, b: string | null | undefined): boolean {
  return !!a && !!b && normName(a) === normName(b);
}

/** Full LLM health contract (GET /api/models/status). */
export interface LlmHealth {
  backend: string;
  base_url: string | null;
  state: LlmState;
  reachable: boolean;
  available: boolean;
  version: string | null;
  latency_ms: number | null;
  models: LlmModelInfo[];
  text_models: string[];
  vision_models: string[];
  embedding_models: string[];
  configured_model: string | null;
  configured_model_available: boolean;
  configured_model_resolved: string | null;
  configured_model_kind: ModelKind | null;
  vision_model: string | null;
  vision_model_available: boolean;
  generation: GenerationState;
  error: string | null;
  hint: string | null;
  warnings: string[];
}

const http = axios.create({ baseURL: '/api', timeout: 15000 });
installAuthInterceptor(http); // v0.5.0: an expired session returns the user to the sign-in page

/** Full LLM health contract (GET /api/models/status). */
export async function getLlmStatus(): Promise<LlmHealth> {
  const { data } = await http.get<LlmHealth>('/models/status');
  return normalizeLlmHealth(data);
}

/** Explicit, lightweight generation smoke test (POST /api/models/test). */
export async function testGeneration(model?: string): Promise<GenerationState> {
  const { data } = await http.post<GenerationState>('/models/test', { model: model || null }, { timeout: 240000 });
  return data;
}

/** Fill in any missing keys so consumers never crash on a partial payload. */
export function normalizeLlmHealth(raw: Partial<LlmHealth> | null | undefined): LlmHealth {
  const r = raw ?? {};
  return {
    backend: r.backend ?? 'ollama',
    base_url: r.base_url ?? null,
    state: (r.state as LlmState) ?? 'error',
    reachable: !!r.reachable,
    available: !!r.available,
    version: r.version ?? null,
    latency_ms: r.latency_ms ?? null,
    models: r.models ?? [],
    text_models: r.text_models ?? [],
    vision_models: r.vision_models ?? [],
    embedding_models: r.embedding_models ?? [],
    configured_model: r.configured_model ?? null,
    configured_model_available: !!r.configured_model_available,
    configured_model_resolved: r.configured_model_resolved ?? null,
    configured_model_kind: r.configured_model_kind ?? null,
    vision_model: r.vision_model ?? null,
    vision_model_available: !!r.vision_model_available,
    generation: r.generation ?? { status: 'untested', model: null, checked_at: null, latency_ms: null, error: null, source: null },
    error: r.error ?? null,
    hint: r.hint ?? null,
    warnings: r.warnings ?? [],
  };
}

/** Human-readable error from an axios failure (FastAPI puts it in `detail`). */
export function apiErrorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { status?: number; data?: { detail?: unknown } }; message?: string; code?: string };
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d: { msg?: string; loc?: unknown[] }) => `${(d.loc ?? []).slice(-1)[0] ?? 'field'}: ${d.msg ?? 'invalid'}`).join('; ');
  }
  if (!e?.response) {
    if (e?.code === 'ECONNABORTED') return `${fallback} (the request timed out)`;
    return `${fallback} — the backend could not be reached. Is it running?`;
  }
  return `${fallback} (HTTP ${e.response.status})`;
}
