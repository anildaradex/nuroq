"""
news_engine.py — Phase 4 of ARCHITECTURE.md rebuild.

Two responsibilities:

1. NewsClassifier — keyword-based shock classifier on headlines. Returns one
   of POSITIVE_BOOST / NEUTRAL / NEGATIVE_WARNING / NEGATIVE_BLOCK. Pure
   function, no I/O. Used inline by the live agent's news-final-check.

2. NewsPoller — background thread polling Polygon news every 30 min for the
   live agent's watchlist (top N + held positions). Writes new headlines +
   classifications to news_cache so the hot path is a single SELECT.

3. check_news_for_crossing(ticker) — helper called inline by LiveAgent right
   before firing a BUY approval. Returns the latest classification + headline
   from news_cache (or NEUTRAL if no recent news). Hot-path safe (no API call).

Polygon free-tier budget (300 req/hr):
  35 tickers × 1 call each / 30 min cadence = 70 calls/hr → well under limit.
"""

from __future__ import annotations

import os
import re
import time
import threading
import requests
from dataclasses import dataclass
from typing import Optional, List

from data_fetcher import (
    rate_limiter, news_cache_v2, watchlist_today,
)


# ─── Classification keyword sets ──────────────────────────────────────────────
# Order of evaluation matters: BLOCK > WARNING > BOOST > NEUTRAL.
# Keywords are matched case-insensitively against headline text.

NEGATIVE_BLOCK_KEYWORDS = [
    r"\bhalt(ed)?\b",
    r"\bSEC investigation\b",
    r"\bfraud\b",
    r"\bbankruptcy\b",
    r"\bdelist(ed|ing)?\b",
    r"\bdoj (probe|investigation|charges)\b",
    r"\bgoing concern\b",
    r"\bchapter 11\b",
    r"\baccounting (probe|investigation|scandal)\b",
]

NEGATIVE_WARNING_KEYWORDS = [
    r"\bdowngrade(d|s)?\b",
    r"\b(earnings|revenue) miss\b",
    r"\blawsuit\b",
    r"\brecall(s|ed)?\b",
    r"\bCEO (departure|resigns|steps down)\b",
    r"\bguidance (cut|lowered|reduced|withdrawn)\b",
    r"\bprobe\b",
    r"\bsubpoena\b",
    r"\blayoffs\b",
    r"\bdata breach\b",
]

POSITIVE_BOOST_KEYWORDS = [
    r"\b(earnings|revenue|sales)\s+beat\b",
    r"\bbeats?\b.{0,30}\b(estimates|expectations|forecast|consensus)\b",
    r"\btops?\s+(estimates|expectations|forecast|consensus)\b",
    r"\bupgrade(d|s)?\b",
    r"\bFDA approval\b",
    r"\bwins? (contract|deal|order)\b",
    r"\bpartnership with\b",
    r"\bguidance (raised|hiked|increased)\b",
    r"\brecord (revenue|profit|earnings)\b",
    r"\bbuyback\b",
    r"\bdividend increase\b",
    r"\bacquir(ed|es|ing)\b",
]


# Pre-compile regexes once at import time.
_BLOCK_RE   = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_BLOCK_KEYWORDS]
_WARNING_RE = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_WARNING_KEYWORDS]
_BOOST_RE   = [re.compile(p, re.IGNORECASE) for p in POSITIVE_BOOST_KEYWORDS]


# ─── NewsClassifier ───────────────────────────────────────────────────────────

CLASSIFICATIONS = ("POSITIVE_BOOST", "NEUTRAL", "NEGATIVE_WARNING", "NEGATIVE_BLOCK")


@dataclass(frozen=True)
class NewsVerdict:
    classification: str
    matched_keyword: Optional[str]
    headline: str


class NewsClassifier:
    """Keyword-based 4-bucket classifier. Pure function, no I/O."""

    @staticmethod
    def classify(headline: str) -> NewsVerdict:
        if not headline:
            return NewsVerdict("NEUTRAL", None, "")
        # Evaluate in priority order — block strongest, then warning, then boost.
        for r in _BLOCK_RE:
            if r.search(headline):
                return NewsVerdict("NEGATIVE_BLOCK", r.pattern, headline)
        for r in _WARNING_RE:
            if r.search(headline):
                return NewsVerdict("NEGATIVE_WARNING", r.pattern, headline)
        for r in _BOOST_RE:
            if r.search(headline):
                return NewsVerdict("POSITIVE_BOOST", r.pattern, headline)
        return NewsVerdict("NEUTRAL", None, headline)


# ─── Live agent's news-final-check (called inline on crossing) ────────────────

def check_news_for_crossing(ticker: str) -> Optional[dict]:
    """
    Returns the most recent classified news entry for ticker, or None if no
    recent news. Reads from news_cache (no API call — hot-path safe).
    Output dict shape: {classification, headline, source, published_at, ingested_at}.
    """
    return news_cache_v2.get_latest_classification(ticker)


# ─── NewsPoller (background thread) ───────────────────────────────────────────

