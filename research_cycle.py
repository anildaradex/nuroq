#!/usr/bin/env python3
"""
research_cycle.py — Tier 1 overnight research runner (Phase 2 of ARCHITECTURE.md).

Standalone entrypoint for cron / launchd. Disables the dashboard's interactive
background services (Telegram polling, position monitor) so it can coexist with
a running dashboard process without token conflicts, then invokes
`dashboard.run_research_cycle` which:

  1. Pulls a Polygon US-wide grouped snapshot
  2. Filters by liquidity + momentum
  3. Pre-fetches fundamentals and history for the top N
  4. Runs Gemma analysis on each, writing through to:
       - fundamentals_cache (24h TTL)
       - ai_scores_cache    (24h TTL)
  5. Writes a ranked snapshot to the watchlist_today table for the live
     reactive agent (Phase 3) to consume at market open.

Sends one-shot Telegram notifications at start, 25/50/75% milestones, and on
completion. Does NOT start a polling bot, so it never conflicts with the
dashboard's interactive approval bot.

Usage:
  python research_cycle.py                  # default: top 150, with Telegram
  python research_cycle.py --top-n 200      # widen scope
  python research_cycle.py --no-telegram    # silent run (cron testing)
  python research_cycle.py --dry-run        # parse args + log plan, no work

Suggested cron (Linux):
  0 2 * * 1-5 cd /path/to/repo && ./.venv/bin/python research_cycle.py >> research.log 2>&1

macOS launchd: see ARCHITECTURE.md / docs.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

# Critical: disable polling-bot + 60s monitor BEFORE importing dashboard,
# so an already-running dashboard's Telegram session is not disrupted.
os.environ["NUROQ_BACKGROUND_SERVICES"] = "0"


def _send_one_shot_telegram(message: str, token: str, chat_id: str) -> None:
    """One-shot Telegram bot send. No polling, no conflict with the dashboard bot."""
    try:
        from telegram import Bot

        async def _send():
            bot = Bot(token)
            await bot.send_message(chat_id=chat_id, text=message)

        asyncio.run(_send())
    except Exception as e:
        print(f"[research_cycle] ⚠️ Telegram notify failed: {e}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--top-n", type=int, default=150,
                   help="Number of liquid+momentum candidates to deeply analyze (default 150)")
    p.add_argument("--no-telegram", action="store_true",
                   help="Suppress all Telegram notifications")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse args, log the plan, exit without doing work")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print(f"[research_cycle] Starting at {datetime.now().isoformat()}")
    print(f"[research_cycle] top_n={args.top_n} telegram={'off' if args.no_telegram else 'on'} "
          f"dry_run={args.dry_run}")

    if args.dry_run:
        print("[research_cycle] --dry-run set, exiting without work.")
        return 0

    # Defer heavy import until after we know we're actually running.
    # Importing dashboard loads Gemma (~3GB) and instantiates singletons.
    print("[research_cycle] Importing dashboard (loads Gemma + analyst)...")
    import dashboard  # noqa: E402

    # Redirect the gatekeeper.send_notification to a one-shot Bot send if Telegram is on.
    # The polling Application object doesn't exist (gated off), so the dashboard's
    # gatekeeper.send_notification would no-op silently. We replace it so progress
    # notifications actually go out.
    if not args.no_telegram:
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            def _bound_notify(msg: str, _tok=token, _cid=chat_id) -> None:
                _send_one_shot_telegram(msg, _tok, _cid)
            dashboard.gatekeeper.send_notification = _bound_notify
            print("[research_cycle] Telegram one-shot notifier wired (no polling).")
        else:
            print("[research_cycle] ⚠️ TELEGRAM_TOKEN/CHAT_ID missing — running silent.")
    else:
        dashboard.gatekeeper.send_notification = lambda msg: None
        print("[research_cycle] --no-telegram: notifications suppressed.")

    print(f"[research_cycle] Invoking run_research_cycle(top_n={args.top_n})...")
    started = datetime.now()
    try:
        result = dashboard.run_research_cycle(top_n=args.top_n)
        print(f"[research_cycle] {result}")
    except KeyboardInterrupt:
        print("[research_cycle] Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"[research_cycle] ❌ Failed: {e}", file=sys.stderr)
        return 1

    elapsed = (datetime.now() - started).total_seconds()
    print(f"[research_cycle] Done in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
