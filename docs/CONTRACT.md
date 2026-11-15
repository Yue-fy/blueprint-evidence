# CONTRACT

> The interface between parts of the system. Solvers, the router, the API and the web demo build against
> this file in parallel. Prefer additive changes.
> Code of record: `stoa/contract.py` (pydantic models). If this file and the code disagree, the code wins.
>
> Written during the hackathon. Short names in the eval notes below (F, S0, R1, …) are the roles of the
> team's coding agents: F built the eval tool, S0 was the gatekeeper, the only one that ran the sealed splits.
> `experiments/...` scripts named here live in the team's workspace repository, not in this one.

## Base URLs and ports

| Service | Local | Notes |
|---|---|---|
| Model server (vLLM, OpenAI-compatible) | `http://127.0.0.1:8001/v1` | `$LLM_BASE_URL`; start it with `serving/start.sh` |
| API (FastAPI, `stoa/api.py`) | `http://127.0.0.1:8100` | CORS for `http://localhost:5173` |
| Web dev server (`web/`) | `http://localhost:5173` | `cd web && npm run dev` |

## The solver interface (Python)

```python
from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

@register("myname")
class MySolver(BaseSolver):
    extensions = (".pdf",)          # optional filter used by the default can_handle
    sources = ("swiss_dwellings",)  # optional filter used by the default can_handle
    def solve(self, path: str, question: str) -> Answer: ...
```

- `path` is **dataset-relative**, exactly as in `questions.jsonl` (`"data/<source>/<file>"`). Open it with
  `stoa.data.abspath(path)`. Sibling formats: `stoa.data.siblings(path)` → `{".dxf": "data/...", ".ifc": ...}`
  (for `ifc_models` plans this includes the parent model under `".ifc"`).
- `can_handle(path, question) -> bool`: cheap check, no heavy I/O.
- `solve(path, question) -> Answer`: never raise for "I don't know"; return `Answer.abstain(name, reason)`
  (value `None`). Exceptions are caught by the eval and counted as abstentions with `error` set.
- Must be thread-safe (the eval calls `solve` from several threads). Cache per drawing.
- Registry: every module under `stoa/solvers/` is imported on first use; a module that fails to import is
  reported (`python -m stoa.eval --list`) but doesn't break other solvers. Constructor kwargs come from
  `--solver-arg key=value`.

## Shared data types (JSON)

### `Answer`
```json
{
  "value": 7,                     // number | string | null (null = abstain)
  "unit": "count",                // "count" | "m2" | "m3" | "m" | "ft2" | ... | null for text
  "confidence": 0.82,             // 0..1
  "evidence": [ /* Evidence */ ],
  "trace": ["parsed: count windows", "dxf layer WINDOW: 7 inserts"],  // short human-readable steps
  "solver": "dxf",
  "assumptions": ["no scale bar; assumed door width 0.9 m"],   // what the answer relies on
  "missing": ["no DXF sibling; counted from raster"],            // what would make it certain
  "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0,
           "api_tokens_in": 0, "api_tokens_out": 0, "llm_calls": 0, "wall_seconds": 0.0},
  "recognition": {"kind": "ifc_plan", "label": "BIM-exported floor plan", "confidence": 0.95,   // D31, optional
                  "scale": "1:100", "scale_source": "printed", "storey": "2", "pages": 1, "page": 0,
                  "reader": "pdf_vector", "why": "..."},
  "clarify": {"ask": "Two readings disagree: which area do you mean?", "why": "two readings disagree",   // D47, optional
              "options": [{"label": "net floor area", "value": 61.69, "unit": "m2", "evidence": [0],
                           "question": "What is the net floor area of slab D13?"}], "click": null},
  "data_used": ["local", "gemini"],                                                                        // D47, optional
  "support": {"state": "approximate", "why": "traced outline, not a printed area", "shown": null, "of": null}  // D50, optional
}
```
`clarify` (D47, additive, optional, default `null`): a question back to the user when two readings disagree or the
scale is unknown. `value` stays the best answer; each option is a full alternative answer (`evidence` = indices into
`Answer.evidence`, `question` = the sharper wording to ask next); `click` is `"room"` / `"scale"` when the UI should
let the user click a room or two points instead. Solvers also write one `clarify: …` trace line.
`data_used` (D47, additive, optional, default `null`): where this question's drawing data went (`local`, `gemini`,
`nvidia`, `verda`); the pipeline also writes a `data_used: …` trace line, and private mode keeps it `["local"]`.
`support` (D50, additive, optional, default `null`): does the located evidence support the value? `state` is
`supported` (located evidence of the asked quantity: exact geometry, a printed label of the right kind, one box per
counted object), `approximate` (located but estimated, e.g. `Evidence.exact` false, a traced outline, or a count with
`shown` < `of`) or `unlocated` (no box / GUID: prior, image-only VLM). Set by `stoa.trust.support.mark_support()` at the
end of `Pipeline.solve`, which also writes a `support: <state> — <why>` trace line. It is not the same as evidence coverage.
`recognition` (D31, additive, optional, default `null`): what `stoa.router.recognize.recognize(path)` read from the
file's content alone (drawing kind, storey, printed scale, page picked of N; `page` is 0-based like `Evidence.page`).
The UI's "Recognised as … [Change]" chip reads it. Until every caller sets it, the same JSON is also the first trace
line `recognised: {json}`; readers should prefer the field and fall back to the trace line.
`Answer` validates on assignment too: `ans.cost = {...}` becomes a `Cost`, and `ans.confidence = 1.2` raises
(the eval records it as an exception, i.e. a refusal; check `errors` in the eval output). `Cost` also behaves
as a read-only mapping of its non-zero fields, so old code doing `{**d, **(ans.cost or {})}` keeps working.

