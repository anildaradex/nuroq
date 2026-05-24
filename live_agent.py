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
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional, Callable, Dict, List

from data_fetcher import (
    history_cache, fundamentals_cache, ai_score_cache,
    watchlist_today, live_triggers,
)
from scoring import calculate_technicals, get_weekly_confluence, calculate_quant_score


# ─── Constants ────────────────────────────────────────────────────────────────

BUY_CROSSING_THRESHOLD  = 65   # score must cross UP through this to fire BUY
SELL_CROSSING_THRESHOLD = 30   # score must cross DOWN through this for SELL exit
EARNINGS_RISK_BOOST     = 10   # raise BUY threshold by this much if earnings risk
DEFAULT_DAILY_BUY_CAP   = 5    # max BUY approvals fired per market session
INTRADAY_BAR_HISTORY_LEN = 60  # rolling minute bars kept per ticker for volume/H/L

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
    ticker:           str
    baseline_bars:    list                          # cached daily OHLCV (yesterday and earlier)
    weekly_trend:     str                           # precomputed at watchlist load time
    intraday_bars:    deque = field(default_factory=lambda: deque(maxlen=INTRADAY_BAR_HISTORY_LEN))
    today_high:       Optional[float] = None
    today_low:        Optional[float] = None
    today_volume:     float = 0.0
    last_price:       Optional[float] = None
    last_score:       Optional[int] = None          # last recomputed final_score
    last_bar_ts:      Optional[float] = None        # epoch seconds of latest bar
    last_trigger_ts:  Optional[float] = None        # last time we fired an approval
    is_held_position: bool = False                  # for SELL crossing eligibility


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
    ):
        self.streamer = streamer
        self.logger = logger
        self._fire_buy = fire_buy_callback
        self._fire_sell = fire_sell_callback
        self._get_held_tickers = get_held_tickers
        self.daily_buy_cap = daily_buy_cap

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
        return msg

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
        """Detects crossings and fires approvals / exits subject to daily cap."""
        prev = state.last_score

        # BUY crossing: prev < threshold and new >= threshold
        if prev is not None and prev < BUY_CROSSING_THRESHOLD <= new_score:
            self._handle_buy_crossing(state, prev, new_score)

        # SELL crossing (held positions only): prev > threshold and new <= threshold
        elif (state.is_held_position
              and prev is not None
              and prev > SELL_CROSSING_THRESHOLD >= new_score):
            self._handle_sell_crossing(state, prev, new_score)

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

        # Build a concise reasoning string locally (no LLM in hot path).
        reasoning = (
            f"LIVE crossing: {ticker} score {prev} → {new} (≥ {BUY_CROSSING_THRESHOLD}). "
            f"Price ${state.last_price:.2f}. Cached AI={(ai_score_cache.get(ticker) or {}).get('score', '?')}. "
            f"Weekly trend: {state.weekly_trend}."
        )

        live_triggers.log(
            ticker, "BUY", prev, new, state.last_price or 0,
            action="FIRED_BUY", notes=reasoning,
        )
        state.last_trigger_ts = datetime.now().timestamp()
        try:
            self._fire_buy(ticker, state.last_price or 0, new, reasoning)
            self.logger.log(f"🎯 LiveAgent: BUY crossing {ticker} {prev}→{new} fired.")
        except Exception as e:
            self.logger.log(f"⚠️ LiveAgent: BUY fire callback failed for {ticker}: {e}",
                            level="ERROR")

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
