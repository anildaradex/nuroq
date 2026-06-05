# NuroQ — GCP deploy runbook

Target: **one always-on Compute Engine VM** (e2-small) running the cloud
container; **Gemini** as the AI tiebreaker; **SQLite on a persistent disk**.
Chosen 2026-06-04 (see `../CLOUD_MIGRATION.md` for the why and the alternatives).

## What's in here
- `Dockerfile.cloud` — Linux image: FastAPI backend + live agent, **no MLX**
  (gated out by `sys_platform=='darwin'` in pyproject), Gemini AI backend, TZ=ET.
- `deploy_gce.sh` — idempotent one-shot: build → secrets → disk → VM → firewall.

## Prerequisites (you provide)
1. **A GCP project with billing enabled.** Create one and note the ID:
   ```bash
   gcloud projects create nuroq-prod --name=NuroQ        # or reuse an existing one
   # then link billing in the console: Billing → link account
   ```
2. **`.env`** in the repo root — copy `../.env.cloud.example` and fill it in.
   Generate the API key: `openssl rand -hex 24` → `NUROQ_API_KEY`.
3. `gcloud auth login` (already done on this Mac as anil.dara@gmail.com).

## Deploy
```bash
cd stock-ai-startup
PROJECT_ID=nuroq-prod ./deploy/deploy_gce.sh
```
First run takes ~5–10 min (image build pulls torch/sentence-transformers).
Re-run anytime to ship a new build or rotate secrets — it's idempotent.

## Verify
```bash
IP=$(gcloud compute instances describe nuroq-backend --zone us-central1-a \
       --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
curl http://$IP:8000/health                                   # {"ok":true,...}
curl -H "X-NuroQ-Key: $NUROQ_API_KEY" http://$IP:8000/api/propose-sells
```

## After first deploy
- **HTTPS.** :8000 + the API key is the MVP. Before trusting it with live money,
  put TLS in front — easiest is a Cloudflare Tunnel (no public port at all) or
  Caddy on the VM with a domain. Then drop the `0.0.0.0/0` firewall if used.
- **Frontend.** Build with the cloud API base and deploy to a CDN:
  ```bash
  cd frontend && VITE_API_BASE="https://<your-domain>" npm run build
  # → Cloudflare Pages / Vercel (free). Re-point the iOS app + resubmit (see PUBLISHING.md).
  ```
- **Scheduler.** `research_cycle.py` (nightly) + `premarket_refresh.py` (AM) as
  scheduled jobs. Simplest: a cron line on the VM that `docker exec`s the running
  container with `NUROQ_BACKGROUND_SERVICES=0`, e.g.
  `0 3 * * 1-5 docker exec nuroq python research_cycle.py`. (Or split into
  separate scheduled Compute Engine instances / Cloud Scheduler + a small job.)
- **Backups.** Snapshot the data disk on a schedule:
  `gcloud compute disks snapshot nuroq-data --zone us-central1-a` (cron or a
  resource policy). Or add Litestream → Cloud Storage for continuous backup.

## CI/CD — GitHub Actions auto-deploy

`.github/workflows/deploy.yml` auto-deploys on push to `main` or
`feat/algo-claude-improvements` (and via manual "Run workflow"). It builds
`Dockerfile.cloud`, pushes to Artifact Registry, and rolls the VM with
`update-container` (env/secrets/static-IP/data preserved).

**Auth = Workload Identity Federation (no stored key).** One-time setup already
done in `nuroq-prod-anildara`:
- WIF pool `github-pool` + OIDC provider `github-provider`
  (issuer `token.actions.githubusercontent.com`, restricted to owner `anildaradex`).
- Service account `gh-deployer@nuroq-prod-anildara.iam.gserviceaccount.com` with
  `artifactregistry.writer`, `compute.instanceAdmin.v1`, `iam.serviceAccountUser`.
- Repo `anildaradex/nuroq` bound via `roles/iam.workloadIdentityUser` on that SA.

No GitHub secrets needed — the provider path + SA email are non-sensitive and live
in the workflow. To recreate the WIF infra, see the gcloud commands in this repo's
session log. The health check uses `gcloud` (the VM `:8000` firewall is owner-IP
locked, so a runner curl would be blocked).

## Going live (later)
Live trading stays OFF (`NUROQ_LIVE_TRADING=0`). To enable, the safety belt in
`alpaca_executor._connect` also requires **`NUROQ_WASH_SALE_AWARE=1`** OR
**`NUROQ_SECTION_475=1`**. Add a kill-switch before flipping it.

## Cost (personal use, ~$/mo)
e2-small ~$13 · 10 GB disk ~$0.40 · Gemini Flash ~$8–15 · egress trivial ·
Artifact Registry ~$0.10. **≈ $20–30/mo** + your existing Polygon. Alpaca free.

## Notes / gotchas (carried over)
- **Single instance only** — the Telegram poller + Alpaca websocket can't run in
  two copies (`getUpdates`/stream conflict). Don't scale the VM to >1.
- The container's `NUROQ_BACKGROUND_SERVICES=1` runs Telegram/news/monitor; any
  scheduled job you add must use `=0`.
- Secrets land in instance metadata (project-private) via `--container-env`, and
  are also stored in Secret Manager. Fine for a solo project; for stricter
  hygiene switch to a startup-script that fetches from Secret Manager at boot.
