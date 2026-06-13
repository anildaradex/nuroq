"""
intraday_indicators.py — pure functions for 1-min intraday signals.

Used by day_trader.py strategies. All functions are stateless and operate on
sequences of Bar (from minute_bars.py). No I/O, no LLM, no time.now() —
backtest and live both pass the same bar stream in and get the same answer.

Convention: every function takes `bars: Sequence[Bar]` ordered ascending by ts.
Empty input returns sensible neutral values (None/0.0) rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from minute_bars import Bar


# ---------------------------------------------------------------------------
# VWAP — volume-weighted average price, cumulative from session open
# ---------------------------------------------------------------------------

def session_vwap(bars: Sequence[Bar]) -> Optional[float]:
    """Cumulative VWAP from the first bar in `bars` to the last bar.
    Uses each bar's vwap × volume (Polygon's per-bar vwap), falling back to
    typical price (h+l+c)/3 when vwap is missing."""
    if not bars:
        return None
    total_pv = 0.0
    total_v = 0.0
    for b in bars:
        v = b.volume or 0.0
        if v <= 0:
            continue
        price = b.vwap if b.vwap and b.vwap > 0 else (b.high + b.low + b.close) / 3.0
        total_pv += price * v
        total_v += v
    if total_v <= 0:
        return None
    return total_pv / total_v


def running_vwap(bars: Sequence[Bar]) -> list[Optional[float]]:
    """VWAP at every bar in the sequence (so you can plot it or check bar N).
    Returns a list of len(bars), each entry = VWAP through that bar inclusive."""
    out: list[Optional[float]] = []
    total_pv = 0.0
    total_v = 0.0
    for b in bars:
        v = b.volume or 0.0
        price = (b.vwap if b.vwap and b.vwap > 0
                 else (b.high + b.low + b.close) / 3.0)
        if v > 0:
            total_pv += price * v
            total_v += v
        out.append(total_pv / total_v if total_v > 0 else None)
    return out


# ---------------------------------------------------------------------------
# Opening Range — N-minute high/low at session open
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    mid: float
    range_pct: float       # (high-low)/mid * 100
    avg_volume: float      # mean per-bar volume during the OR window
    bar_count: int


# Regular session minute index: 09:30 ET = minute 570 of the day.
MARKET_OPEN_MIN = 9 * 60 + 30
MARKET_CLOSE_MIN = 16 * 60


def is_regular_session(bar: Bar) -> bool:
    """True if bar is within 09:30–16:00 ET."""
    m = bar.minute_of_day
    return MARKET_OPEN_MIN <= m < MARKET_CLOSE_MIN


def regular_session_bars(bars: Sequence[Bar]) -> list[Bar]:
    """Filter to just regular-session bars (drops premarket + afterhours)."""
    return [b for b in bars if is_regular_session(b)]


def opening_range(bars: Sequence[Bar], window_minutes: int = 5) -> Optional[OpeningRange]:
    """Compute the opening range over the first N minutes of the regular session.

    For window_minutes=5 → 09:30, 09:31, 09:32, 09:33, 09:34 (5 bars).
    Returns None if we don't have enough bars yet."""
    if window_minutes <= 0:
        return None
    reg = regular_session_bars(bars)
    if len(reg) < window_minutes:
        return None
    window = reg[:window_minutes]
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    mid = (hi + lo) / 2.0
    rng_pct = ((hi - lo) / mid * 100.0) if mid > 0 else 0.0
    avg_vol = sum(b.volume or 0.0 for b in window) / float(window_minutes)
    return OpeningRange(high=hi, low=lo, mid=mid, range_pct=rng_pct,
                        avg_volume=avg_vol, bar_count=window_minutes)


# ---------------------------------------------------------------------------
# Intraday ATR — true range averaged over last N 1-min bars
# ---------------------------------------------------------------------------

def intraday_atr(bars: Sequence[Bar], lookback: int = 14) -> Optional[float]:
    """Wilder-style ATR computed on 1-min bars. Returns None if < lookback+1
    bars supplied."""
    if len(bars) < lookback + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i-1].close
        cur = bars[i]
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev_close),
            abs(cur.low - prev_close),
        )
        trs.append(tr)
    # Use simple mean of the last `lookback` true-range values.
    return sum(trs[-lookback:]) / float(lookback)


# ---------------------------------------------------------------------------
# Rolling volume average — for "is THIS bar high-volume?" checks
# ---------------------------------------------------------------------------

def rolling_volume_avg(bars: Sequence[Bar], lookback: int = 20) -> Optional[float]:
    """Mean volume over the last `lookback` bars (exclusive of nothing — uses
    all available up to lookback). Returns None if no bars."""
    if not bars:
        return None
    window = bars[-lookback:]
    if not window:
        return None
    return sum(b.volume or 0.0 for b in window) / float(len(window))


