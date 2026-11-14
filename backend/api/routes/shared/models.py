"""
Models API — list available local models, and pull new ones into Ollama.
"""
import asyncio
import json
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.config import settings
from core.gpu_detect import detect_gpu
from core.llm_engine import get_llm
from core.ollama_client import (
    generation_smoke_test, normalize_model_name, ollama_http_client, validate_model_choice,
)
from core.ollama_runtime import describe_loaded_models, plan_num_ctx

logger = logging.getLogger(__name__)
router = APIRouter()

# Text/analysis models this project is tested with, by hardware tier. The
# "RECOMMENDED" entry of a tier is what GET /recommended suggests; nothing
# here is ever used automatically — the configured OLLAMA_MODEL or the
# user's explicit selection is always what runs.
MODEL_RECOMMENDATIONS = {
    "low_vram_4gb": [
        {"id": "qwen2.5:3b", "name": "Qwen2.5 3B ← RECOMMENDED", "vram_gb": 2.5, "speed": "fast", "quality": "good"},
        {"id": "phi3.5:3.8b-mini-instruct-q4_K_M", "name": "Phi-3.5 Mini (3.8B Q4)", "vram_gb": 3.0, "speed": "fast", "quality": "good"},
    ],
    "mid_range_8gb": [
        {"id": "qwen2.5:7b", "name": "Qwen2.5 7B ← RECOMMENDED", "vram_gb": 5.0, "speed": "medium", "quality": "excellent"},
        {"id": "qwen2.5:3b", "name": "Qwen2.5 3B", "vram_gb": 2.5, "speed": "fast", "quality": "good"},
    ],
    "high_end_16gb": [
        {"id": "qwen2.5:14b", "name": "Qwen2.5 14B ← RECOMMENDED", "vram_gb": 10.0, "speed": "medium", "quality": "outstanding"},
        {"id": "qwen2.5:7b", "name": "Qwen2.5 7B", "vram_gb": 5.0, "speed": "fast", "quality": "excellent"},
    ],
    "enthusiast_24gb_plus": [
        {"id": "qwen2.5:32b", "name": "Qwen2.5 32B ← RECOMMENDED", "vram_gb": 21.0, "speed": "slow", "quality": "best"},
        {"id": "qwen2.5:14b", "name": "Qwen2.5 14B", "vram_gb": 10.0, "speed": "medium", "quality": "outstanding"},
    ],
    # Vision models are only used as an OCR fallback for scanned PDFs when
    # Tesseract is not installed — never for text analysis.
    "vision_ocr_models": [
        {"id": "moondream", "name": "Moondream (vision OCR fallback)", "vram_gb": 1.7},
    ],
    "embedding_models": [
        {"id": "BAAI/bge-m3", "name": "BGE-M3 ← RECOMMENDED (multilingual, 1024-dim)", "vram_gb": 1.5, "note": "Runs via sentence-transformers, not Ollama"},
        {"id": "BAAI/bge-large-en-v1.5", "name": "BGE Large EN (English only, faster)", "vram_gb": 0.6},
    ],
}

# Upper VRAM bound (GB) for each tier in MODEL_RECOMMENDATIONS, checked in
# order — the first tier whose bound the detected VRAM fits under wins.
# Bounds sit a little above each tier's own heaviest model (e.g.
# mid_range_8gb's heaviest listed model needs ~5.5 GB) to leave headroom
# for the OS/desktop compositor/browser also sharing that VRAM, rather
# than recommending a model that technically fits with nothing to spare.
_TIER_VRAM_BOUNDS = [
    (4.5, "low_vram_4gb"),
    (9.0, "mid_range_8gb"),
    (17.0, "high_end_16gb"),
]
_TOP_TIER = "enthusiast_24gb_plus"
# No usable GPU detected: recommend the tier that will actually run rather
# than the tier a discrete GPU would unlock. Ollama can and does fall back
# to CPU inference, but a model sized for 8+ GB of VRAM is a poor
# experience (or outright unusable) on CPU-only/integrated-graphics
# hardware, so "unknown" defaults to the smallest tier, not a middle one.
_NO_GPU_TIER = "low_vram_4gb"


def _tier_for_vram(vram_gb: float) -> str:
    """Map detected GPU memory (GB) to a model-size tier key of MODEL_RECOMMENDATIONS (largest tier
    when above every bound).
    """
    for bound, tier in _TIER_VRAM_BOUNDS:
        if vram_gb <= bound:
            return tier
    return _TOP_TIER


def _recommended_model_in_tier(tier: str) -> dict | None:
    """The tier's own explicitly-flagged pick if there is one, else its
    first entry. Every populated tier in MODEL_RECOMMENDATIONS currently
    has an explicit '← RECOMMENDED' entry; the fallback exists so this
    still returns *something* sensible if that convention ever lapses for
    a tier, rather than returning None and pushing the "what do I actually
    suggest" decision onto the frontend."""
    models = MODEL_RECOMMENDATIONS.get(tier) or []
    for m in models:
        if "RECOMMENDED" in m.get("name", ""):
            return m
    return models[0] if models else None

