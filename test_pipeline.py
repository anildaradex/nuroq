"""Tests for the open-bell pipeline (pipeline/). Also loaded by master_test_suite.py."""
import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from pipeline import ai_flags, execute, features, regime, research, store, tracker
from pipeline.strategies import trend_pullback as tp


def make_bars(closes, vol=5_000_000, start=date(2025, 1, 2), spread=0.01):
    """Daily bars from a close series; high/low are ±spread around the close."""
    bars, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        bars.append({"o": c, "h": c * (1 + spread), "l": c * (1 - spread), "c": c,
                     "v": vol, "t": d.isoformat()})
        d += timedelta(days=1)
    return bars


def uptrend_pullback():
    """300 bars rising 50 -> ~110, then a 4-day dip and an up day closing at its high."""
    closes = [50 + 0.2 * i for i in range(295)]
    peak = closes[-1]
    closes += [peak * 0.985, peak * 0.97, peak * 0.955, peak * 0.945, peak * 0.96]
    bars = make_bars(closes)
    bars[-1]["h"] = bars[-1]["c"]          # closes at the top of its range
    return bars


class TestFeaturesAndEligibility(unittest.TestCase):
    def test_too_little_history_returns_none(self):
        self.assertIsNone(features.compute(make_bars([100.0] * 100)))

    def test_uptrend_pullback_is_eligible(self):
        f = features.compute(uptrend_pullback())
        ok, fails = tp.eligible(f)
        self.assertTrue(ok, fails)
        self.assertGreater(f["pullback_depth_atr"], 1.0)
        self.assertTrue(f["reversal"])

    def test_falling_knife_fails_trend_rules(self):
        closes = [50 + 0.2 * i for i in range(240)] + [98 - 0.8 * i for i in range(60)]
        ok, fails = tp.eligible(features.compute(make_bars(closes)))
        self.assertFalse(ok)
        self.assertTrue(any("50-day" in x for x in fails))

    def test_illiquid_and_cheap_fail(self):
        f = features.compute(make_bars([2 + 0.005 * i for i in range(300)], vol=100_000))
        ok, fails = tp.eligible(f)
        self.assertFalse(ok)
        self.assertEqual(len([x for x in fails if "price" in x or "dollar volume" in x]), 2)

    def test_earnings_within_ten_days_blocks(self):
        f = features.compute(uptrend_pullback())
        self.assertFalse(tp.eligible(f, earnings_in_days=7)[0])
        self.assertTrue(tp.eligible(f, earnings_in_days=11)[0])
        self.assertFalse(tp.eligible(f, reported_last_night=True)[0])


class TestScoreAndExits(unittest.TestCase):
    def setUp(self):
        self.f = features.compute(uptrend_pullback())

    def test_score_breakdown_sums_and_caps(self):
        s = tp.score(self.f, rs_rank=1.0, sector_strong=True, ai_points=10)
        self.assertEqual(s["total"], int(round(sum(s["breakdown"].values()))))
        self.assertLessEqual(s["total"], 100)
        self.assertEqual(s["breakdown"]["relative_strength"], 25)

    def test_veto_forces_avoid(self):
        s = tp.score(self.f, 1.0, True, 10, vetoed=True)
        self.assertEqual(s["rating"], "AVOID")

    def test_rating_thresholds(self):
        self.assertEqual(tp.rating_for(tp.BUY_THRESHOLD), "BUY")
        self.assertEqual(tp.rating_for(tp.BUY_THRESHOLD - 1), "WATCH")
        self.assertEqual(tp.rating_for(tp.WATCH_THRESHOLD - 1), "AVOID")

    def test_percentile_ranks(self):
        r = tp.percentile_ranks({"A": 0.1, "B": 0.5, "C": None, "D": 0.3})
        self.assertEqual((r["A"], r["D"], r["B"], r["C"]), (0.0, 0.5, 1.0, 0.0))

    def test_stop_is_wider_level_within_floor_and_cap(self):
        # pullback low far below entry: capped at 3 ATR
        self.assertEqual(tp.exit_plan(100, 2, 80)["stop"], 94.0)
        # pullback low just under entry: 2 ATR level is wider and wins
        self.assertEqual(tp.exit_plan(100, 2, 99)["stop"], 96.0)
        # between the two: pullback low - 0.25 ATR
        self.assertEqual(tp.exit_plan(100, 2, 95.5)["stop"], 95.0)
        self.assertEqual(tp.exit_plan(100, 2, 99)["target"], 106.0)

    def test_size_respects_risk_and_notional_cap(self):
        self.assertEqual(tp.size(100, 96, 10_000, 1.0)["shares"], 25)     # $100 risk / $4
        self.assertEqual(tp.size(100, 99.5, 10_000, 1.0)["shares"], 25)   # capped at 25% of budget
        self.assertEqual(tp.size(100, 101, 10_000, 1.0)["shares"], 0)


