#!/usr/bin/env bash
# ============================================================================
# CPARAS Platform — start (Linux / macOS). Windows: use start.ps1 / start.cmd.
#   * reuses a running Ollama (never starts a second one), starts it only if
#     nothing answers, validates OLLAMA_MODEL
#   * starts backend + frontend and waits for them by polling real endpoints
#   * Ctrl+C stops backend + frontend (Ollama keeps running)
# Usage: ./start.sh [--backend-only]
# ============================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/scripts/common.sh"
BACKEND="$ROOT/backend"; FRONTEND="$ROOT/frontend"; LOGDIR="$ROOT/logs"; mkdir -p "$LOGDIR"
BACKEND_ONLY=0; [ "${1:-}" = "--backend-only" ] && BACKEND_ONLY=1

printf "\n  CPARAS Platform - start\n  ------------------------------------------------------------\n"

step "Checking the Python environment"
PY="$BACKEND/.venv/bin/python"
if [ ! -x "$PY" ]; then fail "backend/.venv not found - run ./setup.sh first."; exit 1; fi
ok "Python $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
[ -f "$BACKEND/.env" ] || { cp "$BACKEND/.env.example" "$BACKEND/.env"; info "Created backend/.env from .env.example."; }
ENVF="$BACKEND/.env"
BACKEND_PORT=$(env_get "$ENVF" PORT 8000)
BACKEND_HOST=$(env_get "$ENVF" HOST 127.0.0.1)
FRONTEND_PORT=5173
LLM_BACKEND=$(env_get "$ENVF" LLM_BACKEND ollama)
OLLAMA_URL=$(env_get "$ENVF" OLLAMA_BASE_URL http://127.0.0.1:11434); OLLAMA_URL=${OLLAMA_URL%/}
OLLAMA_MODEL=$(env_get "$ENVF" OLLAMA_MODEL qwen2.5:7b)
PARALLEL=$(env_get "$ENVF" OLLAMA_CONCURRENCY 1)
# v0.4.2: Ollama SERVER options (apply when this script starts Ollama).
case "$(env_get "$ENVF" OLLAMA_FLASH_ATTENTION false | tr 'A-Z' 'a-z')" in 1|true|yes) export OLLAMA_FLASH_ATTENTION=1 ;; esac
_kv=$(env_get "$ENVF" OLLAMA_KV_CACHE_TYPE f16)
[ -n "$_kv" ] && [ "$_kv" != "f16" ] && export OLLAMA_KV_CACHE_TYPE="$_kv"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/    [ OK ] NVIDIA GPU: /'
fi

if [ $BACKEND_ONLY -eq 0 ]; then
  step "Checking Node.js"
  command -v node >/dev/null 2>&1 || { fail "Node.js not found. Install Node.js 18+."; exit 1; }
  ok "Node.js $(node --version)"
  VITE="$FRONTEND/node_modules/vite/bin/vite.js"
  [ -f "$VITE" ] || { fail "Frontend dependencies missing - run ./setup.sh (or: cd frontend && npm ci)."; exit 1; }
fi

if [ "$LLM_BACKEND" = "ollama" ]; then
  step "Checking Ollama at $OLLAMA_URL"
  if ensure_ollama "$OLLAMA_URL" "$PARALLEL" 60 "$LOGDIR"; then
    step "Checking installed models"; show_models "$OLLAMA_URL" "$OLLAMA_MODEL" "$PY" || true
  else
    warn "The local LLM is not available: CPARAS ISLD analysis needs it (CPARAS Bulk scoring does not). The app will start and show the exact problem in the UI."
  fi
fi

BPID=""; FPID=""
cleanup() {
  echo; echo "Stopping..."
  [ -n "$FPID" ] && kill "$FPID" 2>/dev/null
  [ -n "$BPID" ] && kill "$BPID" 2>/dev/null
  wait 2>/dev/null; echo "Stopped backend and frontend."
}
trap cleanup EXIT
trap 'exit 130' INT TERM

