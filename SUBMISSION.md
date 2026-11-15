# Submission: Blueprint Evidence

Junction 2026, Stoa challenge "Make Blueprints Talk". Team Hannah Family.

Ask a question about a construction drawing or a building model in plain language. You get the number, where on the
drawing it came from, how sure we are, and what it cost to answer. Demo video: [video link]. Front page:
[README.md](README.md). Method and every number: [docs/RESULTS.md](docs/RESULTS.md).

Every number of ours below names the code it was measured on (a commit in this repository's history) and the mode:
**private** = nothing leaves our server (geometry readers on CPU, Qwen3-VL-8B on our own GPU); **hybrid** = private
plus Gemini 3.1 Pro and NVIDIA Nemotron for unsure answers only.

## How we meet Stoa's four criteria

### 1. Accuracy

| What we measured | Mode | Result | Code |
|---|---|---|---|
| Stoa's four example sheets, 90 questions in Stoa's own kinds (room and type areas, baseboards, slab areas, door counts, piping) | private | **91.1%** (82 / 90) | `1389c6a` |
| The same 90 questions asked directly to Gemini 3.1 Pro / Qwen3-VL-8B, without our pipeline | hosted / local | 61%, no evidence / 26% | |
| Sibling sheets of Stoa's four drawing sets that nobody tuned on (173 questions, scored by our gatekeeper, aggregates only) | private | **65.3%** (113 / 173; 64.7% at `e4a166b`, 61.3% at `b8146dd`) | `1389c6a` |
| 20 numeric questions on Stoa's sheets measured by hand by our teammate, each with a written method | private | **19 / 20** (20 / 20 with the door detector the live demo runs); not independent, since her misses were fixed from her answers: 8 of 12 on the questions she answered after that code was built | `4b62b93`; `f2cd8f1` |
| Real drawings from outside the dataset, held out (105 questions; the same product code and rows) | private / hybrid | 48.6% / **64.8%** | `02a7a4d` / `fc0927a` |
| Stoa dataset, sealed test split (2,000 scored questions) | private | **87.7%** micro, 79.7% weighted | `bb94fe4` |
| Stoa dataset, all 127,044 questions on one Verda GPU | private | 82.6% of 126,208 scored | `b8146dd` |

What these numbers do not say: we looked at Stoa's four sheets all day, so 91% is an upper bound for a new drawing;
the better forecast is the held-out rows: 65% on sibling sheets and about half on other real drawings. On scans,
even of the sheets we tuned on, we get about half right.
Scans and photos are our weakest input. [docs/RESULTS.md](docs/RESULTS.md) has the method, the per-kind numbers and
the known weaknesses.

### 2. Visualising the reasoning so a human can verify it

- **Evidence on the drawing.** Each answer shows where it came from when a reader located it: boxes on the counted
  objects, the outline of a measured room, the measured line, the tag that was read; in an IFC model the counted
  elements light up in 3D. On Stoa's 90 questions every answer carried located evidence (`e4a166b`). The card also
  says how well the evidence backs the number: supported, approximate (for example fewer boxes than the count), or
  not located. A location shows where to look; a human still checks that it supports the number.
- **The answer card shows how the number was made**: the method, the scale used and where it was read (title block,
  scale bar or dimension string), the assumptions ("no scale bar; assumed 1:100"), what is missing to be sure, a
  confidence in words (from "very sure" to "best estimate"), the cost tier and where the data went.
- **Check it yourself in one step.** The card names the one thing to check (for example the scale and one dimension),
  and the Measure tool lets you click two points or trace a room and get the number at the same scale. In a
  project with two copies of a sheet (a scan and its PDF), both readings are put side by side ("this sheet: 245.6 m²
  · the PDF copy: 246.46 m² · they agree").
- **The sheet checks itself.** Self-check compares a sheet with itself (an area table against its rows, a dimension
  chain against its total, printed room areas against the rooms as drawn) and outlines what agrees and what does not
  (an optional feature, `selfcheck`; on in the live demo).
- **A human audit, not only a score.** A teammate checking 34 answers on our review page needed a median of 79 s
  per answer. Her hand-measured answers on Stoa's sheets, each with a written method, are an evaluation set, so
  every correction she made became a test that every new build is checked against (19 of 20 at `4b62b93`, 20 of 20 with the door detector the live demo runs). A
  one-click A4 measurement report carries the crop, the formula, the scale, the method and a "Checked by" line.

### 3. Creativity

- **A project, not a sheet.** Upload several sheets of one building: their roles and levels are recognised, building
  totals avoid double counting (sections, ceiling plans, duplicates), and answers are cross-checked across formats;
  a scanned print uploaded with its original is checked against the original's revision ("Out of date: this print
  is revision C01, the original is C02").
- **Measuring conventions.** One switch recounts every room and total as net floor area, to wall centre lines or
  gross; skirting with or without wet rooms; door openings deducted or not; doors as sets or as leaves.
- **From quantities to an order list.** The takeoff becomes what you would order: skirting in stock lengths with
  waste, doors by size, floor covering by room type, each line outlined on the drawing.
- **The cost of a revision.** Drop a newer issue of a sheet (even a scan or a photo): it is aligned with the old one,
  the changes are marked, and the change is priced (floor, doors, walls) (optional, `STOA_FEATURES=revcompare`, not
  switched on in the live demo).
- **Measured by you.** When you correct a scale, mark a missed door or give the right value, the project remembers
  it, later answers use it and say so ("Measured by you"), and undo is one click.
- **A showroom model.** Our reader turns a 2D floor plan into a 3D model in the web app (`stoa/geometry/plan3d.py`);
  for the booth we turned one apartment into a 3D-printable relief, with each room's name and area as our reader
  reads them.

### 4. Low inference cost

- **Local first.** Deterministic readers (CAD, IFC, PDF vector geometry, schedules, OCR) answer what they can on CPU;
  our fine-tuned Qwen3-VL-8B runs on our own GPU; a hosted model is asked only for unsure answers, and only when the
  user allows it. On the sealed test, 56% of answers needed no model call at all.
- **Cost per 1,000 questions on Stoa's kind of sheets, private** (`e4a166b`, the 401 open Stoa-like questions; GPU
  at $2/h, CPU at $0.05 per core-hour): **$0.42**: $0.08 on vector PDFs, $0.83 on PNG pictures, $2.33 on phone
  photos; median 1.6 s, p95 15 s; no answer called a hosted model. The same rows with Gemini and Nemotron allowed
  (their replies replayed from earlier live calls and priced as if made): 323 against 322 of 381 right for $0.99
  per 1,000, and about $4 live (`4b62b93`: 315 against 314 for $1.04). Gemini 3.1 Pro asked directly costs $7.59
  per 1,000 questions, without evidence. Hybrid on the held-out real drawings: about $8 per 1,000 for +16 points
  (`fc0927a`); on Stoa's own sheets it now adds one answer in 381, so private is enough there.
- **Every answer shows its cost**: a tier badge (cheap, in-between, expensive), the hosted calls and the local GPU
  time, so cost is visible per question, not only per month.
- **The whole dataset for under one euro.** 127,044 questions in 17.2 minutes on one Verda GPU for €0.75 (`f551070`).

## Google Gemini

**The problem.** Building teams decide from drawings (vector PDFs, scans, phone photos) that no software can query. A wrong area or door count goes into a bid or permit, so each answer must show its source, checkable in seconds, at low cost.

**How Gemini is integrated.** Local readers (vector geometry, CAD/IFC parsers, OCR, a small local VLM) answer first. Gemini 3.1 Pro is called only when they are unsure: as a second reader that sees the sheet and returns the value plus a box per counted object, drawn as evidence; and as a planner for unseen question types: one call writes a tool plan our tools execute, so numbers come from tools.

**What is new.** Confidence comes from agreement between independent readings, not self-report. Gemini never overrides a located local measurement: we added that rule after its 10 windows beat our correct 18. Daily call caps, a content cache and a Private mode limit cost and data exposure.

**Why Gemini matters.** On 105 unseen archive drawings, a live hybrid run scored 65 % vs 49 % local (+16 points) at about 10× the cost. On Stoa-like sheets our local readers have caught up, so Gemini stays targeted.

## NVIDIA Nemotron

**Model.** NVIDIA Nemotron 3.5 Super VL (`nvidia/nemotron-3.5-super-vl-120b-a12b`), a vision-language reasoning model,
called through NVIDIA's hosted, OpenAI-compatible API.

**What it does in our working solution.** Nemotron is the independent second opinion on answers we are unsure about.
When an answer's confidence is below 0.8, Nemotron gets what a human checker would get: the question, our answer and how
we found it, the drawing with our evidence boxes drawn on it, and a zoomed crop of that evidence. It replies agree,
disagree or unsure, with a one-line reason and the value it reads itself. The answer card shows that verdict as a
"second opinion" line. The cost line counts the extra tokens and the API call.

**Why it matters.** Stoa asks for answers a person can verify in seconds, from a system that knows what it does not
know. Our readers are deterministic geometry and text, and they are cheap and exact when they find their evidence. When
they cannot locate it, a second model looking at the same evidence a human would is the cheapest way to catch a wrong
answer before the user trusts it. Confident answers never call it, so it adds no cost there.

**How we improved its outputs.**
1. *Grounded input.* It sees our evidence boxes on the page and a zoomed crop, not a bare sheet, so it judges our claim
   instead of solving the drawing from scratch.
2. *Structured verdict.* It returns verdict, reason and its own reading, parsed strictly. An unreadable reply counts as
   "unsure" and changes nothing.
3. *Trust calibrated on measured data.* On 40 low-confidence answers (20 right, 20 wrong), it disagreed with 8 answers
   that had no located evidence, and 7 of those 8 were wrong. We now halve the confidence in that case. When our
   evidence boxes were present, its verdicts did not separate right from wrong (4 of 5 disagreements were on right
   answers), so there they are shown, not applied. Agreement never raises confidence (57 % right against a 50 % base).
4. *It never changes the number.* The verdict moves confidence and adds a visible warning. The value always comes from
   the drawing.
5. *Cost-aware.* It runs only below 0.8 confidence, at about 2,100 input and 45 output tokens per check (median
   8.7 s). It is refused in Private mode, where nothing leaves our server.

**Running in the demo.** Nemotron runs live on uncertain answers when Private mode is off. Our benchmark runs replay
cached model replies and make no new hosted calls, so they do not measure it live. The figures above come from a
dedicated 40-answer verification run.

## Verda

The process, the numbers and the path to scale: [docs/VERDA.md](docs/VERDA.md).

**Ambition.** We used Verda to do what our shared dev box cannot: score every change on Stoa's *whole* dataset
(127,044 questions on 5,939 drawings) instead of a 2,000-question sample, and train our own vision model overnight.
One RTX PRO 6000 answered all 127,044 questions in 17.2 minutes for €0.75 (82.6% right; counts exact, areas ±2%).
We re-ran the full set on each night's integration code (82.61%, then 82.64% on the final gate code, every
deterministic reader identical). On spot A100s we trained a door/window symbol detector from 557,376 free synthetic
boxes, then fine-tuned it six times as real sheets exposed failures (scans, sliding / folding doors, pipework).

