"""
backtest/replay.py — historical bar replay through a Strategy.

The Simulator is the keystone abstraction: it accepts a Strategy + a list of
(ticker, session_date, bars) tuples and produces a TradeLog. The same
Strategy interface is consumed by the live day_trader, so anything tuned in
backtest behaves identically when wired live.

Usage:
    from backtest.replay import Simulator
    from backtest.fill_model import SimulatedFillModel
    from day_trader import ORB5Strategy

    sim = Simulator(SimulatedFillModel(slippage_bps=5))
    log = sim.run(strategy=ORB5Strategy(), sessions=[
        ("AAPL", "2026-05-12", bars_for_aapl_2026_05_12),
        ...
    ])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol, Sequence

from minute_bars import Bar, get_minute_bars
from backtest.fill_model import (
    Fill, OrderIntent, OrderSide, Position, SimulatedFillModel, TradeRecord,
)


# ---------------------------------------------------------------------------
# Strategy protocol — what every day-trading strategy must implement.
# ---------------------------------------------------------------------------

class Strategy(Protocol):
    """A day-trading strategy. Must be stateless across sessions — the
    Simulator calls reset_for_session() at the start of every (ticker, date)
    so the strategy doesn't carry stale ORB / VWAP state across days."""

    setup_id: str

    def reset_for_session(self, ticker: str, session_date: str) -> None: ...

    def on_bar(self, ticker: str, bar: Bar, bars_so_far: Sequence[Bar],
               in_position: bool) -> list[OrderIntent]:
        """Called for every regular-session bar. Returns 0 or more new
        OrderIntents. Strategies only emit entry intents — exits are managed
        by the Simulator using the Position's stop/target/time-stop fields."""
        ...

    def initial_stop_and_target(self, entry_price: float, intent: OrderIntent,
                                bars_so_far: Sequence[Bar]) -> tuple[float, float, int]:
        """Called after an entry fills. Returns (stop_price, target_price,
        time_stop_bars). time_stop_bars=0 means no time stop."""
        ...


# ---------------------------------------------------------------------------
# BarSource — historical from cache, or (later) live from WebSocket.
# ---------------------------------------------------------------------------

class HistoricalBarSource:
    """Reads cached minute bars for a list of (ticker, session_date) pairs."""

    def __init__(self, sessions: Iterable[tuple[str, str]], logger=None):
        self.sessions = list(sessions)
        self.logger = logger

    def stream(self) -> Iterable[tuple[str, str, list[Bar]]]:
        """Yield (ticker, session_date, bars) for each session.
        Bars include premarket so strategies can compute gap / premkt vol."""
        for ticker, sess_date in self.sessions:
            bars = get_minute_bars(ticker, sess_date, include_premarket=True,
                                   logger=self.logger)
            if not bars:
                if self.logger:
                    self.logger.log(f"⚠️ no bars for {ticker} {sess_date}",
                                    level="WARNING")
                continue
            yield (ticker, sess_date, bars)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

@dataclass
class TradeLog:
    """Result of one Simulator.run() invocation."""
    trades: list[TradeRecord] = field(default_factory=list)
    fills_attempted: int = 0
    sessions_processed: int = 0
    sessions_skipped: int = 0


