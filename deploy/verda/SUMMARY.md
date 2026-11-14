# Verda Compute Track: team Hannah Family (Stoa "Make Blueprints Talk")

**Idea.** We answer natural-language questions about construction drawings (CAD, IFC models, PDF sheets, raster
plans and photos of them). Deterministic CAD/IFC/PDF readers answer what they can; a small fine-tuned vision model
(Qwen3-VL-8B + our LoRA adapters) handles raster plans. Every answer carries evidence (boxes, room outlines, IFC GUIDs).
Compute lets us **score every change on the whole dataset (127,044 questions) instead of a 2k sample, and train our
own adapters** instead of renting a frontier model per question.

**Verda products and why.** *GPU instances* (RTX PRO 6000 = our dev GPU, so numbers transfer 1:1; spot for short
jobs; the confidential-computing `.CC` variant keeps customer drawings encrypted in use, in Finland). *Data next to
compute* (the 10 GB dataset downloads in 21–39 s inside the datacenter). *Serverless Containers + Object Storage* for
the production endpoint (designed and scripted, not yet run: needs a human OK for new credentials).

**Three jobs, all scripted in `deploy/verda/` (stdlib API client, one instance at a time, a watcher pulls results and
deletes the instance).**

| Job | Hardware | Wall time | Cost | Result |
|---|---|---|---|---|
| 1. Full-dataset benchmark | 1× RTX PRO 6000 (.CC) | 17.2 min, 123 Q/s | €0.75 run (€4.02 incl. setup/debug) | **82.6%** of 126,208 scored (counts exact, areas ±2%); ECE 0.024; 97% right when conf ≥ 0.8; 311k evidence boxes |
| 2. The exam path at scale | 1× RTX PRO 6000, spot | 17 + 15 min | €1.06 | all 118,036 open Qs with each file uploaded **alone** vs on dataset paths: **81.9% vs 82.3%**; the paired diff found the 2 real gaps |
| 3. Training two small models (same GPU, in parallel) | 1× A100 80 GB, spot (€1.08/h) | TRAIN_TBD | COST_TBD | RESULT_TBD |

**Job 3, training (why compute).** Stoa's test sheets are clean large-format CAD drawings, and "how many doors /
windows" is one of their question kinds. A VLM sees a 14,000 × 9,900 px sheet downscaled to ~3 Mpx, where a door
shrinks to ~16 px; our CAD readers need layers that a PDF or PNG does not have. So we trained a **door/window symbol detector** where the labels cost
nothing: 547 Swiss TRAIN floors (DXF, CC BY 4.0) rendered as clean black CAD line drawings at random scales (18–90
px/m ≈ 1:50–1:200 at 150–300 dpi), line weights, wall styles (outline / poché / grey) and with/without room text,
cut into 15,276 tiles with **557,376 exact boxes** from the DOOR/WINDOW layers (leaf + swing arc; 3-line windows).
Rendering took seconds on the instance's CPUs, next to the data. Model: torchvision Faster R-CNN v2 (BSD, COCO
weights; not YOLO, whose AGPL clashes with our MIT repo), 50 min. Held-out test: 100 Swiss DEV floors × 3 scales ×
2 line weights with exact counts, plus Stoa's DfE / AFCEC sheets and a USACE A-101 sheet. DETAILS_TBD
In the same hour the GPU also trained `cc-wild`, our counting LoRA continued on TRAIN plans re-rendered as phone
photos / scans / whole sheets (labels stay exact under global transforms). WILD_TBD

**Path to production.** The app reaches the model through one env var (`LLM_BASE_URL`, plus `LLM_API_KEY`); pointing
our local pipeline at the Verda-hosted vLLM gave the same accuracy (65.0% vs 63.7% on 80 Qs; ping 9–22 ms from
Espoo). Next: vLLM + base + adapters as a Serverless Container (scale to zero, autoscale on queue depth) with the
adapters in a private EU bucket (`serverless.py`, `PRODUCTION.md`); CPU readers as a separate worker pool; the
full-dataset gate nightly and on every merge; adapter retraining as a scheduled spot job.

**How it scales.** Questions are independent per drawing. One RTX PRO 6000 + 30 vCPU saturates at 123–135 Q/s (vLLM
prefill-bound on raster plans, CPU-bound on CAD readers), so scale GPUs and vCPUs together (8 GPUs ≈ 8×), and keep
the 44% of questions that need no model call on cheap CPU nodes. A 10× bigger question set is a ~€8 job.

**Honest notes.** Stock is thin: at 16:14 no RTX PRO 6000 or B200 could be created; an 8×L40S spot node never opened
SSH (deleted after 14 min, ~€1.7). Spot jobs of 15–90 min were never pre-empted. The sealed `test` split is reported
as one aggregate only. CubiCasa-trained adapters inherit CC BY-NC (demo / research use). Total Verda spend today: SPEND_TBD.
