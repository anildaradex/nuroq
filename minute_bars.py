"""
minute_bars.py — 1-minute OHLCV fetcher + persistent cache.

Used by:
  • backtest/replay.py — historical replay
  • day_trader.py     — intraday indicator inputs (when live)
  • premarket scanner — gap + premkt-volume calculations

Pattern mirrors data_fetcher.HistoryCache (daily bars), so it shares the
PolygonRateLimiter + retry semantics. Bars are stored in their own
`minute_bars` SQLite table to keep the daily `price_history` table small.

Polygon endpoint:
  GET /v2/aggs/ticker/{T}/range/1/minute/{from}/{to}
       ?adjusted=true&sort=asc&limit=50000&apiKey=...

Polygon returns ms-epoch timestamps; we normalize to integer unix-seconds at
the bar OPEN (Polygon's convention — the bar timestamped 09:30:00 ET covers
09:30:00 → 09:30:59).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import requests
from dotenv import load_dotenv

load_dotenv()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
DB_PATH = os.getenv("NUROQ_DB_PATH", "nuroq.db")

# Reuse the existing rate limiter — same Polygon account, same 5/min budget.
try:
    from data_fetcher import rate_limiter as _polygon_rate_limiter  # type: ignore
except Exception:
    _polygon_rate_limiter = None


# ---------------------------------------------------------------------------
# Bar dataclass — used everywhere downstream (backtest, indicators, live)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """One 1-minute OHLCV bar. ts is unix-seconds at bar OPEN (ET)."""
    ticker: str
    ts:     int       # unix seconds at bar OPEN
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float
    vwap:   float     # Polygon's volume-weighted price for this bar

    @property
    def dt_et(self) -> datetime:
        """Bar open as a tz-aware ET datetime."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(self.ts, tz=ZoneInfo("America/New_York"))
        except Exception:
            return datetime.fromtimestamp(self.ts)

    @property
    def minute_of_day(self) -> int:
        """0..1439 — minute index within the local ET day."""
        d = self.dt_et
        return d.hour * 60 + d.minute


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------

class MinuteBarCache:
    """Persistent 1-minute OHLCV cache. Keyed by (ticker, ts)."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS minute_bars (
                    ticker TEXT NOT NULL,
                    ts     INTEGER NOT NULL,
                    open   REAL,
                    high   REAL,
                    low    REAL,
                    close  REAL,
                    volume REAL,
                    vwap   REAL,
                    PRIMARY KEY (ticker, ts)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_minute_bars_ticker_ts "
                "ON minute_bars(ticker, ts)"
            )
            conn.execute("PRAGMA journal_mode=WAL")

    # -- read ----------------------------------------------------------------

    def get_session(self, ticker: str, session_date: str,
                    include_premarket: bool = True) -> list[Bar]:
        """Return all stored bars for ticker on session_date (YYYY-MM-DD).
        Regular session = 09:30–16:00 ET (390 bars). Premarket = 04:00–09:30.
        Returns [] if nothing cached. Bars are ordered ascending by ts."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/New_York")
        except Exception:
            tz = None
        d = date.fromisoformat(session_date)
        if tz:
            start_local = datetime(d.year, d.month, d.day, 4 if include_premarket else 9,
                                   0 if include_premarket else 30, tzinfo=tz)
            end_local = datetime(d.year, d.month, d.day, 20, 0, tzinfo=tz)
        else:
            start_local = datetime(d.year, d.month, d.day, 4 if include_premarket else 9,
                                   0 if include_premarket else 30)
            end_local = datetime(d.year, d.month, d.day, 20, 0)
        start_ts = int(start_local.timestamp())
        end_ts = int(end_local.timestamp())
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume, vwap FROM minute_bars "
                "WHERE ticker=? AND ts >= ? AND ts < ? ORDER BY ts ASC",
                (ticker, start_ts, end_ts),
            ).fetchall()
        return [Bar(ticker=ticker, ts=r[0], open=r[1], high=r[2], low=r[3],
                    close=r[4], volume=r[5], vwap=r[6] if r[6] is not None else r[4])
                for r in rows]

    def has_session(self, ticker: str, session_date: str) -> bool:
        """True if we have at least one regular-session bar cached for this
        ticker/date — used to decide whether to re-fetch."""
        bars = self.get_session(ticker, session_date, include_premarket=False)
        return len(bars) > 0

    def stored_sessions(self, ticker: str) -> list[str]:
        """List of YYYY-MM-DD dates we have any bars cached for."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') FROM minute_bars "
                "WHERE ticker=? ORDER BY 1 ASC",
                (ticker,),
            ).fetchall()
        return [r[0] for r in rows]

    # -- write ---------------------------------------------------------------

    def store(self, ticker: str, raw_bars: Iterable[dict]) -> int:
        """Upsert Polygon-format bar dicts. Returns rows inserted."""
        rows = []
        for b in raw_bars:
            t = b.get("t")
            if isinstance(t, (int, float)) and t > 1e10:   # ms → s
                ts = int(t // 1000)
            else:
                ts = int(t) if t is not None else 0
            if ts <= 0:
                continue
            rows.append((
                ticker, ts,
                b.get("o"), b.get("h"), b.get("l"), b.get("c"),
                b.get("v"), b.get("vw"),
            ))
        if not rows:
            return 0
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO minute_bars "
                "(ticker, ts, open, high, low, close, volume, vwap) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)


# ---------------------------------------------------------------------------
# Polygon fetcher
# ---------------------------------------------------------------------------

def _fetch_minute_bars_from_polygon(ticker: str, start_date: str, end_date: str,
                                    logger=None) -> list:
    """Fetch all 1-min bars in [start_date, end_date] inclusive from Polygon.
    Handles pagination via Polygon's next_url. Returns raw bar dicts."""
    if not POLYGON_API_KEY:
        if logger:
            logger.log("⚠️ POLYGON_API_KEY missing — cannot fetch minute bars.",
                       level="ERROR")
        return []

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute"
        f"/{start_date}/{end_date}?adjusted=true&sort=asc&limit=50000"
        f"&apiKey={POLYGON_API_KEY}"
    )
    all_bars: list = []
    page = 0
    while url and page < 20:                   # hard cap on pagination depth
        page += 1
        if _polygon_rate_limiter is not None:
            _polygon_rate_limiter.wait(logger)
        try:
            resp = requests.get(url, timeout=20).json()
        except Exception as e:
            if logger:
                logger.log(f"⚠️ minute_bars fetch error [{ticker}]: {e}", level="ERROR")
            return all_bars
        results = resp.get("results") or []
        all_bars.extend(results)
        next_url = resp.get("next_url")
        if next_url:
            # Polygon paginated URLs require the API key appended.
            sep = "&" if "?" in next_url else "?"
            url = f"{next_url}{sep}apiKey={POLYGON_API_KEY}"
        else:
            url = None
    if logger:
        logger.log(f"📦 minute_bars [{ticker}] {start_date}→{end_date}: "
                   f"{len(all_bars)} bars in {page} page(s)")
    return all_bars


