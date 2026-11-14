# Router and pipeline: a playbook for new question kinds

`stoa/pipeline.py` (solver `pipeline`) is the team's one entry point. `stoa/router/` holds its parts: the question
parser (`intent.py`), compound questions (`compose.py`), the general agent (`agent.py`), per-question LLM budget
(`budget.py`), gates (`gating.py` + `gating.json`), priors (`prior.py`), canonical rewrites (`canonical.py`) and the
gate calibrator (`calibrate.py`). This page is for whoever adapts the pipeline to Stoa's new question kinds after the
noon reveal. The overnight agents stop at 11:45.

## 1. Measure before changing anything

```bash
# Stoa's examples as rows {"path": "data/...", "question": "...", "answer": "..."}; "answer" is optional (unscored)
.venv/bin/python -m stoa.eval --solver pipeline --split /path/to/stoa_examples.jsonl --no-leaderboard
# one question with its full trace, evidence and cost
.venv/bin/python experiments/w4/ask.py data/ifc_models/<sheet>.pdf "How big is room 119?" [k=v ...]
# where the points go / two runs side by side
.venv/bin/python -m stoa.eval.analyze <run_id> --baseline <run_id>
.venv/bin/python experiments/r2/compare_runs.py <run_a> <run_b> --show
```

The team's noon tools (all on night 277fe9f) wrap these steps; use them instead of writing new scripts:

| Step | Command | Owner |
|---|---|---|
| Text questions → rows with drawings attached | `experiments/r2/attach_drawings.py` (put `[tags]` FIRST unless night-R2 6920168 is merged) | R2 |
| Run + offer the rows to the expert for grading | `workbench/noon.sh SPLIT.jsonl` (or a run id), page on :8200 | H |
| IFC regression after a fix (~3 min, row-by-row vs freeze) | `experiments/w1/regress.sh [EXTRA.jsonl]` | W1 |
| Same-gold probe suite after a fix | `python3 experiments/F/probe_suite.py compare $JUNCTION_SHARED/artifacts/F/probe_suite/<tag>.json` | F |

Write the gold answers before the first run, and add 5 to 10 rewordings of each kind of your own (other
languages, typos, the way a customer would ask). A fix that only covers Stoa's exact sentences will not hold on the
hidden set.

## 2. Read the trace: which step answered

`Pipeline.solve` → `_solve_question` runs these steps in order. The first trace line tells you which step answered.

| Order | Step (code) | Trace starts with | Fires when |
|---|---|---|---|
| 0 | file check | `the file could not be opened` | path not found: prior at conf <= 0.05 |
| 1 | normalisers `normalise_numbers/refs/scope`, `name_sheet` | `question normalised (...)` (added at the end) | Swiss apostrophes, 'apt. D', 'this flat' on CubiCasa, ... |
| 2 | `_link_question` | `route: link question (<why>)` | R1's `is_link_question` fires: plan vs model, sheet sets, 'Is there an IFC model for this plan?' |
| 3 | `_front_end` (translation), then step 2 again | `language front end: ...` | not English: the local LLM translates; answers are capped at `translate_cap` unless both readings agree |
| 4 | `_per_room` (cc5k=1, CubiCasa) | `route: ... room graph (R1)` | room-scoped questions on a raster |
| 5 | `compose.decompose` → `_solve_composed` | `compound question → <op> over N parts` | sum / diff / argmax / list / yes-no comparisons of simpler questions |
| 6 | `_solve_atomic`: parse | `intent: <task> <target> [scope]` | always |
| 6a | `_raw_deterministic` | `<solver> (question as asked): ...` | a deterministic solver in the route (dxf, ifc, schedule, pdf_vector) reads the question itself; kept when conf >= 0.45 |
| 6b | `_run_agent` (`agent.py`) | `general agent first: <why>` / `agent: ...` | operators (average, share, difference, thresholds), unknown classes, or a solver flagged an operation it did not apply |
|    | `agent._finish` | `... lookup(s) of another quantity found nothing ...` | the answer is copied from one ask while an ask about a different quantity came back < 0.2 (a skipped selection step) → 0.3 |
| 6c | template cascade (gate → solvers in `route()` order) | `gate (train n=...)`, `<solver>: ...` | known question kinds |
| 6d | `_kg_crosscheck` | `cross-check: ... ✓ / ✗` | the KG has the same quantity from another file |
| 6e | `_prior_answer` | `prior[...]` | nothing else answered (never a bare refusal) |
| 7 | exit caps in `_solve_question` | `... confidence capped at ...` | count answer to a non-count question, size answer to 'how many', fractions, echoes, a ✗ cross-check |
| 8 | `_shape_guard` (in `solve`) | `answer shape check: ...` | which-floor/room/sheet answered by a number, yes/no answered by a number, how-many answered by yes/no, 'which floor has the most …' answered by a ranked list (ties exempt) → 0.3 |

