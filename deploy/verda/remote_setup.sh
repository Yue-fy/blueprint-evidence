#!/usr/bin/env bash
# Runs ON the Verda instance (as root) after scp of ship/ to /root/ship. Installs everything in parallel:
#   (a) Stoa's full dataset from its public URL, (b) our code from a git bundle into a py3.12 venv (pinned deps),
#   (c) vLLM in its own venv, (d) the base model Qwen/Qwen3-VL-8B-Instruct from Hugging Face.
# Logs: /root/work/setup-*.log; done markers: /root/work/*.done
set -uo pipefail
W=/root/work; mkdir -p $W /root/models /root/shared/{artifacts,results,locks,logs}
cd $W
DS_URL=https://fwjydgxpttajiqpfmqtj.supabase.co/storage/v1/object/public/stoa-junction-2026/junction-dataset.zip
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; }
export PATH=$HOME/.local/bin:$PATH
apt-get -qq update >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get -qq install -y unzip aria2 git python3.12-dev build-essential >/dev/null 2>&1
t0=$(date +%s)
( aria2c -q -x 16 -s 16 -d $W -o ds.zip "$DS_URL" && echo "dl $(( $(date +%s)-t0 ))s" && \
  unzip -q ds.zip -d $W/data && rm -f ds.zip && echo "unzip $(( $(date +%s)-t0 ))s" && touch $W/ds.done ) > $W/setup-ds.log 2>&1 &
( git clone -q /root/ship/code.bundle $W/repo && cd $W/repo && git checkout -q "${BRANCH:-night}" && \
  uv venv -q --python 3.12 /root/venv-stoa && \
  uv pip install -q --python /root/venv-stoa/bin/python -r /root/ship/requirements.lock.txt pytest && \
  uv pip install -q --python /root/venv-stoa/bin/python --no-deps -e . && \
  echo "stoa $(( $(date +%s)-t0 ))s" && touch $W/stoa.done ) > $W/setup-stoa.log 2>&1 &
( uv venv -q --python 3.12 /root/venv-vllm && \
  uv pip install -q --python /root/venv-vllm/bin/python "vllm==${VLLM_VERSION:-0.31.0}" "huggingface_hub[hf_xet]" && \
  echo "vllm $(( $(date +%s)-t0 ))s" && touch $W/vllm.done && \
  /root/venv-vllm/bin/hf download Qwen/Qwen3-VL-8B-Instruct --local-dir /root/models/qwen3-vl-8b >/dev/null && \
  echo "model $(( $(date +%s)-t0 ))s" && touch $W/model.done ) > $W/setup-vllm.log 2>&1 &
# the LoRA tar is uploaded in parallel and extracted by run_full.sh
wait
ls $W/*.done; cat $W/setup-*.log
