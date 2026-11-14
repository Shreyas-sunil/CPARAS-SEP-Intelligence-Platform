#!/usr/bin/env bash
# ============================================================================
# CPARAS Platform — first-time setup (Linux / macOS). Windows: use setup.ps1 / setup.cmd.
# Checks Python (3.10 - 3.12) and Node.js 18+, creates backend/.venv and
# installs packages, creates backend/.env from .env.example (never
# overwrites it), runs npm ci, checks Ollama and OLLAMA_MODEL, Tesseract,
# and downloads the embedding model for offline use.
# Safe to re-run.
# Usage: ./setup.sh [--pull-model] [--skip-embedding-download]
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/scripts/common.sh"
BACKEND="$ROOT/backend"; FRONTEND="$ROOT/frontend"; LOGDIR="$ROOT/logs"; mkdir -p "$LOGDIR"
PULL=0; SKIP_EMB=0
for a in "$@"; do case "$a" in --pull-model) PULL=1;; --skip-embedding-download) SKIP_EMB=1;; esac; done
PROBLEMS=()

printf "\n  CPARAS Platform - setup\n  ------------------------------------------------------------\n"

step "Checking Python"
PYBIN=""
for c in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PYBIN=$c; break; fi
done
[ -n "$PYBIN" ] || { fail "Python 3.10+ not found. Install Python 3.10 - 3.12."; exit 1; }
PYV=$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
"$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)' || warn "Python $PYV is newer than the tested range (3.10 - 3.12); if installation fails, install Python 3.12."
ok "Python $PYV ($PYBIN)"

step "Checking Node.js"
command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || { fail "Node.js 18+ (with npm) not found. Install it from https://nodejs.org"; exit 1; }
NODE_MAJOR=$(node --version | sed 's/v//' | cut -d. -f1)
[ "$NODE_MAJOR" -ge 18 ] || { fail "Node.js $(node --version) is too old; install 18+."; exit 1; }
ok "Node.js $(node --version)"

step "Setting up the backend (virtual environment + packages)"
PY="$BACKEND/.venv/bin/python"
if [ ! -x "$PY" ]; then "$PYBIN" -m venv "$BACKEND/.venv" || { fail "Could not create backend/.venv (on Debian/Ubuntu: apt install python3-venv)."; exit 1; }; ok "Created backend/.venv"; else ok "backend/.venv already exists"; fi
"$PY" -m pip install -q --upgrade pip --disable-pip-version-check
info "Installing Python packages (this can take several minutes the first time)..."
"$PY" -m pip install -r "$BACKEND/requirements.txt" --disable-pip-version-check || { fail "pip install failed - see above."; exit 1; }
ok "Backend packages installed"
if [ -f "$BACKEND/.env" ]; then ok "backend/.env already exists - left unchanged"; else cp "$BACKEND/.env.example" "$BACKEND/.env"; ok "Created backend/.env from .env.example"; fi
ENVF="$BACKEND/.env"

step "Installing frontend packages (npm ci)"
( cd "$FRONTEND" && npm ci --no-audit --no-fund ) || { fail "npm ci failed - see above."; exit 1; }
ok "Frontend packages installed"

OLLAMA_URL=$(env_get "$ENVF" OLLAMA_BASE_URL http://127.0.0.1:11434); OLLAMA_URL=${OLLAMA_URL%/}
OLLAMA_MODEL=$(env_get "$ENVF" OLLAMA_MODEL qwen2.5:7b)
step "Checking Ollama ($OLLAMA_URL)"
if ! command -v ollama >/dev/null 2>&1 && ! http_ok "$OLLAMA_URL/api/tags"; then
  warn "Ollama is not installed (CPARAS ISLD analysis needs it). Install: https://ollama.com/download (Linux: curl -fsSL https://ollama.com/install.sh | sh)"
  PROBLEMS+=("Ollama not installed")
elif ensure_ollama "$OLLAMA_URL" 1 60 "$LOGDIR"; then
  if ! show_models "$OLLAMA_URL" "$OLLAMA_MODEL" "$PY"; then
    if [ $PULL -eq 1 ]; then
      info "Downloading $OLLAMA_MODEL ..."; ollama pull "$OLLAMA_MODEL" && ok "$OLLAMA_MODEL installed" || PROBLEMS+=("model $OLLAMA_MODEL missing")
    else
      info "Re-run with --pull-model, or run:  ollama pull $OLLAMA_MODEL"; PROBLEMS+=("model $OLLAMA_MODEL not installed")
    fi
  fi
else
  PROBLEMS+=("Ollama not reachable")
fi

step "Checking Tesseract OCR (needed for scanned PDFs)"
if command -v tesseract >/dev/null 2>&1; then ok "Tesseract $(tesseract --version 2>&1 | head -1)"; else
  warn "Tesseract not found - scanned PDFs need it (Ubuntu: sudo apt install tesseract-ocr; macOS: brew install tesseract)."; PROBLEMS+=("Tesseract OCR not installed (scanned PDFs only)"); fi

EMB=$(env_get "$ENVF" EMBEDDING_MODEL BAAI/bge-m3)
if [ $SKIP_EMB -eq 1 ]; then step "Skipping the embedding model download ($EMB)"; else
  step "Downloading the embedding model $EMB (~2 GB, once) for offline use"
  if "$PY" -c 'import sys; from huggingface_hub import snapshot_download; print(snapshot_download(repo_id=sys.argv[1]))' "$EMB"; then ok "Embedding model cached - the backend loads it without network access."
  else warn "Could not download $EMB (offline?). Research analytics fall back to TF-IDF; semantic search / Text Matching need the model. Re-run when online."; PROBLEMS+=("embedding model $EMB not downloaded"); fi
fi

printf "\n  ------------------------------------------------------------\n"
if [ ${#PROBLEMS[@]} -gt 0 ]; then printf "  ${c_warn}Setup finished with warnings:${c_off}\n"; for p in "${PROBLEMS[@]}"; do echo "    - $p"; done; else printf "  ${c_ok}Setup complete.${c_off}\n"; fi
echo "  Start the app with:  ./start.sh"
# v0.5.0: CPARAS requires sign-in. The first Super Admin is created once, here or in the browser.
echo "  First time? Create the Super Admin account:  ./create_admin.sh"
echo "             (or open the app and use First-time setup; test accounts: ./seed_test_users.sh)"
echo "  ------------------------------------------------------------"
