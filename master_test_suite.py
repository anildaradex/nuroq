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
        def mock_analyze(prompt, model_key, structured=False):
            return '{"score": 90, "rating": "BUY"}'
                
        self.analyst.analyze = mock_analyze
        res = self.analyst.get_consensus("AAPL", "Prompt")
        
        self.assertTrue(res['is_consensus'])
        self.assertEqual(res['score'], 90)
        self.assertEqual(res['rating'], "BUY")

    def test_consensus_guardrail(self):
        """Test that a BUY rating is downgraded to HOLD if the score is < 70."""
        def mock_analyze(prompt, model_key, structured=False):
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
        """Test portfolio manager adds and removes correctly (SQLite).

        Mocks `alpaca_api.list_positions` to return None so `get_portfolio`
        falls back to the DB read path — otherwise it returns the live
        Alpaca account state (not the seeded test row).
        """
        import tempfile, dashboard
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            test_db = tmp.name
        try:
            with patch.object(dashboard.alpaca_api, "list_positions", return_value=None):
                pm = PortfolioManager(db_path=test_db)
                pm.add_position("NVDA", 10, 100.0)
                df = pm.get_portfolio()
                self.assertIn("NVDA", df['Ticker'].values)
                pm.remove_position("NVDA")
                df_after = pm.get_portfolio()
                self.assertNotIn("NVDA", df_after['Ticker'].values)
        finally:
            os.remove(test_db)

    def test_get_portfolio_uses_alpaca_as_source_of_truth(self):
        """get_portfolio must return ALPACA'S qty/avg/MV/PnL when reachable,
        joined to local SL/TP/AI/entry_date metadata by ticker. Even if the
        local DB has a stale snapshot, the live read returns Alpaca's truth.

        Regression for the 2026-06-07 NVDA/SAN drift bug: the DB row no longer
        controls what users see in Portfolio — that's Alpaca's job — so the
        same drift can't surface as "out of sync" anymore.
        """
        import tempfile, dashboard
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            test_db = tmp.name
        try:
            pm = PortfolioManager(db_path=test_db)
            # Seed a STALE local row (qty/avg deliberately wrong) plus the
            # metadata Alpaca doesn't know about.
            pm.add_position("NVDA", 60, 215.33, sl=200, tp=250, score=68, rating="BUY")

            # Alpaca's truth: 4.75 @ 210.74, MV 1000, PnL -10%.
            live = [{
                "symbol": "NVDA", "qty": 4.75, "avg_entry_price": 210.74,
                "current_price": 189.67, "market_value": 1000.0,
                "unrealized_pl": -100.0, "unrealized_plpc": -0.10,
            }]
            with patch.object(dashboard.alpaca_api, "list_positions", return_value=live):
                df = pm.get_portfolio()

            row = df[df['Ticker'] == 'NVDA'].iloc[0]
            # Alpaca wins on these (the drifted local 60/215.33 must NOT leak):
            self.assertAlmostEqual(float(row['Shares']),    4.75,   places=4)
            self.assertAlmostEqual(float(row['Avg Price']), 210.74, places=4)
            self.assertAlmostEqual(float(row['Current Price']), 189.67, places=4)
            self.assertAlmostEqual(float(row['Total Value']),  1000.0, places=2)
            self.assertAlmostEqual(float(row['PnL %']),       -10.0,  places=2)
            # Local metadata is joined in:
            self.assertAlmostEqual(float(row['Stop Loss']),   200.0)
            self.assertAlmostEqual(float(row['Take Profit']), 250.0)
            self.assertEqual(row['AI Rating'], 'BUY')
            self.assertAlmostEqual(float(row['AI Score']), 68.0)
        finally:
            os.remove(test_db)

    def test_reconcile_syncs_qty_and_avg_drift(self):
        """If Alpaca shows different qty/avg for a position the local DB also holds,
        _reconcile_with_alpaca must update the local row to match Alpaca's truth.

        Regression for the 2026-06-07 NVDA/SAN bug: the prior reconcile only did
        set-membership (add/remove tickers) and ignored qty/avg drift, so partial
        fills, manual sells, and bracket triggers silently broke the local cache.
        """
        import tempfile, sqlite3 as sql
        import dashboard
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            test_db = tmp.name
        try:
            pm = PortfolioManager(db_path=test_db)
            # Local thinks NVDA = 60 @ 215.33 (the stale state we hit in prod).
            pm.add_position("NVDA", 60, 215.33, sl=200, tp=250, score=68, rating="BUY")
            # SAN drift in the other direction (1814 → 2814).
            pm.add_position("SAN", 1814, 12.48, sl=11.92, tp=13.6, score=68, rating="BUY")
            # An untouched position (qty/avg match) to prove we don't churn.
            pm.add_position("AAPL", 17, 302.33, sl=290, tp=325, score=70, rating="BUY")

            # Alpaca's "truth": NVDA shrunk, SAN grew, AAPL unchanged.
            live = [
                {"symbol": "NVDA", "qty": 4.745113851, "avg_entry_price": 210.743099},
                {"symbol": "SAN",  "qty": 2814.0,      "avg_entry_price": 12.3572},
                {"symbol": "AAPL", "qty": 17.0,        "avg_entry_price": 302.33},
            ]
            with patch.object(dashboard.alpaca_api, "list_positions", return_value=live), \
                 patch.object(dashboard.alpaca_api, "get_bracket_levels", return_value={}):
                pm._reconcile_with_alpaca()

            with sql.connect(test_db) as conn:
                rows = {r[0]: (r[1], r[2]) for r in conn.execute(
                    "SELECT ticker, shares, avg_price FROM portfolio").fetchall()}

            self.assertAlmostEqual(rows["NVDA"][0], 4.745113851, places=6)
            self.assertAlmostEqual(rows["NVDA"][1], 210.743099, places=4)
            self.assertAlmostEqual(rows["SAN"][0],  2814.0,     places=4)
            self.assertAlmostEqual(rows["SAN"][1],  12.3572,    places=4)
            # AAPL row was already in sync — must remain untouched (and identical).
            self.assertAlmostEqual(rows["AAPL"][0], 17.0,       places=4)
            self.assertAlmostEqual(rows["AAPL"][1], 302.33,     places=4)
        finally:
            os.remove(test_db)

