# NuroQ — Target Architecture (multi-phase rebuild)

> Living design doc. Updated as phases land. See `CAPABILITIES.md` for current
> user-facing functionality; `SCHEDULING.md` for how to schedule the overnight
> cycle; this doc describes where we're going.
>
> **Last updated:** 2026-05-24 (Phases 2.5, 3b, 4b, 5 ship — rebuild complete · regression suite at 82 tests)
> **Status:** All planned phases complete. Future work tracked under Open follow-ups in CAPABILITIES.md.

---

## Why this rebuild

The original `AgentLoop._run_cycle` does research and reaction in the same code
path on a 4-hour cycle. That conflates two workloads with fundamentally
different latency budgets:

- **Research** ("is this a good name?") tolerates minutes. Needs SEC filings,
  fundamentals, LLM reasoning, RAG grounding.
- **Reaction** ("is the price doing something interesting *right now*?")
  needs to fit in milliseconds. Cheap deterministic math on cached state.

Trying to do both in one cycle means you either run research too rarely (miss
opportunities) or run reaction too slowly (miss the move). The right answer is
to separate the tiers.

---

## Target architecture — three tiers

```
┌─────────────────────────────────────────────────────────────────┐
│ TIER 1 — OVERNIGHT RESEARCH   (runs once at 02:00 ET)           │
│ • Scan full US equity universe (1 Polygon grouped snapshot)     │
│ • Incrementally refresh OHLCV cache                             │
│ • Refresh fundamentals for watchlist candidates                 │
│ • Refresh SEC filings for top 500 by liquidity                  │
│ • Run Gemma analysis on top 500, CACHE the AI scores            │
│ • Output: `watchlist` table — ~200-500 pre-scored tickers       │
│ Budget: ~30-90 min, no time pressure                            │
└─────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ TIER 2 — PRE-MARKET REFRESH   (runs at 09:15 ET)                │
│ • Pull premarket prices for watchlist                           │
│ • Recompute technicals with fresh bars                          │
│ • Pull overnight news per ticker (last 16h)                     │
│ • Re-score quant for each watchlist member                      │
│ • Re-run LLM only on names where news materially changed        │
│ • Output: `watchlist_today` — ready for tick 1                  │
│ Budget: 5-10 min                                                │
└─────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ TIER 3 — LIVE REACTIVE AGENT   (continuous, market hours only)  │
│ • Alpaca WebSocket subscribed to all watchlist tickers          │
│ • On bar arrival:                                               │
│    - Update live price/volume in cached technicals (cheap)      │
│    - Recompute %B, RSI, change% (cheap, ~1ms)                   │
│    - Re-run calculate_quant_score with CACHED fundamentals      │
│      and CACHED AI score                                        │
│    - If threshold CROSSED → Telegram approval                   │
│ • On news event (poll every 15 min):                            │
│    - Detect shock keywords (earnings, FDA, M&A, downgrade)      │
│    - Queue an LLM re-score for that ticker (async, 1 worker)    │
│ • NO Polygon REST in hot path                                   │
│ • NO yfinance in hot path                                       │
│ • NO LLM in hot path (queued async only)                        │
│ Budget: <100ms per tick                                         │
└─────────────────────────────────────────────────────────────────┘
```

## The architectural insight

**The LLM's job is "is this a good name to trade?" — that does not change
minute-to-minute.** The Gemma analysis of NVDA at 3am is still ~95% valid at
10am unless news has dropped. So we cache the AI score overnight per ticker
and use it as a relatively static input during market hours.

**What changes minute-to-minute is the technical state** (price, %B, RSI,
volume) — and that's cheap deterministic math from the live bar. The quant
score recomputes in microseconds with cached fundamentals + cached AI score
+ fresh technicals.

This factoring is what makes sub-100ms reaction possible while still using a
local LLM for the heavy reasoning.

---

## Locked-in design decisions (from session 2026-05-23)

