"""
create_dpo_training_json.py — DPO preference-pair generator (RLFF)

Builds (prompt, chosen, rejected) triples from real OHLCV history and, optionally,
the live signal log in nuroq.db. Labels are derived from forward-return outcomes,
not random numbers, and the prompt format matches dashboard.py:568-581 so the
trained adapter actually shifts inference-time behavior.

Outputs: dpo_data/train.jsonl, dpo_data/valid.jsonl (90/10 holdout, no leakage).
"""

import argparse
import json
import os
import random
import sqlite3
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv

from scoring import calculate_technicals

load_dotenv()
DB_PATH = os.getenv("NUROQ_DB_PATH", "nuroq.db")


# ---------------------------------------------------------------------------
# Prompt builder — MUST match dashboard.py analyze_single_ticker_data
# ---------------------------------------------------------------------------

def build_prompt(ticker: str, techs: dict, pe="N/A", growth="N/A", news="N/A", mem_ctx="N/A") -> str:
    return (
        "### Instruction: Act as a Hedge Fund Analyst. Analyze using Technicals and Fundamentals.\n"
        f"    1. Technicals: {techs['trend']} Trend, RSI Flag: {techs.get('semantic_rsi', 'NEUTRAL')}, "
        f"Bollinger Flag: {techs.get('semantic_bb', 'NEUTRAL')}\n"
        f"    2. Fundamentals: PE Ratio {pe}, Rev Growth {growth}.\n"
        f"    3. Context: {news}\n"
        f"    4. Memory: {mem_ctx}\n\n"
        "    Provide your analysis as a CONCISE JSON object with the following fields:\n"
        '    - "reasoning": "A concise explanation of your findings"\n'
        '    - "considerations": ["Factor 1", "Factor 2", "Factor 3"]\n'
        '    - "rating": "BUY/SELL/HOLD"\n'
        '    - "score": 0-100 (Conviction score)\n\n'
        "    Ensure the response is valid JSON only. Do not repeat the output."
        f" ### Input: Ticker: {ticker}, Close: ${techs['price']} ### Response:"
    )


def build_response(ticker: str, techs: dict, rating: str, score: int) -> str:
    """JSON response matching the format dashboard.get_structured_data parses."""
    if rating == "BUY":
        reasoning = (
            f"{ticker} shows a {techs['trend'].lower()} setup at ${techs['price']}. "
            f"RSI of {techs['rsi']} ({techs.get('semantic_rsi', 'NEUTRAL')}) and "
            f"%B of {techs['percent_b']} ({techs.get('semantic_bb', 'NEUTRAL')}) "
            f"support a long entry. Relative volume {techs['rel_vol']}x confirms participation."
        )
        considerations = [
            f"Trend: {techs['trend']}",
            f"RSI: {techs['rsi']} ({techs.get('semantic_rsi', 'NEUTRAL')})",
            f"Rel Vol: {techs['rel_vol']}x",
        ]
    elif rating == "SELL":
        reasoning = (
            f"{ticker} at ${techs['price']} is rejecting at the upper band with %B {techs['percent_b']} "
            f"and RSI {techs['rsi']} ({techs.get('semantic_rsi', 'NEUTRAL')}). "
            f"Trend reads {techs['trend'].lower()}; risk/reward favors exit or short."
        )
        considerations = [
            f"Trend: {techs['trend']}",
            f"%B: {techs['percent_b']} ({techs.get('semantic_bb', 'NEUTRAL')})",
            f"RSI: {techs['rsi']}",
        ]
    else:  # HOLD
        reasoning = (
            f"{ticker} at ${techs['price']} sits mid-range with RSI {techs['rsi']} and "
            f"%B {techs['percent_b']}. Neither breakout nor breakdown confirmed; wait for clearer signal."
        )
        considerations = [
            f"Trend: {techs['trend']}",
            f"RSI: {techs['rsi']}",
            f"%B: {techs['percent_b']}",
        ]
    return json.dumps({
        "reasoning": reasoning,
        "considerations": considerations,
        "rating": rating,
        "score": int(score),
    })