class TestAgentConfigAndRisk(unittest.TestCase):
    """Tests for agent_config (SQLite-backed knobs) + risk_manager (gatekeeper).

    Goal: protect the safety guards from accidental regressions. The risk
    manager is the single chokepoint for every AUTO entry — if it lets a
    bad entry through, real money is at stake later. These tests force
    each branch (each early-return reason).
    """

    def setUp(self):
        import tempfile, os, importlib
        # Each test gets a fresh DB so config seeds/halts don't leak.
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self._db = tmp.name
        os.environ["NUROQ_DB_PATH"] = self._db
        import agent_config, risk_manager
        importlib.reload(agent_config)
        importlib.reload(risk_manager)
        self.agent_config = agent_config
        self.risk_manager = risk_manager

    def tearDown(self):
        import os
        try: os.remove(self._db)
        except Exception: pass

    def _enable_and_open_market(self):
        """Default config + enable AUTO + monkey-patch market open + entry window."""
        self.agent_config.update(auto_trade_enabled=True)
        # Patch the time-of-day checks so they pass regardless of when tests run.
        self.risk_manager._market_open = lambda now: True
        self.risk_manager._in_entry_window = lambda now, cfg: True

    def test_seed_on_first_read(self):
        cfg = self.agent_config.get()
        self.assertEqual(cfg["budget"], 10000)
        self.assertEqual(cfg["max_concurrent"], 5)
        self.assertEqual(cfg["risk_per_trade_pct"], 1.0)
        self.assertEqual(cfg["daily_loss_limit_pct"], 2.0)
        self.assertFalse(cfg["auto_trade_enabled"])
        self.assertFalse(cfg["margin_allowed"])
        self.assertIsNone(cfg["halted_at"])

    def test_update_whitelist_rejects_unknown_keys(self):
        cfg = self.agent_config.update(budget=20000, evil_key="oops")
        self.assertEqual(cfg["budget"], 20000)
        self.assertNotIn("evil_key", cfg)

    def test_halt_persists_and_blocks_auto(self):
        self.agent_config.halt("test reason")
        cfg = self.agent_config.get()
        self.assertIsNotNone(cfg["halted_at"])
        self.assertEqual(cfg["halt_reason"], "test reason")
        # Halt MUST also disable auto so a stale enable+halt combo doesn't
        # surprise-start when the halt clears.
        self.assertFalse(cfg["auto_trade_enabled"])

    def test_decision_blocked_when_auto_disabled(self):
        d = self.risk_manager.can_enter_trade(
            "NVDA", 100, 2.0, open_positions=0, cash=5000,
            todays_pl=0, equity=10000, on_margin=False,
        )
        self.assertFalse(d.ok)
        self.assertIn("auto_trade disabled", d.reason)

    def test_decision_blocked_when_halted(self):
        self._enable_and_open_market()
        self.agent_config.halt("circuit tripped")
        d = self.risk_manager.can_enter_trade(
            "NVDA", 100, 2.0, open_positions=0, cash=5000,
            todays_pl=0, equity=10000, on_margin=False,
        )
        self.assertFalse(d.ok)
        self.assertIn("halted", d.reason)

    def test_decision_blocked_by_concurrency_cap(self):
        self._enable_and_open_market()
        self.agent_config.update(max_concurrent=3)
        d = self.risk_manager.can_enter_trade(
            "NVDA", 100, 2.0, open_positions=3, cash=5000,
            todays_pl=0, equity=10000, on_margin=False,
        )
        self.assertFalse(d.ok)
        self.assertIn("concurrency cap", d.reason)

    def test_decision_blocked_when_margin_disallowed_and_on_margin(self):
        self._enable_and_open_market()
        d = self.risk_manager.can_enter_trade(
            "NVDA", 100, 2.0, open_positions=0,
            cash=-1000,            # already on margin
            todays_pl=0, equity=10000, on_margin=True,
        )
        self.assertFalse(d.ok)
        self.assertIn("margin disabled", d.reason)

    def test_decision_trips_circuit_on_daily_loss(self):
        self._enable_and_open_market()  # daily limit default = 2%
        d = self.risk_manager.can_enter_trade(
            "NVDA", 100, 2.0, open_positions=0, cash=5000,
            todays_pl=-300,        # -3% on $10k equity → past 2% limit
            equity=10000, on_margin=False,
        )
        self.assertFalse(d.ok)
        # Loss check tripping should ALSO halt the agent (persistent).
        self.assertTrue(self.agent_config.get()["halted_at"])

    def test_decision_sizes_by_risk_budget(self):
        self._enable_and_open_market()
        # entry $100, ATR $2 → SL $96 (2·ATR), per-share risk $4
        # risk budget = 1% of $10k = $100 → shares_by_risk = 25
        # per-position cap = $10k/5 = $2k → shares_by_size = 20
        # cash $5000 → shares_by_cash = 50
        # Final shares = min(25, 20, 50) = 20
        d = self.risk_manager.can_enter_trade(
            "NVDA", 100, 2.0, open_positions=0, cash=5000,
            todays_pl=0, equity=10000, on_margin=False,
        )
        self.assertTrue(d.ok, msg=d.reason)
        self.assertEqual(d.sizing.shares, 20)
        self.assertAlmostEqual(d.sizing.sl, 96.0, places=2)
        self.assertAlmostEqual(d.sizing.tp, 108.0, places=2)   # entry + 4·ATR

    def test_decision_zero_shares_returns_failure(self):
        self._enable_and_open_market()
        # $1000 budget + 1% risk = $10 risk; entry $500, ATR $1 → per-share risk
        # $2 → shares_by_risk = 5; per-position cap = $1000/5 = $200 → 0 shares.
        self.agent_config.update(budget=1000)
        d = self.risk_manager.can_enter_trade(
            "NVDA", 500, 1.0, open_positions=0, cash=2000,
            todays_pl=0, equity=10000, on_margin=False,
        )
        self.assertFalse(d.ok)
        self.assertIn("sized to 0", d.reason)


class TestFlattenAllSideDetection(unittest.TestCase):
    """REGRESSION: flatten_all_positions must submit SELLs for long positions,
    not BUYs. Earlier code did `str(p.side).lower() == "long"` which is False
    when the SDK returns the `PositionSide.LONG` enum (str → "PositionSide.LONG"),
    so every close ended up as a BUY — doubling positions instead of closing them.

    The fix uses `getattr(side, "name", ...)` + substring "LONG" match; this
    test forces the enum shape Alpaca's SDK actually returns so the bug stays
    caught.
    """
    @patch('alpaca_executor.TradingClient')
    def test_long_position_submits_sell_market_order(self, mock_client_class):
        from unittest.mock import MagicMock
        # Mock the Alpaca client and inject a "long" enum-shaped side.
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.get_account().status.value = "ACTIVE"
        mock_client.get_clock().is_open = False   # force the off-hours path
        # Force the close_position() preferred path to fail so we exercise
        # the MarketOrderRequest fallback (where the side-detection bug lived).
        mock_client.close_position.side_effect = Exception("simulated paper off-hours fail")
        # Simulate Alpaca's PositionSide enum: has a .name = "LONG", and
        # str(enum) returns "PositionSide.LONG" (the original buggy case).
        class _FakeSide:
            name = "LONG"
            def __str__(self): return "PositionSide.LONG"
        pos = MagicMock()
        # Defensive: held_for_orders must be 0 so the "all_held" off-hours
        # short-circuit doesn't fire before we hit the close path.
        pos.symbol = "NVDA"; pos.qty = "10"; pos.side = _FakeSide()
        pos.held_for_orders = 0
        mock_client.get_all_positions.return_value = [pos]

        from alpaca_executor import LiveAlpacaExecutor
        ae = LiveAlpacaExecutor()
        result = ae.flatten_all_positions()

        # The fallback path must have submitted a SELL order on NVDA.
        self.assertEqual(result["queued_for_open"], 1, msg=str(result))
        submit_calls = [c for c in mock_client.submit_order.call_args_list
                        if c.kwargs.get("order_data") is not None]
        self.assertTrue(submit_calls, "submit_order was never called")
        order_data = submit_calls[-1].kwargs["order_data"]
        self.assertEqual(getattr(order_data.side, "name", str(order_data.side)),
                         "SELL", msg=f"expected SELL, got {order_data.side!r}")
        self.assertEqual(order_data.symbol, "NVDA")


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


