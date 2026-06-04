#!/usr/bin/env bash
# Build the React app pointed at this Mac's LAN IP, sync into the iOS project,
# and open Xcode. Run this whenever you change frontend code and want to
# re-run on your iPhone.
#
# Prereq: backend must be running with --host 0.0.0.0 (use scripts/dev.sh or
# the dedicated commands below).

set -e
cd "$(dirname "$0")/.."

# Auto-detect Mac's LAN IP (first non-loopback IPv4)
LAN_IP=$(ifconfig | grep -E "inet [0-9]" | grep -v 127.0.0.1 | awk '{print $2}' | head -1)
if [ -z "$LAN_IP" ]; then
    echo "❌ Could not detect LAN IP. Are you connected to a network?"
    exit 1
fi

API_BASE="http://${LAN_IP}:8000"
echo "▶ API base: $API_BASE  (your iPhone must be on the same WiFi)"

cd frontend
VITE_API_BASE="$API_BASE" npm run build

export LANG=en_US.UTF-8
npx cap sync ios

echo ""
echo "▶ Opening Xcode…"
npx cap open ios

echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Next steps in Xcode:"
echo ""
echo "  1. Plug your iPhone into your Mac via USB."
echo "  2. Trust this computer on your phone if prompted."
echo "  3. In Xcode's top bar, click the device dropdown (next to Run button)"
echo "     and pick YOUR IPHONE (not a simulator)."
echo "  4. Click App in the file tree → 'Signing & Capabilities' tab."
echo "     - Team: pick your Apple ID (Add Account if needed — free)."
echo "     - Bundle Identifier: change 'com.nuroq.app' to anything unique like"
echo "       'com.<yourname>.nuroq' if Xcode complains about availability."
echo "  5. Cmd+R to build & install on your iPhone."
echo "  6. First launch: on iPhone, Settings → General → VPN & Device Management"
echo "     → tap 'Apple Development: <your email>' → Trust."
echo "  7. Re-open NuroQ from your home screen."
echo "═══════════════════════════════════════════════════════════════════════"
