#!/usr/bin/env bash
#
# deploy_gce.sh — one-shot deploy of NuroQ to a single always-on Compute Engine
# VM running the cloud container image. Idempotent: safe to re-run to ship a new
# build or rotate secrets.
#
# Prereqs (you provide):
#   • A GCP project with BILLING ENABLED            → export PROJECT_ID=...
#   • gcloud authenticated (gcloud auth login)       (already done on this Mac)
#   • A .env file in the repo root with the secrets below.
#
# What it does:
#   1. Sets project + enables the needed APIs
#   2. Creates an Artifact Registry repo
#   3. Builds the cloud image via Cloud Build (no local Docker needed)
#   4. Stores secrets in Secret Manager (source of truth)
#   5. Creates a persistent data disk (SQLite lives here, survives VM recreate)
#   6. Creates/updates the e2-small VM running the container, secrets injected
#   7. Opens a firewall rule for :8000 (API-key gated)
#
# Usage:
#   PROJECT_ID=my-proj ./deploy/deploy_gce.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

# ─── Config (override via env) ───────────────────────────────────────────────
: "${PROJECT_ID:?Set PROJECT_ID=your-gcp-project (billing enabled)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-${REGION}-a}"
REPO="${REPO:-nuroq}"
# Unique tag per build so `update-container` on the existing VM reliably re-pulls
# (konlet won't re-fetch an unchanged :latest). Override TAG to redeploy a prior build.
TAG="${TAG:-v$(date +%Y%m%d-%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/nuroq:${TAG}"
VM="${VM:-nuroq-backend}"
# e2-medium (4GB): the torch + sentence-transformers stack OOMs on e2-small (2GB),
# which crash-loops the container.
MACHINE="${MACHINE:-e2-medium}"
# SQLite persists on a host-path on the boot disk (survives container restarts +
# reboots). Avoids the separate-disk fsck-on-restart race konlet hits.
BOOT_DISK_GB="${BOOT_DISK_GB:-30}"
DATA_HOST_PATH="${DATA_HOST_PATH:-/var/lib/nuroq}"
ENV_FILE="${ENV_FILE:-.env}"
# Firewall: default to THIS machine's public IP only. Set ALLOWED_CIDR=0.0.0.0/0
# to expose publicly (the NUROQ_API_KEY header still gates every request).
ALLOWED_CIDR="${ALLOWED_CIDR:-$(curl -s https://api.ipify.org)/32}"

# AI auth: "vertex" (VM service account, no key — default/chosen) or "apikey".
GEMINI_AUTH="${GEMINI_AUTH:-vertex}"

# Secrets we expect in $ENV_FILE (the app reads these as env). GEMINI_API_KEY is
# only needed when GEMINI_AUTH=apikey; NUROQ_API_KEY is auto-generated if absent.
SECRET_KEYS=(POLYGON_API_KEY ALPACA_API_KEY ALPACA_SECRET_KEY \
             TELEGRAM_TOKEN TELEGRAM_CHAT_ID NUROQ_API_KEY)
[ "$GEMINI_AUTH" = "apikey" ] && SECRET_KEYS+=(GEMINI_API_KEY)

echo "▶ Project=$PROJECT_ID region=$REGION zone=$ZONE image=$IMAGE"
[ -f "$ENV_FILE" ] || { echo "❌ $ENV_FILE not found"; exit 1; }

gcloud config set project "$PROJECT_ID" >/dev/null

# ─── 1. APIs ─────────────────────────────────────────────────────────────────
echo "▶ Enabling APIs…"
APIS="compute.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com"
[ "$GEMINI_AUTH" = "vertex" ] && APIS="$APIS aiplatform.googleapis.com"
gcloud services enable $APIS >/dev/null

# ─── 2. Artifact Registry ────────────────────────────────────────────────────
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location "$REGION" --description "NuroQ images"

# ─── 3. Build image (Cloud Build) ────────────────────────────────────────────
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "▶ SKIP_BUILD=1 — reusing existing image $IMAGE"
else
  echo "▶ Building image via Cloud Build (uses .dockerignore; mlx skipped on Linux)…"
  gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_IMAGE="$IMAGE" .
fi

# ─── 4. Secrets → Secret Manager ─────────────────────────────────────────────
echo "▶ Syncing secrets from $ENV_FILE → Secret Manager…"
set +x
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
# API auth key (gates every request). Resolve in priority order so it stays
# STABLE across re-runs (regenerating it each deploy would break your clients):
#   1) value in $ENV_FILE  2) existing Secret Manager version  3) generate once.
# Never echoed — retrieve with: gcloud secrets versions access latest --secret=NUROQ_API_KEY
if [ -z "${NUROQ_API_KEY:-}" ]; then
  NUROQ_API_KEY=$(gcloud secrets versions access latest --secret=NUROQ_API_KEY 2>/dev/null || true)
  if [ -z "$NUROQ_API_KEY" ]; then
    NUROQ_API_KEY=$(openssl rand -hex 24)
    echo "  ▶ Generated a new NUROQ_API_KEY (saved to $ENV_FILE; retrieve via Secret Manager)."
  fi
  grep -q '^NUROQ_API_KEY=' "$ENV_FILE" 2>/dev/null || printf '\nNUROQ_API_KEY=%s\n' "$NUROQ_API_KEY" >> "$ENV_FILE"