class Simulator:
    """Drive a Strategy over historical bars with a FillModel.

    One position per (ticker, session) — no pyramiding in v1. Strategies that
    want multi-leg should refactor later."""

    def __init__(self, fill_model: SimulatedFillModel, logger=None):
        self.fill_model = fill_model
        self.logger = logger

    def run(self, strategy: Strategy,
            sessions: Optional[Sequence[tuple[str, str, list[Bar]]]] = None,
            ticker_dates: Optional[Iterable[tuple[str, str]]] = None) -> TradeLog:
        """Either pass pre-loaded `sessions` OR `ticker_dates` to fetch via
        HistoricalBarSource."""
        log = TradeLog()
        if sessions is None:
            if ticker_dates is None:
                return log
            sessions = HistoricalBarSource(ticker_dates, logger=self.logger).stream()
        for ticker, sess_date, bars in sessions:
            self._run_one_session(strategy, ticker, sess_date, bars, log)
        return log

    # -- internals --------------------------------------------------------

    def _run_one_session(self, strategy: Strategy, ticker: str, sess_date: str,
                         bars: list[Bar], log: TradeLog) -> None:
        # Reset strategy state for this ticker/session
        strategy.reset_for_session(ticker, sess_date)
        log.sessions_processed += 1

        # Find the regular-session portion. Iterate ALL bars (so the strategy
        # can see premarket if it wants), but only allow entries during regular.
        # Find indices of regular-session bars.
        from intraday_indicators import is_regular_session
        regular_indices = [i for i, b in enumerate(bars) if is_regular_session(b)]
        if not regular_indices:
            log.sessions_skipped += 1
            return
        last_regular_idx = regular_indices[-1]

        position: Optional[Position] = None
        pending_intent: Optional[OrderIntent] = None

        for i in regular_indices:
            bar = bars[i]
            # 1) Manage open position FIRST — exits take priority over new entries.
            if position is not None:
                exit_result = self.fill_model.evaluate_exit(position, bar)
                # EOD flatten — last regular bar of the session
                if i == last_regular_idx and exit_result is None:
                    exit_result = (bar.close, "EOD")
                if exit_result is not None:
                    exit_price, reason = exit_result
                    pnl = (exit_price - position.entry_price) * position.shares
                    pnl_pct = ((exit_price - position.entry_price) /
                               position.entry_price * 100.0)
                    log.trades.append(TradeRecord(
                        ticker=ticker,
                        setup_id=position.setup_id,
                        entry_ts=position.entry_ts,
                        entry_price=position.entry_price,
                        exit_ts=bar.ts,
                        exit_price=exit_price,
                        shares=position.shares,
                        pnl=round(pnl, 4),
                        pnl_pct=round(pnl_pct, 4),
                        exit_reason=reason,
                        bars_held=position.bars_held + 1,
                    ))
                    position = None
                else:
                    position.bars_held += 1

            # 2) Try to fill a pending entry intent on THIS bar (no peeking).
            if position is None and pending_intent is not None:
                fill = self.fill_model.try_fill_entry(pending_intent, bar)
                log.fills_attempted += 1
                if fill is not None:
                    bars_so_far = bars[:i+1]
                    stop, target, time_stop = strategy.initial_stop_and_target(
                        fill.price, pending_intent, bars_so_far,
                    )
                    position = Position(
                        ticker=ticker, entry_ts=fill.ts,
                        entry_price=fill.price, shares=fill.shares,
                        stop_price=stop, target_price=target,
                        setup_id=fill.setup_id, time_stop_bars=time_stop,
                    )
                pending_intent = None     # one shot — strategy must re-emit if it wants

            # 3) Ask strategy for new entry intents (only if flat and no pending).
            if position is None and pending_intent is None:
                bars_so_far = bars[:i+1]
                intents = strategy.on_bar(ticker, bar, bars_so_far,
                                          in_position=False) or []
                # We only support one position per session/ticker → take the first.
                for intent in intents:
                    if intent.side == OrderSide.BUY:
                        pending_intent = intent
                        break

        # End of regular session: if position somehow still open, force EOD.
        if position is not None:
            last_bar = bars[last_regular_idx]
            exit_price = last_bar.close
            pnl = (exit_price - position.entry_price) * position.shares
            pnl_pct = ((exit_price - position.entry_price) /
                       position.entry_price * 100.0)
            log.trades.append(TradeRecord(
                ticker=ticker, setup_id=position.setup_id,
                entry_ts=position.entry_ts, entry_price=position.entry_price,
                exit_ts=last_bar.ts, exit_price=exit_price,
                shares=position.shares,
                pnl=round(pnl, 4), pnl_pct=round(pnl_pct, 4),
                exit_reason="EOD", bars_held=position.bars_held,
            ))
