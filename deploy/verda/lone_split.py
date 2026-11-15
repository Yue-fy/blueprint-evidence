"""ON the instance (repo dir, STOA_DATA set): the FILES-ALONE variant of the whole open dataset, the way Stoa tests
(their own file uploaded in our UI: no data/<source>/ folder, no sibling DXF/IFC, a meaningless file name).
Every non-text question of train + dev + unused (drawings of the sealed test split excluded) is re-pointed at a
hard link of its file under NEUTRAL_ROOT/lone/<sha1>/d<sha1>.<ext> (experiments/F/neutralize.py, mode lone), then
sharded by drawing into N files of full rows.     python lone_split.py <out_dir> <N>
ORIG=1 keeps the original dataset paths (same rows, same shards: the path-based control run)."""
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())
from stoa.extras.neutralize import neutral_path  # noqa: E402
from stoa.data import load_questions  # noqa: E402

COST = {"cubicasa5k": 1.0, "swiss_dwellings": 0.5, "ifc_models": 1.0, "ifc_bench": 4.0, "opening_design": 8.0}
sd = Path(os.environ["JUNCTION_SHARED"]) / "results" / "splits"
root = Path(os.environ.get("NEUTRAL_ROOT", "/root/work/neutral"))
out, n = Path(sys.argv[1]), int(sys.argv[2])
# test.jsonl is not shipped to instances (V1 cycle 9): the rows below come from train / dev / unused only, which are
# split by drawing, so no sealed row can appear; the filter only matters where test.jsonl exists
tf = sd / "test.jsonl"
sealed = {json.loads(l)["group"] for l in open(tf) if l.strip()} if tf.exists() else set()
# FULL rows (question + answer + unit + type): a row with only a qid is joined with questions.jsonl by the eval,
# which restores the DATASET path and silently measures the path-based run (V1 15:35, as S0's make_upload_probe says)
qs = {q.qid: q for q in load_questions()}
rows, seen = [], set()
for s in ("train", "dev", "unused"):
    for l in open(sd / f"{s}.jsonl"):
        r = json.loads(l)
        if r["qid"] in seen or r.get("group") in sealed or r.get("type") == "text":
            continue
        seen.add(r["qid"])
        q = qs[r["qid"]]
        rows.append({"qid": r["qid"], "question": q.question, "answer": q.answer, "unit": q.unit, "type": q.type,
                     "path": r["path"] if os.environ.get("ORIG") else neutral_path(r["path"], "lone", root),
                     "orig_path": r["path"], "source": r["source"],
                     "meta": {"source": r["source"], "group": r.get("group")}})
by_path = collections.defaultdict(list)
for r in rows:
    by_path[r["orig_path"]].append(r)
groups = sorted(by_path.values(), key=lambda rs: -len(rs) * COST.get(rs[0]["source"], 1.0))
load, shards = [0.0] * n, [[] for _ in range(n)]
for rs in groups:
    i = min(range(n), key=load.__getitem__)
    shards[i] += rs
    load[i] += len(rs) * COST.get(rs[0]["source"], 1.0)
out.mkdir(parents=True, exist_ok=True)
for i, rs in enumerate(shards):
    with open(out / f"s{i:03d}.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rs)
print(f"{len(rows)} rows ({len(sealed)} sealed drawings excluded), {len(by_path)} drawings -> {n} shards, "
      f"by source {dict(collections.Counter(r['source'] for r in rows))}")
