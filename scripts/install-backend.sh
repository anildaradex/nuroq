#!/usr/bin/env bash
# install-backend.sh — Install the always-on backend as a launchd job.
#
# What this does:
#   1. Writes ~/Library/LaunchAgents/com.nuroq.backend.plist
#   2. Loads it with launchctl
#   3. The backend (FastAPI + agent autostart + Telegram bot + news poller)
#      now starts automatically every time you log in, and auto-restarts
#      if it crashes.
#
# Why LaunchAgent (per-user) instead of LaunchDaemon (system-wide):
#   The backend needs your .venv (Python), .env (API keys), and your
#   home-folder paths. LaunchAgent runs as you and has all that. LaunchDaemon
#   would need root + bind-mounting your home dir into a system context —
#   far more complexity for a single-user research tool.
#
# Result: after `sudo pmset repeat wakeorpoweron MTWRF 00:55:00`
# (or just keeping the Mac always-on), the entire NuroQ system is
# zero-touch — survives reboots, crashes, and overnight sleep.
#
# To run by hand:
#   ./scripts/install-backend.sh

set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LA_DIR"

# Why we use `uv run` instead of .venv/bin/uvicorn directly:
#
#   The .venv/bin/uvicorn script and even .venv/bin/python3 live UNDER
#   ~/Documents, which macOS TCC (Transparency Consent and Control) blocks
#   launchd from executing without explicit Full Disk Access on each binary.
#   /bin/sh is SIP-protected and can't be added to FDA at all.
#
#   uv (installed via Homebrew) lives at /opt/homebrew/bin/uv — OUTSIDE
#   ~/Documents, so launchd can execute it freely with no TCC dance.
#   `uv run` auto-discovers and activates the project's .venv from the
#   working directory, so all venv-installed packages (uvicorn, fastapi,
#   mlx, etc.) resolve correctly.
#
#   Bonus: if you ever update dependencies in pyproject.toml, uv will sync
#   the venv on the next boot — zero-touch dependency management.
UV=$(command -v uv 2>/dev/null)
if [ -z "$UV" ] || [ ! -x "$UV" ]; then
    echo "❌ uv not installed — required for the backend launchd job."
    echo "   Install: brew install uv"
    echo "   (Or run scripts/restart.sh manually for now — that uses the venv's"
    echo "   uvicorn directly which works fine from your terminal but not from launchd.)"
    exit 1
fi
echo "▶ uv binary: $UV"

PLIST="$LA_DIR/com.nuroq.backend.plist"
LABEL="com.nuroq.backend"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <!-- Invoke uv (in /opt/homebrew, outside ~/Documents → no TCC issue)
         which auto-activates the project's .venv from WorkingDirectory.
         All venv-installed packages (uvicorn, fastapi, mlx, gemma) resolve
         normally. -->
    <key>ProgramArguments</key>
    <array>
        <string>$UV</string>
        <string>run</string>
        <string>uvicorn</string>
        <string>backend.api:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8000</string>
        <string>--log-level</string>
        <string>info</string>
    </array>

    <!-- Where uvicorn runs FROM — relative paths (nuroq.db, activity.log,
         frontend/dist/) resolve from here. -->
    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <!-- Env vars baked into the backend process.
         NUROQ_BACKGROUND_SERVICES=1 → Telegram bot, news poller, LLM rescore
                                       queue, position monitor all start
         NUROQ_AUTOSTART_AGENT=1     → Live agent starts as soon as FastAPI
                                       boots (no manual UI click needed) -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>NUROQ_BACKGROUND_SERVICES</key>
        <string>1</string>
        <key>NUROQ_AUTOSTART_AGENT</key>
        <string>1</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- Start as soon as this plist is loaded (i.e. at login). -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if the process exits unexpectedly. Without SuccessfulExit
         this would loop on clean shutdowns; with it, only crashes trigger
         a restart. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <!-- Wait 10s between restart attempts (launchd default). Prevents
         tight crash loops. -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!-- Logs. Tail them to see what the backend is doing:
           tail -f $ROOT/backend.out.log
           tail -f $ROOT/backend.err.log -->
    <key>StandardOutPath</key>
    <string>$ROOT/backend.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/backend.err.log</string>

    <!-- Don't nice-down our process — we want full CPU for MLX inference. -->
    <key>Nice</key>
    <integer>0</integer>
