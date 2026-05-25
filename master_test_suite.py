"""
master_test_suite.py — AntiGravity Comprehensive Test Suite
Validates all core functionalities: Data Fetching, Scoring, Memory, LLM Parsing, and Execution.
"""

import unittest
from unittest.mock import patch, MagicMock
import asyncio
import os
import pandas as pd
import sys
import time
from datetime import date, timedelta

# Import the modules
from data_fetcher import AppCache, PolygonRateLimiter, get_polygon_news, get_earnings_risk, get_sentiment, get_fundamentals
from scoring import calculate_technicals, get_weekly_confluence, calculate_sizing, calculate_quant_score
from memory_module import AgentMemory
from event_stream import MarketStreamer

# We must mock mlx-lm before importing dashboard to avoid loading heavy local models during tests
mock_mlx = MagicMock()
mock_mlx.load.return_value = (MagicMock(), MagicMock()) # model, tokenizer
sys.modules['mlx_lm'] = mock_mlx
sys.modules['mlx_lm.sample_utils'] = MagicMock()

# CRITICAL: disable background services before importing dashboard. Otherwise
# `import dashboard` spins up the Telegram poller, the NewsPoller, the position
# monitor, and the LLMRescoreQueue at module-load time — and the Telegram poller
# steals the bot-token getUpdates slot from any actually-running dashboard,
# producing telegram.error.Conflict in the live process's logs. The cron scripts
# already set this flag for the same reason; tests must too.
os.environ.setdefault("NUROQ_BACKGROUND_SERVICES", "0")

import dashboard
from dashboard import EnsembleAnalyst, ShadowExecutor, PortfolioManager

class TestDataFetcher(unittest.TestCase):
    def test_app_cache(self):
        """Test AppCache stores, retrieves, and respects TTL."""
        cache = AppCache(ttl_seconds=1)
        cache.set("AAPL", "Good News")
        self.assertEqual(cache.get("AAPL"), "Good News")
        time.sleep(1.1)
        self.assertIsNone(cache.get("AAPL"))

    @patch('data_fetcher.requests.get')
    def test_get_polygon_news(self, mock_get):
        """Test Polygon news formatting and fallback."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"title": "Apple releases new iPhone", "description": "It has cameras."}]
        }
        mock_get.return_value = mock_resp
        
        # We need to clear cache if it was set
        dashboard.news_cache.cache = {}
        news = get_polygon_news("AAPL")
        self.assertIn("Apple releases new iPhone", news)

    @patch('data_fetcher.yf.Ticker')
    def test_get_earnings_risk(self, mock_ticker):
        """Test earnings risk calculation."""
        mock_stock = MagicMock()
        # Earnings tomorrow -> High Risk
        mock_stock.calendar = {"Earnings Date": [date.today() + timedelta(days=1)]}
        mock_ticker.return_value = mock_stock
        
        res = get_earnings_risk("AAPL")
        self.assertTrue(res['risk'])
        
        # Earnings in 30 days -> Safe
        mock_stock.calendar = {"Earnings Date": [date.today() + timedelta(days=30)]}
        res_safe = get_earnings_risk("AAPL")
        self.assertFalse(res_safe['risk'])

    @patch('data_fetcher.requests.get')
    def test_get_sentiment(self, mock_get):
        """Test Stocktwits sentiment math."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "messages": [
                {"entities": {"sentiment": {"basic": "Bullish"}}},
                {"entities": {"sentiment": {"basic": "Bullish"}}},
                {"entities": {"sentiment": {"basic": "Bearish"}}}
            ]
        } # 66% Bullish
        mock_get.return_value = mock_resp
        
        sentiment = get_sentiment("NVDA")
        self.assertEqual(sentiment, "BULLISH")

class TestScoringEngine(unittest.TestCase):
    def test_calculate_technicals(self):
        """Test indicator math bounds."""
        mock_history = [
            {"c": 100 + i, "o": 99 + i, "h": 101 + i, "l": 98 + i, "v": 1000}
            for i in range(30)
        ]
        techs = calculate_technicals(mock_history)
        self.assertIsNotNone(techs)
        self.assertTrue(0 <= techs['rsi'] <= 100)
        self.assertTrue(techs['atr'] > 0)
        self.assertEqual(techs['trend'], "STRONG UP")

    def test_quant_score_logic(self):
        """
        Negative P/E earns 0 valuation pts; RSI 20 hits the oversold +15;
        STRONG UP + %B 0.05 earns nothing under the momentum-aware rubric
        (the contradictory STRONG UP + lower-band reward was removed in Phase 1
        of the scoring rework). Verifies the cumulative breakdown ends at the
        expected gated score.
        """
        techs = {"trend": "STRONG UP", "rsi": 20, "rel_vol": 2.0, "percent_b": 0.05, "volatility": 1.0}
        funds_bad_pe = {"pe": -5, "growth": 0.2}

        score = calculate_quant_score(techs, funds_bad_pe, "UP", {"risk": False}, "NEUTRAL", 50)

        # Trend confluence +15, RSI oversold +15, rel_vol +10, growth +10,
        # P/E 0 (negative), %B 0 (contradictory STRONG UP + low %B), sentiment +2,
        # quant_base = 52 → AI gate opens, +5 from AI 50.
        self.assertEqual(score, 57)

class TestLLMOrchestration(unittest.TestCase):
    def setUp(self):
        self.analyst = EnsembleAnalyst(mode="single")

    def test_json_parsing_raw(self):
        """Test robust JSON parsing (raw_decode ignores trailing text)."""
        raw_output = 'Here is my analysis:\n{"score": 85, "rating": "BUY", "reasoning": "Good setup."}\nThis text should be ignored.'
        parsed = self.analyst.get_structured_data(raw_output)
        self.assertEqual(parsed['score'], 85)
        self.assertEqual(parsed['rating'], "BUY")

    def test_json_parsing_regex_fallback(self):
        """Test regex fallback when JSON is completely broken."""
        raw_output = 'The rating is BUY and the score is 75. Reasoning: "Very solid."'
        parsed = self.analyst.get_structured_data(raw_output)
        self.assertEqual(parsed['score'], 75)
        self.assertEqual(parsed['rating'], "BUY")

    def test_consensus_logic(self):
        """Test Gemma Analyst inference logic."""
        # Mock analyze to return score for gemma
        def mock_analyze(prompt, model_key):
            return '{"score": 90, "rating": "BUY"}'
                
        self.analyst.analyze = mock_analyze
        res = self.analyst.get_consensus("AAPL", "Prompt")
        
        self.assertTrue(res['is_consensus'])
        self.assertEqual(res['score'], 90)
        self.assertEqual(res['rating'], "BUY")

    def test_consensus_guardrail(self):
        """Test that a BUY rating is downgraded to HOLD if the score is < 70."""
        def mock_analyze(prompt, model_key):
            return '{"score": 60, "rating": "BUY"}'
        
        self.analyst.analyze = mock_analyze
        res = self.analyst.get_consensus("AAPL", "Prompt")
        
        # The score is 60, but the model hallucinated a "BUY". Guardrail should force "HOLD".
        self.assertEqual(res['rating'], "HOLD")

class TestExecutionPortfolio(unittest.TestCase):
    def test_shadow_executor(self):
        """Test shadow executor logs to SQLite correctly."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            test_db = tmp.name
        try:
            executor = ShadowExecutor(db_path=test_db)
            res = executor.execute_trade("AAPL", 150.0, amount=300)
            self.assertIn("BUY", res)
            # Verify the trade landed in the DB
            import sqlite3
            with sqlite3.connect(test_db) as conn:
                rows = conn.execute("SELECT * FROM shadow_trades WHERE ticker='AAPL'").fetchall()
            self.assertEqual(len(rows), 1)
        finally:
            os.remove(test_db)

    def test_portfolio_manager(self):
        """Test portfolio manager adds and removes correctly (SQLite)."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            test_db = tmp.name
        try:
            pm = PortfolioManager(db_path=test_db)
            pm.add_position("NVDA", 10, 100.0)
            df = pm.get_portfolio()
            self.assertIn("NVDA", df['Ticker'].values)
            pm.remove_position("NVDA")
            df_after = pm.get_portfolio()
            self.assertNotIn("NVDA", df_after['Ticker'].values)
        finally:
            os.remove(test_db)

