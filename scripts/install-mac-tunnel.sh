#!/usr/bin/env bash
#
# install-mac-tunnel.sh — expose the LOCAL NuroQ backend at https://test.nuroquant.com
#
# Same Cloudflare-Tunnel trick the cloud box uses, applied to your Mac. After
# this, you can browse to https://test.nuroquant.com from any device (laptop,
# phone over LTE, etc.) and reach the local Gemma backend with full TLS — no
# SSH tunnel, no port forwarding, no exposing your home IP. If your Mac is off,
# the URL 502s (as expected — that IS the meaning).
#
# Architecture:
#   public client → Cloudflare → outbound tunnel from Mac → uvicorn :8000
#
# One interactive step (Cloudflare OAuth) cannot be automated; the rest is.
#
# USAGE
#   1) ./scripts/install-mac-tunnel.sh                 # installs + walks you through
#   2) cloudflared tunnel login                        # interactive: opens browser
#   3) ./scripts/install-mac-tunnel.sh                 # re-run; now it finishes setup
#
# Re-running after step 3 is idempotent: it skips already-done steps.

set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-nuroq-mac}"
# NOTE: NOT named $HOSTNAME — macOS auto-sets that to the Mac's hostname
# (e.g. "Anils-MacBook-Pro.local"), which would silently win over the default
# below and route the tunnel at the wrong name. Bug we hit once; don't again.
PUBLIC_HOSTNAME="${PUBLIC_HOSTNAME:-test.nuroquant.com}"
LOCAL_URL="${LOCAL_URL:-http://localhost:8000}"
LABEL="com.nuroq.tunnel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
CONFIG_DIR="$HOME/.cloudflared"
CONFIG_FILE="${CONFIG_DIR}/config-${TUNNEL_NAME}.yml"

echo "▶ NuroQ Mac tunnel installer"
echo "    tunnel name: $TUNNEL_NAME"
echo "    hostname   : $PUBLIC_HOSTNAME"
echo "    target     : $LOCAL_URL"
echo

# ── 1) cloudflared binary ────────────────────────────────────────────────────
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "▶ Installing cloudflared via Homebrew…"
  brew install cloudflared
else
  echo "✓ cloudflared already installed ($(cloudflared --version | head -1))"
fi

mkdir -p "$CONFIG_DIR"

# ── 2) Cloudflare account auth (interactive — opens a browser) ───────────────
# `cloudflared tunnel login` opens a CF dashboard URL where you pick the
# `nuroquant.com` zone; CF writes a cert.pem to ~/.cloudflared/ that the rest of
# the tool uses to manage tunnels + DNS on your behalf.
if [ ! -f "$CONFIG_DIR/cert.pem" ]; then
  echo
  echo "──────────────────────────────────────────────────────────────────"
  echo "  NEXT STEP — one-time Cloudflare OAuth (cannot be automated)"
  echo "──────────────────────────────────────────────────────────────────"
  echo "  Run this in your terminal:"
  echo
  echo "      cloudflared tunnel login"
  echo
  echo "  It will open a browser; pick the 'nuroquant.com' zone and click"
  echo "  Authorize. A cert.pem will be saved to $CONFIG_DIR."
  echo
  echo "  Then re-run THIS script and it will finish the setup."
  echo "──────────────────────────────────────────────────────────────────"
  exit 0
fi
echo "✓ Cloudflare cert.pem present (account already authorized)"

# ── 3) Create the tunnel (idempotent) ────────────────────────────────────────
TUNNEL_ID="$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n {print $1; exit}')"
if [ -z "$TUNNEL_ID" ]; then
  echo "▶ Creating tunnel '$TUNNEL_NAME'…"
  cloudflared tunnel create "$TUNNEL_NAME"
  TUNNEL_ID="$(cloudflared tunnel list | awk -v n="$TUNNEL_NAME" '$2==n {print $1; exit}')"
else
  echo "✓ Tunnel '$TUNNEL_NAME' already exists (id $TUNNEL_ID)"
fi
CRED_FILE="${CONFIG_DIR}/${TUNNEL_ID}.json"

# ── 4) Route DNS (idempotent) ────────────────────────────────────────────────
# This creates a CNAME on nuroquant.com pointing to the tunnel. If a record
# already exists, cloudflared exits non-zero — we tolerate that.
echo "▶ Routing $PUBLIC_HOSTNAME → tunnel $TUNNEL_NAME (idempotent)…"
cloudflared tunnel route dns "$TUNNEL_NAME" "$PUBLIC_HOSTNAME" 2>&1 \
  | grep -vE "already exists|conflict" || true

# ── 5) Config file ───────────────────────────────────────────────────────────
# Why a per-tunnel config: keeps this isolated from the cloud tunnel's config
# (if you ever run that locally too).
cat > "$CONFIG_FILE" <<EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${CRED_FILE}

ingress:
  - hostname: ${PUBLIC_HOSTNAME}
    service: ${LOCAL_URL}
  - service: http_status:404
EOF
echo "✓ Wrote config $CONFIG_FILE"

# ── 6) launchd service so it auto-starts at login + restarts on crash ────────
# uses the per-tunnel config; logs in ~/Library/Logs/nuroq-tunnel.{out,err}.log
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(command -v cloudflared)</string>
    <string>tunnel</string>
    <string>--no-autoupdate</string>
    <string>--config</string><string>${CONFIG_FILE}</string>
    <string>run</string>
    <string>${TUNNEL_NAME}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>Crashed</key><true/></dict>
  <key>StandardOutPath</key><string>${HOME}/Library/Logs/nuroq-tunnel.out.log</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/nuroq-tunnel.err.log</string>
</dict>
</plist>
EOF

# (Re)load the launchd job so changes take effect.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo
echo "🚀 Tunnel installed and running."
echo
echo "    URL  : https://${PUBLIC_HOSTNAME}"
echo "    logs : ~/Library/Logs/nuroq-tunnel.{out,err}.log"
echo "    stop : launchctl bootout gui/\$(id -u)/${LABEL}"
echo "    start: launchctl kickstart -k gui/\$(id -u)/${LABEL}"
echo
echo "Open the URL once with ?password-flow… well, your normal login page will"
echo "appear. Sign in with your password and you're done. DNS propagation can"
echo "take ~30s the first time."
