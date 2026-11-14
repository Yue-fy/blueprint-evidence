# deploy/ — run it anywhere: batch CLI, API-only mode, Docker, public demo (owner: P1)

Our system normally runs on one GPU box (local Qwen3-VL-8B + LoRAs on :8001, Qwen3.6-35B on :8002). This folder makes
it portable: **API-only mode** needs no GPU (local model steps go to Gemini), **a batch CLI** answers a whole
questions file, and a **CPU Docker image** packages both, plus the web demo behind a shared password.

| File | What |
|---|---|
| `Dockerfile` | `--target api`: CLI + HTTP API, CPU, `STOA_MODE=api`. `--target demo`: + web app + Caddy password |
| `Dockerfile.dockerignore` | keeps `.git`, `node_modules`, `.env` out of the build context (BuildKit) |
| `start-demo.sh`, `Caddyfile` | demo container: API :8100 + web :5173 + password on `$PORT` (8080) |
| `subset.py` | copies the ~25 drawings the demo shows (+ siblings, questions) out of the 12 GB dataset |
| `cloudrun.sh`, `cloudbuild.yaml` | Google Cloud Run deploy (the captain runs it) |
| `watchdog.sh`, `watchdog.demo.conf` | keeps the stable demo alive: restarts a service only when it is dead (section 0) |
| `gate.py` | optional shared-password proxy for a public link (not needed since 12:52) |
| `pack_state.py` | the runtime state the image needs (self-contained TRAIN split, gitignored `deploy/state/`) |
| `vm.sh` | the same on any Docker VM (Vultr etc.) |

Nothing here contains a key. Keys come from the environment (`GEMINI_API_KEY`, `DEMO_PASSWORD`); on Cloud Run from
Secret Manager. Never commit `.env` files or bake keys into an image.

## 0. Keep the stable demo alive: watchdog (captain, 14 Nov 12:44 / 12:52)

Stoa tests on the captain's laptop against the stable demo (web :5180, API :8100) for hours. `watchdog.sh` +
`watchdog.demo.conf` (user space, no systemd) restart a service **only when it is dead**, never a healthy one:

- nothing listens on its port for 2 checks (15 s apart) → started again with its original command and cwd;
- it listens but its health check fails 3 times in a row (no reply in 20 s, connection error or 5xx: a hung
  process) → only the process holding that port is stopped (TERM, KILL after 10 s) and started again;
- a slow but answering service (any 2xx–4xx reply) is never touched; 1–2 failures are only logged.
- 120 s grace after a start, at most 5 starts per service per 10 min. `WATCHDOG_KILL_HUNG=0` = log only for hung ones.
- Every 5 min, end-to-end probes (log only): the API answers a cached tour question (46 doors), web → /api/health.
- Log: `$JUNCTION_SHARED/logs/watchdog.log` (state changes, probes, an all-ok heartbeat every 5 min); service output
  of restarted processes in `logs/watchdog/<name>.log`.

Run it from the captain's shell (the one that started the demo: API keys, `STOA_DATA`), because a restarted service
inherits the watchdog's environment. Frozen copies in `$JUNCTION_SHARED/captain-demo/p1/` (worktree edits never change
what runs):

```bash
cd $JUNCTION_SHARED/captain-demo/p1 && nohup setsid ./watchdog.sh >/dev/null 2>&1 &
tail -f $JUNCTION_SHARED/logs/watchdog.log    # 'watchdog start …', then 'check api=ok:3ms web=ok:2ms' every 5 min
kill <pid from the 'watchdog start pid N' line>   # stop the watchdog; the services keep running
```

Tested on throwaway ports (14 Nov): started services from nothing; a killed API back in ~30 s; a hung server (accepts,
never replies) declared dead after 3 failed checks, stopped and restarted; a slow (1.5 s) and a 404 server left alone;
the exact demo lines (`api`, `web`) started on spare ports answer like :8100 / :5180; a probe logged `FAIL http=500`
for an API whose /health was 200 but /ask failed.

`gate.py` (a shared-password reverse proxy for a public Tailscale Funnel link) is kept but no longer needed. If a
public link is ever wanted: `STOA_GATE_PASSWORD=… python deploy/gate.py --upstream http://127.0.0.1:5180 --port 8190`,
then CHECK that the gate (not something else) owns the port: `curl -s http://127.0.0.1:8190/__gate/health` must print
`{"gate": "ok", ...}`, and only then `tailscale funnel --bg --https=10000 http://127.0.0.1:8190` (443 / 8443 on this box
are used by another project; never `tailscale funnel reset`). The gate used :8140 until 18:10 on 14 Nov; V1's door
detector (no password) listens there now, so an old command would have published it. Tested through it: browser sign-in, uploads, a 12 MB IFC byte-exact, /api.

