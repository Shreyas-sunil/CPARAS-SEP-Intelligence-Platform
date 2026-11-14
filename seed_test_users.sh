#!/usr/bin/env bash
# One TEST account per role with random passwords printed once (v0.5.0). --reset = new passwords.
exec "$(cd "$(dirname "$0")" && pwd)/cparas_users.sh" seed-test-users "$@"
