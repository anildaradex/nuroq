#!/usr/bin/env python3
"""
premarket_refresh.py — Tier 2 pre-market warm-up (Phase 2.5).

Lighter than the overnight research cycle: doesn't re-run LLM analysis. Instead:

  1. Reads today's watchlist (written by the 02:00 cron research_cycle.py)
  2. Pulls fresh Polygon snapshot for watchlist tickers (premarket prices)
  3. Updates the price + change_pct columns in watchlist_today
  4. Pulls fresh news for watchlist tickers (last 16h) and writes to news_cache
  5. Exits

Designed to run at 09:15 ET, between the overnight research cycle and market open.

Usage:
  python premarket_refresh.py                # default, with Telegram updates
  python premarket_refresh.py --no-telegram  # silent
  python premarket_refresh.py --dry-run      # parse + log plan, exit

Suggested cron / launchd: see SCHEDULING.md.
"""

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Optional

# Same gate-flip pattern as research_cycle.py — disable polling Telegram bot +
# 60s monitor so we don't conflict with a running dashboard.
os.environ["NUROQ_BACKGROUND_SERVICES"] = "0"


def _send_one_shot_telegram(message: str, token: str, chat_id: str) -> None:
    try:
        from telegram import Bot

        async def _send():
            bot = Bot(token)
            await bot.send_message(chat_id=chat_id, text=message)
        asyncio.run(_send())
    except Exception as e:
        print(f"[premarket_refresh] ⚠️ Telegram notify failed: {e}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-telegram", action="store_true",
                   help="Suppress Telegram start/done notifications")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse args, log the plan, exit")
    p.add_argument("--news-only", action="store_true",
                   help="Skip price refresh, only re-fetch news")
    p.add_argument("--price-only", action="store_true",
                   help="Skip news refresh, only update prices")
    return p.parse_args()


def refresh_watchlist_prices(snapshot_results: list, logger=None) -> int:
    """
    Update watchlist_today rows with the latest price + intraday change %
    from the Polygon grouped snapshot. Returns # rows updated.
    """
    from data_fetcher import watchlist_today, DB_PATH

    by_ticker = {item['T']: item for item in snapshot_results}
    rows = watchlist_today.get_all()
    if not rows:
        if logger:
            logger.log("ℹ️ Pre-market: watchlist_today is empty, nothing to refresh.")
        return 0

    updated = 0
    with sqlite3.connect(DB_PATH) as conn:
        for r in rows:
            ticker = r['ticker']
            snap = by_ticker.get(ticker)
            if not snap:
                continue
            close = snap.get('c', 0)
            opn = snap.get('o', 0) or close
            change_pct = ((close - opn) / opn * 100) if opn else 0.0
            conn.execute(
                "UPDATE watchlist_today SET price = ?, change_pct = ? WHERE ticker = ?",
                (float(close), float(change_pct), ticker),
            )
            updated += 1
    return updated


def refresh_watchlist_news(tickers: list, logger=None) -> int:
    """
    Pull fresh news for each watchlist ticker, classify, write to news_cache.
    Returns total new headlines stored.
    """
    import requests
    from data_fetcher import news_cache_v2, rate_limiter
    from news_engine import NewsClassifier

    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        if logger:
            logger.log("⚠️ Pre-market: POLYGON_API_KEY missing, skipping news.")
        return 0

    total = 0
    for ticker in tickers:
        rate_limiter.wait(logger)
        url = (f"https://api.polygon.io/v2/reference/news"
               f"?ticker={ticker}&limit=5&apiKey={api_key}")
        try:
            resp = requests.get(url, timeout=10).json()
        except Exception as e:
            if logger:
                logger.log(f"⚠️ Pre-market news [{ticker}]: {e}", level="WARNING")
            continue
        for item in (resp.get("results") or []):
            headline = (item.get("title") or "").strip()
            if not headline:
                continue
            verdict = NewsClassifier.classify(headline)
            wrote = news_cache_v2.store(
                ticker=ticker, headline=headline,
                classification=verdict.classification,
                source="polygon-premarket",
                published_at=item.get("published_utc"),
            )
            if wrote:
                total += 1
    return total


def main() -> int:
    args = parse_args()
    print(f"[premarket_refresh] Starting at {datetime.now().isoformat()}")
    print(f"[premarket_refresh] flags: no_telegram={args.no_telegram} "
          f"dry_run={args.dry_run} news_only={args.news_only} price_only={args.price_only}")

    if args.dry_run:
        print("[premarket_refresh] --dry-run set, exiting.")
        return 0

    # Defer heavy imports until after dry-run check.
    print("[premarket_refresh] Loading dashboard module (caches + helpers)...")
    import dashboard       # noqa: F401 — registers singletons
    from data_fetcher import watchlist_today, POLYGON_API_KEY
    import requests

    notify_token = os.getenv("TELEGRAM_TOKEN")
    notify_chat = os.getenv("TELEGRAM_CHAT_ID")

    def notify(msg: str):
        if not args.no_telegram and notify_token and notify_chat:
            _send_one_shot_telegram(msg, notify_token, notify_chat)

    tickers = watchlist_today.get_tickers()
    if not tickers:
        msg = "⚠️ Pre-market refresh: watchlist_today is empty — did the overnight research cycle run?"
        print(msg)
        notify(msg)
        return 0

    started = datetime.now()
    notify(f"☀️ Pre-market refresh started for {len(tickers)} watchlist tickers.")

    # ─── Price refresh ──────────────────────────────────────────────────────
    n_price = 0
    if not args.news_only:
        try:
            from dashboard import get_last_trading_day
            from data_fetcher import rate_limiter
            target_date = get_last_trading_day()
            rate_limiter.wait()
            url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
                   f"{target_date}?adjusted=true&apiKey={POLYGON_API_KEY}")
            resp = requests.get(url, timeout=20).json()
            results = resp.get("results") or []
            n_price = refresh_watchlist_prices(results, logger=dashboard.logger)
            print(f"[premarket_refresh] Updated price + change_pct on {n_price} watchlist rows.")
        except Exception as e:
            print(f"[premarket_refresh] ⚠️ Price refresh failed: {e}", file=sys.stderr)

    # ─── News refresh ───────────────────────────────────────────────────────
    n_news = 0
    if not args.price_only:
        try:
            n_news = refresh_watchlist_news(tickers, logger=dashboard.logger)
            print(f"[premarket_refresh] Ingested {n_news} new headlines into news_cache.")
        except Exception as e:
            print(f"[premarket_refresh] ⚠️ News refresh failed: {e}", file=sys.stderr)

    elapsed = (datetime.now() - started).total_seconds()
    summary = (f"✅ Pre-market refresh done in {elapsed:.0f}s: "
               f"{n_price} prices updated, {n_news} new headlines.")
    print(f"[premarket_refresh] {summary}")
    notify(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