## 1. Batch answers (what Stoa can run on their hidden set)

Input = the dataset's `questions.jsonl` format: one `{"path": "data/<source>/<file>", "question": "..."}` per line
(other fields are kept). `path` is relative to `--root` (default: the input file's folder if the paths exist there,
else `$STOA_DATA`); absolute paths work too.

```bash
# on our GPU box (local models)
.venv/bin/python -m stoa.cli answer --in questions.jsonl --out answers.jsonl --workers 4
# anywhere without a GPU (API-only mode; needs GEMINI_API_KEY)
.venv/bin/python -m stoa.cli answer --in questions.jsonl --out answers.jsonl --api
# one question
.venv/bin/python -m stoa.cli ask data/swiss_dwellings/site1000_bldg2082_floor9385.pdf "How many doors?" [--api]
```

Each output line = the input row + `value`, `unit`, `confidence`, `evidence` (file, page, bbox + bbox_space, or IFC
GUID), `assumptions`, `missing`, `solver`, `cost` and `error` (`--trace` adds the reasoning steps). An input field we
would overwrite (e.g. the reference `unit`) is kept as `input_unit`. Re-running with the same `--out` resumes;
`--fresh` starts over. The last line on stderr gives the total cost split into GPU / API / CPU.

With Docker (CPU image, API-only mode):

```bash
.venv/bin/python deploy/pack_state.py      # once: runtime state the image needs (gitignored deploy/state/)
docker build -f deploy/Dockerfile --target api -t stoa:api .
docker run --rm -e GEMINI_API_KEY -v /path/to/dataset:/data/stoa:ro -v "$PWD/out":/out stoa:api \
  python -m stoa.cli answer --in /data/stoa/questions.jsonl --out /out/answers.jsonl --workers 2
docker run --rm -p 8100:8100 -e GEMINI_API_KEY -v /path/to/dataset:/data/stoa:ro stoa:api   # HTTP API (POST /ask)
```

### File formats (solver `upload`, the CLI default since 14 Nov 13:40)

`ask` and `answer` run solver `upload` = the pipeline behind `stoa/formats/normalize.py`, so any of these can be the
`path` (alone: no dataset folder, no sibling files needed; files that need no conversion get exactly the pipeline's
answer):

| Upload | What happens | Lone-copy probe (dev questions) |
|---|---|---|
| PDF, 1 page (vector) | pipeline as is | Swiss 100%, IFC plan sheets 53-86% |
| PDF, several pages | answered from the page `pick_page` picks (sheet number / quoted storey in the title, else most drawing); evidence on that page | 2-page sets 93.3%, plan on page 3 100%; right storey sheet 455/455 |
| PDF, scanned (image-only page) | page rendered to PNG at the scan's resolution → image readers; evidence back in PDF points | = the PNG: Swiss scan 55%, CubiCasa 75% |
| PNG / JPG | pipeline as is | CubiCasa JPG 76.7% |
| IFC | pipeline as is (IFC reader) | 96.7%, 56 MB models 100% |
| DXF | dxf reader; other layer names (AIA `A-DOOR`, German `Tür`, …) read through a renamed copy; no role layer → conf ≤ 0.2 + "missing" | Swiss 96.7%; AIA names 100%; PDF + renamed DXF 98.9% (`up_fmt5`) |
| SVG from IfcConvert (`class="Ifc…"`) | `svg_ifc` (counts by IFC class, room areas, wall lengths); other questions → as a vector PDF | 80.0% (354 dev sheets: areas 96.1%) |
| SVG, other | converted to a vector PDF (paths + text kept) | = the PDF (Swiss 100%); text drawn as outlines: low confidence + "export with text" |
| DWG | LibreDWG `dwg2dxf` (team box: `~/.local/bin`, 0.14) / ODA File Converter → `<stem>.dwg.dxf`, converter artefacts repaired (UTF-8 text, aligned text at 0,0) → dxf reader | 100% (`up_fmt6`, 60 rows); Revit DWGs convert in 1–10 s; no converter: "save as DXF or print to PDF" |
| HEIC / HEIF (iPhone) | → upright JPEG with `pillow-heif` (`uv pip install -e .[formats]`; in the team venv) | = the JPG (3/3); no decoder: "export as JPEG" |
| PDF no PDF reader can read (text as outlines, no scale; not an IFC-exported sheet) | `upload` renders the page (200 dpi without a text layer, else 150) → image readers; evidence in PDF points; conf ≤ 0.5 | X1 wild set 34.8 → 44.6%; slow: p50 ~17 s on those files |

