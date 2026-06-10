#!/usr/bin/env bash
# Development server — runs the control plane with auto-reload against a local dev DB.
# Does NOT require the sipsmith-agent to be running; agent calls will fail gracefully.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)"
cd "${ROOT}"

# Use a local config if present, otherwise fall back to example
if [[ -f "config.yaml" ]]; then
  export SIPSMITH_CONFIG="${ROOT}/config.yaml"
  echo "[dev] Using config: ${SIPSMITH_CONFIG}"
elif [[ -f "${ROOT}/config.example.yaml" ]]; then
  echo "[dev] No config.yaml found. Copy config.example.yaml → config.yaml and fill in values."
  echo "      For dev with SQLite fallback, set database.url to: sqlite+aiosqlite:///./dev.db"
  exit 1
fi

# Fetch frontend assets if htmx stub is still just a comment
if grep -q "must be populated" sipsmith/ui/static/js/htmx.min.js 2>/dev/null; then
  echo "[dev] Fetching frontend assets (requires internet)..."
  bash scripts/fetch-assets.sh
fi

echo "[dev] Starting SIPsmith in reload mode..."
uvicorn sipsmith.app:create_app \
  --factory \
  --host 0.0.0.0 \
  --port 8443 \
  --reload \
  --reload-dir sipsmith \
  --reload-dir plugins \
  --log-level info