# ---------------------------------------------------------------------------
# Premarket aggregates — for the gap scanner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PremarketStats:
    high: float
    low: float
    volume: float
    last_price: float
    bar_count: int


def premarket_stats(bars: Sequence[Bar]) -> Optional[PremarketStats]:
    """Aggregate premarket bars (04:00 ET → 09:30 ET). Returns None if no
    premarket bars present."""
    pm = [b for b in bars if b.minute_of_day < MARKET_OPEN_MIN
          and b.minute_of_day >= 4 * 60]
    if not pm:
        return None
    return PremarketStats(
        high=max(b.high for b in pm),
        low=min(b.low for b in pm),
        volume=sum(b.volume or 0.0 for b in pm),
        last_price=pm[-1].close,
        bar_count=len(pm),
    )


def gap_pct(prev_close: float, today_open: float) -> Optional[float]:
    """% gap from prev_close to today_open. None if either is non-positive."""
    if not prev_close or prev_close <= 0 or today_open <= 0:
        return None
    return (today_open - prev_close) / prev_close * 100.0


# ---------------------------------------------------------------------------
# Pattern: bullish 1-min reversal candle (for VWAP pullback)
# ---------------------------------------------------------------------------

def is_bullish_reversal_bar(bar: Bar, prev_bar: Optional[Bar] = None) -> bool:
    """Heuristic: close > open AND close > midpoint (lower wick) AND, if a
    prev_bar is given, close > prev_bar.high (engulfing-style)."""
    if bar.close <= bar.open:
        return False
    mid = (bar.high + bar.low) / 2.0
    if bar.close <= mid:
        return False
    if prev_bar is not None and bar.close <= prev_bar.high:
        return False
    return True


# ---------------------------------------------------------------------------
# Pattern: bull flag detection on 1-min
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BullFlag:
    leg_start_idx: int     # index into bars[]
    leg_end_idx: int
    flag_start_idx: int
    flag_end_idx: int
    leg_pct: float         # how much the leg ran, % from low to high
    flag_high: float
    flag_low: float


def detect_bull_flag(bars: Sequence[Bar],
                     min_leg_pct: float = 3.0,
                     max_leg_bars: int = 30,
                     min_flag_bars: int = 5,
                     max_flag_bars: int = 10,
                     max_flag_pullback_pct: float = 30.0) -> Optional[BullFlag]:
    """Look for a recent strong leg up followed by tight consolidation.

    Algorithm (greedy from the end of `bars`):
      1. The last `min_flag_bars`..`max_flag_bars` define a candidate flag.
      2. The flag's range must be ≤ max_flag_pullback_pct % of the leg's range.
      3. The leg ends at the bar just before the flag starts. Look back up to
         max_leg_bars for the leg's low. Leg pct = (leg_high - leg_low)/leg_low*100.
      4. leg_pct must be ≥ min_leg_pct.

    Returns the BullFlag or None."""
    if len(bars) < min_flag_bars + 3:
        return None
    for flag_len in range(max_flag_bars, min_flag_bars - 1, -1):
        if len(bars) < flag_len + 2:
            continue
        flag = bars[-flag_len:]
        flag_high = max(b.high for b in flag)
        flag_low = min(b.low for b in flag)
        flag_range = flag_high - flag_low
        # Look back from the bar before the flag
        leg_end_idx = len(bars) - flag_len - 1
        leg_start_search = max(0, leg_end_idx - max_leg_bars + 1)
        if leg_end_idx <= leg_start_search:
            continue
        leg_window = bars[leg_start_search:leg_end_idx + 1]
        leg_low_idx_local = min(range(len(leg_window)),
                                key=lambda i: leg_window[i].low)
        leg_low = leg_window[leg_low_idx_local].low
        leg_high = bars[leg_end_idx].high
        if leg_low <= 0:
            continue
        leg_pct = (leg_high - leg_low) / leg_low * 100.0
        if leg_pct < min_leg_pct:
            continue
        leg_range = leg_high - leg_low
        if leg_range <= 0:
            continue
        if (flag_range / leg_range * 100.0) > max_flag_pullback_pct:
            continue
        # Also require the flag to sit in the upper portion of the leg (no
        # giveback below 50%).
        if flag_low < leg_low + 0.5 * leg_range:
            continue
        return BullFlag(
            leg_start_idx=leg_start_search + leg_low_idx_local,
            leg_end_idx=leg_end_idx,
            flag_start_idx=len(bars) - flag_len,
            flag_end_idx=len(bars) - 1,
            leg_pct=leg_pct,
            flag_high=flag_high,
            flag_low=flag_low,
        )
    return None
