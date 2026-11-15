# Path to production on Verda (V1, Sat 14 Nov)

What we run today: a web UI + FastAPI (`stoa.api`) + deterministic CAD/IFC/PDF readers on CPU + one vLLM server
(Qwen3-VL-8B + counting LoRAs `cc-v2`/`cc-v3`). The app reaches the model through two env vars:
`LLM_BASE_URL` (OpenAI-compatible) and, new in cycle 2, `LLM_API_KEY` (Bearer token; unset = no auth as before).

## Target architecture

| Piece | Verda product | Why |
|---|---|---|
| VLM endpoint (Qwen3-VL-8B + LoRAs) | **Serverless Containers**, public `vllm/vllm-openai:v0.31.0` image, 1× L40S 48 GB per replica | Scale to zero between customers, per-minute billing, autoscale on queue depth; 8B bf16 + 2 adapters fits 48 GB. |
| Adapter weights (175 MB each) | **Object Storage** (S3, FIN-03), private bucket, presigned URL in a Verda **secret** | No public model hub; weights and drawings never leave the EU. |
| Batch benchmark / re-scoring | **GPU instance**, spot `1RTXPRO6000.30V` (€1.42/h) or on-demand | The full 127k-question gate runs in ~17 min on one GPU. |
| CPU readers (DXF, IFC, PDF geometry) | CPU container pool (`CPU.16V.64GC`, $0.21/h serverless) | The batch run was CPU-bound (30/30 vCPU busy, GPU KV cache 3%): scale CPU workers separately from the GPU. |
| Confidential drawings | `.CC` instance types with NVIDIA attestation (measured in cycle 1) | Customer data encrypted while in use. |

Serverless prices from `GET /v1/container-types` (14 Nov, USD/h per replica): L40S 2.22 (spot 1.11), RTX PRO 6000
3.12, H100 5.77; CPU 16 vCPU 0.21. Available at 15:05: L40S ×1, H100 ×1–2, B300 ×1; RTX PRO 6000 none.

## Deployment, scripted (`deploy/verda/serverless.py`, stdlib only)

1. `storage-init`: private bucket `junction-hannah-v1-<hex>` in FIN-03 plus an S3 key pair, saved to
   `~/.config/junction-verda/s3.json` (mode 0600, never printed).
2. `upload lora.tar`: SigV4 PUT (`deploy/verda/s3.py`, path-style, `objects.eu-north-3.verda.storage`), then a 7-day
   presigned GET stored as the secret `junction-lora-url`.
3. `create L40S 0 2`: container `junction-vlm`. The start command downloads and untars the adapters to scratch
   `/data`, then runs `vllm serve Qwen/Qwen3-VL-8B-Instruct --enable-lora --lora-modules cc-v2=… cc-v3=…`.
   Health check `GET :8000/health`. Scaling: queue load 1, 64 concurrent requests per replica, scale-down after 300 s.
   `--no-lora` deploys the base model only (no bucket needed).
4. App: `LLM_BASE_URL=https://containers.verda.com/junction-vlm/v1 LLM_API_KEY=$VERDA_INFERENCE_API_KEY`.
5. `delete` / `storage-delete` remove everything.

**Status: not run yet.** At ~15:08 the agent's permission classifier blocked creating the bucket and access key and
the container deployment (new credentials and a new billed resource type). It needs a human OK. The measured
stand-in is from cycle 1: the same vLLM on a Verda GPU instance behind an SSH tunnel, switched with `LLM_BASE_URL`
alone (smoke CubiCasa 65.0% vs 63.7% local, ping 9–22 ms from Espoo).

## What to measure once approved (≈ €1, < 30 min)
- Cold start, scale from zero to healthy: image pull (cached Docker Hub) + 17 GB HF download + 350 MB adapters + load.
- Warm p50/p95 per pipeline question vs the local :8001 server (smoke, 206 Qs), same answers (accuracy within noise).
- Autoscale: 64 → 256 concurrent requests with `max_replica_count 2`, measure the time until a second replica serves.
- Then `scale 0 0`, `delete`, `storage-delete`; balance before and after.

## Door/window detector endpoint (measured, Sat 14 – Sun 15 Nov)

Door and window counts on uploaded sheets come from our own Faster R-CNN detector (`experiments/v1/det_server.py`).
On our shared CPU box a NEW full sheet takes minutes (A-101F 6,000 px: 227 s cold at load ~120), but the readers wait
25 s (`openings`, `STOA_DR_DET_TIMEOUT`) or 40 s (`door_detect`) per question, so a fresh upload falls back to a VLM
guess. The same service with `DET_REMOTE_URL` sends each cache miss (a grey PNG) to a GPU twin on a Verda instance
and gets the raw boxes back: identical boxes, 5–15 s per new sheet (whole AFCEC A-2 at 10,200 × 6,600 px: 4.9 s),
model checksum checked per request (mismatch → 409 → local CPU), any failure → local CPU.

- One command from a fresh instance to a serving twin + local tunnel: `IP=<ip> MODEL=<model.pt> PORT=8154
  bash deploy/verda/det_twin_up.sh`. Measured 00:36–00:41 Sun on the kept A100 (blank directory, new venv): 293 s
  end to end = 20 s venv (torch wheels from the instance's uv cache; a truly fresh instance downloads ~3 GB inside
  the datacenter) + 255 s uploading the 166 MB model over our ~0.7 MB/s uplink + 12 s twin start + 3 s tunnel. A local
  service pointed at it reproduced v5's 79 boxes on the AFCEC north wing exactly, 2.0 s cold. Production would pull the
  model from an EU bucket next to the GPU instead of uploading it.
- Privacy: an offloaded upload leaves this machine for our EU instance. T2's netguard refuses offload ports for
  Private answers; the clean set-up is two servers on the same model + cache: a local-only one for Private requests
  and cache hits, an offload one for the rest.
- What the demo runs (S0 + V1, 01:47 Sun): v5 LOCAL on CPU, no offload. Measured 01:41 through the real web upload path
  in Private mode on a dedicated idle CPU service with an empty cache: Stoa's AFCEC north wing 34 doors (= the expert's
  count) in 8.0 s, because the reader sends the detector only the wing crop. The cache is keyed by image content, so
  a re-upload of the same sheet is a hit (3.8 s). The twin pays off for whole large NEW sheets on a loaded CPU.
- Production form: the twin is stateless (model in the image or a private bucket), so it maps onto a Serverless
  Container on a small GPU (scale to zero between customers) behind the same `DET_REMOTE_URL`.