class TestAlpacaExecution(unittest.TestCase):
    @patch('alpaca_executor.TradingClient')
    def test_alpaca_live_trade_success(self, mock_client_class):
        """Test Alpaca executor routes trades to API correctly."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        with patch.dict(os.environ, {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}):
            from alpaca_executor import LiveAlpacaExecutor
            executor = LiveAlpacaExecutor()
            executor.is_connected = True 
            
            # Test standard market order
            res_market = executor.submit_advanced_order("AAPL", "buy", 10, order_type="Market")
            self.assertIn("✅", res_market)
            mock_client.submit_order.assert_called_once()
            
            # Test limit order
            res_limit = executor.submit_advanced_order("NVDA", "sell", 5, order_type="Limit", limit_price=150.0)
            self.assertIn("Limit: $150.0", res_limit)
            self.assertEqual(mock_client.submit_order.call_count, 2)

    def test_alpaca_fallback_when_keys_missing(self):
        """Test Alpaca executor simulates if keys are missing."""
        with patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}):
            from alpaca_executor import LiveAlpacaExecutor
            executor = LiveAlpacaExecutor()

            self.assertFalse(executor.is_connected)
            res = executor.submit_advanced_order("AAPL", "buy", 10)
            self.assertIn("Simulated", res)


# ═══════════════════════════════════════════════════════════════════════════
# Regression coverage for Phases 1-3a additions (sessions of 2026-05-22..23)
# ═══════════════════════════════════════════════════════════════════════════

class TestMomentumAwarePB(unittest.TestCase):
    """Phase 1 scoring rework: %B logic interacts with trend direction."""

    def _score(self, trend, percent_b, **overrides):
        techs = {
            "trend": trend, "rsi": 50, "rel_vol": 1.0,
            "percent_b": percent_b, "volatility": 1.0,
        }
        techs.update(overrides.get("techs", {}))
        return calculate_quant_score(
            techs, funds={"pe": 100, "growth": 0},
            w_trend="DOWN", e_risk={"risk": False},
            st_sent="NEUTRAL", ai_score=0,
        )

    def test_breakout_path_earns_pb_bonus(self):
        """STRONG UP + %B in 0.5-0.9 (breakout zone) = +10 from %B."""
        s_no_pb = self._score("STRONG UP", 0.45)      # below breakout zone
        s_breakout = self._score("STRONG UP", 0.70)
        self.assertGreaterEqual(s_breakout - s_no_pb, 5)

    def test_reversion_path_earns_pb_bonus(self):
        """WEAK DOWN + %B < 0.1 (oversold) = +10 from %B."""
        s_mid = self._score("WEAK DOWN", 0.50)
        s_oversold = self._score("WEAK DOWN", 0.05)
        self.assertGreaterEqual(s_oversold - s_mid, 10)

    def test_contradictory_combo_earns_nothing(self):
        """STRONG UP + %B 0.05 (lower band) should NOT get the old +10 reward.
        Compare two STRONG UP positions that both fall OUTSIDE all reward zones:
        %B 0.05 (below building zone) vs %B 0.30 (between reversion and building).
        Pre-fix, %B 0.05 earned +10 from the trend-agnostic 'near lower band' rule.
        Post-fix, both earn 0 from the %B block."""
        s_strong_low = self._score("STRONG UP", 0.05)
        s_strong_meh = self._score("STRONG UP", 0.30)
        self.assertEqual(s_strong_low, s_strong_meh)

    def test_extended_above_upper_band_penalty(self):
        """%B > 1.0 = -5 regardless of trend."""
        s_normal = self._score("STRONG UP", 0.70)
        s_extended = self._score("STRONG UP", 1.05)
        self.assertEqual(s_normal - s_extended, 15)  # +10 breakout vs -5 extended


class TestPersistentCaches(unittest.TestCase):
    """Phase 1: FundamentalsCache + AIScoreCache + Phase 2/3 watchlist + triggers."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.remove(self.tmp.name)

    def test_fundamentals_cache_roundtrip(self):
        from data_fetcher import FundamentalsCache
        fc = FundamentalsCache(db_path=self.tmp.name)
        fc.store("AAPL", {"name": "Apple", "industry": "Tech",
                          "pe": 28.5, "f_pe": 25.0, "cap": 3e12,
                          "growth": 0.08, "news": "Headlines..."})
        got = fc.get("AAPL")
        self.assertIsNotNone(got)
        self.assertEqual(got["name"], "Apple")
        self.assertEqual(got["pe"], "28.5")

    def test_fundamentals_cache_ttl_expires(self):
        from data_fetcher import FundamentalsCache
        fc = FundamentalsCache(db_path=self.tmp.name, ttl_hours=0.0001)  # ~0.36s
        fc.store("X", {"pe": 10})
        self.assertIsNotNone(fc.get("X"))
        time.sleep(0.5)
        self.assertIsNone(fc.get("X"))

    def test_ai_score_cache_roundtrip_with_json_field(self):
        from data_fetcher import AIScoreCache
        sc = AIScoreCache(db_path=self.tmp.name)
        sc.store("NVDA", {
            "score": 78, "rating": "BUY",
            "reasoning": "Breakout setup", "bull_case": "Confluence",
            "bear_case": "Extended", "key_risk": "Sector rotation",
            "considerations": ["Trend up", "Vol high"],
        })
        got = sc.get("NVDA")
        self.assertEqual(got["score"], 78)
        self.assertEqual(got["rating"], "BUY")
        self.assertEqual(got["considerations"], ["Trend up", "Vol high"])

    def test_ai_score_cache_invalidate(self):
        from data_fetcher import AIScoreCache
        sc = AIScoreCache(db_path=self.tmp.name)
        sc.store("X", {"score": 80, "rating": "BUY"})
        self.assertIsNotNone(sc.get("X"))
        sc.invalidate("X")
        self.assertIsNone(sc.get("X"))

    def test_watchlist_today_replace_is_atomic(self):
        from data_fetcher import WatchlistToday
        wt = WatchlistToday(db_path=self.tmp.name)
        wt.replace_all([
            {"ticker": "A", "rank": 1, "quant_score": 70, "ai_score": 80,
             "recommendation": "BUY", "price": 10, "change_pct": 2,
             "technicals_summary": "x", "fundamentals_summary": "y"},
            {"ticker": "B", "rank": 2, "quant_score": 60, "ai_score": 70,
             "recommendation": "BUY", "price": 20, "change_pct": 1,
             "technicals_summary": "x", "fundamentals_summary": "y"},
        ])
        self.assertEqual(wt.get_tickers(), ["A", "B"])
        # Replace with different set → old rows must be gone
        wt.replace_all([
            {"ticker": "C", "rank": 1, "quant_score": 90, "ai_score": 80,
             "recommendation": "BUY", "price": 50, "change_pct": 5,
             "technicals_summary": "x", "fundamentals_summary": "y"},
        ])
        self.assertEqual(wt.get_tickers(), ["C"])

    def test_live_triggers_log_and_count(self):
        from data_fetcher import LiveTriggers
        lt = LiveTriggers(db_path=self.tmp.name)
        lt.log("NVDA", "BUY", 60, 68, 215.0, "FIRED_BUY", "test")
        lt.log("AAPL", "BUY", 65, 70, 305.0, "FIRED_BUY", "test")
        lt.log("MSFT", "BUY", 60, 67, 410.0, "SUPPRESSED_CAP", "cap reached")
        self.assertEqual(lt.count_today("FIRED_BUY"), 2)
        self.assertEqual(lt.count_today("SUPPRESSED_CAP"), 1)
        recent = lt.get_recent(limit=10)
        self.assertEqual(len(recent), 3)