class ModelSwitch(BaseModel):
    """Body of POST /api/models/pull: the Ollama model id to download."""
    model_config = {"protected_namespaces": ()}
    model_id: str

@router.get("/recommendations")
async def get_model_recommendations():
    """GET /api/models/recommendations - the static catalogue of tested local models per hardware tier."""
    return MODEL_RECOMMENDATIONS

@router.get("/recommended")
async def get_recommended_model():
    """
    Recommend a model for THIS machine's hardware, rather than the static
    VRAM-tier table `/recommendations` returns for the user to eyeball
    themselves. Detects GPU/VRAM (see core.gpu_detect) and maps it onto
    the closest tier in MODEL_RECOMMENDATIONS; a machine with no
    detectable GPU (no NVIDIA driver, non-NVIDIA GPU, or a platform
    `core.gpu_detect` doesn't have a probe for) falls back to the
    lowest-VRAM tier, so the recommendation errs toward "will actually
    run acceptably" rather than "best in theory".
    """
    gpu = await detect_gpu()
    tier = _tier_for_vram(gpu["vram_gb"]) if gpu.get("detected") and gpu.get("vram_gb") else _NO_GPU_TIER
    return {
        "gpu": gpu,
        "tier": tier,
        "recommended": _recommended_model_in_tier(tier),
        "tier_models": MODEL_RECOMMENDATIONS.get(tier, []),
        # Ollama's own report of what is loaded and how much is in VRAM —
        # the ground truth for "is the GPU being used".
        "ollama_runtime": await describe_loaded_models() if settings.LLM_BACKEND == "ollama" else None,
    }


@router.get("/gpu")
async def gpu_status(model: Optional[str] = None):
    """GPU detection + Ollama's live GPU/CPU split (/api/ps) + the context
    plan the backend will use for `model` (default OLLAMA_MODEL)."""
    gpu = await detect_gpu()
    out = {"gpu": gpu, "ollama_runtime": None, "plan": None}
    if settings.LLM_BACKEND == "ollama":
        out["ollama_runtime"] = await describe_loaded_models()
        try:
            out["plan"] = await plan_num_ctx(model or settings.OLLAMA_MODEL)
        except Exception as e:  # never fail the status call
            out["plan"] = {"error": f"{type(e).__name__}: {e}"}
    return out

async def _probe() -> dict:
    """One LLM health probe through the configured backend (Ollama/llama.cpp/vLLM)."""
    return await get_llm().health_check()


@router.get("/available")
async def get_available_models():
    """Installed models, discovered live from Ollama's /api/tags.

    `models` lists every installed model with its `kind` ("text", "vision",
    "embedding"); `text_models` is what a text-analysis model selector
    should offer. `configured_model` is the server default (OLLAMA_MODEL);
    `configured_model_available` says whether it is actually installed.
    The frontend's selection is sent per request (`model` on /analyze) and
    validated there — this endpoint never picks a model for the caller.
    """
    p = await _probe()
    return {
        "backend": p.get("backend"),
        "reachable": p.get("reachable", False),
        "state": p.get("state"),
        "models": p.get("models", []),
        "text_models": p.get("text_models", []),
        "vision_models": p.get("vision_models", []),
        "embedding_models": p.get("embedding_models", []),
        "configured_model": p.get("configured_model"),
        "configured_model_available": p.get("configured_model_available", False),
        "vision_model": p.get("vision_model"),
        "vision_model_available": p.get("vision_model_available", False),
        "error": p.get("error"),
        "hint": p.get("hint"),
    }


@router.get("/status")
async def model_status():
    """The full LLM health contract (same object as /api/health's `llm`)."""
    return await _probe()


class SmokeTest(BaseModel):
    """Body of POST /api/models/test: model to test (None = the configured OLLAMA_MODEL)."""
    model_config = {"protected_namespaces": ()}
    model: Optional[str] = None


