# Verda process log (what ran, when, on what, with which script, at what cost)

Team Hannah Family, 14–15 Nov 2026 (local time, EET). All scripts are in `deploy/verda/` (stdlib Python + bash) unless
noted; results under `$JUNCTION_SHARED/artifacts/V1/`. Rules we kept: one instance at a time, hostname `junction-*`,
the balance checked before and after every job, the instance deleted by a watcher when its job ends.

**How a job runs.** `verda.py` (Cloud API client: OAuth client credentials → `/balance`, `/instance-types`,
`/instances`; refuses a 2nd instance or a non-`junction-*` hostname) creates the instance → we `scp` a `git bundle`
of the integration branch (a thin bundle on later runs: 0.6–4 MB) + LoRA adapters → `remote_setup.sh` installs uv,
vLLM and our package and downloads Stoa's public dataset inside the datacenter (10 GB in 21–39 s) → the job script
runs → `watch_teardown.sh` (on our machine) pulls shard results every 2 min (spot-safe), merges them, writes a quick
report and deletes the instance (or keeps it when told, `NO_DELETE=1`).

| When | Instance (location) | Job · script | Cost | Result |
|---|---|---|---|---|
| 14 Nov 13:49–15:02 | 1× RTX PRO 6000 `.CC` on demand (FIN-03, €2.62/h) | full benchmark, 127,044 Qs · `run_full.sh` (vLLM Qwen3-VL-8B + our LoRAs; 256 shards × 24 procs) | €0.75 run, €4.0 incl. setup | 82.6% in 17.2 min (123 Q/s) — `runs/all` |
| 15:13–15:58 | 1× RTX PRO 6000 spot (€1.42/h) | files alone vs dataset paths, 118,036 Qs · `lone_split.py`, `chain_lone2.sh` | €1.06 | 81.9 vs 82.3% — `runs/lone2` |
| 16:14 | 8× L40S spot | never opened SSH; deleted | ≈ €1.7 lost | — |
| 16:30–18:38 | 1× A100 80 GB spot (€1.08/h) | style-robust LoRA `cc-wild` (60 min) · `launch_wild.sh`, `wild_chain.sh`; door/window detector v1 (50 min) + v2 hard negatives (25 min) · `det_chain.sh`, `det2_chain.sh` | ≈ €2.3 | LoRA +3 W / 0 L; detector 84% exact door counts on 600 held-out sheets — `wild/`, `det/remote/` |
| ~19:25 → 04:10 Sun | 1× A100 spot `junction-v1-det3` (created by the captain; kept on purpose) | detector fine-tunes v3 scans, v4 pen tables, v5 door symbols, v6/v7 pipework (25–30 min each) · `det3…det7_chain.sh`; full re-runs · `full2_chain.sh` (21:18, €0.60), `full3_chain.sh` (d31, 00:46, €0.63); files-alone sample of d32 · `full4_chain.sh` (01:37, €0.67); detector GPU twins for offload · `det_twin_up.sh`, `det_tunnel.sh` | ≈ €9.5 for 8.7 h | scans 52 → 73% exact; v5 runs in the demo; 82.61% / 82.64% re-runs; files alone = 15:30 code — `det/remote/v3…v7`, `runs/all-n2`, `runs/all-d31`, `runs/lone-d32` |
| 17:30 → | Verda Inference (EU) | text backend `STOA_TEXT_BACKEND=verda` (`stoa/llm.py`) · `experiments/v1/verda_text_bench.py` | per token | Kimi K3.1 Pro 31/32 multi-part splits |

**Spend.** €200 credits → balance €173.88 at 01:50 Sun (€26.1): ≈ €16 GPU instances, the rest Verda Inference text
calls (not all from our batch jobs). ≈ €28–29 after the 04:10 teardown.

**What did not work / limits.** On-demand RTX PRO 6000 sold out after 15:00; a B200 listed but returned 503 on
create; the `.CC` GPU needs attestation + `nvidia-smi conf-compute -srs 1`, and vLLM's v2 model runner gives garbage
under CC (`VLLM_USE_V2_MODEL_RUNNER=0`); our uplink to Verda is ~1 MB/s, so a 350 MB adapter takes 5.5 min; Serverless
Containers + Object Storage are scripted (`serverless.py`, `s3.py`) but creating their credentials needs a human OK.
