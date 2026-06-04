# CLAUDE.md — NuroQ project instructions

> Read by Claude Code automatically at the start of every session in this repo.

## 📌 FIRST THING EVERY SESSION — load context

Before doing any work, **read these two files in order**:

1. **`session.md`** — the running, newest-on-top log of what each session did.
   This is the fast catch-up. Read the "Current session" + last 1–2 session blocks.
2. **`HANDOFF.md`** — the deeper architectural handoff: file map, gotchas,
   commands, open decisions, priority backlog.

If those two disagree with the code, the code wins — but flag the drift.

Deeper references when needed: `ARCHITECTURE.md` (system design),
`CAPABILITIES.md` (what it does), `FRONTEND.md` (React), `MOBILE.md` (iOS),
`PUBLISHING.md` (App Store), `SCHEDULING.md` (cron), `TRAINING_GUIDE.md` (DPO).

## 📝 KEEP THE LOG UPDATED — during and end of session

- **During the session:** whenever you take a *meaningful* action — ship a
  feature, fix a bug, create/delete a file, change config, restart a service,
  install a dependency — append a dated bullet under "Current session" in
  `session.md`. Not every tool call; meaningful checkpoints.
- **End of session, or when the user says "update handoff" / "update session":**
  promote the "Current session" bullets into a numbered, dated session block in
  `session.md`, AND update `HANDOFF.md`'s top section + date.
- Newest entries go on TOP in both files.

> Note: this is a best-effort convention, not an automatic mechanism. Claude
> writes these files when it remembers to at action boundaries. For truly
> automatic stamping, a Stop hook in `.claude/settings.json` can append a
> timestamp to `session.md` on every turn end (ask the user before adding hooks).

## What NuroQ is

Personal paper-trading research tool. Scans US equities, scores them with a
deterministic 100-pt quant rubric + a gated 10% Gemma-4B (MLX) AI tiebreaker,
trades via Alpaca paper API. Two frontends share one Python compute layer:
- **React + FastAPI + Capacitor iOS** (primary): `frontend/`, `backend/api.py`,
  served on :8000. Native iOS app via Capacitor.
- **Gradio dashboard** (legacy, still works): `dashboard.py`, port 7860.

## Architecture quick map

- Compute layer (unchanged across UIs): `scoring.py`, `live_agent.py`,
  `event_stream.py`, `news_engine.py`, `alpaca_executor.py`, `data_fetcher.py`,
  `llm_queue.py`, `research_cycle.py`, `premarket_refresh.py`.
- Backend: `backend/api.py` (FastAPI wrapping dashboard.py helpers as JSON).
- Frontend: `frontend/src/` (Vite + React 19 + TS + Tailwind + Capacitor 8).
- DB: `nuroq.db` (SQLite — watchlist_today, ai_scores_cache, news_cache,
  live_triggers, etc.).

## Conventions / gotchas

- **MLX = Apple Silicon only.** All Gemma inference goes through
  `analyst.analyze()` which holds a class-level lock (`EnsembleAnalyst._gemma_lock`)
  to prevent Metal command-buffer crashes from concurrent inference. Never call
  `generate(...)` directly — route through `analyst.analyze()`.
- **Telegram bot conflict:** importing `dashboard` starts background services
  (Telegram poller, news poller, etc.) unless `NUROQ_BACKGROUND_SERVICES=0`.
  Tests, cron scripts, and one-off imports MUST set that flag, or they steal the
  running backend's getUpdates slot (telegram.error.Conflict).
- **launchd + TCC:** project lives under `~/Documents` which macOS protects.
  The backend launchd job (`scripts/install-backend.sh`) invokes `uv run` (uv is
  in /opt/homebrew, outside the protected dir) — do NOT switch it to call
  `.venv/bin/uvicorn` directly (TCC blocks it).
- **Tests:** `./.venv/bin/python master_test_suite.py` (91 tests, ~4s). Keep green.
- **Build/run:** `./scripts/restart.sh` (launchd-aware), `./scripts/dev.sh`
  (HMR), `./scripts/ios.sh` (build + sync + open Xcode).
- **Paper trading only:** `paper=not NUROQ_LIVE_TRADING`. Live trading also
  requires `NUROQ_WASH_SALE_AWARE=1` (safety belt in `alpaca_executor._connect`).
- End git commit messages with the Co-Authored-By line; branch before committing
  on default branch; only commit/push when asked.

## Current branch

`feat/algo-claude-improvements` (substantial uncommitted work — see `HANDOFF.md`).
