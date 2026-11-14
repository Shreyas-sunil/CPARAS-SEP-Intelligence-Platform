"""
Test isolation: every test session uses a throw-away data directory, so
running the suite never writes into (or reads from) the real backend/data
folder — no leftover databases, vector stores or knowledge packages end up
in a release. Individual tests that need their own paths still override
these variables themselves.

Also makes sure no test can reach a real Ollama by accident: the LLM URL
points at a closed local port unless a test patches the HTTP client.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="pytest-data-")
_DEFAULTS = {
    "DATA_DIR": _TMP,
    "UPLOADS_DIR": os.path.join(_TMP, "uploads"),
    "REPORTS_DIR": os.path.join(_TMP, "reports"),
    "DATASETS_DIR": os.path.join(_TMP, "datasets"),
    "CACHE_DIR": os.path.join(_TMP, "cache"),
    "MODELS_DIR": os.path.join(_TMP, "models"),
    "CHROMADB_PATH": os.path.join(_TMP, "chromadb"),
    "FAISS_INDEX_PATH": os.path.join(_TMP, "faiss_index"),
    "ETSI_DATASET_IMPORT_DIR": os.path.join(_TMP, "etsi_corpus", "imports"),
    "ETSI_DATASET_OUTPUT": os.path.join(_TMP, "etsi_corpus", "output"),
    "ETSI_DATASET_DIR": os.path.join(_TMP, "etsi_v0.3"),
    "THREEGPP_ARCHIVE_DIR": os.path.join(_TMP, "3gpp_archive"),
    "DATABASE_URL": "sqlite+aiosqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/"),
    "OLLAMA_BASE_URL": "http://127.0.0.1:9",   # discard port: nothing listens
    "EMBEDDING_ALLOW_DOWNLOAD": "false",
    "ANONYMIZED_TELEMETRY": "False",
}
for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# Allow `python -m pytest tests` from backend/ and `pytest backend/tests` from the root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
