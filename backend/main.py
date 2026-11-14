"""
CPARAS — C-DOT SEP Intelligence Platform — FastAPI Backend
===========================================================
Products: CPARAS Bulk (routes under api/routes/gitas/, mounted at /api/sep)
and CPARAS ISLD (routes under api/routes/cparas/, mounted at /api/analysis
and /api/text-match). Internal module paths keep their historical names.
Fully offline, local LLM-powered SEP/ETSI/3GPP analysis engine.
No cloud APIs required. Supports Ollama, llama.cpp, vLLM backends.
"""

import os

# Must be set before chromadb is imported anywhere: no telemetry, ever.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from api.routes.gitas import dashboard as sep, datasets, scrape, scrape_maintenance, etsi_corpus
from api.routes.cparas import analysis, text_match
from api.routes.shared import health, models, search, retrieval_index
from api.routes.shared import auth as auth_routes, users as user_routes
from auth.dependencies import enforce_access_policy
from core.config import settings
from core.database import init_db
from core.logging_setup import configure_logging, log_llm_startup_status
from core.version import APP_VERSION
from rag.vector_store import init_vector_store

configure_logging("cparas-platform", settings.LOG_LEVEL)
logger = logging.getLogger("cparas_platform")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle.

    IMPORTANT: this NEVER triggers ETSI PDF ingestion. `ensure_dataset_
    available()` only inspects/logs whether a pre-built Standards Corpus package
    exists at ETSI_DATASET_DIR -- the 54k-document pipeline is a one-time,
    explicit build step (CLI `ingest`/`package build`, or the
    POST /api/etsi-corpus/ingest/* endpoints), never something the app
    itself kicks off on startup. See etsi_corpus/package.py.
    """
    logger.info("Initializing CPARAS Platform (CPARAS Bulk + CPARAS ISLD)...")
    await init_db()
    await _auth_startup()
    await init_vector_store()
    # v0.5.0: delete demo chunks left by the retired demo-corpus switch (no-op normally).
    from ingestion.legacy_cleanup import remove_legacy_demo_chunks
    await remove_legacy_demo_chunks()
    from etsi_corpus.package import ensure_dataset_available
    await ensure_dataset_available()
    from api.routes.gitas.dashboard import recover_interrupted_runs
    await recover_interrupted_runs()
    await log_llm_startup_status(logger)
    warm = None
    if settings.WARMUP_ON_STARTUP:
        warm = asyncio.get_running_loop().create_task(_warmup())
    logger.info("CPARAS Platform backend ready (interactive API docs at /api/docs).")
    yield
    if warm is not None and not warm.done():
        warm.cancel()
    logger.info("Shutting down.")


async def _auth_startup() -> None:
    """v0.5.0: housekeeping for authentication at start-up -- drop dead
    sessions and, while NO account exists yet, (re)create the one-time
    first-run setup token and tell the operator how to create the first
    super admin. Never creates an account itself."""
    from core.database import AsyncSessionLocal
    from auth import service as auth_service
    async with AsyncSessionLocal() as db:
        removed = await auth_service.purge_expired_sessions(db)
        token_file = await auth_service.ensure_setup_token(db)
        await db.commit()
    if removed:
        logger.info(f"Auth: removed {removed} expired session(s).")
    if token_file:
        logger.warning(
            "Auth: no CPARAS user exists yet. Create the first Super Admin either\n"
            "  * in the browser: open the app, choose 'First-time setup' and paste the one-time token from\n"
            f"      {token_file}\n"
            "  * or on this machine: cd backend && python -m auth.cli create-superadmin\n"
            "    (Windows: double-click create_admin.cmd in the project folder)."
        )


async def _warmup() -> None:
    """v0.4.7 speed: pay the one-time costs in the background right after startup instead of inside the
    first analysis - load the embedding model, validate the retrieval index, load (or build and cache)
    the Bulk clause corpus and its lexical index. Every step is optional and failures are only logged;
    the API is already serving while this runs."""
    import time
    t0 = time.time()
    await asyncio.sleep(0.5)
    # 1. the LLM first, so Ollama loads the analysis model (same options/num_ctx as an analysis) into VRAM
    #    before the embedding model takes its share - the first ISLD analysis then skips the model load.
    if settings.LLM_BACKEND == "ollama":
        try:
            from core.llm_engine import get_llm
            llm = get_llm()
            probe = await llm.health_check()
            if isinstance(probe, dict) and probe.get("reachable") and probe.get("configured_model_available"):
                await asyncio.wait_for(llm.generate("Reply OK.", system="", temperature=0.0, max_tokens=1,
                                                    json_mode=False), timeout=180)
                logger.info(f"Warm-up: {settings.OLLAMA_MODEL} loaded in Ollama.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.info(f"Warm-up: LLM not pre-loaded ({type(e).__name__}: {e}).")
    try:
        from rag.vector_store import _embedder
        await _embedder._get_model_async()
    except Exception as e:  # noqa: BLE001 - unavailable model is reported elsewhere
        logger.info(f"Warm-up: embedding model not loaded ({e}).")
    try:
        from services import sqlite_retrieval as sr
        if await asyncio.to_thread(sr.available):
            from research.bulk_corpus import get_bulk_corpus
            corpus = await asyncio.to_thread(get_bulk_corpus)
            if corpus.source in ("dataset", "retrieval") and len(corpus.entries) > settings.BULK_EXHAUSTIVE_MAX_ENTRIES:
                from research import bulk_fast
                await asyncio.to_thread(bulk_fast.lexical_index_for, corpus)
                try:
                    from rag.vector_store import LocalEmbedder
                    vc = await asyncio.to_thread(bulk_fast.vector_cache_for, corpus, settings.EMBEDDING_MODEL,
                                                 LocalEmbedder.precision, settings.BULK_CORPUS_EMBED_MAX_TOKENS)
                    bulk_fast.schedule_background_fill(*bulk_fast.caches_for(corpus, vc))
                except Exception as e:  # noqa: BLE001
                    logger.info(f"Warm-up: Bulk vector cache not prepared ({e}).")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.info(f"Warm-up: retrieval index / Bulk corpus not prepared ({e}).")
    logger.info(f"Warm-up finished in {time.time() - t0:.1f}s.")


app = FastAPI(
    title="CPARAS — C-DOT SEP Intelligence Platform",
    description="Offline AI-powered SEP/ETSI/3GPP patent analysis — CPARAS Bulk (bulk/portfolio "
                "declaration analysis) and CPARAS ISLD (single-declaration deep analysis).",
    version=APP_VERSION,
    lifespan=lifespan,
    # v0.5.0 RBAC: every route passes the access policy (auth/policy.py) before
    # its handler runs -- deny by default, see auth/dependencies.py.
    dependencies=[Depends(enforce_access_policy)],
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# CORS — only this application's own local frontends (Vite dev server,
# `vite preview`, the Docker nginx frontend). The API is bound to 127.0.0.1
# by default; CORS_EXTRA_ORIGINS adds more origins explicitly if needed.
_CORS_ORIGINS = [
    f"http://{h}:{p}" for h in ("localhost", "127.0.0.1") for p in (5173, 4173, 3000)
] + [o.strip() for o in settings.CORS_EXTRA_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    # v0.5.0: the session cookie must travel with requests from an explicitly
    # allowed origin (never "*", which browsers refuse with credentials).
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-CPARAS-Auth", "X-CPARAS-Permission", "Content-Disposition"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Register route groups
app.include_router(health.router,    prefix="/api",      tags=["health"])
# v0.5.0: authentication + user administration (super admin)
app.include_router(auth_routes.router, prefix="/api/auth", tags=["auth"])
app.include_router(user_routes.router, prefix="/api/auth", tags=["user-admin"])
app.include_router(analysis.router,  prefix="/api/analysis", tags=["analysis"])
app.include_router(models.router,    prefix="/api/models",   tags=["models"])
app.include_router(search.router,    prefix="/api/search",   tags=["search"])
app.include_router(datasets.router,  prefix="/api/datasets", tags=["datasets"])
app.include_router(sep.router,       prefix="/api/sep",     tags=["sep-dashboard"])
app.include_router(text_match.router, prefix="/api/text-match", tags=["text-matching"])
app.include_router(scrape.router,     prefix="/api/scrape",   tags=["scraper"])
app.include_router(scrape_maintenance.router, prefix="/api/scrape", tags=["scraper-maintenance"])
# v0.3: shared ETSI Standards Corpus -- the manifest-driven pipeline that
# replaces the 3GPP crawler (still mounted above at /api/scrape, now
# secondary/legacy) as the primary way to build the platform's standards
# Standards Corpus. Feeds the same vector store CPARAS ISLD and CPARAS Bulk
# retrieve from (see services/retrieval_service.py).
app.include_router(etsi_corpus.router, prefix="/api/etsi-corpus", tags=["etsi-standards-corpus"])
app.include_router(retrieval_index.router, prefix="/api/retrieval-index", tags=["retrieval-index"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=1,  # single worker for GPU memory sharing
    )
