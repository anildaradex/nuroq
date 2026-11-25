# Path 1 plan: open-bell pipeline v1

Status: revision 2, 2026-11-22, updated after deep research. Awaiting approval. Nothing here is built yet.

Evidence and sources: `reports/Open bell pullback plan validation.md` (notes in `research_notes/Open bell pullback plan validation/`).

## Decisions already made

| Question | Decision |
|---|---|
| Holding period | Swing: hold for days, not flattened at 15:50 ET |
| Style | A trend strategy that uses a pullback to time entry and shape risk |
| Rollout | Notify only first |
| Architecture | Sources decide *when to look*; one shared pipeline decides *what to do* |

## Open decisions (need an answer before build)

1. **Backtest data.** An honest backtest needs 8–10+ years of history that includes delisted stocks. yfinance does not provide that; Norgate and Sharadar are paid sources described as doing so. Choose a source, or accept a backtest limited to current large caps and labelled as biased.
2. **Backtest timing.** The research recommends building the backtest harness before or alongside the notify phase, because the forward log is only interpretable against a backtest expectation. This roughly doubles the work in this phase.

## What it does each trading day

1. **Overnight research** selects liquid, uptrending stocks and scores them. The AI runs only on the top ~30 that pass the rules, one stock per call.
2. **Market regime check.** No new entries while the regime gate is off.
3. **09:35 ET re-check** against live quotes: still eligible, no adverse gap, tradable spread, no blocking news.
4. **Shared gate and sizing**, then the source's mode decides the action. In Notify mode: one Telegram message and an in-app feed row per pick ("would buy N shares at $X, stop $Y, target $Z").
5. **Forward log.** Every pick, every skipped pick and the full eligible set are recorded daily, with the fields listed under "Forward log".

## Strategy v1 rules

All numbers are starting values. Most are judgment calls, not sourced; the report lists which.

### Market regime gate

- On when the 5-day average of SPY is above 1.01 × its 200-day average.
- Off when it is below 0.99 ×. In between, keep the previous state.
- Expected benefit is smaller drawdowns, not higher return. Breadth and VIX are logged, not used as gates.

### Eligibility (all must be true)

- Price over $5 (unadjusted, as of the date).
- 20-day median dollar volume over $20M.
- Close above the 50-day average; 50-day above the 200-day.
- 200-day average higher than it was 22 trading days ago.
- Within 25% of the 52-week high.
- No earnings in the next 10 trading days, and did not report after the prior close. Excluded names' forward returns are logged.

### Score (0–100)

| Component | Points | Specification |
|---|---|---|
| Relative strength | 25 | Average percentile rank, within the eligible set, of 126-day and 252-day return, each skipping the last 21 days |
| Nearness to 52-week high | 15 | Price ÷ 52-week high, scored continuously |
| Pullback depth | 20 | 1.5–4 ATR below the 20-day high |
| Reversal confirmation | 10 | Close above the prior day's high, or a close in the top third of the day's range |
| Sector relative strength | 10 | Sector ETF outperforming SPY over 6 months |
| Near 20/50-day average | 5 | Tie-breaker only |
| Volume behaviour | 5 | Full points if down-day volume in the pullback is below the 50-day average; zero if any down day exceeds about 2× average |
| AI conviction | 10 | Derived from event flags, see below |

Ratings: BUY / WATCH / AVOID from one shared threshold constant. No SELL rating; exits are a separate policy.

### AI role

- **Event flags, not opinions.** For each stock the AI returns flags from a closed list of event types (for example accounting probe, guidance cut, CEO exit, positive catalyst), each with an event date and at least one source URL.
- **Veto.** A pick is vetoed (rated AVOID) only if an adverse event is dated within 10 days, is confirmed by two independent domains, a primary source or a matching `news_cache` block, and at least 2 of 3 runs agree on the event type. Unconfirmed flags are shown and logged, not applied.
- **Points.** Confirmed positive catalyst and no adverse event = 10; nothing found = 5; unconfirmed adverse flag = 0. Fixed 5 when the model is unavailable.
- **Roles by model.** The local model classifies cached headlines; the search-grounded model confirms. Model, version and prompt version are logged per pick.
- **Input hygiene.** Unicode normalisation before ticker matching, enum-constrained JSON output, retrieved web text treated as data.
- **Never backtested.** The backtest holds the AI term constant; the AI is judged forward-only.

