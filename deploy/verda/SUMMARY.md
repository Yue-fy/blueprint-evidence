# Verda Compute Track: team Hannah Family (Stoa "Make Blueprints Talk")

**Idea.** We answer natural-language questions about construction drawings (CAD, IFC models, PDF sheets, raster
plans and photos of them). Deterministic CAD/IFC/PDF readers answer what they can; a small fine-tuned vision model
(Qwen3-VL-8B + our LoRA adapters) handles raster plans. Every answer carries evidence (boxes, room outlines, IFC GUIDs).
Compute lets us **score every change on the whole dataset (127,044 questions) instead of a 2k sample, and train our
own adapters** instead of renting a frontier model per question.

**Verda products and why.** *GPU instances* (RTX PRO 6000 = our dev GPU, so numbers transfer 1:1; spot for short
jobs; the confidential-computing `.CC` variant keeps customer drawings encrypted in use, in Finland). *Data next to
compute* (the 10 GB dataset downloads in 21–39 s inside the datacenter). *Serverless Containers + Object Storage* for
the production endpoint (designed and scripted, not yet run: needs a human OK for new credentials). *Verda Inference*
(EU-hosted LLM API) as the text model for free-text steps, one env var (`STOA_TEXT_BACKEND=verda`): Kimi K3.1 Pro split
31/32 multi-part questions (9 languages) correctly vs 29/32 for our local 35B and 20/32 for the 8B, at 2.5 s; DeepSeek
V4.1 Flash answers in 0.18 s. End to end the pipeline gets 32/32 with either backend (+$0.03 per 1k questions).

**Four jobs, all scripted in `deploy/verda/` (stdlib API client, one instance at a time, a watcher pulls results and
deletes the instance).**

| Job | Hardware | Wall time | Cost | Result |
|---|---|---|---|---|
| 1. Full-dataset benchmark | 1× RTX PRO 6000 (.CC) | 17.2 min, 123 Q/s | €0.75 run (€4.02 incl. setup/debug) | **82.6%** of 126,208 scored (counts exact, areas ±2%); ECE 0.024; 97% right when conf ≥ 0.8; 311k evidence boxes |
| 2. The exam path at scale | 1× RTX PRO 6000, spot | 17 + 15 min | €1.06 | all 118,036 open Qs with each file uploaded **alone** vs on dataset paths: **81.9% vs 82.3%**; the paired diff found the 2 real gaps |
| 3. Training two small models (same GPU, in parallel) | 1× A100 80 GB, spot (€1.08/h) | 50 + 60 min train, + 25 min hard-negative fine-tune | ≈ €2.3 (A100 spot, 16:29–18:38) | detector: 84% exact door counts (600 held-out sheets), after the hard-negative fine-tune exact on 3 unseen US/UK sheets; cc-wild LoRA: +3 W / 0 L end to end on real-world drawings |
| 4. Scan-robust detector (v3, v4) | 1× A100 spot, kept for the night | 30 + 25 min fine-tunes; evals on the GPU in seconds per sheet | ≈ €1.1 (19:59–21:15) | scanned school sheet 54 → 100 doors (clean PDF 98), scanned A-101 10 → 13 of 13, a real HABS archive scan 3 → 4 of 4; held-out scanned prints 52 → 73% exact; clean sheets unchanged (13/13, 6/6) at a 0.9 score floor |