Stoa (13 Nov): refusing is not useful. `value: null` is allowed but counted as a **refusal**; prefer a best
estimate with a low `confidence` plus `assumptions` and `missing`.

Cost: `stoa.contract.record_cost(**fields)` adds to the current question's meter (the eval wraps each
`solve()` in a `CostMeter`). The LLM client (`stoa/llm.py`) calls it after every call, so solvers get
token/API accounting for free; local-server calls set `llm_calls`, tokens and `gpu_seconds` (wall time of
the call is a fine proxy), hosted calls also set `api_calls` and `api_tokens_in/out`. Threads started inside
`solve()` need `contextvars.copy_context().run(...)` to keep the meter. The eval takes the field-wise max of
the meter and `Answer.cost` (no double counting) and fills `wall_seconds` itself.
Units: give the unit you computed in; the eval converts (m2↔ft2, m↔mm/cm/ft/in, m3↔ft3/l). A unit of the
wrong dimension (m for an area question) is wrong. Counts are rounded to the nearest integer before scoring.

### `Evidence`
```json
{
  "file": "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf",  // dataset-relative
  "page": 0,                      // 0-based page index for PDFs; null for images and IFC
  "bbox": [x0, y0, x1, y1],       // null for IFC-only evidence; a point is a box with x0==x1, y0==y1
  "bbox_space": "pdf_pt",         // "pdf_pt" | "image_px" | "dxf" | "svg" (D29); required when bbox is set
  "ifc_guid": null,               // IFC GlobalId for 3D evidence (file = the .ifc)
  "note": "window 3",
  "exact": true,                  // D25: true = source geometry, false = estimated, null = unknown
  "polygon": null,                // D28: closed outline [[x, y], ...] in bbox_space (the room an area measured)
  "polyline": null                // D28: open measured line [[x, y], ...] in bbox_space (a length's path)
}
```
- `pdf_pt`: PDF points in PyMuPDF's page coordinate space (`page.rect`: origin top-left, y down).
  The web viewer must apply the same convention (PDF.js viewport with the page's rotation).
- `image_px`: pixels of the image file as stored on disk (origin top-left, y down).
- `dxf` (D29): the .dxf's own drawing units (modelspace coordinates, **y up**). Only for a DXF uploaded without a
  PDF/PNG twin; with a twin (incl. the sheet the web upload renders from a lone DXF) evidence stays `pdf_pt` on it.
- `svg` (D29): the .svg's raw user coordinates — the same numbers as its paths / circles / texts (y down); the
  file's viewBox maps them onto the canvas (IfcConvert plans: paper millimetres). Only for an SVG without a PDF/PNG
  twin (`stoa.evidence.plan_svg`).
- A viewer that does not know a space shows the note at file level and never draws the box.
- Evidence may point to a sibling file (e.g. the DXF-derived box drawn on the PDF that was asked about,
  or GlobalIds in the `.ifc` behind an `ifc_models` plan). Prefer evidence on the asked file.
- One Evidence per counted object for counts; for areas, the room polygon's bbox (or the schedule row).
- `exact` (D25, additive, default null): `true` when the place comes from the drawing's own geometry (DXF entities,
  IFC/SVG twin, schedule rows, printed text); `false` when it is estimated (CubiCasa detector / segmentation boxes,
  VLM boxes, door swings, model footprints the plan does not draw, E1 candidates). The pipeline fills it with
  `stoa.evidence.exactness.mark_exact` where a solver left it null; the UI draws `false` dashed ("approximate").
