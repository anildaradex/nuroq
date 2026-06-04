#!/usr/bin/env bash
# Dev mode: run FastAPI :8000 and Vite :5173 side-by-side.
# Vite proxies /api → :8000 so frontend code uses relative URLs.
# Hot module reload works for the React app; the backend reloads on .py change.

set -e
cd "$(dirname "$0")/.."

# Kill any prior instance
pkill -9 -f "uvicorn.*backend" 2>/dev/null || true
pkill -9 -f "vite" 2>/dev/null || true
sleep 1

trap 'pkill -P $$; exit' INT TERM

echo "▶ FastAPI    → http://127.0.0.1:8000"
NUROQ_BACKGROUND_SERVICES=1 ./.venv/bin/uvicorn backend.api:app \
    --port 8000 --reload --log-level info &

sleep 2

echo "▶ Vite (HMR) → http://127.0.0.1:5173"
(cd frontend && npm run dev) &

wait
