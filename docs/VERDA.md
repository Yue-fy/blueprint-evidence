# Verda: how we used it

This is the process document for the Verda Compute Track. Short version: we used Verda GPUs to **score every change
on the whole Stoa dataset** (127,044 questions) and to **train our own door and window detector**, instead of renting
a frontier model per question. The scripts are in [`deploy/verda/`](../deploy/verda/); the job-by-job log (when, which
instance, which script, what it cost, what it gave, and what did not work) is
[`deploy/verda/PROCESS.md`](../deploy/verda/PROCESS.md), the numbers are in
[`deploy/verda/SUMMARY.md`](../deploy/verda/SUMMARY.md) and the production plan
[`deploy/verda/PRODUCTION.md`](../deploy/verda/PRODUCTION.md). All runs were on 14 and 15 November 2026.

## Ambition

Construction drawings are huge (a 14,000 px sheet shrinks to about 3 megapixels for a vision model, so a door is about
16 px) and every customer's drawings are confidential. We wanted answers that come from our own readers and our own
small models, measured on the full dataset after every change, so that a gain on the sheets we looked at is not
mistaken for a gain everywhere. On our shared workstation one full run takes hours; on one Verda GPU it takes 17
minutes.

## Fit: which Verda services, and why

| Service | What we ran on it | Why this one |
|---|---|---|
| GPU instance, RTX PRO 6000, confidential-computing variant (`.CC`), Finland | The full benchmark: our whole pipeline with Qwen3-VL-8B and our own LoRA adapters served by vLLM | The same GPU as our workstation, so numbers transfer one to one; the drawing stays encrypted while in use, and in the EU |
| GPU instance, A100 80 GB spot | Training and fine-tuning the door and window symbol detector; later full re-runs | Spot price for jobs of 15–90 minutes; none was pre-empted |
| Data next to compute | The 10 GB dataset downloaded inside the datacenter in 21–39 s | No upload of the dataset from our machine |
| Verda Inference (EU-hosted) | A text backend for multi-part questions (`STOA_TEXT_BACKEND=verda`) | One setting; it keeps text steps in the EU |
| Serverless Containers + Object Storage | The production endpoint (scripted, see Path to scale) | Scale to zero between customers, weights in a private EU bucket |

## The process, step by step

1. **One instance at a time, deleted when the job ends.** A small client with no dependencies (`deploy/verda/verda.py`)
   creates a spot or on-demand instance, reads its address and deletes it. Credentials come from the environment and
   are never printed.
2. **Ship the code, fetch the data next to the GPU.** `remote_setup.sh` installs the environment on the instance from a
   git bundle of our code and downloads Stoa's public dataset there.
3. **Run the job.** `run_full.sh` starts vLLM (`vllm_up.sh`) and runs the whole dataset in shards (`shard.py`);
   `progress.sh` reports the rate.
4. **Pull the results, then tear down.** `watch_teardown.sh` copies the predictions back and deletes the instance;
   `report.py` and `compare_runs.py` score them and compare two runs bucket by bucket (which answers changed, and
   whether a deterministic reader changed at all).

## Working prototype: the numbers

| Job | Hardware, time | Cost | Result |
|---|---|---|---|
| Full benchmark (`f551070`) | 1× RTX PRO 6000 `.CC`, 17.2 min (123 questions/s) | €0.75 | 82.6% of 126,208 scored questions right (counts exact, areas and lengths within 2%); 97% right among answers marked sure |
| Uploads at scale: each file uploaded alone, as Stoa tests | RTX PRO 6000 spot, 17 + 15 min | €1.06 | 118,036 questions: 81.9% against 82.3% with dataset paths; the paired comparison found the 2 real gaps |
| Door and window detector, trained from free synthetic labels | A100 spot, 50 min | about €1 | 547 Swiss CAD floors (CC BY 4.0) drawn as line drawings → 557,376 exact boxes; 84% exact door counts on 600 held-out sheets; 13 of 13 and 6 of 6 doors on two unseen US sheets |
| Fine-tunes for scans and for sliding, folding and angled doors | same A100, 25–30 min each | about €0.5 each | held-out sheets printed as scans: 52% → 73% exact door counts; Stoa's school sheet: 98 → 127 of 128 drawn doors |
| Full re-run on the final gate code (`b8146dd`) | same A100, 34.8 min | €0.63 | 82.64%; every deterministic reader identical to the earlier run; only model answers moved, in both directions (run-to-run noise) |

In the product: the detector trained on Verda is behind door and window counts on uploaded sheets (the gate chose its
latest version for the demo: 4 won, 0 lost on 56 door and window questions, every clean control identical). The
detector's training and serving code lives in the team's workspace repository; this repository calls it as an
optional service (`STOA_DET_URL`) and answers without it.

Spend: about €26 of the €200 in credits by 02:00 on Sunday 15 November, most of it on GPU instances and the rest
on Verda Inference text calls. The last instance, a detector endpoint for the night, is deleted at the end of the event.

## Path to scale

- **One setting to move the model.** The app reaches its model through `LLM_BASE_URL` (and `LLM_API_KEY`). Pointed at
  vLLM on a Verda GPU it gave the same accuracy as our own server (65.0% against 63.7% on 80 questions, 9–22 ms ping
  from Espoo). The **EU only** mode in the README is exactly this.
- **The detector too.** Our detector service can send each new sheet to a GPU twin on a Verda instance
  (`DET_REMOTE_URL`, model checksum checked per request, CPU fallback): 5–15 s instead of minutes for a new 6,000 px
  sheet, with identical boxes.
- **Next: Serverless Containers.** vLLM with our adapters and the detector as Verda Serverless Containers that scale
  to zero between customers, weights in a private Object Storage bucket in Finland (`deploy/verda/serverless.py`,
  `s3.py`; scripted, not run: creating the bucket credentials needs a human's approval). The CPU readers become a
  separate worker pool, because a full run is CPU-bound on the CAD readers.
- **How it scales.** Questions are independent per drawing. One RTX PRO 6000 with 30 vCPUs saturates at 123–135
  questions per second; add GPUs together with their vCPUs (8 GPUs ≈ 8×). The 44% of questions that need no model run
  on cheap CPU nodes. A question set ten times larger is a job of about €8.
- **Every merge gated on the whole dataset**, and retraining as a scheduled spot job.

## Honest notes

- Stock was thin: at one point no RTX PRO 6000 or B200 could be created, and one 8× L40S spot node never opened SSH
  (about €1.7 lost).
- The 82.6% is on Stoa's dataset questions. Uploads, real drawings and scans are harder and are reported separately
  in [RESULTS.md](RESULTS.md).
- Adapters trained on CubiCasa5K inherit its non-commercial licence ([DATA_AND_LICENCES.md](DATA_AND_LICENCES.md)).
- The hybrid mode also sends some hard questions to Gemini, whose processing location we do not control; the Verda
  path uses only our own models.
