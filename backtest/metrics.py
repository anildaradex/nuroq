"""
backtest/metrics.py — aggregate stats over a TradeLog.

Print a quick summary table or get a structured dict for the React UI.
Everything is dependency-light (stdlib only — no pandas in this file) so it
works in the live agent too if you want intraday "running expectancy".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

from backtest.fill_model import TradeRecord


@dataclass
class Metrics:
    trades: int = 0
    winners: int = 0
    losers: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    max_consec_losers: int = 0
    max_drawdown: float = 0.0
    avg_bars_held: float = 0.0
    by_setup: dict = None
    by_hour: dict = None
    by_ticker: dict = None
    by_exit_reason: dict = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def compute(trades: Sequence[TradeRecord]) -> Metrics:
    m = Metrics(by_setup={}, by_hour={}, by_ticker={}, by_exit_reason={})
    m.trades = len(trades)
    if not trades:
        return m

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    flats = [t for t in trades if t.pnl == 0]
    m.winners = len(wins)
    m.losers = len(losses)
    m.breakeven = len(flats)
    m.win_rate = round(m.winners / m.trades * 100.0, 2)
    m.total_pnl = round(sum(t.pnl for t in trades), 2)
    m.avg_win = round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0.0
    m.avg_loss = round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0.0
    # Expectancy per trade (dollars)
    m.expectancy = round(
        (m.winners / m.trades) * m.avg_win +
        (m.losers / m.trades) * m.avg_loss, 2
    )
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    m.profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0.0

    # Max consecutive losers
    consec = 0
    longest = 0
    for t in trades:
        if t.pnl < 0:
            consec += 1
            longest = max(longest, consec)
        else:
            consec = 0
    m.max_consec_losers = longest

    # Max drawdown (running equity curve from total_pnl sequence)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
    m.max_drawdown = round(max_dd, 2)

    m.avg_bars_held = round(
        sum(t.bars_held for t in trades) / m.trades, 1
    )

    # Breakdowns -----------------------------------------------------------
    def _bucket_stats(group: list[TradeRecord]) -> dict:
        if not group:
            return {"n": 0, "pnl": 0.0, "win_rate": 0.0, "expectancy": 0.0}
        n = len(group)
        wins_g = [t for t in group if t.pnl > 0]
        losses_g = [t for t in group if t.pnl < 0]
        wr = round(len(wins_g) / n * 100.0, 2)
        avg_w = sum(t.pnl for t in wins_g) / len(wins_g) if wins_g else 0.0
        avg_l = sum(t.pnl for t in losses_g) / len(losses_g) if losses_g else 0.0
        exp = round((len(wins_g)/n) * avg_w + (len(losses_g)/n) * avg_l, 2)
        return {
            "n": n,
            "pnl": round(sum(t.pnl for t in group), 2),
            "win_rate": wr,
            "expectancy": exp,
        }

    by_setup = defaultdict(list)
    by_hour = defaultdict(list)
    by_ticker = defaultdict(list)
    by_exit = defaultdict(list)
    for t in trades:
        by_setup[t.setup_id].append(t)
        by_ticker[t.ticker].append(t)
        by_exit[t.exit_reason].append(t)
        # entry hour, ET
        if _ET is not None:
            hour = datetime.fromtimestamp(t.entry_ts, tz=_ET).hour
        else:
            hour = datetime.fromtimestamp(t.entry_ts).hour
        by_hour[hour].append(t)
    m.by_setup = {k: _bucket_stats(v) for k, v in by_setup.items()}
    m.by_hour = {str(k): _bucket_stats(v) for k, v in sorted(by_hour.items())}
    m.by_ticker = {k: _bucket_stats(v) for k, v in by_ticker.items()}
    m.by_exit_reason = {k: _bucket_stats(v) for k, v in by_exit.items()}
    return m


def print_report(m: Metrics, title: str = "Backtest results") -> str:
    """Render a stdout-friendly summary block. Returns the rendered string."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(title)
    lines.append("=" * 70)
    lines.append(f"  Trades:           {m.trades}")
    lines.append(f"  Winners / Losers: {m.winners} / {m.losers}  "
                 f"(WR {m.win_rate}%)")
    lines.append(f"  Total P&L:        ${m.total_pnl:,.2f}")
    lines.append(f"  Avg winner:       ${m.avg_win:,.2f}")
    lines.append(f"  Avg loser:        ${m.avg_loss:,.2f}")
    lines.append(f"  Expectancy/trade: ${m.expectancy:,.2f}")
    lines.append(f"  Profit factor:    {m.profit_factor}")
    lines.append(f"  Max consec losses:{m.max_consec_losers}")
    lines.append(f"  Max drawdown:     ${m.max_drawdown:,.2f}")
    lines.append(f"  Avg bars held:    {m.avg_bars_held}")
    if m.by_setup:
        lines.append("")
        lines.append("  By setup:")
        for k, v in m.by_setup.items():
            lines.append(f"    {k:10s}  n={v['n']:3d}  pnl=${v['pnl']:>8,.2f}"
                         f"  WR={v['win_rate']:5.1f}%  E=${v['expectancy']:>6.2f}")
    if m.by_exit_reason:
        lines.append("")
        lines.append("  By exit reason:")
        for k, v in m.by_exit_reason.items():
            lines.append(f"    {k:10s}  n={v['n']:3d}  pnl=${v['pnl']:>8,.2f}"
                         f"  WR={v['win_rate']:5.1f}%")
    if m.by_hour:
        lines.append("")
        lines.append("  By entry hour (ET):")
        for k, v in m.by_hour.items():
            lines.append(f"    {k:>2}:00      n={v['n']:3d}  pnl=${v['pnl']:>8,.2f}"
                         f"  WR={v['win_rate']:5.1f}%")
    if m.by_ticker and len(m.by_ticker) <= 30:
        lines.append("")
        lines.append("  By ticker:")
        for k, v in sorted(m.by_ticker.items(),
                           key=lambda kv: -kv[1]['pnl']):
            lines.append(f"    {k:8s}    n={v['n']:3d}  pnl=${v['pnl']:>8,.2f}"
                         f"  WR={v['win_rate']:5.1f}%")
    lines.append("=" * 70)
    out = "\n".join(lines)
    print(out)
    return out