# ---------------------------------------------------------------------------
# OHLCV loader — reads directly from nuroq.db (no Polygon hits)
# ---------------------------------------------------------------------------

def load_bars_by_ticker(db_path: str, min_bars: int) -> dict:
    """Returns {ticker: [bar_dicts_sorted_by_date]} for tickers with ≥ min_bars."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ticker, date, open, high, low, close, volume "
            "FROM price_history ORDER BY ticker, date ASC"
        ).fetchall()
    by_ticker = defaultdict(list)
    for tk, d, o, h, l, c, v in rows:
        by_ticker[tk].append({"t": d, "o": o, "h": h, "l": l, "c": c, "v": v})
    return {tk: bars for tk, bars in by_ticker.items() if len(bars) >= min_bars}


# ---------------------------------------------------------------------------
# Historical preference-pair generation (forward-return labels)
# ---------------------------------------------------------------------------

def generate_historical_pairs(
    bars_by_ticker: dict,
    horizon: int,
    threshold: float,
    max_per_ticker: int,
    lookback_min: int,
    rng: random.Random,
) -> list:
    pairs = []
    for ticker, bars in bars_by_ticker.items():
        eligible_indices = list(range(lookback_min, len(bars) - horizon))
        if not eligible_indices:
            continue
        rng.shuffle(eligible_indices)
        per_ticker = 0
        for i in eligible_indices:
            if per_ticker >= max_per_ticker:
                break
            techs = calculate_technicals(bars[: i + 1])
            if not techs:
                continue
            price_now = bars[i]["c"]
            price_fwd = bars[i + horizon]["c"]
            if price_now <= 0:
                continue
            ret = (price_fwd - price_now) / price_now

            if ret > threshold:
                winning, losing = "BUY", "SELL"
                win_score = rng.randint(78, 92)
                lose_score = rng.randint(15, 35)
            elif ret < -threshold:
                winning, losing = "SELL", "BUY"
                win_score = rng.randint(15, 35)
                lose_score = rng.randint(78, 92)
            else:
                continue  # No clear preference signal

            pairs.append({
                "prompt":   build_prompt(ticker, techs),
                "chosen":   build_response(ticker, techs, winning, win_score),
                "rejected": build_response(ticker, techs, losing, lose_score),
                "_meta": {"ticker": ticker, "as_of": bars[i]["t"], "fwd_return": round(ret, 4)},
            })
            per_ticker += 1
    return pairs


# ---------------------------------------------------------------------------
# RLFF augmentation — preference pairs from logged signals + realized outcomes
# ---------------------------------------------------------------------------

def generate_rlff_pairs(
    db_path: str,
    bars_by_ticker: dict,
    horizon: int,
    threshold: float,
) -> list:
    """For every logged signal where horizon trading days have elapsed, label by realized return."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ticker, timestamp, price, signal, ai_score "
            "FROM all_signals ORDER BY timestamp ASC"
        ).fetchall()

    pairs = []
    for ticker, ts, price, signal, ai_score in rows:
        bars = bars_by_ticker.get(ticker)
        if not bars:
            continue
        signal_date = ts[:10]  # 'YYYY-MM-DD HH:MM' → 'YYYY-MM-DD'
        idx = next((i for i, b in enumerate(bars) if b["t"] >= signal_date), None)
        if idx is None or idx + horizon >= len(bars):
            continue
        techs = calculate_technicals(bars[: idx + 1])
        if not techs:
            continue
        ret = (bars[idx + horizon]["c"] - bars[idx]["c"]) / bars[idx]["c"]

        if ret > threshold:
            winning, losing = "BUY", "SELL"
        elif ret < -threshold:
            winning, losing = "SELL", "BUY"
        else:
            continue

        pairs.append({
            "prompt":   build_prompt(ticker, techs),
            "chosen":   build_response(ticker, techs, winning, 85),
            "rejected": build_response(ticker, techs, losing, 25),
            "_meta": {"ticker": ticker, "logged_signal": signal, "as_of": signal_date,
                      "fwd_return": round(ret, 4), "source": "rlff"},
        })
    return pairs


