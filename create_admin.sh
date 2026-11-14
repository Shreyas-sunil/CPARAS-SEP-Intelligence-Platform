#!/usr/bin/env bash
# Create a CPARAS Super Admin (first-time setup, v0.5.0). Prompts for username and password.
exec "$(cd "$(dirname "$0")" && pwd)/cparas_users.sh" create-superadmin "$@"
