"""
v0.4.2 — GPU fit, phi3.5 HTTP 500, reproducibility.

* phi3.5 has no grouped-query attention (32 KV heads): at the old fixed
  num_ctx=24576 its KV cache alone is ~9 GiB, so Ollama could not load it
  on a laptop/external RTX and answered HTTP 500. The planner must lower
  the context for it.
* qwen2.5:3b must stay entirely on a 6 GB GPU at full context, and get a
  reduced (not minimum) context on a 4 GB GPU.
* Every request carries a fixed seed and the planned num_ctx; the smoke
  test loads the model with the same num_ctx as an analysis.
"""
import asyncio
import json

import httpx

import core.ollama_client as oc
import core.ollama_runtime as rt
from core.config import settings
from core.gpu_detect import _parse_nvidia_smi_output, _parse_windows_registry_gpus

GiB = 1024 ** 3

PHI35_SHOW = {"model_info": {
    "general.architecture": "phi3", "phi3.block_count": 32, "phi3.attention.head_count": 32,
    "phi3.attention.head_count_kv": 32, "phi3.embedding_length": 3072, "phi3.context_length": 131072,
}}
QWEN3B_SHOW = {"model_info": {
    "general.architecture": "qwen2", "qwen2.block_count": 36, "qwen2.attention.head_count": 16,
    "qwen2.attention.head_count_kv": 2, "qwen2.embedding_length": 2048, "qwen2.context_length": 32768,
}}
PHI35_SIZE = 2_393_232_672
QWEN3B_SIZE = 1_929_912_432


def gpu(gb):
    return {"detected": True, "name": "NVIDIA GeForce RTX", "vram_gb": gb, "source": "nvidia-smi"}


def test_profile_kv_per_token():
    phi = rt.profile_from_show(PHI35_SHOW, PHI35_SIZE)
    qwen = rt.profile_from_show(QWEN3B_SHOW, QWEN3B_SIZE)
    assert phi["kv_elems_per_token"] == 32 * 32 * (96 + 96)
    assert qwen["kv_elems_per_token"] == 36 * 2 * (128 + 128)
    assert phi["context_length"] == 131072


def test_phi35_context_is_reduced_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_FLASH_ATTENTION", False)
    monkeypatch.setattr(settings, "OLLAMA_KV_CACHE_TYPE", "f16")
    plan = rt.plan_context(rt.profile_from_show(PHI35_SHOW, PHI35_SIZE), gpu(6.0), 24576)
    assert plan["num_ctx"] < 24576
    assert plan["num_ctx"] == settings.OLLAMA_MIN_NUM_CTX  # doesn't fully fit -> partial offload
    assert plan["fits_gpu"] is False