class TestSection475(unittest.TestCase):
    """§475(f) mark-to-market mode neutralizes the wash-sale guard (default OFF).

    Added 2026-06-03: a valid §475 election makes the wash-sale rule (§1091)
    inapplicable. NUROQ_SECTION_475=1 short-circuits wash_sale_check so every BUY
    gate passes, and satisfies the live-trading safety belt. The flag must NEVER
    default on — the software cannot assume the user filed the election.
    """

    def test_helper_defaults_off(self):
        from dashboard import section_475_active
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NUROQ_SECTION_475", None)
            self.assertFalse(section_475_active())
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "1"}):
            self.assertTrue(section_475_active())
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "0"}):
            self.assertFalse(section_475_active())

    def test_wash_sale_check_short_circuits_when_elected(self):
        """With §475 on, wash_sale_check returns risk=False BEFORE any Alpaca
        call — so even a would-be loss re-entry is unflagged."""
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "1"}):
            # Patch get_recent_fills to blow up: if it's called, the short-circuit
            # failed to return early. It must NOT be reached.
            with patch.object(dashboard.alpaca_api, "get_recent_fills",
                              side_effect=AssertionError("Alpaca reached despite §475")):
                res = dashboard.wash_sale_check("SAN", force_refresh=True)
        self.assertFalse(res["risk"])
        self.assertTrue(res.get("section_475"))
        self.assertIn("475", res["hint"])

    def test_wash_sale_guard_still_active_when_off(self):
        """With §475 off, a loss re-entry is still flagged risk=True (regression
        guard — the neutralizer must not leak into the default path)."""
        now = time.time()
        fake_fills = [
            {"side": "BUY",  "qty": 100, "fill_price": 13.00, "filled_at_ts": now - 5 * 86400},
            {"side": "SELL", "qty": 100, "fill_price": 12.50, "filled_at_ts": now - 2 * 86400},
        ]
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "0"}):
            with patch.object(dashboard.alpaca_api, "get_recent_fills", return_value=fake_fills):
                res = dashboard.wash_sale_check("SAN", force_refresh=True)
        self.assertTrue(res["risk"])
        self.assertTrue(res["likely_loss_sells"])

    @patch('alpaca_executor.TradingClient')
    def test_section_475_satisfies_live_trading_safety_belt(self, mock_client_class):
        """NUROQ_SECTION_475=1 alone (no NUROQ_WASH_SALE_AWARE) must allow live
        trading to connect, instead of raising the wash-sale-aware RuntimeError."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test",
            "NUROQ_LIVE_TRADING": "1", "NUROQ_SECTION_475": "1",
            "NUROQ_WASH_SALE_AWARE": "0",
        }):
            from alpaca_executor import LiveAlpacaExecutor
            try:
                LiveAlpacaExecutor()  # must not raise the safety-belt RuntimeError
            except RuntimeError as e:
                self.fail(f"§475 should satisfy the safety belt, but it raised: {e}")
        # Live client constructed with paper=False
        _, kwargs = mock_client_class.call_args
        self.assertEqual(kwargs.get("paper"), False)

    def test_live_trading_belt_still_blocks_without_ack(self):
        """Neither ack set → live trading must still hard-fail (belt intact)."""
        with patch.dict(os.environ, {
            "ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test",
            "NUROQ_LIVE_TRADING": "1", "NUROQ_SECTION_475": "0",
            "NUROQ_WASH_SALE_AWARE": "0",
        }):
            from alpaca_executor import LiveAlpacaExecutor
            with self.assertRaises(RuntimeError):
                LiveAlpacaExecutor()


class TestSellProposals(unittest.TestCase):
    """Option B: the core quant layer proactively PROPOSES sells on held
    positions — tax-loss harvest (§475-gated), rotate, or exit-weak.
    Added 2026-06-03. propose_sells() is pure; we mock Alpaca + watchlist.
    """

    def _pos(self, symbol, qty, avg, cur):
        pl = (cur - avg) * qty
        plpc = (cur - avg) / avg if avg else 0.0
        return {"symbol": symbol, "qty": float(qty), "avg_entry_price": float(avg),
                "current_price": float(cur), "market_value": cur * qty,
                "cost_basis": avg * qty, "unrealized_pl": pl, "unrealized_plpc": plpc}

    def _wl(self, rows):
        # rows: list of (ticker, quant_score, recommendation)
        return [{"ticker": t, "quant_score": s, "recommendation": rec,
                 "ai_score": s, "price": 100.0, "rank": i}
                for i, (t, s, rec) in enumerate(rows)]

    def test_harvest_suppressed_without_475(self):
        """A loser must NOT generate a harvest proposal when §475 is OFF."""
        positions = [self._pos("SAN", 100, 13.0, 12.0)]  # -7.7%
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "0"}), \
             patch.object(dashboard.alpaca_api, "list_positions", return_value=positions), \
             patch.object(dashboard.watchlist_today, "get_all",
                          return_value=self._wl([("SAN", 70, "HOLD")])):
            props = dashboard.propose_sells()
        self.assertFalse(any(p["kind"] == "TAX_LOSS_HARVEST" for p in props))

    def test_harvest_proposed_under_475(self):
        """Same loser DOES generate a harvest proposal when §475 is ON."""
        positions = [self._pos("SAN", 100, 13.0, 12.0)]
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "1"}), \
             patch.object(dashboard.alpaca_api, "list_positions", return_value=positions), \
             patch.object(dashboard.watchlist_today, "get_all",
                          return_value=self._wl([("SAN", 50, "HOLD")])):
            props = dashboard.propose_sells()
        harvest = [p for p in props if p["kind"] == "TAX_LOSS_HARVEST"]
        self.assertEqual(len(harvest), 1)
        self.assertEqual(harvest[0]["ticker"], "SAN")
        self.assertTrue(harvest[0]["section_475"])
        self.assertIn("475", harvest[0]["reason"])

    def test_strong_loser_not_harvested(self):
        """A red position that still scores strongly (≥ ceiling) is NOT harvested
        even under §475 — don't dump conviction for a tax nicety."""
        positions = [self._pos("NVDA", 10, 130.0, 125.0)]  # red but strong
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "1"}), \
             patch.object(dashboard.alpaca_api, "list_positions", return_value=positions), \
             patch.object(dashboard.watchlist_today, "get_all",
                          return_value=self._wl([("NVDA", 80, "BUY")])):
            props = dashboard.propose_sells()
        self.assertEqual(props, [])

    def test_rotate_when_stronger_candidate_exists(self):
        """Weak holding + a much higher-scoring non-held BUY → ROTATE."""
        positions = [self._pos("MU", 50, 90.0, 92.0)]  # small gain, weak score
        wl = self._wl([("MU", 40, "HOLD"), ("AVGO", 78, "BUY")])
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "0"}), \
             patch.object(dashboard.alpaca_api, "list_positions", return_value=positions), \
             patch.object(dashboard.watchlist_today, "get_all", return_value=wl):
            props = dashboard.propose_sells()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["kind"], "ROTATE")
        self.assertEqual(props[0]["rotate_into"], "AVGO")

    def test_exit_weak_when_no_candidate(self):
        """Weak holding, no stronger candidate → EXIT_WEAK (not rotate)."""
        positions = [self._pos("MU", 50, 90.0, 92.0)]
        wl = self._wl([("MU", 40, "HOLD")])
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "0"}), \
             patch.object(dashboard.alpaca_api, "list_positions", return_value=positions), \
             patch.object(dashboard.watchlist_today, "get_all", return_value=wl):
            props = dashboard.propose_sells()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["kind"], "EXIT_WEAK")
        self.assertIsNone(props[0]["rotate_into"])

    def test_healthy_position_no_proposal(self):
        """A strong, green holding generates no proposal."""
        positions = [self._pos("AAPL", 20, 150.0, 165.0)]
        with patch.dict(os.environ, {"NUROQ_SECTION_475": "1"}), \
             patch.object(dashboard.alpaca_api, "list_positions", return_value=positions), \
             patch.object(dashboard.watchlist_today, "get_all",
                          return_value=self._wl([("AAPL", 82, "BUY")])):
            props = dashboard.propose_sells()
        self.assertEqual(props, [])

    def test_propose_sells_fails_closed(self):
        """If Alpaca lookup throws, propose nothing (never raise)."""
        with patch.object(dashboard.alpaca_api, "list_positions",
                          side_effect=RuntimeError("alpaca down")):
            self.assertEqual(dashboard.propose_sells(), [])

    def test_log_sell_proposals_dedups(self):
        """Second log of the same (date,ticker,kind) writes 0 new feed rows."""
        dashboard._proposed_sell_keys.clear()
        proposals = [{"ticker": "ZZZ", "kind": "EXIT_WEAK", "score": 40,
                      "current_price": 10.0, "reason": "test"}]
        with patch.object(dashboard.live_triggers, "log") as mock_log:
            first = dashboard.log_sell_proposals(list(proposals))
            second = dashboard.log_sell_proposals(list(proposals))
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(mock_log.call_count, 1)
        dashboard._proposed_sell_keys.clear()


