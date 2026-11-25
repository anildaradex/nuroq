"""Indicators computed from daily bars. Bars are dicts {o,h,l,c,v,t}, oldest first."""
from __future__ import annotations

from statistics import median
from typing import Optional

MIN_BARS = 222          # 200-day average plus its value 22 days earlier
SKIP = 21               # momentum windows skip the most recent month


def _sma(vals: list, n: int, end: Optional[int] = None) -> Optional[float]:
    end = len(vals) if end is None else end
    if end < n:
        return None
    return sum(vals[end - n:end]) / n


def _atr(bars: list, n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def skip_month_return(closes: list, lookback: int) -> Optional[float]:
    """Return over `lookback` bars ending SKIP bars ago."""
    end = len(closes) - 1 - SKIP
    start = end - lookback
    if start < 0 or closes[start] <= 0:
        return None
    return closes[end] / closes[start] - 1


def compute(bars: list) -> Optional[dict]:
    """Returns the feature dict, or None when there is too little history."""
    bars = [b for b in bars if b.get("c") and b.get("h") and b.get("l")]
    if len(bars) < MIN_BARS:
        return None

    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    close = closes[-1]
    atr = _atr(bars)
    if not atr or atr <= 0:
        return None

    # 20-day high and the pullback that followed it
    win = highs[-20:]
    hi20 = max(win)
    hi_idx = len(highs) - 20 + max(i for i, h in enumerate(win) if h == hi20)
    pullback_low = min(lows[hi_idx:])
    avg_vol50 = _sma(vols, 50) or 0.0
    down_vols = [vols[i] for i in range(hi_idx + 1, len(bars)) if closes[i] < closes[i - 1]]
    if not down_vols or avg_vol50 <= 0:
        volume_state = "neutral"
    elif max(down_vols) > 2 * avg_vol50:
        volume_state = "heavy"
    elif max(down_vols) < avg_vol50:
        volume_state = "light"
    else:
        volume_state = "neutral"

    day_range = highs[-1] - lows[-1]
    sma20, sma50, sma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)

    return {
        "close": close,
        "change_pct": round((close / closes[-2] - 1) * 100, 2),
        "day_low": lows[-1],
        "day_high": highs[-1],
        "atr": atr,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "sma200_22ago": _sma(closes, 200, len(closes) - 22),
        "high_52w": max(highs[-252:]),
        "nearness": close / max(highs[-252:]),
        "high_20": hi20,
        "pullback_depth_atr": (hi20 - close) / atr,
        "pullback_low": pullback_low,
        "median_dollar_vol": median(c * v for c, v in zip(closes[-20:], vols[-20:])),
        "ret_126": skip_month_return(closes, 126),
        "ret_252": skip_month_return(closes, 252),
        "reversal": closes[-1] > highs[-2]
                    or (day_range > 0 and (close - lows[-1]) / day_range >= 2 / 3),
        "near_ma": min(abs(close - sma20), abs(close - sma50)) <= atr,
        "volume_state": volume_state,
        "as_of": bars[-1].get("t"),
    }
