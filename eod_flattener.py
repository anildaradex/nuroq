"""
eod_flattener.py — daemon that closes all open positions ~10 min before
the cash-equities close (default 15:50 ET), so day-trading positions
don't carry overnight.

Designed to run as a long-lived daemon thread started at backend boot when
the live agent is autonomy-enabled (or always, if you prefer belt-and-
suspenders — it's a no-op when there's nothing to close).

The schedule comes from agent_config.eod_flatten_time. Reads the config
every loop so changes via /api/config take effect on the next tick without
a restart.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime, time as dtime

import agent_config


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def _parse_hhmm(s: str, default: dtime) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


class EODFlattener:
    """Wakes once a minute, fires `alpaca_api.flatten_all_positions()` if:
       - today is a weekday
       - it's at or after eod_flatten_time
       - we haven't already flattened today
       - we're in auto_trade_enabled mode (so the human path stays human)
    """

    def __init__(self, alpaca_api, logger):
        self.alpaca_api = alpaca_api
        self.logger = logger
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_flatten_date: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="eod-flattener", daemon=True
        )
        self._thread.start()
        self.logger.log("🕒 EOD flattener daemon started.")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self.logger.log(f"⚠️ EOD flattener tick failed: {e}", level="WARNING")
            # Sleep 60s but wake up faster if stopped.
            self._stop.wait(60)

    def _tick(self) -> None:
        cfg = agent_config.get()
        now = _now_et()
        if now.weekday() >= 5:
            return

        # ── Path A: pending OPEN flatten (user clicked Clean Slate off-hours
        #    and the pending_cancel bracket lock blocked it). Fire once at
        #    market open (or slightly after — give brackets ~2 minutes to fully
        #    release before we retry). This runs INDEPENDENTLY of the
        #    auto_trade_enabled flag, because Clean Slate is a manual user action.
        if cfg.get("pending_open_flatten"):
            if dtime(9, 32) <= now.time() < dtime(16, 0):
                self.logger.log("🧹 Pending-open flatten: market is open — retrying.")
                try:
                    res = self.alpaca_api.flatten_all_positions()
                    self.logger.log(
                        f"🧹 Pending-open flatten done: closed {res.get('closed_count')}, "
                        f"queued {res.get('queued_for_open')}, errors={len(res.get('errors') or [])}."
                    )
                    # Only clear the flag on full success (no errors AND something happened).
                    if not res.get("errors") and (res.get("closed_count") or res.get("queued_for_open")):
                        agent_config.clear_open_flatten()
                    try:
                        from dashboard import gatekeeper
                        gatekeeper.send_notification(
                            f"🧹 Auto-retry flatten at open: closed {res.get('closed_count')}, "
                            f"queued {res.get('queued_for_open')}."
                        )
                    except Exception:
                        pass
                except Exception as e:
                    self.logger.log(f"⚠️ Pending-open flatten failed: {e}", level="ERROR")
            return

        # ── Path B: regular EOD flatten (close-of-session). Gated on AUTO so
        #    the human-approval flow stays human.
        if not cfg.get("auto_trade_enabled"):
            return
        flatten_at = _parse_hhmm(cfg["eod_flatten_time"], dtime(15, 50))
        if not (flatten_at <= now.time() < dtime(16, 0)):
            return
        today_str = now.strftime("%Y-%m-%d")
        if self._last_flatten_date == today_str:
            return

        self.logger.log(
            f"🧹 EOD flatten: time is {now.strftime('%H:%M')} ET — "
            f"closing all open positions."
        )
        try:
            res = self.alpaca_api.flatten_all_positions()
            self._last_flatten_date = today_str
            self.logger.log(
                f"🧹 EOD flatten done: closed {res.get('closed_count')} positions, "
                f"errors={len(res.get('errors') or [])}."
            )
            try:
                from dashboard import gatekeeper
                gatekeeper.send_notification(
                    f"🧹 EOD flatten @ {now.strftime('%H:%M')} ET — "
                    f"closed {res.get('closed_count')} positions."
                )
            except Exception:
                pass
        except Exception as e:
            self.logger.log(f"⚠️ EOD flatten attempt failed: {e}", level="ERROR")
