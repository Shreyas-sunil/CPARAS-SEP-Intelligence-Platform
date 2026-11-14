/**
 * Local-LLM status panel used in CPARAS ISLD (server reachable, models installed, selected model,
 * generation tested).
 */

import { useCallback, useEffect, useState } from 'react';
import { Loader2, Zap } from 'lucide-react';
import type { GenerationState, GpuStatus, LlmHealth } from './llmHealth';
import { apiErrorMessage, getGpuStatus, sameModel, testGeneration } from './llmHealth';

type Tone = 'ok' | 'warn' | 'err' | 'muted';
const DOT: Record<Tone, string> = { ok: 'var(--c-ok)', warn: 'var(--c-warn)', err: 'var(--c-err)', muted: 'var(--c-muted-2)' };

/** One labelled status line. */
function Row({ label, value, tone, title }: { label: string; value: React.ReactNode; tone: Tone; title?: string }) {
  return (
    <div className="llm-row" title={title}>
      <span className="status-dot" style={{ background: DOT[tone] }} aria-hidden="true" />
      <span className="llm-row-label">{label}</span>
      <span className="llm-row-value">{value}</span>
    </div>
  );
}

/**
 * The four questions answered separately, never collapsed into one light:
 *   Ollama server reachable → installed models → selected model installed → generation works.
 * "Generation: Ready" is only ever shown after a real generation succeeded
 * (the explicit test below, or a completed analysis) — never inferred from
 * the model list answering.
 */