fi
export NUROQ_API_KEY
CONTAINER_ENV=""
for k in "${SECRET_KEYS[@]}"; do
  v="${!k:-}"
  if [ -z "$v" ]; then echo "  ⚠️  $k empty in $ENV_FILE (skipping)"; continue; fi
  if gcloud secrets describe "$k" >/dev/null 2>&1; then
    printf '%s' "$v" | gcloud secrets versions add "$k" --data-file=- >/dev/null
  else
    printf '%s' "$v" | gcloud secrets create "$k" --data-file=- >/dev/null
  fi
  CONTAINER_ENV+="--container-env=${k}=${v} "
done

# Non-secret runtime config (paper trading stays ON until you flip it).
RUNTIME_ENV="\
--container-env=NUROQ_AI_BACKEND=gemini \
--container-env=NUROQ_DB_PATH=/data/nuroq.db \
--container-env=NUROQ_BACKGROUND_SERVICES=1 \
--container-env=NUROQ_AUTOSTART_AGENT=1 \
--container-env=NUROQ_INPROC_SCHEDULER=1 \
--container-env=NUROQ_LIVE_TRADING=0 \
--container-env=TZ=America/New_York"

# Vertex mode: the container authenticates to Gemini via the VM's service account
# (Application Default Credentials reached through the GCE metadata server). No
# API key. Grant the default compute SA the aiplatform.user role.
if [ "$GEMINI_AUTH" = "vertex" ]; then
  RUNTIME_ENV="$RUNTIME_ENV \
--container-env=NUROQ_GEMINI_VERTEX=1 \
--container-env=GOOGLE_CLOUD_PROJECT=${PROJECT_ID} \
--container-env=GOOGLE_CLOUD_LOCATION=${REGION}"
  PROJ_NUM=$(gcloud projects describe "$PROJECT_ID" --format='get(projectNumber)')
  VM_SA="${PROJ_NUM}-compute@developer.gserviceaccount.com"
  echo "▶ Granting ${VM_SA} roles/aiplatform.user (Vertex)…"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${VM_SA}" --role="roles/aiplatform.user" \
    --condition=None >/dev/null
fi

# ─── 5. (SQLite lives on a host-path on the boot disk — see DATA_HOST_PATH) ───
#   Back it up with boot-disk snapshots:
#     gcloud compute disks snapshot $VM --zone $ZONE

# ─── 6. Firewall (:8000, source-restricted) ──────────────────────────────────
gcloud compute firewall-rules describe nuroq-api >/dev/null 2>&1 || \
  gcloud compute firewall-rules create nuroq-api \
    --allow tcp:8000 --source-ranges "$ALLOWED_CIDR" --target-tags nuroq
echo "▶ Firewall :8000 open to $ALLOWED_CIDR (every request still needs the X-NuroQ-Key header)."

# ─── 7. VM with container ────────────────────────────────────────────────────
if gcloud compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  echo "▶ Updating existing VM container → $IMAGE …"
  # shellcheck disable=SC2086
  gcloud compute instances update-container "$VM" --zone "$ZONE" \
    --container-image "$IMAGE" $CONTAINER_ENV $RUNTIME_ENV
else
  echo "▶ Creating VM $VM ($MACHINE) with the container…"
  # shellcheck disable=SC2086
  gcloud compute instances create-with-container "$VM" \
    --zone "$ZONE" --machine-type "$MACHINE" --tags nuroq \
    --scopes=https://www.googleapis.com/auth/cloud-platform \
    --boot-disk-size="${BOOT_DISK_GB}GB" \
    --container-image "$IMAGE" \
    --container-mount-host-path "mount-path=/data,host-path=${DATA_HOST_PATH},mode=rw" \
    --container-restart-policy=always \
    $CONTAINER_ENV $RUNTIME_ENV
fi

IP=$(gcloud compute instances describe "$VM" --zone "$ZONE" \
      --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo ""
echo "✅ Deployed. VM external IP: $IP"
echo "   Health:  curl http://$IP:8000/health"
echo "   API:     curl -H \"X-NuroQ-Key: \$NUROQ_API_KEY\" http://$IP:8000/api/propose-sells"
echo "   Logs:    gcloud compute ssh $VM --zone $ZONE --command 'docker logs \$(docker ps -q)'"
echo ""
echo "⚠️  Next: put HTTPS in front (Caddy/CF Tunnel) before trusting this with live trading,"
echo "   and point the React app's VITE_API_BASE at it (see deploy/README.md)."
