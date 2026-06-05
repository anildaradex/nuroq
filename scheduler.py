"""
scheduler.py — minimal in-process daily scheduler for the cloud deployment.

On the Mac, the research + premarket jobs run as launchd cron plists (separate
processes with NUROQ_BACKGROUND_SERVICES=0). The single always-on cloud container
has no launchd, so when NUROQ_INPROC_SCHEDULER=1 the backend runs them itself on
daemon threads. Running them IN the backend process is safe — there is no second
Telegram getUpdates poller to conflict with (the conflict only arises between
separate processes) — and it reuses the already-loaded singletons.

Default OFF, so the Mac/dev experience is unchanged. The cloud image sets it to 1.

Jobs (US/Eastern, weekdays):
  • 03:30  research cycle    → populates watchlist_today + AI scores before open
  • 08:00  morning proposals → logs sell proposals into the Recent Activity feed
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _next_fire(hour: int, minute: int, weekdays_only: bool = True,
               now: datetime | None = None) -> datetime:
    """Next datetime (in ET) at HH:MM strictly after `now`, skipping weekends
    when weekdays_only. Pure + deterministic for testing."""
    now = now or datetime.now(ET)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    if weekdays_only:
        while target.weekday() >= 5:  # 5=Sat, 6=Sun
            target += timedelta(days=1)
    return target


def _seconds_until(hour: int, minute: int, weekdays_only: bool = True,
                   now: datetime | None = None) -> float:
    now = now or datetime.now(ET)
    return max(1.0, (_next_fire(hour, minute, weekdays_only, now) - now).total_seconds())


def start_inproc_scheduler(jobs, logger) -> int:
    """Spawn one daemon thread per job. `jobs` is a list of
    (name, hour, minute, fn). Returns the number of jobs scheduled."""
    def _loop(name, hour, minute, fn):
        while True:
            time.sleep(_seconds_until(hour, minute))
            try:
                logger.log(f"⏰ Scheduler: running '{name}'…")
                fn()
                logger.log(f"⏰ Scheduler: '{name}' done.")
            except Exception as e:
                logger.log(f"⚠️ Scheduler: '{name}' failed: {e}", level="ERROR")
            time.sleep(61)  # step past the firing minute so we don't double-run

    for (name, hour, minute, fn) in jobs:
        threading.Thread(target=_loop, args=(name, hour, minute, fn),
                         daemon=True, name=f"sched-{name}").start()
        logger.log(f"⏰ Scheduler: '{name}' scheduled for {hour:02d}:{minute:02d} ET (weekdays).")
    return len(jobs)
