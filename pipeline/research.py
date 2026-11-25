"""Nightly research: universe -> features -> eligibility -> score -> AI flags -> stored picks."""
from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from pipeline import ai_flags, data, features, regime, store
from pipeline.strategies import trend_pullback as tp

AI_TOP_N = 30


def rank_and_score(feats: dict, sectors: dict, sector_strong: dict,
                   ai: Optional[dict] = None) -> list:
    """feats: {ticker: features} for ELIGIBLE names. Returns rows sorted by score, best first."""
    ai = ai or {}
    ranks = tp.percentile_ranks({t: tp.momentum_value(f) for t, f in feats.items()})
    rows = []
    for t, f in feats.items():
        sec = sectors.get(t)
        strong = sector_strong.get(sec) if sec else None
        a = ai.get(t)
        no_ai = tp.score(f, ranks[t], strong)
        full = tp.score(f, ranks[t], strong, a["points"], a["vetoed"]) if a else no_ai
        rows.append({
            "ticker": t, "sector": sec, "features": f, "ai": a,
            "score": full["total"], "rating": full["rating"], "breakdown": full["breakdown"],
            "score_no_ai": no_ai["total"], "rating_no_ai": no_ai["rating"],
        })
    rows.sort(key=lambda r: (-r["score"], r["ticker"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def sector_strength(history: dict) -> dict:
    """{sector: True/False/None}: sector ETF ahead of SPY over ~6 months (skip-month)."""
    def ret(sym):
        closes = [b["c"] for b in history.get(sym, [])]
        return features.skip_month_return(closes, 126)
    spy = ret("SPY")
    out = {}
    for sector, etf in data.SECTOR_ETF.items():
        r = ret(etf)
        out[sector] = None if spy is None or r is None else r > spy
    return out


def run(ai_run: Optional[Callable] = None, notify: Callable = lambda m: None,
        log: Callable = print, today: Optional[date] = None) -> dict:
    """Full nightly run. Stores results and returns a summary dict."""
    today = today or date.today()
    snap, run_date = data.grouped_snapshot(data.trading_days_back(1, today))
    if not snap:
        raise RuntimeError("no market snapshot available")
    past, _ = data.grouped_snapshot(data.trading_days_back(126, date.fromisoformat(run_date)))
    candidates = data.prefilter(snap, past)
    log(f"🔬 [Open-bell research] {run_date}: {len(candidates)} liquid uptrend candidates.")

    meta = data.ticker_meta(candidates)
    stocks = [t for t in candidates if (meta.get(t) or {}).get("quote_type") == "EQUITY"]
    history = data.ensure_history(stocks + data.BENCHMARKS, log)

    prev_on = store.get_state("regime_on")
    history = {t: [b for b in bars if b["t"] <= run_date] for t, bars in history.items()}
    reg = regime.evaluate(history.get("SPY", []), prev_on)
    store.set_state("regime_on", reg["on"])

    feats = {}
    for t in stocks:
        f = features.compute([b for b in history.get(t, []) if b["t"] <= run_date])
        if not f or f["as_of"] != run_date:
            continue
        ok, _ = tp.eligible(f)
        if not ok:
            continue
        days = data.earnings_in_trading_days(t, today)
        ok, _ = tp.eligible(f, earnings_in_days=days)
        if ok:
            feats[t] = f

    sectors = {t: meta[t].get("sector") for t in feats}
    strong = sector_strength(history)
    rows = rank_and_score(feats, sectors, strong)

    ai = {}
    if ai_run is not None:
        from news_engine import check_news_for_crossing
        for r in rows[:AI_TOP_N]:
            t = r["ticker"]
            try:
                news = check_news_for_crossing(t)
                blocked = bool(news and news.get("classification") == "NEGATIVE_BLOCK")
            except Exception:
                blocked = False
            ai[t] = ai_flags.assess(t, meta[t].get("name") or t, ai_run, today, blocked)
        rows = rank_and_score(feats, sectors, strong, ai)

    picks = [r for r in rows if r["rating"] == "BUY"]
    store.save_run(run_date, reg, len(candidates), rows, picks)

    vetoed = [r["ticker"] for r in rows if r["ai"] and r["ai"]["vetoed"]]
    summary = {"run_date": run_date, "regime": reg, "candidates": len(candidates),
               "eligible": len(rows), "buys": [r["ticker"] for r in picks],
               "vetoed": vetoed, "rows": rows}
    msg = (f"🔬 Open-bell research {run_date}: {len(rows)} eligible, {len(picks)} BUY"
           f"{' (' + ', '.join(summary['buys'][:8]) + ')' if picks else ''}. "
           f"Regime {'ON' if reg['on'] else 'OFF'} — {reg['reason']}."
           f"{' Vetoed: ' + ', '.join(vetoed) + '.' if vetoed else ''}")
    log(msg)
    notify(msg)
    return summary