### 09:35 ET entry checks

- Skip if `max(open, 09:35 price) − prior close` exceeds 1 ATR.
- Skip if the 09:35 price is more than 1 ATR below the prior close or below the prior day's low (provisional).
- Skip if the bid or midpoint is at or below the planned stop.
- Skip if the spread is over 0.30% of price or over 0.10 ATR, or the quote is older than 15 seconds. This skip alone gets one re-check at 10:00.
- Skip on a NEGATIVE_BLOCK headline in `news_cache`.
- Otherwise no same-day retry. Skipped picks are logged with the reason and their forward returns tracked.

### Order mechanics (used once Approve or Auto exists)

- Bracket order with a marketable limit entry at ask + min(0.10 ATR, 0.30% of price); cancel after 2 minutes and log as a skip. No market-on-open orders.
- Whole shares; `gtc` exit legs; the stop leg is a stop-market order.
- Explicit handling for a partly filled entry with no active stop, and reconciliation after every exit.
- The time stop runs app-side (a bracket has no time leg).

### Exit plan carried with each pick

- Stop: the **wider** of (pullback low − 0.25 ATR) and (entry − 2 ATR), no closer than 1.5 ATR and no further than 3 ATR. It is a disaster stop, not a source of return.
- Target: entry + 3 ATR, kept as the outer bracket leg.
- Time stop: close after 10 trading days.
- Shadow-logged alternatives: exit on the first close above the 5-day average, a 5-day exit and a 10-day exit.

### Portfolio limits

- At most 2 new entries per day, at most 2 open positions per sector.
- At most 20–25% of budget in any one position.
- Existing `risk_manager` limits still apply: 1% risk per trade, concurrency cap, daily loss limit, no margin.

## Forward log (fields needed from day one)

- Prior close, official open, 09:35 bid/ask/mid, 10:00 price, entry-day close. On the free IEX feed, backfill 09:35 from the SIP one-minute bar after 09:50.
- Reference entry price: the 09:35 ask.
- Gap in ATR units, spread, every skip with its reason.
- 1-, 5- and 10-day forward returns, plus the simulated bracket outcome: exit date, exit reason, R multiple, maximum adverse and favourable excursion. Assume the stop fills first when stop and target share a bar; fill gaps at the open.
- The full eligible set with scores, daily.
- Score and rating with and without the AI term; vetoed names' forward returns.
- Confirmed and unconfirmed reversal candidates under identical exits.

Benchmarks: BUY picks against the same-day equal-weight eligible set, with a random-entry placebo percentile. SPY is reported beta-adjusted. Costs: 10 bps per side with a 20 bps stress case, replaced by measured half-spread plus 2 bps after 8 weeks.

## Build

### 1. Shared pipeline (new `pipeline/` package)

| Module | Job |
|---|---|
| `features.py` | One place that computes indicators from daily bars: moving averages, 52-week high, ATR, return ranks, sector strength, volume behaviour, earnings date |
| `strategies/trend_pullback.py` | `eligible(f)` → pass/fail with reasons; `score(f)` → score with per-component breakdown |
| `regime.py` | Buffered SPY 200-day gate with state |
| `ai_flags.py` | Event-flag prompt, confirmation rule, flag-to-points mapping |
| `gate.py` | `risk_manager` rules plus held, wash-sale, news block, daily, sector and notional caps |
| `execute.py` | Mode dispatch (Off / Notify / Approve / Auto), sizing, entry checks, feed logging |
| `tracker.py` | Forward log and its daily update job |

### 2. Research cycle v2 (`dashboard.run_research_cycle`)

- Candidate filter: the eligibility rules, ranked by score. Replaces "up over 20 days, sorted by volume".
- Rules and score first; AI only on the eligible top ~30.
- `watchlist_today` gains the score breakdown and the exit plan.
- Daily history fetch extended from ~140 to ~400 calendar days (`data_fetcher.py:780`).

### 3. Configuration

- New `open_bell_mode` in `agent_config`: Off / Notify / Approve / Auto. Default **Notify**. New row in the Configuration panel.
- Positions tagged with the source that opened them; the 15:50 flattener closes only intraday-source positions. Must ship before Approve or Auto is selectable.

### 4. Retire the old Path 1

- Remove session-open "Refresh-BUY" alerts and auto-buys (`live_agent.py:292`).
- Keep one morning digest, built from the new watchlist.

