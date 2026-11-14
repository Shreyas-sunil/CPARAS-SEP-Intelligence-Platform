"""
Local LLM Engine
================
Unified interface for Ollama (default), llama.cpp (via llama-cpp-python) and
vLLM. All inference is local — no cloud calls are ever made.

Tested Ollama text models (pick one that fits your RAM/VRAM):
  qwen2.5:3b                         ~2.5 GB   fastest, fine for small declarations
  phi3.5:3.8b-mini-instruct-q4_K_M   ~2.5 GB   alternative small model
  qwen2.5:7b                         ~5 GB     default — best accuracy of the three
Vision (OCR fallback only, never used for text analysis):
  moondream                          ~1.7 GB

Every backend's health_check() returns the same contract as
core/ollama_client.probe_ollama(), so callers never special-case a backend.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

import httpx

from core.config import settings
from core.ollama_client import (
    ollama_http_client, ollama_request_slots, probe_ollama, record_generation, generation_state,
)
from core.ollama_runtime import clamp_num_predict, plan_num_ctx

logger = logging.getLogger(__name__)


# ── Base Interface ─────────────────────────────────────────────────────────────

class BaseLLMBackend(ABC):
    """Interface every local LLM backend implements (Ollama, llama.cpp, vLLM). Callers only use
    `get_llm()` and these three methods, so another backend - or a platform-wide LLM gateway in a
    larger deployment - can be plugged in by adding one subclass and a branch in
    `get_llm_backend()`.
    """
    @abstractmethod
    async def generate(self, prompt: str, system: str = "", temperature: float = 0.1,
                       max_tokens: int = 8192, repeat_penalty: float = 1.3,
                       repeat_last_n: int = 512, model: Optional[str] = None,
                       json_mode: bool = True, num_ctx: Optional[int] = None) -> str:
        """Run one completion and return the full text. `json_mode` asks the backend to constrain
        output to JSON where supported; `model` / `num_ctx` override the configured defaults for
        this call only.
        """
        ...

    @abstractmethod
    async def stream(self, prompt: str, system: str = "", temperature: float = 0.1,
                     max_tokens: int = 8192, repeat_penalty: float = 1.3,
                     repeat_last_n: int = 512, model: Optional[str] = None,
                     json_mode: bool = True, num_ctx: Optional[int] = None) -> AsyncIterator[str]:
        """Same as `generate`, yielding tokens as they are produced."""
        ...

    @abstractmethod
    async def health_check(self) -> dict:
        """Reachability / model-availability report in the shared LLM health contract (see
        core/ollama_client.py).
        """
        ...


# ── Ollama Backend ─────────────────────────────────────────────────────────────

class LLMError(RuntimeError):
    """A generation failed for a reason the caller can show the user
    (model not installed, out of memory, backend unreachable...)."""


class OllamaBackend(BaseLLMBackend):
    """
    Calls the local Ollama server (default http://127.0.0.1:11434).
    Install: https://ollama.com  ·  then e.g.:  ollama pull qwen2.5:7b

    Health/model discovery lives in core/ollama_client.py (one contract for
    every consumer). This class only does generation, and records the
    outcome of every real generation so the health contract can report
    "generation ready/failed" from evidence rather than inferring it from
    /api/tags answering.
    """

    # Connection-level failures that are safe to retry (nothing was
    # produced yet). HTTP errors from Ollama itself (404 model not found,
    # 500 out of memory) are configuration/resource problems and are NOT
    # retried.
    _RETRYABLE = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                  httpx.ConnectTimeout, httpx.WriteError, httpx.ReadError, httpx.PoolTimeout)

    def __init__(self):
        """Read the Ollama URL, default model and per-request timeout from settings."""
        self.base_url = settings.OLLAMA_BASE_URL.rstrip("/")
        self.model = settings.OLLAMA_MODEL
        self.timeout = settings.OLLAMA_TIMEOUT

    async def generate(self, prompt: str, system: str = "", temperature: float = 0.1,
                       max_tokens: int = 8192, repeat_penalty: float = 1.3,
                       repeat_last_n: int = 512, model: Optional[str] = None,
                       json_mode: bool = True, num_ctx: Optional[int] = None) -> str:
        """Generate through Ollama's /api/chat. Streams internally and joins the tokens, so a long
        generation keeps the connection active instead of looking idle to firewalls; the context
        window is sized per model to fit the GPU (core/ollama_runtime.py).
        """
        # Streams under the hood (still returns one joined string) so the
        # connection carries a steady trickle of bytes for the whole
        # generation instead of sitting silent for many minutes on a
        # non-streaming request — the latter looks idle to NAT/firewalls and
        # can be killed before OLLAMA_TIMEOUT is reached.
        #
        # generate() is the only safe place to retry: it fully buffers the
        # response before returning, so a failed attempt has produced no
        # visible output anywhere yet.
        max_attempts = 3
        effective_model = model or self.model
        started = time.perf_counter()
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                chunks: list[str] = []
                async for token in self.stream(
                    prompt, system=system, temperature=temperature, max_tokens=max_tokens,
                    repeat_penalty=repeat_penalty, repeat_last_n=repeat_last_n, model=model,
                    json_mode=json_mode, num_ctx=num_ctx,
                ):
                    chunks.append(token)
                record_generation(effective_model, True,
                                  latency_ms=int((time.perf_counter() - started) * 1000))
                return "".join(chunks)
            except self._RETRYABLE as e:
                last_error = e
                if attempt < max_attempts:
                    backoff = 2 ** attempt  # 2s, 4s
                    logger.warning(
                        f"Ollama generate(model={effective_model}) attempt {attempt}/{max_attempts} "
                        f"failed with a transient connection error ({type(e).__name__}: {e}) — "
                        f"retrying in {backoff}s."
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        f"Ollama generate(model={effective_model}) failed after {max_attempts} attempts: "
                        f"{type(e).__name__}: {e}"
                    )
            except LLMError as e:
                record_generation(effective_model, False, error=str(e))
                raise
        err = LLMError(
            f"Could not get a response from Ollama at {self.base_url} for model '{effective_model}' "
            f"after {max_attempts} attempts ({type(last_error).__name__}: {last_error}). "
            "Check that Ollama is running and that the machine has enough RAM/VRAM for this model."
        )
        record_generation(effective_model, False, error=str(err))
        raise err from last_error

    async def stream(self, prompt: str, system: str = "", temperature: float = 0.1,
                     max_tokens: int = 8192, repeat_penalty: float = 1.3,
                     repeat_last_n: int = 512, model: Optional[str] = None,
                     json_mode: bool = True, num_ctx: Optional[int] = None) -> AsyncIterator[str]:
        """Stream tokens from Ollama's /api/chat with the analysis options (temperature, repeat
        penalty, JSON format, fitted num_ctx, fixed seed). Raises with an actionable message for a
        missing model or an unreachable/out-of-memory server.
        """
        # `model` lets a caller (e.g. /analyze, when the user picked a model
        # in the model selector) override the configured default for one
        # call without touching settings.OLLAMA_MODEL.
        #
        # `json_mode` (default True) sets Ollama's native `format: "json"`,
        # which constrains sampling to syntactically valid JSON. It does not
        # guarantee the schema, so the salvage/verification layers remain.
        #
        # `repeat_penalty` / `repeat_last_n`: counters degenerate repetition
        # loops; callers emitting many similar rows pass gentler values (see
        # the analysis routes).
        effective_model = model or self.model
        # v0.4.2: num_ctx is planned per model so weights + KV cache fit in
        # VRAM (core/ollama_runtime.py). The same value is used for every
        # request to a model, so Ollama never reloads it between calls and
        # the GPU/CPU layer split is identical on every run.
        plan = await plan_num_ctx(effective_model, num_ctx or settings.OLLAMA_NUM_CTX)
        ctx = int(plan["num_ctx"])
        num_predict = clamp_num_predict(max_tokens, ctx, len(prompt) + len(system or ""))
        if num_predict < max_tokens:
            logger.debug(f"num_predict {max_tokens} -> {num_predict} to stay inside num_ctx={ctx}")
        options = {
            "temperature": temperature,
            "num_predict": num_predict,
            "num_ctx": ctx,
            "repeat_penalty": repeat_penalty,
            "repeat_last_n": repeat_last_n,
            # Reproducibility: a fixed seed + greedy decoding at temperature 0
            # gives the same output for the same input on every run.
            "seed": int(settings.OLLAMA_SEED),
        }
        if temperature <= 0:
            options["top_k"] = 1
            options["top_p"] = 1.0
        if int(getattr(settings, "OLLAMA_NUM_GPU", -1)) >= 0:
            options["num_gpu"] = int(settings.OLLAMA_NUM_GPU)
        payload = {
            "model": effective_model,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "options": options,
        }
        if json_mode:
            payload["format"] = "json"
        # Process-wide cap on simultaneous Ollama requests (OLLAMA_CONCURRENCY):
        # each concurrent request needs its own num_ctx-sized KV cache, so
        # unbounded parallelism is the fastest way to exhaust VRAM.
        async with ollama_request_slots.get(settings.OLLAMA_CONCURRENCY):
            async with ollama_http_client(self.base_url, self.timeout) as client:
                async with client.stream("POST", f"{self.base_url}/api/generate", json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        detail = body
                        try:
                            detail = json.loads(body).get("error") or body
                        except Exception:
                            pass
                        hint = ""
                        if resp.status_code == 404:
                            hint = f" Install it with: ollama pull {effective_model}"
                        elif resp.status_code >= 500:
                            hint = (f" The model could not be loaded with a {ctx}-token context "
                                    "(usually not enough VRAM/RAM). Lower OLLAMA_NUM_CTX in "
                                    "backend/.env, set OLLAMA_FLASH_ATTENTION=true and "
                                    "OLLAMA_KV_CACHE_TYPE=q8_0 and restart Ollama with start.ps1, "
                                    "or pick a smaller model.")
                        raise LLMError(
                            f"Ollama rejected the request for model '{effective_model}' "
                            f"(HTTP {resp.status_code}): {detail.strip()[:300]}.{hint}"
                        )
                    # Ollama's NDJSON stream is always UTF-8 but usually carries
                    # no charset; forcing it avoids httpx sniffing a partial
                    # multi-byte window as cp1252 (mojibake in CJK names).
                    resp.encoding = "utf-8"
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            # e.g. the runner crashed / ran out of memory mid-generation.
                            raise LLMError(
                                f"Ollama reported an error while generating with '{effective_model}': "
                                f"{chunk['error']}"
                            )
                        if token := chunk.get("response", ""):
                            yield token
                        if chunk.get("done"):
                            break

    async def health_check(self) -> dict:
        """The shared health contract (see core/ollama_client.py)."""
        return await probe_ollama(
            self.base_url, self.model, getattr(settings, "VISION_MODEL", ""),
            timeout=float(getattr(settings, "OLLAMA_HEALTH_TIMEOUT", 5.0)),
        )


# ── llama.cpp Backend (via OpenAI-compatible server or Python bindings) ────────

class LlamaCppBackend(BaseLLMBackend):
    """
    Uses llama-cpp-python with GPU offload.
    Install: CMAKE_ARGS="-DLLAMA_CUDA=on" pip install llama-cpp-python
    Or CPU: pip install llama-cpp-python

    Place your GGUF model in ./models/
    """

    def __init__(self):
        """The GGUF model is loaded lazily on first use."""
        self._llm = None

    def _get_llm(self):
        """Load the GGUF model from LLAMACPP_MODEL_PATH once (raises a clear error when llama-cpp-
        python is not installed).
        """
        if self._llm is None:
            try:
                from llama_cpp import Llama
                logger.info(f"Loading GGUF model: {settings.LLAMACPP_MODEL_PATH}")
                self._llm = Llama(
                    model_path=settings.LLAMACPP_MODEL_PATH,
                    n_gpu_layers=settings.LLAMACPP_N_GPU_LAYERS,
                    n_ctx=settings.LLAMACPP_N_CTX,
                    n_threads=settings.LLAMACPP_N_THREADS,
                    verbose=settings.DEBUG,
                )
                logger.info("GGUF model loaded successfully.")
            except ImportError:
                raise RuntimeError("llama-cpp-python not installed. Run: pip install llama-cpp-python")
        return self._llm

    def _build_prompt(self, prompt: str, system: str) -> str:
        """Mistral/Llama instruct format."""
        if system:
            return f"[INST] <<SYS>>\n{system}\n<</SYS>>\n\n{prompt} [/INST]"
        return f"[INST] {prompt} [/INST]"

    async def generate(self, prompt: str, system: str = "", temperature: float = 0.1,
                       max_tokens: int = 8192, repeat_penalty: float = 1.3,
                       repeat_last_n: int = 512, model: Optional[str] = None,
                       json_mode: bool = True, num_ctx: Optional[int] = None) -> str:
        """Blocking llama.cpp completion run in a worker thread; extra arguments are accepted for
        interface parity and ignored.
        """
        # repeat_penalty/repeat_last_n/json_mode/num_ctx accepted for interface
        # parity with OllamaBackend (see its docstring) but not wired into
        # llama.cpp here — llama-cpp-python does support grammar-constrained
        # JSON output via a `response_format` argument in recent versions,
        # but it isn't implemented in this backend yet (see Overall
        # Assessment / future improvements). `model` is also accepted for
        # interface parity but ignored: llama.cpp loads one fixed GGUF file
        # (settings.LLAMACPP_MODEL_PATH), so there's nothing to switch —
        # model selection only applies to the Ollama backend.
        llm = self._get_llm()
        full_prompt = self._build_prompt(prompt, system)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: llm(full_prompt, max_tokens=max_tokens, temperature=temperature, stop=["[INST]"])
        )
        return result["choices"][0]["text"].strip()

    async def stream(self, prompt: str, system: str = "", temperature: float = 0.1,
                     max_tokens: int = 8192, repeat_penalty: float = 1.3,
                     repeat_last_n: int = 512, model: Optional[str] = None,
                     json_mode: bool = True, num_ctx: Optional[int] = None) -> AsyncIterator[str]:
        """Stream tokens from llama.cpp's synchronous iterator through an asyncio queue."""
        # llama.cpp streaming is synchronous, wrap it. `model`/`json_mode`
        # accepted for interface parity but ignored (see generate() above).
        llm = self._get_llm()
        full_prompt = self._build_prompt(prompt, system)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        _SENTINEL = object()

        def _stream_sync():
            """Worker-thread producer: push tokens into the queue and always finish with the sentinel
            or the exception, so the consumer can never hang.
            """
            # Always deliver the sentinel (or the exception): previously an
            # exception inside llama.cpp left the consumer awaiting
            # queue.get() forever.
            try:
                for chunk in llm(full_prompt, max_tokens=max_tokens, temperature=temperature,
                                 stream=True, stop=["[INST]"]):
                    token = chunk["choices"][0]["text"]
                    loop.call_soon_threadsafe(queue.put_nowait, token)
            except Exception as e:  # pragma: no cover - depends on llama.cpp
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        loop.run_in_executor(None, _stream_sync)
        while True:
            token = await queue.get()
            if token is _SENTINEL:
                break
            if isinstance(token, Exception):
                raise LLMError(f"llama.cpp generation failed: {token}") from token
            yield token

    async def health_check(self) -> dict:
        """Same contract as probe_ollama(). Does NOT load the model (that
        can take minutes and GBs of RAM) — it only checks the GGUF file and
        the Python bindings exist."""
        import os
        path = settings.LLAMACPP_MODEL_PATH
        out = _non_ollama_contract("llamacpp", None, path)
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            out["error"] = "llama-cpp-python is not installed."
            out["hint"] = "pip install llama-cpp-python (see README for GPU builds)."
            return out
        if not os.path.isfile(path):
            out["error"] = f"GGUF model file not found: {path}"
            out["hint"] = "Set LLAMACPP_MODEL_PATH in backend/.env to an existing .gguf file."
            return out
        name = os.path.basename(path)
        out.update(reachable=True, available=True, state="ready",
                   models=[{"name": name, "kind": "text"}], text_models=[name],
                   configured_model=name, configured_model_available=True,
                   configured_model_resolved=name, configured_model_kind="text")
        return out


