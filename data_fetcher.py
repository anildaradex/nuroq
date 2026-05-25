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


# ---------------------------------------------------------------------------
# SQLite Fundamentals Cache (Phase 1 of ARCHITECTURE.md rebuild)
# ---------------------------------------------------------------------------

class FundamentalsCache:
    """
    Persistent yfinance fundamentals cache. Survives restarts so the overnight
    research cycle (Tier 1) can populate it once and the live agent (Tier 3)
    can read it without re-fetching during market hours.
    """

    def __init__(self, db_path: str = DB_PATH, ttl_hours: int = 24):
        self.db_path = db_path
        self.ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fundamentals_cache (
                    ticker     TEXT PRIMARY KEY,
                    name       TEXT,
                    industry   TEXT,
                    pe         TEXT,
                    f_pe       TEXT,
                    cap        TEXT,
                    growth     TEXT,
                    news       TEXT,
                    fetched_at REAL NOT NULL
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")

    def get(self, ticker: str) -> Optional[dict]:
        """Returns fresh cached fundamentals or None if stale/missing."""
        cutoff = time.time() - self.ttl_seconds
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT name, industry, pe, f_pe, cap, growth, news, fetched_at "
                "FROM fundamentals_cache WHERE ticker = ? AND fetched_at >= ?",
                (ticker.upper(), cutoff),
            ).fetchone()
        if not row:
            return None
        return {
            "name":   row[0],
            "industry": row[1],
            "pe":     row[2],
            "f_pe":   row[3],
            "cap":    row[4],
            "growth": row[5],
            "news":   row[6],
            "_fetched_at": row[7],
        }

    def store(self, ticker: str, data: dict) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO fundamentals_cache "
                    "(ticker, name, industry, pe, f_pe, cap, growth, news, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ticker.upper(),
                        str(data.get("name", "")),
                        str(data.get("industry", "N/A")),
                        str(data.get("pe", "N/A")),
                        str(data.get("f_pe", "N/A")),
                        str(data.get("cap", "N/A")),
                        str(data.get("growth", "N/A")),
                        str(data.get("news", "")),
                        time.time(),
                    ),
                )


# ---------------------------------------------------------------------------
# SQLite AI Score Cache (Phase 1 of ARCHITECTURE.md rebuild)
# ---------------------------------------------------------------------------