class TestRegime(unittest.TestCase):
    def _spy(self, last5):
        return make_bars([100.0] * 195 + [last5] * 5)

    def test_on_off_and_hysteresis(self):
        self.assertTrue(regime.evaluate(self._spy(105), False)["on"])
        self.assertFalse(regime.evaluate(self._spy(95), True)["on"])
        self.assertTrue(regime.evaluate(self._spy(100.2), True)["on"])     # inside band: hold
        self.assertFalse(regime.evaluate(self._spy(100.2), False)["on"])

    def test_short_history_is_off_without_prior(self):
        self.assertFalse(regime.evaluate(make_bars([100.0] * 50), None)["on"])


class TestAIFlags(unittest.TestCase):
    TODAY = date(2026, 11, 20)

    def _run(self, events, domains):
        import json
        return lambda prompt: (json.dumps({"events": events}),
                               [{"title": d, "url": "x"} for d in domains])

    def test_parse_drops_unknown_types_and_bad_dates(self):
        txt = ('noise {"events":[{"type":"guidance_cut","date":"2026-11-18","summary":"s",'
               '"sources":["https://www.Reuters.com/x"]},{"type":"vibes","date":"2026-11-18"},'
               '{"type":"buyback","date":"soon"}]}')
        ev = ai_flags.parse_events(txt)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["sources"], ["reuters.com"])

    def test_confirmed_adverse_event_vetoes(self):
        e = [{"type": "guidance_cut", "date": "2026-11-18", "summary": "cut FY guide",
              "sources": ["reuters.com", "cnbc.com"]}]
        out = ai_flags.assess("X", "X Corp", self._run(e, ["reuters.com", "cnbc.com"]), self.TODAY)
        self.assertTrue(out["vetoed"])
        self.assertEqual(out["points"], 0)
        self.assertEqual(out["runs"], 3)
        self.assertIn("guidance_cut", out["veto_reason"])

    def test_unconfirmed_adverse_scores_zero_without_veto(self):
        e = [{"type": "accounting_probe", "date": "2026-11-18", "summary": "rumour",
              "sources": ["someblog.com"]}]
        out = ai_flags.assess("X", "X Corp", self._run(e, ["someblog.com"]), self.TODAY)
        self.assertFalse(out["vetoed"])
        self.assertEqual(out["points"], 0)
        self.assertEqual(out["runs"], 1)

    def test_stale_adverse_event_does_not_cost_points(self):
        e = [{"type": "guidance_cut", "date": "2026-10-27", "summary": "old", "sources": ["x.com"]}]
        out = ai_flags.assess("X", "X", self._run(e, ["x.com"]), self.TODAY)
        self.assertEqual(out["points"], 5)
        self.assertFalse(out["flags"][0]["recent"])

    def test_stale_event_is_not_confirmed(self):
        e = {"type": "guidance_cut", "date": "2026-10-01", "sources": ["sec.gov"]}
        self.assertFalse(ai_flags.is_confirmed(e, self.TODAY, {"sec.gov"}))

    def test_positive_catalyst_and_neutral_points(self):
        e = [{"type": "buyback", "date": "2026-11-19", "summary": "b", "sources": ["sec.gov"]}]
        self.assertEqual(ai_flags.assess("X", "X", self._run(e, ["sec.gov"]), self.TODAY)["points"], 10)
        self.assertEqual(ai_flags.assess("X", "X", self._run([], []), self.TODAY)["points"], 5)

    def test_model_unavailable_is_neutral(self):
        def boom(prompt):
            raise RuntimeError("no creds")
        out = ai_flags.assess("X", "X", boom, self.TODAY)
        self.assertFalse(out["available"])
        self.assertEqual(out["points"], 5)
        self.assertFalse(out["vetoed"])


