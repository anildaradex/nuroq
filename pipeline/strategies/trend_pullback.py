"""Trend strategy that uses a pullback to time entry. Rules, score, exit plan, sizing."""
from __future__ import annotations

from typing import Optional

MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 20_000_000
MAX_OFF_HIGH = 0.25
EARNINGS_BLACKOUT_DAYS = 10

BUY_THRESHOLD = 70
WATCH_THRESHOLD = 50

WEIGHTS = {
    "relative_strength": 25, "nearness": 15, "pullback": 20, "reversal": 10,
    "sector": 10, "near_ma": 5, "volume": 5, "ai": 10,
}

STOP_ATR_BASE, STOP_ATR_MIN, STOP_ATR_MAX = 2.0, 1.5, 3.0
TARGET_ATR = 3.0
TIME_STOP_DAYS = 10
MAX_POSITION_PCT = 0.25


def eligible(f: dict, earnings_in_days: Optional[int] = None,
             reported_last_night: bool = False) -> tuple[bool, list]:
    """All rules must pass. Returns (ok, reasons_failed)."""
    fails = []
    if f["close"] <= MIN_PRICE:
        fails.append(f"price ${f['close']:.2f} <= ${MIN_PRICE:.0f}")
    if f["median_dollar_vol"] <= MIN_DOLLAR_VOL:
        fails.append("median dollar volume under $20M")
    if not (f["close"] > f["sma50"] > f["sma200"]):
        fails.append("not close > 50-day > 200-day")
    if f["sma200_22ago"] is None or f["sma200"] <= f["sma200_22ago"]:
        fails.append("200-day average not rising")
    if f["nearness"] < 1 - MAX_OFF_HIGH:
        fails.append(f"{(1 - f['nearness']) * 100:.0f}% below 52-week high")
    if earnings_in_days is not None and 0 <= earnings_in_days <= EARNINGS_BLACKOUT_DAYS:
        fails.append(f"earnings in {earnings_in_days} trading days")
    if reported_last_night:
        fails.append("reported earnings after the prior close")
    return (not fails, fails)


def momentum_value(f: dict) -> Optional[float]:
    """Raw momentum used for cross-sectional ranking (average of 6- and 12-month)."""
    vals = [v for v in (f.get("ret_126"), f.get("ret_252")) if v is not None]
    return sum(vals) / len(vals) if vals else None


def percentile_ranks(values: dict) -> dict:
    """{ticker: raw} -> {ticker: 0..1 rank}. Tickers with None get 0."""
    known = sorted((v, t) for t, v in values.items() if v is not None)
    out = {t: 0.0 for t in values}
    n = len(known)
    for i, (_, t) in enumerate(known):
        out[t] = 1.0 if n == 1 else i / (n - 1)
    return out


def _pullback_fraction(depth_atr: float) -> float:
    if depth_atr < 0.5 or depth_atr > 6:
        return 0.0
    if depth_atr < 1.5:
        return depth_atr - 0.5
    if depth_atr <= 4:
        return 1.0
    return (6 - depth_atr) / 2


def score(f: dict, rs_rank: float, sector_strong: Optional[bool] = None,
          ai_points: float = 5.0, vetoed: bool = False) -> dict:
    """Returns {total, rating, breakdown}. A vetoed stock is AVOID whatever its score."""
    w = WEIGHTS
    b = {
        "relative_strength": w["relative_strength"] * max(0.0, min(1.0, rs_rank)),
        "nearness": w["nearness"] * max(0.0, min(1.0, (f["nearness"] - 0.75) / 0.25)),
        "pullback": w["pullback"] * _pullback_fraction(f["pullback_depth_atr"]),
        "reversal": w["reversal"] if f["reversal"] else 0.0,
        "sector": w["sector"] * (0.5 if sector_strong is None else float(sector_strong)),
        "near_ma": w["near_ma"] if f["near_ma"] else 0.0,
        "volume": w["volume"] * {"light": 1.0, "neutral": 0.5, "heavy": 0.0}[f["volume_state"]],
        "ai": max(0.0, min(float(w["ai"]), ai_points)),
    }
    b = {k: round(v, 1) for k, v in b.items()}
    total = int(round(sum(b.values())))
    return {"total": total, "rating": rating_for(total, vetoed), "breakdown": b}


def rating_for(total: int, vetoed: bool = False) -> str:
    if vetoed:
        return "AVOID"
    if total >= BUY_THRESHOLD:
        return "BUY"
    return "WATCH" if total >= WATCH_THRESHOLD else "AVOID"


def exit_plan(entry: float, atr: float, pullback_low: float) -> dict:
    """Disaster stop: the wider of the two levels, kept between 1.5 and 3 ATR away."""
    stop = min(pullback_low - 0.25 * atr, entry - STOP_ATR_BASE * atr)
    dist = max(STOP_ATR_MIN * atr, min(STOP_ATR_MAX * atr, entry - stop))
    return {
        "stop": round(entry - dist, 2),
        "target": round(entry + TARGET_ATR * atr, 2),
        "time_stop_days": TIME_STOP_DAYS,
        "stop_atr": round(dist / atr, 2),
    }


def size(entry: float, stop: float, budget: float, risk_pct: float) -> dict:
    """Whole shares: 1%-style risk sizing, capped at MAX_POSITION_PCT of budget."""
    per_share = entry - stop
    if entry <= 0 or per_share <= 0:
        return {"shares": 0, "risk_dollars": 0.0, "position_value": 0.0}
    by_risk = int((budget * risk_pct / 100) // per_share)
    by_cap = int((budget * MAX_POSITION_PCT) // entry)
    shares = max(0, min(by_risk, by_cap))
    return {
        "shares": shares,
        "risk_dollars": round(shares * per_share, 2),
        "position_value": round(shares * entry, 2),
    }