| Decision | Choice |
|---|---|
| Where overnight research runs | Same Mac, cron-scheduled at 02:00 ET |
| Live engine reaction policy | React on threshold **crossings** only (e.g., crosses 65 from below). Avoid spamming on a stock that sits at 70 all day. |
| LLM refresh during market hours | On news shock only. Use cached overnight score by default. |
| Phase 1 scope | Persist fundamentals + AI scores to SQLite (foundation for everything else). |

---

## Phased rollout

### Phase 1 — Persistent caches (foundation) ✅ DONE

- `fundamentals_cache` and `ai_scores_cache` SQLite tables created
- `FundamentalsCache` class (L1→L2→L3 layered, 24h TTL)
- `AIScoreCache` class with `store` / `get` / `invalidate`
- Both `analyze_stock` (full path with RAG) and `analyze_single_ticker_data`
  (scanner path) write through to `ai_scores_cache`
- `get_fundamentals` reads L1 → L2 → fetches L3 only on miss

---

### Phase 2 — Overnight research cycle ✅ DONE

- `research_cycle.py` — standalone CLI with `--top-n`, `--no-telegram`,
  `--dry-run` flags. Sets `NUROQ_BACKGROUND_SERVICES=0` before importing
  dashboard so it never conflicts with a running dashboard's Telegram poller.
- `watchlist_today` SQLite table — ranked snapshot, atomically replaced each run
- `_build_watchlist_rows` ranks by quant_score → ai_score → change_pct
- One-shot Telegram notifications at start, 25/50/75/100% milestones
- Same `run_research_cycle` function is also wired to the "🔬 Run Research
  Cycle" button in the dashboard top-right for ad-hoc runs
- macOS launchd + Linux cron/systemd setup documented in `SCHEDULING.md`

**Out of scope for Phase 2:** UI changes beyond the trigger button, live
agent changes (Phase 3).

---

### Phase 3a — Live reactive agent ✅ DONE

- `live_agent.py` — `LiveAgent` class with `start` / `stop` / `_on_bar` /
  `_evaluate` / `_check_crossings` / `status`
- Reads `watchlist_today` + currently-held positions at start, subscribes via
  `MarketStreamer.bar_callback` (new sibling to the existing notable-event
  trigger_callback path)
- Per-ticker in-memory state: `TickerState` (baseline daily bars, intraday H/L/V
  rolling, last score, weekly trend, held flag)
- Hot path on each bar (<100ms target): update intraday state → synthesize
  today's bar → recompute technicals → pull cached fundamentals + AI score →
  recompute final quant_score → check for threshold CROSSING (not above-threshold)
- BUY crossing through 65↑ → existing async approval flow + bracket order
- SELL crossing through 30↓ on held position → `close_position` + portfolio remove
- Daily BUY cap (default 5) suppresses excess approvals with one notification
- All crossings logged to `live_triggers` SQLite table with action taken
- Off-hours guard: refuses to start unless `NUROQ_FORCE_LIVE=1` or market open
- Fallback: if `watchlist_today` is empty, uses `TOP_TICKERS`
- The old 4-hour `AgentLoop._run_cycle` is retired — only the LiveAgent runs
  during market hours; the overnight research cycle (Phase 2) handles
  universe-wide refresh
- Agent tab UI now shows live status: subscribed/held counts, bars processed,
  BUYs fired/cap, SELLs fired, suppressed (cap), started_at, last bar time

### Phase 3b — Live agent robustness ✅ DONE

- WebSocket reconnect loop in `MarketStreamer._run_stream`: catches exceptions
  from `_run_forever`, sleeps with exponential backoff (5s → 120s capped), retries
  up to `max_reconnect_attempts` (default 8). On exhaustion, marks streamer stopped.
- Stale-bar detection via `MarketStreamer.check_staleness()`: tracks
  `last_bar_received_at` per bar; returns a report dict if age > 5 min while
  running, optionally fires a `stale_alert_callback` once per staleness episode.
  Auto-resets when bars resume.
- Hysteresis (`hysteresis_bars`, default 2): crossing must persist for N
  consecutive bars before firing. New `TickerState.bars_above_buy /
  bars_below_sell` counters track this.
- Per-ticker cooldown (`per_ticker_cooldown_s`, default 30 min): after a fire,
  same-ticker crossings within the window are blocked at `_cooldown_ok` check.

