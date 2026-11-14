/**
 * Displays the active LLM model + runtime. Used in several places (analysis
 * header, activity feed, results dashboard) so the model name is always
 * sourced from the same prop the caller passes in — never hardcoded —
 * keeping every occurrence in sync with whatever is actually selected/used.
 */
import { Cpu } from 'lucide-react';

/** Which local model and backend an analysis is running on. */
export default function ModelBadge({
  model,
  backend = 'Ollama',
  size = 'md',
}: {
  model?: string | null;
  backend?: string | null;
  size?: 'sm' | 'md';
}) {
  if (!model) return null;
  const small = size === 'sm';
  return (
    <span
      className="model-badge"
      style={{
        fontSize: small ? 10 : 12,
        padding: small ? '3px 8px' : '5px 12px',
      }}
    >
      <span className="model-badge-icon"><Cpu size={small ? 11 : 13} /></span>
      <span className="model-badge-name">{model}</span>
      {backend && <span className="model-badge-runtime">{backend}</span>}
    </span>
  );
}
