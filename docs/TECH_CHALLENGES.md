# Blueprint Evidence: how we used Verda, Google Gemini and NVIDIA Nemotron

Team Hannah Family, Junction 2026, Stoa challenge "Make Blueprints Talk".

- Live demo (no password): https://aalto-trx50-ai-top.tailaec1b5.ts.net:10000
- Demo video (2:28): https://youtu.be/G-aLb5OFr9Y
- Launch film (90 s): https://youtu.be/alYQhzlKdLo
- Source code: https://github.com/Yue-fy/blueprint-evidence

## Verda

In the Verda track's four criteria.

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

**Short version.** We used Verda GPUs to test and train at full scale. A small client with no dependencies creates one GPU
instance, ships our code, downloads Stoa's public dataset next to the GPU, runs the job, and a watcher pulls the
results and deletes the instance. On one RTX PRO 6000 with confidential computing in Finland, our whole pipeline
(Qwen3-VL-8B with our own LoRA, served by vLLM) answered all 127,044 dataset questions in 17 minutes for €0.75, so
every change can be scored on the full set; the final code ran there in 34.8 min on an A100 for €0.63 (≈ €0.005 per
1,000 questions), with every deterministic reader identical. On A100 spot instances we trained the door and window symbol detector
behind door counts on uploads, from free synthetic labels, and fine-tuned it for scans. Process document:
[docs/VERDA.md](VERDA.md).

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

**Short version.** Model: NVIDIA Nemotron 3.5 Super VL. What it does: in hybrid mode it is the visible second
opinion on unsure answers. It sees the question, our answer, how we found it, and the sheet with our evidence marked
plus a zoomed crop, and replies agree, disagree or unsure with a reason and its own reading, shown on the answer
card. Why it matters: checking is the human's cost, and Nemotron points the checker at the answers to look at first;
it never changes the number. How we improved its outputs: we give it our evidence instead of the bare sheet, ask for
a structured verdict, and measured what its verdicts are worth. On 40 unsure answers its disagreement with answers
whose evidence was not located was right 7 times in 8, so those drop to half confidence; with evidence boxes it did
not separate right from wrong, so it is shown, not applied.
