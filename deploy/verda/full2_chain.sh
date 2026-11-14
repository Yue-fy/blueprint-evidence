#!/usr/bin/env bash
# ON the kept Verda instance (V1 cycle 6): after det4 finishes, re-run `pipeline` on ALL 127,044 questions with
# tonight's integrated code (night from /root/ship/code_full2.bundle), same clean config as the 14:35 run (local models
# only: Qwen3-VL-8B + cc-v2/cc-v3, no Gemini / NVIDIA / OCR server) -> paired with runs/all for S0 (compare_runs.py).
#   TAG=all-n2 PROCS=18 setsid nohup bash /root/ship/full2_chain.sh > /root/work/full2.log 2>&1 &
set -uo pipefail
W=/root/work; R=$W/repo
log() { echo "[$(date +%H:%M:%S)] $*"; }
until [ -f $W/det4/done ]; do sleep 20; done
log "det4 done; switching the repo to night"
# -f: det_render.py was hand-copied into this checkout for det4; without -f the checkout fails and the run would
# silently use the old code -> verify HEAD and stop if it is not the bundle's night
cd $R && git fetch -q /root/ship/code_full2.bundle night:night-full2 && git checkout -f -q night-full2
want=$(git rev-parse night-full2); [ "$(git rev-parse HEAD)" = "$want" ] || { log "checkout FAILED, not running"; exit 1; }
log "repo at $(git log -1 --format='%h %s' | cut -c1-100)"
uv() { $HOME/.local/bin/uv "$@"; }
uv pip install -q --python /root/venv-stoa/bin/python --no-deps -e . && log "stoa reinstalled"
cd $R && GPU_UTIL=${GPU_UTIL:-0.85} SPLIT=all SHARDS=${SHARDS:-256} PROCS=${PROCS:-18} WORKERS=${WORKERS:-6} TAG=${TAG:-all-n2} \
  bash /root/ship/run_full.sh
log "full run exit $?"
