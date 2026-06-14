"""
premarket_scanner.py — build today's Day Trade Watchlist (DTW).

For each candidate ticker, compute a Gap Momentum Score from premarket bars
and news catalyst, then write the top N tickers to agent_config.dt_universe
so the live DayTraderEngine only considers them.

  GMS = gap_pct × log10(premarket_volume) × catalyst_weight

Where catalyst_weight is:
  POSITIVE_BOOST → 1.5
  NEUTRAL_NEWS / no news → 1.0
  NEGATIVE_WARNING → 0.5    (allow, but downrank — pullback risk)
  NEGATIVE_BLOCK   → ticker dropped entirely

The scan reads:
  • watchlist_today (the swing research cycle's morning output) — default seed
  • get_full_history(ticker) for prev close
  • get_minute_bars(ticker, today) for premarket aggregates
  • check_news_for_crossing(ticker) for catalyst

The result is one row per kept ticker plus the comma-joined universe string
that gets written to agent_config.dt_universe.

Run order in the morning:
  03:30 ET — research cycle populates watchlist_today
  08:00 ET — premarket_scanner.build_dt_universe() runs (this module)
  09:30 ET — DayTraderEngine starts evaluating intraday on the picked tickers
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

import agent_config
from intraday_indicators import gap_pct, premarket_stats
from minute_bars import get_minute_bars


# ---------------------------------------------------------------------------
# Tunables — exposed as constants so a future PR can promote them to
# agent_config without breaking callers that pass explicit kwargs.
# ---------------------------------------------------------------------------

DEFAULT_TOP_N             = 10
DEFAULT_MIN_GAP_PCT       = 4.0      # gap must be ≥ 4% (long bias)
DEFAULT_MIN_PREMKT_VOL    = 50_000   # filters dead names
DEFAULT_MAX_SCAN_TICKERS  = 50       # Polygon rate-limit guard
DEFAULT_MIN_PRICE         = 1.0
DEFAULT_MAX_PRICE         = 500.0    # paper-trading sanity ceiling


@dataclass(frozen=True)
class ScanRow:
    ticker: str
    prev_close: float
    last_premkt_price: float
    gap_pct: float
    premkt_volume: float
    catalyst: str           # POSITIVE_BOOST | NEUTRAL_NEWS | NEGATIVE_WARNING
    catalyst_weight: float
    gms: float              # Gap Momentum Score
    headline: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "prev_close": round(self.prev_close, 4),
            "last_premkt_price": round(self.last_premkt_price, 4),
            "gap_pct": round(self.gap_pct, 2),
            "premkt_volume": int(self.premkt_volume),
            "catalyst": self.catalyst,
            "catalyst_weight": self.catalyst_weight,
            "gms": round(self.gms, 2),
            "headline": self.headline,
        }


def _today_et_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return date.today().strftime("%Y-%m-%d")


def _catalyst_weight(classification: Optional[str]) -> tuple[float, bool]:
    """(weight, drop?). drop=True means the ticker is removed entirely."""
    if not classification:
        return (1.0, False)
    c = classification.upper()
    if c == "POSITIVE_BOOST":
        return (1.5, False)
    if c == "NEUTRAL_NEWS" or c == "NEUTRAL":
        return (1.0, False)
    if c == "NEGATIVE_WARNING":
        return (0.5, False)
    if c == "NEGATIVE_BLOCK":
        return (0.0, True)
    return (1.0, False)


def _candidates_from_watchlist(logger=None) -> list[str]:
    """Default candidate set = today's swing watchlist + Alpaca holdings."""
    cands: list[str] = []
    try:
        from data_fetcher import watchlist_today
        rows = watchlist_today.get_all() or []
        cands.extend([r["ticker"] for r in rows if r.get("ticker")])
    except Exception as e:
        if logger:
            logger.log(f"⚠️ scanner: watchlist_today read failed: {e}",
                       level="WARNING")
    # Also pull held positions (so DT can manage / day-trade names you already own).
    try:
        from dashboard import alpaca_api
        held = alpaca_api.list_position_symbols() or set()
        cands.extend(sorted(held))
    except Exception:
        pass
    # Dedupe preserving order.
    seen = set()
    out: list[str] = []
    for t in cands:
        u = t.upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def build_dt_universe(*, top_n: int = DEFAULT_TOP_N,
                      min_gap_pct: float = DEFAULT_MIN_GAP_PCT,
                      min_premkt_volume: float = DEFAULT_MIN_PREMKT_VOL,
                      min_price: float = DEFAULT_MIN_PRICE,
                      max_price: float = DEFAULT_MAX_PRICE,
                      max_scan_tickers: int = DEFAULT_MAX_SCAN_TICKERS,
                      candidates: Optional[Sequence[str]] = None,
                      session_date: Optional[str] = None,
                      write_config: bool = True,
                      logger=None) -> dict:
    """Run the scan; return a structured result dict.

    Result shape:
      {
        "session_date": "YYYY-MM-DD",
        "scanned": <count>,
        "kept": <count>,
        "rows": [ScanRow dicts ordered by GMS desc],
        "universe": "AAPL,NVDA,..."   # comma-joined top N tickers, written to config
        "filters": { reason: count }   # how many got dropped by each gate
      }
    """
    sess = session_date or _today_et_str()
    if not candidates:
        candidates = _candidates_from_watchlist(logger=logger)
    candidates = [c.upper() for c in (candidates or [])]
    if max_scan_tickers and len(candidates) > max_scan_tickers:
        candidates = candidates[:max_scan_tickers]
    if logger:
        logger.log(f"📡 premarket scanner: {len(candidates)} candidates for {sess}")

    rows: list[ScanRow] = []
    filters = {
        "no_prev_close": 0,
        "no_premarket_bars": 0,
        "premkt_volume": 0,
        "price_band": 0,
        "gap_too_small": 0,
        "negative_block": 0,
    }
    for ticker in candidates:
        try:
            row = _scan_one(ticker, sess, min_gap_pct, min_premkt_volume,
                            min_price, max_price, filters, logger=logger)
            if row is not None:
                rows.append(row)
        except Exception as e:
            if logger:
                logger.log(f"⚠️ scanner: {ticker} threw: {e}", level="WARNING")

    rows.sort(key=lambda r: -r.gms)
    top = rows[:top_n]
    universe = ",".join(r.ticker for r in top)

    if write_config:
        try:
            agent_config.update(dt_universe=universe)
            if logger:
                logger.log(f"📡 premarket scanner: wrote dt_universe="
                           f"\"{universe}\" ({len(top)} tickers)")
        except Exception as e:
            if logger:
                logger.log(f"⚠️ scanner: dt_universe write failed: {e}",
                           level="WARNING")

    return {
        "session_date": sess,
        "scanned": len(candidates),
        "kept": len(top),
        "rows": [r.to_dict() for r in top],
        "all_rows_count": len(rows),
        "universe": universe,
        "filters": filters,
    }


