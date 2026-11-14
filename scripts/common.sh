#!/usr/bin/env bash
# Shared helpers for setup.sh / start.sh (sourced, not run directly).

c_ok='\033[0;32m'; c_warn='\033[1;33m'; c_err='\033[0;31m'; c_step='\033[0;36m'; c_off='\033[0m'
step() { printf "\n${c_step}==> %s${c_off}\n" "$*"; }
ok()   { printf "    ${c_ok}[ OK ]${c_off} %s\n" "$*"; }
info() { printf "    [INFO] %s\n" "$*"; }
warn() { printf "    ${c_warn}[WARN]${c_off} %s\n" "$*"; }
fail() { printf "    ${c_err}[FAIL]${c_off} %s\n" "$*"; }

# read KEY from a .env file (process environment wins)
env_get() { # file key default
  local file=$1 key=$2 def=$3 val
  val=$(printenv "$key" 2>/dev/null || true)
  if [ -z "$val" ] && [ -f "$file" ]; then
    val=$(grep -E "^[[:space:]]*${key}=" "$file" | tail -1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
  fi
  [ -n "$val" ] && echo "$val" || echo "$def"
}

# curl that never uses a proxy for local URLs
lcurl() { curl --noproxy '*' -s "$@"; }

http_ok() { # url [timeout]
  local code
  code=$(lcurl -o /dev/null -m "${2:-3}" -w '%{http_code}' "$1" 2>/dev/null) || return 1
  [ "${code:0:1}" = "2" ]
}

wait_http() { # url timeout what [pid]
  local url=$1 timeout=$2 what=$3 pid=${4:-} start=$SECONDS
  while (( SECONDS - start < timeout )); do
    if http_ok "$url"; then echo; return 0; fi
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then echo; fail "$what exited early."; return 1; fi
    printf '.'; sleep 0.7
  done
  echo; fail "$what did not become ready within ${timeout}s ($url)."; return 1
}

port_owner() { # port -> "pid name" or empty
  local port=$1 out
  if command -v lsof >/dev/null 2>&1; then
    out=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $2, $1}')
  elif command -v ss >/dev/null 2>&1; then
    out=$(ss -ltnpH "sport = :$port" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)",pid=\([0-9]*\).*/\2 \1/p' | head -1)
  fi
  echo "$out"
}

url_port() { local p; p=$(echo "$1" | sed -n 's#.*://[^/:]*:\([0-9][0-9]*\).*#\1#p'); echo "${p:-11434}"; }

# Make sure Ollama answers; NEVER start a second server when one is running.
ensure_ollama() { # base_url parallel timeout logdir
  local url=$1 parallel=${2:-1} timeout=${3:-60} logdir=${4:-.} port owner
  if http_ok "$url/api/tags"; then ok "Ollama is already running at $url - not starting another instance."; return 0; fi
  port=$(url_port "$url"); owner=$(port_owner "$port")
  if [ -n "$owner" ]; then
    if echo "$owner" | grep -qi ollama; then
      info "Ollama (PID ${owner%% *}) holds port $port but is not answering yet - waiting."
      wait_http "$url/api/tags" "$timeout" "Ollama" && { ok "Ollama is answering at $url."; return 0; }
      fail "Ollama is listening on port $port but $url/api/tags does not answer. Restart Ollama."; return 1
    fi
    fail "Port $port is used by '${owner#* }' (PID ${owner%% *}), not by Ollama, and $url/api/tags does not answer like Ollama."
    info "Stop that program, or run Ollama on another port (OLLAMA_HOST=127.0.0.1:11500 ollama serve) and set OLLAMA_BASE_URL accordingly."
    return 1
  fi
  if ! command -v ollama >/dev/null 2>&1; then
    fail "Ollama is not running and the 'ollama' command was not found. Install it from https://ollama.com/download"
    return 1
  fi
  info "Ollama is not running - starting: ollama serve (log: $logdir/ollama.log)"
  mkdir -p "$logdir"
  OLLAMA_NUM_PARALLEL="$parallel" nohup ollama serve > "$logdir/ollama.log" 2>&1 &
  local opid=$!
  if wait_http "$url/api/tags" "$timeout" "Ollama" "$opid"; then ok "Ollama started (PID $opid). It keeps running after this script exits."; return 0; fi
  tail -20 "$logdir/ollama.log" 2>/dev/null; return 1
}

# List models and validate the configured one (never substitutes a model).
show_models() { # base_url model python
  local url=$1 model=$2 py=${3:-python3}
  lcurl -m 5 "$url/api/tags" | "$py" -c '
import json, sys
want = sys.argv[1].strip().lower()
want = want if ":" in want.rsplit("/", 1)[-1] else want + ":latest"
try:
    models = json.load(sys.stdin).get("models", [])
except Exception as e:
    print("    [WARN] could not read the model list:", e); sys.exit(1)
def vision(m):
    n = m.get("name", "").lower(); fam = [f.lower() for f in (m.get("details") or {}).get("families") or []]
    return any(h in n for h in ("moondream", "llava", "vision", "minicpm-v", "qwen2-vl", "qwen2.5vl")) or bool({"clip", "mllama"} & set(fam))
text = [m["name"] for m in models if not vision(m)]; vis = [m["name"] for m in models if vision(m)]
print("    [INFO] Installed text models :", ", ".join(text) or "none")
if vis: print("    [INFO] Vision (OCR-only)     :", ", ".join(vis))
names = [m["name"].lower() if ":" in m["name"] else m["name"].lower() + ":latest" for m in models]
if want in names:
    print("    [ OK ] Configured model \x27%s\x27 is installed." % sys.argv[1]); sys.exit(0)
print("    [WARN] Configured model \x27%s\x27 (OLLAMA_MODEL) is NOT installed. Run:  ollama pull %s   (or set OLLAMA_MODEL in backend/.env / pick a model in the UI)" % (sys.argv[1], sys.argv[1]))
sys.exit(2)
' "$model"
}

port_free() { # port what
  local owner; owner=$(port_owner "$1")
  if [ -n "$owner" ]; then fail "Port $1 (needed for the $2) is already used by '${owner#* }' (PID ${owner%% *})."; return 1; fi
  return 0
}
