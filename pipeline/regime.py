"""Market regime gate: SPY 5-day average against its 200-day average, with a 1% band."""
from __future__ import annotations

from typing import Optional

BAND = 0.01


def evaluate(spy_bars: list, prev_on: Optional[bool]) -> dict:
    """On above +1%, off below -1%, otherwise keep the previous state.

    With no previous state the gate starts from which side of the 200-day it is on.
    """
    closes = [float(b["c"]) for b in spy_bars if b.get("c")]
    if len(closes) < 200:
        return {"on": bool(prev_on) if prev_on is not None else False,
                "ratio": None, "reason": "insufficient SPY history"}
    ratio = (sum(closes[-5:]) / 5) / (sum(closes[-200:]) / 200)
    if ratio > 1 + BAND:
        on = True
    elif ratio < 1 - BAND:
        on = False
    else:
        on = prev_on if prev_on is not None else ratio >= 1
    return {"on": on, "ratio": round(ratio, 4),
            "reason": f"SPY 5-day avg is {(ratio - 1) * 100:+.1f}% vs 200-day avg"}
