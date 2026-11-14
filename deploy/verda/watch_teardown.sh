#!/usr/bin/env bash
# LOCAL: wait until the LAST of one or more chained remote runs finishes (t1), stalls (no new rows for STALL_MIN)
# or a deadline passes; then for every run: merge its shard predictions, quick_report, pull to
# $JUNCTION_SHARED/artifacts/V1/runs/<tag>/; then DELETE the instance and note it in $JUNCTION_SHARED/verda-watch.log.
# Spot-safe: shard prediction files are rsynced to runs/<tag>/partial/ every 2 min.
#   ID=<instance id> IP=<ip> TAGS="lone:upload same:pipeline" DEADLINE=HH:MM setsid nohup bash watch_teardown.sh > log 2>&1 &
#   NO_DELETE=1: pull + report only, keep the instance (V1 cycle 6)
set -u
ID=${ID:?}; IP=${IP:?}; TAGS=${TAGS:-${TAG:-all}:${SOLVER:-pipeline}}; STALL_MIN=${STALL_MIN:-20}; PRICE=${PRICE:-2.6174}
SH=.stoa; BASE=$SH/artifacts/V1/runs
HERE=$(cd "$(dirname "$0")" && pwd)
K="-i $HOME/.ssh/verda_junction -o BatchMode=yes -o ConnectTimeout=20"
SSH="ssh $K root@$IP"
LAST=${TAGS##* }; LTAG=${LAST%%:*}
dl=$(date -d "${DEADLINE:-23:00}" +%s); last=-1; same=0; why=done
log() { echo "[$(date +%H:%M:%S)] $*"; }
while :; do
  if $SSH "test -f /root/work/runs/$LTAG/t1" 2>/dev/null; then why=done; break; fi
  for ts in $TAGS; do
    t=${ts%%:*}; s=${ts##*:}; mkdir -p $BASE/$t/partial
    $SSH "test -f /root/work/runs/$t/t0" 2>/dev/null || continue
    started=1
    p=$($SSH "SOLVER=$s bash /root/ship/progress.sh $t" 2>/dev/null); log "$t: $p"; cur=$p
    rsync -a -e "ssh $K" "root@$IP:/root/shared/artifacts/eval/*-$s-s*.jsonl" $BASE/$t/partial/ 2>/dev/null
  done
  n=$(echo "${cur:-}" | sed -n 's/^rows=\([0-9]*\).*/\1/p'); n=${n:-$last}
  if [[ $n == "$last" ]]; then same=$((same+1)); else same=0; last=$n; fi
  (( ${started:-0} )) || same=0   # a queued chain (e.g. waiting for another job) is not a stall
  if (( same * 2 >= STALL_MIN )); then why="stalled (rows=$n unchanged ${STALL_MIN} min)"; break; fi
  if (( $(date +%s) > dl )); then why="deadline ${DEADLINE:-23:00} (rows=$n)"; break; fi
  sleep 120
done
log "stop: $why"
for ts in $TAGS; do
  t=${ts%%:*}; s=${ts##*:}; OUT=$BASE/$t; mkdir -p $OUT
  $SSH "test -f /root/work/runs/$t/t0" 2>/dev/null || { log "$t never started"; continue; }
  $SSH "cd /root/work/runs/$t && f=\$(find /root/shared/artifacts/eval -name '*-$s-s*.jsonl' -newer t0 \$( [ -f t1 ] && echo ! -newer t1 )); \
    cat \$f > preds_all.jsonl; gzip -kf preds_all.jsonl; [ -f t1 ] || date +%s > t1_partial; \
    JUNCTION_SHARED=/root/shared /root/venv-stoa/bin/python /root/ship/quick_report.py preds_all.jsonl t0 \$( [ -f t1 ] && echo t1 || echo t1_partial ) $PRICE > quick_report.txt 2>&1; \
    SOLVER=$s bash /root/ship/progress.sh $t > final_progress.txt"
  rsync -a -e "ssh $K" --exclude 'preds_all.jsonl' root@$IP:/root/work/runs/$t/ $OUT/ \
    && log "pulled $t to $OUT ($(du -sh $OUT | cut -f1))" || log "PULL FAILED $t"
done
$SSH "cp /root/work/vllm.log $BASE/ 2>/dev/null; true"
if [ "${NO_DELETE:-0}" = 1 ]; then  # captain 19:23: a kept instance (global teardown deletes it at 02:10)
  echo "- [$(date +%H:%M)] watcher: runs '$TAGS' $why; results in $BASE/<tag>; instance $ID KEPT (NO_DELETE)" >> $SH/verda-watch.log
  exit 0
fi
log "balance before delete: $(python3 -I $HERE/verda.py balance)"
python3 -I $HERE/verda.py delete $ID && log "deleted instance $ID"
sleep 20; log "instances now: $(python3 -I $HERE/verda.py instances)"; log "balance after: $(python3 -I $HERE/verda.py balance)"
echo "- [$(date +%H:%M)] watcher: runs '$TAGS' $why; results in $BASE/<tag>; instance $ID DELETED" >> $SH/verda-watch.log
