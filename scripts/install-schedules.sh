#!/usr/bin/env bash
# install-schedules.sh — Install launchd plists for the overnight research
# cycle and the pre-market refresh, so the system is fresh every weekday
# morning without you lifting a finger.
#
# Times are in the Mac's LOCAL timezone. Configured here for CDT (Central):
#   03:30 CT  research_cycle.py       (= 04:30 ET, comfortably after Polygon
#                                      publishes the prior-day grouped daily
#                                      snapshot at ~03:00 ET — earlier times
#                                      can race the publish and get empty data)
#   08:00 CT  premarket_refresh.py    (= 09:00 ET, 30 min before market open)
# If your TZ shifts (e.g. CST in winter, or you move to a different zone),
# edit the StartCalendarInterval entries and reload with this script.
#
# pmset wake-on-schedule is also configured so a sleeping Mac still wakes
# for the 01:00 cron job (requires sudo — script prompts).

set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LA_DIR"

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "❌ Python not found at $PYTHON — run 'uv sync' first."
    exit 1
fi

# ────────────────────────────────────────────────────────────────────────────
# Plist 1: overnight research cycle  (01:00 CT, weekdays)
# ────────────────────────────────────────────────────────────────────────────
RESEARCH_PLIST="$LA_DIR/com.nuroq.research-cycle.plist"
cat > "$RESEARCH_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nuroq.research-cycle</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$ROOT/research_cycle.py</string>
        <string>--top-n</string>
        <string>150</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <!-- NUROQ_BACKGROUND_SERVICES=0 prevents this cron job from spinning up
         a Telegram bot poller that would conflict with the running backend's
         poller. The research cycle just writes to SQLite; it doesn't need
         the news poller / position monitor / Telegram bot at all. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>NUROQ_BACKGROUND_SERVICES</key>
        <string>0</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- 01:00 local time (= 02:00 ET in CDT), Mon-Fri.
         Weekday: 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer><key>Weekday</key><integer>1</integer></dict>
        <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer><key>Weekday</key><integer>2</integer></dict>
        <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer><key>Weekday</key><integer>3</integer></dict>
        <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer><key>Weekday</key><integer>4</integer></dict>
        <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer><key>Weekday</key><integer>5</integer></dict>
    </array>

    <key>StandardOutPath</key>
    <string>$ROOT/research_cycle.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/research_cycle.err.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF
echo "✅ Wrote $RESEARCH_PLIST"

# ────────────────────────────────────────────────────────────────────────────
# Plist 2: pre-market refresh  (08:15 CT, weekdays)
# ────────────────────────────────────────────────────────────────────────────
PREMARKET_PLIST="$LA_DIR/com.nuroq.premarket-refresh.plist"
cat > "$PREMARKET_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nuroq.premarket-refresh</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$ROOT/premarket_refresh.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>NUROQ_BACKGROUND_SERVICES</key>
        <string>0</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- 08:15 local time (= 09:15 ET in CDT), Mon-Fri. -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>1</integer></dict>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>2</integer></dict>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>3</integer></dict>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>4</integer></dict>
        <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer><key>Weekday</key><integer>5</integer></dict>
    </array>

    <key>StandardOutPath</key>
    <string>$ROOT/premarket_refresh.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/premarket_refresh.err.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF
echo "✅ Wrote $PREMARKET_PLIST"

# ────────────────────────────────────────────────────────────────────────────
# Load (or reload) both plists
# ────────────────────────────────────────────────────────────────────────────
for plist in "$RESEARCH_PLIST" "$PREMARKET_PLIST"; do
    label=$(basename "$plist" .plist)
    # If already loaded, unload first so the new schedule takes effect
    if launchctl list 2>/dev/null | grep -q "$label"; then
        echo "▶ Reloading $label…"
        launchctl unload "$plist" 2>/dev/null || true
    fi
    launchctl load -w "$plist"
done
echo ""

# ────────────────────────────────────────────────────────────────────────────
# Verify
# ────────────────────────────────────────────────────────────────────────────
echo "=== Currently scheduled (launchctl) ==="
launchctl list | grep nuroq || echo "(none loaded — something went wrong)"
echo ""

# ────────────────────────────────────────────────────────────────────────────
# pmset wake — needs sudo, just print the command for the user to run
# ────────────────────────────────────────────────────────────────────────────
cat <<EOF
═══════════════════════════════════════════════════════════════════════
  ✅ launchd schedules installed.

  Times (Mac local CDT):
    • 03:30 CT  research_cycle.py       (~5h before market open, safely
                                         after Polygon's daily snapshot)
    • 08:00 CT  premarket_refresh.py    (30 min before market open)

  Logs:
    • $ROOT/research_cycle.out.log
    • $ROOT/research_cycle.err.log
    • $ROOT/premarket_refresh.out.log
    • $ROOT/premarket_refresh.err.log

  ⚠️  One more step — wake the Mac before the cron fires:

      sudo pmset repeat wakeorpoweron MTWRF 03:25:00

  Verify with: pmset -g sched
  Cancel with: sudo pmset repeat cancel

  Or, if your Mac is always plugged in and on:
    System Settings → Lock Screen → "Turn display off when inactive: Never"
    System Settings → Battery → "Prevent automatic sleeping when display is off"

  To run a cron job manually right now (sanity check):
    launchctl start com.nuroq.research-cycle
    launchctl start com.nuroq.premarket-refresh

  To uninstall:
    launchctl unload -w ~/Library/LaunchAgents/com.nuroq.research-cycle.plist
    launchctl unload -w ~/Library/LaunchAgents/com.nuroq.premarket-refresh.plist
═══════════════════════════════════════════════════════════════════════
EOF
