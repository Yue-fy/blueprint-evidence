#!/usr/bin/env bash
# ON the kept Verda instance (V1 cycle 10): full3_chain.sh generalised to any solver / split, e.g. the FILES-ALONE pass
# (every file uploaded alone under a neutral name, solver upload = the web's answer path) on the final gate code,
# paired with runs/lone2 (night code at 15:30, same rows):
#   BUNDLE=/root/ship/code_d32.bundle REF=night-S0-d32 TAG=lone-d32 SOLVER=upload SPLIT=lone PROCS=12 GPU_UTIL=0.75 \
#     setsid nohup bash /root/ship/full4_chain.sh > /root/work/full4.log 2>&1 < /dev/null &
set -uo pipefail
W=/root/work; R=$W/repo; BUNDLE=${BUNDLE:?}; REF=${REF:?}; TAG=${TAG:?}; SOLVER=${SOLVER:-pipeline}; SPLIT=${SPLIT:-all}
log() { echo "[$(date +%H:%M:%S)] $*"; }
cd $R && git fetch -q "$BUNDLE" "$REF:run-$TAG" && git checkout -f -q "run-$TAG"
want=$(git rev-parse "run-$TAG"); [ "$(git rev-parse HEAD)" = "$want" ] || { log "checkout FAILED, not running"; exit 1; }
log "repo at $(git log -1 --format='%h %s' | cut -c1-100)"
$HOME/.local/bin/uv pip install -q --python /root/venv-stoa/bin/python --no-deps -e . && log "stoa reinstalled"
/root/venv-stoa/bin/python -c "import stoa.solvers.$SOLVER, stoa.solvers.pipeline" || { log "import FAILED, not running"; exit 1; }
cd $R && GPU_UTIL=${GPU_UTIL:-0.75} SOLVER=$SOLVER SPLIT=$SPLIT SHARDS=${SHARDS:-256} PROCS=${PROCS:-12} \
  WORKERS=${WORKERS:-6} TAG=$TAG bash /root/ship/run_full.sh
log "full run exit $?"
