"""
backtest/fill_model.py — simulated order fills with realistic friction.

The Simulator hands every OrderIntent to the fill model on each new bar.
For OPEN orders we check the NEXT bar (no bar-close peeking) to apply
slippage. For exits (stop/target), we check intra-bar high/low against the
order's trigger price.

The model is intentionally conservative — for paper-trading expectancy you
want to OVERESTIMATE slippage so the real account performs at least this
well, not worse.

Defaults:
  slippage_bps = 5   → 0.05% of price slipped on market orders / stops
  commission   = 0   → Alpaca paper has none; broker-agnostic so easy to set
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from minute_bars import Bar


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(str, Enum):
    """How the entry order is triggered.

    MARKET — fill at next bar open + slippage.
    STOP   — fill if next bar high >= trigger (BUY) / low <= trigger (SELL).
             Fill price = trigger + slippage past it.
    LIMIT  — fill if next bar low <= limit (BUY) / high >= limit (SELL).
             Fill price = limit.
    """
    MARKET = "MARKET"
    STOP = "STOP"
    LIMIT = "LIMIT"


@dataclass
class OrderIntent:
    """What the strategy wants. The simulator decides if/when it fills."""
    ticker: str
    side: OrderSide
    kind: OrderKind
    shares: int
    trigger_price: float = 0.0     # for STOP / LIMIT
    setup_id: str = ""             # "ORB5" / "VWAP_PB" / "FLAG"
    note: str = ""


@dataclass
class Fill:
    """Confirmation that an OrderIntent executed."""
    ticker: str
    side: OrderSide
    shares: int
    price: float
    ts: int                        # unix seconds of the bar the fill occurred on
    setup_id: str
    intent: OrderIntent


@dataclass
class Position:
    """A live position the simulator is tracking."""
    ticker: str
    entry_ts: int
    entry_price: float
    shares: int
    stop_price: float
    target_price: float
    setup_id: str
    # Trailing state:
    moved_to_breakeven: bool = False
    time_stop_bars: int = 0        # max bars the position may be held; 0 = no cap
    bars_held: int = 0


@dataclass
class TradeRecord:
    """One round-trip trade — for the TradeLog / metrics."""
    ticker: str
    setup_id: str
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    exit_reason: str               # "TARGET" | "STOP" | "TIME" | "EOD" | "MANUAL"
    bars_held: int

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


class SimulatedFillModel:
    """Convert OrderIntents into Fills using a slippage model.

    Long-only here (the day-trader v1 is long-only). Shorts would mirror."""

    def __init__(self, slippage_bps: float = 5.0, commission_per_share: float = 0.0):
        self.slippage_bps = float(slippage_bps)
        self.commission_per_share = float(commission_per_share)

    def _slip(self, price: float, side: OrderSide) -> float:
        """Apply slippage in the unfavorable direction."""
        slip = price * self.slippage_bps / 10_000.0
        if side == OrderSide.BUY:
            return price + slip
        return max(0.01, price - slip)

    # -- Entry fills -------------------------------------------------------

    def try_fill_entry(self, intent: OrderIntent, next_bar: Bar) -> Optional[Fill]:
        """Decide if `intent` fills on `next_bar`. Returns Fill or None.
        For LONG-only v1: STOP entry = breakout, fills if next bar HIGH >= trigger."""
        if intent.side != OrderSide.BUY:
            return None
        if intent.kind == OrderKind.MARKET:
            price = self._slip(next_bar.open, OrderSide.BUY)
            return Fill(intent.ticker, OrderSide.BUY, intent.shares,
                        round(price, 4), next_bar.ts, intent.setup_id, intent)
        if intent.kind == OrderKind.STOP:
            if next_bar.high >= intent.trigger_price:
                # Worst-case: filled at trigger + slippage past it
                fill_price = self._slip(intent.trigger_price, OrderSide.BUY)
                fill_price = max(fill_price, next_bar.open)   # no time-travel
                return Fill(intent.ticker, OrderSide.BUY, intent.shares,
                            round(fill_price, 4), next_bar.ts, intent.setup_id, intent)
            return None
        if intent.kind == OrderKind.LIMIT:
            if next_bar.low <= intent.trigger_price:
                return Fill(intent.ticker, OrderSide.BUY, intent.shares,
                            round(intent.trigger_price, 4), next_bar.ts,
                            intent.setup_id, intent)
            return None
        return None

    # -- Exit decisions ---------------------------------------------------

    def evaluate_exit(self, pos: Position, bar: Bar) -> Optional[tuple[float, str]]:
        """Return (exit_price, reason) if the position exits during `bar`.
        Stop ALWAYS evaluated before target (conservative — assumes the worst
        case where stop hit first within the bar). Reason ∈ {STOP, TARGET, TIME}."""
        # Stop hit
        if bar.low <= pos.stop_price:
            exit_price = self._slip(pos.stop_price, OrderSide.SELL)
            # Don't time-travel above bar close
            exit_price = min(exit_price, bar.high)
            return (round(exit_price, 4), "STOP")
        # Target hit
        if bar.high >= pos.target_price:
            # Target is a limit — assume filled AT target
            return (round(pos.target_price, 4), "TARGET")
        # Time stop
        if pos.time_stop_bars and pos.bars_held >= pos.time_stop_bars:
            exit_price = self._slip(bar.close, OrderSide.SELL)
            return (round(exit_price, 4), "TIME")
        return None
