#!/usr/bin/env bash
# ON the Verda instance (captain 16:15): door/window symbol detector for clean CAD sheets.
#   1. det_render.py: Swiss TRAIN floors (train split minus train_gate) as clean CAD line drawings + exact DXF boxes,
#      tiles of 1024 px; Swiss DEV floors as whole sheets at 25/45/70 px/m (held-out eval)
#   2. det_train.py: torchvision Faster R-CNN v2, time box $DET_MIN (GPU shared with the cc-wild job: fits in 80 GB)
#   3. det_eval.py: dev exact counts / precision / recall + Stoa's DfE + AFCEC sheets and X1's A-101F (PDF @150 dpi)
# Markers /root/work/det/{rendered,trained,done}; the local watcher deletes the instance after done (+ wild/all.done).
set -uo pipefail
O=/root/work/det; R=/root/work/repo; mkdir -p $O/logs
export STOA_DATA=/root/work/data/junction-dataset JUNCTION_SHARED=/root/shared PYTHONUNBUFFERED=1
log() { echo "[$(date +%H:%M:%S)] $*"; }
cd $R
( /root/venv-stoa/bin/python experiments/v1/det_render.py $O/dev --split dev --full --procs 4 > $O/logs/render_dev.log 2>&1
  /root/venv-stoa/bin/python -c "import fitz,sys; d=fitz.open(sys.argv[1]); d[0].get_pixmap(dpi=150).save(sys.argv[2])" \
     /root/ship/det_extra/a101f.pdf /root/ship/det_extra/a101f-150dpi.png 2>> $O/logs/render_dev.log ) &
DV=$!
/root/venv-stoa/bin/python experiments/v1/det_render.py $O/train --split train --renders ${RENDERS:-4} --procs ${PROCS:-10} \
  > $O/logs/render_train.log 2>&1
log "train tiles: $(tail -n 1 $O/logs/render_train.log)"; touch $O/rendered
/root/venv-vllm/bin/python experiments/v1/det_train.py --data $O/train --out $O/model --minutes ${DET_MIN:-50} \
  --workers ${DET_WORKERS:-6} > $O/logs/train.log 2>&1
log "train exit $?: $(tail -n 1 $O/logs/train.log | cut -c1-300)"; touch $O/trained
wait $DV; log "dev renders: $(tail -n 1 $O/logs/render_dev.log)"
/root/venv-vllm/bin/python experiments/v1/det_eval.py --model $O/model/model.pt --dev $O/dev \
  --extra /root/ship/det_extra/dfe.png /root/ship/det_extra/afcec.png /root/ship/det_extra/a101f-150dpi.png \
  --out $O/out > $O/logs/eval.log 2>&1
log "eval exit $?"; cat $O/out/dev_report.txt 2>/dev/null
touch $O/done
