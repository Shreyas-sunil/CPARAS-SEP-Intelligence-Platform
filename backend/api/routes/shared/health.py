"""
Health / diagnostics route.
=============================
GET /api/health -- a structured status check covering every optional
external dependency this platform touches, so a missing/unreachable piece
(most commonly: Ollama not running yet) is visible as a specific,
actionable status rather than the app just failing somewhere later with
no context.

Overall `status` is one of:
  READY       -- every checked component is reachable
  DEGRADED    -- the backend itself is fine, but an optional component
                 (LLM, vector store) is unreachable -- most features still
                 work (deterministic extraction, search, dashboards); only
                 the LLM-dependent ones (summarisation, essentiality
                 analysis) fall back to their documented degraded paths.
  UNAVAILABLE -- a required component (the database, or corpus storage
                 itself) is unreachable -- the app is not usable.

This intentionally never makes the whole app fail to start just because
Ollama isn't running yet -- that's the whole point of DEGRADED existing
as a distinct state from UNAVAILABLE.
"""
from datetime import datetime, timezone

from fastapi import APIRouter

from core.config import settings
from core.version import APP_VERSION, SERVICE_NAME
from core.llm_engine import get_llm

router = APIRouter()


async def _check_database() -> dict:
    """Run `SELECT 1` against the configured database; {"status": "ok"|"error", ...}. Never raises."""
    try:
        from core.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "backend": "sqlite" if "sqlite" in settings.DATABASE_URL else "other"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def _check_vector_store() -> dict:
    """Vector-store backend and chunk statistics, or an error entry if it is unavailable (e.g. chromadb
    not installed). Never raises.
    """
    try:
        from rag.vector_store import get_vector_store
        vs = get_vector_store()
        stats = await vs.get_stats()
        return {"status": "ok", "backend": settings.VECTOR_BACKEND, **{k: v for k, v in stats.items() if k != "backend"}}
    except Exception as e:
        return {"status": "error", "backend": settings.VECTOR_BACKEND, "detail": str(e)}


def _check_embeddings() -> dict:
    """Is the sentence-transformers model available locally? (No network,
    no model load — just a cache lookup.)"""
    try:
        from rag.vector_store import LocalEmbedder, _embedding_model_is_local
        if LocalEmbedder._model is not None:
            return {"status": "ok", "model": settings.EMBEDDING_MODEL, "loaded": True}
        if LocalEmbedder._load_error:
            not_downloaded = "is not downloaded" in LocalEmbedder._load_error
            return {"status": "missing" if not_downloaded else "error", "model": settings.EMBEDDING_MODEL,
                    "detail": LocalEmbedder._load_error}
        if _embedding_model_is_local(settings.EMBEDDING_MODEL):
            return {"status": "ok", "model": settings.EMBEDDING_MODEL, "loaded": False}
        if settings.EMBEDDING_ALLOW_DOWNLOAD:
            return {"status": "ok", "model": settings.EMBEDDING_MODEL, "loaded": False,
                    "detail": "Not downloaded yet; will be downloaded on first use (EMBEDDING_ALLOW_DOWNLOAD=true)."}
        return {"status": "missing", "model": settings.EMBEDDING_MODEL,
                "detail": f"Embedding model '{settings.EMBEDDING_MODEL}' is not downloaded. Run the setup script, "
                          "or set EMBEDDING_ALLOW_DOWNLOAD=true once while online. Research analytics use the "
                          "offline TF-IDF fallback until then; semantic search / Text Matching need the model."}
    except Exception as e:
        return {"status": "error", "model": settings.EMBEDDING_MODEL, "detail": f"{type(e).__name__}: {e}"}


def _check_ocr() -> dict:
    """Tesseract availability (cached after the first check). Optional:
    only scanned PDFs need it."""
    try:
        from utils.pdf_parser import tesseract_status
        st = tesseract_status()
        return {"status": "ok" if st["available"] else "missing", "engine": "tesseract",
                "version": st.get("version"), "path": st.get("path"), "detail": st.get("error")}
    except Exception as e:
        return {"status": "error", "engine": "tesseract", "detail": f"{type(e).__name__}: {e}"}


async def _check_llm() -> dict:
    """The shared LLM health contract (core/ollama_client.py), plus a
    `status` summary: "ok" only when the configured text model is installed
    and servable. Server reachability, installed models, the configured
    model and generation state are all reported separately — never
    collapsed into one boolean."""
    try:
        result = await get_llm().health_check()
    except Exception as e:  # health must never 500
        return {"status": "error", "backend": settings.LLM_BACKEND, "state": "error",
                "reachable": False, "available": False, "error": f"{type(e).__name__}: {e}",
                "hint": None, "models": [], "text_models": [], "vision_models": [],
                "configured_model": None, "configured_model_available": False, "warnings": []}
    return {"status": "ok" if result.get("available") else result.get("state", "error"), **result}


def _check_corpus_storage() -> dict:
    """Real write/delete probe in the 3GPP crawl output folder, so a read-only or permission-denied
    data folder shows up in health before a crawl fails on it.
    """
    try:
        from ingestion.gpp_crawl import default_out_root
        root = default_out_root()
        root.mkdir(parents=True, exist_ok=True)
        # Real read/write check, not just "the path string is set" --
        # catches a read-only mount or permissions problem before a real
        # crawl hits it.
        probe = root / ".health_check_probe"
        probe.write_text("ok")
        probe.unlink()
        series_dirs = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        return {"status": "ok", "path": str(root), "series_downloaded": len(series_dirs)}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/health")
async def health():
    """GET /api/health - PUBLIC (no sign-in), polled by the UI's status chip and by the start scripts.

    Returns overall READY (everything ok), DEGRADED (database and storage fine, but an optional
    component - vector store, local LLM, embedding model - is not) or UNAVAILABLE (database or
    storage broken), plus a detail object per component and the app version. Contains no user data.
    """
    db = await _check_database()
    vector_store = await _check_vector_store()
    llm = await _check_llm()
    embeddings = _check_embeddings()
    corpus_storage = _check_corpus_storage()

    required_ok = db["status"] == "ok" and corpus_storage["status"] == "ok"
    optional_ok = vector_store["status"] == "ok" and llm["status"] == "ok" and embeddings["status"] == "ok"

    if not required_ok:
        overall = "UNAVAILABLE"
    elif not optional_ok:
        overall = "DEGRADED"
    else:
        overall = "READY"

    return {
        "status": overall,
        "service": SERVICE_NAME,
        "version": APP_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": db,
        "vector_store": vector_store,
        "llm": llm,
        "embeddings": embeddings,
        "ocr": _check_ocr(),
        "corpus_storage": corpus_storage,
        # Enforced, not just claimed: ChromaDB's anonymous telemetry is
        # switched off in main.py / rag/vector_store.py, and nothing else in
        # the backend reports usage anywhere.
        "telemetry": False,
    }
