"""Split all questions (or one split) into N shard files of {"qid"} rows, one drawing per shard (keeps per-file
caches in one process), balanced by a rough per-source cost. Usage: python shard.py <out_dir> <N> [split]"""
import collections
import json
import sys
from pathlib import Path

from stoa.data import load_questions
from stoa.eval.splits import load_split

COST = {"cubicasa5k": 1.0, "swiss_dwellings": 0.5, "ifc_models": 1.0, "ifc_bench": 4.0, "opening_design": 8.0}

out, n = Path(sys.argv[1]), int(sys.argv[2])
qs = list(load_questions()) if len(sys.argv) < 4 or sys.argv[3] == "all" else load_split(sys.argv[3])
by_path = collections.defaultdict(list)
for q in qs:
    by_path[q.path].append(q)
src = lambda p: p.split("/")[1] if p.startswith("data/") else p.split("/")[0]
groups = sorted(by_path.items(), key=lambda kv: -len(kv[1]) * COST.get(src(kv[0]), 1.0))
load = [0.0] * n
shards = [[] for _ in range(n)]
for p, rows in groups:  # greedy: biggest drawing first into the lightest shard
    i = min(range(n), key=load.__getitem__)
    shards[i].extend(rows)
    load[i] += len(rows) * COST.get(src(p), 1.0)
out.mkdir(parents=True, exist_ok=True)
for i, rows in enumerate(shards):
    with open(out / f"s{i:03d}.jsonl", "w") as f:
        for q in rows:
            f.write(json.dumps({"qid": q.qid}) + "\n")
print(f"{len(qs)} questions, {len(by_path)} drawings -> {n} shards, load min/max {min(load):.0f}/{max(load):.0f}")
