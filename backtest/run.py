"""
backtest/run.py — CLI runner for a backtest.

  python -m backtest.run --tickers AAPL,NVDA,TSLA,AMD,SPY,QQQ,META,MSFT,AMZN,GOOG \
                        --start 2026-06-08 --end 2026-06-12

Caches minute bars in nuroq.db so repeat runs are instant (no re-fetch).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

# Allow running as a script from repo root: python -m backtest.run
sys.path.insert(0, ".")

from backtest.fill_model import SimulatedFillModel
from backtest.metrics import compute, print_report
from backtest.replay import HistoricalBarSource, Simulator
from day_trader import ORB5Params, ORB5Strategy
from minute_bars import get_minute_bars_range


class _StdoutLogger:
    def log(self, msg, level="INFO"):
        print(f"[{level}] {msg}")


def main():
    p = argparse.ArgumentParser(description="Day-trader backtest runner")
    p.add_argument("--tickers", default="AAPL,NVDA,TSLA,AMD,SPY,QQQ,META,MSFT,AMZN,GOOG")
    p.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--volume-mult", type=float, default=2.0,
                   help="ORB5 breakout volume multiplier (vs ORB avg)")
    p.add_argument("--require-vwap", action="store_true", default=False,
                   help="Require breakout close > VWAP")
    p.add_argument("--time-stop", type=int, default=30,
                   help="Bars to hold without target before market exit")
    p.add_argument("--target-r", type=float, default=2.0,
                   help="Target = entry + this many R")
    p.add_argument("--quiet-fetch", action="store_true", default=False)
    args = p.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    logger = None if args.quiet_fetch else _StdoutLogger()

    # Phase 1 — fetch (cache-aware)
    print(f"\n📥 Fetching bars for {len(tickers)} tickers, {args.start} → {args.end}…")
    sessions: list[tuple[str, str, list]] = []
    per_ticker_loaded: dict[str, int] = {}
    for t in tickers:
        by_date = get_minute_bars_range(t, args.start, args.end, logger=logger)
        per_ticker_loaded[t] = sum(len(b) for b in by_date.values())
        for d, bars in by_date.items():
            sessions.append((t, d, bars))
    print(f"  loaded {sum(per_ticker_loaded.values()):,} bars across "
          f"{len(sessions)} (ticker, day) sessions")
    for t, n in per_ticker_loaded.items():
        print(f"    {t:6s}  {n:>6,} bars")

    if not sessions:
        print("⚠️ no bars loaded — check Polygon key + date range")
        return 1

    # Phase 2 — backtest
    params = ORB5Params(
        volume_multiplier=args.volume_mult,
        require_vwap_align=args.require_vwap,
        time_stop_bars=args.time_stop,
        target_R_multiple=args.target_r,
    )
    strat = ORB5Strategy(params)
    sim = Simulator(SimulatedFillModel(slippage_bps=args.slippage_bps))
    print(f"\n🧪 Running ORB-5 backtest "
          f"(vol_mult={args.volume_mult}, vwap_req={args.require_vwap}, "
          f"time_stop={args.time_stop}, target_R={args.target_r}, "
          f"slip={args.slippage_bps}bps)…")
    log = sim.run(strat, sessions=sessions)
    print(f"  sessions processed: {log.sessions_processed}  "
          f"(skipped {log.sessions_skipped})  fills_attempted: {log.fills_attempted}")

    # Phase 3 — metrics
    print()
    m = compute(log.trades)
    print_report(m, title=f"ORB-5 backtest · {args.start} → {args.end} "
                          f"· {len(tickers)} tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
