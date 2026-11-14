"""Fast headline numbers of a merged predictions file (no evidence ground-truth pass): accuracy per split, per
source x type, weighted by the full-data bucket shares (as the eval's weighted_acc), cost/latency. The sealed
`test` split appears as one aggregate line only.   python quick_report.py preds_all.jsonl[.gz] [t0 t1 eur_per_h]"""
import collections
import gzip
import json
import os
import sys
from pathlib import Path

p = sys.argv[1]
op = gzip.open if p.endswith(".gz") else open
rows = [json.loads(l) for l in op(p, "rt")]
split_of = {}
sd = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "results" / "splits"
for s in ("smoke", "dev", "test", "train", "unused"):
    for l in open(sd / f"{s}.jsonl"):
        split_of.setdefault(json.loads(l)["qid"], s)
SC = ("count", "area", "length", "volume")
sc = [r for r in rows if r["type"] in SC and r.get("gold") is not None]
shares = collections.Counter(f"{r['source']}/{r['type']}" for r in sc)


def acc(rs):
    rs = [r for r in rs if r["type"] in SC and r.get("gold") is not None]
    return (sum(bool(r["correct"]) for r in rs) / len(rs), len(rs)) if rs else (None, 0)


def wacc(rs):
    b = collections.defaultdict(list)
    for r in rs:
        if r["type"] in SC and r.get("gold") is not None:
            b[f"{r['source']}/{r['type']}"].append(bool(r["correct"]))
    tot = sum(shares[k] for k in b)
    return sum(shares[k] / tot * (sum(v) / len(v)) for k, v in b.items()) if b else None


out = {"n": len(rows), "errors": sum(1 for r in rows if r.get("error")),
       "abstained": sum(1 for r in rows if r.get("abstained") or r.get("value") is None)}
a, n = acc(rows)
out.update(accuracy=round(a, 4), scored_n=n, weighted=round(wacc(rows), 4))
ev = [r for r in rows if r["type"] in SC]
out["evidence_rate"] = round(sum(1 for r in ev if r.get("evidence")) / len(ev), 4)
cost = [r.get("cost") or {} for r in rows]
out["llm_calls_per_q"] = round(sum(c.get("llm_calls") or 0 for c in cost) / len(rows), 3)
out["tok_in_per_q"] = round(sum(c.get("llm_tokens_in") or 0 for c in cost) / len(rows))
out["usd_per_1k_model"] = round(sum(c.get("usd") or 0 for c in cost) / len(rows) * 1000, 4)
out["det_share"] = round(sum(1 for c in cost if not (c.get("llm_calls") or 0)) / len(rows), 4)
lat = sorted(r.get("latency_s") or 0 for r in rows)
out["latency_p50_s"], out["latency_p95_s"] = lat[len(lat) // 2], lat[int(0.95 * len(lat))]
if len(sys.argv) >= 5:
    t0, t1, price = float(open(sys.argv[2]).read()), float(open(sys.argv[3]).read()), float(sys.argv[4])
    out["wall_s"] = t1 - t0
    out["q_per_s"] = round(len(rows) / (t1 - t0), 1)
    out["eur_run"] = round(price * (t1 - t0) / 3600, 3)
    out["eur_per_1k_q"] = round(out["eur_run"] / len(rows) * 1000, 4)
tj = [r for r in rows if r.get("text_judge") is not None]
if tj:
    out["text_judge"] = round(sum(bool(r["text_judge"]) for r in tj) / len(tj), 4)
    out["text_judged_n"] = len(tj)
print(json.dumps(out, indent=1))
by = collections.defaultdict(list)
for r in rows:
    by[split_of.get(r["qid"], "none")].append(r)
print("\nsplit       n       scored  acc     weighted")
for s in ("smoke", "dev", "test", "train", "unused", "none"):
    if s in by:
        a, n = acc(by[s])
        w = wacc(by[s])
        print(f"{s:8s} {len(by[s]):7d} {n:7d}  {a and round(100 * a, 2)}  {w and round(100 * w, 2)}"
              + ("   (sealed: aggregate only)" if s == "test" else ""))
print("\nsource/type (all splits; excl. test in brackets)")
bt = collections.defaultdict(list)
for r in rows:
    bt[f"{r['source']}/{r['type']}"].append(r)
for k in sorted(bt):
    a, n = acc(bt[k])
    a2, n2 = acc([r for r in bt[k] if split_of.get(r["qid"]) != "test"])
    sol = collections.Counter(str(r.get("solver")).replace("pipeline/", "") for r in bt[k]).most_common(3)
    lm = sum(r.get("latency_s") or 0 for r in bt[k]) / len(bt[k])
    print(f"{k:28s} n={len(bt[k]):6d} acc={a and round(100 * a, 2)} [{a2 and round(100 * a2, 2)}] lat={lm:.2f}s {sol}")