## 3. Where to put a fix for a new kind

- **A wording the parser misreads** (wrong task, target or scope in the `intent:` line): `intent.py` (rules, every
  language) or a normaliser in `pipeline.py`. Run the parse over `questions.jsonl` before and after and count the
  changes. Tonight's rule was 0 changes on the dataset unless the change is the point.
- **A question about files, links or the drawing set**: R1's `experiments/r1/kg_qa` (trigger and reader) and the
  gate at the top of `_link_question`. On drawings without a project graph only file-subject questions are admitted
  (`_FILE_SUBJECT`).
- **A new operator over things we can already read** (top-k, 'except', 'per storey'): `compose.py` when it splits
  into simple questions; else the agent (`agent.py` tools: ask, table, rows, find_text, look, calc, answer).
- **A new quantity on one file type**: the owning solver (ifc = W1, dxf = W2, raster = W3, schedule / pdf_vector,
  see docs/TEAM.md). The pipeline asks it as asked first (step 6a), so a fix there needs no router change.
- **Confident but wrong shape or scope**: add an exit check (step 7 or 8). Cap the confidence and say why in
  `missing`. Don't change the value.

## 4. Rules that held tonight

1. **Narrow triggers.** Before running anything, count how many dataset and open-split questions a new route
   catches (`questions.jsonl` + `$JUNCTION_SHARED/results/splits/*.jsonl`). A route that should fire on 0 of them
   leaves dev/hard/hard2 unchanged by construction, so you only need to measure the new probe.
2. **Paired runs with an off switch.** Each step has a solver arg (`--solver-arg links=0`, `pvm=0`, `agent=0`,
   `compose=0`, `cc5k=0`, `lora=0`, `both_readings=0`, `translate=0`; full list in the `pipeline.py` docstring). Run the
   same rows with it on and off, and compare per row, not only the totals.
3. **A new reader checks, it does not overrule.** When two readers disagree, cap the confidence (LINK_DISAGREE 0.6,
   XCHECK_CAP 0.7, SHAPE_CAP 0.3) and name the other answer in `missing`. Don't flip a confident answer.
4. **Cost is scored.** Read tokens, calls and p95 off the eval footer. A solver that abstains after an expensive
   read (text layer, OCR, a VLM call) costs that read on every question routed to it first.
5. **Imports under threads.** The eval runs 6 worker threads. Load a solver module through the registry
   (`self._get(name)`) before a guarded `from stoa.solvers.X import f`. A bare guarded import from several threads
   at once met a half-initialised module and silently returned None.
6. **Gemini** (`stoa.llm.chat(..., backend="gemini", purpose=...)`): never inside bulk evals. The eval hides the key
   for runs over 200 rows unless `--allow-gemini` is set. The pipeline doesn't call Gemini.

## 5. Known gaps (11:25)