</dict>
</plist>
EOF
echo "✅ Wrote $PLIST"

# If already loaded, unload first so the new plist takes effect
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    echo "▶ Backend already loaded — reloading…"
    launchctl unload "$PLIST" 2>/dev/null || true
    sleep 1
fi

# Also stop any uvicorn that's currently bound to :8000 so launchd can take over
pkill -9 -f "uvicorn.*backend" 2>/dev/null && echo "  killed previous manual uvicorn" || true
sleep 1

echo "▶ Loading $LABEL…"
launchctl load -w "$PLIST"

# Give uvicorn a few seconds to bind the port
sleep 5

echo ""
echo "=== Verify ==="
if launchctl list | grep -q "$LABEL"; then
    PID=$(launchctl list | grep "$LABEL" | awk '{print $1}')
    if [ "$PID" != "-" ]; then
        echo "✅ $LABEL is running (PID $PID)"
    else
        echo "⚠️  $LABEL is loaded but not currently running — check $ROOT/backend.err.log"
    fi
else
    echo "❌ $LABEL not loaded — something went wrong"
    exit 1
fi

# Probe the backend. Gemma model load takes ~10s, so retry for a while.
echo "▶ Waiting up to 60s for backend to respond…"
for i in $(seq 1 12); do
    sleep 5
    if curl -sS -m 3 -o /dev/null "http://127.0.0.1:8000/api/today/cards" 2>/dev/null; then
        echo "✅ Backend responding on http://127.0.0.1:8000"
        LAN_IP=$(ifconfig | grep -E "inet [0-9]" | grep -v 127.0.0.1 | awk '{print $2}' | head -1)
        [ -n "$LAN_IP" ] && echo "✅ iPhone-reachable at http://${LAN_IP}:8000"
        exit 0
    fi
done

# Backend didn't come up — check for the TCC permission issue (the most
# common cause of failure on macOS when the project lives in ~/Documents)
echo ""
echo "⚠️  Backend did not respond within 60s — diagnosing…"
echo ""
if grep -q "Operation not permitted" "$ROOT/backend.err.log" 2>/dev/null; then
    cat <<EOF
═══════════════════════════════════════════════════════════════════════
  ❌ TCC permission denied (macOS Transparency Consent and Control)

  Even though we're invoking uv (outside ~/Documents), uv is trying to
  read project files inside ~/Documents and is being blocked.

  FIX (one-time, ~30 seconds):
    1. Open System Settings → Privacy & Security → Full Disk Access
    2. Click + (Touch ID may be required)
    3. Press ⌘+Shift+G ("Go to folder") and paste this EXACT path:

       $UV

    4. Click Open → it should appear in the FDA list with the toggle ON
    5. Run: launchctl kickstart -k gui/\$(id -u)/$LABEL

  Alternative: move the project out of ~/Documents (e.g. to ~/Code/) so
  TCC doesn't apply at all.
═══════════════════════════════════════════════════════════════════════
EOF
else
    echo "❌ Backend not responding — check $ROOT/backend.err.log for the actual error:"
    echo ""
    tail -20 "$ROOT/backend.err.log" 2>&1 | sed 's/^/    /'
fi
exit 1

cat <<EOF

═══════════════════════════════════════════════════════════════════════
  ✅ Backend installed as a launchd job.

  Behavior:
    • Auto-starts on every login (RunAtLoad)
    • Auto-restarts on crash (KeepAlive)
    • Will NOT restart on a clean shutdown (so you can stop it manually)

  Manual control:
    launchctl unload ~/Library/LaunchAgents/com.nuroq.backend.plist   # stop
    launchctl load   ~/Library/LaunchAgents/com.nuroq.backend.plist   # start
    launchctl kickstart -k gui/\$(id -u)/com.nuroq.backend             # restart

  Tail logs:
    tail -f backend.out.log
    tail -f backend.err.log

  Uninstall:
    launchctl unload ~/Library/LaunchAgents/com.nuroq.backend.plist
    rm ~/Library/LaunchAgents/com.nuroq.backend.plist
═══════════════════════════════════════════════════════════════════════
EOF
