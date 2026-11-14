"""
Ollama runtime planning — keep the model on the GPU (v0.4.2)
=============================================================
Ollama decides per model load how many layers go to the GPU, from the
model's weights PLUS the KV cache for the requested context window
(`num_ctx`). Every request used to ask for `OLLAMA_NUM_CTX` (24576) tokens
regardless of model or card, which on a laptop / external RTX with 4–6 GB:

  * pushed most layers of qwen2.5:3b to the CPU ("GPU not used"), and
  * made phi3.5 (no grouped-query attention: ~384 KiB of KV cache per
    token, i.e. ~9 GiB at 24576 tokens) fail to load at all — Ollama
    answers HTTP 500 ("model requires more system memory ..." / "llama
    runner process has terminated").

This module sizes `num_ctx` per model from the model's real architecture
(GET /api/show) and the detected VRAM so weights + KV cache + compute
buffer fit on the GPU, clamped to the model's own maximum context and to
OLLAMA_NUM_CTX. Without a GPU it caps the context to what system RAM can
hold, so a CPU-only machine gets a slower run instead of an HTTP 500.

The plan is a pure function of (model, total VRAM, settings) — never of
momentary free memory — so every call for a model uses the SAME num_ctx:
Ollama keeps the model loaded instead of reloading it, the layer split is
the same on every run, and results are reproducible.

`describe_loaded_models` reads GET /api/ps, Ollama's own report of how much
of each loaded model is in VRAM — the ground truth the UI shows.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from core.config import settings
from core.gpu_detect import detect_gpu_cached
from core.ollama_client import normalize_model_name, ollama_http_client

logger = logging.getLogger(__name__)

GiB = 1024 ** 3
# CUDA context, cuBLAS workspace, output buffers — roughly constant per load.
_FIXED_OVERHEAD_BYTES = int(0.5 * GiB)
# llama.cpp micro-batch used for prompt processing (Ollama default num_batch).
_UBATCH = 512
_KV_ELEM_BYTES = {"f16": 2.0, "fp16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}
# Each request needs at least this much context to be useful; chunk sizes
# in the analysis routes are scaled from the planned value.
_HARD_MIN_CTX = 4096

_profile_cache: dict[tuple[str, str], dict] = {}


def _kv_elem_bytes() -> float:
    """Bytes per KV-cache element for the configured OLLAMA_KV_CACHE_TYPE (f16 = 2, q8_0 = 1, q4_0 =
    0.5).
    """
    return _KV_ELEM_BYTES.get(str(getattr(settings, "OLLAMA_KV_CACHE_TYPE", "f16")).lower(), 2.0)


def _arch_value(info: dict, arch: str, key: str):
    """Value of an architecture-specific key from Ollama's /api/show model_info (e.g.
    'llama.block_count'), tolerating GGUFs that omit the architecture prefix.
    """
    v = info.get(f"{arch}.{key}")
    if v is None:  # some GGUFs omit the arch prefix in /api/show
        for k, val in info.items():
            if k.endswith("." + key):
                return val
    return v


def _layer_sum(v, layers: int) -> Optional[float]:
    """head_count_kv may be one int or a per-layer list (e.g. gemma)."""
    if v is None:
        return None
    if isinstance(v, list):
        nums = [x for x in v if isinstance(x, (int, float))]
        return float(sum(nums)) if nums else None
    try:
        return float(v) * layers
    except (TypeError, ValueError):
        return None


def profile_from_show(show: dict, size_bytes: Optional[int]) -> dict:
    """Pure: derive the numbers the planner needs from /api/show output."""
    info = show.get("model_info") or {}
    arch = info.get("general.architecture") or ""
    layers = _arch_value(info, arch, "block_count")
    heads = _arch_value(info, arch, "attention.head_count")
    heads_kv = _arch_value(info, arch, "attention.head_count_kv")
    embd = _arch_value(info, arch, "embedding_length")
    k_len = _arch_value(info, arch, "attention.key_length")
    v_len = _arch_value(info, arch, "attention.value_length")
    ctx_max = _arch_value(info, arch, "context_length")

    if isinstance(heads, list):
        heads = max([h for h in heads if isinstance(h, (int, float))] or [0]) or None
    kv_elems_per_token = None
    try:
        layers_i = int(layers) if layers else 0
        if layers_i and heads and embd:
            head_dim = float(embd) / float(heads)
            k = float(k_len) if k_len else head_dim
            v = float(v_len) if v_len else head_dim
            kv_heads_total = _layer_sum(heads_kv if heads_kv is not None else heads, layers_i)
            if kv_heads_total:
                kv_elems_per_token = kv_heads_total * (k + v)
    except (TypeError, ValueError, ZeroDivisionError):
        kv_elems_per_token = None
    return {
        "architecture": arch or None,
        "layers": int(layers) if isinstance(layers, (int, float)) else None,
        "head_count": int(heads) if isinstance(heads, (int, float)) else None,
        "kv_elems_per_token": kv_elems_per_token,
        "context_length": int(ctx_max) if isinstance(ctx_max, (int, float)) and ctx_max > 0 else None,
        "size_bytes": int(size_bytes) if size_bytes else None,
    }


async def get_model_profile(base_url: str, model: str) -> dict:
    """Architecture numbers for `model` (cached per process). Never raises;
    an empty profile just means "plan conservatively"."""
    key = (base_url.rstrip("/"), normalize_model_name(model))
    if key in _profile_cache:
        return _profile_cache[key]
    profile: dict = {"kv_elems_per_token": None, "context_length": None, "size_bytes": None,
                     "head_count": None, "layers": None, "architecture": None}
    try:
        async with ollama_http_client(base_url, 10.0) as client:
            show = await client.post(f"{base_url}/api/show", json={"model": model})
            size = None
            tags = await client.get(f"{base_url}/api/tags")
            if tags.status_code == 200:
                want = normalize_model_name(model)
                for m in (tags.json() or {}).get("models", []):
                    if normalize_model_name(m.get("name") or m.get("model")) == want:
                        size = m.get("size")
                        break
            if show.status_code == 200:
                profile = profile_from_show(show.json() or {}, size)
                _profile_cache[key] = profile
            else:
                profile["size_bytes"] = size
    except Exception as e:
        logger.debug(f"Could not read model profile for {model}: {type(e).__name__}: {e}")
    return profile


def _system_ram_bytes() -> Optional[int]:
    """Total system RAM via psutil, or None when psutil is not installed."""
    try:
        import psutil  # optional
        return int(psutil.virtual_memory().total)
    except Exception:
        return None


def plan_context(profile: dict, gpu: dict, requested: int, *, concurrency: int = 1,
                 ram_bytes: Optional[int] = None) -> dict:
    """
    Pure planner. Returns {"num_ctx", "reason", "fits_gpu", ...}.

    GPU path: largest context (≤ requested, ≤ model max) for which
      weights + overhead + ctx * concurrency * (KV + compute) ≤ VRAM − reserve.
    If even OLLAMA_MIN_NUM_CTX does not fit, OLLAMA_MIN_NUM_CTX is used and
    Ollama offloads as many layers as fit (partial GPU) — still no crash.
    CPU path: the same formula against ~60% of system RAM.
    """
    requested = max(_HARD_MIN_CTX, int(requested))
    cap = requested
    if profile.get("context_length"):
        cap = min(cap, int(profile["context_length"]))
    min_ctx = max(_HARD_MIN_CTX, min(int(getattr(settings, "OLLAMA_MIN_NUM_CTX", 8192)), cap))
    out = {"num_ctx": cap, "requested": requested, "model_max": profile.get("context_length"),
           "fits_gpu": None, "reason": "configured"}

    if not getattr(settings, "OLLAMA_FIT_CONTEXT_TO_GPU", True):
        out["reason"] = "OLLAMA_FIT_CONTEXT_TO_GPU=false"
        return out
    kv = profile.get("kv_elems_per_token")
    weights = profile.get("size_bytes")
    if not kv or not weights:
        out["reason"] = "model architecture unknown — using configured context"
        return out

    flash = bool(getattr(settings, "OLLAMA_FLASH_ATTENTION", False))
    per_token = kv * _kv_elem_bytes()
    if not flash and profile.get("head_count"):
        per_token += _UBATCH * profile["head_count"] * 4  # f32 KQ scores
    per_token *= max(1, int(concurrency or 1))

    if gpu.get("detected") and gpu.get("vram_gb"):
        reserve = float(getattr(settings, "GPU_VRAM_RESERVE_GB", 0.8)) * GiB
        budget = float(gpu["vram_gb"]) * GiB - reserve - weights - _FIXED_OVERHEAD_BYTES
        target = "GPU"
    elif ram_bytes:
        budget = ram_bytes * 0.6 - weights - _FIXED_OVERHEAD_BYTES
        target = "RAM"
    else:
        out["reason"] = "no GPU detected and RAM unknown — using configured context"
        return out

    fit = int(budget // per_token) if budget > 0 else 0
    fit = (fit // 1024) * 1024
    out["fit_tokens"] = fit
    if fit >= cap:
        out.update(num_ctx=cap, fits_gpu=(target == "GPU"),
                   reason=f"model + {cap}-token context fit in {target}")
    elif fit >= min_ctx:
        out.update(num_ctx=fit, fits_gpu=(target == "GPU"),
                   reason=f"context reduced {cap} → {fit} so the model fits entirely in {target}")
    else:
        out.update(num_ctx=min_ctx, fits_gpu=False,
                   reason=(f"only ~{max(fit, 0)} tokens of context fit in {target} next to the weights; "
                           f"using {min_ctx} (Ollama will split layers between GPU and CPU)"
                           if target == "GPU" else
                           f"limited RAM: using {min_ctx}-token context"))
    return out


_last_logged: dict[str, int] = {}


async def plan_num_ctx(model: str, requested: Optional[int] = None) -> dict:
    """The num_ctx every request for `model` should use (see module doc)."""
    base_url = settings.OLLAMA_BASE_URL.rstrip("/")
    requested = int(requested or settings.OLLAMA_NUM_CTX)
    profile = await get_model_profile(base_url, model)
    gpu = await detect_gpu_cached()
    plan = plan_context(profile, gpu, requested, concurrency=settings.OLLAMA_CONCURRENCY,
                        ram_bytes=None if gpu.get("detected") else _system_ram_bytes())
    if _last_logged.get(model) != plan["num_ctx"]:
        _last_logged[model] = plan["num_ctx"]
        logger.info(f"Context plan for {model}: num_ctx={plan['num_ctx']} ({plan['reason']}; "
                    f"GPU={gpu.get('name') or 'none'} {gpu.get('vram_gb') or ''} GB)")
    return plan


def chunk_chars_for_ctx(num_ctx: int, base_chars: int = 20000, base_ctx: int = 24576,
                        floor: int = 3500) -> int:
    """Scale a chunk size tuned for `base_ctx` to the planned context, so a
    smaller GPU-fitting context never receives a prompt it would truncate."""
    if num_ctx >= base_ctx:
        return base_chars
    return max(floor, int(base_chars * num_ctx / base_ctx))


def clamp_num_predict(max_tokens: int, num_ctx: int, prompt_chars: int) -> int:
    """Output tokens that still fit in the window after the prompt
    (≈3 chars/token, conservative). Generating past num_ctx makes Ollama
    shift the context and drop the document's start."""
    est_prompt = math.ceil(prompt_chars / 3.0)
    room = num_ctx - est_prompt - 32
    return max(256, min(int(max_tokens), room))


async def describe_loaded_models(base_url: Optional[str] = None) -> dict:
    """GET /api/ps: which models are loaded and how much of each is in VRAM."""
    base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
    try:
        async with ollama_http_client(base_url, 5.0) as client:
            r = await client.get(f"{base_url}/api/ps")
            if r.status_code != 200:
                return {"available": False, "models": [], "error": f"/api/ps answered HTTP {r.status_code}"}
            items = (r.json() or {}).get("models") or []
    except Exception as e:
        return {"available": False, "models": [], "error": f"{type(e).__name__}: {e}"}
    models = []
    for m in items:
        size = int(m.get("size") or 0)
        vram = int(m.get("size_vram") or 0)
        pct = round(100.0 * vram / size) if size else 0
        models.append({
            "name": m.get("name") or m.get("model"),
            "size_gb": round(size / GiB, 2),
            "vram_gb": round(vram / GiB, 2),
            "gpu_percent": pct,
            "processor": ("100% GPU" if pct >= 99 else "100% CPU" if pct <= 0 else f"{pct}% GPU / {100 - pct}% CPU"),
            "context_length": m.get("context_length"),
            "expires_at": m.get("expires_at"),
        })
    return {"available": True, "models": models, "error": None}
