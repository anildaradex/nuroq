"""
scoring.py — AntiGravity Quantitative Scoring Engine
Handles all technical analysis, position sizing, and the hybrid quant score.

Quant Refactor (v2) changes vs original:
  1. P/E Bug Fix    — negative P/E no longer earns valuation points.
  2. RSI Deadzone   — oversold RSI (<30) now earns mean-reversion bonus.
  3. %B Integration — Bollinger Band position now contributes to the score.
  4. AI Gate        — AI conviction capped unless quant base score >= 40.
"""


# ---------------------------------------------------------------------------
# Technical Indicators
# ---------------------------------------------------------------------------

from typing import Optional, Union

def calculate_technicals(history: list) -> Optional[dict]:
    """
    Computes RSI, Bollinger Bands, ATR, and volume metrics from OHLCV history.
    Requires at least 5 bars; returns None if insufficient data.
    """
    if len(history) < 5:
        return None

    closes  = [d["c"] for d in history]
    volumes = [d["v"] for d in history]
    highs   = [d["h"] for d in history]
    lows    = [d["l"] for d in history]

    price      = closes[-1]
    prev_close = closes[-2]
    change     = round(((price - prev_close) / prev_close) * 100, 2)

    # 1. RSI (14-day)
    window_rsi = min(14, len(closes))
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas[-window_rsi:]]
    losses = [-d if d < 0 else 0 for d in deltas[-window_rsi:]]
    avg_gain = sum(gains) / window_rsi
    avg_loss = sum(losses) / window_rsi
    rs  = avg_gain / avg_loss if avg_loss > 0 else 100
    rsi = round(100 - (100 / (1 + rs)), 2)

    # 2. Bollinger Bands (20-day)
    window_bb = min(20, len(closes))
    sma_20    = sum(closes[-window_bb:]) / window_bb
    variance  = sum((x - sma_20) ** 2 for x in closes[-window_bb:]) / window_bb
    std_dev   = variance ** 0.5
    upper_band = sma_20 + (2 * std_dev)
    lower_band = sma_20 - (2 * std_dev)
    band_range = upper_band - lower_band
    percent_b  = round((price - lower_band) / band_range, 2) if band_range > 0 else 0.5

    # 3. Volume & Volatility
    window_vol = min(20, len(volumes))
    avg_vol    = sum(volumes[-window_vol:]) / window_vol
    rel_vol    = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 0
    volatility = round(((highs[-1] - lows[-1]) / price) * 100, 2)

    # 4. ATR (14-day) — used for position sizing
    high_low    = [h - l for h, l in zip(highs[-14:], lows[-14:])]
    high_close  = [abs(h - c) for h, c in zip(highs[-14:], closes[-15:-1])]
    low_close   = [abs(l - c) for l, c in zip(lows[-14:], closes[-15:-1])]
    tr          = [max(hl, hc, lc) for hl, hc, lc in zip(high_low, high_close, low_close)]
    atr         = round(sum(tr) / len(tr), 2) if tr else 1.0

    # 5. 20-Day backtest reference
    backtest_idx  = min(21, len(closes))
    buy_price_20d = closes[-backtest_idx]

    # Semantic flags for LLM prompt grounding
    semantic_rsi   = "OVERSOLD (Bullish)"          if rsi < 30  else "OVERBOUGHT (Bearish)" if rsi > 70  else "NEUTRAL"
    semantic_bb    = "LOWER BAND REVERSAL (Bullish)" if percent_b < 0.1 else "UPPER BAND BREAKOUT (Bearish)" if percent_b > 0.9 else "NEUTRAL"
    semantic_trend = "STRONG UP" if price > sma_20 else "WEAK DOWN"

    return {
        "price":        price,
        "change":       change,
        "rsi":          rsi,
        "percent_b":    percent_b,
        "trend":        semantic_trend,
        "sma_20":       round(sma_20, 2),
        "rel_vol":      rel_vol,
        "volatility":   volatility,
        "atr":          atr,
        "gain_20d":     round(((price - buy_price_20d) / buy_price_20d) * 100, 2),
        "semantic_rsi": semantic_rsi,
        "semantic_bb":  semantic_bb,
    }


def get_weekly_confluence(history: list) -> str:
    """Derives weekly trend from daily bars to save API calls."""
    if len(history) < 50:
        return "UNKNOWN"
    weekly_closes = [history[i]["c"] for i in range(len(history) - 1, -1, -5)][::-1]
    if len(weekly_closes) < 10:
        return "UNKNOWN"
    sma_10 = sum(weekly_closes[-10:]) / 10
    return "UP" if weekly_closes[-1] > sma_10 else "DOWN"


# ---------------------------------------------------------------------------
# Position Sizing
# ---------------------------------------------------------------------------

def calculate_sizing(price: float, atr: float, account: float = 10_000, risk_pct: float = 0.01) -> dict:
    """
    ATR-based 1%-risk-per-trade position sizing.
    Stop = 2×ATR below entry. Target = 4×ATR above entry (4:1 R/R).
    """
    risk_dollars = account * risk_pct
    stop_dist    = 2 * atr
    shares       = round(risk_dollars / stop_dist, 2) if stop_dist > 0 else 1
    sl           = round(price - stop_dist, 2)
    tp           = round(price + (4 * atr), 2)
    return {"shares": shares, "sl": sl, "tp": tp, "amount": round(shares * price, 2)}