class AIScoreCache:
    """
    Persistent Gemma analysis cache. Stored after every LLM run so the live
    agent can use overnight-computed AI scores without re-running inference.
    Invalidated by TTL or explicit news-shock event (Phase 4).
    """

    def __init__(self, db_path: str = DB_PATH, ttl_hours: int = 24):
        self.db_path = db_path
        self.ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_scores_cache (
                    ticker             TEXT PRIMARY KEY,
                    score              INTEGER,
                    rating             TEXT,
                    reasoning          TEXT,
                    bull_case          TEXT,
                    bear_case          TEXT,
                    key_risk           TEXT,
                    considerations_json TEXT,
                    generated_at       REAL NOT NULL
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")

    def get(self, ticker: str) -> Optional[dict]:
        """Returns fresh cached AI score dict or None if stale/missing."""
        import json as _json
        cutoff = time.time() - self.ttl_seconds
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT score, rating, reasoning, bull_case, bear_case, key_risk, "
                "considerations_json, generated_at "
                "FROM ai_scores_cache WHERE ticker = ? AND generated_at >= ?",
                (ticker.upper(), cutoff),
            ).fetchone()
        if not row:
            return None
        considerations = []
        try:
            considerations = _json.loads(row[6]) if row[6] else []
        except Exception:
            pass
        return {
            "score":          int(row[0]) if row[0] is not None else 50,
            "rating":         row[1] or "HOLD",
            "reasoning":      row[2] or "",
            "bull_case":      row[3] or "",
            "bear_case":      row[4] or "",
            "key_risk":       row[5] or "",
            "considerations": considerations,
            "_generated_at":  row[7],
        }

    def store(self, ticker: str, data: dict) -> None:
        import json as _json
        cons = data.get("considerations", []) or []
        try:
            cons_json = _json.dumps(cons)
        except Exception:
            cons_json = "[]"
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO ai_scores_cache "
                    "(ticker, score, rating, reasoning, bull_case, bear_case, key_risk, "
                    "considerations_json, generated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ticker.upper(),
                        int(data.get("score", 50)),
                        str(data.get("rating", "HOLD")).upper(),
                        str(data.get("reasoning", ""))[:4000],
                        str(data.get("bull_case", ""))[:2000],
                        str(data.get("bear_case", ""))[:2000],
                        str(data.get("key_risk", ""))[:2000],
                        cons_json,
                        time.time(),
                    ),
                )

    def invalidate(self, ticker: str) -> None:
        """Force-expire the cache for a ticker. Used on news shock (Phase 4)."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM ai_scores_cache WHERE ticker = ?",
                    (ticker.upper(),),
                )


# ---------------------------------------------------------------------------
# Watchlist Today (Phase 2 of ARCHITECTURE.md rebuild)
# ---------------------------------------------------------------------------

class WatchlistToday:
    """
    Output of the overnight research cycle — the ranked short-list the live
    reactive agent (Phase 3) will subscribe to and react against.
    Rebuilt every research run; previous day's rows are cleared.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_today (
                    ticker                TEXT PRIMARY KEY,
                    rank                  INTEGER NOT NULL,
                    ai_score              INTEGER,
                    quant_score           INTEGER,
                    recommendation        TEXT,
                    price                 REAL,
                    change_pct            REAL,
                    technicals_summary    TEXT,
                    fundamentals_summary  TEXT,
                    generated_at          REAL NOT NULL
                )
            """)
            conn.execute("PRAGMA journal_mode=WAL")

    def replace_all(self, rows: list) -> int:
        """
        Atomically replace yesterday's watchlist with today's.
        `rows` is a list of dicts produced by the research cycle.
        Returns the number of rows written.
        """
        if not rows:
            return 0
        now = time.time()
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM watchlist_today")
                conn.executemany(
                    "INSERT INTO watchlist_today "
                    "(ticker, rank, ai_score, quant_score, recommendation, price, "
                    "change_pct, technicals_summary, fundamentals_summary, generated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            r["ticker"].upper(),
                            int(r["rank"]),
                            int(r.get("ai_score") or 0) if r.get("ai_score") is not None else None,
                            int(r.get("quant_score") or 0) if r.get("quant_score") is not None else None,
                            str(r.get("recommendation", "HOLD")).upper(),
                            float(r.get("price") or 0),
                            float(r.get("change_pct") or 0),
                            str(r.get("technicals_summary", ""))[:500],
                            str(r.get("fundamentals_summary", ""))[:500],
                            now,
                        )
                        for r in rows
                    ],
                )
        return len(rows)

    def get_all(self) -> list:
        """Returns today's watchlist sorted by rank ASC. Returns [] if empty."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ticker, rank, ai_score, quant_score, recommendation, price, "
                "change_pct, technicals_summary, fundamentals_summary, generated_at "
                "FROM watchlist_today ORDER BY rank ASC"
            ).fetchall()
        return [
            {
                "ticker":               r[0],
                "rank":                 r[1],
                "ai_score":             r[2],
                "quant_score":          r[3],
                "recommendation":       r[4],
                "price":                r[5],
                "change_pct":           r[6],
                "technicals_summary":   r[7],
                "fundamentals_summary": r[8],
                "generated_at":         r[9],
            }
            for r in rows
        ]

    def get_tickers(self) -> list:
        """Returns just the list of ticker symbols on today's watchlist."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ticker FROM watchlist_today ORDER BY rank ASC"
            ).fetchall()
        return [r[0] for r in rows]

    def get_metadata(self) -> dict:
        """
        Returns {count, generated_at} for the current watchlist. Used by the
        UI to show 'Last research cycle: <timestamp> · N candidates'.
        Returns {count: 0, generated_at: None} if empty.
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(generated_at) FROM watchlist_today"
            ).fetchone()
        count = int(row[0]) if row and row[0] else 0
        generated_at = float(row[1]) if row and row[1] else None
        return {"count": count, "generated_at": generated_at}


# ---------------------------------------------------------------------------
# Live Triggers Log (Phase 3 of ARCHITECTURE.md rebuild)
# ---------------------------------------------------------------------------