class NewsPoller:
    """
    Periodic Polygon news fetcher for live-agent watchlist + held positions.
    Writes only NEW headlines (via NewsCache's INSERT OR IGNORE) so repeated
    polls don't bloat the table.

    Lifecycle:
      poller = NewsPoller(get_tickers_fn=..., interval_seconds=1800)
      poller.start()
      ...
      poller.stop()
    """

    def __init__(
        self,
        get_tickers_fn,
        logger,
        polygon_api_key: Optional[str] = None,
        interval_seconds: int = 1800,        # 30 min default
        max_tickers_per_cycle: int = 35,
        max_headlines_per_ticker: int = 5,
        on_shock_callback=None,              # Phase 4b: called with (ticker, verdict) on BLOCK/WARNING/BOOST
    ):
        self.get_tickers_fn = get_tickers_fn
        self.logger = logger
        self.api_key = polygon_api_key or os.getenv("POLYGON_API_KEY", "")
        self.interval_seconds = interval_seconds
        self.max_tickers_per_cycle = max_tickers_per_cycle
        self.max_headlines_per_ticker = max_headlines_per_ticker
        self.on_shock_callback = on_shock_callback

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.is_running = False
        self.cycles_completed = 0
        self.last_cycle_at: Optional[float] = None
        self.headlines_ingested_total = 0
        self.shocks_dispatched_total = 0

    # ─── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> str:
        if self.is_running:
            return "NewsPoller already running."
        if not self.api_key:
            return "⚠️ NewsPoller: POLYGON_API_KEY not set, not starting."
        self.is_running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._main_loop, name="news-poller",
                                        daemon=True)
        self._thread.start()
        self.logger.log(f"🟢 NewsPoller started (every {self.interval_seconds}s, "
                        f"top {self.max_tickers_per_cycle} tickers).")
        return "NewsPoller started."

    def stop(self) -> str:
        if not self.is_running:
            return "NewsPoller already stopped."
        self.is_running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.logger.log(f"🔴 NewsPoller stopped. {self.cycles_completed} cycles, "
                        f"{self.headlines_ingested_total} headlines ingested.")
        return "NewsPoller stopped."

    # ─── main loop ────────────────────────────────────────────────────────────

    def _main_loop(self) -> None:
        # Stagger the first cycle by a few seconds so dashboard startup isn't blocked.
        if self._stop_event.wait(timeout=5):
            return
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                self.logger.log(f"⚠️ NewsPoller cycle error: {e}", level="WARNING")
            self.cycles_completed += 1
            self.last_cycle_at = time.time()
            # Sleep in chunks so stop() is responsive.
            if self._stop_event.wait(timeout=self.interval_seconds):
                return

    # ─── one poll cycle ───────────────────────────────────────────────────────

    def _poll_once(self) -> int:
        tickers = self._target_tickers()
        if not tickers:
            self.logger.log("ℹ️ NewsPoller: no tickers to poll this cycle.")
            return 0
        ingested = 0
        for ticker in tickers:
            if self._stop_event.is_set():
                break
            ingested += self._poll_ticker(ticker)
        self.headlines_ingested_total += ingested
        self.logger.log(f"📰 NewsPoller cycle complete: {len(tickers)} tickers, "
                        f"{ingested} new headlines.")
        return ingested

    def _target_tickers(self) -> List[str]:
        """Watchlist top N + held positions, deduped, capped at max_tickers_per_cycle."""
        try:
            tickers = self.get_tickers_fn() or []
        except Exception as e:
            self.logger.log(f"⚠️ NewsPoller: get_tickers_fn failed: {e}", level="WARNING")
            return []
        seen, out = set(), []
        for t in tickers:
            tu = (t or "").upper()
            if tu and tu not in seen:
                seen.add(tu); out.append(tu)
        return out[:self.max_tickers_per_cycle]

    def _poll_ticker(self, ticker: str) -> int:
        """Fetch + classify + store. Returns count of NEW headlines stored."""
        rate_limiter.wait(self.logger)
        url = (f"https://api.polygon.io/v2/reference/news"
               f"?ticker={ticker}&limit={self.max_headlines_per_ticker}&apiKey={self.api_key}")
        try:
            resp = requests.get(url, timeout=10).json()
        except Exception as e:
            self.logger.log(f"⚠️ NewsPoller fetch [{ticker}]: {e}", level="WARNING")
            return 0

        results = resp.get("results", []) if isinstance(resp, dict) else []
        ingested = 0
        for item in results:
            headline = (item.get("title") or "").strip()
            if not headline:
                continue
            verdict = NewsClassifier.classify(headline)
            wrote = news_cache_v2.store(
                ticker=ticker,
                headline=headline,
                classification=verdict.classification,
                source="polygon",
                published_at=item.get("published_utc"),
            )
            if wrote:
                ingested += 1
                if verdict.classification != "NEUTRAL":
                    self.logger.log(
                        f"📰 NewsPoller [{ticker}] {verdict.classification}: {headline[:120]}"
                    )
                    # Phase 4b: dispatch shock callback. Dashboard wires this to
                    # ai_score_cache.invalidate(ticker) + llm_queue.enqueue(ticker).
                    if self.on_shock_callback is not None:
                        try:
                            self.on_shock_callback(ticker, verdict)
                            self.shocks_dispatched_total += 1
                        except Exception as e:
                            self.logger.log(
                                f"⚠️ NewsPoller shock callback for {ticker} raised: {e}",
                                level="WARNING"
                            )
        return ingested

    # ─── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "running":                  self.is_running,
            "cycles_completed":         self.cycles_completed,
            "last_cycle_at":            self.last_cycle_at,
            "headlines_ingested_total": self.headlines_ingested_total,
            "interval_seconds":         self.interval_seconds,
        }
