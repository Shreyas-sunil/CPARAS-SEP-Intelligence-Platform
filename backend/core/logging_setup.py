"""
Logging setup shared by the backend entry point.

* Every line carries the service name, so logs from CPARAS ISLD, CPARAS
  Platform and Initial Categoriser can be told apart when they run side by
  side (they often do, on one machine).
* Successful high-frequency polling requests (health / progress / job
  status) are dropped from uvicorn's access log — they otherwise bury the
  lines that matter. Failed polls (4xx/5xx) are still logged.
"""

import logging
import re

# Paths polled by the frontends every 1-30 s.
_POLL_PATHS = re.compile(
    r'"GET /api/(health|models/status|models/available|analysis/progress/[^ ]+|'
    r'categoriser/dataset/jobs/\d+|sep/runs/\d+(/status)?|scrape/jobs[^ ]*|etsi-corpus/[^ ]*status[^ ]*)'
    r'(\?[^ ]*)? HTTP/[0-9.]+" 2\d\d'
)


class _DropSuccessfulPolls(logging.Filter):
    """uvicorn access-log filter that hides the high-frequency progress/status polls the UI makes every
    second (matched by _POLL_PATHS), so the log keeps real requests and errors.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        """False for a poll line (drop it); any error while matching keeps the record rather than
        losing log output.
        """
        try:
            return not _POLL_PATHS.search(record.getMessage())
        except Exception:
            return True


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure root logging once for the backend process: one format with the service name, the given
    level, the poll filter on uvicorn's access log, and noisy third-party loggers capped at WARNING.
    """
    lvl = getattr(logging, (level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format=f"%(asctime)s [%(levelname)s] {service} %(name)s: %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(_DropSuccessfulPolls())
    # Third-party chatter that is not useful at INFO.
    for noisy in ("httpx", "httpcore", "chromadb.telemetry", "urllib3"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))


async def log_llm_startup_status(logger: logging.Logger) -> None:
    """One clear line at startup saying whether the LLM is usable, and
    exactly why not if it isn't. Never raises, never blocks startup."""
    try:
        from core.llm_engine import get_llm
        p = await get_llm().health_check()
    except Exception as e:  # pragma: no cover
        logger.warning(f"LLM status check at startup failed: {type(e).__name__}: {e}")
        return
    if p.get("available"):
        logger.info(
            f"LLM ready: {p.get('backend')} at {p.get('base_url')} "
            f"(version {p.get('version') or '?'}), {len(p.get('models') or [])} model(s) installed, "
            f"configured model '{p.get('configured_model')}' is installed."
        )
    else:
        logger.warning(
            f"LLM not ready [{p.get('state')}]: {p.get('error')} {p.get('hint') or ''}".strip()
        )
    for w in p.get("warnings") or []:
        logger.warning(f"LLM: {w}")
