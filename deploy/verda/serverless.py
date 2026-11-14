"""Verda Serverless Containers prototype: our VLM (Qwen3-VL-8B + counting LoRAs cc-v2/cc-v3) as an autoscaling,
scale-to-zero, EU-hosted OpenAI-compatible endpoint. The adapters live in a private Verda Object Storage bucket
(FIN-03); the container downloads them at start-up through a presigned URL held in a Verda secret. The base model is
pulled from Hugging Face. Stdlib only. Never prints keys or the presigned URL.

  python deploy/verda/serverless.py storage-init                # bucket + S3 access key -> ~/.config/junction-verda/s3.json
  python deploy/verda/serverless.py upload <lora.tar>           # PUT the adapters tarball, store presigned URL as secret
  python deploy/verda/serverless.py create [L40S] [min] [max] [--no-lora]  # create deployment junction-vlm
                                                                # (--no-lora: base model only, no bucket needed)
  python deploy/verda/serverless.py status | replicas | logs | syslogs | get
  python deploy/verda/serverless.py scale <min> <max> [scale_down_delay_s]
  python deploy/verda/serverless.py delete                      # delete the deployment (billing stops)
  python deploy/verda/serverless.py storage-delete              # delete object, bucket, secret and access key

Then: LLM_BASE_URL=<endpoint_base_url>/v1 LLM_API_KEY=$VERDA_INFERENCE_API_KEY  (stoa/llm.py "local" backend).
"""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s3  # noqa: E402
import verda  # noqa: E402

NAME = os.environ.get("VERDA_DEPLOYMENT", "junction-vlm")
SECRET = "junction-lora-url"
OBJ = "lora/lora.tar"
IMAGE = os.environ.get("VLLM_IMAGE", "docker.io/vllm/vllm-openai:v0.31.0")  # same vLLM as our box and the batch run

START_BASE = r"""exec vllm serve Qwen/Qwen3-VL-8B-Instruct --host 0.0.0.0 --port 8000 \
  --served-model-name qwen3-vl-8b default --gpu-memory-utilization 0.90 --max-model-len 32768 \
  --max-num-seqs 64 --limit-mm-per-prompt '{"image": 8, "video": 0}'
"""

START = r"""set -e
t0=$(date +%s)
mkdir -p /data/lora /data/hf
python3 - <<'PY'
import os, tarfile, urllib.request
with urllib.request.urlopen(os.environ["LORA_URL"], timeout=600) as r:
    tarfile.open(fileobj=r, mode="r|").extractall("/data/lora", filter="data")
PY
echo "[junction] adapters ready after $(( $(date +%s) - t0 )) s: $(ls /data/lora)"
exec vllm serve Qwen/Qwen3-VL-8B-Instruct --host 0.0.0.0 --port 8000 \
  --served-model-name qwen3-vl-8b default --gpu-memory-utilization 0.90 --max-model-len 32768 \
  --max-num-seqs 64 --limit-mm-per-prompt '{"image": 8, "video": 0}' \
  --enable-lora --max-lora-rank 64 --max-loras 4 --lora-modules cc-v2=/data/lora/cc-v2 cc-v3=/data/lora/cc-v3
"""


def _keyfile() -> dict:
    return json.loads(s3.KEYFILE.read_text())


def req(method: str, path: str, body=None):
    return verda._req(method, path, body, token=verda.token())