---

### Phase 4a — News reactivity (final-check gating) ✅ DONE

- `news_engine.py` — three components: `NewsClassifier` (pure-function keyword
  classifier, 4 buckets: POSITIVE_BOOST / NEUTRAL / NEGATIVE_WARNING /
  NEGATIVE_BLOCK), `NewsPoller` (background thread, polls top-N watchlist +
  held positions every 30 min, writes new headlines + classifications to
  news_cache), `check_news_for_crossing(ticker)` (hot-path safe helper for
  LiveAgent — single SELECT, no API call)
- `news_cache` SQLite table — (ticker, headline, source, classification,
  published_at, ingested_at) with PRIMARY KEY (ticker, headline) so
  `INSERT OR IGNORE` de-dupes naturally
- `LiveAgent._handle_buy_crossing` now calls `check_news_for_crossing` before
  firing: NEGATIVE_BLOCK suppresses + logs `SUPPRESSED_NEWS`; WARNING/BOOST
  decorate the reasoning string with a tag; NEUTRAL/no-news fires normally
- News poller gated by `NUROQ_BACKGROUND_SERVICES` so cron jobs don't double-poll
- Budget: 35 tickers × every 30 min = 70 Polygon calls/hr (well under 300/hr free-tier limit)
- Test coverage: 4 classifier buckets, news_cache round-trip + dedup + TTL,
  LiveAgent suppress/decorate paths

### Phase 4b — News-driven LLM re-score ✅ DONE

- `llm_queue.py` — new `LLMRescoreQueue` class. Single worker thread,
  per-ticker dedup (via a `_pending` set), idle gap between re-runs to avoid
  GPU pinning. Tracks queue depth + processed count + dedup drops + errors
  via `.status()`.
- `NewsPoller` extended with `on_shock_callback(ticker, verdict)` constructor
  param. Fires once per newly-stored non-NEUTRAL headline.
- `dashboard.py` wires the callback to: `ai_score_cache.invalidate(ticker)` +
  `llm_rescore_queue.enqueue(ticker, reason)` for `NEGATIVE_BLOCK` and
  `NEGATIVE_WARNING`. `POSITIVE_BOOST` decorates approvals but doesn't trigger
  a re-score (cached score already optimistic).
- Worker calls `analyze_single_ticker_data` which writes the fresh score
  through to `ai_scores_cache` (Phase 1 wiring).

### Phase 4c — Future (Alpaca news WebSocket, LLM sentiment, etc.)

Out of scope. Tracked under CAPABILITIES.md follow-ups.

---

### Phase 5 — Operational polish ✅ DONE (scope: observability)

- New `🩺 Health` Gradio tab via `render_health_snapshot()`. Markdown table
  with traffic-light indicators per component:
  - Research cycle: last-run timestamp + age + candidate count
  - LiveAgent: subscribed/held counts, bars processed, BUYs fired/cap, last bar
  - NewsPoller: cycles completed, headlines ingested, last cycle age
  - LLM Rescore Queue: depth, processed, dedup drops, errors
  - Alpaca: connection status + equity + buying power
  - SQLite caches: row counts for price_history / fundamentals_cache /
    ai_scores_cache / news_cache / live_triggers / all_signals
- 🔄 Refresh Health button refreshes the snapshot on demand.

### Phase 2.5 — Pre-market refresh ✅ DONE

- `premarket_refresh.py` standalone CLI. Sets `NUROQ_BACKGROUND_SERVICES=0`
  before importing dashboard (same pattern as `research_cycle.py`).
- Reads `watchlist_today`, pulls fresh Polygon snapshot, updates `price` and
  `change_pct` columns in-place for each watchlist ticker.
- Pulls news for watchlist tickers from Polygon (last 16h), classifies via
  `NewsClassifier`, writes to `news_cache`.
- Flags: `--no-telegram`, `--dry-run`, `--news-only`, `--price-only`.
- Designed for 09:15 ET launchd / cron entry (see SCHEDULING.md).

### NOT YET DONE (deferred)

