"""
risk_manager.py — gatekeeper for every autonomous trade attempt.

The live agent calls `can_enter_trade()` BEFORE submitting a bracket order.
Returns (ok, reason, sizing) — if `ok` is False, the agent skips the entry
and the reason gets logged. This module is the central place where all the
safety guardrails live, so they can't be accidentally bypassed by a code
path that forgets one check.

Guardrails (top → bottom is enforcement order):
  1. AUTO_TRADE flag is on                  → else "auto disabled"
  2. Not currently halted                   → else "halted: <reason>"
  3. US market is open right now             → else "market closed"
  4. Inside the entry window                 → else "outside entry window"
  5. Today's P&L still within loss limit     → else circuit-breaker triggers
  6. Open-position count under cap           → else "concurrency cap"
  7. Margin policy respected                 → else "would use margin"
  8. Per-trade $-risk under cap              → else position sized smaller
  9. Enough cash to actually buy the shares  → else "insufficient cash"

`update_after_close(pnl)` is called when a position closes — accumulates
into today's realized P&L, and trips the circuit if loss limit hit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional, Tuple

import agent_config


@dataclass
class Sizing:
    shares: int
    sl: float                  # stop-loss price
    tp: float                  # take-profit price
    position_value: float      # shares * entry
    risk_dollars: float        # shares * (entry - sl)


@dataclass
class Decision:
    ok: bool
    reason: str                # short, machine-readable
    sizing: Optional[Sizing]


def _now_et() -> datetime:
    """Wall time in US/Eastern. Falls back to UTC offset if tz lib missing."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Best-effort fallback: assume the host is on ET. (Production VM is
        # us-central, but the launchd timer normalizes to America/New_York.)
        return datetime.now()


def _market_open(now: datetime) -> bool:
    """True for regular US equities cash session (9:30–16:00 ET, weekdays)."""
    if now.weekday() >= 5:   # Sat/Sun
        return False
    t = now.time()
    return dtime(9, 30) <= t < dtime(16, 0)


def _parse_hhmm(s: str, default: dtime) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


def _in_entry_window(now: datetime, cfg: dict) -> bool:
    start = _parse_hhmm(cfg["entry_window_start"], dtime(9, 35))
    end = _parse_hhmm(cfg["entry_window_end"], dtime(15, 0))
    t = now.time()
    return start <= t < end


def can_enter_trade(symbol: str, entry: float, atr: float,
                    open_positions: int, cash: float,
                    todays_pl: float, equity: float,
                    on_margin: bool) -> Decision:
    """Decide whether the agent may enter a new long position right now.

    `open_positions`  — count of CURRENT held positions on Alpaca
    `cash`            — available cash buying power (NOT margin buying power)
    `todays_pl`       — realized + unrealized P&L today, dollars
    `equity`          — account equity (for daily-loss-pct calc)
    `on_margin`       — True if account is currently using margin
    """
    cfg = agent_config.get()
    symbol = (symbol or "").upper()

    # 1. Halt flag (manual or circuit-breaker tripped). Check FIRST so a halt
    # surfaces its real reason — halt() also disables auto, which would
    # otherwise trip the next branch with a less-useful message.
    if cfg.get("halted_at"):
        return Decision(False, f"halted: {cfg.get('halt_reason') or 'manual'}", None)

    # 2. AUTO flag
    if not cfg.get("auto_trade_enabled"):
        return Decision(False, "auto_trade disabled", None)

    # 3. Market open
    now = _now_et()
    if not _market_open(now):
        return Decision(False, "market closed", None)

    # 4. Entry window (avoids opening 5-min noise + last hour)
    if not _in_entry_window(now, cfg):
        return Decision(False,
                        f"outside entry window {cfg['entry_window_start']}–{cfg['entry_window_end']} ET",
                        None)

    # 5. Daily loss limit — if today's bleed past the threshold, halt the day.
    if equity > 0:
        loss_pct = -todays_pl / equity * 100 if todays_pl < 0 else 0
        if loss_pct >= cfg["daily_loss_limit_pct"]:
            agent_config.halt(
                f"daily loss limit hit: {loss_pct:.2f}% of equity "
                f"(limit {cfg['daily_loss_limit_pct']:.2f}%)"
            )
            return Decision(False, "daily loss limit", None)

    # 6. Concurrency cap
    if open_positions >= cfg["max_concurrent"]:
        return Decision(False,
                        f"concurrency cap: {open_positions}/{cfg['max_concurrent']}",
                        None)

    # 7. Margin policy. If margin is disabled and the account is already on
    # margin (cash < 0), refuse new entries — would dig deeper into debt.
    if not cfg["margin_allowed"] and (on_margin or cash <= 0):
        return Decision(False, "margin disabled (cash <= 0)", None)

    # 8 + 9. Sizing — risk-based, then cash-capped.
    # SL = entry − 2·ATR (Wilder default); TP = entry + 4·ATR (R:R = 2:1).
    if entry <= 0 or atr <= 0:
        return Decision(False, "invalid entry/atr", None)
    sl = max(entry - 2 * atr, 0.01)
    tp = entry + 4 * atr
    per_share_risk = entry - sl
    if per_share_risk <= 0:
        return Decision(False, "non-positive per-share risk", None)

    # Per-trade risk budget = risk_per_trade_pct of CONFIGURED budget
    # (not equity — the user said "fixed budget", so the budget is the basis).
    risk_budget = cfg["budget"] * cfg["risk_per_trade_pct"] / 100.0
    shares_by_risk = int(risk_budget // per_share_risk)
    # Position value cap: never put more than budget / max_concurrent into one
    # trade (so a single name can't dominate the book).
    per_position_cap = cfg["budget"] / max(1, cfg["max_concurrent"])
    shares_by_size = int(per_position_cap // entry)
    # Cash cap: don't spend more than available cash.
    shares_by_cash = int(max(0, cash) // entry) if not cfg["margin_allowed"] else shares_by_size

    shares = max(0, min(shares_by_risk, shares_by_size, shares_by_cash))
    if shares < 1:
        return Decision(False,
                        f"sized to 0 (risk_budget=${risk_budget:.0f}, "
                        f"per_share_risk=${per_share_risk:.2f}, cash=${cash:.0f})",
                        None)

    return Decision(True, "ok", Sizing(
        shares=shares, sl=round(sl, 2), tp=round(tp, 2),
        position_value=round(shares * entry, 2),
        risk_dollars=round(shares * per_share_risk, 2),
    ))


def trip_circuit_if_loss_exceeded(todays_pl: float, equity: float) -> Tuple[bool, str]:
    """Standalone hook the EOD daemon / monitor can call to enforce the
    daily-loss circuit even when no entry is being attempted. Returns
    (tripped, reason). Idempotent."""
    cfg = agent_config.get()
    if cfg.get("halted_at"):
        return (True, cfg.get("halt_reason") or "already halted")
    if equity <= 0 or todays_pl >= 0:
        return (False, "")
    loss_pct = -todays_pl / equity * 100
    if loss_pct >= cfg["daily_loss_limit_pct"]:
        reason = (f"daily loss limit hit: {loss_pct:.2f}% of equity "
                  f"(limit {cfg['daily_loss_limit_pct']:.2f}%)")
        agent_config.halt(reason)
        return (True, reason)
    return (False, "")
