#!/usr/bin/env bash
# (Re)start vLLM on the Verda instance: Qwen3-VL-8B + LoRAs cc-v2/cc-v3 (or $LORAS: 'name=dir name=dir'). Extra vLLM flags via $EXTRA; log $LOG.
export PATH=/root/venv-vllm/bin:$PATH VLLM_ALLOW_RUNTIME_LORA_UPDATING=True
# Confidential-computing GPUs (Verda *.CC types): vLLM 0.31's v2 model runner lets GPU kernels read pinned host
# memory directly (UvaBuffer), which CC mode cannot do -> every output is garbage ('!!!?,?,?'). The v1 runner
# copies explicitly and is correct. CC also needs `nvidia-smi conf-compute -srs 1` (after attestation) first.
export VLLM_USE_V2_MODEL_RUNNER=${VLLM_USE_V2_MODEL_RUNNER:-0}
LOG=${LOG:-/root/work/vllm.log}
for p in $(pgrep -f "venv-vllm/bin/vllm"); do kill $p; done; sleep 3
setsid nohup /root/venv-vllm/bin/vllm serve /root/models/qwen3-vl-8b --host 127.0.0.1 --port 8001 \
  --served-model-name qwen3-vl-8b default --gpu-memory-utilization ${GPU_UTIL:-0.90} --max-model-len 32768 \
  --max-num-seqs ${MAX_SEQS:-128} --limit-mm-per-prompt '{"image": 8, "video": 0}' --trust-remote-code \
  --enable-lora --max-lora-rank 64 --max-loras 4 --lora-modules ${LORAS:-cc-v2=/root/lora/cc-v2 cc-v3=/root/lora/cc-v3} \
  ${EXTRA:-} > $LOG 2>&1 < /dev/null &
for i in $(seq 1 90); do curl -s -m 2 127.0.0.1:8001/v1/models >/dev/null && break; pgrep -f "venv-vllm/bin/vllm" >/dev/null || { echo died; tail -5 $LOG; exit 1; }; sleep 5; done
curl -s 127.0.0.1:8001/v1/chat/completions -H "content-type: application/json" \
  -d '{"model":"default","messages":[{"role":"user","content":"What is 2+3? Answer with one number."}],"max_tokens":12,"temperature":0}' \
  | python3 -c "import json,sys; print('TEST:', repr(json.load(sys.stdin)['choices'][0]['message']['content']))"
