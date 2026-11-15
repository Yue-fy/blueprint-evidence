# Junction platform submission: copy-paste fields (RL, Sun 04:4x; FINAL for night `a5834c86` = the demo since S0's web swap F 04:31 (= 016824ef of swap E 04:13, d34b 1389c6a3 + web layer, + the marks-chip revert); earlier: freeze 75f9ee4e)

Status: FINAL TEXT. Numbers name their code; every commit named is in the history of the release (`2a764ce1`).
Description restructured around Stoa's four judging criteria (captain 02:00 (a)); tech paragraphs = P1 / E1 / V1's
final texts. Claims follow AUDIT 'Claims to change' and RESEARCH/VERIFIED.md, and name as live only the features the
demo has switched on (others: 'in the repository').
---

## Project name (≤ 30 characters)

Blueprint Evidence

## Punchline (≤ 200 characters)

Ask a construction drawing a question in plain words: get the area, length or count, the evidence drawn on the sheet, how sure we are, and what the answer cost.

## Description (≤ 5,000 characters, Markdown)

**The problem.** Quantity takeoff is still done by hand. A Finnish study of 18 experts calls the current process only
seemingly digital, mostly done from floor plans (TATE2030 TH15, Tampere University, 2026). A frontier model asked
directly gets 61% of Stoa's own questions right and shows no evidence, so nobody can check it.

**What it does.** Drop a drawing (vector PDF, DXF, DWG, IFC, PNG or JPEG, a scan, a phone photo of a print) and ask in
your own words and language: room and room-type areas, skirting lengths minus door openings, slab areas by type, door
and window counts, piping tags and pipe sizes (we do not trace pipe networks). You get the number, the evidence drawn on the sheet, how sure we are, what
it cost and where the data went. On scans and photos we OCR the text and find the scale in the title block, a scale
bar or the dimension strings; with no scale we give a best estimate, say so, and let you fix it with one known length.

### How we meet Stoa's four criteria

**1. Accuracy** (each number names its code and mode; private = nothing leaves our server)

