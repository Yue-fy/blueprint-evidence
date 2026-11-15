#!/usr/bin/env bash
# ON the kept Verda instance (V1 cycle 9): re-run `pipeline` on ALL 127,044 questions with the code of a bundle ref
# (thin bundle on top of the repo already there), same clean config as runs/all and runs/all-n2 (local models only:
# Qwen3-VL-8B + cc-v2/cc-v3, no Gemini / NVIDIA / OCR server) -> paired with runs/all-n2 for S0 (compare_runs.py).
#   BUNDLE=/root/ship/code_d31.bundle REF=night-S0-d31 TAG=all-d31 PROCS=12 GPU_UTIL=0.75 \
#     setsid nohup bash /root/ship/full3_chain.sh > /root/work/full3.log 2>&1 < /dev/null &
# PROCS / GPU_UTIL are lower than the 21:18 run on purpose: detector twins on this GPU serve S0's d31 lane meanwhile.
set -uo pipefail
W=/root/work; R=$W/repo; BUNDLE=${BUNDLE:?}; REF=${REF:?}; TAG=${TAG:?}
log() { echo "[$(date +%H:%M:%S)] $*"; }
cd $R && git fetch -q "$BUNDLE" "$REF:run-$TAG" && git checkout -f -q "run-$TAG"
want=$(git rev-parse "run-$TAG"); [ "$(git rev-parse HEAD)" = "$want" ] || { log "checkout FAILED, not running"; exit 1; }
log "repo at $(git log -1 --format='%h %s' | cut -c1-100)"
$HOME/.local/bin/uv pip install -q --python /root/venv-stoa/bin/python --no-deps -e . && log "stoa reinstalled"
/root/venv-stoa/bin/python -c "import stoa.solvers.pipeline" || { log "import FAILED, not running"; exit 1; }
cd $R && GPU_UTIL=${GPU_UTIL:-0.75} SPLIT=all SHARDS=${SHARDS:-256} PROCS=${PROCS:-12} WORKERS=${WORKERS:-6} TAG=$TAG \
  bash /root/ship/run_full.sh
log "full run exit $?"
