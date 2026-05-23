"""
data_fetcher.py — NuroQ Data Layer
Handles all external API interactions: Polygon.io, yfinance, StockTwits.
Includes rate limiting, in-memory caching, SQLite history cache, and async bulk fetching.
"""

import os
import time
import sqlite3
import requests
import threading
import asyncio
import concurrent.futures
import yfinance as yf
from datetime import date, timedelta
from typing import Optional
from dotenv import load_dotenv
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

load_dotenv()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
DB_PATH = os.getenv("NUROQ_DB_PATH", "nuroq.db")


# ---------------------------------------------------------------------------
# Rate Limiter & In-Memory Cache
# ---------------------------------------------------------------------------

class PolygonRateLimiter:
    """Thread-safe rate limiter for Polygon Free Tier (5 req/min)."""
    def __init__(self, max_per_min: int = 5):
        self.max_per_min = max_per_min
        self.requests: list = []
        self.lock = threading.Lock()

    def wait(self, logger=None) -> None:
        """
        Reserve a request slot, sleeping outside the lock so concurrent workers
        can still queue up. Loops in case another thread filled the slot while
        this one was sleeping.
        """
        while True:
            with self.lock:
                now = time.time()
                self.requests = [r for r in self.requests if now - r < 60]
                if len(self.requests) < self.max_per_min:
                    self.requests.append(now)
                    return
                wait_time = 60 - (now - self.requests[0]) + 0.5
            # Sleep without holding the lock so other workers can compute their own wait.
            if logger and wait_time > 0:
                logger.log(f"⏳ Rate Limiter: Pausing {round(wait_time, 1)}s to respect Polygon limit.")
            time.sleep(max(wait_time, 0.1))


class AppCache:
    """Simple TTL-based in-memory cache."""
    def __init__(self, ttl_seconds: int = 3600):
        self.cache: dict = {}
        self.ttl = ttl_seconds

    def get(self, key: str):
        if key in self.cache:
            val, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return val
        return None

    def set(self, key: str, val) -> None:
        self.cache[key] = (val, time.time())


# ---------------------------------------------------------------------------
# SQLite Price History Cache
# ---------------------------------------------------------------------------

class HistoryCache:
    """
    Persistent OHLCV cache backed by nuroq.db (price_history table).

    Cache logic:
    - Cache HIT:  rows exist AND most recent bar date >= last trading day → return from DB (~5ms)
    - Cache MISS: fetch only the missing bars from Polygon (usually 1 bar), store, return
    - First run:  no rows → fetch full 100-day block once, store permanently
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_table()
        self._lock = threading.Lock()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    ticker TEXT NOT NULL,
                    date   TEXT NOT NULL,
                    open   REAL,
                    high   REAL,
                    low    REAL,
                    close  REAL,
                    volume REAL,
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")  # Safe concurrent writes

    def _get_last_trading_day(self) -> str:
        """Returns the most recent completed trading day (Mon-Fri)."""
        d = date.today()
        # If today is a weekday and before market close we still want yesterday
        # So we always look back at least 1 day
        d -= timedelta(days=1)
        while d.weekday() >= 5:  # Skip weekends
            d -= timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    def get(self, ticker: str, allow_stale: bool = False) -> Optional[list]:
        """
        Returns cached OHLCV bars if fresh, else None.
        Fresh = most recent stored bar date >= last trading day (unless allow_stale is True).
        """
        last_td = self._get_last_trading_day()
        with sqlite3.connect(self.db_path) as conn:
            # Check if we have a recent enough bar
            row = conn.execute(
                "SELECT MAX(date) FROM price_history WHERE ticker = ?", (ticker,)
            ).fetchone()
            if not row or not row[0]:
                return None  # No data at all
            if not allow_stale and row[0] < last_td:
                return None  # Stale data — needs refresh

            # Return all stored bars for this ticker, sorted ascending
            rows = conn.execute(
                "SELECT open, high, low, close, volume, date FROM price_history "
                "WHERE ticker = ? ORDER BY date ASC", (ticker,)
            ).fetchall()

        # Convert to the same dict format Polygon returns
        return [{"o": r[0], "h": r[1], "l": r[2], "c": r[3], "v": r[4], "t": r[5]} for r in rows]

    def get_latest_date(self, ticker: str) -> Optional[str]:
        """Returns the most recent stored date for a ticker, or None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM price_history WHERE ticker = ?", (ticker,)
            ).fetchone()
        return row[0] if row and row[0] else None

    def store(self, ticker: str, bars: list):
        """Upserts a list of OHLCV bar dicts into the price_history table."""
        if not bars:
            return
        rows = []
        for b in bars:
            # Polygon returns timestamp in ms; convert to date string
            bar_date = b.get("t")
            if isinstance(bar_date, (int, float)):
                bar_date = date.fromtimestamp(bar_date / 1000).strftime("%Y-%m-%d")
            rows.append((
                ticker,
                bar_date,
                b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v")
            ))
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO price_history "
                    "(ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                    rows
                )