| Test | Mode | Result | Code |
|---|---|---|---|
| Stoa's four example sheets, 90 questions (we tuned on these) | private | 91.1% | 1389c6a |
| Same 90 asked directly to Gemini 3.1 Pro | hosted | 61%, no evidence | |
| Sibling sheets nobody tuned on (173) | private | 65.3% | 1389c6a |
| Real drawings from outside the dataset, held out (105) | private / hybrid | 48.6% / 64.8% | 02a7a4d / fc0927a |
| Expert calibration: 20 hand-measured answers | private | 19 / 20 (20 with the demo's detector) | 1389c6a |
| Stoa dataset, sealed test (2,000) | private | 87.7% | bb94fe4 |
| Stoa dataset, all 127,044 questions, on one Verda GPU | private | 82.6% | b8146dd |

Answers we call sure (confidence 0.8 or more) were right 189 times in 191 on the open Stoa-like set (1389c6a), and
98.3% of the time on the whole dataset (80,653 sure answers of 127,044). By part: CAD floor plans 99.3%, IFC models 100%.

**2. Reasoning a human can verify.** Boxes on counted objects, room outlines, measured lines, counted IFC elements lit
up in 3D. The card shows the method, the scale and where it was read, the assumptions, what is missing, and whether
the evidence supports the number, is approximate or was not located. Measure lets you click a length or trace a room
at the same scale; Self-check compares the sheet with itself (area tables, dimension chains, printed room areas
against the rooms drawn). The check list ranks every takeoff row by how likely it is to be wrong ("check these 6 of 149
first"); one click makes an A4 measurement report. Our expert needed a median of 79 s to check an answer.

**3. Creativity.** A project, not a sheet: several sheets of one building, levels and sheet roles recognised, building
totals without double counting, cross-checks across formats. One switch changes the measuring convention (net, wall
centre lines, gross). The takeoff becomes an order list. Corrections are remembered as "Measured by you". Ask about
a box drawn on part of a sheet; door sizes follow the sheet's references to the door schedule. Paste a list of
questions and get a table (add ?features=batch to the demo link). Compare two issues of a sheet: every change marked and priced (sample 'Primary school, first floor: two issues compared').

**4. Low inference cost.** Private by default. Cheap deterministic readers first (CAD, IFC, PDF geometry, schedules, OCR), our fine-tuned
Qwen3-VL-8B on our own GPU next, Gemini 3.1 Pro and NVIDIA Nemotron only for unsure answers and only when you allow
it. About €0.005 per 1,000 questions: all 127,044 Stoa dataset questions cost €0.63 on one Verda A100 spot GPU;
median answer 1.6 s. Allowing Gemini and Nemotron on 381 Stoa-like questions: one more right answer (323 vs 322) at
about 2.3× the cost. Every answer shows
a cost-tier badge. In Private mode no hosted model is asked, and for an upload the server also refuses every outside connection while it answers (checked from the operating system).

**Limits.** 91% is an upper bound: on sibling sheets nobody tuned on we get 65% right, on other real drawings about
half, and on scans, even of the sheets we tuned on, about half. "Sure" means sure on the data it was measured on; on new drawings treat it as a hint. Long
sets of 25+ pages do not always find the right page. The first answer on a new scan can take minutes.

**Team Hannah Family:** Yue, Erin, Joanna, Ziying. Code: MIT. Drawings keep their own licences.

## Tech challenge usage

**Verda** (V1's final text, 02:25, in the Verda track's four criteria):

**Ambition.** We used Verda to do what our shared dev box cannot: score every change on Stoa's *whole* dataset
(127,044 questions on 5,939 drawings) instead of a 2,000-question sample, and train our own vision model overnight.
One RTX PRO 6000 answered all 127,044 questions in 17.2 minutes for €0.75 (counts exact, areas ±2%).
We re-ran the full set on each night's integration code as a regression gate; the final demo code took 34.8 min on a
spot A100 for €0.63, ≈ €0.005 per 1,000 questions, every deterministic reader identical, and the answers it marks as
sure were right 98.3% of the time. On spot A100s we trained a door/window symbol detector from 557,376 free synthetic
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
6,000 px scan, identical boxes; one script brings a twin up in 293 s. Spend: ≈ €30 of the €200 credits; the last instance was deleted at 05:20 Sun.

**Path to scale.** Questions are independent per drawing. One RTX PRO 6000 + 30 vCPU serves 123–135 dataset questions/s,
so we add GPUs together with their vCPUs (8 GPUs ≈ 8×); a 10× larger question set is a ~€8 job. A fresh upload
costs more (the file is read from scratch: ~5 questions/s per A100, decode-bound), which is what autoscaling is for. Production: our
fine-tuned vLLM model and the detector as Verda Serverless Containers (scale to zero, EU-hosted, weights in a
private bucket; scripted, not yet run), CPU readers as a worker pool, the full-dataset gate on every merge, and
retraining as a scheduled spot job.

*Short version (~137 words), if the field wants one paragraph:* We used Verda GPUs to test and train at full scale. A small client with no dependencies creates one GPU
instance, ships our code, downloads Stoa's public dataset next to the GPU, runs the job, and a watcher pulls the
results and deletes the instance. On one RTX PRO 6000 with confidential computing in Finland, our whole pipeline
(Qwen3-VL-8B with our own LoRA, served by vLLM) answered all 127,044 dataset questions in 17 minutes for €0.75, so
every change can be scored on the full set; the final code ran there in 34.8 min on an A100 for €0.63 (≈ €0.005 per
1,000 questions), with every deterministic reader identical. On A100 spot instances we trained the door and window symbol detector
behind door counts on uploads, from free synthetic labels, and fine-tuned it for scans. Process document:
docs/VERDA.md in the repository.

**Google Gemini** (P1's final text, v6, four parts, 195 words):

**The problem.** Building teams decide from drawings (vector PDFs, scans, phone photos) that no software can query. A wrong area or door count goes into a bid or permit, so each answer must show its source, checkable in seconds, at low cost.

**How Gemini is integrated.** Local readers (vector geometry, CAD/IFC parsers, OCR, a small local VLM) answer first. Gemini 3.1 Pro is called only when they are unsure: as a second reader that sees the sheet and returns the value plus a box per counted object, drawn as evidence; and as a planner for unseen question types: one call writes a tool plan our tools execute, so numbers come from tools.

**What is new.** Confidence comes from agreement between independent readings, not self-report. Gemini never overrides a located local measurement: we added that rule after its 10 windows beat our correct 18. Daily call caps, a content cache and a Private mode limit cost and data exposure.

**Why Gemini matters.** On 105 unseen archive drawings, a live hybrid run scored 65 % vs 49 % local (+16 points) at about 10× the cost. On Stoa-like sheets our local readers have caught up, so Gemini stays targeted.

**NVIDIA Nemotron** (E1's final text, 02:3x, 433 words):

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

*Short version (~155 words):* Model: NVIDIA Nemotron 3.5 Super VL. What it does: in hybrid mode it is the visible second
opinion on unsure answers. It sees the question, our answer, how we found it, and the sheet with our evidence marked
plus a zoomed crop, and replies agree, disagree or unsure with a reason and its own reading, shown on the answer
card. Why it matters: checking is the human's cost, and Nemotron points the checker at the answers to look at first;
it never changes the number. How we improved its outputs: we give it our evidence instead of the bare sheet, ask for
a structured verdict, and measured what its verdicts are worth. On 40 unsure answers its disagreement with answers
whose evidence was not located was right 7 times in 8, so those drop to half confidence; with evidence boxes it did
not separate right from wrong, so it is shown, not applied.

## Links

- Repository: https://github.com/Yue-fy/blueprint-evidence
- Live demo: https://aalto-trx50-ai-top.tailaec1b5.ts.net:10000 (open, no password)
- Video, product demo (2:28): https://youtu.be/G-aLb5OFr9Y
- Launch film (90 s): https://youtu.be/alYQhzlKdLo

## How to test us (for Stoa's engineers)

1. Open the live demo (no password). Start with one of your four example sheets in the start
   view, or drop your own file: PDF, PNG or JPEG, a scan or a phone photo, DXF, DWG or IFC.
2. Ask in your own words, in any language: "What is the area of the laundry?", "Linear metres of skirting in the
   main hall, minus door openings", "How many doors on this floor?", "Area of the 300 mm slab".
3. Check the answer: the outline or boxes on the drawing, the scale and where it was read, the method and the
   assumptions. Use Measure to click one known length yourself and compare.
4. Private mode is on when the demo opens: nothing leaves our server. Switch it off in the top bar to let Gemini
   and Nemotron check unsure answers. The card says where the data went and what the answer cost.
5. Open the quantity takeoff for the whole sheet: the check list says which rows to check first. Switch the
   measuring convention, turn it into an order list, and export the CSV. With several sheets of one building,
   upload them as one project and ask for a building total.

---

### Notes for the captain (not for the platform)

- Character counts (`wc -m`, 02:5x): see the 'Field lengths' line at the end; all fields are under their limits.
- Features named as live in the Description / test steps are the ones the demo API (:8100, 016824ef's answer code since S0's swap E at 04:13;
  STOA_FEATURES unchanged, read from the :8100 and :5180 processes at 04:32) has: privacy, region, corrections, project, takeoff, orderlist, conventions, checklist,
  report, selfcheck, xref, and since 05:31 revcompare (revision cost; S0 swap H). Off: batch (question lists), trust_map.
  Trust map and question lists (batch) are in the repository but off in the demo.
- The demo opens in Private mode (S0 D62 5, since 75f9ee4e), so Nemotron and Gemini run only after a visitor switches
  Private off (step 4 says so; the NVIDIA text's 'Running in the demo' line says the same). NVIDIA asks that Nemotron
  run in the working solution: worth one check that the demo with Private off reaches it.
- The repository link stays private until you make it public at submission. Its main = 1ea103a (RL 04:41: release/a5834c86 = night
  a5834c86 = the live demo since S0's web swap F 04:31, FULL_ENV 11/11, scan 0, T2 pre-push check PASS). The repository's
  SUBMISSION.md and this platform text both have V1's '82.65% on the final demo code'. Outside git, the demo's web also
  loads captain-demo/hotfix.css (your Sat 21:2x hotfix: hides the drawing-vs-3D 'Differences found' pill + panel); the
  repository's web shows that panel.
- Verda's 'Spend: ≈ €26 of the €200 credits by 02:00 Sun' is V1's dated figure. The last instance is deleted at
  04:10; V1's board then has the exact spend (V1 02:52: ≈ €28.6; 'about €29 of the €200 in credits' if you want the
  final figure). No prize amounts anywhere (checked: none in this file).
- Gemini paragraph = P1's final text, unchanged. Held-out row = the same-product-code pair P1 named at 03:00
  (02a7a4d Gemini off 51 / 105 vs fc0927a Gemini on 68 / 105 on the same rows: +16.2 points); the older 49.5% was
  a gate run on a commit outside the release history.
- Field lengths (characters, 03:1x, `len()` of each field as pasted): name 18 / 30, punchline 160 / 200, description 4297 / 5,000, Verda 2450, Gemini 1150, NVIDIA 2581 (words 412 / 197 / 433; short versions follow the Verda and NVIDIA texts).
