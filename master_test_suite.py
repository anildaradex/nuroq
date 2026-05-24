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
        self.assertIn("No watchlist generated yet", header)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