step "Starting the backend on http://$BACKEND_HOST:$BACKEND_PORT"
port_free "$BACKEND_PORT" backend || exit 1
( cd "$BACKEND" && exec "$PY" -m uvicorn main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" ) > "$LOGDIR/backend.log" 2>&1 &
BPID=$!
info "Backend PID $BPID - log: logs/backend.log"
HEALTH="http://127.0.0.1:$BACKEND_PORT/api/health"
wait_http "$HEALTH" 180 "Backend" "$BPID" || { tail -30 "$LOGDIR/backend.log"; exit 1; }
ok "Backend is ready."

if [ $BACKEND_ONLY -eq 0 ]; then
  step "Starting the frontend on http://127.0.0.1:$FRONTEND_PORT"
  port_free "$FRONTEND_PORT" frontend || exit 1
  ( cd "$FRONTEND" && API_TARGET="http://127.0.0.1:$BACKEND_PORT" exec node "$VITE" --port "$FRONTEND_PORT" --strictPort ) > "$LOGDIR/frontend.log" 2>&1 &
  FPID=$!
  wait_http "http://127.0.0.1:$FRONTEND_PORT/" 90 "Frontend" "$FPID" || { tail -20 "$LOGDIR/frontend.log"; exit 1; }
  ok "Frontend is ready."
fi

printf "\n  ------------------------------------------------------------\n  ${c_ok}CPARAS Platform is running${c_off}\n"
[ $BACKEND_ONLY -eq 0 ] && echo "    App        : http://127.0.0.1:$FRONTEND_PORT"
echo "    API docs   : http://127.0.0.1:$BACKEND_PORT/api/docs"
# v0.5.0: sign-in is required; say how to create the first account if there is none yet.
lcurl -m 5 "http://127.0.0.1:$BACKEND_PORT/api/auth/setup-status" | "$PY" -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
if d.get("needs_setup"):
    print("    Sign-in    : NO ACCOUNT YET - create the first Super Admin: ./create_admin.sh")
    print("                 or open the app and use First-time setup (token file: %s)" % d.get("setup_token_file"))
else:
    print("    Sign-in    : required - accounts: Administration > User Management (Super Admin)")
'
lcurl -m 10 "$HEALTH" | "$PY" -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
l = d.get("llm") or {}
print("    Status     :", d.get("status"))
print("    LLM server :", "connected" if l.get("reachable") else "not reachable", "(%s)" % l.get("base_url"))
if l.get("reachable"):
    print("    Models     : %d text, %d vision" % (len(l.get("text_models") or []), len(l.get("vision_models") or [])))
    print("    Default    : %s (%s)" % (l.get("configured_model"), "installed" if l.get("configured_model_available") else "NOT installed"))
elif l.get("error"): print("    LLM error  :", l.get("error"))
' || true
lcurl -m 60 "http://127.0.0.1:$BACKEND_PORT/api/retrieval-index/status" | "$PY" -c '
import json, sys
try: r = json.load(sys.stdin)
except Exception: sys.exit(0)
if r.get("ok"): print("    3GPP index : %s (%s documents)" % (r.get("dir"), format(r.get("documents") or 0, ",")))
elif r.get("configured"): print("    3GPP index : NOT usable -", (r.get("problems") or [{}])[0].get("message"))
else: print("    3GPP index : not installed - copy retrieval_data into", r.get("expected_dir"))
' || true
echo "    Logs       : $LOGDIR"
echo "  Press Ctrl+C to stop the backend and frontend (Ollama keeps running)."
echo "  ------------------------------------------------------------"
while true; do
  sleep 2
  kill -0 "$BPID" 2>/dev/null || { fail "Backend stopped."; tail -30 "$LOGDIR/backend.log"; exit 1; }
  if [ -n "$FPID" ]; then kill -0 "$FPID" 2>/dev/null || { fail "Frontend stopped."; tail -20 "$LOGDIR/frontend.log"; exit 1; }; fi
done