### 5. Backtest harness (timing and data: see open decisions)

- 8–10+ years of survivorship-free daily data covering Q4 2018, 2020 and 2022; earnings dates as known on the decision day.
- Tune at most 2–3 parameters; at most 20–30 configurations, every one logged.
- Walk-forward with a 10-day purge; final 12–18 months untouched.
- Sweeps to run: stop width (1.5 / 2 / 2.5 / 3 ATR and none), current versus proposed score weights, 15% / 20% / 25% from the 52-week high.
- Backtest expectations are written down before the forward test starts.

### 6. Tests

- Strategy rules on synthetic series: uptrend pullback passes; falling knife, extended stock, earnings within 10 days and regime-off day fail.
- Regime gate hysteresis; wider-stop rule with floor and cap; notional cap.
- AI veto applies only when confirmed; points mapping; model-unavailable default.
- Entry skips (gap up, gap down, spread, stale quote); Notify mode never places an order.
- Tracker fields, flattener source filter, history extension.

## Promotion ladder

Eight to twelve weeks of picks cannot show an edge: detecting a 0.5% per-trade edge needs roughly 785 independent trades (the researchers' arithmetic on assumed inputs). The forward phase tests plumbing and consistency; evidence of edge comes from the backtest.

| Step | Gate |
|---|---|
| Notify → Approve | At least 12 weeks, 55 completed runs, 40 BUY picks with 10-day outcomes, one earnings season. Logging complete; live picks replay through the offline pipeline with ≥ 95% match; measured median half-spread ≤ 10 bps. Hand review of the worst pick and week, every skip, every veto and the five best picks |
| Approve → Auto at 0.25% risk | At least 30 approved trades; mean entry slippage ≤ 5 bps; no unprotected positions or orphan orders |
| 0.25% → 0.5% → 1% risk | Backtest clears out-of-sample t ≥ 3.0 or Deflated Sharpe Ratio ≥ 0.95, with at least 500 out-of-sample trades, positive at 20 bps per side, and beats the random-entry placebo at the 95th percentile. Pooled forward picks (≥ 100, then ≥ 150) not more than 2 standard errors below the backtest expectation |
| Demotion | Soft at −6R from peak, hard at −10R, until the backtest gives a drawdown distribution. No consecutive-loss trigger below 10 |
| AI weight | Set to 0 if, after 60+ picks, the with-AI ranking is worse than rules-only or vetoed names do not underperform kept picks |

## Not in this phase

- Live rescoring (Path 2), big-move re-analysis (Path 3) and the day-trader keep their current logic. Path 2 will watch the new watchlist.
- The scanner moves to the new strategy in a follow-up.
- Multi-agent debate, reflection loops, trailing exits and a separate volatility-targeting layer: considered and not adopted.

## Effort

- Pipeline, research cycle, configuration, forward log and tests: about 3.5 days.
- Backtest harness: about 2–3 days more, plus the data source.

## What changed from revision 1

| Area | Revision 1 | Revision 2 |
|---|---|---|
| Score weights | 65 points on pullback shape, 20 on 3-month relative strength | 40 on relative strength and nearness to the high, 35 on pullback shape, 10 on sector strength |
| Stop | Tighter of two levels, 1 ATR floor | Wider of two levels, 1.5 ATR floor, 3 ATR cap |
| Regime gate | SPY close above 200-day | 5-day average with a ±1% band |
| Earnings | No entry within 5 days | No entry within 10 days, or after a report the prior evening |
| AI | 0–10 free score plus free-text veto | Cited event flags; veto only when confirmed; points mapped from flags |
| Entry | Market order, last-trade reference | Capped limit order, ask reference, spread and gap-down checks |
| Limits | 3 entries per day | 2 entries per day, 20–25% position cap |
| Promotion | 8 weeks, 60 picks, beat SPY | Plumbing gate, then a backtest-based edge gate and staged size |
| Backtest | 1–2 years, ~10 tuned values, after notify | 8–10+ years, 2–3 tuned values, before or alongside notify |

Corrections to revision 1's evidence table: StockBench's results show 8 of 14 models finishing within about 2 points above equal-weight buy-and-hold before costs, not "most fail to beat"; and the 57%-to-21% drawdown figure attributed to StockCharts could not be confirmed.
