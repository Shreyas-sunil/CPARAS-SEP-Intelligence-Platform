"""
Memory planning for transformer embedding (no torch import - pure arithmetic, unit-testable).

WHY THIS EXISTS (v0.4.4.1)
------------------------
A BERT/XLM-R encoder such as BAAI/bge-m3 (24 layers, hidden 1024, 16 heads, FFN 4096, up to 8,192
tokens) materialises, per layer, an attention-score tensor of shape  [batch, heads, seq, seq]  in
float32.  Its size is  batch x heads x seq^2 x 4 bytes  - QUADRATIC in sequence length:

    batch 32, seq 2048:  32 x 16 x 2048^2 x 4  =  8,589,934,592 bytes  (the "8.59 GB" in the Windows log)
    batch  2, seq 8192:   2 x 16 x 8192^2 x 4  =  8,589,934,592 bytes
    batch  1, seq 8192:                         =  4,294,967,296 bytes  (per single sequence)
    batch 32, seq  512:                         =    536,870,912 bytes

sentence-transformers pads every text in a batch to the longest one, so ONE unbroken oversized
chunk makes the whole batch of 32 pay for it.  Planning batches from token lengths, and splitting
oversized chunks beforehand, keeps the working set bounded whatever the document contains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

FLOAT_BYTES = 4  # the model runs in float32 on CPU


@dataclass(frozen=True)
class ModelShape:
    """Transformer dimensions used to estimate the working memory of one encode call (defaults = bge-m3
    / XLM-R large).
    """
    layers: int = 24
    hidden: int = 1024
    heads: int = 16
    ffn: int = 4096


BGE_M3 = ModelShape()


class SequenceTooLarge(RuntimeError):
    """A single sequence cannot fit the memory budget even alone (batch of 1)."""

    def __init__(self, tokens: int, needed_bytes: int, budget_bytes: int):
        """A single sequence of `tokens` would need `needed_bytes` of working memory, more than the
        `budget_bytes` available.
        """
        self.tokens, self.needed_bytes, self.budget_bytes = tokens, needed_bytes, budget_bytes
        super().__init__(
            f"a single {tokens}-token sequence needs about {needed_bytes / 2**20:.0f} MB of working memory, "
            f"above the {budget_bytes / 2**20:.0f} MB budget")


def attention_scores_bytes(batch: int, seq_len: int, heads: int = 16) -> int:
    """Size of ONE attention-score tensor [batch, heads, seq, seq] in float32."""
    return batch * heads * seq_len * seq_len * FLOAT_BYTES


def estimate_peak_bytes(batch: int, seq_len: int, shape: ModelShape = BGE_M3) -> int:
    """Conservative peak working memory of one forward pass (weights NOT included).

    Counts the score tensor AND its softmax output (both alive at once in eager attention) plus the
    per-token activations (q/k/v/context/residual/layer-norm copies ~ 6 x hidden, and the FFN
    intermediate plus its activation ~ 2 x ffn).  Deliberately an over-estimate: an estimate that
    is too high only makes batches smaller; one that is too low is what crashed the import.
    """
    batch, seq_len = max(1, int(batch)), max(1, int(seq_len))
    scores = 2 * attention_scores_bytes(batch, seq_len, shape.heads)
    acts = batch * seq_len * FLOAT_BYTES * (6 * shape.hidden + 2 * shape.ffn)
    return scores + acts


def plan_batches(lengths: Sequence[int], max_batch: int, budget_bytes: int,
                 shape: ModelShape = BGE_M3, strict: bool = True) -> list[list[int]]:
    """Group text indices into batches whose estimated peak fits ``budget_bytes``.

    ``lengths`` are token counts (already capped at the model limit).  Texts are sorted
    longest-first so each batch pads to its first member's length; a batch grows while it still
    fits.  Returns lists of ORIGINAL indices.

    ``strict=True``: raises SequenceTooLarge when even one text does not fit the budget.
    ``strict=False``: such a text gets a batch of its own and is simply attempted (the caller's
    out-of-memory handling decides) - used for searches and bulk scoring, which must keep working
    for long inputs exactly as before.
    """
    max_batch = max(1, int(max_batch))
    order = sorted(range(len(lengths)), key=lambda i: -lengths[i])
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_len = 0
    for i in order:
        L = max(1, int(lengths[i]))
        if not cur:
            need = estimate_peak_bytes(1, L, shape)
            if strict and need > budget_bytes:
                raise SequenceTooLarge(L, need, budget_bytes)
            cur, cur_len = [i], L          # sorted descending: the first member is the batch's padded length
            continue
        if len(cur) < max_batch and estimate_peak_bytes(len(cur) + 1, cur_len, shape) <= budget_bytes:
            cur.append(i)
        else:
            batches.append(cur)
            need = estimate_peak_bytes(1, L, shape)
            if strict and need > budget_bytes:
                raise SequenceTooLarge(L, need, budget_bytes)
            cur, cur_len = [i], L
    if cur:
        batches.append(cur)
    return batches


def resolve_budget_bytes(setting_mb: int, free_ram_mb: Optional[float]) -> int:
    """EMBEDDING_MEMORY_BUDGET_MB -> bytes.  0 = auto: 25% of free RAM, clamped to 512..4096 MB."""
    if setting_mb and setting_mb > 0:
        mb = float(setting_mb)
        if free_ram_mb is not None:
            mb = min(mb, max(256.0, free_ram_mb * 0.6))   # never plan past what is actually free
        return int(mb * 2**20)
    free = free_ram_mb if free_ram_mb is not None else 2048.0
    return int(min(4096.0, max(512.0, free * 0.25)) * 2**20)


_MEMORY_MARKERS = (
    "not enough memory", "can't allocate memory", "cannot allocate memory", "out of memory",
    "defaultcpuallocator", "alloc_cpu", "bad_alloc", "memoryerror", "unable to allocate",
    "paging file is too small", "insufficient memory", "failed to allocate",
)


def is_memory_error(exc: BaseException) -> bool:
    """True for the many shapes an out-of-memory condition takes (Python, PyTorch CPU/CUDA, numpy,
    Windows 'paging file' errors, native std::bad_alloc surfaced as RuntimeError)."""
    if isinstance(exc, MemoryError):
        return True
    if type(exc).__name__ in ("OutOfMemoryError", "MemoryError"):
        return True
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(m in msg for m in _MEMORY_MARKERS)


def model_shape_from(model) -> ModelShape:
    """Read layers/hidden/heads/ffn from a loaded SentenceTransformer when possible, else bge-m3."""
    try:
        cfg = model[0].auto_model.config
        return ModelShape(
            layers=int(getattr(cfg, "num_hidden_layers", BGE_M3.layers)),
            hidden=int(getattr(cfg, "hidden_size", BGE_M3.hidden)),
            heads=int(getattr(cfg, "num_attention_heads", BGE_M3.heads)),
            ffn=int(getattr(cfg, "intermediate_size", BGE_M3.ffn)),
        )
    except Exception:  # noqa: BLE001 - any model without that layout
        return BGE_M3


def free_ram_mb() -> Optional[float]:
    """Currently available system RAM in MB, or None when it cannot be determined."""
    try:
        import psutil
        return psutil.virtual_memory().available / 2**20
    except Exception:  # noqa: BLE001
        return None


def process_rss_mb() -> Optional[float]:
    """Resident memory of this process in MB (None without psutil); logged by memory-bounded
    import/indexing.
    """
    try:
        import psutil
        return psutil.Process().memory_info().rss / 2**20
    except Exception:  # noqa: BLE001
        return None
