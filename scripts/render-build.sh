#!/usr/bin/env bash
# Native Python + checked-in plain HTML/CSS/ESM; no Node or disk access.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked --no-dev
test -f frontend/index.html || { printf '%s\n' 'Missing frontend/index.html' >&2; exit 1; }