- `polygon` / `polyline` (D28, additive, default null): the room outline an area answer measured / the line a length
  answer measured, same `page` and `bbox_space` as `bbox` (which stays their bounding box). Filled post hoc by
  `stoa.evidence.shapes` (DXF/SVG twin, the PDF's own floor model or filled room paths, IFC wall centre lines).

## Eval CLI

```bash
.venv/bin/python -m stoa.eval --solver <name> --split smoke|dev [--limit N] \
    [--source S] [--type T] [--grep REGEX] [--workers N] [--solver-arg k=v] [--handled-only] [--note "..."]
.venv/bin/python -m stoa.eval --list        # registered solvers + import errors
```
- Splits (v2) live in `$JUNCTION_SHARED/results/splits/{smoke,dev,test,train,unused,hard}.jsonl`. Grouped
  by drawing (`stoa.data.drawing_group`): sibling formats, floors of one Swiss site, plans and discipline
  models of one IFC project all stay together. Eval splits keep one language per concept (EN or the local
  language; `stoa.data.concept_map`). `test` is sealed (only S0, with `--allow-test`); opening_design is
  dev-only. `unused` = translations and capped-out questions of eval drawings: never train on them. `hard` =
  generated generality checks on dev drawings (`stoa/eval/hard.py`). `hard2` = reasoning kinds that
  questions.jsonl never asks (averages, ratios, percentages, thresholds, complements, unit changes, argmin,
  yes/no; `stoa/eval/hard2.py`, 256 Qs on dev drawings); it adds the scored type `ratio` (dimensionless,
  within 2%). `hard2p` = the same questions reworded (other English phrasing, German/French/Finnish;
  `stoa/eval/hard2p.py`). `hard2q` = a third wording (Italian/Dutch/Swedish + new English; `hard2p.py --q`):
  the clean HELD-OUT set since 01:12 (hard2p wordings were quoted in messages, so hard2p is contaminated for
  dxf/ifc). Develop on `hard2`; on `hard2p`/`hard2q` report aggregates only and never quote their questions.
  `hard2r` (01:55) = SEALED: a fresh draw of the hard2 recipes in a fourth wording (Spanish/Estonian/Polish + new
  English; `stoa/eval/hard2r.py`, templates outside git in `$JUNCTION_SHARED/sealed/F/`). Only F and S0 run it, at
  milestones (test runs), aggregates only; it replaces hard2q as the clean held-out set. `hard3` (03:52) = SEALED
  derived quantities (prices, rates, constants applied to dev gold; F + S0 only). `hardL_open` / `hardL` (04:30) =
  the SAME known-gold dev questions in 8 untuned languages (`stoa/eval/hardl.py`, hosted-Nemotron translations;
  meta.lang / meta.base_qid): `hardL_open` (English bases + cs/hu/tr/ro) is open, `hardL` (nb/hr/el/lt) is held out
  (F/S0, aggregates); paired language penalty with `experiments/F/hardl_paired.py EN_RUN RUN`. Load in code:
  `stoa.eval.splits.load_split("train")` → `list[Question]` with answers.
- `--limit N` takes the same fixed random subset every run. `--grep REGEX` keeps questions matching the regex
  (case-insensitive), e.g. `--grep 'window|ikkun|fenster|fönster'` for one concept in every language.
- Scoring: count exact (after rounding); area/length/volume within 2% after unit conversion; text/other
  not scored (a rough `text_fuzzy` rate is reported separately). Abstain = wrong, reported as coverage.
- Output: table per source × type (accuracy, coverage, accuracy-when-answered, evidence rate, text~),
  micro accuracy, macro accuracy, **`weighted_acc`** (bucket accuracies weighted by each bucket's share of
  scored questions in the FULL dataset; the team's headline accuracy, S0 D1), refusal rate, cost per question
  (tokens, LLM/API calls, GPU s, wall s, **est. USD/question** with the rates in `stoa/eval/runner.py:RATES`),
  and accuracy per cost tier (`deterministic` / `local` / `api`).
  **Calibration** (`metrics.calibration`; Stoa's "know what you don't know"): ECE (10 bins), Brier, AUROC
  (does confidence rank right above wrong answers; 0.5 = noise), AURC/excess AURC (risk–coverage), and the
  share of answers with confidence ≥ 0.8 plus their accuracy. **Evidence**: `evidence_rate` (any evidence),
  `evidence_2d_rate` (a bbox the UI can draw), `evidence_count_match` (count answers whose located objects,
  i.e. unique IFC GUIDs + boxes without a GUID, equal the answer). One line appended to `$JUNCTION_SHARED/results/leaderboard.jsonl`;
  per-question predictions in `$JUNCTION_SHARED/artifacts/eval/<run-id>.jsonl` (gold, prediction,
  evidence, trace, error) for error analysis.
- Set `AGENT_ID=<your id>` in the environment so the leaderboard line records who ran it.
- `--judge local|nvidia`: grade text/other answers with an LLM judge (`stoa/eval/judge.py`), reported as
  `text_judge` per bucket, never part of accuracy; the judge's cost is not charged to the solver.
- Other tools: `python -m stoa.eval.analyze RUN [--baseline RUN]` (loss tables, examples, calibration),
  `python -m stoa.eval.leaderboard [--split dev]` (latest full run per solver on the current split version),
  `python -m stoa.eval.gates CHEAP_RUN CHALLENGER_RUN` (per-concept gate table from `--split train_gate` runs),
  `python -m stoa.eval.calibration RUN [RUN ...] [--by-source]` (calibration side by side),
  `python -m stoa.eval.review RUN.jsonl [--out f.md] [--only-low 0.8]` (one readable section per question: answer,
  confidence, solver, evidence, assumptions, missing, cost, trace; for gold-less files such as Stoa's examples;
  refuses held-out/sealed runs).
  Extra splits: `train_gate` (960 train Qs for choosing gates/thresholds), `cc5k_heldout` (CC5K-pretrained
  models only), `hard` (generality), `human` (the expert's own questions from agent H's workbench,
  `$JUNCTION_SHARED/human/questions.jsonl`; last save per qid wins; `meta.kind = "human:<sureness>"`; report it
  separately from dev), or any split file path.
- **Hosted Gemini guard** (no bulk use of Google's hackathon key): a run of more than 200
  questions hides `GEMINI_API_KEY` / `GOOGLE_API_KEY` from every solver (printed on stderr) unless `--allow-gemini`.
  Solvers that call Gemini must therefore work (fall back) without the key. `experiments/F/probe_suite.py` always hides it.
- **Ad-hoc question files** (e.g. the example questions Stoa shows on Saturday): `--split path/to/file.jsonl`, one
  JSON object per line with `path` (relative to the dataset root, e.g. `data/cubicasa5k/x.png`) and `question`;
  optional `answer`, `type` (count|area|length|volume|ratio|text; default `other` = not scored), `unit`, `qid`
  (default: hash of path + question). Rows without `answer` are predicted (value, evidence, trace, cost in the
  predictions file) but never scored or judged; the report prints how many. Add `--no-leaderboard` for quick looks.
- **Files outside the dataset** (Stoa's own files, demo uploads): `path` may be absolute; evidence `file` is then that
  absolute path. Nothing else is known about such a file except what `stoa.data` reads from its path (no new interface,
  this documents existing behaviour, F c226–c227):
  - `source_of()`: the `<source>` of a `.../data/<source>/...` folder in the path (known sources only); else an IFC plan
    sheet (`.pdf/.svg/.png/.jpg`) whose model sits in the same folder (`<model>_<view>.pdf` next to `<model>.ifc`, or the
    only `.ifc` in the folder) is `ifc_models`; else `unknown` (routed by extension: the CubiCasa readers and the
    LoRA's per-concept gate are not used; `dxf` still reads a same-stem DXF twin whose layer table uses the Swiss layer
    names, W2 e461199).
  - `siblings()`: files with the same stem in the same folder (+ the IFC model of an `ifc_models` sheet).
  - `dataset_rel()`: an absolute path is cut to `data/<source>/<file>` only when the dataset holds the same file
    (same relative path and size), so a new drawing is never confused with a dataset file of the same name.
  - Measured on the OPEN neutral-path probe (168 dev Qs × 5 layouts of the same bytes, night 1a4e2af, run
    `20261114-101541-pipeline-neutral_path-6f61`): dataset paths 91.1%; a neutral folder with all twins, original or
    hashed names, 90.5%; the asked file alone 66.1% (IFC sheets without their model 0–14%, at confidence ≤ 0.31;
    Swiss PDFs alone 90–100% via `pdf_vector`; CubiCasa PNGs same accuracy, conf-wrong 2 → 3; schedules unchanged).
  - **Recommended layout for new files**: `<ROOT>/data/<source>/<name>.<ext>`, every format of one drawing under one
    stem, an IFC model next to its sheets; then `--split file.jsonl` with absolute paths. Check a new route on such
    copies with `experiments/F/neutralize.py IN OUT --mode hash|name|lone`, and run F's same-gold regression suite
    with `experiments/F/probe_suite.py run --tag X` / `compare` (baseline = night 637a6df, the freeze candidate).

## HTTP API (`stoa/api.py`, port 8100)

### `POST /ask`
Request: `{"path": "data/...", "question": "How many windows ...?", "solver": "pipeline"}` (`solver`
optional, default `pipeline`). Response `200`: an `Answer` JSON (above). `404` if the file doesn't exist.
Additive fields (D6, 00:33): `"intent"` (the router's parse of the question, `stoa.router.intent.parse(q).to_dict()`),
`"plan"` (only for compound questions: `{"op": "sum"|..., "parts": [...], "sub_questions": [...]}`) and
`"cached"` (bool). Clients must ignore fields they don't know.

### `GET /intent?q=...`
Returns the router's intent parse for a question (debug/demo).

### `GET /file?path=data/...`
Returns the drawing file (PDF, PNG, JPG, SVG, IFC, DXF) with the right content type. Only paths under the
dataset root (reject `..`).

### `GET /files?source=<source>&q=<substring>&limit=<n>`
Response: `{"files": [{"path": "data/...", "source": "...", "ext": ".pdf", "siblings": {".dxf": "..."},
"n_questions": 12, "license": "CC BY 4.0", "credit": "..."}]}`.

### `GET /questions?path=data/...`
Response: `{"questions": [{"question": "...", "type": "count", "unit": "count"}]}` (example questions for
the demo; no answers).

Further endpoints (e.g. `GET /ifc-index?path=`, uploads) are defined in `stoa/api.py`.

## Env vars (names only; values live in `.env` or the shell, never in git)

| Name | Used by | Where to get it |
|---|---|---|
| `STOA_DATA` | all | dataset root (contains `questions.jsonl`, `data/`) |
| `STOA_PREVIEW` | optional | preview dataset root |
| `JUNCTION_SHARED` | eval, caches | local state folder (splits, leaderboard, caches, artifacts); default `.stoa` |
| `JUNCTION_VENV` | all | Python env (`$JUNCTION_VENV/bin/python`) |
| `MODEL_DIR` | `serving/start.sh` | model weights |
| `LLM_BASE_URL` | `stoa/llm.py` | local OpenAI-compatible server, default `http://127.0.0.1:8001/v1` |
| `NVIDIA_API_KEY` | `stoa/llm.py` | NVIDIA hosted API (Nemotron); your own key |
| `GEMINI_API_KEY` | `stoa/llm.py` (backend `gemini`) | your own Google AI Studio key; escalation only, keep it out of bulk evals |
| `GEMINI_MAX_CALLS_PER_DAY` | `stoa/llm.py`, `stoa/agent/escalate.py` | optional; per-day call cap for Gemini |
| `STOA_GEMINI_ESCALATE` / `STOA_VERIFY` | `stoa/pipeline.py` | `1` = Gemini escalation / Nemotron verify after the cascade (API/demo default); evals and gates set `0` |
| `LLM2_BASE_URL` | `stoa/llm.py` | second local OpenAI-compatible server (`local2` backend, :8002) |
| `LLM_API_KEY` / `LLM2_API_KEY` | `stoa/llm.py` | optional Bearer token for an authenticated `local` / `local2` endpoint (e.g. Verda serverless); unset = no auth |
| `AGENT_ID` | eval | optional; recorded in leaderboard lines |
| `CC5K_WEIGHTS` | `raster_cc5k` | pretrained CubiCasa5K weights `.pkl` (CC BY-NC 4.0, outside git) |
| `CC5K_PYTHON` | `raster_cc5k` | Python with torch for live CPU inference (default: `.venv-cc5k/bin/python`); cached counts need none |
| `CC5K_THREADS` | `raster_cc5k` | optional; CPU threads for live inference (default 6) |