class TestScheduler(unittest.TestCase):
    """In-process cloud scheduler date math (scheduler._next_fire). Added 2026-06-04."""

    def _et(self, y, m, d, hh, mm):
        from scheduler import ET
        from datetime import datetime as dt
        return dt(y, m, d, hh, mm, tzinfo=ET)

    def test_fires_later_today_when_time_not_passed(self):
        from scheduler import _next_fire
        now = self._et(2026, 6, 4, 1, 0)   # Thursday 01:00 ET, before 03:30
        nxt = _next_fire(3, 30, now=now)
        self.assertEqual((nxt.month, nxt.day, nxt.hour, nxt.minute), (6, 4, 3, 30))

    def test_rolls_to_next_day_when_time_passed(self):
        from scheduler import _next_fire
        now = self._et(2026, 6, 4, 9, 0)   # Thursday 09:00, after 03:30
        nxt = _next_fire(3, 30, now=now)
        self.assertEqual((nxt.month, nxt.day), (6, 5))  # Friday

    def test_skips_weekend_to_monday(self):
        from scheduler import _next_fire
        now = self._et(2026, 6, 5, 9, 0)   # Friday 09:00 → next is Mon Jun 8
        nxt = _next_fire(3, 30, now=now)
        self.assertEqual(nxt.weekday(), 0)               # Monday
        self.assertEqual((nxt.month, nxt.day), (6, 8))

    def test_saturday_rolls_to_monday(self):
        from scheduler import _next_fire
        now = self._et(2026, 6, 6, 2, 0)   # Saturday → Monday Jun 8
        nxt = _next_fire(3, 30, now=now)
        self.assertEqual((nxt.weekday(), nxt.day), (0, 8))


class TestGeminiStructuredOutput(unittest.TestCase):
    """GeminiBackend uses JSON-schema structured output on the scoring path (so the
    score always parses) but free-form text for Ask-AI. Added 2026-06-05."""

    def _stub_genai(self):
        """Returns (genai_module, captured_calls). GenerateContentConfig is stubbed
        to return its kwargs dict so we can inspect what generate() requested."""
        import types as _t
        captured = {}
        gen = _t.ModuleType("google.genai")
        gt = _t.ModuleType("google.genai.types")

        class _Resp:
            text = '{"score":73,"rating":"BUY","reasoning":"NVDA strong"}'

        class _Models:
            def generate_content(self, **kw):
                captured.update(kw)
                return _Resp()

        class _Client:
            def __init__(self, **kw):
                self.models = _Models()

        gen.Client = _Client
        gt.GenerateContentConfig = lambda **kw: kw   # config -> plain dict
        gen.types = gt
        return gen, gt, captured

    def _make_backend(self, gen, gt):
        import sys, google
        with patch.dict(sys.modules, {"google.genai": gen, "google.genai.types": gt}):
            google.genai = gen
            with patch.dict(os.environ, {"GEMINI_API_KEY": "x", "NUROQ_GEMINI_VERTEX": "0"}):
                from analyst_backends import GeminiBackend
                return GeminiBackend()

    def test_scoring_path_sends_json_schema(self):
        gen, gt, captured = self._stub_genai()
        import sys, google
        b = self._make_backend(gen, gt)
        with patch.dict(sys.modules, {"google.genai": gen, "google.genai.types": gt}):
            google.genai = gen
            out = b.generate("score NVDA", structured=True)
        cfg = captured.get("config", {})
        self.assertEqual(out, '{"score":73,"rating":"BUY","reasoning":"NVDA strong"}')
        self.assertEqual(cfg.get("response_mime_type"), "application/json")
        self.assertIn("response_schema", cfg)
        self.assertIn("score", cfg["response_schema"]["properties"])

    def test_ask_path_is_free_text(self):
        gen, gt, captured = self._stub_genai()
        import sys, google
        b = self._make_backend(gen, gt)
        with patch.dict(sys.modules, {"google.genai": gen, "google.genai.types": gt}):
            google.genai = gen
            b.generate("why did NVDA move in May?", structured=False)
        cfg = captured.get("config", {})
        self.assertNotIn("response_schema", cfg)
        self.assertNotIn("response_mime_type", cfg)


# ===========================================================================
# Day-trader: minute bars, intraday indicators, ORB-5 strategy, simulator.
# ===========================================================================

