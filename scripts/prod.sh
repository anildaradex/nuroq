#!/usr/bin/env bash
# Production-style single-port: build the React bundle, FastAPI serves it
# at / and the API at /api/*. One process, one port (8000).

set -e
cd "$(dirname "$0")/.."

echo "▶ Building React bundle…"
(cd frontend && npm install --silent && npm run build)

pkill -9 -f "uvicorn.*backend" 2>/dev/null || true
sleep 1

echo "▶ FastAPI (SPA + API) → http://127.0.0.1:8000"
NUROQ_BACKGROUND_SERVICES=1 ./.venv/bin/uvicorn backend.api:app \
    --port 8000 --log-level info
