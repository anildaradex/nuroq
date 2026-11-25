"""Market data access for the pipeline: universe snapshot, deep history, sector, earnings, quotes."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from pipeline import store

MIN_HISTORY_BARS = 280
MAX_CANDIDATES = 250
META_TTL_S = 30 * 86400

SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Basic Materials": "XLB", "Utilities": "XLU",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}
BENCHMARKS = ["SPY"] + sorted(set(SECTOR_ETF.values()))

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_meta (
    ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, quote_type TEXT, fetched_at REAL
);
CREATE TABLE IF NOT EXISTS earnings_dates (
    ticker TEXT PRIMARY KEY, earnings_date TEXT, fetched_on TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(store.db_path())
    c.executescript(_META_SCHEMA)
    return c


def trading_days_back(n: int, start: Optional[date] = None) -> date:
    d = start or date.today()
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def grouped_snapshot(day: date, max_stepback: int = 5) -> tuple[dict, str]:
    """Polygon market-wide daily bars for `day`, stepping back over holidays."""
    from data_fetcher import POLYGON_API_KEY, rate_limiter
    for _ in range(max_stepback + 1):
        rate_limiter.wait()
        url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
               f"{day.isoformat()}?adjusted=true&apiKey={POLYGON_API_KEY}")
        results = requests.get(url, timeout=30).json().get("results") or []
        if results:
            return {r["T"]: r for r in results}, day.isoformat()
        day = trading_days_back(1, day)
    return {}, day.isoformat()


def prefilter(now: dict, past: dict, limit: int = MAX_CANDIDATES) -> list:
    """Liquid common-stock-looking tickers that are up over ~6 months, strongest first."""
    out = []
    for t, r in now.items():
        if not re.fullmatch(r"[A-Z]{1,5}", t):
            continue
        c, v = r.get("c") or 0, r.get("v") or 0
        p = (past.get(t) or {}).get("c")
        if c <= 5 or c * v <= 20_000_000 or not p or c <= p:
            continue
        out.append((c / p, t))
    return [t for _, t in sorted(out, reverse=True)[:limit]]


def ticker_meta(tickers: list) -> dict:
    """{ticker: {name, sector, quote_type}} from a 30-day cache, filling gaps from yfinance."""
    import yfinance as yf
    now = time.time()
    with _conn() as c:
        rows = c.execute("SELECT ticker, name, sector, quote_type, fetched_at FROM ticker_meta").fetchall()
    cached = {r[0]: {"name": r[1], "sector": r[2], "quote_type": r[3]}
              for r in rows if now - r[4] < META_TTL_S}
    missing = [t for t in tickers if t not in cached]

    def fetch(t):
        try:
            info = yf.Ticker(t).info or {}
            return t, {"name": info.get("longName") or t, "sector": info.get("sector"),
                       "quote_type": info.get("quoteType")}
        except Exception:
            return t, None

    if missing:
        with ThreadPoolExecutor(max_workers=8) as ex:
            fetched = [(t, m) for t, m in ex.map(fetch, missing) if m]
        with _conn() as c:
            c.executemany("INSERT OR REPLACE INTO ticker_meta VALUES (?,?,?,?,?)",
                          [(t, m["name"], m["sector"], m["quote_type"], now) for t, m in fetched])
        cached.update(dict(fetched))
    return {t: cached[t] for t in tickers if t in cached}


def ensure_history(tickers: list, log=None) -> dict:
    """{ticker: bars} with ~2 years of daily bars, fetched in bulk from yfinance when short or stale."""
    import yfinance as yf
    from data_fetcher import history_cache

    last_td = trading_days_back(1).isoformat()
    full, tail = [], []
    for t in tickers:
        bars = history_cache.get(t, allow_stale=True) or []
        if len(bars) < MIN_HISTORY_BARS:
            full.append(t)
        elif bars[-1]["t"] < last_td:
            tail.append(t)

    for group, period in ((full, "2y"), (tail, "1mo")):
        for i in range(0, len(group), 100):
            chunk = group[i:i + 100]
            try:
                df = yf.download(chunk, period=period, interval="1d", auto_adjust=False,
                                 group_by="ticker", threads=True, progress=False)
            except Exception as e:
                if log:
                    log(f"⚠️ pipeline history download failed ({len(chunk)} tickers): {e}")
                continue
            for t in chunk:
                try:
                    sub = (df[t] if len(chunk) > 1 else df).dropna(subset=["Close"])
                    if len(chunk) == 1 and hasattr(sub.columns, "nlevels") and sub.columns.nlevels > 1:
                        sub = sub.droplevel(0, axis=1)
                    history_cache.store(t, [
                        {"o": float(r.Open), "h": float(r.High), "l": float(r.Low),
                         "c": float(r.Close), "v": float(r.Volume), "t": idx.strftime("%Y-%m-%d")}
                        for idx, r in sub.iterrows()
                    ])
                except Exception:
                    continue
    return {t: (history_cache.get(t, allow_stale=True) or []) for t in tickers}


def earnings_in_trading_days(ticker: str, today: Optional[date] = None) -> Optional[int]:
    """Trading days until the next earnings date (cached per day). None if unknown."""
    import yfinance as yf
    today = today or date.today()
    with _conn() as c:
        row = c.execute("SELECT earnings_date, fetched_on FROM earnings_dates WHERE ticker=?",
                        (ticker,)).fetchone()
    if row and row[1] == today.isoformat():
        e = row[0]
    else:
        try:
            cal = yf.Ticker(ticker).calendar or {}
            dates = cal.get("Earnings Date") or []
            e = dates[0].isoformat() if dates else None
        except Exception:
            e = None
        with _conn() as c:
            c.execute("INSERT OR REPLACE INTO earnings_dates VALUES (?,?,?)",
                      (ticker, e, today.isoformat()))
    if not e:
        return None
    target = date.fromisoformat(e[:10])
    if target < today:
        return None
    n, d = 0, today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Alpaca RFC-3339 timestamps carry nanoseconds; keep whole seconds."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.rstrip("Z").split(".")[0]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def live_snapshot(ticker: str) -> Optional[dict]:
    """Alpaca snapshot: {bid, ask, quote_age_s, day_open, last}. None when unavailable."""
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        return None
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/snapshot",
            params={"feed": os.getenv("ALPACA_DATA_FEED", "iex")},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=8,
        )
        if r.status_code != 200:
            return None
        j = r.json()
        q, bar, tr = j.get("latestQuote") or {}, j.get("dailyBar") or {}, j.get("latestTrade") or {}
        ts = _parse_ts(q.get("t"))
        age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None
        return {"bid": float(q.get("bp") or 0), "ask": float(q.get("ap") or 0),
                "quote_age_s": age, "day_open": float(bar.get("o") or 0),
                "last": float(tr.get("p") or 0)}
    except Exception:
        return None
