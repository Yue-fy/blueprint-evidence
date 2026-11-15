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
   run**; 82.6% of 126,208 scored rows right (counts exact, areas/lengths ±2%) — `runs/all/report.txt`. Re-run on
   tonight's code, 21:18–21:52 on the A100 (€0.60): 82.61%, no regression in any reader — `runs/all-n2/compare_vs_all.txt`;
   on the final gate code (d31), 00:46–01:21 Sun (€0.63): 82.64%, deterministic readers identical — `runs/all-d31/`.
2. Detector training, 16:53–17:45: **50 min on 1× A100 80 GB spot (€1.08/h)**, 547 Swiss TRAIN floors → 557,376 exact
   boxes; 600 held-out Swiss sheets: 84% exact door counts, box precision 0.995; + a 25-min hard-negative fine-tune:
   13/13 and 6/6 doors on two unseen US sheets (door schedules as gold) — `det/remote/model`, `det/remote/v2`.
3. Scan fine-tunes, 19:59–21:13 (det3 30 min + det4 25 min, same A100): held-out Swiss sheets printed as scans
   **52 → 73% exact door counts** (windows 24 → 49%), clean 85 → 84%; scanned DfE school sheet 54 → 100 doors (clean
   PDF 98), a real HABS archive scan 3 → 4 of 4 — `det/remote/v3`, `det/remote/v4`. 21:53–22:24 (v5, 25 min): sliding /
   folding / angled-leaf door symbols → Stoa's school sheet 98 → 127 of 128 drawn doors (expert count) — `det/remote/v5`.
4. Spend: **€26.1 of the €200 credits by 01:50 on 15 Nov** (balance €173.88): ≈ €16 GPU instances, the rest Verda
   Inference text calls; the A100 is kept as the detector endpoint to the 04:10 teardown (≈ €28–29 in total).
5. In the product: the detector behind door counts on uploaded sheets (v5, trained on Verda, chosen for the demo at 01:47 Sun by
   the gate: 4 W / 0 L vs v2 on 56 door/window rows, every clean control identical — `det/ab_gate/table.txt`),
   Verda Inference as a text backend option (`STOA_TEXT_BACKEND=verda`), the full-dataset benchmark scripts
   (`deploy/verda/`). Future: the cc-wild LoRA (+3 W / 0 L on 142 real-world questions; needs one vLLM restart),
   the Serverless Container endpoint (scripted, not run), the full-dataset gate on every merge. Measured prototype of
   that path (22:50): our detector service sends each new sheet to a GPU twin on the Verda A100 (one env var, model
   checksum checked, CPU fallback): **15 s instead of 4+ min** for a 6,000 px scan, identical boxes — `det_server.py`.

## Platform text (final)
(V1, 15 Nov 02:25. Numbers dated 14–15 Nov 2026; runs under `$JUNCTION_SHARED/artifacts/V1/`. No prize amounts.)

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
6,000 px scan, identical boxes; one script brings a twin up in 293 s. Spend: ≈ €26 of the €200 credits by 02:00 Sun.

**Path to scale.** Questions are independent per drawing. One RTX PRO 6000 + 30 vCPU serves 123–135 dataset questions/s,
so we add GPUs together with their vCPUs (8 GPUs ≈ 8×); a 10× larger question set is a ~€8 job. A fresh upload
costs more (the file is read from scratch: ~5 questions/s per A100, decode-bound), which is what autoscaling is for. Production: our
fine-tuned vLLM model and the detector as Verda Serverless Containers (scale to zero, EU-hosted, weights in a
private bucket; scripted, not yet run), CPU readers as a worker pool, the full-dataset gate on every merge, and
retraining as a scheduled spot job.