# Module-level singletons
rate_limiter    = PolygonRateLimiter()
news_cache      = AppCache(ttl_seconds=7200)   # 2 hours
funds_cache     = AppCache(ttl_seconds=14400)  # 4 hours
history_cache   = HistoryCache(db_path=DB_PATH)


# ---------------------------------------------------------------------------
# Polygon.io Fetchers
# ---------------------------------------------------------------------------

def get_polygon_news(ticker: str, logger=None) -> Optional[str]:
    """Fetches latest news headlines from Polygon for a given ticker."""
    cached = news_cache.get(ticker)
    if cached:
        return cached

    rate_limiter.wait(logger)
    url = (
        f"https://api.polygon.io/v2/reference/news"
        f"?ticker={ticker}&limit=3&apiKey={POLYGON_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=10).json()
        if "results" in resp:
            news_summary = ""
            for item in resp["results"]:
                title = item.get("title", "No Title")
                summary = item.get("description", "")[:150]
                news_summary += f"- {title}: {summary}...\n"
            if news_summary:
                news_cache.set(ticker, news_summary)
            return news_summary if news_summary else None
    except Exception as e:
        if logger:
            logger.log(f"⚠️ Polygon News Error [{ticker}]: {e}", level="WARNING")
    return None


_POLYGON_RATE_LIMIT_RETRIES = 3
_POLYGON_RATE_LIMIT_BACKOFF = 60  # seconds; doubled on each retry up to max


def _fetch_bars_from_polygon(ticker: str, start_date: str, end_date: str, logger=None) -> list:
    """
    Fetches OHLCV bars from Polygon for a given date range.
    Bounded retry on rate-limit responses (3 attempts with exponential backoff),
    no recursion.
    """
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
        f"/{start_date}/{end_date}?adjusted=true&sort=asc&apiKey={POLYGON_API_KEY}"
    )
    backoff = _POLYGON_RATE_LIMIT_BACKOFF
    for attempt in range(1, _POLYGON_RATE_LIMIT_RETRIES + 1):
        rate_limiter.wait(logger)
        try:
            response = requests.get(url, timeout=15).json()
        except Exception as e:
            if logger:
                logger.log(f"⚠️ History Fetch Error [{ticker}]: {e}", level="ERROR")
            raise

        if "results" in response:
            return response["results"]

        status = response.get("status", "")
        body = str(response).lower()
        is_rate_limited = status == "ERROR" or "limit" in body or "exceeded" in body

        if not is_rate_limited:
            if logger:
                logger.log(f"⚠️ No OHLCV for {ticker}: {status}", level="WARNING")
            return []

        if attempt >= _POLYGON_RATE_LIMIT_RETRIES:
            if logger:
                logger.log(f"🛑 Polygon rate-limit retries exhausted for {ticker} after {attempt} attempts.",
                           level="ERROR")
            return []
        if logger:
            logger.log(f"🛑 Polygon rate-limited for {ticker} (attempt {attempt}/{_POLYGON_RATE_LIMIT_RETRIES}). "
                       f"Backing off {backoff}s.", level="WARNING")
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)
    return []


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((requests.exceptions.RequestException, ConnectionError))
)
def get_full_history(ticker: str, logger=None) -> list:
    """
    Returns ~100 days of OHLCV data for a ticker.

    Cache-first strategy:
    1. Check SQLite cache → if fresh (bars up to yesterday), return instantly.
    2. If stale → fetch only missing bars from Polygon and append.
    3. If empty → fetch full 100-day block from Polygon and store.
    """
    t0 = time.time()

    # 1. Cache HIT
    cached = history_cache.get(ticker)
    if cached:
        if logger:
            elapsed = round(time.time() - t0, 3)
            logger.log(f"📦 Cache HIT [{ticker}] — {len(cached)} bars in {elapsed}s")
        return cached

    # 2. Incremental fetch (stale data — fetch only missing bars)
    latest_date = history_cache.get_latest_date(ticker)
    end_date    = date.today().strftime("%Y-%m-%d")

    if latest_date:
        # Only fetch from the day after our last stored bar
        start_date = (date.fromisoformat(latest_date) + timedelta(days=1)).strftime("%Y-%m-%d")
        if logger:
            logger.log(f"🔄 Incremental fetch [{ticker}] from {start_date} → {end_date}")
        new_bars = _fetch_bars_from_polygon(ticker, start_date, end_date, logger)
        if new_bars:
            history_cache.store(ticker, new_bars)
        # Return full refreshed cache, fallback to stale if fetch returned 0 bars
        return history_cache.get(ticker, allow_stale=True) or []
    else:
        # 3. Full fetch (first time this ticker is seen)
        start_date = (date.today() - timedelta(days=140)).strftime("%Y-%m-%d")
        if logger:
            logger.log(f"🌐 Full fetch [{ticker}] — first time in cache")
        bars = _fetch_bars_from_polygon(ticker, start_date, end_date, logger)
        if bars:
            history_cache.store(ticker, bars)
        return bars


