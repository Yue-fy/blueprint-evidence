# Configuration

Every setting is a plain environment variable. Nothing is required for the bundled samples. The main ones are
in `.env.example`, with comments. This page lists **every** variable the code reads, with its default. It is
generated from the code by the release export (`release/export.py` in the team workspace), so it matches
this commit. Most of the variables in the second table are switches we used in experiments and evals: the
defaults are what produced the results in [RESULTS.md](RESULTS.md).

## Described settings

| Variable | Default | What | Read in |
|---|---|---|---|
| `AGENT_ID` | empty | optional; recorded in leaderboard lines | `stoa/eval/runner.py`, `stoa/llm.py` |
| `CC5K_PYTHON` | `.venv-cc5k/bin/python` / `a.PY` | Python with torch for live CPU inference (default: `.venv-cc5k/bin/python`); cached counts need none | `stoa/extras/cc5k_model/adapter.py`, `stoa/extras/room_graph/graph.py`, `stoa/solvers/raster_cc5k/__init__.py` (+1) |
| `CC5K_THREADS` | `6` | optional; CPU threads for live inference (default 6) | `stoa/extras/cc5k_model/predict.py` |
| `CC5K_WEIGHTS` | `.stoa/models/cc5k/model_best_val_loss_var.pkl` | pretrained CubiCasa5K weights `.pkl` (CC BY-NC 4.0, outside git) | `stoa/extras/cc5k_model/predict.py` |
| `GEMINI_API_KEY` | unset | your own Google AI Studio key; escalation only, keep it out of bulk evals | `stoa/eval/runner.py`, `stoa/llm.py`, `stoa/solvers/piping.py` |
| `GEMINI_MAX_CALLS_PER_DAY` | `200` | optional; per-day call cap for Gemini | `stoa/llm.py` |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini model for hosted steps (Gemini 3.1 Pro is chosen explicitly for hard cases) | `stoa/llm.py` |
| `JUNCTION_SHARED` | `.stoa` / `/root/shared` / … | local state folder (splits, leaderboard, caches, artifacts); default `.stoa` | `deploy/verda/lone_split.py`, `deploy/verda/quick_report.py`, `deploy/verda/report.py` (+31) |
| `JUNCTION_VENV` | unset | Python env (`$JUNCTION_VENV/bin/python`) | `web/mock/stoaApi.ts` |
| `LLM2_API_KEY` | unset | optional Bearer token for an authenticated `local` / `local2` endpoint (e.g. Verda serverless); unset = no auth | `stoa/llm.py` |
| `LLM2_BASE_URL` | `http://127.0.0.1:8002/v1` / empty | second local OpenAI-compatible server (`local2` backend, :8002) | `stoa/features/privacy.py`, `stoa/features/takeoff.py`, `stoa/llm.py` (+1) |
| `LLM_API_KEY` | unset | optional Bearer token for an authenticated `local` / `local2` endpoint (e.g. Verda serverless); unset = no auth | `stoa/llm.py` |
| `LLM_BASE_URL` | `http://127.0.0.1:8001/v1` / empty | local OpenAI-compatible server, default `http://127.0.0.1:8001/v1` | `stoa/features/privacy.py`, `stoa/features/takeoff.py`, `stoa/llm.py` (+1) |
| `LLM_MODEL` | `default` | served model name on the local server (`default` = whatever it serves) | `stoa/llm.py` |
| `LLM_NEVER_UP_WAIT` | `15` | seconds a model step waits for a local model server that never answered in this process (default 15) | `stoa/llm.py` |
| `LLM_RESTART_WAIT` | `300` | seconds a model step waits for a local model server that went down and is restarting (default 300) | `stoa/llm.py` |
| `MODEL_DIR` | `.stoa/models` | model weights | `serving/start.sh` |
| `NVIDIA_API_KEY` | unset | NVIDIA hosted API (Nemotron); your own key | `stoa/llm.py` |
| `NVIDIA_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | model on the NVIDIA API (the verifier) | `stoa/llm.py` |
| `STOA_API_BACKEND` | `gemini` | hosted backend for local steps (default gemini) | `stoa/llm.py` |
| `STOA_API_GEMINI` | `1` | HTTP API only: `1` = Gemini 3.1 Pro escalation for unsure answers (per request: `solver_args`) | `stoa/api.py` |
| `STOA_API_LOCAL_ONLY` | `cc-v2,cc-v3` | comma list of local-only model ids (default cc-v2,cc-v3; any "cc-*" counts too) | `stoa/llm.py` |
| `STOA_API_MODEL` | unset | model for the local VLM steps (default gemini-3-flash-preview: the cheap one) | `stoa/llm.py` |
| `STOA_API_MODEL_LORA` | empty | hosted model that answers the local-only adapters' prompts (default = STOA_API_MODEL; "none" = fail fast). No logprobs there, so the solver's confidence falls back to its constant (0.3). | `stoa/llm.py` |
| `STOA_API_MODEL_TEXT` | unset | model for "local2" free-text steps (default = STOA_API_MODEL) | `stoa/llm.py` |
| `STOA_API_VERIFY` | `1` | HTTP API only: `1` = NVIDIA Nemotron second opinion on unsure answers without located evidence | `stoa/api.py` |
| `STOA_CACHE` | `tempfile.gettempdir()` / `tmp` / … | Cache for page renders of read-only inputs (default ~/.cache/stoa; writable inputs get them alongside) | `stoa/evidence/dxfplan.py`, `stoa/features/privacy.py`, `stoa/features/region.py` (+2) |
| `STOA_DATA` | `DEFAULT_DATA` / `junction-dataset` / … | dataset root (contains `questions.jsonl`, `data/`) | `stoa/data.py`, `stoa/extras/cc5k_model/adapter.py`, `stoa/extras/raster_scale/find_areas.py` (+6) |
| `STOA_DWG2DXF` | unset | path of a LibreDWG `dwg2dxf` binary | `stoa/formats/dwg.py` |
| `STOA_GEMINI_ESCALATE` | `0` / `1` | `1` = Gemini escalation / Nemotron verify after the cascade (API/demo default); evals and gates set `0` | `stoa/pipeline.py`, `stoa/solvers/piping.py` |
| `STOA_GEMINI_TEAM_CAP` | `800` | Daily Gemini call caps, kept low on purpose for the hackathon key (counted in .stoa/logs/gemini_calls.jsonl): every call (default 200 per STOA_AGENT name), and the escalation of unsure answers (default 800); raise for your own key | `stoa/agent/escalate.py` |
| `STOA_LLM_CACHE` | unset | `1` = on-disk cache of temperature-0 model responses | `stoa/llm.py` |
| `STOA_MODE` | empty | API-only mode without a local GPU: STOA_MODE=api (see README) | `stoa/cli.py`, `stoa/llm.py`, `web/server/ask_upload.py` |
| `STOA_OCR_PYTHON` | `.venv-ocr/bin/python` | Python of a separate env with PaddleOCR (OCR runs as a subprocess there) | `stoa/ocr/__init__.py` |
| `STOA_TEXT_BACKEND` | empty | backend for free-text model steps (multi-part splits, IFC fact-card answers): `local2`, `verda` | `stoa/agent/private.py`, `stoa/llm.py`, `web/mock/stoaApi.ts` (+1) |
| `STOA_VERIFY` | `0` | `1` = Gemini escalation / Nemotron verify after the cascade (API/demo default); evals and gates set `0` | `stoa/pipeline.py` |
| `VERDA_INFERENCE_API_KEY` | unset | key for Verda Inference (EU-hosted, text only); used with `STOA_TEXT_BACKEND=verda` | `stoa/llm.py` |
| `VERDA_MODEL` | `deepseek-4-1-flash-1017` | model on Verda Inference for `STOA_TEXT_BACKEND=verda` | `stoa/llm.py` |

## Other switches

Tuning and experiment switches. Read the module named in the last column for what each one does.

| Variable | Default | Read in |
|---|---|---|
| `BLUEPRINT_INVERT` | `1` | `web/server/normalize_upload.py` |
| `CC5K_COUNTS` | (computed in code) | `stoa/solvers/raster_cc5k/crosscheck.py` |
| `CC5K_LIVE` | `0` | `stoa/solvers/raster_cc5k/crosscheck.py` |
| `CC5K_MAX_SIDE` | `3000` | `stoa/extras/cc5k_model/predict.py` |
| `CC5K_RESIZE_TO` | `2000` | `stoa/extras/cc5k_model/predict.py` |
| `CC5K_SAVE_MAPS` | unset; `stoa/extras/raster_scale/scale.py` sets `str(LIVE_MAPS)` when unset | `stoa/extras/cc5k_model/predict.py`, `stoa/extras/raster_scale/scale.py` |
| `CC5K_SAVE_NAME` | unset | `stoa/extras/cc5k_model/predict.py`, `stoa/extras/raster_scale/scale.py` |
| `DRY` | `0` | `serving/watchdog.sh` |
| `E1_ANCHORED` | `1` | `stoa/evidence/linework.py` |
| `E1_ARROW_SEED` | `1` | `stoa/evidence/lineplan.py` |
| `E1_BRIDGE` | `1` | `stoa/evidence/linework.py` |
| `E1_BRIDGE_M` | `2.5` | `stoa/evidence/linework.py` |
| `E1_CANON_WORDS` | `1` | `stoa/evidence/lineplan.py` |
| `E1_COUNT_EXACT` | `1` | `stoa/evidence/lineplan.py` |
| `E1_COUNT_NO_TITLES` | `1` | `stoa/evidence/lineplan.py` |
| `E1_DASH_GAP_PT` | `4.0` | `stoa/evidence/slabs.py` |
| `E1_DEBUG_GRID` | unset | `stoa/evidence/slabs.py` |
| `E1_DIM_GRID` | `0` | `stoa/evidence/slabs.py` |
| `E1_DOOR_CLUSTER` | `1` | `stoa/evidence/lineplan.py` |
| `E1_DOOR_LEAVES` | `1` | `stoa/evidence/linework.py` |
| `E1_DOOR_SEMI` | `1` | `stoa/evidence/linework.py` |
| `E1_DROP_DASHED` | `1` | `stoa/evidence/linework.py` |
| `E1_EXACT_FIRST` | `1` | `stoa/evidence/lineplan.py` |
| `E1_FACE_SNAP` | `1` | `stoa/evidence/linework.py` |
| `E1_FACE_SNAP_CUT_PT` | `3.5` | `stoa/evidence/linework.py` |
| `E1_FACE_SNAP_MIN` | `0.9` | `stoa/evidence/linework.py` |
| `E1_FILL_GROW_M` | `0.08` | `stoa/evidence/linework.py` |
| `E1_FLOOR_VIEWS` | `1` | `stoa/evidence/lineplan.py` |
| `E1_HATCH_RINGS` | `1` | `stoa/evidence/slabs.py` |
| `E1_HATCH_RING_PT` | `2.5` | `stoa/evidence/slabs.py` |
| `E1_KEEP_SEPARATIONS` | `1` | `stoa/evidence/linework.py` |
| `E1_LABEL_LINES` | `4` | `stoa/evidence/lineplan.py` |
| `E1_MAIN_EXT` | `1` | `stoa/evidence/slabs.py` |
| `E1_NUM_PICK` | `1` | `stoa/evidence/lineplan.py` |
| `E1_NUM_PT_TRACED` | `20.0` | `stoa/evidence/slabs.py` |
| `E1_OCR_BLOCKS` | `1` | `stoa/evidence/slabs.py` |
| `E1_OCR_FOLD` | `1` | `stoa/evidence/lineplan.py` |
| `E1_PHRASE_SUM` | `1` | `stoa/evidence/lineplan.py` |
| `E1_POCKET_GUARD` | `1` | `stoa/evidence/lineplan.py` |
| `E1_PRINTED_TAG` | `1` | `stoa/evidence/lineplan.py` |
| `E1_PRINTED_TAG_TOL` | `0.2` | `stoa/evidence/lineplan.py` |
| `E1_QUAL_CLASH` | `1` | `stoa/evidence/lineplan.py` |
| `E1_RECESS_AREA` | `1` | `stoa/evidence/lineplan.py` |
| `E1_RECESS_CUT` | `1` | `stoa/evidence/lineplan.py` |
| `E1_RECESS_LEAVES` | `1` | `stoa/evidence/lineplan.py` |
| `E1_RECESS_PERIM` | `1` | `stoa/evidence/lineplan.py` |
| `E1_REGION_SKIP` | `1` | `stoa/evidence/linework.py` |
| `E1_REVEAL_CUT` | `1` | `stoa/evidence/linework.py` |
| `E1_REVEAL_W` | `0.8,1.6` | `stoa/evidence/lineplan.py` |
| `E1_RING_FIX` | `0` | `stoa/evidence/linework.py` |
| `E1_ROOM_EXACT` | `1` | `stoa/evidence/lineplan.py` |
| `E1_ROW_KEY` | `1` | `stoa/evidence/textground.py` |
| `E1_SKIRT_REVEALS` | `0` | `stoa/evidence/lineplan.py` |
| `E1_SLAB_WORDING` | `1` | `stoa/evidence/slabs.py` |
| `E1_TABLE_OCR` | `1` | `stoa/evidence/areatable.py` |
| `E1_TABLE_WORDING` | `1` | `stoa/evidence/areatable.py` |
| `E1_THIN_FILL_PT` | `0.5` | `stoa/evidence/linework.py` |
| `E1_TRACED_ALL_FALLBACK` | `1` | `stoa/evidence/linework.py` |
| `E1_TRACED_DASHES` | `1` | `stoa/evidence/linework.py` |
| `E1_TRACED_DASH_GAP_PT` | `6.0` | `stoa/evidence/linework.py` |
| `E1_TRACED_FILLS` | `0` | `stoa/evidence/linework.py` |
| `E1_TRACED_NO_ALL` | `1` | `stoa/evidence/linework.py` |
| `E1_TRACED_WALL_PT` | `1.0` | `stoa/evidence/linework.py` |
| `E1_WALLS_FALLBACK` | `1` | `stoa/evidence/lineplan.py` |
| `E1_WALL_TWINS` | `1` | `stoa/evidence/lineplan.py` |
| `EXTRA_ARGS` | empty | `serving/start.sh` |
| `FREE_GB` | `$(free_gb)` | `serving/watchdog.sh` |
| `G1_STACK_FRAG` | `1` | `stoa/solvers/label_count.py` |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `stoa/llm.py` |
| `GEMMA_IMG_TOKENS` | `1120` | `serving/start.sh` |
| `GPU_UTIL` | `0.45` | `serving/start.sh` |
| `IFC_GEOM_THREADS` | `0` | `stoa/solvers/ifc/index.py` |
| `INTERVAL` | `60` | `serving/watchdog.sh` |
| `JUDGE` | `1` | `deploy/verda/report.py` |
| `LLM2_DECODE_TPS` | `700` | `stoa/llm.py` |
| `LLM2_MODEL` | `default` | `stoa/llm.py` |
| `LLM2_PREFILL_TPS` | `25000` | `stoa/llm.py` |
| `LLM_DECODE_TPS` | `1000` | `stoa/llm.py` |
| `LLM_MAX_PIXELS` | `3200000` | `stoa/llm.py` |
| `LLM_PREFILL_TPS` | `25000` | `stoa/llm.py` |
| `LLM_SERVED_MODEL_TTL` | `30` | `stoa/llm.py` |
| `LORA` | empty | `serving/start.sh` |
| `LORA_URL` | unset | `deploy/verda/serverless.py` |
| `MAX_LEN` | `32768` | `serving/start.sh` |
| `MAX_RESTARTS` | `3` | `serving/watchdog.sh` |
| `NEUTRAL_ROOT` | `/root/work/neutral` / `.stoa/neutral` | `deploy/verda/lone_split.py`, `stoa/extras/neutralize.py` |
| `NO_LORA` | `0` | `serving/start.sh` |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | `stoa/llm.py` |
| `ORIG` | unset | `deploy/verda/lone_split.py` |
| `OUT` | unset | `stoa/solvers/raster/photo.py` |
| `PADDLE_PDX_CACHE_HOME` | unset; `stoa/ocr/engine.py` sets `".stoa/paddlex"` when unset | `stoa/ocr/engine.py` |
| `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` | unset; `stoa/ocr/engine.py` sets `"True"` when unset | `stoa/ocr/engine.py` |
| `PORT` | `8001` | `serving/start.sh`, `serving/stop.sh` |
| `R1_RG_PRINTED` | `always` | `stoa/extras/room_graph/answer.py` |
| `R1_RG_TEXTLAYER` | `0` | `stoa/extras/room_graph/answer.py` |
| `ROOM_ABSORB` | `1` | `web/server/room_shape.py` |
| `ROOM_E1` | `1` | `web/server/room_shape.py` |
| `ROOM_UNHATCH` | `1` | `web/server/room_shape.py` |
| `RUNTIME_LORA` | `0` | `serving/start.sh` |
| `STOA_AGENT` | unset | `stoa/llm.py`, `web/mock/stoaApi.ts` |
| `STOA_AGENT_STOREYS` | `0` | `stoa/router/agent.py` |
| `STOA_API_MIN_TOKENS` | `2048` | `stoa/llm.py` |
| `STOA_API_PRICES` | `{}` | `stoa/cost.py` |
| `STOA_API_URL` | unset | `web/mock/stoaApi.ts` |
| `STOA_AREA_CHECK_BELOW` | `0.7` | `stoa/agent/vision.py` |
| `STOA_ASK_CONTEXT` | unset | `stoa/agent/click.py`, `stoa/features/region.py`, `stoa/formats/normalize.py` (+2) |
| `STOA_BACKED_CONF` | `0.6` | `stoa/agent/vision.py` |
| `STOA_CHECKLIST_DIR` | unset | `stoa/features/checklist.py`, `stoa/features/privacy.py` |
| `STOA_CORRECTIONS_ASK_ANY_PATH` | `0` | `stoa/features/corrections.py` |
| `STOA_CORRECTIONS_DIR` | unset | `stoa/features/corrections.py` |
| `STOA_DET_PXM` | `46` | `stoa/solvers/door_detect.py` |
| `STOA_DET_ROOMS` | `0` | `stoa/solvers/door_detect.py` |
| `STOA_DET_TIMEOUT` | `30` / `40` | `stoa/features/takeoff.py`, `stoa/solvers/door_detect.py` |
| `STOA_DET_URL` | `http://127.0.0.1:8140` | `stoa/features/takeoff.py`, `stoa/solvers/door_detect.py`, `stoa/solvers/openings.py` |
| `STOA_DIMS_PAPER` | `0` | `stoa/solvers/upload.py` |
| `STOA_DR_ANGLED` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_ARCLESS` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_CLOSETS` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_CLOSETS_ALL` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_CLOSET_Q` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_CLOSET_SAY` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_CUT_LEAF` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_DET_MAX_QUEUE` | `4` | `stoa/solvers/openings.py` |
| `STOA_DR_DET_TIMEOUT` | `30` / `25` | `stoa/features/takeoff.py`, `stoa/solvers/openings.py` |
| `STOA_DR_EACH` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_EXACT_LABEL` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_EXCLUDE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_EXTERIOR` | `0` | `stoa/solvers/openings.py` |
| `STOA_DR_FACE_PEN` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_FULLRES_DET` | `auto` | `stoa/solvers/openings.py` |
| `STOA_DR_IMAGES` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_IMG_SIDE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_KEYPLAN` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_MULLION` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_ODD` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_PARTITION` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_PLAN_TITLE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_REPEAT` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_ROOM_XCHECK` | `0` | `stoa/solvers/openings.py` |
| `STOA_DR_SCALE_BAND` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_SCAN_BOTH` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_SCATTER` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_SCHEDULE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_SCHED_DROP` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_SLIDING` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_STACK` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_TANGENT_OWN` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_TRACE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_TYPED` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_UNIT_EDGE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_VWIN` | `1` | `stoa/features/takeoff.py`, `stoa/solvers/openings.py` |
| `STOA_DR_VWIN_DET` | `0` | `stoa/solvers/openings.py` |
| `STOA_DR_WHOLE_IMG` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_WING` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_WING_FULLRES` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_WIN_EXTERIOR` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_WIN_PROFILE` | `1` | `stoa/solvers/openings.py` |
| `STOA_DR_XCHECK_TIMEOUT` | `10` | `stoa/solvers/openings.py` |
| `STOA_DWG_TEXT_FIX` | `1` | `stoa/formats/dwg.py` |
| `STOA_EU_ONLY` | `0` / empty | `stoa/agent/private.py`, `stoa/llm.py`, `stoa/netguard.py` (+1) |
| `STOA_EU_RELAY_PORTS` | empty | `stoa/netguard.py` |
| `STOA_EXAMPLES_DIR` | empty | `stoa/features/privacy.py`, `stoa/features/takeoff.py`, `web/mock/stoaApi.ts` |
| `STOA_F5_BLUR` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_DEMO_DIR` | (computed in code) | `stoa/features/privacy.py`, `stoa/features/revcompare.py` |
| `STOA_F5_DENSE` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_DENSE_DIFF` | `0.45` | `stoa/features/revcompare.py` |
| `STOA_F5_GHOST` | `0.25` | `stoa/features/revcompare.py` |
| `STOA_F5_GHOST_PAPER` | `p90` | `stoa/features/revcompare.py` |
| `STOA_F5_HATCH_EDGE` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_IFC_MOVE` | `0.05` | `stoa/features/revcompare.py` |
| `STOA_F5_LAYER_WAIT` | `180` | `stoa/features/revcompare.py` |
| `STOA_F5_OCR` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_OCR_AREAS` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_OCR_SCORE` | `0.8` | `stoa/features/revcompare.py` |
| `STOA_F5_OCR_WAIT` | `6` | `stoa/features/revcompare.py` |
| `STOA_F5_OTHER` | `0.85` | `stoa/features/revcompare.py` |
| `STOA_F5_R` | `3` | `stoa/features/revcompare.py` |
| `STOA_F5_SET_MAX` | `40` | `stoa/features/revcompare.py` |
| `STOA_F5_SMALL_DIGITS` | `16` | `stoa/features/revcompare.py` |
| `STOA_F5_SPECK` | `0.0035` | `stoa/features/revcompare.py` |
| `STOA_F5_TEXT_DIFF` | `0.3` | `stoa/features/revcompare.py` |
| `STOA_F5_TEXT_NCC` | `0.6` | `stoa/features/revcompare.py` |
| `STOA_F5_UPRIGHT` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_WALLS` | `1` | `stoa/features/revcompare.py` |
| `STOA_F5_WALL_H` | `2.7` | `stoa/features/revcompare.py` |
| `STOA_FEATURES` | empty | `stoa/agent/click.py`, `stoa/features/__init__.py`, `stoa/features/corrections.py` (+5) |
| `STOA_FEAT_CACHE` | `Path(tempfile.gettempdir()) / "stoa-feat"` | `stoa/features/privacy.py`, `stoa/features/revcompare.py` |
| `STOA_G1_AGENT_TYPE` | `1` | `stoa/pipeline.py` |
| `STOA_G1_APT_HEADLINE` | `1` | `stoa/solvers/area_tags.py` |
| `STOA_G1_AXB` | `1` | `stoa/pipeline.py` |
| `STOA_G1_BARE_ROOM` | `1` | `stoa/solvers/label_count.py` |
| `STOA_G1_BARE_TOTAL` | `0` | `stoa/pipeline.py` |
| `STOA_G1_COLD_GUESS` | `1` | `stoa/pipeline.py` |
| `STOA_G1_GLOSS_AREA` | `1` | `stoa/pipeline.py` |
| `STOA_G1_GROSS_NET` | `1` | `stoa/pipeline.py` |
| `STOA_G1_LIST_ANS` | `1` | `stoa/agent/vision.py` |
| `STOA_G1_LIVING` | `1` | `stoa/pipeline.py` |
| `STOA_G1_NAME_LIST` | `1` | `stoa/solvers/area_tags.py` |
| `STOA_G1_OCR_DOUBT` | `1` | `stoa/solvers/piping_image.py` |
| `STOA_G1_ONE_ROOM` | `1` | `stoa/agent/click.py` |
| `STOA_G1_OPENING_SIZE` | `1` | `stoa/router/intent.py` |
| `STOA_G1_PRIVATE_WHY` | `1` | `stoa/pipeline.py` |
| `STOA_G1_QUAL` | `1` | `stoa/solvers/label_count.py` |
| `STOA_G1_ROOM_SCOPE` | `1` | `stoa/pipeline.py`, `stoa/router/intent.py` |
| `STOA_G1_ROW_KEYS` | `1` | `stoa/solvers/area_tags.py` |
| `STOA_G1_SCHOOL` | `1` | `stoa/pipeline.py` |
| `STOA_G1_SETTLED` | `1` | `stoa/agent/vision.py` |
| `STOA_G1_SHAPE` | `1` | `stoa/pipeline.py` |
| `STOA_G1_TAKEOFF` | `1` | `stoa/pipeline.py` |
| `STOA_G1_TYPE_TAG_ROUTE` | `1` | `stoa/pipeline.py` |
| `STOA_G1_WHOLE_PLAN` | `1` | `stoa/pipeline.py` |
| `STOA_GATE_OPEN_FILE` | `~/.stoa_gate_open` | `deploy/gate.py` |
| `STOA_GATE_PASSWORD` | empty | `deploy/gate.py` |
| `STOA_GEMINI_BELOW` | `0.5` | `stoa/agent/escalate.py` |
| `STOA_GEMINI_PLANNER` | `gemini-3.1-pro-preview` | `stoa/agent/gemini.py` |
| `STOA_GEMINI_PURPOSE` | unset | `stoa/llm.py` |
| `STOA_GEMINI_VISION` | `gemini-3.1-pro-preview` | `stoa/agent/vision.py` |
| `STOA_GEMINI_VISION_STEP` | `1` | `stoa/agent/escalate.py` |
| `STOA_GLANCE_VISITOR` | empty | `web/server/glance.py` |
| `STOA_HOSTED_DEADLINE_S` | `100` | `stoa/llm.py` |
| `STOA_HOSTED_TIMEOUT_S` | `75` | `stoa/llm.py`, `web/mock/stoaApi.ts` |
| `STOA_IMAGE_MAX_SIDE` | `str(IMAGE_MAX_SIDE)` | `stoa/formats/normalize.py` |
| `STOA_JUNK_OCR` | `1` | `stoa/formats/normalize.py`, `stoa/solvers/raster/textlayer.py` |
| `STOA_KG_REPO` | unset | `web/mock/stoaApi.ts` |
| `STOA_LEADERBOARD` | unset | `web/mock/stoaApi.ts` |
| `STOA_LIKE_THR` | `0.8` | `stoa/agent/click.py` |
| `STOA_LLM_CACHE_DIR` | `str(SHARED / "artifacts" / "llm-cache")` | `stoa/llm.py` |
| `STOA_LLM_REPLAY` | unset | `stoa/agent/escalate.py`, `stoa/llm.py` |
| `STOA_LOCAL2_FALLBACK` | `1` | `stoa/llm.py` |
| `STOA_MEASURE_KEEP_M` | `0` | `stoa/features/region.py` |
| `STOA_NETGUARD` | `1` | `stoa/netguard.py` |
| `STOA_NETGUARD_LATE` | `1` | `stoa/netguard.py` |
| `STOA_NETGUARD_RELAYS` | `1` | `stoa/netguard.py` |
| `STOA_NETGUARD_RELAY_TTL` | `10` | `stoa/netguard.py` |
| `STOA_O1_ARC_THREADS` | `6` | `stoa/solvers/raster/doorarcs.py` |
| `STOA_O1_ARITH` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_BARE_UNIT` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_BAR_FIX` | `1` | `stoa/solvers/raster/scalebar.py` |
| `STOA_O1_BAR_OFF` | empty | `stoa/solvers/raster/scalebar.py` |
| `STOA_O1_BLOCK` | `1` | `stoa/solvers/raster/chains.py` |
| `STOA_O1_BOX_DISAGREE` | `0` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_BRIDGE` | `2.0` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_BRIDGE_MIN` | `90` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_CACHED_VLM` | `1` | `stoa/solvers/raster/sheet.py` |
| `STOA_O1_CALLOUTS` | `1` | `stoa/solvers/raster/callouts.py` |
| `STOA_O1_CHAINS` | `1` | `stoa/solvers/raster/chains.py` |
| `STOA_O1_CHAIN_AGG` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_CHAIN_GAP` | `1` | `stoa/solvers/raster/chains.py` |
| `STOA_O1_DIMS_WALLS` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_DIM_GUARD` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_DIM_IDS` | `1` | `stoa/solvers/raster/apartment.py` |
| `STOA_O1_GRID` | `0` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_GROW` | `60` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_HV_SIZE` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_IMPERIAL_OUT` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_LABEL_ABBR` | `1` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_LABEL_CUT` | `1` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_LABEL_FIT` | `1.5` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_LARGE_SHEET` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_LEADER` | `1` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_LEAF_GATE` | `1` | `stoa/solvers/raster/doorarcs.py` |
| `STOA_O1_LEAK_LABELS` | `10` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_LEVEL_ANY` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_LOOSE_CHAINS` | `1` | `stoa/solvers/raster/chains.py` |
| `STOA_O1_MEASURE_KEEP` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_NOTES` | `1` | `stoa/solvers/raster/notes.py` |
| `STOA_O1_OCR_GATE` | `1` | `stoa/ocr/quality.py` |
| `STOA_O1_PHOTO` | `1` | `stoa/solvers/raster/photo.py` |
| `STOA_O1_PHOTO_F` | `1` | `stoa/solvers/raster/photo.py` |
| `STOA_O1_PHOTO_FLAT` | `1` | `stoa/solvers/raster/photo.py` |
| `STOA_O1_PIXEL_DPI` | `1` | `stoa/solvers/raster/scalebar.py` |
| `STOA_O1_PRINTED_SIZE` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_PRINTED_SUM` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_RCP_GUARD` | `1` | `stoa/solvers/raster/views.py` |
| `STOA_O1_RECOUNT` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_RICHER` | `1` | `stoa/solvers/raster/textlayer.py` |
| `STOA_O1_SCAN_BRIDGE` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_SCAN_FILL_CAP` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_SECTOR` | `1.2` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_SEED_SKIP` | `1` | `stoa/solvers/raster/measure.py` |
| `STOA_O1_SHEET_NOTE` | `1` | `stoa/solvers/raster/scalebar.py` |
| `STOA_O1_SIDE_EST` | `1` | `stoa/solvers/raster/sides.py` |
| `STOA_O1_SIDE_GUARD` | `1` | `stoa/solvers/raster/sides.py` |
| `STOA_O1_STATIONS` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_STICKY` | `1` | `stoa/solvers/raster/textlayer.py` |
| `STOA_O1_THICK_GUARD` | `1` | `stoa/solvers/raster/textqa.py` |
| `STOA_O1_TILE_JOIN` | `0` | `stoa/solvers/raster/textlayer.py` |
| `STOA_O1_TYPED_ROOMS` | `1` | `stoa/solvers/raster/apartment.py` |
| `STOA_O1_TYPE_TAGS` | `1` | `stoa/solvers/raster/tags.py` |
| `STOA_O1_UNION` | `1` | `stoa/solvers/raster/textlayer.py` |
| `STOA_O1_VOCAB` | `1` | `stoa/ocr/__init__.py` |
| `STOA_OCR_NEG_TTL` | `1800` | `stoa/ocr/__init__.py` |
| `STOA_OCR_SERVER_BG` | `1800` | `stoa/ocr/__init__.py` |
| `STOA_OCR_SIZE` | `small` | `stoa/ocr/engine.py` |
| `STOA_OCR_SMALL_MP` | `3` | `stoa/ocr/__init__.py` |
| `STOA_OCR_SMALL_WAIT` | `6` | `stoa/ocr/__init__.py` |
| `STOA_OCR_THREADS` | `4` / `8` | `stoa/features/revcompare.py`, `stoa/ocr/__init__.py`, `stoa/ocr/engine.py` |
| `STOA_OCR_TIMEOUT` | `120` | `stoa/ocr/__init__.py` |
| `STOA_OCR_UPSCALE` | `0` | `stoa/ocr/engine.py` |
| `STOA_OCR_URL` | `http://127.0.0.1:8110` | `stoa/ocr/__init__.py` |
| `STOA_OCR_VFIX` | `1` | `stoa/ocr/vfix.py` |
| `STOA_OCR_VFIX_DEBUG` | unset | `stoa/ocr/vfix.py` |
| `STOA_PAGE_DIR` | `Path(tempfile.gettempdir()) / "stoa-pages"` | `stoa/features/privacy.py`, `stoa/pipeline.py` |
| `STOA_PICK_PAGE` | `1` | `stoa/pipeline.py` |
| `STOA_PICTURE_PNG` | `1` | `stoa/formats/normalize.py`, `web/server/normalize_upload.py` |
| `STOA_PRIVACY_SAMPLE_FORGET` | `1` | `stoa/features/privacy.py` |
| `STOA_PRIVATE` | `0` / empty | `stoa/agent/private.py`, `stoa/features/privacy.py`, `stoa/features/takeoff.py` (+5) |
| `STOA_PRIVATE_RELAY_NOTE` | `1` | `stoa/llm.py` |
| `STOA_PROJECTS_DIR` | unset | `stoa/features/privacy.py`, `stoa/features/project.py` |
| `STOA_QUESTION_CALLS` | `12` | `stoa/router/budget.py` |
| `STOA_QUESTION_TOKENS` | `20000` | `stoa/router/budget.py` |
| `STOA_RECOGNIZE` | `1` | `stoa/pipeline.py` |
| `STOA_REGION_CACHE` | unset | `stoa/features/region.py` |
| `STOA_REGION_CROP` | unset | `stoa/features/region.py` |
| `STOA_REGION_MARKS` | `1` | `stoa/features/region.py` |
| `STOA_REGION_PEN_MM` | `0.15` | `stoa/features/region.py` |
| `STOA_SAMPLE_DIRS` | empty | `stoa/features/privacy.py` |
| `STOA_SANDBOX_SECONDS` | `3` | `stoa/sandbox.py` |
| `STOA_SCAN_OCR_LAYER` | `1` | `stoa/formats/normalize.py` |
| `STOA_SCAN_PAPER` | `1` | `stoa/formats/paper.py`, `stoa/solvers/upload.py` |
| `STOA_SCAN_TEXT` | `1` | `stoa/formats/pages.py` |
| `STOA_SCAN_TEXT_ASK_PAGES` | `4` | `stoa/formats/pages.py` |
| `STOA_SCAN_TEXT_PAGES` | `12` | `stoa/formats/pages.py` |
| `STOA_SELFCHECK_ROOTS` | empty | `stoa/features/selfcheck.py` |
| `STOA_SHEET_PX` | `4500` | `stoa/pipeline.py` |
| `STOA_STYLE_GUESS` | `1` | `stoa/data.py` |
| `STOA_T1_BARE_FI` | `1` | `stoa/trust/support.py` |
| `STOA_T1_CALIBRATE` | `1` | `stoa/features/checklist.py`, `stoa/trust/calibrate.py` |
| `STOA_T1_DIMCHECK` | `1` | `stoa/trust/chaincheck.py` |
| `STOA_T1_FAMILY` | `1` | `stoa/trust/calibrate.py` |
| `STOA_T1_GAP` | `1` | `stoa/trust/support.py` |
| `STOA_T1_KEEP_OWN` | `1` | `stoa/trust/calibrate.py` |
| `STOA_T1_PLURAL` | `1` | `stoa/trust/support.py` |
| `STOA_T1_PRINTEDCOUNT` | `1` | `stoa/trust/printedcount.py` |
| `STOA_T1_ROOMS` | `1` | `stoa/trust/support.py` |
| `STOA_T1_TITLE` | `1` | `stoa/trust/support.py` |
| `STOA_T1_UNCOUNTED` | `guess` | `stoa/trust/sanity.py` |
| `STOA_T1_WEAK_MARKS` | `1` | `stoa/trust/support.py` |
| `STOA_TAKEOFF_AGENT` | `0` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_CACHE` | empty | `stoa/features/privacy.py`, `stoa/features/takeoff.py`, `web/server/glance.py` |
| `STOA_TAKEOFF_LLM` | `0` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_MAX_ROOMS` | `200` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_OCR_SNAP` | `1` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_OFFWALL_PICTURES` | `1` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_Q_BUDGET` | `75` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_SAME_OUTLINE` | `1` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_SHEET_PICTURE` | `1` | `stoa/features/takeoff.py` |
| `STOA_TAKEOFF_WIN_SIDE` | `1` | `stoa/features/takeoff.py` |
| `STOA_TEXT_CACHE` | `1` | `stoa/ocr/__init__.py`, `stoa/solvers/raster/textlayer.py` |
| `STOA_TEXT_ENGINE` | `auto` | `stoa/solvers/raster/sheet.py`, `stoa/solvers/raster/textlayer.py` |
| `STOA_TILE_JOIN` | `1` | `stoa/solvers/raster/textlayer.py` |
| `STOA_TRACE_WAIT` | `90`; `stoa/cli.py` sets `"600"` when unset | `stoa/cli.py`, `stoa/solvers/openings.py`, `stoa/solvers/upload.py` |
| `STOA_TR_CONJ` | `1` | `stoa/llm.py` |
| `STOA_UPLOADS` | `Path(tempfile.gettempdir()) / "stoa-uploads"` | `stoa/features/corrections.py`, `stoa/features/privacy.py`, `stoa/features/project.py` (+7) |
| `STOA_UPLOAD_SWEEP_MIN` | `15` | `stoa/features/privacy.py` |
| `STOA_UPLOAD_TTL_HOURS` | `0` | `stoa/features/privacy.py` |
| `STOA_UPLOAD_WORKERS` | unset | `web/mock/stoaApi.ts` |
| `STOA_VECTOR_BUDGET` | `30` | `stoa/solvers/upload.py` |
| `STOA_VECTOR_XCHECK` | `0` | `stoa/agent/vision.py` |
| `STOA_VERIFIER_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | `stoa/agent/verify.py` |
| `STOA_VERIFY_BELOW` | `0.8` | `stoa/agent/escalate.py` |
| `STOA_VISION_CAP` | `0.45` | `stoa/agent/vision.py` |
| `STOA_VISION_SECOND` | `1` | `stoa/agent/vision.py` |
| `STOA_VISION_WAIT` | `25` | `stoa/agent/vision.py` |
| `STOA_VP_BRACKET` | `1` | `stoa/sheet/labels.py` |
| `STOA_VP_DIMSCALE` | `1` | `stoa/sheet/views.py` |
| `STOA_VP_DWELLING` | `1` | `stoa/sheet/footprint.py`, `stoa/sheet/labels.py` |
| `STOA_VP_FRAME` | `1` | `stoa/sheet/footprint.py` |
| `STOA_VP_ONE_FLOOR_ROW` | `1` | `stoa/sheet/footprint.py` |
| `STOA_VP_SIDE_NAMES` | `1` | `stoa/sheet/labels.py` |
| `STOA_VP_TAG_OVERLAP` | `1` | `stoa/sheet/labels.py` |
| `STOA_VP_TITLE_KIND` | `1` | `stoa/sheet/sets.py` |
| `STOA_VP_TOTAL_ROW` | `1` | `stoa/sheet/footprint.py` |
| `STOA_VP_UNGLUE` | `1` | `stoa/sheet/labels.py` |
| `STOA_WARM_GRACE` | `3` | `stoa/formats/warm.py` |
| `STOA_WARM_HOSTED` | empty | `stoa/formats/warm.py` |
| `STOA_WARM_WAIT` | `240` | `stoa/formats/warm.py` |
| `STOA_X1_BARE_TICK` | `1` | `stoa/solvers/raster/textqa.py` |
| `TMPDIR` | `/tmp` | `stoa/solvers/openings.py` |
| `UNTIL` | `19:00` | `serving/watchdog.sh` |
| `VERDA_BASE_URL` | empty | `stoa/netguard.py` |
| `VERDA_CLIENT_ID` | unset | `deploy/verda/verda.py` |
| `VERDA_CLIENT_SECRET` | unset | `deploy/verda/verda.py` |
| `VERDA_DEPLOYMENT` | `junction-vlm` | `deploy/verda/serverless.py` |
| `VERDA_INFERENCE_BASE_URL` | `https://inference.verda.com/v1` | `stoa/llm.py`, `stoa/netguard.py` |
| `VERDA_S3_ENDPOINT` | `https://objects.eu-north-3.verda.storage` | `deploy/verda/s3.py` |
| `VERDA_S3_KEYFILE` | `Path.home() / ".config/junction-verda/s3.json"` | `deploy/verda/s3.py` |
| `VERDA_S3_REGION` | `eu-north-3` | `deploy/verda/s3.py` |
| `VLLM_IMAGE` | `docker.io/vllm/vllm-openai:v0.31.0` | `deploy/verda/serverless.py` |
| `VLLM_VENV` | `.venv-vllm` | `serving/start.sh` |
| `WATCH` | `8001 8002` | `serving/watchdog.sh` |