def _et_ts(yyyymmdd: str, hh: int, mm: int) -> int:
    """Build a unix-seconds ts at the given HH:MM ET on the given date.
    Used in synthetic-bar fixtures so Bar.minute_of_day resolves correctly."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = None
    y, m, d = (int(x) for x in yyyymmdd.split("-"))
    dt = datetime(y, m, d, hh, mm, tzinfo=tz) if tz else datetime(y, m, d, hh, mm)
    return int(dt.timestamp())


def _bar(ticker: str, yyyymmdd: str, hh: int, mm: int,
         o: float, h: float, l: float, c: float, v: float,
         vw: float | None = None):
    """Convenience constructor for a synthetic Bar at an exact ET wall-time."""
    from minute_bars import Bar
    return Bar(
        ticker=ticker,
        ts=_et_ts(yyyymmdd, hh, mm),
        open=o, high=h, low=l, close=c, volume=v,
        vwap=vw if vw is not None else (h + l + c) / 3.0,
    )


def _synthetic_session(ticker: str, yyyymmdd: str,
                       open_price: float = 100.0,
                       orb_height: float = 1.0,
                       breakout_at_minute: int | None = 35,
                       breakout_volume_mult: float = 3.0,
                       drift: float = 0.10) -> list:
    """Build a full regular-session day of 1-min bars (09:30-16:00 ET).

    Bars 0-4 (09:30-09:34) form the Opening Range with `orb_height` span.
    Optional breakout at `breakout_at_minute` (default 35 = 09:35 ET 5th
    bar after the OR window — index 5) with elevated volume.
    Steady upward drift after that so a follow-through TP can fire."""
    bars = []
    base_vol = 1000.0
    # ORB window (0..4) — tight range around open_price
    or_low = open_price - orb_height / 2.0
    or_high = open_price + orb_height / 2.0
    for i in range(5):
        m = 30 + i
        # alternate hi/lo touches but stay inside range
        h = or_high if i % 2 == 0 else open_price + orb_height * 0.3
        l = or_low if i % 2 == 1 else open_price - orb_height * 0.3
        c = (h + l) / 2.0
        o = (h + l) / 2.0
        bars.append(_bar(ticker, yyyymmdd, 9, m, o, h, l, c, base_vol))
    # Regular session continues 09:35 → 16:00 (385 more bars)
    cur_price = open_price + orb_height * 0.4   # near upper portion of OR
    minutes_after = 0
    for hh in range(9, 16):
        for mm_start in (0, 30) if hh in (9, 15) else (0,):
            # we just iterate per-minute below; this is a marker
            pass
    # Simpler: iterate minute by minute from 09:35..15:59
    cur_dt_min = 35    # of the 9:XX hour
    cur_hh = 9
    while not (cur_hh == 16 and cur_dt_min == 0):
        if cur_dt_min == 60:
            cur_dt_min = 0
            cur_hh += 1
            if cur_hh == 16:
                break
        minutes_after += 1
        # Apply breakout if we're at the target minute
        is_breakout = (breakout_at_minute is not None
                       and cur_hh == 9 and cur_dt_min == breakout_at_minute)
        if is_breakout:
            o = cur_price
            c = or_high + orb_height * 0.5     # clear breakout
            h = c + 0.05
            l = o - 0.02
            v = base_vol * breakout_volume_mult
        else:
            o = cur_price
            c = cur_price + drift if minutes_after > 10 else cur_price + drift * 0.1
            h = max(o, c) + 0.05
            l = min(o, c) - 0.05
            v = base_vol * (1.0 + (0.2 if minutes_after % 5 == 0 else 0))
        bars.append(_bar(ticker, yyyymmdd, cur_hh, cur_dt_min, o, h, l, c, v))
        cur_price = c
        cur_dt_min += 1
    return bars


class TestMinuteBars(unittest.TestCase):
    """MinuteBarCache store/get + Bar.minute_of_day round-trip."""

    def setUp(self):
        # Redirect to tempfile so we don't touch real nuroq.db
        import tempfile
        import os as _os
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._prev_db = _os.environ.get("NUROQ_DB_PATH")
        _os.environ["NUROQ_DB_PATH"] = self.tmp.name
        # Force re-import so the cache picks up the new path
        import importlib
        import minute_bars
        importlib.reload(minute_bars)
        self.minute_bars = minute_bars

    def tearDown(self):
        import os as _os
        try:
            _os.unlink(self.tmp.name)
        except Exception:
            pass
        if self._prev_db is None:
            _os.environ.pop("NUROQ_DB_PATH", None)
        else:
            _os.environ["NUROQ_DB_PATH"] = self._prev_db

    def test_bar_minute_of_day(self):
        b = _bar("X", "2026-06-10", 9, 30, 100.0, 100.5, 99.5, 100.2, 5000.0)
        self.assertEqual(b.minute_of_day, 9 * 60 + 30)

    def test_store_and_retrieve(self):
        cache = self.minute_bars.MinuteBarCache(db_path=self.tmp.name)
        raw = [
            {"t": _et_ts("2026-06-10", 9, 30) * 1000, "o": 100, "h": 101,
             "l": 99.5, "c": 100.7, "v": 5000, "vw": 100.4},
            {"t": _et_ts("2026-06-10", 9, 31) * 1000, "o": 100.7, "h": 101.5,
             "l": 100.3, "c": 101.2, "v": 6000, "vw": 100.9},
        ]
        n = cache.store("AAPL", raw)
        self.assertEqual(n, 2)
        out = cache.get_session("AAPL", "2026-06-10")
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0].close, 100.7)
        self.assertAlmostEqual(out[1].vwap, 100.9)

    def test_has_session(self):
        cache = self.minute_bars.MinuteBarCache(db_path=self.tmp.name)
        self.assertFalse(cache.has_session("AAPL", "2026-06-10"))
        cache.store("AAPL", [
            {"t": _et_ts("2026-06-10", 10, 0) * 1000, "o": 100, "h": 101,
             "l": 99, "c": 100.5, "v": 1000, "vw": 100.2},
        ])
        self.assertTrue(cache.has_session("AAPL", "2026-06-10"))


class TestIntradayIndicators(unittest.TestCase):
    """VWAP, ORB, ATR, reversal-bar, bull-flag pure-function checks."""

    def test_session_vwap_basic(self):
        from intraday_indicators import session_vwap
        bars = [
            _bar("X", "2026-06-10", 9, 30, 100, 100, 100, 100, 1000, vw=100),
            _bar("X", "2026-06-10", 9, 31, 100, 102, 100, 102, 2000, vw=101),
        ]
        # Weighted avg: (100*1000 + 101*2000) / 3000 = 100.6667
        v = session_vwap(bars)
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v, (100 * 1000 + 101 * 2000) / 3000, places=4)

    def test_session_vwap_empty(self):
        from intraday_indicators import session_vwap
        self.assertIsNone(session_vwap([]))

    def test_opening_range_window(self):
        from intraday_indicators import opening_range
        bars = _synthetic_session("X", "2026-06-10",
                                  open_price=50.0, orb_height=0.5)
        orb = opening_range(bars, window_minutes=5)
        self.assertIsNotNone(orb)
        # Mid should be ~50 (we centered the OR there)
        self.assertAlmostEqual(orb.mid, 50.0, places=1)
        self.assertEqual(orb.bar_count, 5)
        self.assertGreater(orb.avg_volume, 0)
        # Range pct ~ 1% on a $50 stock with $0.5 height
        self.assertAlmostEqual(orb.range_pct, 1.0, places=1)

    def test_opening_range_insufficient_bars(self):
        from intraday_indicators import opening_range
        bars = [_bar("X", "2026-06-10", 9, 30, 100, 101, 99, 100, 1000),
                _bar("X", "2026-06-10", 9, 31, 100, 101, 99, 100, 1000)]
        self.assertIsNone(opening_range(bars, window_minutes=5))

    def test_intraday_atr(self):
        from intraday_indicators import intraday_atr
        bars = []
        for i in range(20):
            bars.append(_bar("X", "2026-06-10", 10, i, 100, 100.5, 99.5, 100, 1000))
        atr = intraday_atr(bars, lookback=14)
        self.assertIsNotNone(atr)
        self.assertAlmostEqual(atr, 1.0, places=1)   # TR ≈ high-low = 1

    def test_intraday_atr_too_few_bars(self):
        from intraday_indicators import intraday_atr
        bars = [_bar("X", "2026-06-10", 10, 0, 100, 101, 99, 100, 1000)]
        self.assertIsNone(intraday_atr(bars, lookback=14))

    def test_is_bullish_reversal_bar_true(self):
        from intraday_indicators import is_bullish_reversal_bar
        prev = _bar("X", "2026-06-10", 10, 0, 100, 100.5, 99.0, 99.2, 1000)
        cur = _bar("X", "2026-06-10", 10, 1, 99.2, 101, 99.0, 100.8, 2000)
        self.assertTrue(is_bullish_reversal_bar(cur, prev))

    def test_is_bullish_reversal_bar_false_red(self):
        from intraday_indicators import is_bullish_reversal_bar
        cur = _bar("X", "2026-06-10", 10, 1, 101, 101.2, 99.5, 100.0, 2000)
        self.assertFalse(is_bullish_reversal_bar(cur, None))

    def test_detect_bull_flag(self):
        from intraday_indicators import detect_bull_flag
        # Build: 10 bars rising leg (100 → 105 = 5%), then 6 flat bars (104.5–105.0)
        bars = []
        for i in range(10):
            p = 100 + i * 0.5
            bars.append(_bar("X", "2026-06-10", 10, i,
                             p - 0.05, p + 0.05, p - 0.1, p, 1000))
        # Flag: tight range 104.5–105 for 6 bars
        for i in range(6):
            p = 104.7 + (0.1 if i % 2 else 0)
            bars.append(_bar("X", "2026-06-10", 10, 10 + i,
                             p, p + 0.05, p - 0.05, p, 800))
        flag = detect_bull_flag(bars, min_leg_pct=3.0)
        self.assertIsNotNone(flag)
        self.assertGreater(flag.leg_pct, 3.0)


class TestORB5Strategy(unittest.TestCase):
    """ORB-5 fire / no-fire conditions and stop/target placement."""

    def test_fires_on_breakout_with_volume_and_vwap(self):
        from day_trader import ORB5Strategy, ORB5Params
        from backtest.fill_model import OrderSide
        params = ORB5Params(volume_multiplier=2.0, require_vwap_align=False,
                            min_orb_range_pct=0.1)
        s = ORB5Strategy(params)
        bars = _synthetic_session("X", "2026-06-10", open_price=100.0,
                                  orb_height=1.0, breakout_at_minute=35,
                                  breakout_volume_mult=3.0)
        s.reset_for_session("X", "2026-06-10")
        triggered = None
        for i, b in enumerate(bars):
            intents = s.on_bar("X", b, bars[:i+1], in_position=False)
            if intents:
                triggered = (i, intents[0])
                break
        self.assertIsNotNone(triggered, "ORB5 did not fire on breakout bar")
        idx, intent = triggered
        self.assertEqual(intent.side, OrderSide.BUY)
        self.assertEqual(intent.setup_id, "ORB5")

    def test_no_fire_when_volume_too_low(self):
        from day_trader import ORB5Strategy, ORB5Params
        params = ORB5Params(volume_multiplier=10.0, require_vwap_align=False,
                            min_orb_range_pct=0.1)
        s = ORB5Strategy(params)
        bars = _synthetic_session("X", "2026-06-10", breakout_volume_mult=1.5)
        s.reset_for_session("X", "2026-06-10")
        for i, b in enumerate(bars):
            self.assertEqual(s.on_bar("X", b, bars[:i+1], in_position=False), [])

    def test_no_fire_inside_orb_window(self):
        from day_trader import ORB5Strategy
        s = ORB5Strategy()
        bars = _synthetic_session("X", "2026-06-10")
        s.reset_for_session("X", "2026-06-10")
        # First 5 bars are the OR window — strategy must not fire
        for i, b in enumerate(bars[:5]):
            self.assertEqual(s.on_bar("X", b, bars[:i+1], in_position=False), [])

    def test_stop_at_orb_low(self):
        from day_trader import ORB5Strategy, ORB5Params
        from backtest.fill_model import OrderIntent, OrderKind, OrderSide
        params = ORB5Params(min_orb_range_pct=0.1, require_vwap_align=False)
        s = ORB5Strategy(params)
        bars = _synthetic_session("X", "2026-06-10",
                                  open_price=100.0, orb_height=1.0,
                                  breakout_at_minute=35,
                                  breakout_volume_mult=3.0)
        s.reset_for_session("X", "2026-06-10")
        # Drive bars until fire to populate state.orb
        for i, b in enumerate(bars):
            intents = s.on_bar("X", b, bars[:i+1], in_position=False)
            if intents:
                intent = intents[0]
                stop, target, time_stop = s.initial_stop_and_target(
                    intent.trigger_price, intent, bars[:i+1],
                )
                # Stop should be at OR low (~99.5)
                self.assertAlmostEqual(stop, 99.5, places=1)
                # Target = entry + 2R
                R = intent.trigger_price - stop
                self.assertAlmostEqual(target, intent.trigger_price + 2 * R, places=2)
                self.assertEqual(time_stop, params.time_stop_bars)
                break
        else:
            self.fail("ORB5 should have fired on the synthetic breakout")

    def test_one_fire_per_session(self):
        """Strategy must NOT fire a second time after firing once."""
        from day_trader import ORB5Strategy, ORB5Params
        params = ORB5Params(volume_multiplier=2.0, require_vwap_align=False,
                            min_orb_range_pct=0.1)
        s = ORB5Strategy(params)
        bars = _synthetic_session("X", "2026-06-10", breakout_volume_mult=3.0)
        s.reset_for_session("X", "2026-06-10")
        fired_count = 0
        for i, b in enumerate(bars):
            if s.on_bar("X", b, bars[:i+1], in_position=False):
                fired_count += 1
        self.assertEqual(fired_count, 1)

    def test_no_entry_after_14_30(self):
        from day_trader import ORB5Strategy, ORB5Params
        params = ORB5Params(volume_multiplier=2.0, require_vwap_align=False,
                            min_orb_range_pct=0.1, no_entry_after_min=14 * 60 + 30)
        s = ORB5Strategy(params)
        # Build a session whose ONLY breakout is at 14:35 (no_entry blocks it)
        bars = _synthetic_session("X", "2026-06-10", breakout_at_minute=None,
                                  breakout_volume_mult=3.0)
        # Force a manual breakout bar at 14:35 ET
        from minute_bars import Bar
        late_breakout = _bar("X", "2026-06-10", 14, 35,
                             100.4, 102.0, 100.3, 101.5, 5000.0, vw=101.0)
        bars.append(late_breakout)
        bars.sort(key=lambda b: b.ts)
        s.reset_for_session("X", "2026-06-10")
        for i, b in enumerate(bars):
            self.assertEqual(s.on_bar("X", b, bars[:i+1], in_position=False), [])


class TestBacktestSimulator(unittest.TestCase):
    """End-to-end: synthetic session → Simulator → TradeLog with one trade."""

    def test_synthetic_round_trip(self):
        from day_trader import ORB5Strategy, ORB5Params
        from backtest.replay import Simulator
        from backtest.fill_model import SimulatedFillModel
        params = ORB5Params(volume_multiplier=2.0, require_vwap_align=False,
                            min_orb_range_pct=0.1)
        strat = ORB5Strategy(params)
        bars = _synthetic_session("X", "2026-06-10",
                                  open_price=100.0, orb_height=1.0,
                                  breakout_at_minute=35,
                                  breakout_volume_mult=3.0,
                                  drift=0.2)
        sim = Simulator(SimulatedFillModel(slippage_bps=0))
        log = sim.run(strat, sessions=[("X", "2026-06-10", bars)])
        self.assertEqual(log.sessions_processed, 1)
        self.assertEqual(len(log.trades), 1)
        trade = log.trades[0]
        self.assertEqual(trade.ticker, "X")
        self.assertEqual(trade.setup_id, "ORB5")
        # With strong drift the breakout should hit T1 → winner
        self.assertIn(trade.exit_reason, ("TARGET", "EOD", "TIME"))

    def test_simulator_metrics(self):
        from backtest.fill_model import TradeRecord
        from backtest import metrics
        trades = [
            TradeRecord("AAA", "ORB5", 0, 100, 60, 102, 100,
                        200, 2.0, "TARGET", 30),
            TradeRecord("AAA", "ORB5", 0, 100, 60,  99, 100,
                       -100, -1.0, "STOP", 15),
            TradeRecord("BBB", "ORB5", 0, 50,  60,  51, 200,
                        200, 2.0, "TARGET", 25),
        ]
        m = metrics.compute(trades)
        self.assertEqual(m.trades, 3)
        self.assertEqual(m.winners, 2)
        self.assertEqual(m.losers, 1)
        self.assertAlmostEqual(m.win_rate, 66.67, places=1)
        self.assertEqual(m.total_pnl, 300.0)
        self.assertEqual(m.by_setup["ORB5"]["n"], 3)


# ===========================================================================
# Day-trader: risk_manager.can_enter_dt_trade gate + premarket_scanner ranking
# ===========================================================================

class TestCanEnterDtTrade(unittest.TestCase):
    """Verify the DT-aware risk gate accepts/rejects the right inputs.
    Uses a tempfile DB so we can mutate agent_config freely without polluting
    the real one."""

    def setUp(self):
        import tempfile
        import os as _os
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._prev_db = _os.environ.get("NUROQ_DB_PATH")
        _os.environ["NUROQ_DB_PATH"] = self.tmp.name
        import importlib
        import agent_config
        import risk_manager
        importlib.reload(agent_config)
        importlib.reload(risk_manager)
        self.agent_config = agent_config
        self.risk_manager = risk_manager
        # Pretend market is open + inside DT window so gates 3/4 always pass.
        from datetime import datetime, time as _t
        try:
            from zoneinfo import ZoneInfo
            self._tz = ZoneInfo("America/New_York")
        except Exception:
            self._tz = None
        self._stub_now = datetime(2026, 6, 15, 10, 0, tzinfo=self._tz) \
                         if self._tz else datetime(2026, 6, 15, 10, 0)
        self._orig_now_et = self.risk_manager._now_et
        self._orig_market_open = self.risk_manager._market_open
        self.risk_manager._now_et = lambda: self._stub_now
        self.risk_manager._market_open = lambda _now: True

    def tearDown(self):
        import os as _os
        self.risk_manager._now_et = self._orig_now_et
        self.risk_manager._market_open = self._orig_market_open
        try:
            _os.unlink(self.tmp.name)
        except Exception:
            pass
        if self._prev_db is None:
            _os.environ.pop("NUROQ_DB_PATH", None)
        else:
            _os.environ["NUROQ_DB_PATH"] = self._prev_db

    def _ok_args(self, **over):
        d = dict(symbol="AAPL", entry=100.0, stop=99.0, target=102.0,
                 open_positions=0, cash=50000.0, todays_pl=0.0,
                 equity=50000.0, on_margin=False)
        d.update(over)
        return d

    def test_blocked_when_mode_not_auto(self):
        # default is "disabled"
        d = self.risk_manager.can_enter_dt_trade(**self._ok_args())
        self.assertFalse(d.ok)
        self.assertIn("dt_mode=disabled", d.reason)
        self.agent_config.update(dt_mode="shadow")
        d = self.risk_manager.can_enter_dt_trade(**self._ok_args())
        self.assertFalse(d.ok)
        self.assertIn("dt_mode=shadow", d.reason)

    def test_allowed_when_mode_auto_and_gates_pass(self):
        self.agent_config.update(dt_mode="auto")
        d = self.risk_manager.can_enter_dt_trade(**self._ok_args())
        self.assertTrue(d.ok, d.reason)
        self.assertIsNotNone(d.sizing)
        # Sizing should be > 0
        self.assertGreater(d.sizing.shares, 0)
        # Per-share risk = 1, dt_risk_per_trade_pct=0.5%, budget=10000
        # so shares_by_risk = (10000 * 0.005) / 1 = 50
        # per_position_cap = 10000/3 ≈ 3333 → shares_by_size = 33
        # → min = 33
        self.assertEqual(d.sizing.shares, 33)

    def test_blocked_by_dt_concurrency_cap(self):
        self.agent_config.update(dt_mode="auto", dt_max_concurrent=2)
        d = self.risk_manager.can_enter_dt_trade(**self._ok_args(open_positions=2))
        self.assertFalse(d.ok)
        self.assertIn("DT concurrency", d.reason)

    def test_blocked_by_dt_entry_window_end(self):
        self.agent_config.update(dt_mode="auto", dt_entry_window_end="14:30")
        # Move stub to 15:00 ET — past the window
        from datetime import datetime as _dt
        late = _dt(2026, 6, 15, 15, 0, tzinfo=self._tz) if self._tz else _dt(2026, 6, 15, 15, 0)
        self.risk_manager._now_et = lambda: late
        d = self.risk_manager.can_enter_dt_trade(**self._ok_args())
        self.assertFalse(d.ok)
        self.assertIn("outside DT entry window", d.reason)

    def test_blocked_by_daily_loss_limit(self):
        self.agent_config.update(dt_mode="auto", daily_loss_limit_pct=2.0)
        # 3% loss on $50k = $1500 down → trip
        d = self.risk_manager.can_enter_dt_trade(
            **self._ok_args(todays_pl=-1500.0, equity=50000.0))
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "daily loss limit")
        # And confirm halt persisted to config.
        self.assertIsNotNone(self.agent_config.get().get("halted_at"))

    def test_blocked_when_halted(self):
        self.agent_config.halt("test halt")
        self.agent_config.update(dt_mode="auto")
        d = self.risk_manager.can_enter_dt_trade(**self._ok_args())
        self.assertFalse(d.ok)
        self.assertTrue(d.reason.startswith("halted:"))

    def test_blocked_by_invalid_ordering(self):
        self.agent_config.update(dt_mode="auto")
        d = self.risk_manager.can_enter_dt_trade(
            **self._ok_args(stop=101.0, target=103.0))  # stop > entry
        self.assertFalse(d.ok)
        self.assertIn("ordering invalid", d.reason)


class TestPremarketScanner(unittest.TestCase):
    """Premarket scanner ranking + filter gates. Mocks data sources so this
    runs offline."""

    def setUp(self):
        import tempfile
        import os as _os
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._prev_db = _os.environ.get("NUROQ_DB_PATH")
        _os.environ["NUROQ_DB_PATH"] = self.tmp.name
        import importlib
        import agent_config
        import minute_bars
        import premarket_scanner
        importlib.reload(agent_config)
        importlib.reload(minute_bars)
        importlib.reload(premarket_scanner)
        self.scanner = premarket_scanner

    def tearDown(self):
        import os as _os
        try:
            _os.unlink(self.tmp.name)
        except Exception:
            pass
        if self._prev_db is None:
            _os.environ.pop("NUROQ_DB_PATH", None)
        else:
            _os.environ["NUROQ_DB_PATH"] = self._prev_db

    def _premarket_bars(self, ticker, base_price=100.0, vol_per_bar=10_000):
        """Build a sequence of premarket bars (08:00-09:29 ET) for the ticker."""
        bars = []
        for m in range(90):
            hh = 8 + m // 60
            mm = m % 60
            p = base_price + m * 0.01
            bars.append(_bar(ticker, "2026-06-15", hh, mm,
                             p, p + 0.05, p - 0.05, p, vol_per_bar))
        return bars

    def test_ranking_picks_top_n_and_writes_universe(self):
        # Stub get_full_history → returns yesterday's close
        # Stub get_minute_bars → returns synthetic premarket
        # Stub check_news_for_crossing → returns variety
        history_data = {
            "GAPPER1": [{"c": 100.0}, {"c": 100.0}],  # prev close 100
            "GAPPER2": [{"c": 50.0},  {"c": 50.0}],   # prev close 50
            "FLAT":    [{"c": 100.0}, {"c": 100.0}],
            "DEAD":    [{"c": 100.0}, {"c": 100.0}],
        }
        # GAPPER1: +5% gap, high vol, POSITIVE_BOOST → highest GMS
        # GAPPER2: +6% gap, modest vol, NEUTRAL_NEWS  → middle GMS
        # FLAT:    +0% gap                            → filtered (gap_too_small)
        # DEAD:    +5% gap, premkt vol < 50k          → filtered (premkt_volume)
        last_prices = {
            "GAPPER1": 105.0, "GAPPER2": 53.0, "FLAT": 100.0, "DEAD": 105.0,
        }
        per_bar_vol = {
            "GAPPER1": 5_000, "GAPPER2": 2_000, "FLAT": 5_000, "DEAD": 100,
        }
        news_table = {
            "GAPPER1": {"classification": "POSITIVE_BOOST",
                        "headline": "FDA approval"},
            "GAPPER2": {"classification": "NEUTRAL_NEWS", "headline": ""},
            "FLAT":    None,
            "DEAD":    None,
        }

        def fake_history(ticker, logger=None):
            return history_data.get(ticker, [])

        def fake_minute_bars(ticker, session_date, include_premarket=True,
                             force_refresh=False, logger=None):
            base = last_prices.get(ticker)
            if not base:
                return []
            vol = per_bar_vol.get(ticker, 1000)
            # Build bars whose last close = base price
            bars = self._premarket_bars(ticker, base_price=base - 0.9, vol_per_bar=vol)
            # Force the last bar's close to EXACTLY base so gap_pct matches
            last = bars[-1]
            from minute_bars import Bar
            bars[-1] = Bar(
                ticker=last.ticker, ts=last.ts, open=last.open,
                high=max(last.high, base), low=min(last.low, base - 0.5),
                close=base, volume=last.volume, vwap=last.vwap,
            )
            return bars

        def fake_news(ticker):
            return news_table.get(ticker)

        with patch.object(self.scanner, "get_minute_bars", side_effect=fake_minute_bars), \
             patch("data_fetcher.get_full_history", side_effect=fake_history), \
             patch("news_engine.check_news_for_crossing", side_effect=fake_news):
            result = self.scanner.build_dt_universe(
                top_n=2,
                candidates=["GAPPER1", "GAPPER2", "FLAT", "DEAD"],
                session_date="2026-06-15",
            )

        self.assertEqual(result["scanned"], 4)
        self.assertEqual(result["kept"], 2)
        # GAPPER1 must rank above GAPPER2 (positive boost + higher volume).
        self.assertEqual(result["rows"][0]["ticker"], "GAPPER1")
        self.assertEqual(result["rows"][1]["ticker"], "GAPPER2")
        # Filters captured the dropped ones.
        self.assertGreaterEqual(result["filters"]["gap_too_small"], 1)
        self.assertGreaterEqual(result["filters"]["premkt_volume"], 1)
        # Universe was written to agent_config.
        import agent_config
        self.assertEqual(agent_config.get()["dt_universe"], "GAPPER1,GAPPER2")

    def test_negative_block_drops_ticker(self):
        history_data = {"BAD": [{"c": 100.0}, {"c": 100.0}]}

        def fake_history(ticker, logger=None):
            return history_data.get(ticker, [])

        def fake_minute_bars(ticker, session_date, include_premarket=True,
                             force_refresh=False, logger=None):
            return self._premarket_bars("BAD", base_price=104.5, vol_per_bar=10_000)

        def fake_news(ticker):
            return {"classification": "NEGATIVE_BLOCK", "headline": "SEC fraud probe"}

        with patch.object(self.scanner, "get_minute_bars", side_effect=fake_minute_bars), \
             patch("data_fetcher.get_full_history", side_effect=fake_history), \
             patch("news_engine.check_news_for_crossing", side_effect=fake_news):
            result = self.scanner.build_dt_universe(
                top_n=5, candidates=["BAD"], session_date="2026-06-15",
            )
        self.assertEqual(result["kept"], 0)
        self.assertEqual(result["filters"]["negative_block"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
