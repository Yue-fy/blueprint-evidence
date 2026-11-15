# Verda Compute Track: team Hannah Family (Stoa "Make Blueprints Talk")

**Idea.** We answer natural-language questions about construction drawings (CAD, IFC models, PDF sheets, scans) and
show the evidence on the drawing. Deterministic readers answer what they can; small models we train ourselves do the
rest. Verda compute lets us **score every change on the whole dataset (127,044 questions) and train our own models**
instead of renting a frontier model per question.

**Verda products and why.** *GPU instances*: RTX PRO 6000 (= our dev GPU, so numbers transfer 1:1; the `.CC` variant
keeps drawings encrypted in use, in Finland) and A100 spot for training (€1.08/h). *Data next to compute*: the 10 GB
dataset downloads in 21–39 s inside the datacenter. *Verda Inference* (EU-hosted) as a one-env-var text backend:
Kimi K3.1 Pro split 31/32 multi-part questions (9 languages) vs 29/32 for our local 35B; DeepSeek V4.1 Flash in 0.18 s.
*Serverless Containers + Object Storage* for the production endpoint (scripted; creating credentials needs a human OK).

**Jobs** (all scripted in `deploy/verda/`: stdlib API client, one instance at a time, a watcher pulls results and
deletes the instance).

| Job | Hardware, time | Cost | Result |
|---|---|---|---|
| 1. Full benchmark | RTX PRO 6000 .CC, 17.2 min (123 Q/s) | €0.75 | **82.6%** of 126,208 scored (counts exact, areas ±2%); ECE 0.024; 97% right when conf ≥ 0.8 |
| 2. Exam path at scale | RTX PRO 6000 spot, 17 + 15 min | €1.06 | 118,036 Qs with each file uploaded **alone**: 81.9% vs 82.3% with dataset paths; the paired diff found the 2 real gaps |
| 3. Door/window detector + style-robust LoRA | A100 spot, 50 + 60 + 25 min | ≈ €2.3 | **84% exact door counts** on 600 held-out sheets; 13/13 and 6/6 doors on unseen US sheets; LoRA +3 W / 0 L on real-world drawings |
| 4. Scan-robust detector | same A100, 30 + 25 min | ≈ €1.1 | held-out scanned prints **52 → 73% exact**; scanned school sheet 54 → 100 doors (clean PDF 98); real archive scan 3 → 4 of 4 |
| 5. Full re-run, 9 h of features later | same A100, 33 min | €0.60 | **82.61 vs 82.63%**; every deterministic reader identical, only VLM answers flip (A100 vs RTX PRO 6000 noise) |
| 6. Sliding / folding / angled-leaf doors | same A100, 25 min | ≈ €0.6 | Stoa's school sheet **98 → 127 of 128 drawn doors**; unseen US sheets 5 → 7 of 8 exact |
| 7. Pipework + note-leader negatives (v6, v7) | same A100, 2 × 25 min | ≈ €1.0 | v7: unseen US sheets 8 of 8, no leader lines boxed; end to end on Stoa's sheets = v5 → v5 stays; **the demo runs v5** (gate: 4 W / 0 L vs v2 on 56 door/window rows, clean controls identical) |
| 8. Full re-run on the final gate code (d31) | same A100, 34.8 min (61 Q/s) | €0.63 | **82.64%**, every deterministic reader identical; confident-wrong answers 2,689 → 624, but a new 'unlocated count = a guess' rule makes small-plan counts under-confident (ECE 0.024 → 0.256; reported to the owner with a fix that keeps 98% right at conf ≥ 0.8) |
| 9. Exam path on the final code (d32), files alone | same A100, 37 min, 11,891-Q sample by drawing (the upload path is decode-bound: 5 Q/s) | €0.67 | accuracy **= the 15:30 code** (83.66 vs 83.65%, paired); confident-wrong 244 → 19; ECE 0.04 → 0.27 on apartment plans, a measured narrow fix (0.15) handed to its owner |

**Why a detector.** A VLM sees a 14,000 px sheet shrunk to ~3 Mpx (a door ≈ 16 px), and a PDF has no CAD layers.
Our labels cost nothing: 547 Swiss TRAIN floors (DXF, CC BY 4.0) drawn as line drawings at random scales and pens →
557,376 exact boxes, rendered in seconds next to the GPU; torchvision Faster R-CNN (BSD). Each failure on a real sheet
became a synthetic fix plus a 25–30 min fine-tune (callout bubbles, scanned faint arcs, CAD pen tables, sliding /
folding doors, pipework). The boxes also count leaves (13 doors = 15 leaves on A-101, as in its door schedule) and give
the scale when a sheet has none (−7 … +1% on 6 unseen sheets). Lessons: the scan models scatter false boxes at
0.4–0.9, so the service enforces a 0.9 score floor; and a new detector must be gated through the reader that uses its
boxes (v7's different box extents first cost 2 dorm wings through the closet rule).

**Path to production.** The app reaches its models through one env var (`LLM_BASE_URL` + `LLM_API_KEY`); our pipeline
pointed at the Verda-hosted vLLM gave the same accuracy (65.0 vs 63.7% on 80 Qs; 9–22 ms ping from Espoo). The
detector runs that way too: our local service sends each new sheet to a GPU twin on Verda (`DET_REMOTE_URL`, model
checksum checked, CPU fallback): **15 s instead of 4+ min** for a 6,000 px scan, identical boxes, so door counts on a
fresh upload fit the readers' 25–40 s budget. `det_twin_up.sh` brings a twin up in one command (293 s measured, 255 s
of it our model upload). Next: vLLM + adapters and the detector as Serverless Containers (scale to zero) with weights
in a private EU bucket (`serverless.py`, `PRODUCTION.md`), CPU readers as a worker pool, the full-dataset gate on every
merge, retraining as a scheduled spot job. (The demo also escalates some hard questions to Gemini, whose processing
location we do not control; the Verda path uses only our own models.)

**How it scales.** Questions are independent per drawing. One RTX PRO 6000 + 30 vCPU saturates at 123–135 Q/s (vLLM
prefill on raster plans, CPU on CAD readers), so add GPUs together with their vCPUs (8 GPUs ≈ 8×); the 44% of
questions that need no model run on cheap CPU nodes. A 10× bigger question set is a ~€8 job.

**Honest notes.** Stock is thin: at 16:14 no RTX PRO 6000 or B200 could be created, and an 8×L40S spot node never
opened SSH (~€1.7 lost); spot jobs of 15–90 min were never pre-empted. The 82.6% is on Stoa's dataset questions;
uploads and scans are harder and reported separately. CubiCasa-trained adapters inherit CC BY-NC (demo / research).
Spend: €26.1 of the €200 credits by 01:50 Sun (balance €173.88): ≈ €16 GPU instances, the rest Verda Inference
text calls (14 Nov: DeepSeek V4.1 Flash 3,695 requests, Kimi K3.1 Pro 697, not all from our batch jobs); the A100
(€1.08/h) stays as the detector endpoint until the 04:10 teardown, so ≈ €28–29 in total.
