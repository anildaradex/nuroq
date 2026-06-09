"""
live_agent.py — Tier 3 reactive agent (Phase 3 of ARCHITECTURE.md rebuild).

Subscribes to today's watchlist + currently-held positions via Alpaca's
WebSocket, recomputes the quant score on every minute bar using cached
state (fundamentals + AI score from overnight cycle), and fires Telegram
approvals only on THRESHOLD CROSSINGS — not every bar above threshold.

Hot-path budget: <100ms per bar. Achieved by:
  • No Polygon REST calls (cached daily OHLCV)
  • No yfinance calls (cached fundamentals)
  • No LLM calls (cached AI score from overnight)
  • Pure deterministic math on top of pre-cached state

LiveAgent is started from dashboard.AgentLoop during market hours.
Off-hours behavior is governed by `is_market_hours()` + the env var
`NUROQ_FORCE_LIVE=1` for testing.
"""

from __future__ import annotations

import os
import time
import sqlite3
import threading
import requests
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional, Callable, Dict, List

from data_fetcher import (
    history_cache, fundamentals_cache, ai_score_cache,
    watchlist_today, live_triggers,
)
from scoring import calculate_technicals, get_weekly_confluence, calculate_quant_score, calculate_sizing


# ─── Constants ────────────────────────────────────────────────────────────────

BUY_CROSSING_THRESHOLD  = 65   # score must cross UP through this to fire BUY
SELL_CROSSING_THRESHOLD = 30   # score must cross DOWN through this for SELL exit
EARNINGS_RISK_BOOST     = 10   # raise BUY threshold by this much if earnings risk
DEFAULT_DAILY_BUY_CAP   = 5    # max BUY approvals fired per market session
INTRADAY_BAR_HISTORY_LEN = 60  # rolling minute bars kept per ticker for volume/H/L

# Score-shift detector — LOG-ONLY (intentionally NO Telegram). Records a
# SCORE_SHIFT_UP/DOWN row in live_triggers when a ticker's live score moves
# this many points from its session-open baseline. These rows surface in the
# in-app Recent Activity feed for ambient awareness, but never fire a Telegram
# ping (the user found momentum pings too noisy). One row per direction per
# ticker per session.
SCORE_SHIFT_DELTA       = 10

# Phase 3b: anti-noise / anti-churn
DEFAULT_HYSTERESIS_BARS        = 2     # score must stay above threshold for N bars
DEFAULT_PER_TICKER_COOLDOWN_S  = 1800  # 30 min lockout per ticker after a fire

# US Equities regular session in ET. Pre/after market intentionally excluded —
# scoring is calibrated for regular-session bars.
MARKET_OPEN_ET  = dtime(9, 30)
MARKET_CLOSE_ET = dtime(16, 0)


# ─── Market hours helpers ─────────────────────────────────────────────────────

def is_market_hours(now: Optional[datetime] = None) -> bool:
    """
    Returns True if the US equities regular session is open right now.
    Uses local-time naively — sufficient if the host is set to ET, or the
    user runs with NUROQ_FORCE_LIVE=1 for testing.
    """
    n = now or datetime.now()
    if n.weekday() >= 5:   # Sat=5, Sun=6
        return False
    t = n.time()
    return MARKET_OPEN_ET <= t <= MARKET_CLOSE_ET


# ─── Per-ticker live state ────────────────────────────────────────────────────

@dataclass
class TickerState:
    """All mutable state the live agent tracks per ticker, in-memory."""
    ticker:               str
    baseline_bars:        list                          # cached daily OHLCV (yesterday and earlier)
    weekly_trend:         str                           # precomputed at watchlist load time
    intraday_bars:        deque = field(default_factory=lambda: deque(maxlen=INTRADAY_BAR_HISTORY_LEN))
    today_high:           Optional[float] = None
    today_low:            Optional[float] = None
    today_volume:         float = 0.0
    last_price:           Optional[float] = None
    last_score:           Optional[int] = None          # last recomputed final_score
    last_bar_ts:          Optional[float] = None        # epoch seconds of latest bar
    last_trigger_ts:      Optional[float] = None        # last time we fired an approval
    is_held_position:     bool = False                  # for SELL crossing eligibility
    # Phase 3b: hysteresis counter — bars consecutively above BUY threshold
    bars_above_buy:       int = 0
    bars_below_sell:      int = 0
    # Score-shift detector (LOG-ONLY → feed, no Telegram). baseline_score is
    # locked on the first bar of the session; shift_fired_* dedup to one feed
    # entry per direction per session so an oscillating ticker doesn't flood
    # the Recent Activity feed.
    baseline_score:       Optional[int] = None
    shift_fired_up:       bool = False
    shift_fired_down:     bool = False


