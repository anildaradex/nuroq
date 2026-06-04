# NuroQ — Session Log

> **Running, append-only log of what each working session did.** Newest entry on
> top. This is the FAST context file — read it first on every new session, then
> `HANDOFF.md` for the deeper architectural picture.
>
> **Maintenance convention** (also recorded in `CLAUDE.md`):
> - At the START of a session: read this file + `HANDOFF.md`.
> - DURING a session: append a dated bullet under "Current session" each time a
>   meaningful action is taken (feature shipped, bug fixed, file created,
>   config changed, service restarted). Not every tool call — meaningful steps.
> - At the END / when asked to "update handoff": promote "Current session" notes
>   into a numbered, dated session block here AND update `HANDOFF.md`.
>
> Raw, complete transcripts are also auto-saved by Claude Code under
> `~/.claude/projects/.../*.jsonl` — but those are uncurated. This file is the
> curated trail.

---

## Session 5 — 2026-05-31 → 2026-06-01 (COMPLETE)

**AI & analysis**
- Built **Ask AI** free-form Q&A under the Analyze chart (`AskBar` in
  `AnalyzeView.tsx`, `POST /api/ask`, `dashboard.ask_about_ticker`). Grounds
  Gemma with price action (month-by-month) + cached news (cache-first, 8s live
  fallback) + SEC RAG (10s bounded) + fundamentals. Reuses drift detector.
  Verified live (NVDA 7.6s, RKLB "why did it increase in May" → correct 78.7%).

**Trading correctness & UX**
- **Wash-sale Layer-1 guard** (IRS §1091) on ALL BUY paths: `wash_sale_check()`
  via `alpaca_api.get_recent_fills()`, 5-min memoized, fails-open. Gated in
  `handle_quick_trade`, `live_agent._handle_buy_crossing` (suppress + info
  Telegram), Telegram REFEX (→ "EXECUTE ANYWAY" REFEXOK override). `/api/wash-sale`
  + OrderReviewModal red-banner two-tap override. Safety belt in
  `alpaca_executor._connect` (live trading needs NUROQ_WASH_SALE_AWARE=1).
- **Limit-bracket orders** (`submit_bracket_order(limit_price=...)`); modal
  defaults to Limit, SL/TP visible for both types (fixed hide-on-Limit bug).
- **OrderReviewModal** — spacious review surface (Watchlist ⚡), editable
  shares/SL/TP, cost/risk/reward/R:R, big Cancel+Submit.
- **Double-submit prevention** — React 1.5s debounce + idempotency UUID
  (backend 10s replay cache) + isPending. (Fixed the SAN-bought-twice bug.)
- **Watchlist ⚡ Trade button** moved to 2nd column (was off-screen mobile);
  refresh uses `isFetching` so spinner shows every refetch.

**Telegram**
- **Refresh-BUY alerts** at session open: 1 msg/BUY ticker w/ sizing +
  ✅ EXECUTE / ⏭️ Dismiss buttons. Idempotent per-ticker/day. Cap 12.
- **Session-open digest** table once/day.
- **Score-shift detector** → LOG-ONLY (feed via SCORE_SHIFT_UP/DOWN rows in
  live_triggers, NO Telegram — momentum pings were too noisy).
- **SL/TP exit alerts rebuilt**: deduped (`_alert_state` re-arms on zone exit —
  fixed the every-60s MSFT spam) + rich (shares, value, cost basis, unrealized
  P&L $/%) + actionable (💰 SELL ALL / ✊ Hold buttons → SELLEX/SELLHOLD
  callbacks → `close_position`).

**Portfolio / Alpaca sync (big one)**
- **Two-way reconcile** (`_reconcile_with_alpaca`, runs every refresh_prices):
  removes phantom local rows (killed MSFT "position not found"), imports Alpaca
  positions missing locally (avg cost from position, SL/TP from open brackets),
  backfills advisory ATR SL/TP on bare rows. New executor helpers:
  `list_positions`, `list_position_symbols`, `get_bracket_levels` (nested-leg
  aware). Local tracker went 2→11 positions matching Alpaca.
- **Real protective OCO orders** (`submit_protective_oco`, OCO order class):
  placed live SL+TP on the 7 bare positions (AAPL, AMD, GOOG, MU, NVDA, SPY, V)
  so they auto-exit even if NuroQ is offline. `/api/protect` resolves shares
  from LIVE position (not stale tracker). Telegram PROTECT callback. NOTE:
  META/NVO/SAN/SONY still TP-only (no stop) — user chose "bare 7 only".
- **Portfolio page redesign**: 4 stat cards (cost basis, market value,
  unrealized P&L, N-day return), beautiful equity-curve area chart (7/30/90D
  toggle, green/red by direction, baseline ref line), holdings table with
  cost-basis + P&L$ columns + TOTAL footer.

**Design / infra**
- **Global type scale bump** (Google Finance-inspired): overrode Tailwind
  `fontSize` scale + base body 13→14px + comfortable line-heights + table cell
  padding (`px-3 py-2.5`). One CSS change, all views inherit. Fixed the cramped
  Bloomberg-density feel.
- Cron: premarket 08:15→08:00 CT, research 01:00→03:30 CT, Memorial-Day holiday
  rollback in `run_research_cycle`, premarket news scope trimmed to actionable
  subset (~83min → ~5-7min).
- launchd backend autostart via `uv run` (TCC-safe, uv in /opt/homebrew);
  `restart.sh` launchd-aware (`scripts/install-backend.sh`).
- Logging convention established: `session.md` + `CLAUDE.md` (auto-read on
  session start; this is that file).
- Fixed live-agent AttributeError crash (`_check_score_shift` missing from a
  prior garbled-edit; re-added).

**End state:** 91/91 tests pass · backend running via launchd on :8000 ·
iOS bundle synced · 11 Alpaca positions, 7 with full OCO protection · branch
`feat/algo-claude-improvements` (substantial uncommitted work).

**Open / next-session candidates:**
- Protect the 4 TP-only positions (META/NVO/SAN/SONY) with full OCO if wanted
  (needs cancel + replace of existing TP order).
- Commit the working tree (lots uncommitted on `feat/algo-claude-improvements`).
- Equity history is thin (30/90d show +0.00%, only 7d has movement) — fills in
  as the paper account accrues daily snapshots.
- Verify the new type scale on phone; tune any spot that wraps/oversizes.

---

## Session 4 — 2026-05-25 (evening)

- Today **design lab**: 4 layout variants (`TodayA-D.tsx`) behind a switcher.
- **launchd cron** installed (research + premarket) via `scripts/install-schedules.sh`.
- FastAPI **auto-starts live agent** on boot (`NUROQ_AUTOSTART_AGENT=1`).
- See `HANDOFF.md` "Session 4" for detail.

## Session 3 — 2026-05-25 (earlier)

- All 8 React views wired (Analyze, Watchlist, Portfolio, Scanner, Agent,
  Signals, System, Logs). Quick Trade actually trades via `/api/trade`.
- Command palette analyzes tickers. `PUBLISHING.md` (TestFlight + App Store).

## Session 2 — 2026-05-24 (evening)

- **React + FastAPI + Capacitor iOS** stack stood up alongside Gradio.
- Today view full fidelity; status pills; iOS responsive + native app.
- See `HANDOFF.md` for the migration detail.

## Session 1 — 2026-05-24

- Gradio UI redesign (Phases A–D): density pass, Today tab, drill-down,
  ⌘K palette, command bar. Test-suite watchlist-wipe bug fixed.

---

_Older history predates this log — see `HANDOFF.md`, `CAPABILITIES.md`,
`ARCHITECTURE.md`._
