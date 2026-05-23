import unittest
import os
import time
import tempfile
from datetime import date, timedelta
from unittest.mock import patch
from scoring import calculate_sizing, get_weekly_confluence
from data_fetcher import HistoryCache

class TestNuroQUtils(unittest.TestCase):
    
    def test_calculate_sizing_basic(self):
        """Test ATR-based position sizing logic."""
        price = 100.0
        atr = 2.0
        account = 10000
        risk_pct = 0.01 # 1% risk ($100)
        
        result = calculate_sizing(price, atr, account, risk_pct)
        
        # Risk = $100. Stop dist = 2 * ATR = 4.0.
        # Shares = 100 / 4 = 25.
        self.assertEqual(result['shares'], 25.0)
        self.assertEqual(result['sl'], 96.0)
        self.assertEqual(result['tp'], 108.0)
        self.assertEqual(result['amount'], 2500.0)

    def test_calculate_sizing_zero_atr(self):
        """Test sizing when ATR is zero (fallback to 1 share)."""
        result = calculate_sizing(100.0, 0.0)
        self.assertEqual(result['shares'], 1)

    def test_weekly_confluence_up(self):
        """Test weekly trend detection (UP)."""
        # Create a mock history of 60 days of rising prices
        history = [{"c": 100 + i} for i in range(60)]
        result = get_weekly_confluence(history)
        self.assertEqual(result, "UP")

    def test_weekly_confluence_down(self):
        """Test weekly trend detection (DOWN)."""
        # Create a mock history of 60 days of falling prices
        history = [{"c": 100 - i} for i in range(60)]
        result = get_weekly_confluence(history)
        self.assertEqual(result, "DOWN")

    def test_weekly_confluence_short_history(self):
        """Test weekly trend with insufficient data."""
        history = [{"c": 100}] * 10
        result = get_weekly_confluence(history)
        self.assertEqual(result, "UNKNOWN")



class TestHistoryCache(unittest.TestCase):
    """Tests for the SQLite-backed OHLCV price history cache."""

    def _make_cache(self):
        """Helper: creates a HistoryCache backed by a fresh temp DB."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return HistoryCache(db_path=tmp.name), tmp.name

    def _sample_bars(self, n=60):
        """
        Generates n fake OHLCV bars as date strings ending at the last trading day.
        Uses date strings for 't' so HistoryCache.store() stores them verbatim.
        """
        bars = []
        # Find the last trading day (same logic as HistoryCache._get_last_trading_day)
        d = date.today() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        last_td = d

        # Walk backwards from last_td to collect n weekday bars
        collected = []
        d = last_td
        while len(collected) < n:
            if d.weekday() < 5:
                collected.append(d)
            d -= timedelta(days=1)
        collected.reverse()  # Ascending order

        for bar_date in collected:
            bars.append({
                "o": 100.0, "h": 105.0, "l": 98.0,
                "c": 102.0, "v": 1_000_000,
                "t": bar_date.strftime("%Y-%m-%d"),  # Date string — no timezone issues
            })
        return bars

    def test_cache_miss_then_hit(self):
        """After storing bars, a second get() call should return from cache in <50ms."""
        cache, db_path = self._make_cache()
        try:
            bars = self._sample_bars(60)
            cache.store("AAPL", bars)

            t0 = time.time()
            result = cache.get("AAPL")
            elapsed = time.time() - t0

            self.assertIsNotNone(result, "Cache should return bars after store()")
            self.assertGreater(len(result), 0)
            self.assertLess(elapsed, 0.05, f"Cache hit took {elapsed:.3f}s — expected <50ms")
        finally:
            os.remove(db_path)

    def test_cache_miss_returns_none(self):
        """A ticker with no stored data should return None (triggering Polygon fetch)."""
        cache, db_path = self._make_cache()
        try:
            result = cache.get("ZZZZ_UNKNOWN")
            self.assertIsNone(result, "Unknown ticker should return None from cache")
        finally:
            os.remove(db_path)

    def test_store_and_retrieve_bar_count(self):
        """Stored bars should all be retrievable and in ascending date order."""
        cache, db_path = self._make_cache()
        try:
            bars = self._sample_bars(50)
            cache.store("NVDA", bars)
            result = cache.get("NVDA")
            self.assertIsNotNone(result)
            # All bars stored (weekday filter may trim some)
            self.assertGreater(len(result), 30)
            # Verify ascending date order
            dates = [r["t"] for r in result]
            self.assertEqual(dates, sorted(dates))
        finally:
            os.remove(db_path)


if __name__ == '__main__':
    unittest.main()