export default function LlmStatusPanel({ llm, selectedModel, loading, onGenerationChecked }: {
  llm: LlmHealth | null;
  selectedModel: string;
  loading?: boolean;
  onGenerationChecked?: (g: GenerationState) => void;
}) {
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [localGen, setLocalGen] = useState<GenerationState | null>(null);
  const [gpuStatus, setGpuStatus] = useState<GpuStatus | null>(null);

  const isOllamaBackend = llm?.backend === 'ollama';
  const refreshGpu = useCallback(async () => {
    if (!isOllamaBackend) return;
    try { setGpuStatus(await getGpuStatus(selectedModel || undefined)); } catch { /* best effort */ }
  }, [isOllamaBackend, selectedModel]);
  useEffect(() => { void refreshGpu(); }, [refreshGpu]);

  if (!llm) {
    return <div className="llm-panel"><Row label="Local LLM" value={loading ? 'Checking…' : 'Unknown'} tone="muted" /></div>;
  }

  const isOllama = llm.backend === 'ollama';
  const selectedInstalled = !!selectedModel && llm.text_models.includes(selectedModel);
  // Prefer the most recent result for THE SELECTED model.
  const gen = [localGen, llm.generation].find(g => g && g.model === selectedModel) ?? null;

  const runTest = async () => {
    setTesting(true);
    setTestError(null);
    try {
      const g = await testGeneration(selectedModel);
      setLocalGen(g);
      onGenerationChecked?.(g);
      void refreshGpu();
    } catch (e) {
      setTestError(apiErrorMessage(e, 'Generation test failed'));
    } finally {
      setTesting(false);
    }
  };

  const serverLabel = isOllama ? 'Ollama server' : `${llm.backend} server`;
  return (
    <div className="llm-panel" aria-live="polite">
      <Row label={serverLabel} tone={llm.reachable ? 'ok' : 'err'}
        title={llm.base_url ?? undefined}
        value={llm.reachable
          ? <>Connected{llm.version ? <span className="muted"> · v{llm.version}</span> : null}</>
          : llm.state === 'not_ollama' ? 'Port answered, but not Ollama' : 'Not reachable'} />
      {llm.reachable && (
        <Row label="Installed models" tone={llm.text_models.length ? 'ok' : 'err'}
          title={llm.models.map(m => `${m.name} (${m.kind})`).join('\n')}
          value={<>{llm.text_models.length} text{llm.vision_models.length ? <span className="muted"> · {llm.vision_models.length} vision (OCR only)</span> : null}</>} />
      )}
      {llm.reachable && (
        <Row label="Selected model" tone={selectedInstalled ? 'ok' : 'warn'}
          value={selectedModel ? <span className="mono">{selectedModel}</span> : <span className="muted">none selected</span>} />
      )}
      {llm.reachable && selectedModel && (
        <Row label="Model available" tone={selectedInstalled ? 'ok' : 'err'} value={selectedInstalled ? 'Yes' : 'No — not installed'} />
      )}
      {llm.reachable && selectedInstalled && (
        <Row label="Generation"
          tone={gen?.status === 'ready' ? 'ok' : gen?.status === 'failed' ? 'err' : 'muted'}
          title={gen?.error ?? undefined}
          value={
            <span className="llm-gen">
              {gen?.status === 'ready' ? <>Ready{gen.latency_ms != null ? <span className="muted"> · {(gen.latency_ms / 1000).toFixed(1)} s</span> : null}</>
                : gen?.status === 'failed' ? 'Failed'
                : 'Not tested yet'}
              {isOllama && (
                <button type="button" className="btn btn-sm btn-ghost" onClick={runTest} disabled={testing}
                  title="Run a tiny (8-token) generation to confirm this model actually works. The first run also loads the model, which can take a while.">
                  {testing ? <><Loader2 size={12} className="spin" /> Testing…</> : <><Zap size={12} /> Test</>}
                </button>
              )}
            </span>
          } />
      )}
      {llm.reachable && isOllama && gpuStatus && (() => {
        const loaded = gpuStatus.ollama_runtime?.models.find(m => sameModel(m.name, selectedModel));
        const g = gpuStatus.gpu;
        const tone: Tone = loaded ? (loaded.gpu_percent >= 99 ? 'ok' : loaded.gpu_percent > 0 ? 'warn' : 'err')
          : g.detected ? 'muted' : 'warn';
        return (
          <Row label="GPU" tone={tone}
            title={[g.detected ? `${g.name} · ${g.vram_gb} GB (${g.source})` : `No GPU detected: ${g.error ?? 'unknown reason'}`,
              gpuStatus.plan?.reason ? `Context: ${gpuStatus.plan.num_ctx} tokens — ${gpuStatus.plan.reason}` : ''].filter(Boolean).join('\n')}
            value={loaded
              ? <>{loaded.processor}<span className="muted"> · {loaded.vram_gb}/{loaded.size_gb} GB{gpuStatus.plan?.num_ctx ? ` · ctx ${gpuStatus.plan.num_ctx}` : ''}</span></>
              : g.detected ? <>{g.name}<span className="muted"> · {g.vram_gb} GB · model not loaded</span></>
              : 'No NVIDIA GPU detected'} />
        );
      })()}
      {gen?.status === 'failed' && gen.error && <div className="note" style={{ color: 'var(--c-err)' }}>{gen.error}</div>}
      {testError && <div className="note" style={{ color: 'var(--c-err)' }}>{testError}</div>}
      {!llm.reachable && (llm.error || llm.hint) && (
        <div className="note" style={{ color: 'var(--c-err)' }}>
          {llm.error} {llm.hint && <span className="muted">{llm.hint}</span>}
        </div>
      )}
      {llm.reachable && !llm.text_models.length && (
        <div className="note" style={{ color: 'var(--c-err)' }}>
          No text model is installed{llm.vision_models.length ? ' (only vision models, which are used for OCR only)' : ''}. Install one, e.g. <code>ollama pull qwen2.5:7b</code>
        </div>
      )}
      {llm.reachable && llm.configured_model && !llm.configured_model_available && llm.text_models.length > 0 && (
        <div className="note" style={{ color: 'var(--c-warn)' }}>
          The server default model <code>{llm.configured_model}</code> (OLLAMA_MODEL) is not installed — choose an installed model above, or run <code>ollama pull {llm.configured_model}</code>.
        </div>
      )}
    </div>
  );
}
