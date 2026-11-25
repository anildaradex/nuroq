"""Forward log: fills in forward returns and the simulated bracket outcome for past picks."""
from __future__ import annotations

from typing import Callable, Optional

from pipeline import data, store
from pipeline.strategies import trend_pullback as tp

HORIZONS = (1, 5, 10)


def forward_returns(bars: list, entry_date: str, entry_price: float) -> dict:
    """Close-to-entry returns N trading days after the entry day. Missing horizons are omitted."""
    idx = next((i for i, b in enumerate(bars) if b["t"] >= entry_date), None)
    out = {}
    if idx is None or entry_price <= 0:
        return out
    for n in HORIZONS:
        if idx + n < len(bars):
            out[f"fwd_{n}"] = round(bars[idx + n]["c"] / entry_price - 1, 5)
    return out


def simulate_bracket(bars: list, entry_date: str, entry: float, stop: float, target: float,
                     time_stop_days: int = tp.TIME_STOP_DAYS) -> Optional[dict]:
    """Stop, target or time stop, whichever comes first. Gaps fill at the open; stop wins a shared bar.

    Returns None until the trade has an outcome.
    """
    idx = next((i for i, b in enumerate(bars) if b["t"] >= entry_date), None)
    risk = entry - stop
    if idx is None or risk <= 0:
        return None
    lo, hi = entry, entry
    for day, b in enumerate(bars[idx:idx + time_stop_days + 1]):
        # the entry-day bar includes the minutes before 09:35; accepted as an approximation
        lo, hi = min(lo, b["l"]), max(hi, b["h"])
        exit_px = reason = None
        if b["l"] <= stop:
            exit_px, reason = (min(b["o"], stop) if day else stop), "stop"
        elif b["h"] >= target:
            exit_px, reason = (max(b["o"], target) if day else target), "target"
        elif day == time_stop_days:
            exit_px, reason = b["c"], "time"
        if reason:
            return {"exit_date": b["t"], "exit_reason": reason,
                    "r_multiple": round((exit_px - entry) / risk, 3),
                    "mae_r": round((lo - entry) / risk, 3),
                    "mfe_r": round((hi - entry) / risk, 3)}
    return None


def update(log: Callable = print, history_fn: Callable = data.ensure_history) -> dict:
    """Fills missing forward fields for every stored pick and eligible row."""
    with store.connect() as c:
        pick_rows = [dict(r) for r in c.execute(
            "SELECT * FROM open_bell_picks WHERE entry_date IS NOT NULL "
            "AND (fwd_10 IS NULL OR (status='notified' AND exit_reason IS NULL))").fetchall()]
        elig_rows = [dict(r) for r in c.execute(
            "SELECT run_date, ticker FROM open_bell_eligible WHERE fwd_10 IS NULL").fetchall()]
    tickers = sorted({r["ticker"] for r in pick_rows} | {r["ticker"] for r in elig_rows})
    if not tickers:
        return {"picks": 0, "eligible": 0}
    hist = history_fn(tickers, log)

    for r in pick_rows:
        bars = hist.get(r["ticker"]) or []
        d0 = next((b for b in bars if b["t"] >= r["entry_date"]), None)
        price = r["ref_entry"] or r["ask"] or (d0 or {}).get("o")
        if not price:
            continue
        fields = forward_returns(bars, r["entry_date"], price)
        if r["status"] == "notified" and r["stop"] and r["target"]:
            fields.update(simulate_bracket(bars, r["entry_date"], r["ref_entry"],
                                           r["stop"], r["target"]) or {})
        if fields:
            store.update_row("open_bell_picks", r["run_date"], r["ticker"], fields)

    for r in elig_rows:
        bars = hist.get(r["ticker"]) or []
        d0 = next((b for b in bars if b["t"] > r["run_date"]), None)   # first session after the run
        if d0:
            fields = forward_returns(bars, d0["t"], d0["o"])
            if fields:
                store.update_row("open_bell_eligible", r["run_date"], r["ticker"], fields)

    log(f"📒 Open-bell tracker: updated {len(pick_rows)} pick(s), {len(elig_rows)} eligible row(s).")
    return {"picks": len(pick_rows), "eligible": len(elig_rows)}
