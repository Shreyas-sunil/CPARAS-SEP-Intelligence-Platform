"""
The LLM health contract (core/ollama_client.py) — one schema, every state.

Regression target: OllamaBackend.health_check() used to return
{"status": "ok", "available_models": [...]} while /api/health read
result.get("available"), so a perfectly healthy Ollama was always reported
as "Local LLM offline". These tests pin the contract end to end
(probe -> backend.health_check() -> GET /api/health) without a real Ollama.
"""
import asyncio
import json

import httpx
import pytest

import core.ollama_client as oc

REFERENCE_MODELS = ["phi3.5:3.8b-mini-instruct-q4_K_M", "moondream:latest", "qwen2.5:3b", "qwen2.5:7b"]


def _tags(names):
    out = []
    for n in names:
        fam = ["phi2", "clip"] if "moondream" in n else ["qwen2"]
        out.append({"name": n, "model": n, "size": 1, "details": {"family": fam[0], "families": fam,
                                                                  "parameter_size": "7B", "quantization_level": "Q4_K_M"}})
    return {"models": out}


def _install_transport(monkeypatch, handler):
    def factory(base_url, timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    monkeypatch.setattr(oc, "ollama_http_client", factory)


def _ollama(names, gen_status=200, gen_body=None):
    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_tags(names))
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.9.0"})
        if request.url.path == "/api/generate":
            return httpx.Response(gen_status, json=gen_body or {"response": "OK", "done": True})
        return httpx.Response(404)
    return handler


def probe(model="qwen2.5:7b", vision="moondream"):
    return asyncio.run(oc.probe_ollama("http://127.0.0.1:11434", model, vision, timeout=2))


def test_ready_with_reference_models(monkeypatch):
    _install_transport(monkeypatch, _ollama(REFERENCE_MODELS))
    r = probe()
    assert r["reachable"] is True and r["available"] is True and r["state"] == "ready"
    assert r["configured_model_available"] is True and r["configured_model_resolved"] == "qwen2.5:7b"
    assert r["version"] == "0.9.0"
    # moondream is a vision model: never offered as a text-analysis model
    assert "moondream:latest" in r["vision_models"]
    assert "moondream:latest" not in r["text_models"]
    assert set(r["text_models"]) == {"phi3.5:3.8b-mini-instruct-q4_K_M", "qwen2.5:3b", "qwen2.5:7b"}
    # bare "moondream" resolves to the installed ":latest" tag
    assert r["vision_model_available"] is True
    # /api/tags answering is NOT evidence that generation works
    assert r["generation"]["status"] in ("untested", "ready", "failed")


def test_configured_model_missing_is_reported_not_substituted(monkeypatch):
    _install_transport(monkeypatch, _ollama(REFERENCE_MODELS))
    r = probe(model="mistral:7b-instruct-q4_K_M")
    assert r["reachable"] is True
    assert r["available"] is False
    assert r["state"] == "model_missing"
    assert r["configured_model_resolved"] is None
    assert "mistral:7b-instruct-q4_K_M" in r["error"] and "ollama pull" in r["hint"]


def test_no_models(monkeypatch):
    _install_transport(monkeypatch, _ollama([]))
    r = probe()
    assert r["reachable"] is True and r["available"] is False and r["state"] == "no_models"


def test_unreachable(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    _install_transport(monkeypatch, handler)
    r = probe()
    assert r["reachable"] is False and r["available"] is False and r["state"] == "unreachable"
    assert "already running" in r["hint"]  # never a blind "run ollama serve"


def test_port_owned_by_something_else(monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(404, text="<html>nginx</html>"))
    r = probe()
    assert r["reachable"] is False and r["state"] == "not_ollama"


def test_every_state_has_the_full_schema(monkeypatch):
    keys = set(oc._empty_result("u", "m", "v"))
    for handler in (_ollama(REFERENCE_MODELS), _ollama([]),
                    lambda req: httpx.Response(500, text="x")):
        _install_transport(monkeypatch, handler)
        assert keys <= set(probe())


def test_validate_model_choice(monkeypatch):
    _install_transport(monkeypatch, _ollama(REFERENCE_MODELS))
    r = probe()
    assert oc.validate_model_choice("qwen2.5:3b", r) == ("qwen2.5:3b", None)
    resolved, err = oc.validate_model_choice("moondream", r, purpose="text")
    assert resolved is None and "vision" in err
    resolved, err = oc.validate_model_choice("llama3:8b", r)
    assert resolved is None and "not installed" in err
    assert oc.validate_model_choice("moondream", r, purpose="vision") == ("moondream:latest", None)


def test_generation_smoke_test_states(monkeypatch):
    oc.reset_generation_state()
    _install_transport(monkeypatch, _ollama(REFERENCE_MODELS))
    g = asyncio.run(oc.generation_smoke_test("http://127.0.0.1:11434", "qwen2.5:7b", timeout=2))
    assert g["status"] == "ready" and g["model"] == "qwen2.5:7b"
    _install_transport(monkeypatch, _ollama(REFERENCE_MODELS, gen_status=500,
                                            gen_body={"error": "out of memory"}))
    g = asyncio.run(oc.generation_smoke_test("http://127.0.0.1:11434", "qwen2.5:7b", timeout=2))
    assert g["status"] == "failed" and "out of memory" in g["error"]
    oc.reset_generation_state()


def test_loopback_client_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://corporate-proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://corporate-proxy.invalid:8080")
    assert oc._is_loopback_url("http://127.0.0.1:11434")
    assert oc._is_loopback_url("http://localhost:11434")
    assert not oc._is_loopback_url("http://ollama.internal:11434")
    client = oc.ollama_http_client("http://127.0.0.1:11434", 1)
    try:
        assert client._trust_env is False
    finally:
        asyncio.run(client.aclose())


def test_backend_health_check_and_api_health_agree(monkeypatch):
    """The exact bug: a healthy Ollama must make /api/health's llm report ok."""
    _install_transport(monkeypatch, _ollama(REFERENCE_MODELS))
    from core.config import settings
    monkeypatch.setattr(settings, "LLM_BACKEND", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen2.5:7b")
    import core.llm_engine as le
    monkeypatch.setattr(le, "_llm_instance", None)
    result = asyncio.run(le.get_llm().health_check())
    assert result["available"] is True

    # The exact consumer that used to misread the contract.
    try:
        from api.routes.shared import health as health_route   # CPARAS ISLD / Platform
    except ImportError:
        from api.routes import health as health_route          # Initial Categoriser
    llm = asyncio.run(health_route._check_llm())
    assert llm["status"] == "ok", json.dumps(llm)[:500]
    assert llm["reachable"] is True and llm["configured_model_available"] is True
    monkeypatch.setattr(le, "_llm_instance", None)
