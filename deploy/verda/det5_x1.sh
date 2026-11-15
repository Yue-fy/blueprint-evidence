#!/usr/bin/env bash
# ON the instance (V1 cycle 7): X1's Stoa-style sheets for v4 vs v5 on the GPU at the 0.9 floor (det5_chain's last
# step used the vLLM venv for the client, which has no PyMuPDF -> exit 1). Servers: vllm venv (torch); client: stoa venv.
set -uo pipefail
O=/root/work/det5; D4=/root/work/det4
export JUNCTION_SHARED=/root/shared PYTHONUNBUFFERED=1
log() { echo "[$(date +%H:%M:%S)] $*"; }
cd /root/x1/code
for M in v4:8150:$D4/model/model.pt v5:8151:$O/model/model.pt; do
  IFS=: read n port p <<< "$M"
  DET_DEVICE=cuda DET_SCORE_FLOOR=0.9 DET_THREADS=8 DET_MODEL=$p setsid nohup /root/venv-vllm/bin/python det_server.py --port $port \
    > $O/logs/server_$n.log 2>&1 < /dev/null &
  echo $! > $O/server_$n.pid
done
for i in $(seq 120); do curl -s -m 2 127.0.0.1:8151/health | grep -q ok && curl -s -m 2 127.0.0.1:8150/health | grep -q ok && break; sleep 2; done
for M in v4:8150 v5:8151; do
  IFS=: read n port <<< "$M"
  X1SCAN=/root/x1/x1scan STOA_LIKE=/root/x1/stoa-like /root/venv-stoa/bin/python det_scan_eval.py --url http://127.0.0.1:$port \
    --tag ${n}_x1 > $O/logs/x1_$n.log 2>&1; log "x1 $n exit $?"
  X1SCAN=/root/x1/x1scan STOA_LIKE=/root/x1/stoa-like /root/venv-stoa/bin/python det_scan_eval.py --url http://127.0.0.1:$port \
    --tag ${n}_x1 --sweep > $O/logs/x1_${n}_sweep.log 2>&1; log "x1 $n sweep exit $?"
done
cp /root/shared/artifacts/V1/det/scan_eval_v*_x1.jsonl $O/ 2>/dev/null
for n in v4 v5; do kill $(cat $O/server_$n.pid) 2>/dev/null; done
touch $O/x1.done; log "x1 done"
