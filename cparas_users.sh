#!/usr/bin/env bash
# CPARAS account administration (v0.5.0) - Linux / macOS. Windows: cparas_users.cmd.
# Runs `python -m auth.cli <command>` with backend/.venv from the backend folder (same database as
# the app; works whether or not the app is running). Example: ./cparas_users.sh list-users
# Commands: create-superadmin | create-user | seed-test-users | reset-password | set-role |
#           set-active | list-users   (see backend/auth/cli.py, or run with --help)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || { echo "backend/.venv not found - run ./setup.sh first." >&2; exit 1; }
cd "$ROOT/backend" && exec "$PY" -m auth.cli "${@:---help}"