class TestPolygonHardening(unittest.TestCase):
    """Phase 3 review fixes: rate-limiter lock release + bounded retry + timestamp norm."""

    def test_rate_limiter_releases_lock_during_sleep(self):
        """Background thread that's mid-sleep must NOT hold the lock."""
        import threading
        from data_fetcher import PolygonRateLimiter
        rl = PolygonRateLimiter(max_per_min=5)
        for _ in range(5):
            rl.wait()  # fill the slot

        # Spawn a thread that will sleep inside .wait() (slot is full)
        def sleeper():
            rl.wait()
        t = threading.Thread(target=sleeper, daemon=True)
        t.start()
        time.sleep(0.2)  # let the sleeper enter time.sleep

        # Must be able to grab the lock immediately (not wait ~60s).
        t0 = time.time()
        with rl.lock:
            pass
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0,
                        f"Lock held during sleep: {elapsed:.1f}s (regression!)")

    def test_polygon_fetch_bounded_retry(self):
        """_fetch_bars_from_polygon must cap retries (no recursion)."""
        import data_fetcher
        calls = {"n": 0}
        def fake_get(*args, **kwargs):
            calls["n"] += 1
            resp = MagicMock()
            resp.json.return_value = {"status": "ERROR", "error": "rate limit exceeded"}
            return resp
        with patch("data_fetcher.requests.get", side_effect=fake_get), \
             patch("data_fetcher.time.sleep"), \
             patch("data_fetcher.rate_limiter.wait"):
            result = data_fetcher._fetch_bars_from_polygon("X", "2026-01-01", "2026-01-02")
        self.assertEqual(calls["n"], 3, "should attempt 3 times, no recursion")
        self.assertEqual(result, [])

    def test_history_cache_normalizes_int_timestamps(self):
        """Polygon returns int ms; HistoryCache must store as date strings."""
        import tempfile
        from data_fetcher import HistoryCache
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            pass
        try:
            hc = HistoryCache(db_path=tmp.name)
            hc.store("TST", [{"o":1,"h":2,"l":0,"c":1,"v":100,"t":1716422400000}])
            got = hc.get("TST", allow_stale=True)
            self.assertIsInstance(got[0]["t"], str)
        finally:
            os.remove(tmp.name)


class TestDashboardHelpers(unittest.TestCase):
    """Phase 1-2: _live_equity, _build_watchlist_rows, render_alpaca_panel."""

    def test_live_equity_returns_alpaca_equity_when_connected(self):
        with patch.object(dashboard.alpaca_api, "get_account_summary",
                          return_value={"connected": True, "equity": 50_000}):
            self.assertEqual(dashboard._live_equity(), 50_000.0)

    def test_live_equity_falls_back_when_disconnected(self):
        with patch.object(dashboard.alpaca_api, "get_account_summary",
                          return_value={"connected": False, "equity": 0}):
            self.assertEqual(dashboard._live_equity(fallback=10_000), 10_000)

    def test_build_watchlist_rows_ranks_by_quant_then_ai_then_change(self):
        analyses = [
            {"Ticker": "LOW",  "Score": 55, "Change %": 0.5, "Rating": "HOLD",
             "Price": 10, "Trend": "STRONG UP", "PE Ratio": 25,
             "20D Gain %": 1, "Analysis": "x"},
            {"Ticker": "HIGH", "Score": 80, "Change %": 3.0, "Rating": "BUY",
             "Price": 50, "Trend": "STRONG UP", "PE Ratio": 20,
             "20D Gain %": 5, "Analysis": "x"},
            {"Ticker": "MID",  "Score": 65, "Change %": 2.0, "Rating": "BUY",
             "Price": 30, "Trend": "STRONG UP", "PE Ratio": 22,
             "20D Gain %": 3, "Analysis": "x"},
        ]
        funds = {t["Ticker"]: {"pe": 25, "growth": 0.1} for t in analyses}
        with patch.object(dashboard.ai_score_cache, "get", return_value=None):
            ranked = dashboard._build_watchlist_rows(analyses, funds)
        self.assertEqual([r["ticker"] for r in ranked], ["HIGH", "MID", "LOW"])
        self.assertEqual([r["rank"] for r in ranked], [1, 2, 3])

    def test_build_watchlist_rows_defensive_coercion(self):
        """Stringly Score / None change_pct shouldn't kill the sort."""
        analyses = [
            {"Ticker": "OK", "Score": 70, "Change %": 2.5, "Rating": "BUY",
             "Price": 100, "Trend": "STRONG UP", "PE Ratio": 25,
             "20D Gain %": 5, "Analysis": "x"},
            {"Ticker": "STR", "Score": "65", "Change %": 1.0, "Rating": "BUY",
             "Price": 50, "Trend": "STRONG UP", "PE Ratio": 22,
             "20D Gain %": 3, "Analysis": "x"},
            {"Ticker": "NULL", "Score": 60, "Change %": None, "Rating": "BUY",
             "Price": 30, "Trend": "STRONG UP", "PE Ratio": 20,
             "20D Gain %": 1, "Analysis": "x"},
        ]
        funds = {t["Ticker"]: {"pe": 25, "growth": 0.1} for t in analyses}
        with patch.object(dashboard.ai_score_cache, "get", return_value=None):
            ranked = dashboard._build_watchlist_rows(analyses, funds)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["ticker"], "OK")  # 70 highest