Each conversion adds one sentence as `assumptions[0]` (e.g. "multi-page PDF (37 pages): answered from page 18 …").
Probe splits: `results/splits/up_fmt.jsonl` … `up_fmt6.jsonl` (`experiments/p1/formats/build*.py`): formats, generic SVG + sets,
scans, renamed-layer DXF, PDF + renamed DXF, DWG.

## 2. API-only mode (`STOA_MODE=api`, or `--api`)

`stoa/llm.py` `api_route()`: every call meant for our local servers goes to a hosted model instead; nothing else
changes (the deterministic DXF / IFC / schedule / CV paths answer exactly as before: 67% of dev questions use no
model at all).

| Variable | Default | Meaning |
|---|---|---|
| `STOA_MODE` | (local) | `api` = no local GPU model |
| `STOA_API_BACKEND` | `gemini` | hosted backend for local steps (`nvidia` also works) |
| `STOA_API_MODEL` | `gemini-3-flash-preview` | the cheap model for the local VLM steps (thinking `minimal`) |
| `STOA_API_MODEL_TEXT` | = `STOA_API_MODEL` | model for the `local2` free-text steps (e.g. `gemini-3.1-pro-preview`) |
| `STOA_API_LOCAL_ONLY` | `cc-v2,cc-v3` | local-only adapters (the CubiCasa counting LoRAs) |
| `STOA_API_MODEL_LORA` | = `STOA_API_MODEL` | hosted model that answers the adapters' prompt; `none` = fail fast (solver abstains) |
| `GEMINI_MAX_CALLS_PER_DAY`, `GEMINI_RPM` | 200, 10 | Google's no-abuse guard (stoa/llm.py), per process host / log |

Explicit hosted calls (the general agent's Gemini 3.1 Pro planner, the Nemotron verifier) keep their own model.

**Measured (14 Nov, 24 dev questions that use the local LLM; 46 Gemini calls in all):**

| | scored acc (16) | CubiCasa counts (10) | schedules (6) | $ / 1k questions | mean wall s |
|---|---|---|---|---|---|
| local (Qwen3-VL-8B + counting LoRA) | **87.5%** | 8/10 | 6/6 | **$0.22** (GPU $0.17 + CPU $0.05) | 3.5 |
| API mode, default (Gemini 3 Flash, also standing in for the LoRA) | 81.3% | 7/10 | 6/6 | $3.62 (API $3.20 + CPU $0.42) | 30 |
| API mode, LoRA step on Gemini 3.1 Pro (`STOA_API_MODEL_LORA=gemini-3.1-pro-preview`) | (cc only) | 8/10 | | CubiCasa rows $7.40 vs Flash $0.80 | 17 |
| API mode, no stand-in (`STOA_API_MODEL_LORA=none`) | 62.5% | 4/10 | 6/6 | $3.62 | 27 |

Runs (artifacts/eval/): local `20261114-122327-pipeline-api_sample24-b8ef`, API no stand-in `-122401-…-735b`,
CubiCasa with Flash / Pro stand-in `-123655-pipeline-api_sample_cc10-1579` / `-123802-…-2255`; the default row
combines 1579 (CubiCasa) with 735b (the other 14 rows, which the stand-in does not touch). Sample:
`experiments/p1/make_sample.py` (seed 14). n is tiny (10 CubiCasa questions): read the CubiCasa column as
"Flash recovers most of the LoRA's lead over the CV fallback", not as a measured difference.
Without the stand-in the pipeline falls back to the CubiCasa CV path, which was wrong at conf 0.6–0.8 on 3 rows; the
stand-in's answers all get the solver's constant confidence 0.3 (no logprobs from Gemini; 7/10 were right, so
under-confident). Schedule answers are identical; IFC free-text answers change wording only. Wall time is mostly the
10 calls/min Gemini throttle. Three API-mode CubiCasa rows in 735b reused room-label outputs the local model cached
earlier on this machine (billed as GPU; patch for O1: `experiments/p1/api_mode_raster_cache.patch`).
**Estimate for the full dev mix** (S0's dev run a0fc, 2,215 questions, 67% deterministic; LLM rows re-priced with the
default API-mode mean of their source): **$0.56 vs $0.030 per 1k questions (~18x)**; the accuracy loss sits on raster
plans, not on vector / IFC / schedule questions.

## 3. Public demo (the captain deploys; agents don't)

```bash
# 1. the drawings the demo shows (~25 files, 30 MB, + their questions) -> a folder outside git; runtime state
.venv/bin/python deploy/subset.py --out /path/to/stoa-demo-data
.venv/bin/python deploy/pack_state.py
# 2a. Google Cloud Run (the temporary Google account includes it): prompts for the two secrets once
PROJECT=<gcp-project-id> DATA_DIR=/path/to/stoa-demo-data deploy/cloudrun.sh
# 2b. or a VM with Docker (Vultr...): keys in ~/stoa.env (GEMINI_API_KEY=..., DEMO_PASSWORD=...), chmod 600
DATA_DIR=~/stoa-demo-data deploy/vm.sh
```

The demo URL asks for user `stoa` + the shared password (`/healthz` is open for health checks). Cloud Run runs one
instance (`--max-instances 1`) so the 200-calls-a-day Gemini cap is one counter; the counter lives in the container
(`/var/stoa/logs/gemini_calls.jsonl`) and restarts with it. If the organisation policy forbids
`--allow-unauthenticated`, open it with `gcloud run services proxy stoa-demo --region europe-north1` instead.
Delete the service and the bucket after the event (the script prints the two commands). Credit the drawings'
licences in the demo (dataset README is copied next to the data by `subset.py`).

## 4. Honest cost (`stoa/cost.py`)

`usd = gpu_usd + api_usd + cpu_usd`, shown per question in `answer` / `ask` and per 1k questions in every eval
summary (`cost per 1k: GPU $… API $… CPU $…`, and `rates` in the summary JSON).

- **GPU**: billed GPU seconds × `STOA_GPU_USD_PER_HOUR` (default **$2.00/h**, one 96 GB GPU cloud-rental equivalent).
  GPU seconds come from tokens (prefill 25k tok/s, decode 1k tok/s on the shared vLLM), not from latency, which
  includes queueing behind other users.
- **API**: tokens × the model's list price, recorded per call (`api_usd`): Gemini 3 Flash $0.50 / $3.00 per 1M
  in / out, Gemini 3.1 Pro $2 / $12, Nemotron (credit-based, no list price) $0.40 / $1.60 assumed. Override with
  `STOA_API_PRICES='{"model": [in, out]}'`. Gemini's thinking tokens are billed as output (its OpenAI endpoint leaves
  them out of `completion_tokens`; we bill `total - prompt`).
