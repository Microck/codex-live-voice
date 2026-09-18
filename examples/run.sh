#!/usr/bin/env bash
# Serves examples/web + /client at the root and mounts the gptlive API under /api/voice.
# Requires: pip install fastapi uvicorn; a signed-in codex CLI >= 0.154 on this machine.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python examples/server.py
