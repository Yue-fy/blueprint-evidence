# Tech Challenge Participation: Verda (paste into the submission description)

**How we used Verda (≈140 words).** We used Verda GPUs to test and train at full scale. A small stdlib client creates one spot GPU instance, ships a git bundle of our code, downloads
Stoa's public dataset next to the GPU, runs the job, and a watcher pulls the results and deletes the instance. On one
RTX PRO 6000 (confidential-computing variant, Finland) our whole pipeline, Qwen3-VL-8B with our own LoRA served by
vLLM, answered all 127,044 dataset questions in 17 minutes for €0.75, so every change can
be scored on the full set. On an A100 spot instance we trained a door/window symbol detector from free synthetic
labels (Swiss CAD floors drawn as line drawings), then fine-tuned it for scanned prints, the case Stoa called hardest.
Verda Inference (EU-hosted) is a one-variable text backend. Path to production: the same model as a Verda Serverless
Container, so customer drawings stay in the EU.

**Facts (all on 14 Nov 2026; runs under `$JUNCTION_SHARED/artifacts/V1/`).**
1. Full benchmark, 14:35–14:52: **127,044 questions in 17.2 min (123 Q/s) on 1× RTX PRO 6000 .CC, €0.75 for the
   run**; 82.6% of 126,208 scored rows right (counts exact, areas/lengths ±2%) — `runs/all/report.txt`.
2. Detector training, 16:53–17:45: **50 min on 1× A100 80 GB spot (€1.08/h)**, 547 Swiss TRAIN floors → 557,376 exact
   boxes; 600 held-out Swiss sheets: 84% exact door counts, box precision 0.995; + a 25-min hard-negative fine-tune:
   13/13 and 6/6 doors on two unseen US sheets (door schedules as gold) — `det/remote/model`, `det/remote/v2`.
3. Scan fine-tunes, 19:59–21:13 (det3 30 min + det4 25 min, same A100): held-out Swiss sheets printed as scans
   **52 → 73% exact door counts** (windows 24 → 49%), clean 85 → 84%; scanned DfE school sheet 54 → 100 doors (clean
   PDF 98), a real HABS archive scan 3 → 4 of 4 — `det/remote/v3`, `det/remote/v4`.
4. Spend: **€11.00 of the €200 credits by 21:30** (balance €189.00), ≈ €16 with the A100 kept to 02:00.
5. In the product: the detector behind door counts on uploaded sheets (v2; the scan model v4 is a gated candidate),
   Verda Inference as a text backend option (`STOA_TEXT_BACKEND=verda`), the full-dataset benchmark scripts
   (`deploy/verda/`). Future: the cc-wild LoRA (+3 W / 0 L on 142 real-world questions; needs one vLLM restart),
   the Serverless Container endpoint (scripted, not run), the full-dataset gate on every merge.