- **CPU**: wall seconds × `STOA_CPU_USD_PER_HOUR` (default $0.05/h).
- **Tier badge** (Stoa judges cost on 3 tiers, not dollars): `stoa.cost.tier(cost)` → `cheap` (no AI model: geometry,
  IFC, text layer, CPU vision) / `in-between` (a few small-model calls: our 8B / LoRA, Gemini Flash) / `expensive`
  (> 30k tokens, > 6 calls, or > $0.003 of hosted calls, e.g. Gemini Pro or OCR of a whole scan), with a one-line
  reason. In `answer` rows (`cost.tier`), `ask`, and the eval summary (`cost tiers:` line, `cost_tier3`).
  Dev (S0 run a0fc): cheap 67% (acc 93%), in-between 33% (82%), expensive 0%.

## Known gaps / not tested here

- Multi-page PDFs are answered from ONE page: a question about the whole set ("how many doors in the building?" on a
  set of storey sheets) is answered from the page picked, not summed over pages.
- DWG: the probe DWGs were written by LibreDWG itself (dxf2dwg); AutoCAD-written DWGs are only checked to convert
  (5 Revit sheets from data/opening_design, 0.8–9.4 s), not scored (no questions on them).

- A clean container needs `deploy/pack_state.py` first (the router reads the TRAIN split from `$JUNCTION_SHARED`):
  without it every question failed (simulated: fresh py3.12 venv from pyproject only + empty state + 30 MB subset,
  no API key → with the state 32/32 answered, 24/24 scored right; pytest 448 in that venv).
- No Docker or gcloud on the team box: the image build, Caddy and Cloud Run script are written, not run. First build:
  check `docker build -f deploy/Dockerfile --target api .` and `docker run … stoa:api python -m stoa.cli ask …`.
- `raster_cc5k` live inference needs `--build-arg WITH_CC5K=1` + the weights mounted (`CC5K_WEIGHTS`, CC BY-NC 4.0);
  without them it falls back to its precomputed counts (not in the image) or abstains.
- Precomputed caches (W1 IFC index / KG, raster text layers) are not in the image; they are rebuilt on first use
  (first IFC question on a model takes longer).
- The web app's Accuracy report reads `$JUNCTION_SHARED/results/leaderboard.jsonl` (`STOA_LEADERBOARD`), which is
  not in the image; mount one if the demo should show it.
