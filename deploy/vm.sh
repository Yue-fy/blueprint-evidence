#!/usr/bin/env bash
# Run the demo on any Linux VM with Docker (e.g. a Vultr CPU instance, Ubuntu 24.04, 4 vCPU / 8 GB) (P1).
# The CAPTAIN runs this on the VM, from a clone of the repo. Keys live in an env file OUTSIDE the repo:
#
#   install -m 600 /dev/null ~/stoa.env && nano ~/stoa.env     # two lines: GEMINI_API_KEY=...  DEMO_PASSWORD=...
#   DATA_DIR=~/stoa-demo-data deploy/vm.sh                      # demo on port 80: http://<vm-ip>/ (user 'stoa')
#
# Plain HTTP: put the VM behind HTTPS (e.g. Caddy with a domain) before sharing the password widely.
set -euo pipefail
ENV_FILE=${ENV_FILE:-$HOME/stoa.env}
DATA_DIR=${DATA_DIR:?set DATA_DIR to a folder made by deploy/subset.py (or the full dataset root)}
HOST_PORT=${HOST_PORT:-80}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (GEMINI_API_KEY=..., DEMO_PASSWORD=...)" >&2; exit 2; }
command -v docker >/dev/null || { echo "install Docker first: https://docs.docker.com/engine/install/" >&2; exit 2; }

DOCKER_BUILDKIT=1 docker build -f "$ROOT/deploy/Dockerfile" --target demo -t stoa:demo "$ROOT"
docker rm -f stoa-demo >/dev/null 2>&1 || true
docker run -d --name stoa-demo --restart unless-stopped \
  --env-file "$ENV_FILE" -e STOA_AGENT=vm \
  -v "$DATA_DIR":/data/stoa:ro -v stoa-state:/var/stoa \
  -p "$HOST_PORT":8080 stoa:demo
echo "started on port $HOST_PORT (user 'stoa'); logs: docker logs -f stoa-demo; health: curl localhost:$HOST_PORT/healthz"
