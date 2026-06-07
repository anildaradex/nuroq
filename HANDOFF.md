# NuroQ — Session Handoff

> **For the next session.** Read this first, then `session.md` (Session 6) for the play-by-play.
>
> **Date:** 2026-06-06 (end of session 6)
> **Branch:** `main` (== `feat/algo-claude-improvements`; PR #7 merged). Default branch is `main`; work can continue on either, deploys trigger on push to `main`.
>
> ## 🚀 NuroQ is LIVE in the cloud
> - **URL:** **https://nuroq.nuroquant.com** (HTTPS via a named Cloudflare tunnel). Open `…/?api_key=<KEY>` once to set the auth cookie, then the bare URL works on any device.
> - **API key:** `gcloud secrets versions access latest --secret=NUROQ_API_KEY --project=nuroq-prod-anildara` (sent as `X-NuroQ-Key` header or `?api_key=`).
> - **Infra:** GCP project `nuroq-prod-anildara`; GCE **e2-medium** VM `nuroq-backend` @ static IP **34.9.20.141** (us-central1-a, **80GB** boot disk). Container = React UI + FastAPI + live agent + cloudflared, all in one image (`deploy/Dockerfile.cloud`, multi-stage, CPU-only torch). **Gemini via Vertex AI** (VM service account, no key). Secrets in Secret Manager; SQLite + tunnel log on host-path `/var/lib/nuroq` → `/data`. Scheduler (research 03:30 ET / proposals 08:00 ET) + live agent autostart. **Paper trading only.**
> - **Deploy:** auto on push to `main` (GitHub Actions + **Workload Identity Federation**, no stored key — `.github/workflows/deploy.yml`). Manual: `PROJECT_ID=nuroq-prod-anildara ./deploy/deploy_gce.sh` (idempotent; reads CLOUDFLARED_TOKEN from Secret Manager). Direct static-IP access (`http://34.9.20.141:8000`) is owner-IP-firewalled.
> - **Tests:** `./.venv/bin/python master_test_suite.py` → **110/110**.
>
> **Top open items:** confirm the merge-to-main auto-deploy went green · Today "Design lab" cleanup (4 layout variants → pick 1) · boot-disk snapshots for backup · iOS re-point to the new URL. Live trading still OFF.
>
> **Deploy gotchas already solved (don't re-hit):** named-tunnel route service must be **http**://localhost:8000 (not https → 502); CPU torch keeps the image ~1.5GB (CUDA torch filled the disk); `update-container` reassigns ephemeral IPs so a **static IP** is attached; the credential-helper token lacks `workflow` scope so **API merges strip `.github/workflows/`** — merge via `git` or verify after.

---

## Session 6b (2026-06-04) — cloud migration: code made GCP-ready

Decided **Compute Engine VM + Gemini**. Restructured NuroQ to run on Linux (full
detail in `CLOUD_MIGRATION.md` §Status and `deploy/README.md`):
- **MLX unblocked** — `pyproject.toml` markers (`sys_platform=='darwin'`) + lazy
  import in `dashboard.py`. `import dashboard`/`backend.api` verified with no MLX.
- **`analyst_backends.py`** — `GeminiBackend` (google-genai) behind the existing
  `analyst.analyze()` chokepoint; `NUROQ_AI_BACKEND` selects gemma|gemini.
- **`NUROQ_DB_PATH`** everywhere; **`X-NuroQ-Key`** auth middleware + `/health`.
- **`deploy/`** — `Dockerfile.cloud`, idempotent `deploy_gce.sh`, `README.md`,
  `.env.cloud.example`; hardened `.dockerignore`.
- Tests 104/104 green.

**🚀 LIVE ON GCP (2026-06-04).** Project `nuroq-prod-anildara`, e2-medium VM
`nuroq-backend` @ **static IP 34.9.20.141** (`nuroq-ip`, us-central1-a). Gemini via **Vertex** (VM
service account — no API key). Secrets in Secret Manager; SQLite on host-path
`/var/lib/nuroq`. Verified: `/health` 200, auth 401→200, `/api/propose-sells` 200.
- API key: `gcloud secrets versions access latest --secret=NUROQ_API_KEY --project=nuroq-prod-anildara` (sent as `X-NuroQ-Key`). Firewall :8000 → deployer IP only.
- Redeploy: `SKIP_BUILD=1 PROJECT_ID=nuroq-prod-anildara ./deploy/deploy_gce.sh` (rebuild: drop SKIP_BUILD). Idempotent.
- Deploy gotchas already fixed in the script: cloudbuild.yaml (custom Dockerfile path), e2-medium (e2-small OOMs torch), host-path mount (separate-disk fsck race), compute-SA roles (artifactregistry.reader + aiplatform.user), stable API key.
- ✅ Vertex inference smoke-tested (`/api/analyze/NVDA` → real Gemini analysis).
- ✅ Cron ported: `scheduler.py` in-process scheduler (`NUROQ_INPROC_SCHEDULER=1`) runs research @ 03:30 ET + sell-proposals @ 08:00 ET weekdays; `/health` reports it. Deploy uses unique image tags so `update-container` re-pulls (stable IP + /data).
- ✅ Gemini score parsing FIXED: scoring path uses Gemini structured output (JSON schema, 2048 tok); `structured` flag threaded through `analyst.analyze()` (Ask-AI stays free-text). Tests 110.
- ✅ CI/CD: `.github/workflows/deploy.yml` auto-deploys on push via Workload Identity Federation (no stored key). SA `gh-deployer`, WIF pool `github-pool`. See deploy/README.md.
- **NOT done:** confirm first Actions run is green; HTTPS front (Caddy/CF Tunnel) before live; frontend VITE_API_BASE + iOS re-point. Live trading still OFF.

---

## Session 6 (2026-06-03) — §475 mode + PDT-rule context

**North-star (stated by user):** make NuroQ a fully autonomous live-trading app —
autotrades without intervention, backtests, measures efficacy, learns from
mistakes, self-improves. Build toward this.

**Regulatory facts (web-verified this session):**
- **PDT rule abolished** — SEC approved FINRA's proposal 2026-04-14, effective
  ~2026-06-04. $25k minimum & "Pattern Day Trader" designation removed; replaced by
  risk-based intraday margin under Rule 4210 (bare margin min stays $2k). 18-month
  broker phase-in to ~Oct 20 2027 (per-broker enforcement, incl. Alpaca, may lag).
  NuroQ never modeled PDT, so there was nothing to remove — the quant layer is free
  to round-trip intraday.
- **§475(f) MTM election** removes wash-sale (§1091) + the $3k cap-loss limit;
  gains/losses become ordinary on Form 4797. It is an election *statement* + Form
  3115, NOT a "Form 475." **For existing individuals the 2026 election window
  closed Apr 15 2026.** Still available via a new entity (~75-day Rev. Proc. 99-17
  window) or for 2027 (by Apr 15 2027). So the flag below is built but should
  remain OFF for 2026 individual trading.
- **Crucial:** NuroQ's wash-sale code is *advisory only*. Disabling it does not
  change real tax treatment (governed by the filed election + 1099-B), and the app
  is still paper-only — no live taxable trades today.

**What shipped — `NUROQ_SECTION_475` (default OFF):**
- `dashboard.section_475_active()` (dashboard.py ~1703) — reads env live, no restart.
- `dashboard.wash_sale_check()` short-circuits at the top when on → `risk=False`,
  empty sell lists, explanatory hint, `section_475: True`, returned BEFORE any
  Alpaca call / cache. One chokepoint ⇒ live_agent `_handle_buy_crossing`,
  `handle_quick_trade`, and the iOS OrderReviewModal all pass through (they read
  `risk`). Not cached, so toggling is instant.
- `alpaca_executor._connect` belt (alpaca_executor.py ~51) now passes if
  `NUROQ_SECTION_475=1` OR `NUROQ_WASH_SALE_AWARE=1` when `NUROQ_LIVE_TRADING=1`.
- Tests: `TestSection475` in master_test_suite.py (helper default-off; short-circuit
  doesn't hit Alpaca; default path still flags loss re-entry; §475 satisfies belt;
  belt still blocks with neither ack). **96/96 green, ~4.2s.**

**Option B — SHIPPED this session.** The core quant layer now proactively proposes
SELLs on held positions, complementing the live agent's reactive SELL crossing
(score ≤ 30, live_agent.py:593):
- `dashboard.propose_sells()` (pure, fails-closed) — joins `alpaca_api.list_positions()`
  with `watchlist_today` scores. Three proposal kinds:
  - **TAX_LOSS_HARVEST** — §475-GATED (only when `section_475_active()`; else a
    wash-sale trap, suppressed). Material loss (≥`HARVEST_MIN_LOSS_PCT`=0.02) on a
    non-strong holding (score < `HARVEST_SCORE_CEILING`=55, or off today's list).
  - **ROTATE** — weak holding (score ≤ `SELL_PROPOSE_WEAK_SCORE`=45) + a non-held
    BUY out-scoring it by ≥`ROTATE_SCORE_EDGE`=20 → `rotate_into` that name.
  - **EXIT_WEAK** — weak holding, no stronger candidate.
  Ranked harvest→rotate→exit, worst-score/biggest-loss first.
- `dashboard.log_sell_proposals()` — PROPOSE_SELL rows in live_triggers (dedup per
  date/ticker/kind) → auto-surface in the React Recent Activity feed.
- Surfaces: `render_next_actions` card (top 4), `GET /api/propose-sells`
  (backend/api.py — returns proposals + `section_475`), and `premarket_refresh.main`
  (logs them each morning). All ADVISORY — nothing auto-executes.
- Tests: `TestSellProposals` (8 cases) in master_test_suite.py. 104/104 green.

**Still open:** React UI card for sell proposals (feed shows the rows already, but
a card with SELL buttons closes the loop); Option C (Investor vs Trader-§475 regime
in the UI); threshold tuning against real watchlist behavior (first live run flagged
several ROTATEs — confirm the score-decay band feels right). And **restart the
backend** so :8000 serves the new endpoint.

---

## Session 5 (2026-05-31) — what shipped

**⚠️ NOTE ON LOGGING:** There is NO automatic session logging. This HANDOFF.md
is updated by hand at end of session. If a session ends without updating it,
that work is undocumented. (This section was written because the user asked
"are you logging to session.md" — answer was no, so we caught up here.)

**Ask AI (free-form Q&A) — the headline feature:**
- `dashboard.ask_about_ticker(ticker, question)` — grounds Gemma with price-action
  summary (month-by-month from cached bars), fundamentals, news (cache-first via
  direct `news_cache` SQL read, bounded 8s live fallback), and SEC-filing RAG
  (bounded 10s via ThreadPoolExecutor timeout). Runs through GPU-locked
  `analyst.analyze()`. Reuses `detect_reasoning_drift` to flag off-topic answers.
- `POST /api/ask` endpoint (backend/api.py) + `api.ask()` client method.
- `AskBar` component in `AnalyzeView.tsx` — sits UNDER the chart. Search input +
  3 suggested-prompt chips + Q&A thread (newest first) with source attributions.
- Verified: NVDA "why did it move" → 7.6s, grounded answer w/ 4 sources.
  RKLB "why did RKLB increase in May?" → correct 78.7% May move + Q1 results.
- Perf note: cache-first design means watchlist tickers answer in ~8s; cold
  tickers (not yet analyzed) hit bounded live fetches. UX flow loads the ticker
  into cache via "Run Analysis" before the AskBar appears, so happy path is fast.

**Wash-sale Layer 1 (IRS §1091 guard):**
- `dashboard.wash_sale_check(ticker)` — queries `alpaca_api.get_recent_fills()`
  (new method in alpaca_executor.py), pairs each SELL with nearest prior BUY as
  proxy basis, flags likely-loss sells in 30-day window. 5-min memoized. Fails OPEN.
- Gated on ALL BUY entry points: `handle_quick_trade` (param `wash_sale_override`),
  `live_agent._handle_buy_crossing` (suppresses + fires informational Telegram),
  Telegram REFEX callback (shows "⚠️ EXECUTE ANYWAY" override button → REFEXOK).
- `POST /api/trade` accepts `wash_sale_override`; `GET /api/wash-sale/{ticker}`.
- OrderReviewModal shows red banner; first Submit tap = acknowledge, second = execute.
- Safety belt in `alpaca_executor._connect`: refuses live trading
  (`NUROQ_LIVE_TRADING=1`) unless `NUROQ_WASH_SALE_AWARE=1` also set.

**Trading UX:**
- **Limit-bracket orders**: `submit_bracket_order(..., limit_price=None)` — Market
  OR Limit entry with same SL/TP. Validates limit sandwiched between SL/TP.
  `handle_quick_trade` routes both. OrderReviewModal defaults to Limit (pre-filled
  at current price), SL/TP visible for BOTH order types (fixed the hide-on-Limit bug).
- **OrderReviewModal** (`components/OrderReviewModal.tsx`): spacious centered modal
  (desktop) / bottom sheet (mobile), opened by Watchlist ⚡. Editable shares/SL/TP,
  cost/risk/reward/R:R summary, big Cancel + Submit. Replaces cramped bottom-strip.
- **Double-submit prevention**: triple-layer — React 1.5s debounce + idempotency
  UUID (backend 10s TTL replay cache in /api/trade) + isPending button disable.
  (Fixed the bug where a double-tap bought SAN twice → 1000 shares.)
- **Watchlist ⚡ Trade button**: moved to 2nd column (was off-screen-right on
  mobile). `/api/trade-setup/{ticker}` returns ATR-sized order. Refresh button
  now uses `isFetching` (spins on every refetch, not just first load).

**Telegram:**
- **Refresh-BUY alerts** at session open: one message per BUY watchlist ticker
  (capped REFRESH_BUY_MAX_TICKERS=12) with sizing + ✅ EXECUTE / ⏭️ Dismiss buttons.
  EXECUTE submits MARKET bracket via REFEX callback. Idempotent per-ticker per-day.
- **Session-open digest** (table of all BUYs) still fires once/day.
- **Score-shift detector**: REMOVED from Telegram (too noisy), kept as LOG-ONLY →
  writes SCORE_SHIFT_UP/DOWN rows to live_triggers which surface in the in-app
  Recent Activity feed. Constant `SCORE_SHIFT_DELTA=10` in live_agent.py.

**Infra / cron:**
- Premarket cron moved 08:15 → **08:00 CT**. Research cron moved 01:00 → **03:30 CT**
  (Polygon publishes daily snapshot ~03:00 ET; earlier raced empty data).
- **Memorial Day bug fix**: `run_research_cycle` now rolls the target date back
  weekday-by-weekday (up to 10) when Polygon returns empty (holiday), instead of
  aborting. pmset wake moved to 03:25 CT.
- Premarket news scope trimmed to actionable subset (BUYs + held, cap 30) — was
  fetching all 150 serially (~83 min, overlapping market open). Now ~5-7 min.
- **launchd backend autostart** (`scripts/install-backend.sh`): runs `uv run
  uvicorn` (uv lives in /opt/homebrew, outside ~/Documents, so TCC doesn't block
  it — the .venv binaries DID get blocked). KeepAlive=Crashed, RunAtLoad.
  `com.nuroq.backend` LaunchAgent now manages the backend.
- `scripts/restart.sh` is launchd-aware: uses `launchctl kickstart` if the job
  exists, else foreground manual mode.

**Bug fixed mid-session:** live agent was silently broken — `_on_bar` referenced
score-shift fields/methods that had been removed earlier, throwing AttributeError
on every bar (caught, but bars weren't scored). Restored TickerState fields
(baseline_score, shift_fired_up/down) + `_check_score_shift` (log-only).

**Known tooling note:** the shell/grep display was unreliable for stretches of
this session (garbled output, wc returning 0). Verification was done by trusting
the compiler (fresh dist mtime = tsc passed), test suite (91/91), and actual
endpoint responses (curl → JSON files read via Read tool), not shell echo.

---

## Session 4 (2026-05-25)

> **Date:** 2026-05-25 (late evening, session 4)
> **Latest shipped:** Today design lab with 4 layout variants behind a switcher (`TodayA-D.tsx`). launchd cron schedules installed (research at 01:00 CT, premarket at 08:15 CT). FastAPI auto-starts the live agent on boot via `NUROQ_AUTOSTART_AGENT=1`.

---

## Session 4 (2026-05-25 late) — automation + design lab

**Automation (now hands-off in the morning):**
- `scripts/install-schedules.sh` — installs two launchd plists to `~/Library/LaunchAgents/`
  - `com.nuroq.research-cycle` — runs `research_cycle.py --top-n 150` daily at **01:00 CT** weekdays (= 02:00 ET, ~7h before market open)
  - `com.nuroq.premarket-refresh` — runs `premarket_refresh.py` at **08:15 CT** weekdays (= 09:15 ET, 15 min before open)
  - Both set `NUROQ_BACKGROUND_SERVICES=0` so the cron scripts don't conflict with the running backend's Telegram poller
  - Logs to `research_cycle.{out,err}.log` and `premarket_refresh.{out,err}.log` in repo root
  - **One manual step remaining:** `sudo pmset repeat wakeorpoweron MTWRF 00:55:00` (wakes Mac from sleep before the 01:00 cron) — OR set "Prevent automatic sleeping" in System Settings → Battery (more reliable for a desk Mac)
- `scripts/install-backend.sh` — installs `com.nuroq.backend` LaunchAgent so the FastAPI backend auto-starts at login + auto-restarts on crash
  - ⚠️ **Blocked by macOS TCC** because the project lives under `~/Documents/`. launchd can't execute binaries in that path without explicit Full Disk Access. **Fix:** System Settings → Privacy & Security → Full Disk Access → add `/bin/sh` AND the venv's uvicorn binary (`.venv/bin/uvicorn`). Then `launchctl kickstart -k gui/$(id -u)/com.nuroq.backend`. The install script detects this error and prints the exact instructions.
  - Alternative: move project to `~/Code/` or another non-protected location.
  - Until FDA is granted, run `./scripts/restart.sh` manually after reboots.
- **Auto-start the agent on backend boot** via FastAPI `@app.on_event("startup")`:
  - Reads `NUROQ_AUTOSTART_AGENT` env var (default `1`)
  - Idempotent (skips if already running)
  - Catches errors so a failed start doesn't crash the backend
  - **Verified live:** backend boot → "🟢 LiveAgent started — subscribed to 152 tickers (3 held)" appears in logs immediately

**Today redesign (in progress — user reviewing):**
- `views/TodayView.tsx` is now a shell with a 4-button variant switcher banner at top
- `views/today/TodayA.tsx` — "Hero" (Robinhood / Schwab): big 5-6xl P&L with 30d equity sparkline (new `/api/alpaca/history` endpoint), 4 action tiles, compact positions, watching+pending side-by-side, recent activity
- `views/today/TodayB.tsx` — "Terminal" (Bloomberg): dense single-row status bar + 2×2 panel grid (Positions / Watching / Agent Decisions / News Shocks), mono everywhere, color-coded
- `views/today/TodayC.tsx` — "Calm" (Linear / Notion): hero Next Action card up top (auto-selects "Start Agent" CTA if compelling), quieter P&L, 3-up stat strip, breathing room, max-w-3xl
- `views/today/TodayD.tsx` — "Stream" (Trade Ideas): live feed dominates left 2/3, sidebar right 1/3 with P&L + agent control + position counts + Quick Trade shortcut
- User's pick persists in localStorage as `nuroq.todayVariant`
- **TBD next session:** user picks one (or hybrid), I delete the other 3 + the design-lab banner, commit clean

**New scripts:**
- `scripts/restart.sh` — one-command restart with LAN-IP drift detection + auto-rebuild + auto-sync iOS
- `scripts/install-schedules.sh` — install/reinstall launchd cron jobs

**Latest session shipped (session 3, earlier 2026-05-25):** All 8 React views wired (Analyze, Watchlist, Portfolio, Scanner, Agent, Signals, System, Logs) with backend endpoints. Quick Trade now actually trades via `/api/trade`. Command palette analyzes tickers. Plus `PUBLISHING.md` covering TestFlight + App Store.

---

## Latest session (2026-05-25) — what shipped

**8 new FastAPI endpoints in `backend/api.py`:**
- `POST /api/trade` — wraps `dashboard.handle_quick_trade`
- `GET /api/analyze/{ticker}` — full analysis: technicals + fundamentals + Gemma reasoning + trade setup + 120 bars of OHLC + SMA20 + Bollinger bands
- `POST /api/portfolio/remove`
- `GET /api/signals` — signal history
- `GET /api/agent/log` — recent live triggers
- `GET /api/logs?lines=N` — tail of `activity.log`
- `GET /api/system/health` — parses `render_health_snapshot()` into structured components

**8 React views fully wired (no more placeholders):**
- `AnalyzeView.tsx` — ticker input + recent-symbols chips + 60/40 chart-first layout (Recharts ComposedChart with SMA20 + BB), tabbed right rail (Signal / AI Reasoning / Trade Setup)
- `WatchlistView.tsx` — BUY + HOLD tables with row-click drill-down, collapsible HOLDs section
- `PortfolioView.tsx` — positions table with row-click drill-down + remove button with confirm
- `ScannerView.tsx` — Top-20 / Global Deep Scan toggle + run button + results table
- `AgentView.tsx` — Start/Stop buttons with response banner + 4-up status grid + recent triggers log
- `SignalsView.tsx` — filter pills (All / BUY / HOLD / SELL) + table
- `SystemView.tsx` — pill-coded health rows
- `LogsView.tsx` — terminal-style activity log tail with INFO/WARNING/ERROR coloring

**Cross-cutting:**
- App.tsx now lifts `analyzeTicker` and `qtPrefill` state so all views can drill into Analyze and Analyze's Trade Setup can prefill Quick Trade
- Quick Trade BUY/SELL actually call `/api/trade` with full bracket support + status banner + spinners
- Command palette detects bare tickers (`/^[A-Z]{1,6}$/`) and offers an "Analyze TICKER" action

**New doc:** `PUBLISHING.md` — TestFlight Internal (recommended), TestFlight External, and full App Store paths with NuroQ-specific gotchas.

**Bundle:** ~207 KB gzipped JS (Recharts is heavy). Build clean, 91/91 tests pass.

---

## Original migration session (2026-05-24)

**Big shift:** migrated from a single Gradio dashboard to a **React + FastAPI + Capacitor iOS** stack while keeping the Gradio dashboard alive in parallel for fallback.

---

## TL;DR — Three things to know

1. **There are now TWO frontends** that share the same Python backend logic:
   - **Gradio** at `dashboard.py` (legacy, still works, ran on port 7860). Got a big density+navigation overhaul this session before the React migration started.
   - **React + Vite + Tailwind** at `frontend/` (new, primary). Served by FastAPI at `backend/api.py`. Default port 8000.
2. **A native iOS app is wired up** via Capacitor 8. Open `frontend/ios/App/App.xcodeproj` in Xcode, Cmd+R to your phone. App icon + splash already use `nuroq_logo.png`.
3. **All 91 Python tests pass.** Python compute layer (scoring, agent, Alpaca, MLX) unchanged.

---

## Repo state

| Path | What it is | Status |
|---|---|---|
| `dashboard.py` | Legacy Gradio dashboard (~2700 lines) | ✅ works, fully refactored this session |
| `backend/api.py` | NEW — FastAPI wrapping `dashboard.py` helpers as JSON | ✅ 11 endpoints, all tested |
| `frontend/` | NEW — Vite + React 18 + TS + Tailwind + lucide + TanStack Query + cmdk | ✅ builds clean |
| `frontend/ios/` | NEW — Capacitor 8 Xcode project (Swift Package Manager) | ✅ generated, plugins registered |
| `scripts/dev.sh` | NEW — Vite + FastAPI dual-port with proxy + HMR | ✅ |
| `scripts/prod.sh` | NEW — single-port build + serve | ✅ |
| `scripts/ios.sh` | NEW — auto-detect LAN IP, build, sync, open Xcode | ✅ |
| `live_agent.py`, `scoring.py`, etc. | unchanged from previous session | ✅ |

**Uncommitted on `feature/trading-correctness-and-scoring`:** very substantial — entire React frontend, FastAPI backend, Capacitor iOS project, Gradio UI redesign, several `.md` doc updates. Commit when ready.

---

## What got built this session (chronological)

### 1. Bug fix — test suite was wiping production watchlist

**Symptom:** `watchlist_today` table kept getting emptied even after the research cycle wrote 150 rows.

**Cause:** `master_test_suite.py:TestWatchlistUIHelpers` did `DELETE FROM watchlist_today` against the **module-level singleton**, which is bound to the real `nuroq.db`.

**Fix:** Added `setUpClass` / `tearDownClass` that redirects `watchlist_today.db_path` to a tempfile and restores it on teardown. Also added `os.environ.setdefault("NUROQ_BACKGROUND_SERVICES", "0")` at the top of the test suite so `import dashboard` doesn't spin up Telegram bot pollers.

### 2. Bug fix — agent start UI silently dropped error messages

**Symptom:** Click START AGENT → nothing happens, status shows "Stopped / n/a", no explanation.

**Cause:** `st_a_btn.click(agent.start)` had no `outputs=` slot. The descriptive string from `agent.start()` (e.g. "🛑 Market is closed") was discarded.

**Fix:** Added `ag_msg = gr.Markdown(...)` banner under the buttons; routed `agent.start()` / `agent.stop()` return strings into it.

### 3. Removed market-hours gate for dev

[`dashboard.py:AgentLoop.start`] now hardcodes `force = True` so the agent can start any time (weekends/after-hours) during development. **Re-enable** the gate by reverting that one line to `force = os.getenv("NUROQ_FORCE_LIVE", "0") == "1"` once the system is finalized.

### 4. Duplicate-dashboard detection

[`dashboard.py`] added `_detect_duplicate_dashboard()` that uses `pgrep` to find other `python dashboard.py` processes. If found, skips Telegram bot startup with a clear warning. Prevents the `telegram.error.Conflict` getUpdates conflict that broke our session earlier.

### 5. Gradio UI redesign (Phases A → D)

Big density + navigation overhaul before the React migration. All in `dashboard.py`:

- **Today landing tab** (default): hero P&L, 3 summary cards (Watchlist / Agent / News), Smart "Next Actions" card, Live Feed channel bar
- **Persistent status pills** (Agent / Stream / Telegram / Alpaca) auto-refreshing every 60s via `gr.Timer`
- **Drill-down** from Watchlist BUY/HOLD, Portfolio, Signal History, Scanner, Agent Log → Analyze tab with ticker preloaded
- **Recent symbols dropdown** next to Analyze input
- **Send to Quick Trade** button on Trade Setup card
- **Chart-first Analyze** (60/40 split, right-rail tabs)
- **Merged Scanner** (Top 20 / Global Deep Scan via Radio)
- **Renames**: Health→System, Activity Log→Logs, Signals→Signal History
- **Quick Trade open by default**, **Watchlist HOLDs open by default**
- **Research Cycle consent gate** (checkbox + auto-uncheck)
- **⌘K / Ctrl+K command palette** (prompt()-based — real modal in React)
- **CSS density pass**: max 1700px, 13px base font, tighter rows, sticky tab strip, alternating row stripes, compact Alpaca panel
- **Tab persistence** via localStorage
- **Fixed Gradio 6 deprecation warning** (moved theme/js/css from `Blocks()` to `launch()`)

### 6. React + FastAPI migration (the big one)

**Backend** (`backend/api.py`):
- FastAPI app, CORS configured for Vite + Capacitor origins via regex
- 11 endpoints wrapping existing `dashboard.py` helpers as Pydantic-typed JSON
- Mounts `frontend/dist/` at `/` so the SPA + API share one port in production

**Frontend** (`frontend/`):
- Vite + React 19 + TypeScript + Tailwind 3 + lucide-react + @tanstack/react-query + cmdk
- Inter + JetBrains Mono from CDN, dark mode default
- `src/lib/api.ts` — typed fetch client matching backend
- `src/lib/cn.ts` — className + format helpers
- `src/components/TopBar.tsx` — brand + ⌘K trigger + status pills + theme toggle
- `src/components/LeftRailNav.tsx` — 9-item rail (desktop only, ≥ lg)
- `src/components/MobileNav.tsx` — 5-tab iOS bottom bar with "More" sheet (< lg)
- `src/components/StatusPills.tsx` — 4 live pills (dots-only on mobile)
- `src/components/CommandPalette.tsx` — real cmdk modal with Navigate + Actions groups
- `src/components/QuickTrade.tsx` — pinned strip desktop, FAB + sheet on mobile
- `src/views/TodayView.tsx` — full fidelity (hero, cards, next actions, live feed, pending orders)
- `src/views/PlaceholderView.tsx` — placeholder for 8 not-yet-migrated views

**Bundle size:** ~99 KB gzipped JS, ~5 KB gzipped CSS.

### 7. iOS / mobile responsive design

- `viewport-fit=cover` + `env(safe-area-inset-*)` for notch + home indicator
- `apple-mobile-web-app-capable` + `black-translucent` status bar
- `manifest.json` with icons (192/512/apple-touch-icon) all generated from `nuroq_logo.png`
- Bottom MobileNav (iOS-native pattern), 56px tall, respects home indicator
- Responsive TopBar (dots-only pills, icon-only search on mobile)
- Responsive QuickTrade (FAB + slide-up sheet on mobile)
- 44pt minimum touch targets on `pointer: coarse` devices
- `-webkit-tap-highlight-color: transparent`, `touch-action: manipulation`

### 8. Capacitor 8 native iOS app

- `capacitor.config.ts` — appId `com.nuroq.app`, two-mode (production bundled / dev hot-reload via `NUROQ_DEV_SERVER`)
- `frontend/ios/` — full Xcode project generated via `npx cap add ios`
- 6 Capacitor plugins installed: `@capacitor/{app,haptics,preferences,push-notifications,splash-screen,status-bar}` (Swift Package Manager, not CocoaPods)
- `src/lib/native.ts` — bridge with `haptic.{tap,medium,heavy,success}`, `syncStatusBar`, `hideSplashWhenReady`, `onAppResume`
- App icon + splash regenerated from `nuroq_logo.png` (1024×1024 + 2732×2732 padded)
- CORS regex on backend includes `capacitor://localhost`
- `VITE_API_BASE` env var pattern — `scripts/ios.sh` auto-detects Mac's LAN IP and bakes it into the iOS build

---

## Currently running

When the previous session ended:
- **FastAPI backend** on `0.0.0.0:8000` (was running; user killed it with the task-stop in the last message)
- **No Gradio dashboard** running

To bring everything back up:
```bash
cd "/Users/anildara/Documents/Agent Driven Development/stock-ai-startup"

# Option 1 — React + FastAPI dev mode (HMR)
./scripts/dev.sh
# Vite on 5173, FastAPI on 8000 with --reload, Vite proxies /api → 8000

# Option 2 — React production single-port
./scripts/prod.sh
# Builds bundle, FastAPI serves it + API on 8000

# Option 3 — iOS native rebuild + push to phone
./scripts/ios.sh
# Auto-detects Mac's LAN IP, builds React, syncs into Xcode project, opens Xcode

# Option 4 — Legacy Gradio (still works)
NUROQ_FORCE_LIVE=1 ./.venv/bin/python dashboard.py
# Port 7860
```

---

## Critical files to know

```
stock-ai-startup/
├── HANDOFF.md                  ← you are here
├── README.md                   ← updated this session
├── CAPABILITIES.md             ← updated
├── ARCHITECTURE.md             ← updated
├── FRONTEND.md                 ← new — React dev guide
├── MOBILE.md                   ← new — iOS / Capacitor guide
├── SCHEDULING.md               ← unchanged
├── TRAINING_GUIDE.md           ← unchanged
│
├── backend/
│   ├── __init__.py
│   └── api.py                  ← FastAPI app (11 endpoints + CORS + SPA mount)
│
├── frontend/
│   ├── capacitor.config.ts     ← appId, two-mode dev/prod switch
│   ├── package.json            ← React 19, Tailwind 3, cmdk, lucide, TanStack Query, Capacitor 8
│   ├── vite.config.ts          ← dev proxy /api → :8000
│   ├── tailwind.config.js      ← custom buy/sell/accent palette
│   ├── index.html              ← iOS PWA meta tags, Inter + JetBrains Mono CDN
│   ├── public/
│   │   ├── manifest.json
│   │   ├── apple-touch-icon.png, icon-192.png, icon-512.png, favicon.png
│   ├── dist/                   ← built bundle (FastAPI serves from here)
│   ├── ios/                    ← Capacitor-generated Xcode project
│   │   └── App/App.xcodeproj   ← open this in Xcode
│   └── src/
│       ├── App.tsx             ← layout shell + view router
│       ├── main.tsx            ← QueryClient mount
│       ├── index.css           ← Tailwind + design tokens + safe-area + .nuroq-live-dot
│       ├── lib/
│       │   ├── api.ts          ← typed fetch client, VITE_API_BASE for Capacitor
│       │   ├── cn.ts           ← className + fmtUSD/fmtPct/relTime/shortTime
│       │   └── native.ts       ← Capacitor bridge (haptics, status bar, splash, lifecycle)
│       ├── components/
│       │   ├── TopBar.tsx
│       │   ├── LeftRailNav.tsx
│       │   ├── MobileNav.tsx
│       │   ├── StatusPills.tsx
│       │   ├── CommandPalette.tsx
│       │   └── QuickTrade.tsx
│       └── views/
│           ├── TodayView.tsx       ← full fidelity
│           └── PlaceholderView.tsx ← 8 other views are placeholders
│
├── dashboard.py                ← legacy Gradio, fully refactored this session
├── live_agent.py               ← unchanged (force=True hardcoded for dev)
├── scoring.py                  ← unchanged
├── data_fetcher.py             ← unchanged
├── master_test_suite.py        ← 91 tests, test isolation fix added this session
├── nuroq.db                    ← SQLite (watchlist_today, ai_scores_cache, etc.)
├── nuroq_logo.png              ← source for all app icons
│
└── scripts/
    ├── dev.sh                  ← FastAPI + Vite HMR side-by-side
    ├── prod.sh                 ← single-port (FastAPI serves built SPA)
    └── ios.sh                  ← auto-LAN-IP, build, sync, open Xcode
```

---

## Known gotchas / open decisions

1. **`force = True` hardcoded** in `dashboard.py:AgentLoop.start` (the AgentLoop wrapper around live_agent). Re-enable market-hours gate by reverting to `force = os.getenv("NUROQ_FORCE_LIVE", "0") == "1"` when system is production-ready.

2. ~~8 of 9 React views are placeholders.~~ **All views shipped** as of session 3. Code in `frontend/src/views/`:
   - `TodayView.tsx`, `AnalyzeView.tsx`, `WatchlistView.tsx`, `PortfolioView.tsx`, `ScannerView.tsx`, `AgentView.tsx`, `SignalsView.tsx`, `SystemView.tsx`, `LogsView.tsx`

3. ~~Quick Trade BUY/SELL buttons currently stub.~~ **Now actually trades** via `POST /api/trade` (session 3). Supports market + limit + stop + bracket orders. Status banner shows result.

4. **No WebSocket yet.** All live updates are TanStack Query polling at 15-60s intervals. WebSocket push would replace polling for sub-second feel; needs `backend/ws.py` + `src/lib/ws.ts`. See Session +3 in the priority list.

5. **Backend bound to `0.0.0.0:8000`** for iPhone access during dev. **Security note:** anyone on the LAN can reach it. Fine on a home network; restart with default `127.0.0.1` binding when not actively testing on phone.

6. **macOS firewall is on** — first connection from iPhone to Mac will pop a "allow incoming connections" dialog. User must click Allow.

7. **iOS app needs Apple ID for code signing.** Free, no $99 needed for personal sideload. Walked through in `MOBILE.md`.

8. **iOS app bundle ID** is `com.nuroq.app`. If Xcode complains it's taken, change to `com.<yourname>.nuroq` in Signing & Capabilities.

9. **Telegram still active in backend.** Background services start on `import dashboard` unless `NUROQ_BACKGROUND_SERVICES=0`. `scripts/dev.sh` and `scripts/prod.sh` keep them on (so Telegram approvals + news poller work). Test suite and one-off Python imports must set the flag to 0 to avoid Telegram bot conflicts.

10. **CORS regex** in `backend/api.py` accepts any `http://192.168.*.*:*` / `http://10.*.*.*:*` origin. If on a different LAN scheme (172.x), add it.

11. **Watchlist last had 150 rows** from 13:53 ET research cycle. May be stale by next session; re-run `research_cycle.py` or click 🔬 Run Research Cycle to refresh.

12. **Tasks killed by user during session** (not bugs, just stopped): the LAN-bound backend and the CORS-updated backend. Restart with the commands above.

---

## Practical next steps (priority order)

**Sessions 1-3 already shipped:** all 8 React views, all backend endpoints, working Quick Trade. The app is fully usable end-to-end on web + iOS.

### Session +1 (recommended: ship to TestFlight)
1. **Read `PUBLISHING.md`** — covers TestFlight Internal (~30 min) and full App Store paths
2. **Bump version + build in Xcode** → General tab → `1.0.0`, build `1`
3. **Product → Archive** in Xcode
4. **Organizer → Distribute → App Store Connect → Upload**
5. **Create the app entry** at https://appstoreconnect.apple.com → Internal Testing → invite yourself
6. **Install TestFlight app on iPhone** → accept invite → install NuroQ
7. Now you have NuroQ on your phone as a real distributed app (no more USB cable + Xcode required for re-installs)

### Session +2 (native polish that makes it feel like Schwab)
8. **Pull-to-refresh** native gesture on Today/Watchlist/Portfolio
9. **Swipe actions** on Watchlist rows (swipe → send to QT, swipe → dismiss)
10. **Face ID gate** on orders above $X (`@capacitor-community/biometric-auth`)
11. **Apple Push Notifications** — get APNs key from developer.apple.com, backend POSTs to `https://api.push.apple.com` on agent BUY crossings + news shocks. Replaces or augments Telegram.

### Session +3 (real-time push)
12. **WebSocket `/ws`** — replace TanStack polling with real-time event stream from agent + news poller
13. **Live chart** — stream minute bars into the AnalyzeView chart

### Session +4 (publish to public App Store — bigger lift)
14. **Move backend off your Mac** — cloud Mac mini (MacStadium ~$60/mo) or strip MLX and host on Linux
15. **Per-user API key settings** — settings screen + `@capacitor/preferences` keychain storage
16. **Privacy policy** + screenshots + Apple review submission

### Stretch
17. **Home screen widget** showing today's P&L (small Swift snippet + shared App Group)
18. **Apple Watch companion** — true native, separate target in Xcode
19. **Code-split the React bundle** — Recharts is 200 KB; dynamic-import the AnalyzeView so the initial load is faster

---

## Smoke-test commands

```bash
cd "/Users/anildara/Documents/Agent Driven Development/stock-ai-startup"

# Python tests (should be 91/91 OK in ~4s)
./.venv/bin/python master_test_suite.py

# FastAPI quick check (after starting backend on :8000)
curl -sS http://127.0.0.1:8000/api/today/cards
curl -sS http://127.0.0.1:8000/api/status/pills
curl -sS http://127.0.0.1:8000/api/today/feed | head -c 400

# React build sanity
cd frontend && npm run build

# Capacitor sync sanity
cd frontend && npx cap sync ios

# Watchlist freshness
sqlite3 nuroq.db "SELECT COUNT(*), datetime(MAX(generated_at),'unixepoch','localtime') FROM watchlist_today;"
```

---

## Honest assessment

**What works well today:**
- Solo dev iteration on Mac at http://127.0.0.1:8000 in Chrome
- React + iOS sharing one codebase + one backend
- Mobile responsive design is genuinely usable on phone Safari
- Capacitor native app installs and runs

**What's not yet good:**
- Most React views are placeholders — can't actually trade or analyze from the React app yet
- No real-time push — feed polls every 30-60s
- Quick Trade BUY/SELL doesn't actually trade
- No push notifications — agent crossings still go via Telegram only
- iOS app still requires the Mac to be running (backend lives there)

**Architecture is sound for productization later if you want:**
- Backend can be deployed to a cloud VM (anything that runs Python + MLX, so Apple Silicon Mac or remote Mac mini)
- Frontend is just static files — anyone can host on Vercel/Cloudflare/S3
- iOS app submits to App Store
- Multiple users could be supported with proper auth (currently single-tenant)
