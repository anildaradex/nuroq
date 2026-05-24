# NuroQ — Scheduling the overnight research cycle

This guide covers running `research_cycle.py` automatically before market open
each trading day. The script populates `fundamentals_cache`, `ai_scores_cache`,
and `watchlist_today` so the live agent (Phase 3) can react instantly when the
market opens.

> Last updated: 2026-05-23 (Phase 2 ships)

---

## Quick test before scheduling

```bash
cd /path/to/stock-ai-startup
./.venv/bin/python research_cycle.py --dry-run   # parses args, exits
./.venv/bin/python research_cycle.py --top-n 20 --no-telegram   # short live test
```

A 20-ticker run takes ~3-5 min. The default 150-ticker run takes ~15-25 min.

---

## macOS — launchd (recommended)

macOS deprecated `cron` in favor of `launchd`. Create a plist at
`~/Library/LaunchAgents/com.nuroq.research-cycle.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nuroq.research-cycle</string>

    <key>ProgramArguments</key>
    <array>
        <string>/path/to/stock-ai-startup/.venv/bin/python</string>
        <string>/path/to/stock-ai-startup/research_cycle.py</string>
        <string>--top-n</string>
        <string>150</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/stock-ai-startup</string>

    <!-- Run at 02:00 local time, Mon-Fri only -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>1</integer></dict>
        <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>2</integer></dict>
        <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>3</integer></dict>
        <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>4</integer></dict>
        <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>5</integer></dict>
    </array>

    <key>StandardOutPath</key>
    <string>/path/to/stock-ai-startup/research_cycle.out.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/stock-ai-startup/research_cycle.err.log</string>

    <!-- Optional: run even if missed (e.g., laptop was asleep at 02:00) -->
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Load it:

```bash
launchctl load   ~/Library/LaunchAgents/com.nuroq.research-cycle.plist
launchctl start  com.nuroq.research-cycle   # force-run once to test
launchctl list | grep nuroq                  # verify loaded
```

To unload / disable:

```bash
launchctl unload ~/Library/LaunchAgents/com.nuroq.research-cycle.plist
```

### Make sure the Mac is awake at 02:00

`launchd` doesn't wake a sleeping Mac. Two options:

**Option A — Wake-on-schedule via pmset:**

```bash
sudo pmset repeat wakeorpoweron MTWRF 01:55:00
```

This wakes the Mac at 01:55 every weekday so launchd can fire at 02:00.
Verify: `pmset -g sched`. Cancel: `sudo pmset repeat cancel`.

**Option B — Keep the Mac awake (simpler if it's always on a desk):**

System Settings → Lock Screen → "Turn display off when inactive: Never" +
Battery → "Prevent automatic sleeping" (or `caffeinate -i` in a tab).

---

## Linux — cron

If running on a Linux server:

```bash
crontab -e
```

Add:

```
# NuroQ overnight research cycle — weekdays 02:00 local time
0 2 * * 1-5 cd /path/to/stock-ai-startup && ./.venv/bin/python research_cycle.py --top-n 150 >> research_cycle.log 2>&1
```

Test the cron environment (often different from interactive shell):

```bash
env -i HOME=$HOME PATH=/usr/bin:/bin /path/to/stock-ai-startup/.venv/bin/python /path/to/stock-ai-startup/research_cycle.py --dry-run
```

---

## Linux — systemd (alternative)

`/etc/systemd/system/nuroq-research.service`:

```ini
[Unit]
Description=NuroQ overnight research cycle
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/stock-ai-startup
ExecStart=/path/to/stock-ai-startup/.venv/bin/python research_cycle.py --top-n 150
User=youruser
StandardOutput=append:/var/log/nuroq-research.log
StandardError=append:/var/log/nuroq-research.log
```

`/etc/systemd/system/nuroq-research.timer`:

```ini
[Unit]
Description=Run NuroQ research cycle weekdays at 02:00

[Timer]
OnCalendar=Mon..Fri 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nuroq-research.timer
sudo systemctl list-timers --all | grep nuroq
```

---

## Running alongside the dashboard

Both can run simultaneously **as long as both don't try to start a polling
Telegram bot**. The dashboard does (it needs to receive ✅ EXECUTE / ❌ CANCEL
taps). The cron script does NOT (it sets `NUROQ_BACKGROUND_SERVICES=0` before
importing and uses one-shot Bot sends for progress notifications).

If you ever see "TelegramConflict" or "Conflict: terminated by other getUpdates
request", that means two pollers are competing for the same bot token. Check
that no second dashboard instance is running.

---

## What gets cached

Each successful run populates / refreshes three SQLite tables in `nuroq.db`:

| Table | Rows after a 150-ticker run | TTL |
|---|---|---|
| `fundamentals_cache` | 150 | 24h |
| `ai_scores_cache` | 150 | 24h |
| `watchlist_today` | up to 150, ranked by quant_score | replaced atomically each run |

The live agent (Phase 3) will read `watchlist_today` for its WebSocket
subscriptions and `ai_scores_cache` for the AI score component of the quant
formula during market hours.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `❌ Polygon snapshot returned no results` | Free-tier rate limit hit, or weekend day with no market data |
| `🛑 Polygon rate-limit retries exhausted` | Persistent rate limit — wait or upgrade tier |
| `MLX out of memory` | Gemma model + RAG + other apps eating RAM. Close other apps or skip RAG |
| Cycle hangs on a ticker | yfinance rate-limit; the @retry decorator backs off |
| No Telegram notifications | Check `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` |
| Cron didn't fire | `launchctl list \| grep nuroq` (macOS) or `journalctl -u nuroq-research.timer` (systemd) |
| Mac was asleep | See "Make sure the Mac is awake" above |

---

## Future evolution

This is Phase 2 of the rebuild documented in `ARCHITECTURE.md`. Phase 3 will
add the live reactive agent that consumes the `watchlist_today` produced here.
Once both ship, the daily flow is:

1. 02:00 ET — cron triggers `research_cycle.py` (this guide)
2. 02:00–02:30 ET — caches refreshed, watchlist written
3. 09:15 ET — (Phase 2.5, not built yet) pre-market refresh updates prices
4. 09:30 ET — live reactive agent (Phase 3) subscribes to watchlist via WebSocket
5. 09:30–16:00 ET — agent reacts to live bars, fires Telegram approvals on threshold crossings
6. 16:00 ET — (Phase 5) end-of-day summary Telegram digest