class LiveTriggers:
    """
    Persistent log of every threshold crossing detected by the live agent.
    Each row records the crossing event + what action was taken. Used for
    post-hoc analysis ("did we approve / suppress / cap?") and threshold tuning.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS live_triggers (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            REAL NOT NULL,
                    ticker        TEXT NOT NULL,
                    direction     TEXT NOT NULL,
                    score_before  INTEGER,
                    score_after   INTEGER,
                    price         REAL,
                    action        TEXT NOT NULL,
                    notes         TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_live_triggers_ts ON live_triggers(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_live_triggers_ticker ON live_triggers(ticker)")
            conn.execute("PRAGMA journal_mode=WAL")

    def log(
        self,
        ticker: str,
        direction: str,
        score_before: Optional[int],
        score_after: int,
        price: float,
        action: str,
        notes: str = "",
    ) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO live_triggers (ts, ticker, direction, score_before, score_after, "
                    "price, action, notes) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), ticker.upper(), direction.upper(),
                     int(score_before) if score_before is not None else None,
                     int(score_after), float(price), action.upper(), notes[:500]),
                )

    def get_recent(self, limit: int = 50) -> list:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ts, ticker, direction, score_before, score_after, price, action, notes "
                "FROM live_triggers ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": r[0], "ticker": r[1], "direction": r[2],
                "score_before": r[3], "score_after": r[4], "price": r[5],
                "action": r[6], "notes": r[7],
            }
            for r in rows
        ]

    def count_today(self, action: str = "FIRED") -> int:
        """Count rows matching an action since start of today (UTC, good enough)."""
        start_of_day = time.time() - (time.time() % 86400)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM live_triggers WHERE ts >= ? AND action = ?",
                (start_of_day, action.upper()),
            ).fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# News Cache (Phase 4 of ARCHITECTURE.md rebuild)
# ---------------------------------------------------------------------------

class NewsCache:
    """
    Persistent ticker-keyed news + classification cache. Written by the
    background NewsPoller; read at trigger-time by the live agent's
    news-final-check before firing approvals.

    Each ticker stores its most-recent classification + headline so the live
    agent's hot path is a single-row SELECT (<5ms).
    """

    def __init__(self, db_path: str = DB_PATH, ttl_hours: int = 6):
        self.db_path = db_path
        self.ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_cache (
                    ticker          TEXT NOT NULL,
                    headline        TEXT NOT NULL,
                    source          TEXT,
                    classification  TEXT NOT NULL,
                    published_at    TEXT,
                    ingested_at     REAL NOT NULL,
                    PRIMARY KEY (ticker, headline)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_news_ticker_ingested "
                         "ON news_cache(ticker, ingested_at DESC)")
            conn.execute("PRAGMA journal_mode=WAL")

    def store(self, ticker: str, headline: str, classification: str,
              source: str = "polygon", published_at: Optional[str] = None) -> bool:
        """
        Inserts a news row. Returns True if it was a new row, False if it was
        already present (de-dup by (ticker, headline)).
        """
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO news_cache "
                    "(ticker, headline, source, classification, published_at, ingested_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (ticker.upper(), headline[:500], source,
                     classification.upper(), published_at, time.time()),
                )
                return cursor.rowcount > 0

    def get_latest_classification(self, ticker: str) -> Optional[dict]:
        """
        Returns the most-recently-ingested news classification for ticker, if
        within TTL. Used by the live agent's news-final-check. Returns None on
        miss or if the latest news is stale.
        """
        cutoff = time.time() - self.ttl_seconds
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT headline, source, classification, published_at, ingested_at "
                "FROM news_cache "
                "WHERE ticker = ? AND ingested_at >= ? "
                "ORDER BY ingested_at DESC LIMIT 1",
                (ticker.upper(), cutoff),
            ).fetchone()
        if not row:
            return None
        return {
            "headline":       row[0],
            "source":         row[1],
            "classification": row[2],
            "published_at":   row[3],
            "ingested_at":    row[4],
        }

    def get_recent_for_ticker(self, ticker: str, limit: int = 5) -> list:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT headline, classification, ingested_at FROM news_cache "
                "WHERE ticker = ? ORDER BY ingested_at DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        return [{"headline": r[0], "classification": r[1], "ingested_at": r[2]} for r in rows]

    def count_today(self) -> int:
        start_of_day = time.time() - (time.time() % 86400)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM news_cache WHERE ingested_at >= ?",
                (start_of_day,),
            ).fetchone()
        return int(row[0]) if row else 0


# Module-level singletons
rate_limiter      = PolygonRateLimiter()
news_cache        = AppCache(ttl_seconds=7200)   # 2 hours (L1 in-memory)
funds_cache       = AppCache(ttl_seconds=14400)  # 4 hours (L1 in-memory)
history_cache     = HistoryCache(db_path=DB_PATH)
fundamentals_cache = FundamentalsCache(db_path=DB_PATH, ttl_hours=24)  # L2 persistent
ai_score_cache    = AIScoreCache(db_path=DB_PATH, ttl_hours=24)        # L2 persistent
watchlist_today    = WatchlistToday(db_path=DB_PATH)                   # Phase 2 output
live_triggers      = LiveTriggers(db_path=DB_PATH)                     # Phase 3 crossing log
news_cache_v2      = NewsCache(db_path=DB_PATH, ttl_hours=6)           # Phase 4 news + classification


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
        # Return from cache so timestamps are normalized to ISO date strings
        # (Polygon returns `t` as int milliseconds; downstream code expects strings).
        return history_cache.get(ticker, allow_stale=True) or []


# ---------------------------------------------------------------------------
# yfinance Fetchers (sync + async)
# ---------------------------------------------------------------------------

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(Exception)
)
def get_fundamentals(ticker: str, logger=None) -> dict:
    """
    Fetches fundamental data via yfinance with Polygon news fallback.
    Three-tier cache: L1 in-memory (4h) → L2 SQLite (24h) → L3 yfinance fetch.
    """
    # L1: hot in-memory cache
    cached = funds_cache.get(ticker)
    if cached:
        return cached

    # L2: persistent SQLite cache (survives restarts)
    persisted = fundamentals_cache.get(ticker)
    if persisted:
        # Strip internal field before returning + hydrate L1
        persisted.pop("_fetched_at", None)
        funds_cache.set(ticker, persisted)
        if logger:
            logger.log(f"📦 Fundamentals L2 HIT [{ticker}] from SQLite")
        return persisted

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
        # Write through both cache layers
        funds_cache.set(ticker, data)
        try:
            fundamentals_cache.store(ticker, data)
        except Exception as e:
            if logger:
                logger.log(f"⚠️ Fundamentals L2 write failed [{ticker}]: {e}", level="WARNING")
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
