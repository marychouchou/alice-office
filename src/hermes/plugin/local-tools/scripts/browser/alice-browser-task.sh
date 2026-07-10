#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${ALICE_BROWSER_VENV:-/home/alice_gx10/.openclaw/tools/browser/venv}"
exec "$VENV/bin/python" "$SCRIPT_DIR/alice-browser-task.py" "$@"
