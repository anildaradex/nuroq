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

## Current session — Session 6 — 2026-06-03 (IN PROGRESS)

**§475(f) mode + PDT-rule context (tax/regulatory):**
- Reviewed two user claims for accuracy (web-verified): (1) **PDT rule abolished**
  — SEC approved FINRA's proposal 2026-04-14, effective ~2026-06-04; $25k min &
  "Pattern Day Trader" label gone, replaced by risk-based intraday margin (Rule
  4210, $2k margin min), 18-mo broker phase-in to ~Oct 2027. NuroQ had NO PDT
  logic, so nothing to remove. (2) **§475(f) MTM election** makes wash-sale
  (§1091) + $3k cap-loss limit inapplicable (ordinary gains/losses, Form 4797) —
  it's an election statement + Form 3115, NOT a "Form 475." **2026 window CLOSED
  for existing individuals** (was due Apr 15, 2026); open for a new entity (~75-day
  Rev. Proc. 99-17) or 2027 (Apr 15, 2027).
- **Implemented `NUROQ_SECTION_475` flag (default OFF).** Built on the agreed
  basis: ready for a future entity / 2027 election, never assumes the election.
  - `dashboard.section_475_active()` helper (reads env live; no restart needed).
  - `wash_sale_check()` short-circuits at the top when the flag is on → returns
    `risk=False` + explanatory hint BEFORE any Alpaca call or cache write. Single
    chokepoint neutralizes ALL three BUY gates (live_agent, handle_quick_trade,
    iOS OrderReviewModal) since they all key off `risk`. Not cached → toggling is
    instant.
  - `alpaca_executor._connect` safety belt now accepts `NUROQ_SECTION_475=1` OR
    `NUROQ_WASH_SALE_AWARE=1` to satisfy the live-trading wash-sale acknowledgment.
- **Tests: 91 → 96** (`TestSection475`): helper default-off, short-circuit
  returns-before-Alpaca, default-path guard still flags loss re-entry (regression),
  §475 satisfies the live belt, belt still blocks with neither ack. `master_test_suite.py` green (4.2s).
- **Key principle reaffirmed:** the wash-sale code is *advisory only* — removing/
  disabling it does NOT change real tax treatment (set by the filed election +
  1099-B), and NuroQ is still paper-only (no live taxable trades today).

**Option B — core quant layer now PROPOSES sells (shipped):**
- `dashboard.propose_sells()` — PURE, fails-closed. Deliberate pass over held
  Alpaca positions (via `list_positions`) × current watchlist scores → ranked
  proposals of three kinds:
  - **TAX_LOSS_HARVEST** — §475-GATED (suppressed unless `NUROQ_SECTION_475=1`,
    since otherwise it's a wash-sale trap). Material paper loss (≥2%) on a
    non-strong holding (score < 55 or off-list) → realize the now-deductible loss;
    re-entry unrestricted under §475.
  - **ROTATE** — weak holding (score ≤ 45) + a non-held BUY candidate out-scoring
    it by ≥20 → rotate capital into the stronger name (different ticker = never a
    wash sale; PDT-free = same-session fine).
  - **EXIT_WEAK** — weak holding, no stronger candidate → trim/close.
  Tunables: `SELL_PROPOSE_WEAK_SCORE=45`, `HARVEST_SCORE_CEILING=55`,
  `HARVEST_MIN_LOSS_PCT=0.02`, `ROTATE_SCORE_EDGE=20`.
- `dashboard.log_sell_proposals()` — writes PROPOSE_SELL rows to live_triggers
  (auto-surface in the React Recent Activity feed); deduped per (date,ticker,kind).
- Surfaced 3 ways: **Next Actions** card (`render_next_actions`, top 4),
  **`GET /api/propose-sells`** (new backend endpoint, returns proposals + §475
  flag), and the **premarket cron** (`premarket_refresh.main` now calls
  `log_sell_proposals()` each morning → daily feed entries).
- ADVISORY ONLY — nothing auto-executes; user acts via Watchlist ⚡ / Telegram /
  close_position. Verified live: ran against the real paper account → coherent
  ROTATE proposals (SPY/AAPL scores decayed below the live SELL threshold).
- Tests 96 → **104** (`TestSellProposals`): harvest suppressed w/o §475, harvest
  under §475, strong-loser-not-harvested, rotate vs exit-weak, healthy=none,
  fails-closed, dedup. Suite green (~4.2s).

✅ **Backend started 2026-06-03 23:38, now serving new code.** `/api/propose-sells`
→ 200 with live ROTATE proposals (SPY/AAPL/MU scores decayed to 15-25). LiveAgent
autostarted (154 tickers, 9 held), Telegram digest sent.
⚠️ Started MANUALLY (`uv run uvicorn backend.api:app`), NOT via launchd — the
`com.nuroq.backend` job fails with **exit 78 / TCC** (no logs on kickstart). Manual
process won't survive reboot. Fix: re-grant Full Disk Access to `/opt/homebrew/bin/uv`
+ `/bin/sh`, then `launchctl kickstart -k gui/$(id -u)/com.nuroq.backend`.

**Stated north-star (recorded):** evolve NuroQ into a fully autonomous live-trading
app — places autotrades without intervention, backtests, measures its own efficacy,
learns from mistakes, self-improves. Build every feature toward that trajectory.

**Open / next-session candidates:**
- Restart the backend to serve `/api/propose-sells`; build the React UI for sell
  proposals (feed already shows PROPOSE_SELL rows; a dedicated card w/ SELL buttons
  would close the loop).
- Option C: first-class Investor vs Trader-§475 regime surfaced in the UI.
- Tune proposer thresholds against real watchlist behavior (initial live run
  flagged several ROTATEs — confirm the score-decay band feels right).

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