**Fit.** *GPU instances*: RTX PRO 6000 is our dev GPU, so numbers transfer 1:1, and the `.CC` variant keeps drawings
encrypted in use in Finland. *Spot A100* (€1.08/h) for 25–50 min training jobs, never pre-empted. *Data next to
compute*: the 10 GB dataset downloads in 21–39 s inside the datacenter. *Verda Inference* (EU-hosted) is a
one-env-var text backend (Kimi K3.1 Pro split 31/32 multi-part questions). Every job is scripted: a stdlib API
client creates one instance, ships a git bundle, runs, and a watcher pulls the results and deletes the instance.

**Working prototype.** The demo runs the detector we trained on Verda (v5; chosen by our gate at 4 wins / 0 losses
vs the previous model, every clean control unchanged). Exam path at scale: every dataset file uploaded alone, the
way Stoa tests; on the final code (11,891-question sample) accuracy held and confident-wrong answers fell from 244 to under 60.
GPU offload prototype: our detector service sends a new sheet to a GPU twin on Verda: 15 s instead of 4+ min for a
6,000 px scan, identical boxes; one script brings a twin up in 293 s. Spend: ≈ €28 of the €200 credits by 03:20 Sun (≈ €30 when the last instance is deleted at 05:10).

**Path to scale.** Questions are independent per drawing. One RTX PRO 6000 + 30 vCPU serves 123–135 dataset questions/s,
so we add GPUs together with their vCPUs (8 GPUs ≈ 8×); a 10× larger question set is a ~€8 job. A fresh upload
costs more (the file is read from scratch: ~5 questions/s per A100, decode-bound), which is what autoscaling is for. Production: our
fine-tuned vLLM model and the detector as Verda Serverless Containers (scale to zero, EU-hosted, weights in a
private bucket; scripted, not yet run), CPU readers as a worker pool, the full-dataset gate on every merge, and
retraining as a scheduled spot job.