# ---------------------------------------------------------------------------
# Train/valid split — proper holdout, no leakage
# ---------------------------------------------------------------------------

def write_split(pairs: list, out_dir: str, valid_frac: float, rng: random.Random) -> tuple:
    os.makedirs(out_dir, exist_ok=True)
    # Ticker-level holdout to prevent same-ticker leakage across train/valid
    tickers = sorted({p["_meta"]["ticker"] for p in pairs})
    rng.shuffle(tickers)
    n_valid = max(1, int(len(tickers) * valid_frac))
    valid_tickers = set(tickers[:n_valid])

    train = [p for p in pairs if p["_meta"]["ticker"] not in valid_tickers]
    valid = [p for p in pairs if p["_meta"]["ticker"] in valid_tickers]

    def _emit(items, path):
        with open(path, "w") as f:
            for it in items:
                row = {k: v for k, v in it.items() if not k.startswith("_")}
                f.write(json.dumps(row) + "\n")

    _emit(train, os.path.join(out_dir, "train.jsonl"))
    _emit(valid, os.path.join(out_dir, "valid.jsonl"))
    return len(train), len(valid), len(valid_tickers)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH, help="Path to nuroq.db")
    parser.add_argument("--output-dir", default="dpo_data", help="Where to write train/valid jsonl")
    parser.add_argument("--horizon", type=int, default=20, help="Forward-return horizon in trading days")
    parser.add_argument("--threshold", type=float, default=0.05,
                        help="|return| above this counts as a clear winner/loser (default 5%)")
    parser.add_argument("--max-per-ticker", type=int, default=20,
                        help="Cap historical pairs per ticker to avoid one-ticker dominance")
    parser.add_argument("--lookback-min", type=int, default=25,
                        help="Min bars before as-of date so technicals are well-defined")
    parser.add_argument("--rlff-only", action="store_true",
                        help="Use only logged signals from all_signals (smaller, may have no BUYs)")
    parser.add_argument("--no-rlff", action="store_true",
                        help="Skip RLFF augmentation; historical OHLCV only")
    parser.add_argument("--valid-frac", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"❌ {args.db} not found — run the dashboard at least once to populate price_history.")

    rng = random.Random(args.seed)
    min_bars = args.lookback_min + args.horizon + 5
    bars_by_ticker = load_bars_by_ticker(args.db, min_bars=min_bars)
    print(f"📊 Loaded {len(bars_by_ticker)} tickers with ≥{min_bars} bars")

    pairs = []
    if not args.rlff_only:
        hist = generate_historical_pairs(
            bars_by_ticker, args.horizon, args.threshold,
            args.max_per_ticker, args.lookback_min, rng,
        )
        print(f"🧪 Historical pairs: {len(hist)}")
        pairs.extend(hist)

    if not args.no_rlff:
        rlff = generate_rlff_pairs(args.db, bars_by_ticker, args.horizon, args.threshold)
        print(f"🎯 RLFF pairs from all_signals: {len(rlff)}")
        pairs.extend(rlff)

    if not pairs:
        raise SystemExit("❌ No preference pairs generated — try lowering --threshold or --horizon.")

    rng.shuffle(pairs)
    n_train, n_valid, n_valid_tickers = write_split(pairs, args.output_dir, args.valid_frac, rng)
    print(f"✅ Wrote {n_train} train + {n_valid} valid pairs to {args.output_dir}/ "
          f"(holdout: {n_valid_tickers} tickers)")


if __name__ == "__main__":
    main()
