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
        """Test Negative P/E penalty and RSI mean reversion."""
        techs = {"trend": "STRONG UP", "rsi": 20, "rel_vol": 2.0, "percent_b": 0.05, "volatility": 1.0}
        funds_bad_pe = {"pe": -5, "growth": 0.2}
        
        score = calculate_quant_score(techs, funds_bad_pe, "UP", {"risk": False}, "NEUTRAL", 50)
        
        # It should get 0 points for valuation, but lots for RSI oversold and trend
        self.assertTrue(score > 30 and score < 80)

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

if __name__ == "__main__":
    unittest.main(verbosity=2)