- APScheduler unification (cron is fine for now)
- AlpacaNewsStream WebSocket as alternative to polling
- LLM-based sentiment as augmentation for keyword classifier
- Per-watchlist-row LiveAgent status (current panel is aggregate only)

---

## What already exists (don't rebuild)

These pieces of the current code carry over unchanged or with minimal edits:

| Component | Where | Role in new arch |
|---|---|---|
| `MarketStreamer` (Alpaca WebSocket) | `event_stream.py` | Tier 3 bar dispatch |
| `HistoryCache` (SQLite OHLCV) | `data_fetcher.py` | Tier 1+3 OHLCV source |
| `calculate_technicals`, `calculate_quant_score` | `scoring.py` | Tier 1+3 math |
| Polygon grouped snapshot fetcher | `data_fetcher.py` | Tier 1 universe scan |
| Gemma inference (MLX) | `dashboard.py` | Tier 1 LLM (async only in Tier 3) |
| `TradeGatekeeper` (Telegram) | `dashboard.py` | Tier 3 approval |
| `LiveAlpacaExecutor.submit_bracket_order` | `alpaca_executor.py` | Tier 3 execution |
| `PortfolioManager` | `dashboard.py` | All tiers (position state) |

## What gets retired

- `AgentLoop._run_cycle` as currently structured (4-hour cycle scanning the
  market and analyzing). Replaced by `research_cycle.py` (Tier 1) + `live_agent.py` (Tier 3).
- The 20-second per-ticker sleep in the agent loop (rate limiter handles this).
- In-memory `funds_cache` / `news_cache` as primary storage (becomes L1 cache
  on top of persistent SQLite).

## What stays as a fallback path

- Manual single-ticker `analyze_stock` (UI button) — still useful for ad-hoc
  deep dives outside the watchlist.
- `deep_market_scan` (UI button) — still useful for full-universe ad-hoc scans.
- Quick Trade UI — unchanged.

---

## Hard constraints

### Polygon free tier (5 req/min)
- 12,000-ticker universe scan: 1 request (grouped snapshot) ✅
- 500 news fetches every 15 min during market hours = 2,000/hr ❌
  - Workaround: Polygon paid tier ($30/mo) OR less frequent polling OR alternate source

### LLM inference latency
- Gemma 4B on Apple Silicon: 5-15s per inference
- 500 watchlist analyses overnight: ~30-90 min ✅ (acceptable for Tier 1)
- On-demand re-scores during market hours: ~10s each ✅ (queued, not per-bar)

### Apple Silicon (MLX)
- Ties Tier 1 + Tier 3 LLM work to a Mac
- Future migration option: `vLLM` on GCE GPU or hosted Gemma API

### Mac sleeping overnight
- Cron requires the Mac to be awake at 02:00 ET
- Options: leave Mac on, use `pmset` to schedule wake, or run on a Mac mini

---

## Open questions to revisit later

- **Watchlist size**: 200? 500? 1000? Depends on Polygon news rate limit and
  LLM throughput overnight.
- **Threshold crossing definition**: cross-up only, or also cross-down? Cooldown
  between crossings?
- **News shock keywords**: keep a configurable list; start with
  `{earnings, beat, miss, downgrade, upgrade, FDA, halt, M&A, acquisition,
  guidance, recall, lawsuit, SEC investigation}`.
- **Watchlist evolution mid-day**: if a non-watchlist stock has a big move,
  should it get added live? Or wait for next overnight cycle?
- **Multi-machine sync**: if overnight ever moves to a cloud VM, how do the
  caches sync to the Mac running the live agent? SQLite over Tailscale,
  rsync, S3 sync, Postgres?

---

## How to keep this doc fresh

When a phase ships:
1. Update the "Status" line at top.
2. Move the phase's "Deliverables" into a "Done" section (or check them off).
3. If implementation revealed a different design, edit the relevant phase
   section to reflect what actually shipped.
4. If new open questions surfaced, add them to "Open questions."
5. Bump the "Last updated" line.

When the rebuild is complete:
- Mark all phases done, archive this doc, fold any still-true content into
  `CAPABILITIES.md`.