def deployment_body(gpu: str, mn: int, mx: int, lora: bool = True) -> dict:
    env = [{"name": "HF_HOME", "value_or_reference_to_secret": "/data/hf", "type": "plain"}]
    if lora:
        env += [{"name": "LORA_URL", "value_or_reference_to_secret": SECRET, "type": "secret"},
                {"name": "VLLM_ALLOW_RUNTIME_LORA_UPDATING", "value_or_reference_to_secret": "True", "type": "plain"}]
    return {
        "name": NAME,
        "container_registry_settings": {"is_private": False},
        "containers": [{
            "image": IMAGE,
            "should_use_cached_image": True,
            "exposed_port": 8000,
            "healthcheck": {"enabled": True, "port": 8000, "path": "/health"},
            "entrypoint_overrides": {"enabled": True, "entrypoint": ["/bin/bash", "-c"], "cmd": [START if lora else START_BASE]},
            "env": env,
            "volume_mounts": [{"type": "scratch", "mount_path": "/data"},
                              {"type": "memory", "mount_path": "/dev/shm", "size_in_mb": 8192}],
        }],
        "compute": {"name": gpu, "size": 1},
        "scaling": {
            "min_replica_count": mn, "max_replica_count": mx,
            "scale_down_policy": {"delay_seconds": 300}, "scale_up_policy": {"delay_seconds": 0},
            "queue_message_ttl_seconds": 900, "concurrent_requests_per_replica": 64,
            "scaling_triggers": {"queue_load": {"threshold": 1},
                                 "cpu_utilization": {"enabled": False, "threshold": 90},
                                 "gpu_utilization": {"enabled": False, "threshold": 90}},
        },
        "is_spot": False,
    }


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    ts = time.strftime("%H:%M:%S")
    if cmd == "storage-init":
        s3.KEYFILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if s3.KEYFILE.exists():
            raise SystemExit(f"{s3.KEYFILE} exists; refusing to create a second key")
        bucket = "junction-hannah-v1-" + pysecrets.token_hex(3)
        req("POST", "/object-storage/buckets", {"bucket_name": bucket, "location_code": "FIN-03"})
        k = req("POST", "/object-storage/access-keys", {"name": "junction-v1"})
        fd = os.open(s3.KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"bucket": bucket, "id": k["id"], "access_key": k["access_key"],
                       "secret_key": k["secret_key"]}, f)
        print(ts, "bucket", bucket, "created in FIN-03; access key id", k["id"], "saved to", s3.KEYFILE)
    elif cmd == "upload":
        kf = _keyfile()
        t0 = time.time()
        s3.put(kf["bucket"], OBJ, rest[0])
        print(ts, f"upload {time.time() - t0:.0f} s")
        url = s3.presign(kf["bucket"], OBJ, 604800)
        names = {s.get("name") for s in (req("GET", "/secrets") or [])}
        if SECRET in names:
            req("DELETE", f"/secrets/{SECRET}")
        req("POST", "/secrets", {"name": SECRET, "value": url})
        print(ts, "secret", SECRET, "set (presigned GET, 7 days)")
    elif cmd == "create":
        lora = "--no-lora" not in rest
        rest = [a for a in rest if a != "--no-lora"]
        gpu = rest[0] if rest else "L40S"
        mn, mx = (int(rest[1]), int(rest[2])) if len(rest) > 2 else (1, 1)
        r = req("POST", "/container-deployments", deployment_body(gpu, mn, mx, lora))
        print(ts, "created", NAME, gpu, f"min={mn} max={mx} lora={lora}", "endpoint:", (r or {}).get("endpoint_base_url"))
    elif cmd == "get":
        r = req("GET", f"/container-deployments/{NAME}")
        for c in r.get("containers", []):
            c.pop("entrypoint_overrides", None)
        print(json.dumps(r, indent=1)[:3000])
    elif cmd == "status":
        print(ts, json.dumps(req("GET", f"/container-deployments/{NAME}/status")),
              json.dumps(req("GET", f"/container-deployments/{NAME}/replicas")))
    elif cmd == "replicas":
        print(ts, json.dumps(req("GET", f"/container-deployments/{NAME}/replicas"), indent=1))
    elif cmd in ("logs", "syslogs"):
        path = "logs" if cmd == "logs" else "system-logs"
        r = req("GET", f"/container-deployments/{NAME}/{path}")
        rows = r if isinstance(r, list) else (r or {}).get("logs", r)
        for row in (rows or [])[-int(rest[0]) if rest else -60:]:
            print(json.dumps(row)[:400] if not isinstance(row, str) else row[:400])
    elif cmd == "scale":
        body = {"min_replica_count": int(rest[0]), "max_replica_count": int(rest[1])}
        if len(rest) > 2:
            body["scale_down_policy"] = {"delay_seconds": int(rest[2])}
        req("PATCH", f"/container-deployments/{NAME}/scaling", body)
        print(ts, "scaling", json.dumps(req("GET", f"/container-deployments/{NAME}/scaling")))
    elif cmd == "delete":
        req("DELETE", f"/container-deployments/{NAME}")
        print(ts, "deleted", NAME, "->", [d.get("name") for d in req("GET", "/container-deployments") or []])
    elif cmd == "storage-delete":
        kf = _keyfile()
        try:
            print(s3._signed_request("DELETE", kf["bucket"], OBJ)[0])
        except Exception as e:  # noqa: BLE001
            print("object delete:", e)
        req("DELETE", f"/object-storage/buckets/{kf['bucket']}")
        if SECRET in {s.get("name") for s in (req("GET", "/secrets") or [])}:
            req("DELETE", f"/secrets/{SECRET}")
        req("DELETE", f"/object-storage/access-keys/{kf['id']}")
        s3.KEYFILE.unlink()
        print(ts, "storage, secret and key deleted")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
