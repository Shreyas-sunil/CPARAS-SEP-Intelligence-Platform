# SEP Intelligence Platform — Backend (CPU / Ollama-remote variant)
# Multi-stage build. Ollama itself is NOT bundled here — it is expected to
# run either on the host (OLLAMA_BASE_URL=http://host.docker.internal:11434)
# or as its own container on the same compose network (OLLAMA_BASE_URL=
# http://ollama:11434). See docker-compose.yml.

# ── Builder stage: compile wheels once, keep the final image slim ──────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime system dependencies:
#   - libmupdf-dev / mupdf tooling is not needed at runtime (PyMuPDF ships
#     its own binaries via the wheel), but tesseract-ocr IS required — the
#     app calls the `tesseract` binary directly (pytesseract) for Tier 3 OCR
#     of scanned/photocopied declarations, and the image previously shipped
#     with no OCR binary at all, silently forcing every scanned PDF through
#     the less-reliable Tier 4 vision-model fallback.
#   - curl is required for the container HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install pre-built wheels from the builder stage (fast, reproducible, no
# compiler toolchain in the final image).
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

# Data directories are created here for local/dev use; in docker-compose
# these paths are volume-mounted, so this is mainly a safety net for
# `docker run` without compose.
RUN mkdir -p data/uploads data/reports data/datasets data/cache data/chromadb models hf_cache \
    && useradd --create-home --uid 1000 sepapp \
    && chown -R sepapp:sepapp /app

USER sepapp

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Single worker: the LLM engine keeps small amounts of in-process state
# (progress store, model singleton) that must not be split across workers,
# and Ollama/llama.cpp GPU memory should not be shared across processes.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