@router.post("/test")
async def test_generation(body: SmokeTest):
    """Explicit, lightweight generation smoke test (a few tokens) for the
    given model (default: OLLAMA_MODEL). This is the ONLY way the health
    contract's `generation.status` becomes "ready" before a real analysis
    has run. It is never triggered by health polling."""
    if settings.LLM_BACKEND != "ollama":
        raise HTTPException(400, f"The generation test is only implemented for the Ollama backend (LLM_BACKEND={settings.LLM_BACKEND}).")
    probe = await _probe()
    resolved, err = validate_model_choice(body.model or settings.OLLAMA_MODEL, probe, purpose="text")
    if err:
        raise HTTPException(409 if probe.get("reachable") else 503, err)
    # Load the model exactly as an analysis will (same num_ctx and options),
    # so "Ready" here means the analysis will load too, and the analysis
    # reuses the already-loaded model instead of reloading it.
    plan = await plan_num_ctx(resolved)
    extra = {"seed": int(settings.OLLAMA_SEED)}
    if int(getattr(settings, "OLLAMA_NUM_GPU", -1)) >= 0:
        extra["num_gpu"] = int(settings.OLLAMA_NUM_GPU)
    result = await generation_smoke_test(
        settings.OLLAMA_BASE_URL, resolved, timeout=settings.OLLAMA_SMOKE_TEST_TIMEOUT,
        num_ctx=int(plan["num_ctx"]), extra_options=extra, keep_alive=settings.OLLAMA_KEEP_ALIVE,
    )
    result = dict(result)
    result["num_ctx"] = plan["num_ctx"]
    result["context_plan"] = plan.get("reason")
    runtime = await describe_loaded_models()
    loaded = next((m for m in runtime.get("models", [])
                   if normalize_model_name(m.get("name")) == normalize_model_name(resolved)), None)
    result["processor"] = loaded.get("processor") if loaded else None
    result["gpu_percent"] = loaded.get("gpu_percent") if loaded else None
    logger.info(f"Generation smoke test model={resolved} num_ctx={plan['num_ctx']}: {result.get('status')} "
                f"({result.get('latency_ms')} ms, {result.get('processor') or 'offload unknown'})"
                f"{' — ' + result['error'] if result.get('error') else ''}")
    return result


# ── Model pulling (background) ──────────────────────────────────────────────
# A model pull can be several GB and take many minutes on a normal
# connection. The previous implementation `await`ed Ollama's pull endpoint
# directly inside the request handler with stream:false and a 600s client
# timeout — despite the docstring claiming it "runs in background", it
# blocked the HTTP request for the entire download and would hard-fail with
# a client-side timeout on any model/connection combination slower than
# 600s. This tracks pull jobs in-memory (same pattern as
# api/routes/analysis.py's _progress_store) and returns immediately, with a
# separate endpoint to poll progress.

_pull_jobs: dict[str, dict] = {}
_pull_tasks: set = set()  # strong refs so background pulls aren't garbage-collected
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*(:[A-Za-z0-9._\-]+)?$")


async def _run_pull(model_id: str) -> None:
    """Background task behind POST /api/models/pull: stream Ollama's /api/pull progress into
    `_pull_jobs[model_id]` (polled by GET /api/models/pull/status/{model}) until it reports success
    or an error. No client timeout - a multi-GB download can take a long time.
    """
    _pull_jobs[model_id] = {"status": "pulling", "detail": None, "error": None, "started_at": time.time()}
    try:
        async with ollama_http_client(settings.OLLAMA_BASE_URL, None) as client:  # no timeout — a pull can legitimately take a long time
            async with client.stream(
                "POST", f"{settings.OLLAMA_BASE_URL}/api/pull",
                json={"name": model_id, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    _pull_jobs[model_id]["detail"] = chunk.get("status")
                    if chunk.get("error"):
                        _pull_jobs[model_id] = {
                            "status": "error", "detail": None,
                            "error": chunk["error"], "started_at": _pull_jobs[model_id]["started_at"],
                        }
                        return
        _pull_jobs[model_id]["status"] = "done"
    except Exception as e:
        logger.error(f"Model pull failed for {model_id}: {e}")
        _pull_jobs[model_id] = {
            "status": "error", "detail": None, "error": str(e),
            "started_at": _pull_jobs.get(model_id, {}).get("started_at", time.time()),
        }


@router.post("/pull")
async def pull_model(body: ModelSwitch):
    """Start pulling a model via Ollama. Returns immediately — poll
    GET /models/pull/status/{model_id} for progress. Actually runs in the
    background (see _run_pull), unlike the previous version of this
    endpoint."""
    if settings.LLM_BACKEND != "ollama":
        raise HTTPException(400, "Model pulling only supported with Ollama backend")
    if not _MODEL_ID_RE.match(body.model_id or ""):
        raise HTTPException(400, "Invalid model name. Use an Ollama model tag such as qwen2.5:7b.")
    if _pull_jobs.get(body.model_id, {}).get("status") == "pulling":
        return {"status": "already_pulling", "model": body.model_id}
    task = asyncio.create_task(_run_pull(body.model_id))
    _pull_tasks.add(task)
    task.add_done_callback(_pull_tasks.discard)
    return {"status": "started", "model": body.model_id}


@router.get("/pull/status/{model_id:path}")
async def pull_status(model_id: str):
    """Poll the status of a background model pull started via POST /pull."""
    job = _pull_jobs.get(model_id)
    if not job:
        return {"status": "unknown", "model": model_id, "detail": "No pull job found for this model."}
    return {"model": model_id, **job}