def test_phi35_fits_6gb_with_flash_attention_and_q8_kv(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_FLASH_ATTENTION", True)
    monkeypatch.setattr(settings, "OLLAMA_KV_CACHE_TYPE", "q8_0")
    plan = rt.plan_context(rt.profile_from_show(PHI35_SHOW, PHI35_SIZE), gpu(6.0), 24576)
    assert plan["fits_gpu"] is True and 8192 <= plan["num_ctx"] < 24576


def test_qwen3b_full_context_on_6gb_reduced_on_4gb(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_FLASH_ATTENTION", False)
    monkeypatch.setattr(settings, "OLLAMA_KV_CACHE_TYPE", "f16")
    prof = rt.profile_from_show(QWEN3B_SHOW, QWEN3B_SIZE)
    p6 = rt.plan_context(prof, gpu(6.0), 24576)
    assert p6["num_ctx"] == 24576 and p6["fits_gpu"] is True
    p4 = rt.plan_context(prof, gpu(4.0), 24576)
    assert 8192 <= p4["num_ctx"] < 24576 and p4["fits_gpu"] is True
    assert p4["num_ctx"] % 1024 == 0


def test_cpu_only_caps_context_by_ram(monkeypatch):
    monkeypatch.setattr(settings, "OLLAMA_FLASH_ATTENTION", False)
    monkeypatch.setattr(settings, "OLLAMA_KV_CACHE_TYPE", "f16")
    prof = rt.profile_from_show(PHI35_SHOW, PHI35_SIZE)
    plan = rt.plan_context(prof, {"detected": False}, 24576, ram_bytes=8 * GiB)
    assert plan["num_ctx"] < 24576


def test_model_max_context_is_respected():
    prof = rt.profile_from_show(QWEN3B_SHOW, QWEN3B_SIZE)
    plan = rt.plan_context(prof, gpu(48.0), 65536)
    assert plan["num_ctx"] == 32768


def test_unknown_architecture_keeps_configured():
    plan = rt.plan_context({"kv_elems_per_token": None, "size_bytes": None}, gpu(6.0), 24576)
    assert plan["num_ctx"] == 24576


def test_plan_is_independent_of_free_vram():
    """Same model + same card -> same num_ctx, whatever else uses the GPU."""
    prof = rt.profile_from_show(QWEN3B_SHOW, QWEN3B_SIZE)
    a = rt.plan_context(prof, {**gpu(4.0), "vram_free_gb": 3.5}, 24576)
    b = rt.plan_context(prof, {**gpu(4.0), "vram_free_gb": 1.0}, 24576)
    assert a["num_ctx"] == b["num_ctx"]


def test_chunk_size_scales_with_context():
    assert rt.chunk_chars_for_ctx(24576) == 20000
    assert rt.chunk_chars_for_ctx(12288) == 10000
    assert rt.chunk_chars_for_ctx(1024) == 3500


def test_num_predict_clamped_inside_window():
    assert rt.clamp_num_predict(22000, 8192, 9000) == 8192 - 3000 - 32
    assert rt.clamp_num_predict(3000, 24576, 9000) == 3000


# ── GPU detection parsing ────────────────────────────────────────────────────

def test_nvidia_smi_picks_largest_gpu_and_free_memory():
    out = "NVIDIA RTX A500 Laptop GPU, 4096, 3900\nNVIDIA GeForce RTX 4060, 8188, 7600\n"
    r = _parse_nvidia_smi_output(out)
    assert r["name"] == "NVIDIA GeForce RTX 4060" and r["vram_gb"] == 8.0
    assert r["vram_free_gb"] == 7.4 and r["gpu_count"] == 2


def test_windows_registry_fallback_parsing():
    text = json.dumps([
        {"DriverDesc": "Intel(R) UHD Graphics", "mem": 134217728},
        {"DriverDesc": "NVIDIA GeForce RTX 4050 Laptop GPU", "mem": 6442450944},
        {"DriverDesc": "Microsoft Basic Display Adapter", "mem": 999999999999},
    ])
    r = _parse_windows_registry_gpus(text)
    assert r["name"] == "NVIDIA GeForce RTX 4050 Laptop GPU" and r["vram_gb"] == 6.0
    assert r["source"] == "windows-registry"


# ── request payloads ─────────────────────────────────────────────────────────

def _fake_ollama(captured, show=QWEN3B_SHOW, size=QWEN3B_SIZE, ps=None):
    def handler(request: httpx.Request):
        p = request.url.path
        if p == "/api/show":
            return httpx.Response(200, json=show)
        if p == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b", "size": size, "details": {}}]})
        if p == "/api/ps":
            return httpx.Response(200, json={"models": ps or []})
        if p == "/api/generate":
            captured.append(json.loads(request.content))
            if json.loads(request.content).get("stream"):
                return httpx.Response(200, text=json.dumps({"response": "{}", "done": True}) + "\n")
            return httpx.Response(200, json={"response": "OK", "done": True})
        return httpx.Response(404)
    return handler


def _install(monkeypatch, handler):
    def factory(base_url, timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    monkeypatch.setattr(oc, "ollama_http_client", factory)
    monkeypatch.setattr(rt, "ollama_http_client", factory)
    import core.llm_engine as le
    monkeypatch.setattr(le, "ollama_http_client", factory)
    rt._profile_cache.clear()


def test_generate_sends_seed_planned_ctx_and_keep_alive(monkeypatch):
    captured = []
    _install(monkeypatch, _fake_ollama(captured))

    async def fake_gpu(max_age_s=60):
        return gpu(6.0)
    monkeypatch.setattr(rt, "detect_gpu_cached", fake_gpu)
    from core.llm_engine import OllamaBackend
    out = asyncio.run(OllamaBackend().generate("hello", system="sys", temperature=0.0,
                                               max_tokens=1000, model="qwen2.5:3b"))
    assert out == "{}"
    opts = captured[0]["options"]
    assert opts["seed"] == settings.OLLAMA_SEED
    assert opts["top_k"] == 1
    assert opts["num_ctx"] == 24576
    assert captured[0]["keep_alive"] == settings.OLLAMA_KEEP_ALIVE


def test_smoke_test_uses_analysis_context(monkeypatch):
    captured = []
    _install(monkeypatch, _fake_ollama(captured, ps=[{"name": "qwen2.5:3b", "size": 3 * GiB, "size_vram": 3 * GiB}]))

    async def fake_gpu(max_age_s=60):
        return gpu(4.0)
    monkeypatch.setattr(rt, "detect_gpu_cached", fake_gpu)
    from api.routes.shared import models as m

    async def fake_probe():
        return {"reachable": True, "models": [{"name": "qwen2.5:3b", "kind": "text"}], "text_models": ["qwen2.5:3b"]}
    monkeypatch.setattr(m, "_probe", fake_probe)
    monkeypatch.setattr(settings, "LLM_BACKEND", "ollama")
    res = asyncio.run(m.test_generation(m.SmokeTest(model="qwen2.5:3b")))
    planned = rt.plan_context(rt.profile_from_show(QWEN3B_SHOW, QWEN3B_SIZE), gpu(4.0), settings.OLLAMA_NUM_CTX,
                              concurrency=settings.OLLAMA_CONCURRENCY)["num_ctx"]
    assert captured[0]["options"]["num_ctx"] == planned == res["num_ctx"]
    assert res["processor"] == "100% GPU"