# ─── LiveAgent ────────────────────────────────────────────────────────────────

class LiveAgent:
    """
    The Tier 3 reactive engine. One instance per dashboard process.

    `_fire_buy` and `_fire_sell` are dependency-injected callbacks (provided
    by dashboard at construction time) so this module stays import-loop-free
    and easily testable.
    """

    def __init__(
        self,
        streamer,                                          # MarketStreamer
        logger,                                            # AppLogger-like
        fire_buy_callback: Callable[[str, float, int, str], None],
        fire_sell_callback: Callable[[str, float, int, str], None],
        get_held_tickers: Callable[[], List[str]],
        daily_buy_cap: int = DEFAULT_DAILY_BUY_CAP,
        hysteresis_bars: int = DEFAULT_HYSTERESIS_BARS,
        per_ticker_cooldown_s: int = DEFAULT_PER_TICKER_COOLDOWN_S,
    ):
        self.streamer = streamer
        self.logger = logger
        self._fire_buy = fire_buy_callback
        self._fire_sell = fire_sell_callback
        self._get_held_tickers = get_held_tickers
        self.daily_buy_cap = daily_buy_cap
        # Phase 3b: noise gates
        self.hysteresis_bars = max(1, hysteresis_bars)
        self.per_ticker_cooldown_s = max(0, per_ticker_cooldown_s)

        self.state: Dict[str, TickerState] = {}
        self.is_running = False
        self._state_lock = threading.Lock()
        self.started_at: Optional[datetime] = None
        self.bars_processed = 0

    # ─── lifecycle ────────────────────────────────────────────────────────────

    def start(self, force: bool = False) -> str:
        """
        Build watchlist, initialize per-ticker state, hand subscription list
        to the streamer. Returns a status string for the UI/caller.
        """
        if self.is_running:
            return "⚠️ LiveAgent already running."

        if not force and not is_market_hours():
            return ("🛑 Market is closed — LiveAgent did not start. "
                    "Set NUROQ_FORCE_LIVE=1 to override for testing.")

        tickers = self._build_watchlist()
        if not tickers:
            return "⚠️ No tickers to watch (watchlist_today + holdings both empty)."

        self._init_state(tickers)

        # Wire ourselves into the streamer's every-bar callback path.
        self.streamer.bar_callback = self._on_bar
        # If streamer isn't running yet, start it. If already running, just update watchlist.
        if not self.streamer.is_running:
            self.streamer.set_watchlist(tickers)
            self.streamer.start()
        else:
            self.streamer.set_watchlist(tickers)

        self.is_running = True
        self.started_at = datetime.now()
        msg = f"🟢 LiveAgent started — subscribed to {len(tickers)} tickers ({sum(1 for s in self.state.values() if s.is_held_position)} held)."
        self.logger.log(msg)

        # Send the morning BUY digest to Telegram. The live agent fires
        # crossings only (transitions from below→above 65), so tickers that
        # were already BUY-rated at session start would otherwise be invisible
        # until they dip and re-cross. This one-shot digest gives the user
        # immediate visibility into today's actionable candidates.
        try:
            self._send_session_open_digest()
        except Exception as e:
            self.logger.log(f"⚠️ Session-open digest send failed: {e}", level="WARNING")

        # Per-ticker "Refresh BUY" Telegram messages — one detailed message
        # per BUY watchlist ticker, with ATR-sized SL/TP/shares. Lets the user
        # act on each individually via Quick Trade. Idempotent per-ticker per
        # day (separate sentinel from __digest__).
        try:
            self._send_refresh_buy_alerts()
        except Exception as e:
            self.logger.log(f"⚠️ Refresh-BUY alerts send failed: {e}", level="WARNING")

        return msg

    def _send_session_open_digest(self) -> None:
        """
        One-shot Telegram message at agent start listing today's BUY watchlist.
        Idempotent per-session: we mark a flag so a backend crash + restart
        doesn't spam the user with a fresh digest mid-session.
        """
        # Only send once per UTC day. live_triggers table is our durable marker.
        try:
            with sqlite3.connect(live_triggers.db_path) as conn:
                # Use a sentinel ticker '__digest__' to mark "already sent today"
                today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                row = conn.execute(
                    "SELECT 1 FROM live_triggers WHERE ticker='__digest__' AND ts > ? LIMIT 1",
                    (today_start,),
                ).fetchone()
                if row:
                    self.logger.log("  Session-open digest already sent today — skipping.")
                    return
        except Exception:
            pass  # If the check fails, send anyway — duplicate is better than missed.

        rows = watchlist_today.get_all()
        buys = [r for r in rows if r["recommendation"] == "BUY"]
        if not buys:
            return

        lines = [f"🎯 *NuroQ — Today's BUY watchlist* ({len(buys)})"]
        lines.append("_From this morning's research cycle. LiveAgent will ping again only on threshold crossings during the session._\n")
        lines.append("```")
        lines.append(f"{'TICK':<6} {'Q':>3} {'AI':>3}  {'Price':>8}  Δ%")
        for r in buys[:20]:
            chg = float(r.get("change_pct") or 0)
            chg_str = f"{'+' if chg >= 0 else ''}{chg:.1f}%"
            lines.append(f"{r['ticker']:<6} {r.get('quant_score') or 0:>3} "
                         f"{r.get('ai_score') or 0:>3}  ${float(r['price']):>7.2f}  {chg_str}")
        lines.append("```")
        msg_text = "\n".join(lines)

        # Use the fire_buy_callback's underlying Telegram channel — but those
        # are tied to specific orders. Cleaner: hit the Telegram HTTP API
        # directly. The token + chat live in .env.
        token = os.getenv("TELEGRAM_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat:
            self.logger.log("  TELEGRAM_TOKEN/CHAT_ID missing — digest not sent.", level="WARNING")
            return

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg_text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if r.status_code == 200:
                self.logger.log(f"  📱 Telegram digest sent ({len(buys)} BUYs).")
                # Mark sent for today
                try:
                    with sqlite3.connect(live_triggers.db_path) as conn:
                        conn.execute(
                            "INSERT INTO live_triggers (ts, ticker, direction, "
                            "score_before, score_after, price, action, notes) "
                            "VALUES (?, '__digest__', 'INFO', NULL, NULL, NULL, "
                            "'SESSION_OPEN_DIGEST', ?)",
                            (datetime.now().timestamp(), f"sent {len(buys)} BUYs"),
                        )
                except Exception as e:
                    self.logger.log(f"  ⚠️ Digest sentinel write failed: {e}", level="WARNING")
            else:
                self.logger.log(f"  ⚠️ Telegram digest send failed: HTTP {r.status_code}", level="WARNING")
        except Exception as e:
            self.logger.log(f"  ⚠️ Telegram digest exception: {e}", level="WARNING")

    def _send_refresh_buy_alerts(self) -> None:
        """
        One Telegram message per BUY watchlist ticker at session start, with
        ATR-sized SL/TP/shares. Lets the user act on each BUY individually
        even though it's not a fresh crossing.

        Idempotent per ticker per day via the live_triggers table — a backend
        restart mid-session won't re-spam these.

        Cost: ~8 Telegram messages on a typical morning (one per BUY).
        Skipped if the agent's idempotency sentinel for this ticker already
        exists for today.

        Capped at REFRESH_BUY_MAX_TICKERS (default 12) to prevent a runaway
        scenario where the research cycle produces 50+ BUYs.
        """
        token = os.getenv("TELEGRAM_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat:
            self.logger.log("  TELEGRAM_TOKEN/CHAT_ID missing — refresh-BUY alerts skipped.",
                            level="WARNING")
            return

        rows = watchlist_today.get_all()
        buys = [r for r in rows if r["recommendation"] == "BUY"]
        if not buys:
            return

        cap = int(os.getenv("REFRESH_BUY_MAX_TICKERS", "12"))
        buys = buys[:cap]

        # Account equity for sizing — defer to dashboard's _live_equity helper
        # so we use the same sizing math as Quick Trade / live agent fires.
        try:
            from dashboard import _live_equity
            account_equity = _live_equity()
        except Exception:
            account_equity = 100_000.0  # safe fallback

        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        sent = 0
        skipped = 0
        for r in buys:
            ticker = r["ticker"].upper()

            # Per-ticker idempotency check
            try:
                with sqlite3.connect(live_triggers.db_path) as conn:
                    existing = conn.execute(
                        "SELECT 1 FROM live_triggers WHERE ticker=? AND action=? "
                        "AND ts > ? LIMIT 1",
                        (ticker, "REFRESH_BUY", today_start),
                    ).fetchone()
                    if existing:
                        skipped += 1
                        continue
            except Exception:
                pass  # better to dup than miss

            # Compute sizing
            bars = history_cache.get(ticker, allow_stale=True) or []
            price = float(r.get("price") or 0)
            if bars:
                last_close = float(bars[-1].get("c") or price)
                if last_close > 0:
                    price = last_close
            if price <= 0:
                continue

            techs = calculate_technicals(bars) if bars else {}
            atr = float((techs or {}).get("atr") or max(price * 0.02, 0.5))
            try:
                sizing = calculate_sizing(price, atr=atr, account=account_equity)
            except Exception:
                continue

            shares = int(sizing.get("shares", 0))
            sl = float(sizing.get("sl", 0))
            tp = float(sizing.get("tp", 0))
            if shares < 1:
                continue

            cost = shares * price
            risk = (price - sl) * shares if sl > 0 and sl < price else 0
            reward = (tp - price) * shares if tp > price else 0
            rr = (reward / risk) if risk > 0 else 0

            chg = float(r.get("change_pct") or 0)
            chg_str = f"{'+' if chg >= 0 else ''}{chg:.2f}%"
            qs = r.get("quant_score") or 0
            ai = r.get("ai_score") or 0

            msg_text = (
                f"🟢 *BUY READY · {ticker}*\n\n"
                f"`{ticker}` — Quant *{qs}* · AI *{ai}* · Rating *BUY*\n"
                f"Price *${price:.2f}* ({chg_str} intraday)\n\n"
                f"📋 *Suggested order* (ATR-sized, MARKET bracket)\n"
                f"Shares: *{shares:,}*    Cost: *${cost:,.0f}*\n"
                f"SL: *${sl:.2f}*    TP: *${tp:.2f}*    ATR: *${atr:.2f}*\n"
                f"Risk: *-${risk:,.0f}*    Reward: *+${reward:,.0f}*    "
                f"R\\:R *1:{rr:.1f}*\n\n"
                f"_Tap ✅ EXECUTE to submit this exact bracket order. "
                f"Want different size or LIMIT entry? Open NuroQ → Watchlist → ⚡ instead._"
            )

            # Inline keyboard: EXECUTE submits the MARKET bracket immediately;
            # Dismiss just closes the prompt. Callback data format
            # `REFEX:TICKER:SHARES:SL:TP` parsed by TradeGatekeeper.handle_callback.
            # Telegram caps callback_data at 64 bytes — our format stays well under.
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "✅ EXECUTE", "callback_data": f"REFEX:{ticker}:{shares}:{sl:.2f}:{tp:.2f}"},
                    {"text": "⏭️ Dismiss",  "callback_data": f"REFDISMISS:{ticker}"},
                ]],
            }

            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat, "text": msg_text, "parse_mode": "Markdown",
                        "reply_markup": reply_markup,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    sent += 1
                    # Write idempotency sentinel
                    try:
                        with sqlite3.connect(live_triggers.db_path) as conn:
                            conn.execute(
                                "INSERT INTO live_triggers (ts, ticker, direction, "
                                "score_before, score_after, price, action, notes) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (datetime.now().timestamp(), ticker, "BUY",
                                 None, None, price, "REFRESH_BUY",
                                 f"{shares} sh @ ${price:.2f}, SL ${sl}, TP ${tp}"),
                            )
                    except Exception as e:
                        self.logger.log(f"  ⚠️ Refresh-BUY sentinel write failed for {ticker}: {e}",
                                        level="WARNING")
                else:
                    self.logger.log(f"  ⚠️ Refresh-BUY send failed for {ticker}: HTTP {resp.status_code}",
                                    level="WARNING")
            except Exception as e:
                self.logger.log(f"  ⚠️ Refresh-BUY exception for {ticker}: {e}", level="WARNING")

        self.logger.log(f"  📱 Refresh-BUY alerts: {sent} sent, {skipped} skipped (already sent today).")

    def stop(self) -> str:
        if not self.is_running:
            return "LiveAgent already stopped."
        # Detach the bar callback so the streamer won't try to score after we leave.
        self.streamer.bar_callback = None
        self.is_running = False
        msg = f"🔴 LiveAgent stopped. Processed {self.bars_processed} bars this session."
        self.logger.log(msg)
        return msg

    # ─── watchlist construction ───────────────────────────────────────────────

    def _build_watchlist(self) -> List[str]:
        """watchlist_today ∪ currently-held positions. Falls back to [] if both empty."""
        wl = list(watchlist_today.get_tickers() or [])
        try:
            held = [t.upper() for t in (self._get_held_tickers() or [])]
        except Exception as e:
            self.logger.log(f"⚠️ LiveAgent: get_held_tickers failed: {e}", level="WARNING")
            held = []

        combined = []
        seen = set()
        for t in wl + held:
            if t and t not in seen:
                combined.append(t)
                seen.add(t)
        return combined

    def _init_state(self, tickers: List[str]) -> None:
        """Pre-load each ticker's cached daily bars + weekly trend into memory."""
        held_set = set()
        try:
            held_set = {t.upper() for t in (self._get_held_tickers() or [])}
        except Exception:
            pass

        with self._state_lock:
            self.state.clear()
            for ticker in tickers:
                bars = history_cache.get(ticker, allow_stale=True) or []
                w_trend = get_weekly_confluence(bars) if bars else "UNKNOWN"
                self.state[ticker] = TickerState(
                    ticker=ticker,
                    baseline_bars=bars,
                    weekly_trend=w_trend,
                    is_held_position=(ticker in held_set),
                )

    # ─── hot path: per-bar evaluation ─────────────────────────────────────────

    def _on_bar(self, bar) -> None:
        """
        Called inline on the WebSocket loop for every minute bar.
        MUST stay fast — target <100ms. No I/O beyond SQLite cache reads.
        """
        try:
            ticker = bar.symbol
            state = self.state.get(ticker)
            if state is None:
                return  # bar for a ticker we don't watch (shouldn't happen)

            self.bars_processed += 1
            self._update_intraday(state, bar)
            new_score = self._recompute_score(state)
            if new_score is None:
                return

            # Lock baseline on first bar; thereafter log score shifts to the
            # in-app feed (no Telegram).
            if state.baseline_score is None:
                state.baseline_score = new_score
            else:
                self._check_score_shift(state, new_score)

            self._check_crossings(state, new_score)
            state.last_score = new_score
        except Exception as e:
            self.logger.log(f"⚠️ LiveAgent._on_bar [{getattr(bar, 'symbol', '?')}] failed: {e}",
                            level="WARNING")

    def _update_intraday(self, state: TickerState, bar) -> None:
        """Updates rolling intraday H/L/V state from the new bar."""
        price = float(bar.close)
        vol = float(getattr(bar, "volume", 0) or 0)
        high = float(getattr(bar, "high", price) or price)
        low = float(getattr(bar, "low", price) or price)

        state.intraday_bars.append({"c": price, "h": high, "l": low, "v": vol})
        state.last_price = price
        state.last_bar_ts = datetime.now().timestamp()
        state.today_high = high if state.today_high is None else max(state.today_high, high)
        state.today_low = low if state.today_low is None else min(state.today_low, low)
        state.today_volume += vol

    def _recompute_score(self, state: TickerState) -> Optional[int]:
        """
        Synthesizes today's bar from intraday state, recomputes technicals on
        baseline_bars + synthetic today, pulls cached fundamentals + AI score,
        returns the final quant_score. None on insufficient data.
        """
        if not state.baseline_bars or state.last_price is None:
            return None

        # Synthesize today's daily bar from intraday rolling state.
        today_bar = {
            "o": state.baseline_bars[-1]["c"],   # use yesterday close as today open proxy
            "h": state.today_high or state.last_price,
            "l": state.today_low or state.last_price,
            "c": state.last_price,
            "v": state.today_volume or state.baseline_bars[-1].get("v", 0),
            "t": datetime.now().strftime("%Y-%m-%d"),
        }
        live_history = state.baseline_bars + [today_bar]
        techs = calculate_technicals(live_history)
        if not techs:
            return None

        funds = fundamentals_cache.get(state.ticker) or {}
        cached_ai = ai_score_cache.get(state.ticker) or {}
        ai_score = int(cached_ai.get("score", 50))

        # Conservative defaults: assume no earnings risk + neutral sentiment in hot path.
        # The overnight cycle's AI score already absorbed the most-recent context;
        # news-shock invalidation is Phase 4 work.
        final_score = calculate_quant_score(
            techs, funds,
            w_trend=state.weekly_trend,
            e_risk={"risk": False, "days": 30},
            st_sent="NEUTRAL",
            ai_score=ai_score,
        )
        return int(final_score)

    # ─── crossing detection + approval dispatch ───────────────────────────────

    def _check_crossings(self, state: TickerState, new_score: int) -> None:
        """
        Phase 3a + 3b: detects threshold crossings, applies hysteresis (require N
        consecutive bars above threshold) and per-ticker cooldown (block
        re-firing within N seconds), and routes to BUY/SELL handlers.
        """
        prev = state.last_score
        now = time.time()

        # ─── BUY side: hysteresis counter + crossing fire ────────────────────
        if new_score >= BUY_CROSSING_THRESHOLD:
            state.bars_above_buy += 1
        else:
            state.bars_above_buy = 0

        # Fire only when:
        #   (1) crossing just occurred (prev below, new at/above)
        #   (2) sustained for hysteresis_bars consecutive bars
        #   (3) NOT in per-ticker cooldown from a prior fire
        if (prev is not None
            and prev < BUY_CROSSING_THRESHOLD <= new_score
            and state.bars_above_buy >= self.hysteresis_bars
            and self._cooldown_ok(state, now)):
            self._handle_buy_crossing(state, prev, new_score)

        # ─── SELL side: same shape, mirror logic, only for held positions ────
        if new_score <= SELL_CROSSING_THRESHOLD:
            state.bars_below_sell += 1
        else:
            state.bars_below_sell = 0

        if (state.is_held_position
            and prev is not None
            and prev > SELL_CROSSING_THRESHOLD >= new_score
            and state.bars_below_sell >= self.hysteresis_bars
            and self._cooldown_ok(state, now)):
            self._handle_sell_crossing(state, prev, new_score)

    def _cooldown_ok(self, state: TickerState, now: float) -> bool:
        """True if enough time has passed since the last fire on this ticker."""
        if not state.last_trigger_ts or self.per_ticker_cooldown_s <= 0:
            return True
        elapsed = now - state.last_trigger_ts
        if elapsed < self.per_ticker_cooldown_s:
            return False
        return True

    def _check_score_shift(self, state: TickerState, new_score: int) -> None:
        """
        LOG-ONLY score-shift detector. Writes a SCORE_SHIFT_UP / SCORE_SHIFT_DOWN
        row to live_triggers when the live score has drifted SCORE_SHIFT_DELTA
        points from the session-open baseline. Surfaces in the in-app Recent
        Activity feed for ambient awareness. Deliberately NO Telegram (momentum
        pings were too noisy). One row per direction per ticker per session.
        """
        baseline = state.baseline_score
        if baseline is None:
            return
        delta = new_score - baseline
        if abs(delta) < SCORE_SHIFT_DELTA:
            return
        direction = "UP" if delta > 0 else "DOWN"
        if direction == "UP" and state.shift_fired_up:
            return
        if direction == "DOWN" and state.shift_fired_down:
            return

        action_tag = "SCORE_SHIFT_UP" if direction == "UP" else "SCORE_SHIFT_DOWN"
        try:
            live_triggers.log(
                state.ticker, direction, baseline, new_score,
                state.last_price or 0,
                action=action_tag,
                notes=f"score {baseline}→{new_score} ({'+' if delta >= 0 else ''}{delta}) — feed only",
            )
            self.logger.log(
                f"📊 Score shift {state.ticker}: {baseline}→{new_score} "
                f"({direction}) — logged to feed (no Telegram)."
            )
        except Exception as e:
            self.logger.log(f"⚠️ Score-shift log failed for {state.ticker}: {e}", level="WARNING")

        if direction == "UP":
            state.shift_fired_up = True
        else:
            state.shift_fired_down = True

    def _handle_buy_crossing(self, state: TickerState, prev: int, new: int) -> None:
        ticker = state.ticker
        # Daily cap: count today's FIRED BUY rows.
        fired_today = live_triggers.count_today("FIRED_BUY")
        if fired_today >= self.daily_buy_cap:
            live_triggers.log(
                ticker, "BUY", prev, new, state.last_price or 0,
                action="SUPPRESSED_CAP",
                notes=f"Daily BUY approval cap ({self.daily_buy_cap}) already reached.",
            )
            self.logger.log(
                f"🛑 LiveAgent: BUY crossing for {ticker} suppressed — daily cap reached "
                f"({fired_today}/{self.daily_buy_cap})", level="WARNING"
            )
            return

        # Skip if already held (BUY makes no sense on a position we own).
        if state.is_held_position:
            live_triggers.log(
                ticker, "BUY", prev, new, state.last_price or 0,
                action="SUPPRESSED_HELD",
                notes="Already held — no duplicate BUY.",
            )
            return

        # Wash-sale guard (IRS Section 1091 — Layer 1). If we sold this ticker
        # at a loss within the last 30 days, the loss would be disallowed.
        # Suppress automatic re-entry. User can still manually override via the
        # Telegram approval message's override button (which routes through
        # handle_quick_trade with wash_sale_override=True).
        try:
            from dashboard import wash_sale_check
            ws = wash_sale_check(ticker)
            if ws["risk"]:
                live_triggers.log(
                    ticker, "BUY", prev, new, state.last_price or 0,
                    action="SUPPRESSED_WASH_SALE",
                    notes=ws["hint"][:400],
                )
                self.logger.log(
                    f"🛑 LiveAgent: BUY crossing for {ticker} suppressed — wash-sale risk: "
                    f"{ws['hint'][:200]}", level="WARNING",
                )
                # Send a one-shot informational Telegram so the user knows the
                # agent saw the signal but held back. They can manually override
                # via Watchlist ⚡ if they want to.
                try:
                    token = os.getenv("TELEGRAM_TOKEN")
                    chat = os.getenv("TELEGRAM_CHAT_ID")
                    if token and chat:
                        requests.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={
                                "chat_id": chat,
                                "text": (f"🛑 *Wash-sale block · {ticker}*\n\n"
                                         f"Live agent detected a BUY crossing "
                                         f"({prev}→{new}) but suppressed it.\n\n"
                                         f"{ws['hint']}\n\n"
                                         f"_Override via NuroQ → Watchlist → ⚡ "
                                         f"with wash-sale acknowledgment if you want "
                                         f"the entry anyway._"),
                                "parse_mode": "Markdown",
                            },
                            timeout=5,
                        )
                except Exception:
                    pass
                return
        except Exception as e:
            self.logger.log(f"⚠️ Wash-sale check for {ticker} failed (failing open): {e}",
                            level="WARNING")

        # Phase 4: News final-check. Read from news_cache (no API call — hot
        # path safe). NEGATIVE_BLOCK suppresses entirely; WARNING/BOOST
        # decorate the reasoning string; NEUTRAL fires normally.
        news_tag = ""
        try:
            from news_engine import check_news_for_crossing
            news = check_news_for_crossing(ticker)
        except Exception as e:
            self.logger.log(f"⚠️ News check for {ticker} failed: {e}", level="WARNING")
            news = None

        if news and news["classification"] == "NEGATIVE_BLOCK":
            live_triggers.log(
                ticker, "BUY", prev, new, state.last_price or 0,
                action="SUPPRESSED_NEWS",
                notes=f"BLOCK: {news['headline'][:200]}",
            )
            self.logger.log(
                f"🛑 LiveAgent: BUY crossing for {ticker} suppressed by news "
                f"(NEGATIVE_BLOCK): {news['headline'][:120]}", level="WARNING"
            )
            return
        elif news and news["classification"] == "NEGATIVE_WARNING":
            news_tag = f"\n⚠️ Recent negative news: {news['headline'][:200]}"
        elif news and news["classification"] == "POSITIVE_BOOST":
            news_tag = f"\n📈 Catalyst: {news['headline'][:200]}"

        # Build a concise reasoning string locally (no LLM in hot path).
        reasoning = (
            f"LIVE crossing: {ticker} score {prev} → {new} (≥ {BUY_CROSSING_THRESHOLD}). "
            f"Price ${state.last_price:.2f}. Cached AI={(ai_score_cache.get(ticker) or {}).get('score', '?')}. "
            f"Weekly trend: {state.weekly_trend}.{news_tag}"
        )

        live_triggers.log(
            ticker, "BUY", prev, new, state.last_price or 0,
            action="FIRED_BUY", notes=reasoning,
        )
        state.last_trigger_ts = datetime.now().timestamp()

        # ─── Autonomy branch ──────────────────────────────────────────────
        # If auto-trade is enabled AND the risk manager green-lights the
        # entry, submit a bracket order directly — no Telegram approval.
        # Otherwise fall through to the existing approval flow.
        if self._try_auto_trade(ticker, state.last_price or 0, reasoning):
            self.logger.log(f"🤖 LiveAgent: AUTO BUY {ticker} fired (no approval).")
            return

        try:
            self._fire_buy(ticker, state.last_price or 0, new, reasoning)
            self.logger.log(f"🎯 LiveAgent: BUY crossing {ticker} {prev}→{new} fired.")
        except Exception as e:
            self.logger.log(f"⚠️ LiveAgent: BUY fire callback failed for {ticker}: {e}",
                            level="ERROR")

    def _try_auto_trade(self, ticker: str, price: float, reasoning: str) -> bool:
        """If AUTO mode + risk manager allow, place a bracket BUY immediately.
        Returns True if a trade was attempted (success or failure), False if
        we should fall through to the human-approval path. The risk manager
        is the only authority — this function trusts it."""
        try:
            import agent_config
            cfg = agent_config.get()
            if not cfg.get("auto_trade_enabled") or cfg.get("halted_at"):
                return False
        except Exception:
            return False

        try:
            import risk_manager
            from scoring import calculate_technicals
            from history_cache import history_cache
            from alpaca_executor import alpaca_api as ae

            # Need ATR for sizing (same source the existing approval flow uses).
            bars = history_cache.get(ticker, allow_stale=True) or []
            techs = calculate_technicals(bars) if bars else {}
            atr = float((techs or {}).get("atr") or max(price * 0.02, 0.5))

            # Live account state for the gatekeeper.
            acct = ae.get_account_summary() or {}
            positions = ae.list_positions() or []
            decision = risk_manager.can_enter_trade(
                symbol=ticker, entry=price, atr=atr,
                open_positions=len(positions),
                cash=float(acct.get("cash") or 0),
                todays_pl=float(acct.get("todays_pl") or 0),
                equity=float(acct.get("equity") or 0),
                on_margin=float(acct.get("cash") or 0) < 0,
            )
            if not decision.ok:
                self.logger.log(
                    f"🤖 AUTO declined {ticker}: {decision.reason}", level="INFO"
                )
                # We DID see it; logging the suppression but NOT falling through
                # to the approval flow either (auto mode means: this is the only
                # path). Returning True signals "handled, don't fall through".
                live_triggers.log(
                    ticker, "BUY", 0, 0, price,
                    action="AUTO_DECLINED",
                    notes=f"risk_manager: {decision.reason}",
                )
                return True

            s = decision.sizing
            # Submit a bracket BUY. The existing submit_bracket_order handles
            # the OCO SELL legs at SL/TP. No Telegram approval, no extra checks.
            res = ae.submit_bracket_order(
                ticker=ticker, action="BUY", shares=s.shares,
                sl=s.sl, tp=s.tp, limit_price=None,
            )
            live_triggers.log(
                ticker, "BUY", 0, 0, price,
                action="AUTO_EXECUTED",
                notes=(f"shares={s.shares} entry≈${price:.2f} "
                       f"SL=${s.sl:.2f} TP=${s.tp:.2f} risk=${s.risk_dollars:.0f} | {res[:200]}"),
            )
            # Audit Telegram. Notification-only, no buttons.
            if cfg.get("notify_on_trade"):
                try:
                    from dashboard import gatekeeper
                    gatekeeper.send_notification(
                        f"🤖 AUTO BUY {ticker} · {s.shares} @ ${price:.2f}\n"
                        f"SL ${s.sl:.2f} · TP ${s.tp:.2f} · risk ${s.risk_dollars:.0f}\n"
                        f"{reasoning[:200]}"
                    )
                except Exception:
                    pass
        except Exception as e:
            self.logger.log(f"⚠️ AUTO trade attempt for {ticker} failed: {e}",
                            level="ERROR")
            live_triggers.log(
                ticker, "BUY", 0, 0, price,
                action="AUTO_ERROR", notes=str(e)[:300],
            )
        return True

    def _handle_sell_crossing(self, state: TickerState, prev: int, new: int) -> None:
        ticker = state.ticker
        reasoning = (
            f"LIVE crossing: {ticker} score {prev} → {new} (≤ {SELL_CROSSING_THRESHOLD}). "
            f"Held position exit signal. Price ${state.last_price:.2f}."
        )
        live_triggers.log(
            ticker, "SELL", prev, new, state.last_price or 0,
            action="FIRED_SELL", notes=reasoning,
        )
        state.last_trigger_ts = datetime.now().timestamp()
        try:
            self._fire_sell(ticker, state.last_price or 0, new, reasoning)
            self.logger.log(f"📉 LiveAgent: SELL crossing {ticker} {prev}→{new} fired.")
        except Exception as e:
            self.logger.log(f"⚠️ LiveAgent: SELL fire callback failed for {ticker}: {e}",
                            level="ERROR")

    # ─── status / introspection ───────────────────────────────────────────────

    def status(self) -> dict:
        """Snapshot for the UI panel."""
        with self._state_lock:
            n_subscribed = len(self.state)
            n_held = sum(1 for s in self.state.values() if s.is_held_position)
            latest_bar_ts = max((s.last_bar_ts or 0) for s in self.state.values()) if self.state else 0
        return {
            "running":             self.is_running,
            "started_at":          self.started_at.isoformat() if self.started_at else None,
            "subscribed_tickers":  n_subscribed,
            "held_in_watchlist":   n_held,
            "bars_processed":      self.bars_processed,
            "latest_bar_ts":       latest_bar_ts,
            "buys_fired_today":    live_triggers.count_today("FIRED_BUY"),
            "buys_cap":            self.daily_buy_cap,
            "buys_suppressed_cap": live_triggers.count_today("SUPPRESSED_CAP"),
            "sells_fired_today":   live_triggers.count_today("FIRED_SELL"),
        }