# ---------------------------------------------------------------------------
# yfinance Fetchers (sync + async)
# ---------------------------------------------------------------------------

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(Exception)
)
def get_fundamentals(ticker: str, logger=None) -> dict:
    """Fetches fundamental data via yfinance with Polygon news fallback."""
    cached = funds_cache.get(ticker)
    if cached:
        return cached

    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        pe_ratio   = info.get("trailingPE", "N/A")
        forward_pe = info.get("forwardPE", "N/A")
        market_cap = info.get("marketCap", "N/A")
        rev_growth = info.get("revenueGrowth", "N/A")

        news_summary = ""
        try:
            for n in stock.news[:3]:
                news_summary += f"- {n.get('title', '')}\n"
        except Exception:
            pass

        if not news_summary.strip():
            poly_news = get_polygon_news(ticker, logger)
            if poly_news:
                news_summary = poly_news

        data = {
            "name":     info.get("longName", ticker),
            "industry": info.get("industry", "N/A"),
            "pe":       pe_ratio,
            "f_pe":     forward_pe,
            "cap":      market_cap,
            "growth":   rev_growth,
            "news":     news_summary if news_summary.strip() else "No recent news found.",
        }
        funds_cache.set(ticker, data)
        if logger:
            logger.log(f"📊 Fundamentals [{ticker}]: P/E={pe_ratio}, Growth={rev_growth}")
        return data

    except Exception as e:
        if logger:
            logger.log(f"⚠️ Fundamental Fetch Error [{ticker}]: {e}", level="ERROR")
        return {"name": ticker, "industry": "N/A", "pe": "N/A", "f_pe": "N/A", "growth": "N/A", "news": "N/A"}