class TestAlpacaAdditions(unittest.TestCase):
    """Bracket orders, account summary, portfolio history, open orders, client_order_id, lazy reconnect."""

    def _connected_executor(self):
        from alpaca_executor import LiveAlpacaExecutor
        ex = LiveAlpacaExecutor()
        ex.client = MagicMock()
        ex.is_connected = True
        return ex

    def test_bracket_order_request_shape(self):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderClass
        ex = self._connected_executor()
        msg = ex.submit_bracket_order("NVDA", "buy", 10, sl=95.0, tp=120.0)
        self.assertIn("BRACKET", msg)
        order = ex.client.submit_order.call_args.kwargs["order_data"]
        self.assertIsInstance(order, MarketOrderRequest)
        self.assertEqual(order.order_class, OrderClass.BRACKET)
        self.assertEqual(float(order.take_profit.limit_price), 120.0)
        self.assertEqual(float(order.stop_loss.stop_price), 95.0)
        self.assertTrue(order.client_order_id.startswith("nuroq-"))

    def test_bracket_guards(self):
        ex = self._connected_executor()
        # zero shares
        self.assertIn("rejected", ex.submit_bracket_order("X", "buy", 0, sl=10, tp=20).lower())
        # BUY: SL must be < TP
        self.assertIn("rejected", ex.submit_bracket_order("X", "buy", 1, sl=20, tp=10).lower())
        # SELL: SL must be > TP
        self.assertIn("rejected", ex.submit_bracket_order("X", "sell", 1, sl=10, tp=20).lower())
        # zero prices
        self.assertIn("rejected", ex.submit_bracket_order("X", "buy", 1, sl=0, tp=10).lower())

    def test_close_position_calls_client(self):
        ex = self._connected_executor()
        msg = ex.close_position("AAPL")
        self.assertIn("closed", msg.lower())
        ex.client.close_position.assert_called_once_with("AAPL")

    def test_get_account_summary_shape_when_connected(self):
        ex = self._connected_executor()
        fake = MagicMock()
        fake.equity = "100000"
        fake.cash = "80000"
        fake.buying_power = "180000"
        fake.last_equity = "99000"
        fake.status = MagicMock(value="ACTIVE")
        ex.client.get_account.return_value = fake
        s = ex.get_account_summary()
        self.assertTrue(s["connected"])
        self.assertEqual(s["equity"], 100000.0)
        self.assertEqual(s["positions_value"], 20000.0)
        self.assertEqual(s["todays_pl"], 1000.0)

    def test_get_account_summary_shape_when_disconnected(self):
        from alpaca_executor import LiveAlpacaExecutor
        with patch.dict(os.environ, {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}):
            ex = LiveAlpacaExecutor()
            s = ex.get_account_summary()
            self.assertFalse(s["connected"])
            self.assertEqual(s["equity"], 0.0)

    def test_get_portfolio_history_computes_return_pct(self):
        ex = self._connected_executor()
        fake = MagicMock()
        fake.equity = [10000.0, 10500.0, 11000.0]
        fake.timestamp = [1, 2, 3]
        ex.client.get_portfolio_history.return_value = fake
        h = ex.get_portfolio_history(period_days=30)
        self.assertAlmostEqual(h["return_pct"], 10.0)
        self.assertEqual(len(h["equity_series"]), 3)

    def test_get_open_orders_parses_alpaca_shape(self):
        import datetime
        ex = self._connected_executor()
        fake_order = MagicMock()
        fake_order.id = "abc"
        fake_order.symbol = "NVDA"
        fake_order.side = MagicMock(value="buy")
        fake_order.qty = "10"
        fake_order.order_type = MagicMock(value="market")
        fake_order.order_class = MagicMock(value="bracket")
        fake_order.limit_price = None
        fake_order.stop_price = None
        fake_order.status = MagicMock(value="new")
        fake_order.submitted_at = datetime.datetime(2026, 5, 23, 18, 30)
        ex.client.get_orders.return_value = [fake_order]
        orders = ex.get_open_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["side"], "BUY")
        self.assertTrue(orders[0]["is_bracket"])

    def test_client_order_id_unique_per_call(self):
        ex = self._connected_executor()
        ex.submit_advanced_order("X", "buy", 1, "Market")
        ex.submit_advanced_order("X", "buy", 1, "Market")
        ex.submit_bracket_order("X", "buy", 1, sl=10, tp=20)
        ids = [c.kwargs["order_data"].client_order_id
               for c in ex.client.submit_order.call_args_list]
        self.assertEqual(len(set(ids)), 3, "client_order_ids must be unique per call")

    def test_lazy_reconnect_attempted_on_disconnected(self):
        from alpaca_executor import LiveAlpacaExecutor
        with patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}), \
             patch("alpaca_executor.TradingClient") as mock_client_class:
            mock_client_class.side_effect = Exception("network down")
            ex = LiveAlpacaExecutor()
            self.assertFalse(ex.is_connected)
            # Subsequent call to _ensure_connection should try TradingClient again
            mock_client_class.reset_mock()
            mock_client_class.side_effect = Exception("still down")
            ex._ensure_connection()
            self.assertTrue(mock_client_class.called)


class TestStreamerAndLiveAgent(unittest.TestCase):
    """MarketStreamer bar_callback path + LiveAgent crossings."""

    def test_bar_callback_fires_inline_on_every_bar(self):
        """The Phase 3 every-bar path runs inline, no debounce."""
        import asyncio
        from event_stream import MarketStreamer
        fired = []
        ms = MarketStreamer(
            trigger_callback=lambda t: None,
            bar_callback=lambda bar: fired.append(bar.symbol),
            debounce_seconds=99999,  # long debounce for the OTHER callback
        )
        bar = MagicMock(symbol="NVDA", close=100.0, high=101.0, low=99.0, volume=1000)
        asyncio.run(ms._handle_bar(bar))
        asyncio.run(ms._handle_bar(bar))
        # bar_callback fires on every bar, ignoring debounce
        self.assertEqual(fired, ["NVDA", "NVDA"])

    def test_set_watchlist_uses_call_soon_threadsafe(self):
        """Regression: subscribe_bars is sync, must not be wrapped in run_coroutine_threadsafe."""
        from event_stream import MarketStreamer
        import asyncio, threading
        ms = MarketStreamer(trigger_callback=lambda t: None)
        ms.loop = asyncio.new_event_loop()
        ms.stream = MagicMock()
        ms.is_running = True
        loop_thread = threading.Thread(target=ms.loop.run_forever, daemon=True)
        loop_thread.start()
        try:
            ms.set_watchlist(["AAPL"])
            time.sleep(0.1)
            self.assertEqual(ms.stream.subscribe_bars.call_count, 1)
        finally:
            ms.loop.call_soon_threadsafe(ms.loop.stop)
            loop_thread.join(timeout=1.0)

    def test_liveagent_market_hours_guard(self):
        from live_agent import LiveAgent, is_market_hours
        la = LiveAgent(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda *a: None,
            fire_sell_callback=lambda *a: None,
            get_held_tickers=lambda: [],
        )
        if not is_market_hours():
            msg = la.start(force=False)
            self.assertIn("closed", msg.lower())
            self.assertFalse(la.is_running)

    def test_liveagent_check_buy_crossing_fires(self):
        from live_agent import LiveAgent, TickerState
        fired = []
        la = LiveAgent(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda t, p, s, r: fired.append((t, s)),
            fire_sell_callback=lambda *a: None,
            get_held_tickers=lambda: [],
            daily_buy_cap=10,
            hysteresis_bars=1, per_ticker_cooldown_s=0,
        )
        # Clean live_triggers for this ticker so the cap check starts fresh
        import sqlite3
        from data_fetcher import live_triggers
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute("DELETE FROM live_triggers WHERE ticker = 'XCROSS'")
        la.state["XCROSS"] = TickerState(
            ticker="XCROSS", baseline_bars=[], weekly_trend="UP",
            last_price=100.0, last_score=60,
        )
        la._check_crossings(la.state["XCROSS"], 70)  # crosses 65 from 60
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], ("XCROSS", 70))
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute("DELETE FROM live_triggers WHERE ticker = 'XCROSS'")

    def test_liveagent_sell_crossing_held_only(self):
        from live_agent import LiveAgent, TickerState
        fired = []
        la = LiveAgent(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda *a: None,
            fire_sell_callback=lambda t, p, s, r: fired.append(t),
            get_held_tickers=lambda: [],
            hysteresis_bars=1, per_ticker_cooldown_s=0,
        )
        import sqlite3
        from data_fetcher import live_triggers
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute("DELETE FROM live_triggers WHERE ticker IN ('YHELD','YFREE')")

        held = TickerState(ticker="YHELD", baseline_bars=[], weekly_trend="DOWN",
                           last_price=100.0, last_score=50, is_held_position=True)
        free = TickerState(ticker="YFREE", baseline_bars=[], weekly_trend="DOWN",
                           last_price=100.0, last_score=50, is_held_position=False)
        la.state["YHELD"] = held; la.state["YFREE"] = free

        la._check_crossings(held, 25)   # crosses 30 from above on held → fires
        la._check_crossings(free, 25)   # not held → does NOT fire
        self.assertEqual(fired, ["YHELD"])

        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute("DELETE FROM live_triggers WHERE ticker IN ('YHELD','YFREE')")

    def test_liveagent_daily_buy_cap(self):
        from live_agent import LiveAgent, TickerState
        import sqlite3
        from data_fetcher import live_triggers
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute(
                "DELETE FROM live_triggers WHERE ticker IN ('CAP1','CAP2','CAP3','CAP4')"
            )
        fired = []
        la = LiveAgent(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda t, *a: fired.append(t),
            fire_sell_callback=lambda *a: None,
            get_held_tickers=lambda: [],
            daily_buy_cap=2,
            hysteresis_bars=1, per_ticker_cooldown_s=0,
        )
        for tk in ["CAP1", "CAP2", "CAP3", "CAP4"]:
            la.state[tk] = TickerState(ticker=tk, baseline_bars=[], weekly_trend="UP",
                                       last_price=10, last_score=60)
            la._check_crossings(la.state[tk], 70)
        self.assertEqual(len(fired), 2, "cap=2 should fire only first 2 of 4")
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute(
                "DELETE FROM live_triggers WHERE ticker IN ('CAP1','CAP2','CAP3','CAP4')"
            )

    def test_liveagent_status_shape(self):
        from live_agent import LiveAgent
        la = LiveAgent(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda *a: None,
            fire_sell_callback=lambda *a: None,
            get_held_tickers=lambda: [],
        )
        s = la.status()
        for key in ("running", "subscribed_tickers", "bars_processed",
                    "buys_fired_today", "buys_cap", "sells_fired_today"):
            self.assertIn(key, s)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: News engine
