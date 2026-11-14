#!/usr/bin/env bash
# Deploy the public demo to Google Cloud Run in API-only mode (P1). The CAPTAIN runs this with their own gcloud login;
# agents never run it. Keys go to Secret Manager (typed at a prompt, never echoed, never in the repo or the image).
#
#   PROJECT=<gcp-project> DATA_DIR=/path/to/stoa-demo-data deploy/cloudrun.sh
#
# Optional: REGION (europe-north1), SERVICE (stoa-demo), BUCKET (<project>-stoa-demo-data), TARGET (demo | api).
set -euo pipefail
PROJECT=${PROJECT:?set PROJECT to the Google Cloud project id}
REGION=${REGION:-europe-north1}
TARGET=${TARGET:-demo}
SERVICE=${SERVICE:-stoa-$TARGET}
BUCKET=${BUCKET:-$PROJECT-stoa-demo-data}
DATA_DIR=${DATA_DIR:?set DATA_DIR to a folder made by deploy/subset.py (or the full dataset root)}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TAG=$(git -C "$ROOT" rev-parse --short HEAD)
IMAGE=$REGION-docker.pkg.dev/$PROJECT/stoa/$TARGET:$TAG

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com

# 1. secrets: created once, read by the service at run time
secrets=(gemini-api-key)
if [ "$TARGET" = demo ]; then secrets+=(demo-password); fi
for s in "${secrets[@]}"; do
  if ! gcloud secrets describe "$s" >/dev/null 2>&1; then
    read -rsp "value for secret $s: " v; echo
    printf %s "$v" | gcloud secrets create "$s" --replication-policy=automatic --data-file=-
    unset v
  fi
done

# 2. drawings: a bucket mounted read-only at /data/stoa (STOA_DATA)
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
gcloud storage rsync --recursive "$DATA_DIR" "gs://$BUCKET"

# 3. the runtime service account may read the secrets and the bucket
PNUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA="$PNUM-compute@developer.gserviceaccount.com"
for s in "${secrets[@]}"; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
  --role=roles/storage.objectViewer >/dev/null

# 4. image: Cloud Build from the repo (deploy/Dockerfile, target $TARGET); .gitignore keeps node_modules/.env out
gcloud artifacts repositories describe stoa --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create stoa --repository-format=docker --location="$REGION"
gcloud builds submit "$ROOT" --config "$ROOT/deploy/cloudbuild.yaml" --substitutions "_IMAGE=$IMAGE,_TARGET=$TARGET"

# 5. service: ONE instance (the Gemini call cap is counted per instance). demo = public URL + shared password
#    (Caddy); api = private (call it with an identity token, see deploy/README.md)
SET_SECRETS="GEMINI_API_KEY=gemini-api-key:latest"
PORT=8100
AUTH=--no-allow-unauthenticated
if [ "$TARGET" = demo ]; then
  SET_SECRETS+=",DEMO_PASSWORD=demo-password:latest"; PORT=8080; AUTH=--allow-unauthenticated
fi
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" "$AUTH" \
  --port "$PORT" --cpu 2 --memory 4Gi --timeout 900 --concurrency 10 --min-instances 0 --max-instances 1 \
  --execution-environment gen2 --cpu-boost \
  --set-secrets "$SET_SECRETS" \
  --set-env-vars "STOA_MODE=api,GEMINI_MAX_CALLS_PER_DAY=${GEMINI_MAX_CALLS_PER_DAY:-200},STOA_AGENT=cloudrun" \
  --add-volume "name=data,type=cloud-storage,bucket=$BUCKET,readonly=true" \
  --add-volume-mount "volume=data,mount-path=/data/stoa"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "deployed: $URL"
[ "$TARGET" = demo ] && echo "  user 'stoa', password = secret demo-password; health check: $URL/healthz"
echo "remove after the event: gcloud run services delete $SERVICE --region $REGION; gcloud storage rm -r gs://$BUCKET"