class TestEntryCheck(unittest.TestCase):
    PICK = {"atr": 2.0, "prev_close": 100.0, "prev_low": 98.5, "pullback_low": 97.0}

    def _q(self, bid, ask, day_open=100.0, age=2):
        return {"bid": bid, "ask": ask, "day_open": day_open, "quote_age_s": age}

    def test_clean_quote_passes(self):
        reason, m = execute.entry_check(self.PICK, self._q(100.40, 100.45))
        self.assertIsNone(reason)
        self.assertAlmostEqual(m["gap_atr"], 0.21, places=2)

    def test_skips(self):
        cases = [
            (None, "no quote"),
            (self._q(100.4, 100.45, age=40), "stale quote"),
            (self._q(100.0, 100.8), "spread"),
            (self._q(102.5, 102.55, day_open=102.5), "gapped up"),
            (self._q(97.6, 97.65, day_open=97.6), "gapped down"),
        ]
        for q, expect in cases:
            reason, _ = execute.entry_check(self.PICK, q)
            self.assertIn(expect, reason or "")


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._env = patch.dict(os.environ, {"NUROQ_DB_PATH": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.unlink(self._tmp.name)

    def _seed(self, regime_on=True, n=3):
        f = features.compute(uptrend_pullback())
        rows = [{"ticker": t, "rank": i + 1, "score": 80 - i, "rating": "BUY",
                 "score_no_ai": 75, "rating_no_ai": "BUY", "sector": "Technology",
                 "breakdown": {}, "features": f, "ai": None}
                for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"][:n])]
        store.save_run("2026-11-19", {"on": regime_on, "ratio": 1.05}, 10, rows, rows)
        return f


class TestExecuteRun(_DbCase):
    def _quote_for(self, f):
        c = f["close"]
        return lambda t: {"bid": c + 0.10, "ask": c + 0.14, "day_open": c + 0.1, "quote_age_s": 1}

    def _run(self, f, **kw):
        sent = []
        with patch("agent_config.get", return_value={"open_bell_mode": kw.pop("mode", "notify"),
                                                     "budget": 10_000, "risk_per_trade_pct": 1.0}):
            r = execute.run(notify=sent.append, log=lambda m: None,
                            quote_fn=kw.pop("quote_fn", self._quote_for(f)),
                            today=date(2026, 11, 20), **kw)
        return r, sent

    def test_notify_mode_caps_entries_and_sectors(self):
        f = self._seed(n=4)
        r, sent = self._run(f)
        self.assertEqual(r["notified"], ["AAA", "BBB"])
        self.assertIn("daily entry cap", r["skipped"]["CCC"])
        self.assertTrue(any("no order placed" in m for m in sent))
        rows = {p["ticker"]: p for p in store.picks("2026-11-19")}
        self.assertEqual(rows["AAA"]["status"], "notified")
        self.assertGreater(rows["AAA"]["shares"], 0)
        self.assertLess(rows["AAA"]["stop"], rows["AAA"]["ref_entry"])

    def test_off_mode_does_nothing(self):
        f = self._seed()
        r, sent = self._run(f, mode="off")
        self.assertEqual((r["notified"], sent), ([], []))
        self.assertEqual(store.picks("2026-11-19")[0]["status"], "pending")

    def test_regime_off_skips_all(self):
        f = self._seed(regime_on=False)
        r, _ = self._run(f)
        self.assertEqual(r["notified"], [])
        self.assertEqual(set(r["skipped"].values()), {"market regime off"})

    def test_held_and_news_block_skip(self):
        f = self._seed()
        r, _ = self._run(f, held_fn=lambda: {"AAA"}, news_block_fn=lambda t: t == "BBB")
        self.assertEqual(r["notified"], ["CCC"])
        self.assertEqual(r["skipped"]["AAA"], "already held")

    def test_recheck_only_retries_quote_quality_skips(self):
        f = self._seed(n=2)
        wide = lambda t: {"bid": f["close"], "ask": f["close"] + 2, "day_open": f["close"], "quote_age_s": 1}
        r1, _ = self._run(f, quote_fn=wide)
        self.assertEqual(r1["notified"], [])
        r2, _ = self._run(f, recheck=True)
        self.assertEqual(r2["notified"], ["AAA", "BBB"])

    def test_stale_research_is_ignored(self):
        f = self._seed()
        with store.connect() as c:
            c.execute("UPDATE open_bell_runs SET created_at=?", (time.time() - 30 * 3600,))
        r, _ = self._run(f)
        self.assertEqual(r["notified"], [])


class TestTracker(unittest.TestCase):
    def _bars(self, closes):
        return make_bars(closes, start=date(2026, 11, 2), spread=0.0)

    def test_forward_returns(self):
        bars = self._bars([100, 101, 102, 103, 104, 105, 106])
        out = tracker.forward_returns(bars, bars[0]["t"], 100.0)
        self.assertEqual(out, {"fwd_1": 0.01, "fwd_5": 0.05})

    def test_bracket_target_stop_time_and_gap(self):
        up = self._bars([100, 102, 104, 107])
        r = tracker.simulate_bracket(up, up[0]["t"], 100, 96, 106)
        self.assertEqual((r["exit_reason"], r["r_multiple"]), ("target", 1.75))   # gap open 107 > 106
        down = self._bars([100, 99, 94])
        r = tracker.simulate_bracket(down, down[0]["t"], 100, 96, 106)
        self.assertEqual((r["exit_reason"], r["r_multiple"]), ("stop", -1.5))     # gap through stop
        flat = self._bars([100] * 12)
        r = tracker.simulate_bracket(flat, flat[0]["t"], 100, 96, 106)
        self.assertEqual((r["exit_reason"], r["r_multiple"]), ("time", 0.0))
        self.assertIsNone(tracker.simulate_bracket(flat[:5], flat[0]["t"], 100, 96, 106))


class TestResearchScoring(unittest.TestCase):
    def test_rank_and_score_orders_and_tracks_ai_attribution(self):
        f = features.compute(uptrend_pullback())
        feats = {"AAA": dict(f, ret_126=0.5, ret_252=0.9), "BBB": dict(f, ret_126=0.1, ret_252=0.2)}
        ai = {"AAA": {"points": 0.0, "vetoed": True}, "BBB": {"points": 10.0, "vetoed": False}}
        rows = research.rank_and_score(feats, {"AAA": "Technology", "BBB": None},
                                       {"Technology": True}, ai)
        by = {r["ticker"]: r for r in rows}
        self.assertEqual(by["AAA"]["rating"], "AVOID")
        self.assertGreater(by["AAA"]["score_no_ai"], by["BBB"]["score_no_ai"])
        self.assertEqual(by["BBB"]["breakdown"]["sector"], 5.0)
        self.assertEqual([r["rank"] for r in rows], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