# ═══════════════════════════════════════════════════════════════════════════

class TestNewsClassifier(unittest.TestCase):
    """Keyword-based headline classification — 4 buckets, priority order."""

    def test_positive_boost_recognized(self):
        from news_engine import NewsClassifier
        v = NewsClassifier.classify("NVDA beats earnings estimates by 30%")
        self.assertEqual(v.classification, "POSITIVE_BOOST")

    def test_negative_warning_recognized(self):
        from news_engine import NewsClassifier
        v = NewsClassifier.classify("Goldman Sachs downgrades AAPL to Neutral")
        self.assertEqual(v.classification, "NEGATIVE_WARNING")

    def test_negative_block_recognized(self):
        from news_engine import NewsClassifier
        v = NewsClassifier.classify("Trading halted in XYZ pending SEC investigation")
        self.assertEqual(v.classification, "NEGATIVE_BLOCK")

    def test_neutral_for_uninteresting_headline(self):
        from news_engine import NewsClassifier
        v = NewsClassifier.classify("Company X announces new chief financial officer")
        self.assertEqual(v.classification, "NEUTRAL")

    def test_block_beats_warning_when_both_match(self):
        """If a headline matches both block AND warning keywords, BLOCK wins."""
        from news_engine import NewsClassifier
        # 'halt' (BLOCK) + 'lawsuit' (WARNING) both present
        v = NewsClassifier.classify("Trading halted as lawsuit filed against XYZ")
        self.assertEqual(v.classification, "NEGATIVE_BLOCK")

    def test_empty_headline_returns_neutral(self):
        from news_engine import NewsClassifier
        v = NewsClassifier.classify("")
        self.assertEqual(v.classification, "NEUTRAL")


