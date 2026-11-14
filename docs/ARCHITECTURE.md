# Architecture

One Python package, `stoa/`, answers a question about one drawing or IFC model. Every answer, from every solver,
uses the same contract (`stoa/contract.py`). A FastAPI server and a CLI wrap the package, and the web app calls the
server. One eval tool scores every solver the same way.

## Modules

| Path | What it does |
|---|---|
| `stoa/contract.py` | `Answer` (value, unit, confidence, evidence, trace, solver, assumptions, missing, cost), `Evidence` (file, page, bbox in `pdf_pt`, `image_px`, `dxf` or `svg` units, or an `ifc_guid`; optional room `polygon` or measured `polyline`; `exact` = taken from source geometry), `Cost`, `CostMeter`, `record_cost` |
| `stoa/data.py` | Dataset paths: `abspath`, `siblings` (other formats of the same drawing), `source_of`, drawing groups, questions |
| `stoa/pipeline.py` | Solver `pipeline`, the single entry point: normalise, route, gated cascade, cross-checks, confidence caps |
| `stoa/router/` | Pipeline parts: `intent.py` (rule-based question parser, any language), `compose.py` (compound questions), `multipart.py`, `agent.py` (tool-using agent), `gating.py` + `gating.json` (per-kind solver gates measured on train), `prior.py` (train prior), `canonical.py` (canonical English rewrite), `budget.py` (one LLM budget per question), `calibrate.py` (builds the gates), `recognize.py` (drawing kind, storey, printed scale and page from the content, no model), `lengths.py` (deterministic lengths, widths and perimeters on uploads) |
| `stoa/solvers/` | Registry (`__init__.py`), `BaseSolver` (`base.py`) and the solvers (table below) |
| `stoa/agent/` | Hosted steps after the cascade: `escalate.py` (when to escalate and how to combine), `vision.py` (Gemini 3.1 Pro reads the rendered page and must return boxes or points), `gemini.py` (Gemini plan-and-execute agent over our tools), `verify.py` (Nemotron checks the evidence; moves confidence, never the value) |
| `stoa/llm.py` | One OpenAI-compatible client: local vLLM on :8001 (Qwen3-VL-8B + LoRAs), optional :8002 (Qwen3.6-35B-A3B), hosted NVIDIA (Nemotron) and Gemini. It caches responses, records cost, maps boxes back to pixels, and implements API-only mode (`STOA_MODE=api`) |
| `stoa/cost.py` | Cost per answer in dollars (GPU time from tokens, hosted list prices, CPU time) and the three cost tiers (cheap, in-between, expensive) |
| `stoa/kg/` | Project knowledge graph. It links each IFC model to its rendered sheets, storeys, rooms and GlobalIds, and each Swiss PDF to its DXF and PNG. `crosscheck()` checks an answer against another format |
| `stoa/evidence/` | Evidence quality and geometry: `shapes.py` (room outlines from the sheet's filled paths), `lineplan.py` + `linework.py` (rooms traced on line-only CAD sheets), `exactness.py`, `textground.py` (find printed text), `ifcplan_pdf.py` (independent room-area reading of IFC plan PDFs, a cross-check), `groundtruth.py` + `check.py` (is the box on the right object) |
| `stoa/formats/` | Upload normaliser (`normalize.py`): DWG → DXF (`dwg.py`, if LibreDWG is installed), CAD layer names → roles (`dxflayers.py`), generic SVG → vector PDF, HEIC → JPEG (if `pi-heif` is installed), multi-page PDF → the page the question is about (`pages.py`). Evidence is mapped back to the uploaded file and page |
| `stoa/ocr/` | Optional OCR text layer for scans (PP-OCRv6 in its own environment, via a local server or a subprocess); falls back to the VLM text layer |
| `stoa/extras/` | Runtime helpers the pipeline imports: `room_graph` (room graph of raster plans: rooms, doors, areas), `raster_scale` (scale of raster plans), `kg_qa` (cross-format link questions over the project graph), `cc5k_model` (our re-implementation of the CubiCasa5K network and its wrapper) |
| `stoa/geometry/plan3d.py` | 3D geometry from a Swiss DXF floor, for the web app |
| `stoa/style.py` | Guesses an uploaded image's drawing style from its pixels, so CubiCasa-only readers still apply to uploads |
| `stoa/eval/` | `runner.py` (eval CLI), `scoring.py`, `splits.py`, `calibration.py`, `evidence.py` (box accuracy), `analyze.py`, `scorecard.py`, `leaderboard.py`, `judge.py`, `review.py`, generators for the `hard*` splits |
| `stoa/api.py`, `stoa/cli.py` | HTTP API on :8100 and the command line (`ask`, batch `answer`) |

| Solver | Code | Reads | Tier |
|---|---|---|---|
| `ifc` | `stoa/solvers/ifc/` | IFC model (IfcOpenShell). Evidence is GlobalIds plus `pdf_pt` boxes on the rendered plans. Free text goes through a fact card and one LLM call | deterministic |
| `dxf` | `stoa/solvers/vector/dxf_solver.py` | DXF layers (DOOR, WINDOW, FIXTURE, ROOM_OUTLINE, …; other naming conventions are mapped by `stoa/formats/dxflayers.py`). Boxes are mapped from DXF onto the PDF and PNG | deterministic |
| `pdf_vector` | same folder, `pdffloor.py` | Swiss-style vector PDF with no DXF | deterministic |
| `schedule`, `pdf_text`, `pdf_textqa` | `stoa/solvers/vector/` | Schedule tables and text from the PDF text layer | deterministic (table code may use the LLM) |
| `plan_rooms` | `stoa/solvers/plan_rooms.py` | Any vector plan PDF without its model or CAD file: room outlines (filled paths) + room labels + the printed or estimated 1:N scale | deterministic |
| `line_rooms` | `stoa/solvers/line_rooms.py` | A named room on a line-only CAD sheet, traced around its printed label at the printed scale | deterministic |
| `area_tags` | `stoa/solvers/area_tags.py` | Room area tags printed on CAD/Revit sheets ('680 SF', '4.24 m²') | deterministic |
| `label_count` | `stoa/solvers/label_count.py` | Counts of rooms of one type from their printed labels | deterministic |
| `svg_ifc` | `stoa/solvers/svg_ifc.py` | IfcConvert plan SVGs alone: one `<g class="IfcX" data-guid>` per element, room labels, scale | deterministic |
| `raster` | `stoa/solvers/raster/` | Scans, PNGs and photos: scale bar or printed scale, flood fill, door arcs, window symbols, dimension strings, text layer | local |
| `raster_cc5k` | `stoa/solvers/raster_cc5k/` | Pretrained CubiCasa5K network on CPU, one box per symbol (optional; weights not included) | local |
| `vlm_lora` | `stoa/solvers/vlm_direct.py` | Qwen3-VL-8B + our counting LoRA (`cc-v2` / `cc-v3`) | local |
| `vlm_direct` | `stoa/solvers/vlm_direct.py` | Baseline: render the page, ask the VLM, parse one value | local (or hosted) |
| `agent` | `stoa/solvers/agent.py` → `stoa/router/agent.py` | The general agent on its own | local |
| `gemini_agent` | `stoa/solvers/gemini_agent.py` → `stoa/agent/gemini.py` | Gemini plan-and-execute over the pipeline's atomic steps (needs `GEMINI_API_KEY`) | hosted |
| `upload` | `stoa/solvers/upload.py` | `stoa/formats` normaliser in front of `pipeline`. When every PDF reader declines a page, it is rendered and read as an image. This is the solver the API and the web app use for uploads | as routed |
| `prior` / `dummy` | `stoa/solvers/dummy.py` | Most common train answer, with its train hit rate as the confidence | deterministic |

## One question, end to end

```
POST /ask {path, question}  or  python -m stoa.cli ask <path> "<question>"
  │  (stoa/api.py / stoa/cli.py wrap solve() in a CostMeter)
  ▼
upload: normalise the file (DWG → DXF, SVG → PDF, HEIC → JPEG, pick the PDF page)
  ▼
Pipeline.solve ── one LLM budget per question (router/budget.py)
  │ recognise the file from its content (kind, storey, printed scale, page)
  │ normalise numbers, apartment refs, scope, sheet name
  │ link question (plan vs model, sheet set)? ──► extras/kg_qa + ifc plan_vs_model ──► Answer
  │ rules can't read the language? ──► local LLM translates to English (llm.to_english)
  │ compound ("doors and windows", "how many more")? ──► compose: each part ▼, arithmetic in code
  ▼
_solve_atomic
  intent.parse (rules) ──► route(path): file type + recognition + siblings, cheapest first
  │   .ifc → ifc │ PDF+DXF → dxf │ schedule sheet → schedule │ PDF → pdf_vector, plan_rooms, line_rooms, pdf_text
  │   image → raster (+ raster_cc5k, vlm_lora on CubiCasa-style plans) │ always last: vlm_direct
  ├─ unseen wording: ask the deterministic solvers as asked; keep at conf ≥ 0.45
  ├─ operator question (average, share, threshold, unknown class): agent (local LLM + tools)
  ├─ gated cascade (gating.json): skip solvers that don't beat the prior on train;
  │    stop at conf ≥ 0.6, else a cheap second opinion; agree → conf up, disagree → down + `missing`
  ├─ KG cross-check against another format (agree / disagree)
  └─ nothing answered → train prior (low confidence, with assumptions and missing)
  ▼
exit checks: unit conversion, answer-shape guard, echo guard, disagreement cap
  ▼
optional hosted step (gemini=1 / verify=1; on in the API and web app, off in every eval):
  Gemini vision for unsure counts / lengths (boxes required), Gemini planner for unsure unseen wordings,
  Nemotron verdict on unsure answers without located evidence (confidence only)
  ▼
Answer JSON (+ cost tier)
```

Where each step lives (all in `stoa/pipeline.py` unless noted):

- **Front end:** `_solve_question` normalises the question, then tries `_link_question` (links through `stoa/kg`
  and `stoa/extras/kg_qa`) and `_front_end` (translation; capped at confidence 0.9 unless both readings agree), and
  then `stoa/router/compose.py` `decompose`.
- **Recognise, parse and route:** `stoa/router/recognize.py` `recognize(path)` reads the drawing kind, storey and
  printed scale from the file's content (uploads have no folder hint). The result is the first trace line
  `recognised: {json}` and the assumption 'recognised as: …'. `stoa/router/intent.py`
  `parse()` returns an `Intent` (task, target, scope, unit, filters, lang) and a language-free key such as
  `count:window:drawing`. `route()` reads the file type, the recognition and `stoa.data.siblings`. Upload lengths go
  to `stoa/router/lengths.py` first.
- **Deterministic first:** `_raw_deterministic`. **Agent:** `_run_agent` → `stoa/router/agent.py`. It never types a
  number it did not get from a tool, and its confidence comes from the tools it used.
- **Gated cascade:** `stoa/router/gating.py` `plan()` + `gating.json`. A solver's confidence is its smoothed train
  hit rate for the key, capped by the solver's own confidence (deterministic solvers) or by unseen-wording caps.
  `_combine` merges two readings. `max_tries=3`, `accept=0.6`, `agree=cheap`.
- **Checks and fallback:** `_kg_crosscheck` (`stoa/kg`); a disagreeing cross-check caps the confidence at
  `XCHECK_CAP`. `_prior_answer` uses `stoa/router/prior.py`. The exit checks at the end of `_solve_question` convert
  the value to the unit the question asks for. With `_shape_guard` in `solve()`, they also cap the confidence of a
  wrong-shaped answer (a count when no one asked "how many", a fraction for a count, an echoed number, a disagreeing
  cross-check) and say why in `missing`.
- **Hosted step:** `stoa/agent/escalate.py`. Gemini's confidence comes from agreement with our own reading or a
  second sample, never from its self-report. Nemotron's verdict moves only the confidence. Both are off unless
  `--solver-arg gemini=1` / `verify=1` or the env defaults `STOA_GEMINI_ESCALATE=1` / `STOA_VERIFY=1` are set.

Each step has an off switch (`--solver-arg agent=0`, `compose=0`, `links=0`, `translate=0`, `lora=0`, `cc5k=0`, …;
the full list is in the `stoa/pipeline.py` docstring). `stoa/router/README.md` maps each trace line to the step that
wrote it.

## Confidence and cost

- **Confidence** is a measured hit rate, not a model's self-report: the train hit rate of the solver for this kind
  of question, then lowered by checks (new wording, translation, disagreement, wrong shape, prior answer). Agreement
  of two independent methods raises it.
- **Cost** is recorded per answer by `CostMeter` (tokens, GPU seconds, hosted calls, wall time) and priced by
  `stoa/cost.py`. Every rate can be overridden from the environment (`STOA_GPU_USD_PER_HOUR`, `STOA_API_PRICES`, …).
  `stoa.cost.tier()` gives the badge the app shows: cheap, in-between or expensive.

## Run modes

- **Local GPU mode** (default): vLLM serves Qwen3-VL-8B (+ LoRAs) on :8001 (`serving/start.sh`); `LLM_BASE_URL`
  points at it. An optional second server on :8002 (`LLM2_BASE_URL`) runs Qwen3.6-35B-A3B for free-text IFC answers
  (`STOA_TEXT_BACKEND=local2`).
- **API-only mode** (`STOA_MODE=api`, or `--api` on the CLI): no GPU. Calls meant for the local servers go to a hosted
  model (`STOA_API_BACKEND`, default Gemini; `STOA_API_MODEL`, default Gemini 3 Flash). The deterministic solvers are
  unchanged. The LoRA adapters have no hosted twin, so their prompts go to the hosted model too
  (`STOA_API_MODEL_LORA=none` makes them abstain instead).
- **Batch:** `python -m stoa.cli answer --in questions.jsonl --out answers.jsonl [--api]` answers a whole questions
  file and resumes where it stopped.
- **Deploy:** `deploy/` has a CPU Docker image (API-only mode, optional web app behind a password), Google Cloud Run
  scripts, and the Verda GPU scripts used for the full-dataset run.

## Core, extras, UI

- **Core:** `stoa/` (installed by `pyproject.toml`), tests in `tests/`, model-server scripts in `serving/`.
- **Extras:** `stoa/extras/` holds the room graph, raster scale, link QA and CubiCasa5K model wrapper that the
  pipeline imports at run time. They started as experiments in the team's workspace repository and were copied
  here unchanged in behaviour. Comments that cite `experiments/...` refer to that repository.
- **UI:** `web/` (Vite + TypeScript, PDF.js, web-ifc + three.js; dev server :5173). It proxies `/api` to :8100, hands
  uploads to small Python helpers in `web/server/`, and replays recorded answers from `web/fixtures/` when the API is
  down.
- **Outside git:** the dataset (`STOA_DATA`); a work folder (`JUNCTION_SHARED`, default `.stoa` in the repo) for split
  files, the eval leaderboard, predictions and caches; the LoRA adapters; the CubiCasa5K weights (`CC5K_WEIGHTS`).
- **Without the dataset** the product still answers: the train-split statistics it needs at run time (typical-answer
  priors and their hit rates, hashes of known question templates, CubiCasa class priors) ship as a small text-free file,
  `stoa/router/train_stats.json`, used only when the split file is missing (`python -m stoa.router.train_stats --write`
  rebuilds it from the split).

## How to run

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
cd web && npm install && cd ..
serving/start.sh                                         # vLLM :8001, Qwen3-VL-8B (+ LoRAs if present)
.venv/bin/python -m uvicorn stoa.api:app --port 8100     # /health, POST /ask, /file, /files, /questions, /intent, /kg
cd web && npm run dev                                    # http://localhost:5173
.venv/bin/python -m stoa.cli ask <path> "How many windows?"            # add --api for API-only mode
.venv/bin/python -m stoa.eval --list                     # registered solvers
.venv/bin/python -m stoa.eval.splits --write             # split files from $STOA_DATA/questions.jsonl
.venv/bin/python -m stoa.eval --solver pipeline --split dev            # smoke|dev|hard|hard2|<file.jsonl>
.venv/bin/python -m pytest -q
```

Env var names (values live in `.env`, never in git) are listed in `.env.example` and `docs/CONTRACT.md`: among
them `STOA_DATA`, `JUNCTION_SHARED`, `LLM_BASE_URL`, `LLM2_BASE_URL`, `GEMINI_API_KEY`, `NVIDIA_API_KEY`,
`STOA_GEMINI_ESCALATE`, `STOA_VERIFY` and `CC5K_WEIGHTS`. API-only mode (`STOA_MODE`, `STOA_API_*`) is described at
the top of `stoa/llm.py`, and the cost rates (`STOA_GPU_USD_PER_HOUR`, `STOA_API_PRICES`, …) in `stoa/cost.py`. Each eval run appends one line to
`$JUNCTION_SHARED/results/leaderboard.jsonl` and writes per-question predictions to `$JUNCTION_SHARED/artifacts/eval/`.
The eval hides the Gemini key from runs of more than 200 questions unless `--allow-gemini` is given.

## How to add a solver

1. Create `stoa/solvers/<name>.py`, or a package `stoa/solvers/<name>/__init__.py` that imports its solver module.
   On first use, `discover()` in `stoa/solvers/__init__.py` imports every top-level module there, except names
   starting with `_`. A module that fails to import is reported by `--list` and doesn't break the others.
2. Subclass `BaseSolver` (`stoa/solvers/base.py`) and decorate it with `@register("<name>")`. Set `extensions` and
   `sources` to get the default `can_handle`. Constructor kwargs come from `--solver-arg k=v`.
3. Implement `solve(path, question) -> Answer`. `path` is dataset-relative or absolute; open it with
   `self.abspath(path)` and find twins with `self.siblings(path)`. Return a best estimate with a measured
   confidence, one `Evidence` per counted object (`pdf_pt` / `image_px` box or `ifc_guid`), and `assumptions` /
   `missing`. Don't raise for "I don't know": return a low-confidence estimate or `Answer.abstain(...)`. `solve` must
   be thread-safe, so cache per drawing. LLM calls made through `stoa.llm.chat` are metered automatically.
4. Measure it: `python -m stoa.eval --list`, then `--solver <name> --split smoke --limit 20`, then
   `--split dev --source <source>`, then `python -m stoa.eval.analyze <run_id>`. Add `tests/test_<name>.py`.
5. Wire it into the pipeline: add the name to `route()` and `TIER` in `stoa/pipeline.py`, then gate it on train with
   `python -m stoa.router.calibrate --solvers <name> --per-key 40`. This merges the solver into
   `stoa/router/gating.json` and never uses dev or test. Until it is calibrated, an unmeasured solver runs right after
   any solver that is ≥ 90% right on train for that key (`gating.plan`). Compare paired pipeline runs on `dev`, `hard`
   and `hard2` before merging, and on real uploads (files alone, under neutral names) for anything upload-facing.
