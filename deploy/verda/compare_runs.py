"""Pair two prediction files by qid (e.g. path-based `pipeline` vs files-alone `upload`) and print, per source x type:
n, accuracy A / B, delta, wins/losses, conf>=0.8 answers that are wrong, and the commonest solver switches behind the
losses.   python compare_runs.py A.jsonl[.gz] B.jsonl[.gz] [--ext] [--json out.json]"""
import collections
import gzip
import json
import sys

SC = ("count", "area", "length", "volume")


def load(p):
    op = gzip.open if p.endswith(".gz") else open
    out = {}
    for line in op(p, "rt"):
        if line.strip():
            r = json.loads(line)
            if r.get("type") in SC and r.get("gold") is not None:
                out[r["qid"]] = r
    return out


a, b = load(sys.argv[1]), load(sys.argv[2])
common = sorted(set(a) & set(b))
buckets = collections.defaultdict(list)
EXT = "--ext" in sys.argv  # also split by the asked file's extension (an IFC model vs a PDF sheet of it)
for q in common:
    ext = ("." + a[q]["path"].rsplit(".", 1)[-1].lower()) if EXT else ""
    buckets[f"{a[q]['source']}/{a[q]['type']}{ext}"].append(q)
buckets["ALL"] = common
res = {}
print(f"{'bucket':36s} {'n':>7s} {'A':>6s} {'B':>6s} {'delta':>6s} {'W':>5s} {'L':>5s} {'cw_A':>5s} {'cw_B':>5s}")
for k in sorted(buckets, key=lambda k: (k != "ALL", -len(buckets[k]))):
    qs = buckets[k]
    ca = sum(bool(a[q]["correct"]) for q in qs) / len(qs)
    cb = sum(bool(b[q]["correct"]) for q in qs) / len(qs)
    w = sum(bool(b[q]["correct"]) and not a[q]["correct"] for q in qs)
    lo = sum(bool(a[q]["correct"]) and not b[q]["correct"] for q in qs)
    cwa = sum((a[q].get("confidence") or 0) >= 0.8 and not a[q]["correct"] for q in qs)
    cwb = sum((b[q].get("confidence") or 0) >= 0.8 and not b[q]["correct"] for q in qs)
    sw = collections.Counter(f"{a[q].get('solver')} -> {b[q].get('solver')}" for q in qs
                             if a[q]["correct"] and not b[q]["correct"]).most_common(4)
    res[k] = {"n": len(qs), "acc_a": ca, "acc_b": cb, "wins": w, "losses": lo, "confwrong_a": cwa,
              "confwrong_b": cwb, "loss_switches": sw}
    print(f"{k:36s} {len(qs):7d} {100*ca:6.1f} {100*cb:6.1f} {100*(cb-ca):+6.1f} {w:5d} {lo:5d} {cwa:5d} {cwb:5d}")
print("\ncommonest solver switches behind the losses (A solver -> B solver):")
for k, v in res.items():
    if v["losses"] >= 20:
        print(f"  {k}: " + "; ".join(f"{s} x{n}" for s, n in v["loss_switches"]))
print(f"\nA only: {len(set(a) - set(b))}, B only: {len(set(b) - set(a))}")
if "--json" in sys.argv:
    json.dump(res, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=1)