class TestNewsCache(unittest.TestCase):
    """SQLite-backed news + classification cache for Phase 4."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.remove(self.tmp.name)

    def test_store_and_get_latest(self):
        from data_fetcher import NewsCache
        nc = NewsCache(db_path=self.tmp.name)
        wrote = nc.store("NVDA", "Beats estimates", "POSITIVE_BOOST", "polygon", "2026-05-23T18:00:00Z")
        self.assertTrue(wrote)
        latest = nc.get_latest_classification("NVDA")
        self.assertEqual(latest["classification"], "POSITIVE_BOOST")
        self.assertEqual(latest["headline"], "Beats estimates")

    def test_store_dedupes_same_headline(self):
        """INSERT OR IGNORE prevents the same headline being re-stored."""
        from data_fetcher import NewsCache
        nc = NewsCache(db_path=self.tmp.name)
        self.assertTrue(nc.store("NVDA", "Same news", "NEUTRAL"))
        self.assertFalse(nc.store("NVDA", "Same news", "NEUTRAL"))  # second call → False

    def test_latest_returns_most_recent_ingested(self):
        from data_fetcher import NewsCache
        nc = NewsCache(db_path=self.tmp.name)
        nc.store("NVDA", "First headline", "NEUTRAL")
        time.sleep(0.01)
        nc.store("NVDA", "Latest blockbuster", "POSITIVE_BOOST")
        latest = nc.get_latest_classification("NVDA")
        self.assertEqual(latest["headline"], "Latest blockbuster")

    def test_ttl_expires_old_entries_from_get_latest(self):
        from data_fetcher import NewsCache
        nc = NewsCache(db_path=self.tmp.name, ttl_hours=0.0001)  # ~0.36s
        nc.store("X", "old news", "NEUTRAL")
        self.assertIsNotNone(nc.get_latest_classification("X"))
        time.sleep(0.5)
        self.assertIsNone(nc.get_latest_classification("X"))


class TestWatchlistUIHelpers(unittest.TestCase):
    """Watchlist tab + research-cycle status text."""

    @classmethod
    def setUpClass(cls):
        # Redirect the module-level watchlist_today singleton to a temp DB so
        # these tests don't wipe the production nuroq.db (the singleton is
        # imported into dashboard at startup and bound to the real path).
        import tempfile
        from data_fetcher import watchlist_today
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        cls._orig_db_path = watchlist_today.db_path
        watchlist_today.db_path = cls._tmp.name
        watchlist_today._init_table()

    @classmethod
    def tearDownClass(cls):
        from data_fetcher import watchlist_today
        watchlist_today.db_path = cls._orig_db_path
        try:
            os.remove(cls._tmp.name)
        except OSError:
            pass

    def _seed_watchlist(self):
        from data_fetcher import watchlist_today
        watchlist_today.replace_all([
            {"ticker": "NVDA", "rank": 1, "quant_score": 78, "ai_score": 85,
             "recommendation": "BUY", "price": 215.33, "change_pct": 2.18,
             "technicals_summary": "STRONG UP | RSI 58", "fundamentals_summary": "P/E 35"},
            {"ticker": "AAPL", "rank": 2, "quant_score": 68, "ai_score": 75,
             "recommendation": "BUY", "price": 308.82, "change_pct": 1.10,
             "technicals_summary": "STRONG UP | RSI 60", "fundamentals_summary": "P/E 30"},
        ])

    def _clear_watchlist(self):
        import sqlite3
        from data_fetcher import watchlist_today
        with sqlite3.connect(watchlist_today.db_path) as conn:
            conn.execute("DELETE FROM watchlist_today")

    def test_watchlist_metadata_when_empty(self):
        from data_fetcher import watchlist_today
        self._clear_watchlist()
        meta = watchlist_today.get_metadata()
        self.assertEqual(meta["count"], 0)
        self.assertIsNone(meta["generated_at"])

    def test_watchlist_metadata_when_populated(self):
        from data_fetcher import watchlist_today
        self._seed_watchlist()
        meta = watchlist_today.get_metadata()
        self.assertEqual(meta["count"], 2)
        self.assertIsNotNone(meta["generated_at"])
        # Should be within the last few seconds
        self.assertLess(time.time() - meta["generated_at"], 5)
        self._clear_watchlist()

    def test_render_watchlist_buys_empty(self):
        self._clear_watchlist()
        df = dashboard.render_watchlist_buys()
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), dashboard.WATCHLIST_COLS)

    def test_render_watchlist_buys_populated(self):
        """Seeded with 2 BUYs — both should be returned by render_watchlist_buys()."""
        self._seed_watchlist()
        df = dashboard.render_watchlist_buys()
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["Ticker"], "NVDA")
        self.assertEqual(df.iloc[0]["Rank"], 1)
        self.assertEqual(df.iloc[0]["Rating"], "BUY")
        self.assertIn("$215.33", df.iloc[0]["Price"])
        self._clear_watchlist()

    def test_render_watchlist_buys_vs_holds_split(self):
        """Mixed seed: 1 BUY + 1 HOLD → render_watchlist_buys returns 1, render_watchlist_holds returns 1."""
        from data_fetcher import watchlist_today
        watchlist_today.replace_all([
            {"ticker": "NVDA", "rank": 1, "quant_score": 78, "ai_score": 85,
             "recommendation": "BUY", "price": 215.33, "change_pct": 2.18,
             "technicals_summary": "STRONG UP", "fundamentals_summary": "P/E 35"},
            {"ticker": "AMD", "rank": 2, "quant_score": 55, "ai_score": 65,
             "recommendation": "HOLD", "price": 110.0, "change_pct": 0.50,
             "technicals_summary": "Neutral", "fundamentals_summary": "P/E 40"},
        ])
        buys = dashboard.render_watchlist_buys()
        holds = dashboard.render_watchlist_holds()
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys.iloc[0]["Ticker"], "NVDA")
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds.iloc[0]["Ticker"], "AMD")
        self._clear_watchlist()

    def test_refresh_watchlist_view_returns_three_outputs(self):
        """Refresh callback must return (header_md, buys_df, holds_df) for the 3 UI outputs."""
        self._seed_watchlist()
        result = dashboard.refresh_watchlist_view()
        self.assertEqual(len(result), 3)
        header, buys, holds = result
        self.assertIn("Today's Watchlist", header)
        self.assertEqual(len(buys), 2)
        self.assertEqual(len(holds), 0)
        self._clear_watchlist()

    def test_render_watchlist_header_empty(self):
        self._clear_watchlist()
        header = dashboard.render_watchlist_header()
        # New empty-state card uses "No watchlist yet" + a CTA explaining the
        # research cycle. Test asserts on the canonical phrase from the card.
        self.assertIn("No watchlist yet", header)
        self.assertIn("Run Research Cycle", header)

    def test_render_watchlist_header_populated_shows_breakdown(self):
        """Header must show BUY/HOLD breakdown, not just total count."""
        from data_fetcher import watchlist_today
        watchlist_today.replace_all([
            {"ticker": "A", "rank": 1, "quant_score": 70, "ai_score": 80,
             "recommendation": "BUY", "price": 10, "change_pct": 1,
             "technicals_summary": "x", "fundamentals_summary": "y"},
            {"ticker": "B", "rank": 2, "quant_score": 55, "ai_score": 70,
             "recommendation": "HOLD", "price": 20, "change_pct": 0.5,
             "technicals_summary": "x", "fundamentals_summary": "y"},
        ])
        header = dashboard.render_watchlist_header()
        self.assertIn("Today's Watchlist", header)
        self.assertIn("1 BUY", header)
        self.assertIn("1 HOLD", header)
        self.assertIn("Generated", header)
        self._clear_watchlist()

    def test_research_status_text_empty_state(self):
        self._clear_watchlist()
        text = dashboard._research_status_text()
        self.assertIn("No research cycle yet", text)

    def test_research_status_text_idle_with_last_cycle(self):
        self._seed_watchlist()
        text = dashboard._research_status_text()
        self.assertIn("Last cycle", text)
        self.assertIn("2 candidates", text)
        self._clear_watchlist()


class TestLiveAgentNewsFinalCheck(unittest.TestCase):
    """LiveAgent._handle_buy_crossing must respect news classifications."""

    def _make_agent(self, daily_buy_cap=10):
        from live_agent import LiveAgent
        fired = []
        la = LiveAgent(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda t, p, s, r: fired.append((t, r)),
            fire_sell_callback=lambda *a: None,
            get_held_tickers=lambda: [],
            daily_buy_cap=daily_buy_cap,
        )
        return la, fired

    def _make_state(self, ticker, last_score=60):
        from live_agent import TickerState
        return TickerState(
            ticker=ticker, baseline_bars=[], weekly_trend="UP",
            last_price=100.0, last_score=last_score,
        )

    def _clean_triggers(self, ticker):
        import sqlite3
        from data_fetcher import live_triggers
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute("DELETE FROM live_triggers WHERE ticker = ?", (ticker,))

    def test_news_block_suppresses_buy(self):
        self._clean_triggers("NEWSBLK")
        la, fired = self._make_agent()
        state = self._make_state("NEWSBLK", last_score=60)
        la.state["NEWSBLK"] = state

        with patch("news_engine.check_news_for_crossing",
                   return_value={"classification": "NEGATIVE_BLOCK",
                                 "headline": "Trading halted",
                                 "source": "polygon",
                                 "published_at": None, "ingested_at": time.time()}):
            la._handle_buy_crossing(state, prev=60, new=70)

        self.assertEqual(fired, [], "BLOCK should suppress _fire_buy")
        # Verify SUPPRESSED_NEWS row was logged
        from data_fetcher import live_triggers
        recent = live_triggers.get_recent(10)
        rows = [r for r in recent if r["ticker"] == "NEWSBLK"]
        self.assertEqual(rows[0]["action"], "SUPPRESSED_NEWS")
        self._clean_triggers("NEWSBLK")

    def test_news_warning_decorates_reasoning(self):
        self._clean_triggers("NEWSWARN")
        la, fired = self._make_agent()
        state = self._make_state("NEWSWARN", last_score=60)
        la.state["NEWSWARN"] = state

        with patch("news_engine.check_news_for_crossing",
                   return_value={"classification": "NEGATIVE_WARNING",
                                 "headline": "Downgrade to Neutral",
                                 "source": "polygon",
                                 "published_at": None, "ingested_at": time.time()}):
            la._handle_buy_crossing(state, prev=60, new=70)

        self.assertEqual(len(fired), 1)
        ticker, reasoning = fired[0]
        self.assertEqual(ticker, "NEWSWARN")
        self.assertIn("Recent negative news", reasoning)
        self.assertIn("Downgrade to Neutral", reasoning)
        self._clean_triggers("NEWSWARN")

    def test_news_boost_decorates_reasoning(self):
        self._clean_triggers("NEWSBOOST")
        la, fired = self._make_agent()
        state = self._make_state("NEWSBOOST", last_score=60)
        la.state["NEWSBOOST"] = state

        with patch("news_engine.check_news_for_crossing",
                   return_value={"classification": "POSITIVE_BOOST",
                                 "headline": "Beats estimates by 30%",
                                 "source": "polygon",
                                 "published_at": None, "ingested_at": time.time()}):
            la._handle_buy_crossing(state, prev=60, new=70)

        self.assertEqual(len(fired), 1)
        _, reasoning = fired[0]
        self.assertIn("Catalyst", reasoning)
        self.assertIn("Beats estimates", reasoning)
        self._clean_triggers("NEWSBOOST")

    def test_no_news_fires_normally(self):
        self._clean_triggers("NONEWS")
        la, fired = self._make_agent()
        state = self._make_state("NONEWS", last_score=60)
        la.state["NONEWS"] = state

        with patch("news_engine.check_news_for_crossing", return_value=None):
            la._handle_buy_crossing(state, prev=60, new=70)

        self.assertEqual(len(fired), 1)
        _, reasoning = fired[0]
        # No news tag should be present
        self.assertNotIn("Catalyst", reasoning)
        self.assertNotIn("Recent negative news", reasoning)
        self._clean_triggers("NONEWS")


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3b: Reconnect / staleness / hysteresis / cooldown
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamerStaleness(unittest.TestCase):
    """Phase 3b: check_staleness triggers alert when bars stop flowing."""

    def test_check_staleness_returns_none_when_healthy(self):
        from event_stream import MarketStreamer
        ms = MarketStreamer(trigger_callback=lambda t: None,
                            stale_bar_alert_seconds=300)
        ms.is_running = True
        ms.last_bar_received_at = time.time()  # just got a bar
        self.assertIsNone(ms.check_staleness())

    def test_check_staleness_returns_report_and_fires_callback(self):
        from event_stream import MarketStreamer
        alerts = []
        ms = MarketStreamer(trigger_callback=lambda t: None,
                            stale_bar_alert_seconds=1)
        ms.is_running = True
        ms.last_bar_received_at = time.time() - 30   # 30s ago, > threshold
        ms.stale_alert_callback = lambda report: alerts.append(report)
        report = ms.check_staleness()
        self.assertIsNotNone(report)
        self.assertGreaterEqual(report["seconds_since_last_bar"], 1)
        self.assertEqual(len(alerts), 1)
        # Second call within same staleness episode shouldn't re-fire callback
        ms.check_staleness()
        self.assertEqual(len(alerts), 1, "callback should only fire once per episode")

    def test_check_staleness_resets_after_bar_resumes(self):
        import asyncio
        from event_stream import MarketStreamer
        ms = MarketStreamer(trigger_callback=lambda t: None,
                            stale_bar_alert_seconds=1)
        ms.is_running = True
        ms.last_bar_received_at = time.time() - 30
        ms.stale_alert_callback = lambda report: None
        ms.check_staleness()
        self.assertTrue(ms._stale_alert_fired)
        # New bar arrives → flag must reset
        bar = MagicMock(symbol="X", close=1.0, high=1.0, low=1.0, volume=1)
        asyncio.run(ms._handle_bar(bar))
        self.assertFalse(ms._stale_alert_fired)


class TestLiveAgentHysteresisAndCooldown(unittest.TestCase):
    """Phase 3b noise gates."""

    def _make_agent(self, **kwargs):
        from live_agent import LiveAgent
        defaults = dict(
            streamer=MagicMock(), logger=MagicMock(),
            fire_buy_callback=lambda *a: None,
            fire_sell_callback=lambda *a: None,
            get_held_tickers=lambda: [],
            daily_buy_cap=10,
        )
        defaults.update(kwargs)
        return LiveAgent(**defaults)

    def _clean(self, ticker):
        import sqlite3
        from data_fetcher import live_triggers
        with sqlite3.connect(live_triggers.db_path) as conn:
            conn.execute("DELETE FROM live_triggers WHERE ticker = ?", (ticker,))

    def test_hysteresis_blocks_single_bar_crossing(self):
        """With hysteresis_bars=2, a single crossing bar should NOT fire."""
        from live_agent import TickerState
        self._clean("HYST1")
        fired = []
        la = self._make_agent(
            fire_buy_callback=lambda *a: fired.append(a),
            hysteresis_bars=2, per_ticker_cooldown_s=0,
        )
        la.state["HYST1"] = TickerState(ticker="HYST1", baseline_bars=[],
                                         weekly_trend="UP", last_price=10, last_score=60)
        la._check_crossings(la.state["HYST1"], 70)   # 1 bar above threshold
        self.assertEqual(fired, [], "single-bar crossing should be blocked by hysteresis")
        self._clean("HYST1")

    def test_hysteresis_fires_on_second_consecutive_bar(self):
        """With hysteresis_bars=2 + 2 consecutive crossing bars, should fire."""
        from live_agent import TickerState
        self._clean("HYST2")
        fired = []
        la = self._make_agent(
            fire_buy_callback=lambda *a: fired.append(a),
            hysteresis_bars=2, per_ticker_cooldown_s=0,
        )
        la.state["HYST2"] = TickerState(ticker="HYST2", baseline_bars=[],
                                         weekly_trend="UP", last_price=10, last_score=60)
        # First crossing bar — increments counter to 1, doesn't fire (need 2)
        la._check_crossings(la.state["HYST2"], 70)
        la.state["HYST2"].last_score = 70
        # Second bar at threshold — counter becomes 2 → fires
        # But: prev (70) < BUY_THRESHOLD (65) is FALSE, so the crossing-detection
        # branch doesn't trigger. The hysteresis only matters AT the crossing edge.
        # So crossing must arrive on bar where bars_above_buy has already accumulated.
        # Reset: drop below, then cross again sustained.
        la.state["HYST2"].last_score = 60
        la.state["HYST2"].bars_above_buy = 1   # already had 1 above
        la._check_crossings(la.state["HYST2"], 70)   # bars_above_buy becomes 2 → fires
        self.assertEqual(len(fired), 1)
        self._clean("HYST2")

    def test_per_ticker_cooldown_blocks_rapid_refire(self):
        """After firing once, another crossing within cooldown should NOT fire."""
        from live_agent import TickerState
        self._clean("CD1")
        fired = []
        la = self._make_agent(
            fire_buy_callback=lambda *a: fired.append(a),
            hysteresis_bars=1, per_ticker_cooldown_s=600,
        )
        la.state["CD1"] = TickerState(ticker="CD1", baseline_bars=[],
                                       weekly_trend="UP", last_price=10, last_score=60)
        la._check_crossings(la.state["CD1"], 70)
        self.assertEqual(len(fired), 1)
        # Reset score to allow another crossing attempt
        la.state["CD1"].last_score = 60
        la._check_crossings(la.state["CD1"], 70)
        self.assertEqual(len(fired), 1, "cooldown should block rapid re-fire")
        self._clean("CD1")


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4b: LLM rescore queue
# ═══════════════════════════════════════════════════════════════════════════

class TestLLMRescoreQueue(unittest.TestCase):
    def test_enqueue_dedups_same_ticker(self):
        from llm_queue import LLMRescoreQueue
        # Don't start the worker — we just test the dedup logic.
        q = LLMRescoreQueue(run_analysis=lambda t: None, logger=MagicMock())
        self.assertTrue(q.enqueue("NVDA"))
        self.assertFalse(q.enqueue("NVDA"), "second enqueue of same ticker should dedup")
        self.assertTrue(q.enqueue("AAPL"))
        self.assertEqual(q.total_enqueued, 2)
        self.assertEqual(q.total_dedup_drops, 1)

    def test_worker_processes_then_releases_dedup_slot(self):
        from llm_queue import LLMRescoreQueue
        processed = []
        q = LLMRescoreQueue(
            run_analysis=lambda t: processed.append(t),
            logger=MagicMock(),
            worker_idle_sleep_s=0,
        )
        q.start()
        try:
            q.enqueue("X")
            # Wait briefly for worker
            for _ in range(20):
                if processed: break
                time.sleep(0.05)
            self.assertEqual(processed, ["X"])
            # After processing, the slot should be released — same ticker enqueueable again
            for _ in range(10):
                # Let _pending clear
                time.sleep(0.05)
                if q.enqueue("X"):
                    break
            self.assertEqual(q.total_processed, 1)
        finally:
            q.stop()


class TestNewsPollerShockCallback(unittest.TestCase):
    """Phase 4b: NewsPoller dispatches on_shock_callback for non-NEUTRAL classifications."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.remove(self.tmp.name)

    def test_poller_dispatches_shock_callback(self):
        from news_engine import NewsPoller
        shocks = []
        # Use a fresh NewsCache wired to the tmp db; need to monkey-patch the
        # singleton news_cache_v2 used by the poller.
        from data_fetcher import NewsCache
        import news_engine
        old_cache = news_engine.news_cache_v2
        news_engine.news_cache_v2 = NewsCache(db_path=self.tmp.name)
        try:
            poller = NewsPoller(
                get_tickers_fn=lambda: ["XYZ"],
                logger=MagicMock(),
                polygon_api_key="fake",
                interval_seconds=999999,
                on_shock_callback=lambda t, v: shocks.append((t, v.classification)),
            )
            # Mock the Polygon REST response with a BLOCK headline
            with patch("news_engine.requests.get") as mock_get, \
                 patch("news_engine.rate_limiter.wait"):
                mock_resp = MagicMock()
                mock_resp.json.return_value = {
                    "results": [
                        {"title": "Trading halted in XYZ — SEC investigation"},
                        {"title": "XYZ reports better than expected revenue beat"},
                        {"title": "Random uninteresting filing"},
                    ]
                }
                mock_get.return_value = mock_resp
                poller._poll_ticker("XYZ")

            # 2 non-NEUTRAL → 2 shocks dispatched
            self.assertEqual(len(shocks), 2)
            classifications = sorted([c for _, c in shocks])
            self.assertEqual(classifications, ["NEGATIVE_BLOCK", "POSITIVE_BOOST"])
        finally:
            news_engine.news_cache_v2 = old_cache


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5: Health snapshot
# ═══════════════════════════════════════════════════════════════════════════

