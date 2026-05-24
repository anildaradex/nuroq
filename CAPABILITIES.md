# NuroQ — Capabilities & Architecture

> Living reference for what this tool does, how it's wired, and where its limits are.
> **Last updated:** 2026-05-24 (Phase 4a ships: news engine — NewsPoller + NewsClassifier + live-agent final-check gates BUY approvals on breaking news. Master test suite expanded to 61 tests, all passing.)
>
> **For where this is going:** see `ARCHITECTURE.md` for the multi-phase rebuild plan (3-tier system: overnight research → premarket refresh → live reactive agent). See `SCHEDULING.md` for cron / launchd setup.
>
> **How to update:** when you ship a feature or fix that changes a user-visible workflow, a subsystem, or an honest-assessment caveat, edit the relevant section here and bump the date. Keep it terse.

---

## Table of contents

1. [What you can do as a user](#part-1--what-you-can-do-as-a-user)
2. [Subsystems that power it](#part-2--subsystems-that-power-it)
3. [Honest assessment — good for / not good for](#part-3--honest-assessment-good-for-vs-not-good-for)
4. [Open follow-ups](#part-4--open-follow-ups)

---

# Part 1 — What you can do as a user

## 1. Analyze a single stock in depth

**Where:** Analyze tab → enter a ticker → click "Run Analysis"

**What you get:**
- Plotly candlestick chart with Bollinger bands, volume subplot, RSI subplot
- Trade setup card: suggested shares (ATR-sized), stop-loss, take-profit, position size in $, days to next earnings
- AI Chain-of-Thought card: bull case, bear case, key risk, considerations
- Hybrid quant score (0–100) with rating (BUY/SELL/HOLD)
- RAG-grounded context from SEC 10-K/Q + recent news

**Useful for:** Deep dive on a name you're considering. Combines technical + fundamental + AI reasoning + regulatory disclosures in one screen.

**Implementation:** `dashboard.py:analyze_stock` (line ~882).

## 2. Scan the market for opportunities

**Two scopes:**

### Top 20 Universe (Market Scanner → Top 20)
- Hard-coded list of mega-caps (NVDA, AAPL, MSFT, etc.) at `dashboard.py:TOP_TICKERS`
- Fast — uses cached data when fresh
- Returns ranked table

### Global Deep Scan (Market Scanner → Global Deep Scan)
- Pulls Polygon's full US market snapshot (~12,000 stocks)
- Filters: price ≥ $2, volume ≥ 500k, intraday change ≥ 2%
- Takes top 100 by volume
- Runs full quant + AI scoring on each
- Surfaces stocks scoring ≥ 60 as "candidates"

**Useful for:** Daily morning routine — find today's quant-grade momentum setups without manually screening.

**Implementation:** `dashboard.py:deep_market_scan` (line ~1359).

## 3a. News engine (Phase 4a)

**What runs in the background:**
- `NewsPoller` thread polls Polygon news for top 35 watchlist + held tickers every 30 min
- Each headline classified by keyword regex into 4 buckets: POSITIVE_BOOST / NEUTRAL / NEGATIVE_WARNING / NEGATIVE_BLOCK
- New (non-dup) headlines persisted to `news_cache` with classification

**What it does for trading decisions:**
- LiveAgent's `_handle_buy_crossing` reads the latest news classification (single SQLite SELECT, <5ms) before firing approval:
  - **NEGATIVE_BLOCK** (halt, SEC investigation, fraud, bankruptcy, delisting, …): **suppresses** approval, logs `SUPPRESSED_NEWS` in live_triggers, no Telegram message
  - **NEGATIVE_WARNING** (downgrade, miss, lawsuit, recall, CEO departure, …): fires approval but adds `⚠️ Recent negative news: …` to the message
  - **POSITIVE_BOOST** (earnings beat, FDA approval, contract win, buyback, …): fires approval with `📈 Catalyst: …` prepended
  - **NEUTRAL / no news**: fires normally

**Useful for:** Avoiding approvals on stocks that just had bad news, and tagging approvals with positive catalysts so you can prioritize.

**Implementation:** `news_engine.py:NewsPoller` + `NewsClassifier` + `check_news_for_crossing`; integrated at `live_agent.py:_handle_buy_crossing`.

## 3. Run an autonomous agent loop (LiveAgent — Phase 3a)

**Where:** Agent tab → Start

**What it does:**
- Reads today's watchlist (`watchlist_today`, populated by the overnight research cycle / 🔬 Run Research Cycle button) + currently-held positions
- Subscribes to all of them via Alpaca WebSocket (every minute bar arrives ~150 times/min during market hours)
- On every bar: recomputes technicals + quant score using **cached** fundamentals and AI score (no Polygon/yfinance/LLM in hot path — sub-100ms per bar)
- Detects **threshold crossings** (not just above-threshold) — score goes from < 65 to ≥ 65 → BUY crossing; > 30 to ≤ 30 on held position → SELL crossing
- BUY crossing → Telegram approval (async, UI not blocked)
- SELL crossing on held → broker close + bracket cancellation
- **Daily BUY cap (5 default)** prevents Telegram spam on market-wide rallies
- All crossings logged to `live_triggers` SQLite table for review

**Market hours guard:** Won't start outside US equity hours (M-F 09:30-16:00 ET) unless `NUROQ_FORCE_LIVE=1` is set. Currently in testing mode? Set the env var.

**Useful for:** Truly reactive monitoring without an LLM call per bar. The overnight cycle does the heavy "is this a good name?" work; the live agent reacts to "is the technical state suddenly compelling?" in real time.

**Implementation:** `live_agent.py:LiveAgent` + `dashboard.py:AgentLoop` (thin wrapper preserving Start/Stop buttons).

## 4. Real-time event-driven analysis

**Implicit in agent loop — runs whenever agent is active.**

**What it does:**
- Opens an Alpaca WebSocket subscribing to 1-minute bars for the agent's watchlist
- Trigger 1: any single-bar move ≥ 2% intra-minute
- Trigger 2: price > 3% above 5-minute rolling average (breakout)
- Per-ticker debounce: same name can't fire again within 5 minutes
- Triggers fire `analyze_stock(ticker, is_auto=True)` on a 2-worker executor (doesn't block the WebSocket reader)

**Useful for:** Catching breakouts in real time during the trading day without re-scanning the whole market. The 4-hour cycle picks the watchlist; the streamer reacts to live moves.

**Implementation:** `event_stream.py:MarketStreamer`.

## 5. Place manual trades with broker-side SL/TP

**Where:** Quick Trade accordion at the top of every page

**Options:**
- Order types: Market, Limit, Stop, Stop-Limit, Trailing Stop
- TIF: Day, GTC, OPG, IOC, FOK
- **Bracket mode:** on Market orders, optional Stop Loss + Take Profit fields. When both are filled, places an atomic bracket order at Alpaca (entry + SL + TP in one call).
- Idempotent (`client_order_id`) — double-clicks don't double-fire.

**Useful for:** Quick discretionary trades with proper risk management. Don't have to remember to place a separate stop afterward — the broker holds the bracket.

**Implementation:** `dashboard.py:handle_quick_trade` (line ~533) + `alpaca_executor.py:submit_bracket_order` (line ~115).

## 6. Monitor positions and account

**Where:**
- Top of dashboard: Alpaca Account panel (Equity / Cash / Buying Power / Positions / Today's P/L / 30-day return)
- Portfolio tab: open positions table with avg price, current price, PnL %, SL/TP

**What runs in the background:**
- Position monitor thread every 60 seconds: fetches current prices via yfinance, updates SQLite portfolio rows
- SL hit → 🛑 Telegram alert
- TP hit → 🎯 Telegram alert

**Useful for:** At-a-glance view of where you stand without opening the Alpaca website. Alerts when key levels touch.

**Implementation:** `dashboard.py:render_alpaca_panel` (line ~547) + `dashboard.py:PortfolioManager.refresh_prices` (line ~428).

## 7. Build a personal signal history

**Where:** Signals tab → "All Signals" table

**What it stores:**
- Every analysis run gets a row in the `all_signals` SQLite table
- Captures: timestamp, ticker, name, industry, price, technicals summary, fundamentals summary, signal (BUY/SELL/HOLD), AI score, quant score

**Useful for:**
- Reviewing the agent's track record over time
- Feeding into RLFF training data (the DPO pipeline reads this table)
- Backtesting your scoring choices against actual forward returns

**Implementation:** `dashboard.py:PortfolioManager.log_analysis_result` + `all_signals` table at `dashboard.py:354`.

## 8. Train the AI model on your own data (Gemma DPO)

**Where:** Command line — `python create_dpo_training_json.py` then `python train_dpo.py`

**What it does:**
- **Generator** (`create_dpo_training_json.py`): reads `price_history` SQLite table, computes real technicals at historical "as-of" dates, labels by 20-day forward return, emits 6,000+ preference pairs in `dpo_data/{train,valid}.jsonl`
- **Trainer** (`train_dpo.py`): runs `mlx_lm_lora.train --train-mode dpo` against Gemma 3 4B with LoRA adapter saved to `adapters_gemma_dpo/`

**Useful for:** Aligning the AI score component with *your* actual market data (RLFF — Reinforcement Learning from Financial Feedback).

**Honest caveat:** the AI score only contributes 10% of the final score and is gated on quant base ≥ 40, so training mostly affects nuance not direction. See `TRAINING_GUIDE.md` for the full pipeline.

## 9. Activity logging

**Where:** Activity Log tab

**What it shows:**
- Live append of every agent action, scan progress, error, trade execution
- "Refresh" + "Clear" buttons
- Same data also written to `activity.log` on disk

**Useful for:** Debugging the agent's decisions. Every BUY/SELL has a paper trail with the score breakdown that led to it.

---

# Part 2 — Subsystems that power it

## Data ingestion layer

| Source | Used for | Code |
|---|---|---|
| **Polygon.io** (free tier, 5 req/min) | OHLCV bars (100-day history), market-wide grouped snapshots, news fallback | `data_fetcher.py` |
| **yfinance** | Fundamentals (P/E, growth, market cap), earnings calendar, primary news, position price refresh | `data_fetcher.py`, `dashboard.py:434` |
| **StockTwits** | Crowd sentiment (BULLISH/BEARISH/NEUTRAL) | `data_fetcher.py:436` |
| **SEC EDGAR** (via `edgartools`) | 10-K/Q Risk Factors + MD&A for RAG grounding | `rag_engine.py:22` |
| **Alpaca Markets** (paper) | Account state, position state, order execution, real-time WebSocket bars | `alpaca_executor.py`, `event_stream.py` |

## Caching layer

Three-tier where applicable: L1 in-memory (fast, lost on restart) → L2 SQLite (persistent) → L3 source API.

- **SQLite `price_history`** — persistent OHLCV cache, ~327 tickers cached, ~5ms hits
- **SQLite `fundamentals_cache`** — persistent P/E, growth, market cap, news (24h TTL); read-through layer below the in-memory hot cache
- **SQLite `ai_scores_cache`** — persistent Gemma analysis output per ticker (24h TTL); foundation for overnight research → live reactive agent reuse (see `ARCHITECTURE.md`)
- **SQLite `watchlist_today`** — ranked output of the overnight research cycle (or the "🔬 Run Research Cycle" button); atomically replaced each run; consumed by the future live reactive agent (Phase 3)
- **In-memory news cache** — 2h TTL (L1)
- **In-memory fundamentals cache** — 4h TTL (L1)
- **Polygon rate limiter** — 5 req/min, releases lock during sleep so concurrent workers can queue

## Quant scoring engine (`scoring.py`)

- `calculate_technicals` — RSI(14), Bollinger Bands(20,2), ATR(14), SMA(20), volume metrics, semantic flags
- `calculate_quant_score` — 100-pt deterministic rubric (40 tech + 30 fund + 10 BB/vol + 10 sentiment/earnings + 10 gated AI)
- `calculate_sizing` — ATR-based, 1% risk per trade, 2× ATR stop, 4× ATR target (2:1 R/R)
- `get_weekly_confluence` — derives weekly trend from daily bars (no extra API call)

### Score rubric breakdown (100 pts max)

| Block | Sub-block | Pts |
|---|---|---|
| **Technicals (40)** | Trend confluence (daily + weekly) | up to 15 |
| | RSI positioning (oversold mean-reversion or optimal range) | up to 15 |
| | Relative volume | up to 10 |
| **Fundamentals (30)** | Revenue growth | up to 15 |
| | Valuation (P/E) | up to 15 |
| **Bollinger + Vol (10 net)** | %B (momentum-aware: breakout OR mean-reversion) | up to 10 |
| | Volatility penalty (daily range > 5%) | −5 |
| **Sentiment + Risk (10)** | StockTwits sentiment | up to 5 |
| | Earnings within 5 days | −10 |
| **AI conviction (gated, 0–10)** | Gemma 0–100 score × 0.10, only if quant_base ≥ 40 | up to 10 |

### Threshold ladder

| Score | Action |
|---|---|
| ≥ 60 | BUY classification (surfaces in deep scan) |
| ≥ 65 (≥ 75 with earnings risk) | Telegram approval triggered for auto-trade |
| 30 < score < 60 | HOLD |
| ≤ 30 + held + (RSI > 75 OR %B > 0.95 OR earnings risk) | Auto-SELL (closes position at broker) |

## LLM layer

- **Model:** Gemma 3 4B-it-4bit via MLX (Apple Silicon Metal)
- **Role:** Generates structured JSON ({reasoning, bull_case, bear_case, key_risk, considerations, rating, score})
- **Influence:** Up to 10 points of the final 100, gated on quant base ≥ 40
- **Fallback parser:** Regex backup if JSON output malformed (`dashboard.py:get_structured_data` line ~167)

## RAG layer (`rag_engine.py`)

- **Embedder:** `sentence-transformers/all-MiniLM-L6-v2`
- **Sources:** SEC 10-K/Q sections (1A, 7, 2) + Polygon news
- **Retrieval:** Top-3 chunks by cosine similarity to "financial risks and outlook" query
- Injected into LLM prompt only on full `analyze_stock` path (not on scanner sub-analyses for speed)

## Execution layer (`alpaca_executor.py`)

- `submit_advanced_order` — Market / Limit / Stop / Stop-Limit / Trailing Stop with `client_order_id`
- `submit_bracket_order` — atomic entry + SL + TP for BUY/SELL
- `close_position` — closes + cancels open SL/TP legs
- `get_account_summary` — equity, cash, buying power, today's P/L
- `get_portfolio_history` — N-day equity series + return %
- `_ensure_connection` — lazy reconnect on order time (recovers from stale tokens)

## Telegram bot (`TradeGatekeeper`)

- `request_approval(ticker, price, score, reasoning)` — sends inline-button approval message, awaits response with 5-min timeout
- `send_notification(message)` — fire-and-forget alert from any thread (uses `run_coroutine_threadsafe` onto the bot's loop)
- Used for: BUY approval gate, SL/TP hit alerts, auto-SELL exit announcements

## Real-time streaming (`event_stream.py`)

- Alpaca WebSocket (free IEX feed)
- Per-ticker rolling 20-bar deque
- Two trigger types (volatility ≥ 2%, breakout > 3% above 5-min avg)
- 2-worker ThreadPoolExecutor for async callback dispatch
- Per-ticker debounce (default 5 min)
- `set_watchlist` uses `loop.call_soon_threadsafe` to schedule subscribe/unsubscribe onto the WebSocket's loop (sync calls, not coroutines)

## Persistence (SQLite — `nuroq.db`)

| Table | Purpose |
|---|---|
| `price_history` | Cached OHLCV bars (~327 tickers, 31k rows) |
| `fundamentals_cache` | Persistent yfinance fundamentals (24h TTL) — Phase 1 of rebuild |
| `ai_scores_cache` | Persistent Gemma analysis output per ticker (24h TTL) — Phase 1 of rebuild |
| `watchlist_today` | Ranked candidates from overnight research cycle (Phase 2) |
| `live_triggers` | Every threshold crossing the LiveAgent detects (Phase 3a). Columns: ts, ticker, direction (BUY/SELL), score_before, score_after, price, action (FIRED/SUPPRESSED_CAP/SUPPRESSED_HELD/SUPPRESSED_NEWS), notes |
| `news_cache` | Classified news per ticker (Phase 4a). Columns: ticker, headline, source, classification (POSITIVE_BOOST/NEUTRAL/NEGATIVE_WARNING/NEGATIVE_BLOCK), published_at, ingested_at. PK on (ticker, headline) → INSERT OR IGNORE de-dupes |
| `portfolio` | Current open positions (synced from Alpaca) |
| `all_signals` | Persistent log of every analysis (1000+ rows from 2026-04 → present) |
| `shadow_trades` | Legacy table from old SQLite-only execution (still initialized, unused in main flow) |

## Background threads

- Position Monitor (every 60s)
- Telegram bot (own asyncio loop)
- Agent loop (every 4h)
- MarketStreamer WebSocket (own asyncio loop)
- Sleep prevention (`caffeinate` subprocess on macOS)

## Security posture

- Gradio defaults to `127.0.0.1` (localhost only)
- Optional HTTP Basic auth via `GRADIO_USER` + `GRADIO_PASSWORD` env vars
- Public tunnel opt-in (`GRADIO_SHARE=1`); logs a warning if share is on without auth
- `client_order_id` UUID on every Alpaca order for idempotency
- `.env` for all credentials, gitignored

---

# Part 3 — Honest assessment: good for vs. not good for

## Good for

1. **Paper trading practice with real signals** — full pipeline through Alpaca paper account with real bracket orders. You're not just simulating; you're actually trading paper money through real APIs at real prices.
2. **Structured daily research workflow** — replaces "scroll through Twitter, pick a stock, eyeball the chart" with "scan → score → review setup card → approve trade." More disciplined.
3. **Building a personal signal database** — `all_signals` table accumulates ground-truth labeled data over time. Useful for back-testing your own thesis or training models down the road.
4. **Learning quantitative trading by doing** — the rubric is transparent in `scoring.py`. You can see exactly why a stock got 67 instead of 73. Tweakable.
5. **Hands-off monitoring** — agent + Telegram approval means you can step away and still get notified of high-conviction setups.
6. **Privacy-sensitive LLM analysis** — Gemma runs locally. No prompts or portfolio data leaves your machine.
7. **Educational reference architecture** — the codebase shows a complete data → score → execute → monitor pipeline in <2000 LOC. Decent learning artifact.

## Not good for

1. **High-frequency anything** — 4-hour cycle, 60-second monitor. Designed for swing/position trading, not day trading.
2. **Options, derivatives, futures** — equities only, single leg, no spreads or hedges.
3. **Crypto** — no crypto data feed; Alpaca is equities-only on this account type.
4. **Real money** without a careful review pass — paper mode is hard-coded in `alpaca_executor.py:32` (`TradingClient(..., paper=True)`). Flipping that flag without an additional risk review would be reckless.
5. **News-event trading** — news ingestion is polled every few hours, not push-streamed. By the time the agent sees an earnings beat, the move is over.
6. **Cloud deployment** as-is — MLX is Apple Silicon only. Either run on a Mac or rewrite the inference layer.
7. **Production reliability** — single-process, single-machine, no health checks, no failover, no observability beyond a text log file. Personal tool, not infrastructure.
8. **Multi-user / multi-account** — global singletons everywhere (`alpaca_api`, `gatekeeper`, `portfolio_mgr`). One user, one account.

## Realistic best use today

A **personal paper-trading apprenticeship tool**: run it during market hours, it scans for setups using a disciplined quant rubric, surfaces 1–3 candidates a day via Telegram, you approve or reject. Over weeks/months you build a track record in `all_signals` you can review honestly. When you've seen the system make money on paper consistently, *then* think about flipping `paper=False` and risking real capital.

It's not a turn-on-and-print-money system. It's a structured assistant that makes you a more methodical trader by doing the boring data plumbing for you and forcing every decision through the same rubric.

---

# Part 4 — Open follow-ups

Items identified in the code review that have not yet been fixed. Update this list as items land or new ones are discovered.

## High-leverage / correctness

- Auto-SELL fires only when distress gate is met, but the SELL classification at score ≤ 30 itself could still benefit from a confirming bar (e.g. require it on two consecutive cycles before exiting).
- `analyze_single_ticker_data` (scanner path) does not use RAG context. If/when scan latency budget allows, wiring RAG into the scanner would improve scanner accuracy at the cost of speed.

## Operational

- **Generated data files in git:** `dpo_data/train.jsonl`, `dpo_data/valid.jsonl`, `activity.log`, `dashboard.log`, `Categorized_AIinput_Fixed.csv`, `nuroq_logo.png`, `explanation_te.mp3`, `scan_test.txt` should move to a `data/` dir + gitignored or to Git LFS.
- **WAL files not gitignored:** add `*.db-shm` and `*.db-wal` to `.gitignore`.
- **No log rotation:** `activity.log` and `dashboard.log` grow unbounded. Switch to `logging.handlers.RotatingFileHandler`.

## Architecture

- `dashboard.py` is ~1730 lines and mixes UI, orchestration, threading, OS subprocess, and DB layer. Splitting `EnsembleAnalyst`, `TradeGatekeeper`, `ShadowExecutor`, `PortfolioManager`, `AgentLoop` into their own modules would unblock real unit tests.
- `rag_engine.set_identity` runs at import time with a fake `anil@example.com`. Should be a real, working contact per SEC EDGAR ToS, and configured via env var.
- `MarketStreamer.start` uses the private `self.stream._run_forever()` API. Will break on alpaca-py minor version bumps. Public `.run()` is the supported entry point.

## UX / polish

- Gradio 6 deprecation: `theme` and `js` should move from `gr.Blocks(...)` to `demo.launch(...)`.
- Quick Trade SL/TP fields are only visible for Market orders. For Limit + bracket workflows, would need to extend `submit_bracket_order` to accept a LimitOrderRequest entry leg.
- Account panel auto-refreshes only on user click. A periodic refresh (every 30s while market open) would feel more alive.

## Cost / hosting

- MLX restricts deployment to Apple Silicon. If the goal is cloud deployment, either swap inference to `vLLM` on a GPU VM, or call a hosted Gemma via API. See session notes on GCE T4/L4 cost estimates (~$80/mo market-hours-only, ~$275/mo 24/7).
