"""
test_scoring.py — Unit tests for the AntiGravity Quant Scoring Engine (v2)
Run with: uv run test_scoring.py
"""

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scoring import calculate_quant_score, calculate_technicals, calculate_sizing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_techs(**overrides) -> dict:
    base = {
        "trend": "STRONG UP", "rsi": 50, "rel_vol": 1.6,
        "percent_b": 0.5, "volatility": 2.0, "atr": 1.5,
        "price": 100.0, "sma_20": 95.0,
        "semantic_rsi": "NEUTRAL", "semantic_bb": "NEUTRAL",
    }
    base.update(overrides)
    return base

def _base_funds(**overrides) -> dict:
    base = {"pe": 20, "growth": 0.2, "news": "N/A", "name": "TestCo", "industry": "Tech"}
    base.update(overrides)
    return base

E_RISK_SAFE = {"days": 30, "risk": False}
E_RISK_HOT  = {"days": 2,  "risk": True}

passed = failed = 0

def assert_test(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        print(f"  ✅ PASS  {name}")
        passed += 1
    else:
        print(f"  ❌ FAIL  {name}  ← {detail}")
        failed += 1


# ---------------------------------------------------------------------------
# FIX #1 — Negative P/E Bug
# ---------------------------------------------------------------------------
print("\n── FIX #1: Negative P/E Bug ─────────────────────────────────────────")

score_neg_pe = calculate_quant_score(
    _base_techs(), _base_funds(pe=-5), "UP", E_RISK_SAFE, "NEUTRAL", 80
)
score_zero_pe = calculate_quant_score(
    _base_techs(), _base_funds(pe=0), _base_funds()["pe"], E_RISK_SAFE, "NEUTRAL", 80
)
score_good_pe = calculate_quant_score(
    _base_techs(), _base_funds(pe=12), "UP", E_RISK_SAFE, "NEUTRAL", 80
)

assert_test(
    "Negative P/E earns 0 valuation points (less than P/E=12)",
    score_neg_pe < score_good_pe,
    f"neg_pe={score_neg_pe}, good_pe={score_good_pe}"
)
assert_test(
    "Deep-value P/E (<15) scores higher than moderate P/E (20)",
    score_good_pe > calculate_quant_score(
        _base_techs(), _base_funds(pe=20), "UP", E_RISK_SAFE, "NEUTRAL", 80
    ),
    f"pe12={score_good_pe}"
)

# ---------------------------------------------------------------------------
# FIX #2 — RSI Deadzone / Mean-Reversion Bonus
# ---------------------------------------------------------------------------
print("\n── FIX #2: RSI Mean-Reversion Bonus ────────────────────────────────")

score_rsi_oversold = calculate_quant_score(
    _base_techs(rsi=25), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)
score_rsi_neutral = calculate_quant_score(
    _base_techs(rsi=50), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)
score_rsi_overbought = calculate_quant_score(
    _base_techs(rsi=82), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)

assert_test(
    "RSI < 30 (oversold) earns mean-reversion bonus ≥ neutral RSI",
    score_rsi_oversold >= score_rsi_neutral,
    f"oversold={score_rsi_oversold}, neutral={score_rsi_neutral}"
)
assert_test(
    "RSI > 75 (overbought) earns fewer points than neutral RSI",
    score_rsi_overbought < score_rsi_neutral,
    f"overbought={score_rsi_overbought}, neutral={score_rsi_neutral}"
)

# ---------------------------------------------------------------------------
# FIX #3 — Bollinger Band %B Integration
# ---------------------------------------------------------------------------
print("\n── FIX #3: Bollinger %B Integration ────────────────────────────────")

score_lower_band = calculate_quant_score(
    _base_techs(percent_b=0.05), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)
score_mid_band = calculate_quant_score(
    _base_techs(percent_b=0.50), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)
score_upper_band = calculate_quant_score(
    _base_techs(percent_b=0.95), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)
score_high_vol = calculate_quant_score(
    _base_techs(volatility=6.5), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)
score_low_vol = calculate_quant_score(
    _base_techs(volatility=1.5), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 50
)

assert_test(
    "Near lower band (%B=0.05) scores higher than mid-band",
    score_lower_band > score_mid_band,
    f"lower={score_lower_band}, mid={score_mid_band}"
)
assert_test(
    "Near upper band (%B=0.95) scores lower than mid-band",
    score_upper_band < score_mid_band,
    f"upper={score_upper_band}, mid={score_mid_band}"
)
assert_test(
    "High volatility (>5%) incurs a penalty vs low volatility",
    score_high_vol < score_low_vol,
    f"high_vol={score_high_vol}, low_vol={score_low_vol}"
)

# ---------------------------------------------------------------------------
# FIX #4 — AI Conviction Gate
# ---------------------------------------------------------------------------
print("\n── FIX #4: Gated AI Conviction ──────────────────────────────────────")

# Weak quant setup: no growth, bad PE, no volume, bearish trend
weak_techs = _base_techs(trend="WEAK DOWN", rel_vol=0.5, rsi=60, percent_b=0.5)
weak_funds = _base_funds(pe=200, growth=-0.1)

score_weak_ai100 = calculate_quant_score(weak_techs, weak_funds, "DOWN", E_RISK_SAFE, "NEUTRAL", 100)
score_weak_ai0   = calculate_quant_score(weak_techs, weak_funds, "DOWN", E_RISK_SAFE, "NEUTRAL", 0)

assert_test(
    "AI score of 100 does NOT rescue a fundamentally weak setup (gate active)",
    score_weak_ai100 == score_weak_ai0,
    f"ai100={score_weak_ai100}, ai0={score_weak_ai0}"
)

strong_techs = _base_techs(trend="STRONG UP", rel_vol=2.0, rsi=45, percent_b=0.2)
strong_funds = _base_funds(pe=18, growth=0.35)
score_strong_ai100 = calculate_quant_score(strong_techs, strong_funds, "UP", E_RISK_SAFE, "BULLISH", 100)
score_strong_ai0   = calculate_quant_score(strong_techs, strong_funds, "UP", E_RISK_SAFE, "BULLISH", 0)

assert_test(
    "AI score of 100 DOES add value on a strong quant setup",
    score_strong_ai100 > score_strong_ai0,
    f"ai100={score_strong_ai100}, ai0={score_strong_ai0}"
)

# ---------------------------------------------------------------------------
# Earnings Risk Penalty
# ---------------------------------------------------------------------------
print("\n── Earnings Risk Penalty ────────────────────────────────────────────")
score_safe     = calculate_quant_score(_base_techs(), _base_funds(), "UP", E_RISK_SAFE, "NEUTRAL", 70)
score_earnings = calculate_quant_score(_base_techs(), _base_funds(), "UP", E_RISK_HOT,  "NEUTRAL", 70)

assert_test(
    "Earnings within 5 days incurs a penalty",
    score_earnings < score_safe,
    f"safe={score_safe}, earnings={score_earnings}"
)

# ---------------------------------------------------------------------------
# calculate_technicals — sanity check
# ---------------------------------------------------------------------------
print("\n── calculate_technicals Sanity Checks ───────────────────────────────")

mock_history = [
    {"c": 100 + i * 0.5, "o": 99 + i * 0.5, "h": 101 + i * 0.5, "l": 98 + i * 0.5, "v": 1_000_000 + i * 10_000}
    for i in range(60)
]
techs = calculate_technicals(mock_history)
assert_test("calculate_technicals returns a dict", isinstance(techs, dict))
assert_test("RSI is within 0-100", 0 <= techs["rsi"] <= 100, f"rsi={techs['rsi']}")
assert_test("ATR is positive", techs["atr"] > 0, f"atr={techs['atr']}")
assert_test("percent_b is a float", isinstance(techs["percent_b"], float))

# ---------------------------------------------------------------------------
# calculate_sizing — sanity check
# ---------------------------------------------------------------------------
print("\n── calculate_sizing Sanity Checks ───────────────────────────────────")
sizing = calculate_sizing(price=150.0, atr=2.5, account=10_000, risk_pct=0.01)
assert_test("Stop loss is below entry price", sizing["sl"] < 150.0)
assert_test("Take profit is above entry price", sizing["tp"] > 150.0)
assert_test("Shares are positive", sizing["shares"] > 0)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*55}")
print(f"  Results: {passed} passed | {failed} failed | {passed + failed} total")
print(f"{'='*55}")
sys.exit(0 if failed == 0 else 1)
