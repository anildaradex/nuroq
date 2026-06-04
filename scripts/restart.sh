#!/usr/bin/env bash
# restart.sh — Refresh the React+iOS bundle and bounce the backend.
#
# Two modes, auto-detected:
#
#   1. LAUNCHD-MANAGED (preferred — after running install-backend.sh)
#      The com.nuroq.backend LaunchAgent owns the backend lifecycle.
#      This script:
#        - Rebuilds the React bundle if the LAN IP has changed
#        - Syncs into the iOS Xcode project
#        - Tells launchd to bounce the backend (kickstart -k)
#        - Stays running just long enough to verify it's back up
#      The backend keeps running after this script exits.
#
#   2. MANUAL (when launchd job isn't installed)
#      Kills any prior uvicorn, starts a new one in the foreground.
#      Ctrl+C to stop. Backend dies when this script exits.

set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="com.nuroq.backend"

# ─── 1. Detect LAN IP ───────────────────────────────────────────────────────
LAN_IP=$(ifconfig | grep -E "inet [0-9]" | grep -v 127.0.0.1 | awk '{print $2}' | head -1)
if [ -z "$LAN_IP" ]; then
    echo "⚠️  No LAN IP detected — iPhone won't reach the backend."
    LAN_IP="127.0.0.1"
fi
echo "▶ LAN IP: $LAN_IP"
API_BASE="http://${LAN_IP}:8000"

# ─── 2. Rebuild + sync iOS if LAN IP changed since last build ──────────────
LAST_IP_FILE="$ROOT/frontend/.last-built-api-base"
if [ -f "$LAST_IP_FILE" ] && [ "$(cat "$LAST_IP_FILE")" = "$API_BASE" ]; then
    echo "▶ iOS bundle already built for $API_BASE — skipping rebuild."
else
    echo "▶ Building React for $API_BASE…"
    (
        cd frontend
        VITE_API_BASE="$API_BASE" npm run build 2>&1 | tail -4
        echo "$API_BASE" > "$LAST_IP_FILE"
        if [ -d "ios" ]; then
            echo "▶ Syncing iOS Xcode project…"
            export LANG=en_US.UTF-8
            npx cap sync ios 2>&1 | tail -3
        fi
    )
fi

# ─── 3. Mode detect: launchd or manual ──────────────────────────────────────
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    # ─── LAUNCHD MODE ──────────────────────────────────────────────────────
    echo ""
    echo "▶ Detected launchd job '$LABEL' — bouncing it via launchctl."
    launchctl kickstart -k "gui/$(id -u)/$LABEL"

    # Verify it actually came back up (Gemma load takes ~10s)
    echo "▶ Waiting up to 60s for backend to respond…"
    for i in $(seq 1 12); do
        sleep 5
        if curl -sS -m 3 -o /dev/null "http://127.0.0.1:8000/api/today/cards" 2>/dev/null; then
            echo ""
            cat <<EOF
═══════════════════════════════════════════════════════════════════════
  ▶ Backend bounced and back up (managed by launchd).

  Web:                http://127.0.0.1:8000
  iPhone Safari:      http://${LAN_IP}:8000
  iPhone NuroQ.app:   open the app

  The backend will keep running after this script exits.
  launchd will auto-restart it on crash, and on every login.

  Manual controls:
    launchctl kickstart -k gui/\$(id -u)/$LABEL   # restart
    launchctl unload ~/Library/LaunchAgents/$LABEL.plist   # stop
    launchctl load   ~/Library/LaunchAgents/$LABEL.plist   # start
    tail -f backend.out.log                       # follow log
═══════════════════════════════════════════════════════════════════════
EOF
            exit 0
        fi
    done
    echo "❌ Backend didn't come back up within 60s — check backend.err.log"
    tail -10 "$ROOT/backend.err.log" 2>/dev/null | sed 's/^/    /'
    exit 1
fi

# ─── MANUAL MODE ────────────────────────────────────────────────────────────
echo ""
echo "▶ No launchd job installed — running backend in foreground."
echo "  (Tip: run ./scripts/install-backend.sh for an always-on backend.)"

pkill -9 -f "uvicorn.*backend" 2>/dev/null && echo "  killed prior uvicorn" || echo "  no prior uvicorn"
sleep 1

trap 'echo ""; echo "▶ Stopping…"; pkill -P $$; exit 0' INT TERM

NUROQ_BACKGROUND_SERVICES="${NUROQ_BACKGROUND_SERVICES:-1}" \
NUROQ_AUTOSTART_AGENT="${NUROQ_AUTOSTART_AGENT:-1}" \
    ./.venv/bin/uvicorn backend.api:app \
    --host 0.0.0.0 --port 8000 --log-level info &
BACKEND_PID=$!

# Wait until reachable
for i in $(seq 1 20); do
    sleep 3
    if curl -sS -m 3 -o /dev/null "http://127.0.0.1:8000/api/today/cards" 2>/dev/null; then break; fi
done

if ! curl -sS -m 3 -o /dev/null "http://127.0.0.1:8000/api/today/cards"; then
    echo "❌ Backend didn't start cleanly — check the log above."
    wait $BACKEND_PID
    exit 1
fi

cat <<EOF

═══════════════════════════════════════════════════════════════════════
  ▶ Backend up (manual / foreground mode).

  Web:                http://127.0.0.1:8000
  iPhone Safari:      http://${LAN_IP}:8000

  Press Ctrl+C to stop the backend.
═══════════════════════════════════════════════════════════════════════

EOF

wait $BACKEND_PID