# ---------------------------------------------------------------------------
# Cache-first public API
# ---------------------------------------------------------------------------

minute_bar_cache = MinuteBarCache(db_path=DB_PATH)


def get_minute_bars(ticker: str, session_date: str,
                    include_premarket: bool = True,
                    force_refresh: bool = False,
                    logger=None) -> list[Bar]:
    """Cache-first read of one session of 1-min bars for ticker.

    session_date — 'YYYY-MM-DD'. include_premarket includes 04:00–09:30 ET.
    force_refresh bypasses the cache and fetches fresh (still stores result)."""
    if not force_refresh and minute_bar_cache.has_session(ticker, session_date):
        return minute_bar_cache.get_session(ticker, session_date,
                                            include_premarket=include_premarket)
    raw = _fetch_minute_bars_from_polygon(ticker, session_date, session_date,
                                          logger=logger)
    minute_bar_cache.store(ticker, raw)
    return minute_bar_cache.get_session(ticker, session_date,
                                        include_premarket=include_premarket)


def get_minute_bars_range(ticker: str, start_date: str, end_date: str,
                          logger=None) -> dict[str, list[Bar]]:
    """Cache-first read of [start_date, end_date] inclusive. Polygon returns
    multi-day in one call, so we fetch once per missing-range chunk.
    Returns {session_date: [Bar, ...]} for each weekday in the range that
    has data."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    weekdays = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            weekdays.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    # Which sessions are missing?
    missing = [s for s in weekdays
               if not minute_bar_cache.has_session(ticker, s)]
    if missing:
        # Fetch the whole missing range in ONE Polygon call (paginated).
        # Polygon returns all weekdays in [first, last], saving us N round-trips.
        raw = _fetch_minute_bars_from_polygon(ticker, missing[0], missing[-1],
                                              logger=logger)
        minute_bar_cache.store(ticker, raw)

    out: dict[str, list[Bar]] = {}
    for s in weekdays:
        bars = minute_bar_cache.get_session(ticker, s, include_premarket=True)
        if bars:
            out[s] = bars
    return out