class TestReasoningDriftDetector(unittest.TestCase):
    """
    Regression for the SONY → GameStop bug (2026-05-24): Gemma 4B produced
    GameStop-flavored reasoning when asked about SONY because the prompt
    didn't front-load the ticker/company.

    The fix is two-layered: prompt restructuring (covered by
    TestPromptAntiDrift below) AND a runtime drift detector that catches
    leftover hallucinations and prevents cache contamination. These tests
    exercise the detector with the EXACT bad text the user observed.
    """

    # The actual reasoning that showed up in the user's UI for ticker=SONY
    SONY_BUG_REASONING = (
        "GameStop is currently trading with a strong upward trend on both "
        "daily and weekly charts, supported by a neutral RSI and %B indicator. "
        "Despite a 14.1% revenue decline, the company demonstrated impressive "
        "earnings growth, and maintains a substantial cash reserve. However, "
        "the company is transitioning into a holding company, and faces "
        "headwinds from rising memory costs and declining smartphone shipments "
        "impacting Sony's semiconductor division."
    )
    SONY_BUG_CONSIDERATIONS = [
        "Holding company strategy",
        "Semiconductor division performance",
        "Ryan Cohen's track record",
    ]

    def test_sony_screenshot_bug_is_detected(self):
        """The exact reasoning text from the SONY bug screenshot must trigger drift."""
        from dashboard import detect_reasoning_drift
        text = self.SONY_BUG_REASONING + " " + " ".join(self.SONY_BUG_CONSIDERATIONS)
        marker = detect_reasoning_drift(text, ticker="SONY",
                                         company_name="Sony Group Corporation")
        self.assertIsNotNone(marker,
                             "drift detector must catch the SONY/GameStop scenario")
        # The marker should be a GME-origin marker (Ryan Cohen or GameStop)
        self.assertIn(marker, ("GameStop", "Ryan Cohen"))

    def test_considerations_alone_can_trigger_drift(self):
        """Even if `reasoning` is generic, GME markers in `considerations` should trigger."""
        from dashboard import detect_reasoning_drift
        text = "The setup looks reasonable. " + " ".join(self.SONY_BUG_CONSIDERATIONS)
        marker = detect_reasoning_drift(text, ticker="SONY",
                                         company_name="Sony Group Corporation")
        self.assertIsNotNone(marker)

    def test_clean_sony_reasoning_passes(self):
        """A correctly-grounded SONY analysis must NOT trigger drift."""
        from dashboard import detect_reasoning_drift
        text = (
            "Sony Group Corporation shows a strong upward trend with healthy "
            "semiconductor demand. The PlayStation segment provides cash flow "
            "while the music division grows."
        )
        self.assertIsNone(detect_reasoning_drift(text, ticker="SONY",
                                                  company_name="Sony Group Corporation"))

    def test_gme_analysis_mentioning_ryan_cohen_is_fine(self):
        """Analyzing GME and mentioning Ryan Cohen is NOT drift — it's the actual CEO."""
        from dashboard import detect_reasoning_drift
        text = "GameStop's pivot under Ryan Cohen continues to face skepticism."
        self.assertIsNone(detect_reasoning_drift(text, ticker="GME",
                                                  company_name="GameStop Corp"))

    def test_co_mention_treated_as_comparison_not_drift(self):
        """If both target and foreign company are mentioned, treat as legitimate comparison."""
        from dashboard import detect_reasoning_drift
        text = ("Sony is exhibiting a meme-stock pattern reminiscent of "
                "GameStop in early 2021, but with stronger fundamentals.")
        self.assertIsNone(
            detect_reasoning_drift(text, ticker="SONY",
                                    company_name="Sony Group Corporation"),
            "co-mention should not trigger drift detection"
        )

    def test_apple_drift_in_msft_analysis_caught(self):
        """Sanity: drift detector works for other ticker pairs too."""
        from dashboard import detect_reasoning_drift
        text = "The iPhone product line continues to dominate global handset shipments."
        marker = detect_reasoning_drift(text, ticker="MSFT",
                                         company_name="Microsoft Corporation")
        self.assertEqual(marker, "iPhone")

    def test_empty_text_returns_none(self):
        from dashboard import detect_reasoning_drift
        self.assertIsNone(detect_reasoning_drift("", ticker="SONY"))
        self.assertIsNone(detect_reasoning_drift(None, ticker="SONY"))