def _scan_one(ticker: str, session_date: str,
              min_gap_pct: float, min_premkt_volume: float,
              min_price: float, max_price: float,
              filters: dict, logger=None) -> Optional[ScanRow]:
    # Prev close — from the daily history cache (research cycle already filled it).
    try:
        from data_fetcher import get_full_history
        hist = get_full_history(ticker, logger=logger) or []
    except Exception:
        hist = []
    if len(hist) < 2:
        filters["no_prev_close"] += 1
        return None
    prev_close = float(hist[-2].get("c") or 0)            # second-to-last is "yesterday"
    if prev_close <= 0:
        prev_close = float(hist[-1].get("c") or 0)
        if prev_close <= 0:
            filters["no_prev_close"] += 1
            return None

    # Today's premarket bars.
    bars = get_minute_bars(ticker, session_date, include_premarket=True,
                           logger=logger)
    if not bars:
        filters["no_premarket_bars"] += 1
        return None
    pm = premarket_stats(bars)
    if pm is None:
        filters["no_premarket_bars"] += 1
        return None
    if pm.volume < min_premkt_volume:
        filters["premkt_volume"] += 1
        return None
    if not (min_price <= pm.last_price <= max_price):
        filters["price_band"] += 1
        return None

    g = gap_pct(prev_close, pm.last_price)
    if g is None or g < min_gap_pct:
        filters["gap_too_small"] += 1
        return None

    # News catalyst — same path the swing live agent uses.
    catalyst_name = "NONE"
    catalyst_weight = 1.0
    headline = ""
    try:
        from news_engine import check_news_for_crossing
        v = check_news_for_crossing(ticker)
        if v and v.get("classification"):
            catalyst_name = str(v["classification"]).upper()
            headline = (v.get("headline") or "")[:200]
    except Exception:
        pass
    catalyst_weight, drop = _catalyst_weight(catalyst_name)
    if drop:
        filters["negative_block"] += 1
        return None

    gms = g * math.log10(max(pm.volume, 10)) * catalyst_weight
    return ScanRow(
        ticker=ticker, prev_close=prev_close, last_premkt_price=pm.last_price,
        gap_pct=g, premkt_volume=pm.volume,
        catalyst=catalyst_name, catalyst_weight=catalyst_weight,
        gms=gms, headline=headline,
    )
