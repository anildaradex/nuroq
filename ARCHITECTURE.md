# NuroQ — Target Architecture (multi-phase rebuild)

> Living design doc. Updated as phases land. See `CAPABILITIES.md` for current
> user-facing functionality; `SCHEDULING.md` for how to schedule the overnight
> cycle; this doc describes where we're going.
>
> **Last updated:** 2026-05-23 (Phase 2 ships)
> **Status:** Phases 1 + 2 complete · Phase 3 next

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

### Phase 3 — Live reactive agent

**Goal:** Replace cycle-based `AgentLoop` with WebSocket-driven reaction.

**Deliverables:**
- New `live_agent.py` module
- Reads `watchlist_today` at start of market hours
- Subscribes to all watchlist tickers via existing `MarketStreamer`
- On bar trigger: read cached state, update technicals, fast re-score, check
  for threshold crossing
- On crossing: Telegram approval via existing `gatekeeper`
- Existing 4-hour `AgentLoop._run_cycle` is retired during market hours
  (research cycle handles the universe scan)

**Out of scope for Phase 3:** News reactivity (Phase 4), LLM queue refinement.

---

### Phase 4 — News reactivity

**Goal:** Detect breaking news per ticker and refresh AI scores accordingly.

**Deliverables:**
- Periodic news poller (every 15 min during market hours for watchlist tickers)
- New SQLite table `news_cache` (ticker, headline, body, source, published_at)
- Shock detector: regex on title for `{earnings, downgrade, FDA, halt, M&A,
  guidance, recall, lawsuit}`
- On shock: enqueue LLM re-score for that ticker; bump priority in watchlist
- Single-worker LLM queue so re-scores don't contend for the GPU

**Open question:** News source. Options:
- Polygon news REST (free tier limits to 5 req/min — too low for 500 tickers
  every 15 min = 2,000 calls/hr)
- Alpaca news WebSocket (free tier has limited symbol universe)
- Polygon paid tier ($30/mo, unlimited)
- RSS feeds from Yahoo Finance / SeekingAlpha / etc. (free but messy)

---

### Phase 5 — Operational polish

**Goal:** Make the system observable and resilient.

**Deliverables:**
- Unified scheduler (APScheduler) tracking all three tier statuses
- Dashboard panel showing: last research run timestamp, watchlist size, last
  live trigger, LLM queue depth, cache freshness per ticker
- Health checks: research cycle failed alert, cache stale alert
- Optional: cron failure email/Telegram

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