class TestPromptAntiDrift(unittest.TestCase):
    """
    Regression: Gemma 4B was hallucinating GameStop content into a SONY analysis
    because the ticker only appeared at the very end of the prompt. The fix
    front-loads ticker + company name. These tests assert the new shape.
    """

    def test_dpo_training_prompt_front_loads_ticker_and_company(self):
        """The DPO training prompt MUST mirror the inference shape."""
        from create_dpo_training_json import build_prompt
        techs = {"trend": "STRONG UP", "rsi": 60, "percent_b": 0.7,
                 "price": 22.14, "semantic_rsi": "NEUTRAL", "semantic_bb": "NEUTRAL"}
        prompt = build_prompt(
            "SONY", techs, pe=20.5, growth=0.15,
            news="Q1 revenue beat", mem_ctx="N/A",
            company_name="Sony Group Corporation", industry="Consumer Electronics",
        )

        # 1. Anti-drift assertion: company name appears EARLY (in the first 300 chars)
        head = prompt[:300]
        self.assertIn("SONY", head, "ticker must appear in prompt head, not just the response anchor")
        self.assertIn("Sony Group Corporation", head, "company name must appear in prompt head")
        self.assertIn("Consumer Electronics", head, "industry must appear in prompt head")

        # 2. Anti-drift assertion: explicit "do not analyze other company" instruction
        self.assertIn("Do not analyze any other company", prompt)

        # 3. Anti-drift assertion: reasoning schema requires ticker reference
        self.assertIn("SONY's setup", prompt)

    def test_dpo_training_prompt_falls_back_to_ticker_when_name_missing(self):
        """build_prompt without a company_name should still produce a coherent prompt."""
        from create_dpo_training_json import build_prompt
        techs = {"trend": "STRONG UP", "rsi": 60, "percent_b": 0.7,
                 "price": 100.0, "semantic_rsi": "NEUTRAL", "semantic_bb": "NEUTRAL"}
        prompt = build_prompt("ABCD", techs)
        # Ticker should still front-load
        self.assertIn("ABCD", prompt[:200])
        # Falls back to ticker as company name
        self.assertIn("ANALYSIS TARGET: ABCD — ABCD", prompt)


class TestHealthSnapshot(unittest.TestCase):
    def test_render_health_snapshot_returns_markdown_table(self):
        snapshot = dashboard.render_health_snapshot()
        self.assertIsInstance(snapshot, str)
        self.assertIn("System Health", snapshot)
        # Table format markers
        self.assertIn("|:---:|", snapshot)
        # Expected components named
        for label in ("Research cycle", "LiveAgent", "NewsPoller",
                      "LLM Rescore Queue", "Alpaca", "SQLite caches"):
            self.assertIn(label, snapshot)

    def test_age_traffic_light_buckets(self):
        from dashboard import _age_traffic_light
        self.assertEqual(_age_traffic_light(2), "🟢")
        self.assertEqual(_age_traffic_light(18), "🟡")
        self.assertEqual(_age_traffic_light(36), "🔴")


if __name__ == "__main__":
    unittest.main(verbosity=2)