- **Nested selection** ('How many windows does the floor with the most doors have?', 'How many rooms does the floor
  with the most doors have?'): `compose` does not split '<quantity> on the <unit> with the most <other>', so the agent
  gets it, and on IFC it asks invented storey names ('Ground Floor', 'First Floor' → 'not found' at 0.14) instead of
  calling `table()` for the real ones. Since night-W4 3e08025 the copied whole-model answer is capped at 0.3 (was 297
  @0.95), but the value is still wrong (gold 55). Fix: a compose pattern (argmax over the storeys from the IFC fact
  card, then the quantity on that storey), or tell the agent's IFC prompt to read the storey names from `table()` first.
  W1 built the compose fix inside the ifc solver on **night-W1-post 5a6acaf** (rank floors by the other quantity, answer
  on that floor; 0/8 → 7/7 + 1 tie capped, English wordings only; not in night). Merge that first; the agent change is then
  only for other languages / other sources.
- CubiCasa window counts stay weak (~15% exact even with the LoRA).
- The agent's values read from `table()` are not 'backed' (only ask/calc results are), so a right 55 read from the IFC
  fact card scores 0.25 and loses to ifc's whole-model 297 @0.30 (run 20261114-111456-pipeline-agent_ifc-4540).
  Opt-in `STOA_AGENT_STOREYS=1` puts the model's storey names in the agent's first message (paired: noise level).
- **Agent vs an unsure rule answer (`_vs_unsure`): keep the ✓.** Over 405 unique rows in tonight's runs the agent agreed
  with a rule answer at conf 0.2-0.45 on 95 rows, right 87/94 = 93%; it disagreed on 71, right 42/63 = 67%. Agreement does
  not raise confidence there (it only writes the line), and agree rows at final conf ≥ 0.8 are 184/187 right. Don't drop
  the ✓ (W1 / Q suggested it after the 297 row; the failed-step cap 3e08025 handles that row). Open: agree rows that stay
  < 0.5 are 34/39 right (under-confident), and disagree rows at ≥ 0.8 are 61/65 (slightly over-confident). In-sample
  for tonight's fixes; measure on Stoa's examples before acting.
- Resolved since 11:00: Italian 'non sono disegnate' (3 @0.80 with the 3 window GUIDs, R1 + W1 fc01442); dxf
  '(senza balconi)' / 'alloggio X' (W2 c582e13); raster-first windows on uploads (night-W4 1b6d625, cost-neutral after
  W3 9464a6b's pre-scan).
- W2's door 'connects' column (night-W2 29cdc96, day team) needs no agent change: the agent's `table` / `rows` tools read
  only the swiss ROOMS + apartments tables, and `rows.n_doors` takes W2's new meaning (doors into the room) by itself.
  tests/test_router.py test_agent_rows_columns_exist fails if a rename breaks the rows tool.

**Day-team merge order for the router items** (none are in the final night 2daf498, S0 D24): night-W1-post (nested
selection) first, then night-W4 54ee7b4 (ranked-list shape guard, 2 fires tonight) and 44e7889 (opt-in storeys, off by
default), then night-W2 29cdc96.. (door connectivity: rows.n_doors changes meaning, see above), then R2's
artifacts/R2/vlm_lora_method_range.patch v2 (its pipeline.py hunk was reviewed). Re-run pytest and
`experiments/w1/regress.sh` after each. This README sits on top of 54ee7b4/44e7889 in night-W4, so it arrives with them.

## 6. Day additions (G1, Sat 14 Nov)

Stoa's exam = their own files uploaded in our UI (no `data/<source>/` path, often no sibling files); areas > lengths >
counts. Measure every routing change on the **upload probe** (187 dev questions re-asked on the file alone):

```bash
python -m stoa.eval --solver pipeline --split $JUNCTION_SHARED/artifacts/G1/upl_lone.jsonl   # the file alone
python -m stoa.eval --solver pipeline --split $JUNCTION_SHARED/artifacts/G1/upl_dev.jsonl    # same Qs, dataset path
```

| Step | Where | Fires on |
|---|---|---|
| `plan_rooms` (content-based PDF reader) | `stoa/solvers/plan_rooms.py`, `route()` after `pdf_vector` | PDFs outside the dataset without a DXF twin: room fill polygons + labels + printed 'Scale 1:N' → room area / total / labelled sum / count / largest / perimeter / length × width; IFC-viewer style window symbols |
| lone `.dxf` | `route()` | a DXF uploaded alone → `dxf` |
| style hook (R2) | `stoa/data.py _outside_source_cached` | CubiCasa-style rasters outside our folders → `cubicasa5k` at p ≥ 0.8 (`STOA_STYLE_GUESS=0` off) |
| object / modifier typos | `pipeline.normalise_object_typos` | 'bedoroms', 'liivng room', 'extreior walls' (English only, never quoted labels or real words one edit away) |
| relational exists | `compose._RELATIONAL` | 'Is there a door between X and Y' is never composed into exists(all doors) |
| multi-part split | `stoa/router/multipart.py`, `Pipeline._multi_part` | parts asking different quantities or referring back ('…, and what is their total area?') → local split → 'v1 ; v2' |
| Gemini escalation | `stoa/agent/gemini.py`, `escalate.py`; `gemini=1` | answers < 0.5 on non-template wordings: 1 plan call + local tools + 1 answer call |
| Nemotron verifier | `stoa/agent/verify.py`; `verify=1` | answers < 0.8: verdict shown; only 'disagree' on evidence-less answers lowers confidence |
| vision count cap | `_solve_question` | `vlm_direct` count on a PDF/DXF with no located evidence ≤ 0.5 |

Upload probe (lone file): 58.5% (night 652fe2e) → 73.2% (night-G1). Still weak there: IFC-plan door counts and
whole-model questions without the model, wall lengths/volumes (wall fills give lengths only ±20–30%), CubiCasa counts
(raster, O1), Swiss pdf_vector floor/apartment totals (confident misses at 0.93–0.96).
