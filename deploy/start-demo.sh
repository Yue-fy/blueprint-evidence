#!/usr/bin/env bash
# Container entry point for the demo image (P1): API (:8100) + web (vite preview :5173, its /api middleware forwards
# to the API) + Caddy on $PORT with a shared password. If any of the three exits, the container exits (Cloud Run /
# docker --restart brings it back).
set -euo pipefail
: "${DEMO_PASSWORD:?set DEMO_PASSWORD (the shared demo password) in the environment}"
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "warning: GEMINI_API_KEY is not set: only the deterministic paths (IFC, Swiss DXF, schedules) can answer" >&2
fi
export DEMO_USER="${DEMO_USER:-stoa}"
DEMO_PASSWORD_HASH="$(caddy hash-password --plaintext "$DEMO_PASSWORD")"
export DEMO_PASSWORD_HASH
unset DEMO_PASSWORD
mkdir -p "${STOA_UPLOADS:-/tmp/stoa-uploads}" "$JUNCTION_SHARED/logs" "$JUNCTION_SHARED/locks"

cd /app
uvicorn stoa.api:app --host 127.0.0.1 --port 8100 &
(cd /app/web && exec node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 5173 --strictPort) &
caddy run --config /app/deploy/Caddyfile --adapter caddyfile &
wait -n
echo "a demo process exited; stopping the container" >&2
exit 1
