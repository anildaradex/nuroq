#!/bin/sh
# Container entrypoint: start the Cloudflare Tunnel (so NuroQ is reachable over
# HTTPS from anywhere with no inbound firewall opening) alongside uvicorn.
#
#   CLOUDFLARED_TOKEN set  → NAMED tunnel (stable URL on your Cloudflare domain)
#   CLOUDFLARED_TOKEN unset → QUICK tunnel (ephemeral https://<rand>.trycloudflare.com;
#                              the URL is written to /data/cloudflared.log and
#                              surfaced at GET /tunnel-url)
#   NUROQ_TUNNEL=0         → disable the tunnel entirely (direct IP only)
set -e
PORT="${PORT:-8000}"

if [ "${NUROQ_TUNNEL:-1}" = "1" ]; then
  if [ -n "$CLOUDFLARED_TOKEN" ]; then
    echo "[entrypoint] Starting NAMED Cloudflare tunnel…"
    cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARED_TOKEN" &
  else
    echo "[entrypoint] Starting QUICK Cloudflare tunnel (URL → /data/cloudflared.log, /tunnel-url)…"
    cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}" \
      --logfile /data/cloudflared.log &
  fi
fi

# uvicorn is PID 1 — if it dies, konlet restarts the container.
exec uvicorn backend.api:app --host 0.0.0.0 --port "${PORT}" --log-level info