async def get_fundamentals_batch_async(tickers: list, logger=None) -> dict:
    """
    Async batch fetcher for yfinance fundamentals.
    Runs get_fundamentals in a thread pool to avoid blocking the event loop.
    Uses 4 workers to stay under yfinance throttle thresholds.
    """
    loop = asyncio.get_event_loop()
    results = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            ticker: loop.run_in_executor(executor, get_fundamentals, ticker, logger)
            for ticker in tickers
        }
        for ticker, future in futures.items():
            try:
                results[ticker] = await future
            except Exception as e:
                if logger:
                    logger.log(f"⚠️ Async fundamentals error [{ticker}]: {e}", level="ERROR")
                results[ticker] = {"name": ticker, "industry": "N/A", "pe": "N/A", "f_pe": "N/A", "growth": "N/A", "news": "N/A"}

    return results


async def get_history_batch_async(tickers: list, logger=None, skip_stale: bool = False) -> dict:
    """
    Async batch pre-fetcher for OHLCV price history.
    Cache-first: most tickers return in ~5ms from SQLite.
    Only tickers with stale/missing cache hit Polygon (usually 0-5 per scan after day 1).

    skip_stale=True: Used by scan_market when the bulk grouped snapshot already has today's
    bar. In that mode, stale tickers return the stale cache immediately without hitting
    Polygon — the caller (scan_market) injects the current bar from the bulk snapshot instead.
    This eliminates all per-ticker incremental Polygon calls during a market scan.
    """
    loop = asyncio.get_event_loop()
    results = {}
    cache_hits = 0
    skipped = 0

    def _fetch_one(ticker):
        nonlocal cache_hits, skipped
        # Fast path: fresh cache → no API call needed
        cached = history_cache.get(ticker)
        if cached:
            cache_hits += 1
            return cached
        # skip_stale mode: return stale data immediately, caller will inject latest bar
        if skip_stale:
            stale = history_cache.get(ticker, allow_stale=True)
            if stale:
                skipped += 1
                return stale
        # Slow path: incremental or full fetch from Polygon
        return get_full_history(ticker, logger)

    # max_workers=1 ensures Polygon calls are serialized and respect the rate limiter.
    # On subsequent runs with skip_stale=True, all tickers return from SQLite (~5ms each).
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            ticker: loop.run_in_executor(executor, _fetch_one, ticker)
            for ticker in tickers
        }
        for ticker, future in futures.items():
            try:
                results[ticker] = await future
            except Exception as e:
                if logger:
                    logger.log(f"⚠️ Async history error [{ticker}]: {e} - Falling back to stale cache.", level="ERROR")
                results[ticker] = history_cache.get(ticker, allow_stale=True) or []

    if logger:
        logger.log(f"📦 History batch: {cache_hits} cache hits, {skipped} stale-skipped, "
                   f"{len(tickers) - cache_hits - skipped} Polygon fetches")
    return results


# ---------------------------------------------------------------------------
# Risk & Sentiment
# ---------------------------------------------------------------------------

@retry(wait=wait_exponential(multiplier=1, min=1, max=5), stop=stop_after_attempt(2))
def get_earnings_risk(ticker: str) -> dict:
    """Returns True if earnings fall within the next 5 trading days."""
    try:
        s = yf.Ticker(ticker)
        cal = s.calendar
        if not cal or "Earnings Date" not in cal:
            return {"days": 99, "risk": False}
        e_date = cal["Earnings Date"][0]
        days_diff = (e_date - date.today()).days
        return {"days": days_diff, "risk": 0 <= days_diff <= 5}
    except Exception:
        return {"days": 99, "risk": False}


@retry(wait=wait_exponential(multiplier=1, min=1, max=5), stop=stop_after_attempt(2))
def get_sentiment(ticker: str) -> str:
    """Returns crowd sentiment (BULLISH/BEARISH/NEUTRAL) from StockTwits."""
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        resp = requests.get(url, timeout=5).json()
        msgs = resp.get("messages", [])
        bulls = sum(1 for m in msgs if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bears = sum(1 for m in msgs if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total = bulls + bears
        if total == 0:
            return "NEUTRAL"
        bull_pct = (bulls / total) * 100
        return "BULLISH" if bull_pct > 60 else ("BEARISH" if bull_pct < 40 else "NEUTRAL")
    except Exception:
        return "NEUTRAL"
