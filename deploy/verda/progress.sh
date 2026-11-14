#!/usr/bin/env bash
# ON the instance: progress of a run: answered rows, rate, GPU util, load, free RAM.  bash progress.sh [TAG]
TAG=${1:-all}; W=/root/work
n=$(find /root/shared/artifacts/eval -name "*-${SOLVER:-pipeline}-s*.jsonl" -newer $W/runs/$TAG/t0 -exec cat {} + 2>/dev/null | wc -l)
t0=$(cat $W/runs/$TAG/t0 2>/dev/null || date +%s); el=$(( $(date +%s) - t0 ))
logs=$(ls $W/runs/$TAG/*.log 2>/dev/null | wc -l); done_=$(grep -l "^predictions:" $W/runs/$TAG/*.log 2>/dev/null | wc -l)
echo "rows=$n elapsed=${el}s rate=$(( n / (el>0?el:1) ))/s shards_started=$logs shards_done=$done_ \
gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader) load=$(cut -d' ' -f1 /proc/loadavg) \
mem_free_gb=$(free -g | awk '/Mem/{print $7}') errors=$(grep -h -c Traceback $W/runs/$TAG/*.log 2>/dev/null | paste -sd+ | bc 2>/dev/null)"
