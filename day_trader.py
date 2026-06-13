"""
day_trader.py — NuroQ intraday strategy module.

PUBLIC SURFACE
  • Strategy (Protocol) — same interface used by backtest.replay.Simulator
                          and the live agent. Tune in backtest → behaves
                          identically live.
  • ORB5Strategy        — v1 production strategy. Opening Range Breakout on
                          the first 5 minutes of regular session.
  • DayTraderMode       — disabled | shadow | approve | auto
  • DayTraderConfig     — knobs read from agent_config (DT-specific subset)

DESIGN INVARIANTS (do NOT break)
  • Strategies are PURE — no I/O, no LLM, no Polygon calls. They take a bar
    + history and return OrderIntents. The simulator and the live wrapper
    are the only places that touch the outside world.
  • One position per (ticker, session). The strategy emits ONE entry per
    crossing; the simulator manages exits via Position.stop/target/time_stop.
  • Never call analyst.analyze() or any LLM in on_bar(). Premarket plans
    and post-trade journaling are the only LLM touchpoints (queued async).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional, Sequence

from minute_bars import Bar
from backtest.fill_model import OrderIntent, OrderKind, OrderSide
from intraday_indicators import (
    OpeningRange, intraday_atr, is_regular_session, opening_range,
    regular_session_bars, rolling_volume_avg, session_vwap,
)


# ---------------------------------------------------------------------------
# Mode enum (used by live wrapper + agent_config)
# ---------------------------------------------------------------------------

class DayTraderMode(str, enum.Enum):
    DISABLED = "disabled"  # off entirely — no scan, no log
    SHADOW   = "shadow"    # detect + log triggers, place NO orders
    APPROVE  = "approve"   # detect + send Telegram approval, NO auto-place
    AUTO     = "auto"      # full autonomy — risk_manager green-lights → place


# ---------------------------------------------------------------------------
# ORB-5 Strategy
# ---------------------------------------------------------------------------

@dataclass
class ORB5Params:
    """All tunable thresholds. Defaults are paper-trading-conservative.
    Loaded from agent_config when running live; settable explicitly in tests."""
    orb_window_minutes: int = 5
    min_orb_range_pct:  float = 0.2     # OR must span ≥0.2% of mid (skip dead names)
    max_orb_range_pct:  float = 5.0     # OR must span ≤5% of mid (skip gappers with no setup)
    volume_multiplier:  float = 2.0     # breakout bar volume ≥ 2× ORB avg
    require_vwap_align: bool = True     # breakout close must be > VWAP
    target_R_multiple:  float = 2.0     # target = entry + 2 × (entry - stop)
    time_stop_bars:     int   = 30      # exit at market if no T1 within 30 min
    no_entry_after_min: int   = 14*60 + 30  # 14:30 ET — no new entries after this

    # Stop placement: ORL by default. Alternative: midpoint of ORB.
    stop_at: str = "ORL"   # "ORL" | "ORB_MID"


@dataclass
class _SessionState:
    """Per-session strategy memory. Reset each (ticker, session_date)."""
    ticker: str
    session_date: str
    orb: Optional[OpeningRange] = None
    fired: bool = False                 # one entry per session per ticker
    orb_bars_seen: int = 0


class ORB5Strategy:
    """Opening Range Breakout — first 5 min H/L, break above on volume + VWAP.

    Fire condition (LONG, all required):
      1. Past the ORB window (regular-session bar idx ≥ orb_window_minutes)
      2. Bar close > ORB high
      3. Bar volume > ORB.avg_volume × volume_multiplier
      4. Bar close > session VWAP   (if require_vwap_align)
      5. Strategy hasn't already fired this session
      6. Bar time < no_entry_after_min  (e.g. before 14:30 ET)

    Stop placement:  ORL  (or ORB mid)
    Target:          entry + 2R (R = entry - stop)
    Time stop:       30 bars (≈30 min) if no winning exit
    """

    setup_id = "ORB5"

    def __init__(self, params: Optional[ORB5Params] = None):
        self.params = params or ORB5Params()
        self._state: Optional[_SessionState] = None

    # -- Strategy protocol ------------------------------------------------

    def reset_for_session(self, ticker: str, session_date: str) -> None:
        self._state = _SessionState(ticker=ticker, session_date=session_date)

    def on_bar(self, ticker: str, bar: Bar, bars_so_far: Sequence[Bar],
               in_position: bool) -> list[OrderIntent]:
        s = self._state
        if s is None or s.ticker != ticker:
            self.reset_for_session(ticker, "")
            s = self._state
        if s.fired or in_position:
            return []
        if not is_regular_session(bar):
            return []
        # Don't trade in the last 90 min
        if bar.minute_of_day >= self.params.no_entry_after_min:
            return []
        # Build the OR only once we have enough regular-session bars
        if s.orb is None:
            s.orb = opening_range(bars_so_far, self.params.orb_window_minutes)
            if s.orb is None:
                return []                          # still inside the OR window
            # Sanity range filters
            if s.orb.range_pct < self.params.min_orb_range_pct:
                s.fired = True                     # too tight — skip whole session
                return []
            if s.orb.range_pct > self.params.max_orb_range_pct:
                s.fired = True                     # too wide — skip whole session
                return []
        # Breakout check — only on regular bars AFTER the OR window
        reg_so_far = regular_session_bars(bars_so_far)
        if len(reg_so_far) <= self.params.orb_window_minutes:
            return []
        # Condition 2 — close above OR high
        if bar.close <= s.orb.high:
            return []
        # Condition 3 — volume confirmation
        if (bar.volume or 0) < s.orb.avg_volume * self.params.volume_multiplier:
            return []
        # Condition 4 — VWAP alignment
        if self.params.require_vwap_align:
            vwap = session_vwap(reg_so_far)
            if vwap is None or bar.close <= vwap:
                return []
        # Fire — STOP entry at bar.close (we want fill on the next bar's
        # action, not a peek-ahead market at this close). Use a STOP order at
        # `bar.close` so the simulator fills next bar if it trades through.
        s.fired = True
        # Compute share size = caller's responsibility (risk manager when live;
        # backtest passes shares directly). For backtest we use 100 as default.
        return [OrderIntent(
            ticker=ticker,
            side=OrderSide.BUY,
            kind=OrderKind.STOP,
            shares=100,                            # placeholder — see initial_stop_and_target
            trigger_price=round(bar.close, 4),
            setup_id=self.setup_id,
            note=(f"ORB5 breakout: close ${bar.close:.2f} > ORH ${s.orb.high:.2f}; "
                  f"vol {bar.volume:.0f} ≥ {self.params.volume_multiplier}× "
                  f"avg {s.orb.avg_volume:.0f}"),
        )]

    def initial_stop_and_target(self, entry_price: float, intent: OrderIntent,
                                bars_so_far: Sequence[Bar]) -> tuple[float, float, int]:
        s = self._state
        if s is None or s.orb is None:
            # Fallback: 1% stop, 2% target
            return (round(entry_price * 0.99, 2),
                    round(entry_price * 1.02, 2),
                    self.params.time_stop_bars)
        if self.params.stop_at == "ORB_MID":
            stop = s.orb.mid
        else:
            stop = s.orb.low
        # Guard against degenerate "stop above entry"
        stop = min(stop, entry_price - 0.01)
        risk = max(0.01, entry_price - stop)
        target = entry_price + self.params.target_R_multiple * risk
        return (round(stop, 4), round(target, 4), self.params.time_stop_bars)


# ---------------------------------------------------------------------------
# Sizing helper — used by both backtest and live to compute shares from $ risk.
# ---------------------------------------------------------------------------

def shares_for_risk(entry: float, stop: float, risk_dollars: float,
                    cash_available: float) -> int:
    """Compute shares so that |entry-stop| × shares ≈ risk_dollars, capped by
    cash. Floor to int. Returns 0 if any input is non-positive."""
    if entry <= 0 or risk_dollars <= 0 or cash_available <= 0:
        return 0
    per_share = max(0.01, entry - stop)
    by_risk = int(risk_dollars // per_share)
    by_cash = int(cash_available // entry)
    return max(0, min(by_risk, by_cash))


# ---------------------------------------------------------------------------
# LIVE ENGINE — wraps ORB5Strategy for the bar-callback hot path.
# ---------------------------------------------------------------------------
#
# Plumbing:
#   live_agent._on_bar() → DayTraderEngine.on_bar(symbol, bar) (one extra call)
#
# The engine is fully self-contained:
#   • Reads agent_config.dt_mode on EVERY bar (so flipping mode in the UI
#     takes effect within ~1 minute, no restart needed).
#   • Mode == "disabled" → returns immediately, zero overhead.
#   • Maintains per-ticker bar history (last ~500 1-min bars).
#   • Calls ORB5Strategy.on_bar() and logs to live_triggers with a DT_ prefix
#     so the React feed surfaces day-trades distinctly from swing crossings.
#   • In SHADOW mode: logs only.
#   • In APPROVE mode: logs + sends a Telegram message (no auto-place).
#   • In AUTO mode: logs + delegates to risk_manager → alpaca_executor.
#
# Per-session per-ticker, ONE position per (date, ticker). The engine resets
# state at the first regular-session bar each new ET day.

import threading
import time
from collections import defaultdict, deque
from datetime import datetime as _dt
from typing import Callable

from minute_bars import Bar as _Bar


def _today_et_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return _dt.now().strftime("%Y-%m-%d")


def _et_now() -> _dt:
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo("America/New_York"))
    except Exception:
        return _dt.now()


@dataclass
class _LivePosition:
    ticker: str
    entry_ts: int
    entry_price: float
    shares: int
    stop_price: float
    target_price: float
    setup_id: str
    bars_held: int = 0
    time_stop_bars: int = 0
    moved_to_breakeven: bool = False
    client_order_id: str = ""


class DayTraderEngine:
    """Live wrapper around ORB5Strategy. Default-disabled; gated by dt_mode.

    Inject a `submit_fn(ticker, shares, entry, sl, tp, client_order_id, reason)`
    callback at construction — the engine calls it ONLY in AUTO mode and ONLY
    if risk_manager.can_enter_trade() green-lights. In SHADOW/APPROVE it
    never calls submit_fn.

    Inject `notify_fn(text)` for SHADOW + APPROVE Telegram messages.

    Inject `live_triggers_log(...)` matching the LiveTriggers.log signature
    for audit-trail rows. All three are passed in (not imported) so the
    engine stays test-friendly + decouples from live_agent module structure.
    """

    def __init__(self,
                 *,
                 submit_fn: Optional[Callable] = None,
                 notify_fn: Optional[Callable[[str], None]] = None,
                 live_triggers_log: Optional[Callable] = None,
                 universe: Optional[list[str]] = None,
                 logger=None):
        self.submit_fn = submit_fn
        self.notify_fn = notify_fn
        self.live_triggers_log = live_triggers_log
        self.logger = logger
        # Per-ticker bar history (cap at ~500 to bound memory)
        self._bars: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        # Per-ticker strategy instance — each holds its own _SessionState
        self._strategies: dict[str, ORB5Strategy] = {}
        # Open positions keyed by ticker
        self._positions: dict[str, _LivePosition] = {}
        # The date we're currently in — when ET day rolls, reset state
        self._session_date: str = _today_et_str()
        # In-day attempt counter (for cap enforcement). Counted per-day.
        self._fires_today: int = 0
        self._lock = threading.Lock()
        # Override universe for tests / explicit lists. None = use config.dt_universe.
        self._universe_override: Optional[list[str]] = universe

    # -- public --------------------------------------------------------------

    def status(self) -> dict:
        """Lightweight snapshot for the /api/day-trader/status endpoint."""
        import agent_config
        cfg = agent_config.get()
        return {
            "mode": cfg.get("dt_mode", "disabled"),
            "session_date": self._session_date,
            "fires_today": self._fires_today,
            "open_positions": list(self._positions.keys()),
            "open_position_count": len(self._positions),
            "universe_size": len(self._effective_universe()),
            "bars_seen_per_ticker": {t: len(q) for t, q in self._bars.items()},
        }

    def on_bar(self, ticker: str, live_bar) -> None:
        """Hot-path entry. live_bar must expose .close/.open/.high/.low/.volume
        and either .timestamp (datetime) or .start (datetime) — accepts both
        Alpaca SDK and Polygon WS bar shapes."""
        # Re-read mode every bar so UI flips take effect immediately.
        import agent_config
        cfg = agent_config.get()
        mode = (cfg.get("dt_mode") or "disabled").lower()
        if mode == "disabled":
            return

        # ET day rollover → reset all per-session state.
        today = _today_et_str()
        if today != self._session_date:
            with self._lock:
                self._session_date = today
                self._strategies.clear()
                self._positions.clear()
                self._fires_today = 0
                for q in self._bars.values():
                    q.clear()

        # Universe filter — if configured, only trade these tickers.
        universe = self._effective_universe()
        if universe and ticker not in universe:
            return

        # Normalize live bar → minute_bars.Bar dataclass
        try:
            bar = self._normalize_bar(ticker, live_bar)
        except Exception as e:
            if self.logger:
                self.logger.log(f"⚠️ DayTrader: bar normalize failed for {ticker}: {e}",
                                level="WARNING")
            return

        # Push into per-ticker history (cap deque already handles eviction).
        self._bars[ticker].append(bar)

        # Manage open position FIRST (exit logic).
        pos = self._positions.get(ticker)
        if pos is not None:
            self._manage_open_position(pos, bar, mode)
            return        # one position per ticker — no re-entry same bar

        # Concurrency cap (DT-specific).
        if len(self._positions) >= cfg.get("dt_max_concurrent", 3):
            return

        # Build strategy on first bar for this ticker.
        strat = self._strategies.get(ticker)
        if strat is None:
            from day_trader import ORB5Params, ORB5Strategy as _ORB5
            params = ORB5Params(
                volume_multiplier=float(cfg.get("dt_volume_multiplier", 2.0)),
                require_vwap_align=bool(cfg.get("dt_require_vwap", True)),
                time_stop_bars=int(cfg.get("dt_time_stop_bars", 30)),
                target_R_multiple=float(cfg.get("dt_target_r_multiple", 2.0)),
            )
            strat = _ORB5(params)
            strat.reset_for_session(ticker, today)
            self._strategies[ticker] = strat

        # Evaluate.
        bars_so_far = list(self._bars[ticker])
        intents = strat.on_bar(ticker, bar, bars_so_far, in_position=False)
        if not intents:
            return
        intent = intents[0]
        # Compute stop/target eagerly so we can include them in the log/Telegram.
        stop, target, time_stop_bars = strat.initial_stop_and_target(
            intent.trigger_price, intent, bars_so_far,
        )
        self._handle_fire(ticker, intent, bar, stop, target, time_stop_bars,
                          mode=mode, cfg=cfg)

    # -- internals -----------------------------------------------------------

    def _effective_universe(self) -> list[str]:
        if self._universe_override is not None:
            return [t.upper() for t in self._universe_override]
        import agent_config
        cfg = agent_config.get()
        raw = (cfg.get("dt_universe") or "").strip()
        if not raw:
            return []           # empty = no universe filter (engine still acts on every ticker passed in)
        return [t.strip().upper() for t in raw.split(",") if t.strip()]

    def _normalize_bar(self, ticker: str, live_bar) -> _Bar:
        # Alpaca SDK bar has .timestamp; Polygon WS bar usually has .start_time
        ts_attr = (getattr(live_bar, "timestamp", None)
                   or getattr(live_bar, "start_time", None)
                   or getattr(live_bar, "t", None))
        if isinstance(ts_attr, (int, float)):
            ts = int(ts_attr if ts_attr < 1e11 else ts_attr // 1000)
        elif hasattr(ts_attr, "timestamp"):
            ts = int(ts_attr.timestamp())
        else:
            ts = int(time.time())
        vw = getattr(live_bar, "vwap", None) or getattr(live_bar, "vw", None)
        h = float(getattr(live_bar, "high", 0) or 0)
        l = float(getattr(live_bar, "low", 0) or 0)
        c = float(getattr(live_bar, "close", 0) or 0)
        return _Bar(
            ticker=ticker, ts=ts,
            open=float(getattr(live_bar, "open", c) or c),
            high=h, low=l, close=c,
            volume=float(getattr(live_bar, "volume", 0) or 0),
            vwap=float(vw) if vw else (h + l + c) / 3.0,
        )

    def _handle_fire(self, ticker: str, intent, bar: _Bar,
                     stop: float, target: float, time_stop_bars: int,
                     *, mode: str, cfg: dict) -> None:
        self._fires_today += 1
        client_order_id = f"dt_{self._session_date}_{ticker}_{intent.setup_id}"
        risk = round((intent.trigger_price - stop), 4)
        note = (f"{intent.setup_id} entry={intent.trigger_price:.2f} "
                f"stop={stop:.2f} target={target:.2f} "
                f"risk/sh=${risk:.2f} t_stop={time_stop_bars}bars "
                f"mode={mode}")

        # Always log to the feed.
        if self.live_triggers_log:
            try:
                self.live_triggers_log(
                    ticker, "BUY", 0, 0, intent.trigger_price,
                    action=f"DT_{mode.upper()}_FIRE",
                    notes=note[:400],
                )
            except Exception as e:
                if self.logger:
                    self.logger.log(f"⚠️ DayTrader: live_triggers log failed: {e}",
                                    level="WARNING")

        if self.logger:
            self.logger.log(f"🤖 DT[{mode}] FIRE {ticker} — {note}")

        if mode == "shadow":
            # Telegram disabled in shadow to avoid alert fatigue.
            return

        if mode == "approve":
            if self.notify_fn:
                self.notify_fn(
                    f"🤖 *Day-trader signal · {ticker}* (approval mode)\n\n"
                    f"Setup: `{intent.setup_id}`\n"
                    f"Entry: ${intent.trigger_price:.2f}\n"
                    f"Stop:  ${stop:.2f}    (risk ${risk:.2f}/sh)\n"
                    f"Target: ${target:.2f}\n"
                    f"Time stop: {time_stop_bars} bars\n\n"
                    f"_Manual confirm via the app or Telegram quick-trade._"
                )
            return

        # mode == "auto"
        if self.submit_fn is None:
            if self.logger:
                self.logger.log(f"⚠️ DT[auto] {ticker}: no submit_fn wired; "
                                f"falling back to shadow.", level="WARNING")
            return
        try:
            # Caller decides on risk_manager + sizing + Alpaca submit. We give
            # the intent + computed stop/target + a stable client_order_id for
            # idempotency across Mac/cloud divergence.
            shares = self.submit_fn(
                ticker=ticker, shares=intent.shares,
                entry=intent.trigger_price, sl=stop, tp=target,
                client_order_id=client_order_id,
                reason=note,
                setup_id=intent.setup_id,
            )
            if shares and shares > 0:
                self._positions[ticker] = _LivePosition(
                    ticker=ticker, entry_ts=bar.ts,
                    entry_price=intent.trigger_price, shares=int(shares),
                    stop_price=stop, target_price=target,
                    setup_id=intent.setup_id,
                    time_stop_bars=time_stop_bars,
                    client_order_id=client_order_id,
                )
        except Exception as e:
            if self.logger:
                self.logger.log(f"⚠️ DT[auto] submit failed for {ticker}: {e}",
                                level="ERROR")

    def _manage_open_position(self, pos: _LivePosition, bar: _Bar,
                              mode: str) -> None:
        pos.bars_held += 1
        # The actual bracket SL/TP fires server-side at Alpaca; the engine just
        # tracks the position for in_position gating + time-stop notification.
        # Time stop notify (does NOT auto-close in v1 — Alpaca bracket handles
        # SL/TP; user manually exits a stalled position from the UI).
        if pos.time_stop_bars and pos.bars_held >= pos.time_stop_bars:
            if self.live_triggers_log:
                try:
                    self.live_triggers_log(
                        pos.ticker, "SELL", 0, 0, bar.close,
                        action=f"DT_{mode.upper()}_TIME_STOP",
                        notes=(f"{pos.setup_id} time stop reached "
                               f"({pos.bars_held} bars) without TP. "
                               f"Consider manual flatten."),
                    )
                except Exception:
                    pass
            # Drop from internal tracking so a new entry could fire if the
            # strategy retriggers later (it won't — fired flag set).
            self._positions.pop(pos.ticker, None)