# ---------------------------------------------------------------------------
# Hybrid Quant Score (v2 — Refactored)
# ---------------------------------------------------------------------------

def calculate_quant_score(
    techs: dict,
    funds: dict,
    w_trend: str,
    e_risk: dict,
    st_sent: str,
    ai_score: int,
) -> int:
    """
    Computes a hybrid 100-point score combining technical signals, fundamentals, 
    social sentiment, and gated AI conviction.
    
    Args:
        techs: Dictionary of technical indicators (RSI, %B, Trend, etc.)
        funds: Dictionary of fundamental data (P/E, Growth)
        w_trend: Weekly trend direction ('UP' or 'DOWN')
        e_risk: Dictionary containing 'risk' (bool) and 'days' to next earnings
        st_sent: Social sentiment string ('Bullish', 'Bearish', 'Neutral')
        ai_score: The conviction score from the Neural Ensemble (0-100)
        
    Returns:
        int: The final hybrid conviction score (0-100)

    Point allocation (v2):
      Technicals      : 40 pts
        - Trend Confluence    : 15 pts
        - RSI Positioning     : 15 pts  (includes mean-reversion bonus)
        - Relative Volume     : 10 pts
      Fundamentals    : 30 pts
        - Revenue Growth      : 15 pts
        - Valuation (P/E)     : 15 pts  (fixed: negative P/E → 0 pts)
      Bollinger + Vol : 10 pts
        - %B Position         : 10 pts  (new)
        - Volatility Penalty  : -5 pts  (new; >5% daily range)
      Sentiment/Risk  : 10 pts
        - Social Sentiment    :  5 pts
        - Earnings Risk       : -10 pts penalty
      AI Conviction   : 10 pts (gated — only applied if quant base >= 40)
    """
    score = 0

    # ── 1. TECHNICALS (40 pts) ──────────────────────────────────────────────

    # Trend Confluence (15 pts)
    if techs["trend"] == "STRONG UP" and w_trend == "UP":
        score += 15
    elif techs["trend"] == "STRONG UP" or w_trend == "UP":
        score += 7

    # RSI Positioning (15 pts) — FIX #2: added mean-reversion zone & overbought penalty
    rsi = techs["rsi"]
    if rsi < 30:          score += 15  # Oversold / mean-reversion setup
    elif 30 <= rsi <= 65: score += 10  # Optimal entry range
    elif 65 < rsi <= 75:  score +=  5  # Extended but not extreme
    elif rsi > 75:        score -=  5  # Overbought penalty

    # Relative Volume (10 pts)
    rel_vol = techs.get("rel_vol", 0)
    if rel_vol > 1.5:   score += 10
    elif rel_vol > 1.0: score +=  5

    # ── 2. FUNDAMENTALS (30 pts) ────────────────────────────────────────────

    # Revenue Growth (15 pts)
    try:
        growth = float(funds.get("growth", 0))
        if growth > 0.3:        score += 15
        elif growth > 0.1:      score += 10
        elif growth > 0:        score +=  5
        # growth <= 0: 0 pts (declining revenue penalised by omission)
    except (TypeError, ValueError):
        pass

    # Valuation — FIX #1: negative P/E rewarded nothing; 0 < pe < 15 is "value"
    try:
        pe = float(funds.get("pe", 100))
        if 0 < pe < 15:         score += 15  # Deep value
        elif 15 <= pe < 30:     score += 10  # Reasonable
        elif 30 <= pe < 50:     score +=  5  # Extended
        # pe <= 0 or pe >= 50: 0 pts
    except (TypeError, ValueError):
        pass

    # ── 3. BOLLINGER BAND POSITION (10 pts) — FIX #3 ───────────────────────
    percent_b  = techs.get("percent_b", 0.5)
    volatility = techs.get("volatility", 0)

    if percent_b < 0.1:        score += 10  # Near lower band → mean-reversion
    elif percent_b < 0.3:      score +=  5  # Modest oversold zone
    elif percent_b > 0.9:      score -=  5  # Extended above upper band

    # Volatility Risk Penalty: daily swing > 5% is a risky environment
    if volatility > 5.0:       score -=  5

    # ── 4. SENTIMENT & RISK (10 pts) ────────────────────────────────────────
    if st_sent == "BULLISH":   score +=  5
    elif st_sent == "NEUTRAL": score +=  2

    if e_risk.get("risk"):     score -= 10  # Earnings within 5 days: high risk

    # ── 5. GATED AI CONVICTION (10 pts) — FIX #4 ───────────────────────────
    # AI contribution is only applied when the objective quant base (everything above)
    # already passes a minimum quality threshold of 40 pts.
    # Prevents hallucinated AI scores from rescuing fundamentally weak setups.
    quant_base = score  # capture score before AI addition
    if quant_base >= 40:
        score += round((ai_score / 100) * 10)

    return max(0, min(100, round(score)))
