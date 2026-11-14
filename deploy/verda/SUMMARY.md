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

**Three jobs, all scripted in `deploy/verda/` (stdlib API client, one instance at a time, a watcher pulls results and
deletes the instance).**

| Job | Hardware | Wall time | Cost | Result |
|---|---|---|---|---|
| 1. Full-dataset benchmark | 1× RTX PRO 6000 (.CC) | 17.2 min, 123 Q/s | €0.75 run (€4.02 incl. setup/debug) | **82.6%** of 126,208 scored (counts exact, areas ±2%); ECE 0.024; 97% right when conf ≥ 0.8; 311k evidence boxes |
| 2. The exam path at scale | 1× RTX PRO 6000, spot | 17 + 15 min | €1.06 | all 118,036 open Qs with each file uploaded **alone** vs on dataset paths: **81.9% vs 82.3%**; the paired diff found the 2 real gaps |
| 3. Training two small models (same GPU, in parallel) | 1× A100 80 GB, spot (€1.08/h) | 50 + 60 min train, + 25 min hard-negative fine-tune | ≈ €2.5 (A100 spot, 16:30–~18:50) | detector: 84% exact door counts (600 held-out sheets), 13/13 on an unseen US sheet; cc-wild LoRA: +3 W / 0 L end to end on real-world drawings |

**Job 3, training (why compute).** Stoa's test sheets are large clean CAD drawings and "how many doors" is one of
their question kinds; a VLM sees a 14,000 px sheet shrunk to ~3 Mpx (a door ≈ 16 px), and a PDF has no CAD layers.
So we trained a **door/window symbol detector** whose labels cost nothing: 547 Swiss TRAIN floors (DXF, CC BY 4.0)
rendered next to the data as CAD line drawings at random scales / line weights / wall styles → 15,276 tiles, 557,376
exact boxes, in seconds. torchvision Faster R-CNN v2 (BSD; not YOLO, whose AGPL clashes with our MIT repo), 50 min.
Held-out (600 Swiss DEV sheets): **84% exact door counts, 96% within ±1, box precision 0.995**; windows 59% exact
(16% of Swiss DOOR entities are duplicates the dataset gold counts but no drawing shows). On an unseen US fitness-center
sheet (two plans, two ceiling plans, two 3D views) it finds **exactly the 13 doors** with a metric size check (boxes <
0.75 m dropped), 0 false on the ceiling plans; on a USACE sheet all 6 doors score ≥ 0.98 but section-callout bubbles
also fire → a 25-min **hard-negative fine-tune** (callouts, ceiling tags, chairs drawn unlabelled): FT_RESULT.
Same GPU, same hour: **`cc-wild`**, our counting LoRA continued on TRAIN plans re-rendered as phone photos / scans /
whole sheets: vs today's adapter, styled counts +2.3 / +4.0 points, clean CubiCasa +1.5, clean Swiss −2.0 (noise);
end to end on 142 real-world questions **84 → 87 right, 3 wins / 0 losses**.

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
as one aggregate only. CubiCasa-trained adapters inherit CC BY-NC (demo / research use). Total Verda spend today: ≈ €10 of the €200 credits (balance €191.82 at 18:08 with the A100 still running).
