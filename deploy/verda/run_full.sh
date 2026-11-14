#!/usr/bin/env bash
# Runs ON the Verda instance. Starts vLLM (Qwen3-VL-8B + counting LoRAs cc-v2/cc-v3) on the whole GPU, then runs
# the `pipeline` solver (clean config: cc5k=0, local models only) over every question in parallel processes.
#   SPLIT=all|smoke|dev  SHARDS=64  PROCS=20  WORKERS=6  bash run_full.sh
#   SOLVER=upload SPLIT=lone: the files-alone variant (lone_split.py: neutral hard links, no folder / sibling hints)
#   SPLIT=same: the same rows and shards on their original dataset paths (control)
set -uo pipefail
W=/root/work; R=$W/repo; SPLIT=${SPLIT:-all}; SOLVER=${SOLVER:-pipeline}; SHARDS=${SHARDS:-64}; PROCS=${PROCS:-20}; WORKERS=${WORKERS:-6}
export STOA_DATA=$W/data/junction-dataset JUNCTION_SHARED=/root/shared LLM_BASE_URL=http://127.0.0.1:8001/v1 \
       STOA_OCR_URL=http://127.0.0.1:9 AGENT_ID=V1 PYTHONUNBUFFERED=1
unset GEMINI_API_KEY NVIDIA_API_KEY
export PATH=/root/venv-vllm/bin:$PATH   # flashinfer JIT needs ninja (from the vllm venv); triton needs python3.12-dev
[ -d /root/lora/cc-v2 ] || { mkdir -p /root/lora && tar -C /root/lora -xf /root/ship/lora.tar; }
curl -s -m 2 127.0.0.1:8001/v1/models >/dev/null || bash /root/ship/vllm_up.sh || exit 1   # prints a TEST answer
curl -s 127.0.0.1:8001/v1/models | head -c 400; echo
TAG=${TAG:-$SPLIT-$(date +%H%M)}; SD=$W/shards/$TAG; mkdir -p $SD $W/runs/$TAG
if [ "$SPLIT" = lone ] || [ "$SPLIT" = same ]; then cd $R && ORIG=$([ "$SPLIT" = same ] && echo 1) NEUTRAL_ROOT=$W/neutral /root/venv-stoa/bin/python /root/ship/lone_split.py $SD $SHARDS
else cd $R && /root/venv-stoa/bin/python /root/ship/shard.py $SD $SHARDS $SPLIT; fi
date +%s > $W/runs/$TAG/t0
ls $SD/*.jsonl | xargs -P $PROCS -I{} sh -c 'b=$(basename {} .jsonl); /root/venv-stoa/bin/python -m stoa.eval --solver '$SOLVER' --solver-arg cc5k=0 --split {} --workers '$WORKERS' --no-leaderboard --quiet --note "V1 verda '$TAG' $b" > '$W/runs/$TAG'/$b.log 2>&1'
date +%s > $W/runs/$TAG/t1
echo "done $TAG in $(( $(cat $W/runs/$TAG/t1) - $(cat $W/runs/$TAG/t0) ))s"
