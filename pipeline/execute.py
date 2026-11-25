"""09:35 ET step: re-check each pick against live quotes, then act according to the source's mode."""
from __future__ import annotations

import time
from datetime import date
from typing import Callable, Optional

from pipeline import data, store
from pipeline.strategies import trend_pullback as tp

MODES = ("off", "notify")          # approve / auto arrive with the flattener source filter
MAX_ENTRIES_PER_DAY = 2
MAX_PER_SECTOR = 2
GAP_ATR = 1.0
MAX_SPREAD_PCT = 0.30
MAX_SPREAD_ATR = 0.10
MAX_QUOTE_AGE_S = 15
MAX_RESEARCH_AGE_S = 20 * 3600     # picks older than this are not acted on
RETRYABLE = ("spread", "stale quote", "no quote")


def entry_check(pick: dict, q: Optional[dict]) -> tuple[Optional[str], dict]:
    """Returns (skip_reason or None, measured fields). Pure: no I/O."""
    if not q or q.get("bid", 0) <= 0 or q.get("ask", 0) <= 0:
        return "no quote", {}
    atr, prev_close = pick["atr"], pick["prev_close"]
    bid, ask = q["bid"], q["ask"]
    mid = (bid + ask) / 2
    spread = ask - bid
    day_open = q.get("day_open") or mid
    m = {
        "bid": bid, "ask": ask, "mid": round(mid, 4), "day_open": day_open,
        "quote_age_s": q.get("quote_age_s"),
        "gap_atr": round((max(day_open, mid) - prev_close) / atr, 2),
        "spread_pct": round(spread / mid * 100, 3),
    }
    plan = tp.exit_plan(ask, atr, pick["pullback_low"])
    if q.get("quote_age_s") is not None and q["quote_age_s"] > MAX_QUOTE_AGE_S:
        return f"stale quote ({q['quote_age_s']:.0f}s old)", m
    if m["spread_pct"] > MAX_SPREAD_PCT or spread > MAX_SPREAD_ATR * atr:
        return f"spread too wide ({m['spread_pct']:.2f}%)", m
    if m["gap_atr"] > GAP_ATR:
        return f"gapped up {m['gap_atr']:.1f} ATR", m
    if mid < prev_close - GAP_ATR * atr or mid < pick["prev_low"]:
        return "gapped down below prior low or 1 ATR", m
    if mid <= plan["stop"]:
        return "price at or below planned stop", m
    return None, m


def run(notify: Callable = lambda m: None, log: Callable = print,
        quote_fn: Callable = data.live_snapshot, held_fn: Callable = lambda: set(),
        news_block_fn: Callable = lambda t: False, wash_sale_fn: Callable = lambda t: False,
        recheck: bool = False, today: Optional[date] = None) -> dict:
    """Processes today's picks. `recheck=True` is the 10:00 pass for quote-quality skips only."""
    import agent_config
    cfg = agent_config.get()
    mode = cfg.get("open_bell_mode", "notify")
    result = {"mode": mode, "notified": [], "skipped": {}}
    if mode == "off":
        return result

    today = today or date.today()
    run_row = store.latest_run()
    if not run_row or time.time() - run_row["created_at"] > MAX_RESEARCH_AGE_S:
        log("⚠️ Open-bell: no fresh research for today — nothing to do.")
        return result
    run_date = run_row["run_date"]
    picks = store.picks(run_date)

    def skip(p, reason, fields=None):
        store.update_row("open_bell_picks", run_date, p["ticker"],
                         {**(fields or {}), "status": "skipped", "skip_reason": reason,
                          "checked_at": time.time(), "entry_date": today.isoformat()})
        result["skipped"][p["ticker"]] = reason

    todo = [p for p in picks if (p["status"] == "skipped" and (p["skip_reason"] or "").startswith(RETRYABLE))
            ] if recheck else [p for p in picks if p["status"] == "pending"]
    if not todo:
        return result

    if not run_row["regime_on"]:
        for p in todo:
            skip(p, "market regime off")
        notify(f"🚦 Open-bell: market regime is OFF — {len(todo)} pick(s) not acted on.")
        return result

    held = {t.upper() for t in (held_fn() or set())}
    done = [p for p in picks if p["status"] == "notified"]
    sector_count: dict = {}
    for p in done:
        sector_count[p["sector"]] = sector_count.get(p["sector"], 0) + 1
    accepted = len(done)

    for p in todo:
        t = p["ticker"]
        if accepted >= MAX_ENTRIES_PER_DAY:
            skip(p, f"daily entry cap ({MAX_ENTRIES_PER_DAY}) reached"); continue
        if t in held:
            skip(p, "already held"); continue
        if p["sector"] and sector_count.get(p["sector"], 0) >= MAX_PER_SECTOR:
            skip(p, f"sector cap ({p['sector']})"); continue
        if news_block_fn(t):
            skip(p, "blocking negative headline"); continue
        if wash_sale_fn(t):
            skip(p, "wash-sale risk"); continue

        reason, m = entry_check(p, quote_fn(t))
        if reason:
            skip(p, reason, m); continue

        plan = tp.exit_plan(m["ask"], p["atr"], p["pullback_low"])
        sz = tp.size(m["ask"], plan["stop"], float(cfg["budget"]), float(cfg["risk_per_trade_pct"]))
        if sz["shares"] < 1:
            skip(p, "sized to 0 shares", m); continue

        store.update_row("open_bell_picks", run_date, t, {
            **m, "status": "notified", "skip_reason": None, "checked_at": time.time(),
            "entry_date": today.isoformat(), "ref_entry": m["ask"], "stop": plan["stop"],
            "target": plan["target"], "shares": sz["shares"], "risk_dollars": sz["risk_dollars"],
        })
        accepted += 1
        sector_count[p["sector"]] = sector_count.get(p["sector"], 0) + 1
        result["notified"].append(t)
        notify(
            f"🔔 Open-bell pick · {t} (score {p['score']}, #{p['rank']})\n"
            f"Would buy {sz['shares']} sh at ${m['ask']:.2f} (${sz['position_value']:,.0f})\n"
            f"Stop ${plan['stop']:.2f} ({plan['stop_atr']} ATR) · Target ${plan['target']:.2f} · "
            f"Time stop {plan['time_stop_days']} days\n"
            f"Risk ${sz['risk_dollars']:,.0f} · Gap {m['gap_atr']:+.1f} ATR · Spread {m['spread_pct']:.2f}%\n"
            f"Notify only — no order placed."
        )

    if result["skipped"] and not recheck:
        lines = "\n".join(f"• {t}: {r}" for t, r in result["skipped"].items())
        notify(f"⏭️ Open-bell skipped {len(result['skipped'])} pick(s):\n{lines}")
    log(f"🔔 Open-bell ({'recheck' if recheck else '09:35'}): "
        f"{len(result['notified'])} notified, {len(result['skipped'])} skipped.")
    return result