# ── vLLM Backend (OpenAI-compatible) ──────────────────────────────────────────

class VLLMBackend(BaseLLMBackend):
    """
    Calls a local vLLM server (OpenAI-compatible API).
    Install: pip install vllm
    Start:   python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-7B-Instruct --port 8002
    """

    def __init__(self):
        """Read the vLLM server URL and model from settings."""
        self.base_url = settings.VLLM_BASE_URL
        self.model = settings.VLLM_MODEL

    async def generate(self, prompt: str, system: str = "", temperature: float = 0.1,
                       max_tokens: int = 8192, repeat_penalty: float = 1.3,
                       repeat_last_n: int = 512, model: Optional[str] = None,
                       json_mode: bool = True, num_ctx: Optional[int] = None) -> str:
        """Completion through vLLM's OpenAI-compatible /v1/chat/completions endpoint."""
        # repeat_penalty/repeat_last_n/num_ctx accepted for interface parity with
        # OllamaBackend but not applicable to the OpenAI-compatible vLLM
        # API used here (no equivalent param in this payload shape). `model`
        # is accepted for interface parity — vLLM serves a single model per
        # server process, so per-call overrides aren't meaningful here; the
        # frontend's model-selection feature only targets the Ollama backend.
        # `json_mode`: vLLM's OpenAI-compatible server DOES support
        # constrained JSON output via response_format, unlike llama.cpp's
        # raw completion API — so this one is actually wired up, not just
        # accepted-and-ignored.
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with ollama_http_client(self.base_url, settings.OLLAMA_TIMEOUT) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            resp.encoding = "utf-8"  # ISSUE 8 — see OllamaBackend.stream()'s comment
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def stream(self, prompt: str, system: str = "", temperature: float = 0.1,
                     max_tokens: int = 8192, repeat_penalty: float = 1.3,
                     repeat_last_n: int = 512, model: Optional[str] = None,
                     json_mode: bool = True, num_ctx: Optional[int] = None) -> AsyncIterator[str]:
        """Streamed completion through vLLM's OpenAI-compatible endpoint (server-sent events)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with ollama_http_client(self.base_url, settings.OLLAMA_TIMEOUT) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                resp.encoding = "utf-8"  # ISSUE 8 — see OllamaBackend.stream()'s comment
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            if delta := chunk["choices"][0].get("delta", {}).get("content"):
                                yield delta
                        except (json.JSONDecodeError, KeyError):
                            continue

    async def health_check(self) -> dict:
        """Same contract as probe_ollama()."""
        out = _non_ollama_contract("vllm", self.base_url, self.model)
        try:
            async with ollama_http_client(self.base_url, 5) as client:
                resp = await client.get(f"{self.base_url}/models")
                resp.raise_for_status()
                ids = [m.get("id") for m in (resp.json() or {}).get("data", []) if m.get("id")]
        except Exception as e:
            out["error"] = f"vLLM server at {self.base_url} is not reachable: {type(e).__name__}: {e}"
            out["hint"] = "Start the vLLM OpenAI-compatible server or fix VLLM_BASE_URL."
            return out
        out["reachable"] = True
        out["models"] = [{"name": i, "kind": "text"} for i in ids]
        out["text_models"] = ids
        if self.model in ids:
            out.update(available=True, state="ready", configured_model_available=True,
                       configured_model_resolved=self.model, configured_model_kind="text")
        else:
            out["state"] = "model_missing" if ids else "no_models"
            out["error"] = f"vLLM is serving {ids or 'no models'}, not VLLM_MODEL='{self.model}'."
            out["hint"] = "Set VLLM_MODEL to the model the vLLM server was started with."
        return out


def _non_ollama_contract(backend: str, base_url: Optional[str], model: Optional[str]) -> dict:
    """Empty LLM health contract for llama.cpp / vLLM backends, filled in by their own health checks,
    so the UI reads one shape for every backend.
    """
    return {
        "backend": backend, "base_url": base_url, "state": "unreachable",
        "reachable": False, "available": False, "version": None, "latency_ms": None,
        "models": [], "text_models": [], "vision_models": [], "embedding_models": [],
        "configured_model": model, "configured_model_available": False,
        "configured_model_resolved": None, "configured_model_kind": None,
        "vision_model": None, "vision_model_available": False,
        "generation": generation_state(), "error": None, "hint": None, "warnings": [],
    }


# ── Factory ────────────────────────────────────────────────────────────────────

def get_llm_backend() -> BaseLLMBackend:
    """Return the configured LLM backend singleton."""
    backend = settings.LLM_BACKEND
    if backend == "ollama":
        return OllamaBackend()
    elif backend == "llamacpp":
        return LlamaCppBackend()
    elif backend == "vllm":
        return VLLMBackend()
    else:
        raise ValueError(f"Unknown LLM backend: {backend}")


# Global singleton (reused across requests for efficiency)
_llm_instance: Optional[BaseLLMBackend] = None

def get_llm() -> BaseLLMBackend:
    """The process-wide LLM backend instance (created on first use from LLM_BACKEND)."""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = get_llm_backend()
    return _llm_instance
