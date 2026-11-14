"""Merge the per-shard prediction files of one Verda run and report it: overall (stoa.eval.runner.summarize, the
same metrics as every leaderboard line), per split (smoke/dev/test/train/unused, joined by qid with
$JUNCTION_SHARED/results/splits) and per source x type; throughput and wall time from the run's t0/t1.
The sealed `test` split is reported as ONE aggregate line only (D15); no per-row test output is printed.

  python report.py <run_dir> <preds.jsonl ...> [--price-eur-h 2.6174] [--slim out.jsonl] [--json out.json]
"""
import argparse
import collections
import json
import os
import sys
from pathlib import Path

from stoa.eval.runner import print_report, summarize

ap = argparse.ArgumentParser()
ap.add_argument("run_dir")
ap.add_argument("preds", nargs="+")
ap.add_argument("--price-eur-h", type=float, default=2.6174)
ap.add_argument("--slim", default=None)
ap.add_argument("--json", default=None)
a = ap.parse_args()

rows = []
for p in a.preds:
    with open(p) as f:
        rows += [json.loads(l) for l in f if l.strip()]
seen, uniq = set(), []
for r in rows:
    if r["qid"] not in seen:
        seen.add(r["qid"]); uniq.append(r)
rows = uniq
if os.environ.get("JUDGE", "1") == "1":  # text answers: the eval's local LLM judge (on the instance's own vLLM)
    try:
        from stoa.eval.judge import judge_rows
        judge_rows(rows, "local", workers=32)
    except Exception as e:  # noqa: BLE001
        print(f"[report] judge skipped: {e}", file=sys.stderr)
rd = Path(a.run_dir)
t0, t1 = (float((rd / x).read_text()) if (rd / x).exists() else None for x in ("t0", "t1"))
wall = (t1 - t0) if t0 and t1 else None

split_of = {}
sd = Path(os.environ.get("JUNCTION_SHARED", "/root/shared")) / "results" / "splits"
for s in ("smoke", "dev", "test", "train", "unused"):
    f = sd / f"{s}.jsonl"
    if f.exists():
        for l in open(f):
            split_of.setdefault(json.loads(l)["qid"], s)

m = summarize(rows)
print_report(m)
out = {"n": len(rows), "wall_s": wall, "q_per_s": len(rows) / wall if wall else None,
       "eur": a.price_eur_h * wall / 3600 if wall else None, "overall": {k: v for k, v in m.items() if not isinstance(v, (dict, list))}}
if wall:
    out["eur_per_1k_q"] = out["eur"] / len(rows) * 1000
by_split = collections.defaultdict(list)
for r in rows:
    by_split[split_of.get(r["qid"], "none")].append(r)
out["by_split"] = {}
for s, rs in sorted(by_split.items()):
    ms = summarize(rs)
    out["by_split"][s] = {k: ms.get(k) for k in ("n", "scored_n", "accuracy", "weighted_acc", "macro_accuracy", "evidence_rate", "text_judge", "text_judged_n", "errors", "latency_p95_s", "usd_per_1k_q")}
by_st = collections.defaultdict(list)
for r in rows:
    by_st[(r["source"], r["type"])].append(r)
out["by_source_type"] = {}
for (src, typ), rs in sorted(by_st.items()):
    sc = [r for r in rs if r.get("correct") is not None]
    nonseal = [r for r in sc if split_of.get(r["qid"]) != "test"]
    out["by_source_type"][f"{src}/{typ}"] = {
        "n": len(rs), "acc": round(sum(bool(r["correct"]) for r in sc) / len(sc), 4) if sc else None,
        "acc_excl_test": round(sum(bool(r["correct"]) for r in nonseal) / len(nonseal), 4) if nonseal else None,
        "errors": sum(1 for r in rs if r.get("error")),
        "lat_mean_s": round(sum(r.get("latency_s") or 0 for r in rs) / len(rs), 3),
        "solvers": collections.Counter(str(r.get("solver")) for r in rs).most_common(3)}
tok = sum((r.get("cost") or {}).get("llm_tokens_in") or 0 for r in rows)
tok_out = sum((r.get("cost") or {}).get("llm_tokens_out") or 0 for r in rows)
calls = sum((r.get("cost") or {}).get("llm_calls") or 0 for r in rows)
out["llm"] = {"calls": calls, "tokens_in": tok, "tokens_out": tok_out, "calls_per_q": calls / max(1, len(rows))}
print(json.dumps({k: v for k, v in out.items() if k != "by_source_type"}, indent=1))
for k, v in out["by_source_type"].items():
    print(f"{k:32s} n={v['n']:6d} acc={v['acc']} acc_excl_test={v['acc_excl_test']} err={v['errors']} lat={v['lat_mean_s']} {v['solvers']}")
if a.json:
    Path(a.json).write_text(json.dumps(out, indent=1))
if a.slim:
    with open(a.slim, "w") as f:
        for r in rows:
            c = r.get("cost") or {}
            f.write(json.dumps({"qid": r["qid"], "split": split_of.get(r["qid"], "none"), "source": r["source"],
                                "type": r["type"], "correct": r.get("correct"), "abstained": r.get("abstained"),
                                "confidence": r.get("confidence"), "solver": r.get("solver"),
                                "latency_s": r.get("latency_s"), "llm_calls": c.get("llm_calls"),
                                "tok_in": c.get("llm_tokens_in"), "error": bool(r.get("error")),
                                "n_evidence": len(r.get("evidence") or [])}) + "\n")
