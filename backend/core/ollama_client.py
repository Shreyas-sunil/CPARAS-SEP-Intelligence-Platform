"""
Ollama connectivity, model discovery and the LLM health contract
================================================================
This module is the ONE place that decides whether the local LLM is usable.
Every consumer (GET /api/health, GET /api/models/*, the analyze/classify
endpoints' model validation, the startup scripts' expectations) reads the
same dict produced by :func:`probe_ollama`, so "is Ollama up?" can never
again be answered differently in two places.

Health contract (all keys always present)::

    {
      "backend": "ollama",
      "base_url": "http://127.0.0.1:11434",
      "state": "ready" | "unreachable" | "not_ollama" | "no_models" | "model_missing",
      "reachable": bool,        # something that speaks the Ollama API answered /api/tags
      "available": bool,        # reachable AND the configured text model is installed
      "version": str | None,    # from /api/version (best effort)
      "latency_ms": int | None,
      "models": [ {name, kind, size, parameter_size, quantization, family}, ... ],
      "text_models": [...], "vision_models": [...], "embedding_models": [...],
      "configured_model": "qwen2.5:7b",
      "configured_model_available": bool,
      "configured_model_resolved": "qwen2.5:7b" | None,   # exact installed tag
      "configured_model_kind": "text" | "vision" | "embedding" | None,
      "vision_model": "moondream",
      "vision_model_available": bool,
      "generation": {"status": "untested" | "ready" | "failed", "model", "checked_at",
                     "latency_ms", "error", "source"},
      "error": str | None,      # what is wrong, specifically
      "hint": str | None,       # what the user should do about it
      "warnings": [str, ...],
    }

The four questions the UI must be able to answer separately:

    Ollama server reachable?   -> reachable
    Models installed?          -> len(models) / text_models
    Selected model installed?  -> configured_model_available (or validate_model())
    Generation functional?     -> generation.status  (only ever "ready" after a
                                  real generation succeeded — never inferred from
                                  /api/tags answering)

A health poll never runs a generation. The explicit, lightweight smoke test is
:func:`generation_smoke_test` (POST /api/models/test).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ── HTTP client ──────────────────────────────────────────────────────────────

def _is_loopback_url(url: str) -> bool:
    """Whether `url` points at this machine (localhost / 127.0.0.0/8 / ::1); used to bypass system HTTP
    proxies for the local Ollama server.
    """
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
    except Exception:
        return False
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ollama_http_client(base_url: str, timeout: Optional[float]) -> httpx.AsyncClient:
    """An httpx client for talking to Ollama.

    ``trust_env=False`` for loopback URLs: httpx honours HTTP(S)_PROXY /
    ALL_PROXY environment variables by default, and on many corporate
    Windows machines those are set system-wide. Without this, a request to
    http://127.0.0.1:11434 can be sent to the corporate proxy, which fails —
    and the app reports "Ollama unreachable" while 'ollama list' works fine
    in the same terminal.
    """
    return httpx.AsyncClient(timeout=timeout, trust_env=not _is_loopback_url(base_url))


# ── model naming / classification ────────────────────────────────────────────

def normalize_model_name(name: Optional[str]) -> str:
    """Ollama treats ``moondream`` and ``moondream:latest`` as the same model."""
    n = (name or "").strip()
    if not n:
        return ""
    last = n.rsplit("/", 1)[-1]
    if ":" not in last:
        n = f"{n}:latest"
    return n.lower()


def resolve_installed(name: Optional[str], installed: list[str]) -> Optional[str]:
    """Return the exact installed tag that ``name`` refers to, or None.
    Never substitutes a different model — only resolves the implicit
    ``:latest`` tag and case."""
    wanted = normalize_model_name(name)
    if not wanted:
        return None
    for tag in installed:
        if normalize_model_name(tag) == wanted:
            return tag
    return None


# Families Ollama reports for multimodal projectors, and name fragments of
# well-known vision-only / OCR-oriented models. A model that matches is a
# VISION model: it must not be offered as the default text-analysis model.
_VISION_FAMILIES = {"clip", "mllama"}
_VISION_NAME_HINTS = (
    "moondream", "llava", "bakllava", "vision", "minicpm-v", "qwen2-vl", "qwen2.5vl",
    "qwen2.5-vl", "granite3.2-vision", "llama3.2-vision", "cogvlm", "moondream2",
)
_EMBED_FAMILIES = {"bert", "nomic-bert"}
_EMBED_NAME_HINTS = ("embed", "bge-", "bge:", "minilm", "e5-")


def classify_model(entry: dict) -> str:
    """'vision' | 'embedding' | 'text' for one /api/tags entry."""
    name = str(entry.get("name") or entry.get("model") or "").lower()
    details = entry.get("details") or {}
    families = {str(f).lower() for f in (details.get("families") or [])}
    if details.get("family"):
        families.add(str(details["family"]).lower())
    if families & _VISION_FAMILIES or any(h in name for h in _VISION_NAME_HINTS):
        return "vision"
    if families & _EMBED_FAMILIES or any(h in name for h in _EMBED_NAME_HINTS):
        return "embedding"
    return "text"


def _model_summary(entry: dict) -> dict:
    """Compact description of one installed Ollama model (name, kind text/vision/embedding, size,
    parameters, quantisation, family).
    """
    details = entry.get("details") or {}
    name = entry.get("name") or entry.get("model") or ""
    return {
        "name": name,
        "kind": classify_model(entry),
        "size": entry.get("size"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "family": details.get("family"),
        "modified_at": entry.get("modified_at"),
    }


# ── generation state (updated by real generations and by the smoke test) ─────

_generation_state: dict[str, Any] = {
    "status": "untested", "model": None, "checked_at": None,
    "latency_ms": None, "error": None, "source": None,
}


def record_generation(model: Optional[str], ok: bool, *, latency_ms: Optional[int] = None,
                      error: Optional[str] = None, source: str = "analysis") -> None:
    """Called after every real generation attempt. This — not /api/tags —
    is the only evidence that "generation works"."""
    _generation_state.update({
        "status": "ready" if ok else "failed",
        "model": model,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latency_ms": latency_ms,
        "error": None if ok else (error or "Generation failed"),
        "source": source,
    })


def generation_state() -> dict:
    """Copy of the last generation smoke-test result shown in the LLM status panel."""
    return dict(_generation_state)


def reset_generation_state() -> None:
    """Test hook."""
    _generation_state.update({"status": "untested", "model": None, "checked_at": None,
                              "latency_ms": None, "error": None, "source": None})


# ── probe ────────────────────────────────────────────────────────────────────

def _empty_result(base_url: str, configured_model: str, vision_model: str) -> dict:
    """Health-contract skeleton with every field present ('unreachable' until probed), so the UI never
    has to guess missing keys.
    """
    return {
        "backend": "ollama",
        "base_url": base_url,
        "state": "unreachable",
        "reachable": False,
        "available": False,
        "version": None,
        "latency_ms": None,
        "models": [],
        "text_models": [],
        "vision_models": [],
        "embedding_models": [],
        "configured_model": configured_model or None,
        "configured_model_available": False,
        "configured_model_resolved": None,
        "configured_model_kind": None,
        "vision_model": vision_model or None,
        "vision_model_available": False,
        "generation": generation_state(),
        "error": None,
        "hint": None,
        "warnings": [],
    }


async def probe_ollama(base_url: str, configured_model: str, vision_model: str = "",
                       timeout: float = 5.0) -> dict:
    """Cheap, side-effect-free check of the Ollama server and installed
    models (GET /api/tags + GET /api/version). Never raises."""
    base_url = (base_url or "").rstrip("/")
    out = _empty_result(base_url, configured_model, vision_model)
    started = time.perf_counter()
    try:
        async with ollama_http_client(base_url, timeout) as client:
            resp = await client.get(f"{base_url}/api/tags")
            out["latency_ms"] = int((time.perf_counter() - started) * 1000)
            if resp.status_code != 200:
                out["state"] = "not_ollama"
                out["error"] = (f"{base_url}/api/tags answered HTTP {resp.status_code}, "
                                "which is not what an Ollama server returns.")
                out["hint"] = ("Another program may be using this port, or OLLAMA_BASE_URL points "
                               "at the wrong service. Open the URL above in a browser: Ollama "
                               "returns a JSON list of models.")
                return out
            resp.encoding = "utf-8"
            try:
                data = resp.json()
                raw_models = data.get("models")
                if not isinstance(raw_models, list):
                    raise ValueError("no 'models' list")
            except Exception:
                out["state"] = "not_ollama"
                out["error"] = f"{base_url}/api/tags did not return Ollama's JSON model list."
                out["hint"] = ("Check OLLAMA_BASE_URL. The port may be used by a different program.")
                return out
            try:
                v = await client.get(f"{base_url}/api/version")
                if v.status_code == 200:
                    out["version"] = (v.json() or {}).get("version")
            except Exception:
                pass
    except httpx.ConnectError as e:
        out["error"] = f"Nothing is accepting connections at {base_url} ({type(e).__name__})."
        out["hint"] = ("Start Ollama (open the Ollama app, or run 'ollama serve' in a terminal) "
                       "and check that OLLAMA_BASE_URL matches the address it listens on. If "
                       "'ollama serve' says the port is already in use, Ollama is already "
                       "running — then check OLLAMA_BASE_URL and any firewall/proxy instead.")
        return out
    except httpx.TimeoutException:
        out["error"] = f"Ollama at {base_url} did not answer /api/tags within {timeout:.0f}s."
        out["hint"] = ("Ollama may be busy loading a model or the machine is overloaded. "
                       "Wait a moment and re-check.")
        return out
    except Exception as e:  # malformed URL, DNS failure, etc.
        out["error"] = f"Could not query Ollama at {base_url}: {type(e).__name__}: {e}"
        out["hint"] = "Check OLLAMA_BASE_URL in backend/.env (it should look like http://127.0.0.1:11434)."
        return out

    out["reachable"] = True
    models = [_model_summary(m) for m in raw_models if isinstance(m, dict)]
    models.sort(key=lambda m: m["name"])
    out["models"] = models
    names = [m["name"] for m in models]
    out["text_models"] = [m["name"] for m in models if m["kind"] == "text"]
    out["vision_models"] = [m["name"] for m in models if m["kind"] == "vision"]
    out["embedding_models"] = [m["name"] for m in models if m["kind"] == "embedding"]

    resolved = resolve_installed(configured_model, names)
    out["configured_model_resolved"] = resolved
    out["configured_model_available"] = resolved is not None
    if resolved:
        out["configured_model_kind"] = next(m["kind"] for m in models if m["name"] == resolved)
    if vision_model:
        out["vision_model_available"] = resolve_installed(vision_model, names) is not None

    if not models:
        out["state"] = "no_models"
        out["error"] = "Ollama is running but has no models installed."
        out["hint"] = f"Install one, e.g.  ollama pull {configured_model or 'qwen2.5:7b'}"
    elif not resolved:
        out["state"] = "model_missing"
        available_txt = ", ".join(out["text_models"]) or "none"
        out["error"] = (f"The configured model '{configured_model}' is not installed in Ollama. "
                        f"Installed text models: {available_txt}.")
        out["hint"] = (f"Run  ollama pull {configured_model}  or set OLLAMA_MODEL in backend/.env "
                       "to one of the installed models (or pick one in the model selector).")
    else:
        out["state"] = "ready"
        out["available"] = True

    if out["configured_model_kind"] in ("vision", "embedding"):
        out["warnings"].append(
            f"OLLAMA_MODEL '{configured_model}' looks like a {out['configured_model_kind']} model; "
            "text analysis needs a text model such as qwen2.5:7b.")
    if vision_model and not out["vision_model_available"]:
        out["warnings"].append(
            f"Vision OCR model '{vision_model}' is not installed (only needed for scanned PDFs when "
            f"Tesseract is unavailable): ollama pull {vision_model}")
    return out


def validate_model_choice(model: Optional[str], probe: dict, *, purpose: str = "text") -> tuple[Optional[str], Optional[str]]:
    """Validate a model the caller wants to use against a probe result.

    Returns (resolved_tag, None) on success or (None, error_message).
    Never substitutes another model.
    """
    if not probe.get("reachable"):
        return None, (probe.get("error") or "Ollama is not reachable.") + (
            f" {probe['hint']}" if probe.get("hint") else "")
    if not model:
        return None, "No model was selected and OLLAMA_MODEL is empty."
    names = [m["name"] for m in probe.get("models", [])]
    resolved = resolve_installed(model, names)
    if not resolved:
        txt = ", ".join(probe.get("text_models") or []) or "none"
        return None, (f"Model '{model}' is not installed in Ollama. Installed text models: {txt}. "
                      f"Install it with: ollama pull {model}")
    kind = next((m["kind"] for m in probe["models"] if m["name"] == resolved), "text")
    if purpose == "text" and kind != "text":
        return None, (f"Model '{resolved}' is a {kind} model and cannot be used for text analysis. "
                      "Choose a text model such as qwen2.5:7b, qwen2.5:3b or phi3.5.")
    return resolved, None


async def generation_smoke_test(base_url: str, model: str, *, timeout: float = 120.0,
                                num_ctx: int = 2048, extra_options: Optional[dict] = None,
                                keep_alive: Optional[str] = None) -> dict:
    """Tiny, explicit generation (a few tokens) that proves the model can
    actually generate. Only run on demand — never from a health poll. The
    generous default timeout covers the first-request model load, which on
    a 7B model can take tens of seconds on a modest machine.

    v0.4.2: callers pass the SAME num_ctx an analysis will use (see
    POST /api/models/test). Testing at 2048 tokens used to load the model
    in a configuration the analysis never uses — the test said "Ready",
    then the analysis reloaded the model at 24576 tokens and failed with
    HTTP 500, so the two disagreed run after run."""
    base_url = (base_url or "").rstrip("/")
    started = time.perf_counter()
    options = {"temperature": 0.0, "num_predict": 8, "num_ctx": num_ctx}
    options.update(extra_options or {})
    payload = {
        "model": model,
        "prompt": "Reply with the single word OK.",
        "stream": False,
        "options": options,
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    try:
        async with ollama_http_client(base_url, timeout) as client:
            resp = await client.post(f"{base_url}/api/generate", json=payload)
            latency = int((time.perf_counter() - started) * 1000)
            body: dict = {}
            try:
                body = resp.json()
            except Exception:
                pass
            if resp.status_code != 200 or body.get("error"):
                err = body.get("error") or f"HTTP {resp.status_code}"
                record_generation(model, False, latency_ms=latency, error=err, source="smoke_test")
                return generation_state()
            text = (body.get("response") or "").strip()
            if not text:
                record_generation(model, False, latency_ms=latency,
                                  error="The model returned an empty response.", source="smoke_test")
                return generation_state()
            record_generation(model, True, latency_ms=latency, source="smoke_test")
            return generation_state()
    except httpx.TimeoutException:
        record_generation(model, False, latency_ms=int((time.perf_counter() - started) * 1000),
                          error=f"No response within {timeout:.0f}s (model may still be loading, or "
                                "the machine lacks RAM/VRAM for it).", source="smoke_test")
    except Exception as e:
        record_generation(model, False, error=f"{type(e).__name__}: {e}", source="smoke_test")
    return generation_state()


# ── process-wide concurrency limit for Ollama requests ───────────────────────

class _LoopBoundSemaphore:
    """asyncio.Semaphore bound lazily to the running loop (tests and
    uvicorn --reload create fresh loops; a semaphore created on one loop
    cannot be awaited from another)."""

    def __init__(self) -> None:
        """Semaphore is created lazily inside the running event loop."""
        self._sem: Optional[asyncio.Semaphore] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._size = 1

    def get(self, size: int) -> asyncio.Semaphore:
        """The concurrency semaphore for the current event loop, recreated when the loop or the
        configured size changes (tests and reloads run several loops).
        """
        loop = asyncio.get_running_loop()
        size = max(1, int(size or 1))
        if self._sem is None or self._loop is not loop or self._size != size:
            self._sem = asyncio.Semaphore(size)
            self._loop = loop
            self._size = size
        return self._sem


ollama_request_slots = _LoopBoundSemaphore()