**Job 3, training (why compute).** Stoa's test sheets are large clean CAD drawings and "how many doors" is one of
their question kinds; a VLM sees a 14,000 px sheet shrunk to ~3 Mpx (a door ≈ 16 px), and a PDF has no CAD layers.
So we trained a **door/window symbol detector** whose labels cost nothing: 547 Swiss TRAIN floors (DXF, CC BY 4.0)
rendered next to the data as CAD line drawings at random scales / line weights / wall styles → 15,276 tiles, 557,376
exact boxes, in seconds. torchvision Faster R-CNN v2 (BSD; not YOLO, whose AGPL clashes with our MIT repo), 50 min.
Held-out (600 Swiss DEV sheets): **84% exact door counts, 96% within ±1, box precision 0.995**; windows 59% exact
(16% of Swiss DOOR entities are duplicates the dataset gold counts but no drawing shows). On an unseen US fitness-center
sheet (two plans, two ceiling plans, two 3D views) it finds **exactly the 13 doors** with a metric size check (boxes <
0.75 m dropped), 0 false on the ceiling plans; on Stoa's DfE school sheet its boxes cover 118 of the 123 door
swings an independent vector arc finder found; on a UK school plan it counts the asked windows exactly (2 and 6); on a USACE sheet all 6 doors score ≥ 0.98 but section-callout bubbles
also fire → a 25-min **hard-negative fine-tune** (callouts, ceiling tags, chairs drawn unlabelled) fixes it: **13/13 and
6/6 doors**, all DfE boxes on real door swings, Swiss dev 84.0 → 85.7%, and on the same Swiss sheets with the clutter drawn
in 14.8% → 84.7% (the first model collapsed on cluttered sheets). It also reports **leaves**: a box ≥ 1.6 m long
is a double door, so 13 doors = 15 leaves (A-101) and 6 = 9 (A-101F), both as in the door schedules. And it doubles
as a **measuring instrument**: a doorset box is ~0.97 m across (Swiss gold), so the doors themselves give the drawing
scale when the sheet's own is missing or wrong (a screenshot crop, a PNG without DPI, "1:200 @ A1" printed on A3):
estimated vs true 1:N on 7 unseen PDF sheets = 93.5 / 96, 199.6, 201.3, 200.3, 202.2, 186.7 / 200 (UK schools,
A2 and A0), 43.1 / 50 (big industrial doors), i.e. −7 … +1 % on offices and schools.
Same GPU, same hour: **`cc-wild`**, our counting LoRA continued on TRAIN plans re-rendered as phone photos / scans /
whole sheets: vs today's adapter, styled counts +2.3 / +4.0 points, clean CubiCasa +1.5, clean Swiss −2.0 (noise);
end to end on 142 real-world questions **84 → 87 right, 3 wins / 0 losses**.

**Job 4, scans (Stoa: the hardest questions are on scanned drawings).** On X1's simulated scans of Stoa-style sheets
the detector under-counted: 54 doors on the scanned DfE school sheet vs 89 on the clean PDF, 10 of 13 on the scanned
A-101. Every miss is a faint, thin swing arc beside a heavy wall. Two fine-tunes on the same synthetic Swiss tiles, on
the A100 the captain kept for the night: **v3** adds a scan simulator (skew with the boxes moved along, 150–300 dpi
loss, ink weight, broken lines, paper tone, blur, dirt, JPEG); **v4** adds CAD pen tables (heavy walls, thin grey door
pens, as US/UK offices draw them). Held-out Swiss sheets printed as scans: **52 → 73% exact door counts** (windows
24 → 49%), clean 85 → 84%. On real-world sheets: DfE scan **100 vs 98 doors on the clean PDF** (an independent vector
arc finder: ~102), scanned A-101 13 of 13 (PNG; 14 on the PDF copy), a **real HABS archive scan 4 of 4** (before: 3),
clean A-101 / A-101F still 13/13 and 6/6. One regression: the scanned A-101F now gives 7 instead of 6. Lesson: training
on scans made the model sure of real doors (scores ≈ 1.0) but scattered false boxes (dimension ticks, wall gaps) over
0.4–0.9, so at the old threshold it doubled US door counts. The service now enforces a per-model score floor (0.9).
The new models were evaluated on the instance's GPU in seconds per sheet, while our shared box sat at load 228.

**Path to production.** The app reaches the model through one env var (`LLM_BASE_URL`, plus `LLM_API_KEY`); pointing
our local pipeline at the Verda-hosted vLLM gave the same accuracy (65.0% vs 63.7% on 80 Qs; ping 9–22 ms from
Espoo). Next: vLLM + base + adapters as a Serverless Container (scale to zero, autoscale on queue depth) with the
adapters in a private EU bucket (`serverless.py`, `PRODUCTION.md`); CPU readers as a separate worker pool; the
full-dataset gate nightly and on every merge; adapter retraining as a scheduled spot job. (Today's demo also
escalates some hard questions to a hosted API, Gemini, whose processing location we do not control; the Verda path
above uses only our own models.)

**How it scales.** Questions are independent per drawing. One RTX PRO 6000 + 30 vCPU saturates at 123–135 Q/s (vLLM
prefill-bound on raster plans, CPU-bound on CAD readers), so scale GPUs and vCPUs together (8 GPUs ≈ 8×), and keep
the 44% of questions that need no model call on cheap CPU nodes. A 10× bigger question set is a ~€8 job.

**Honest notes.** Stock is thin: at 16:14 no RTX PRO 6000 or B200 could be created; an 8×L40S spot node never opened
SSH (deleted after 14 min, ~€1.7). Spot jobs of 15–90 min were never pre-empted. The sealed `test` split is reported
as one aggregate only. CubiCasa-trained adapters inherit CC BY-NC (demo / research use). Total Verda spend: €9.67 of the €200 credits by 20:20 (balance €190.33), plus the A100 spot kept 19:22–02:00 at
€1.08/h, ≈ €17 in all. The 82.6% is on Stoa's dataset questions only; uploaded sheets and scans are harder, and we
report them separately.
