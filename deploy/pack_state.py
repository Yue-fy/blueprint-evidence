"""Pack the runtime state the pipeline reads from $JUNCTION_SHARED into deploy/state/ (gitignored) for the image (P1).

Why: the router's prior (stoa/router/prior.py, canonical.py), the `prior` solver and raster read the TRAIN split at run
time: `$JUNCTION_SHARED/results/splits/train.jsonl`. On the team box that file holds only question ids, joined with the
full dataset's questions.jsonl, so it cannot work in a container that mounts a demo subset or Stoa's hidden set (P1
14 Nov: every question failed with FileNotFoundError). This writes the split SELF-CONTAINED (path, question, answer,
unit, type per row; load_split uses such rows as-is) under deploy/state/results/splits/, which the Dockerfile copies
to /var/stoa. Dataset-derived: never commit deploy/state/ (deploy/.gitignore); it goes into the private image only.

    $JUNCTION_VENV/bin/python deploy/pack_state.py            # before docker build / deploy/cloudrun.sh
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "deploy" / "state"
SPLITS = ("train",)  # what the answer path needs; dev/hard/hard2 are optional (ifc solver skips missing ones)


def main() -> int:
    sys.path.insert(0, str(REPO))
    from stoa.eval.splits import load_split

    d = OUT / "results" / "splits"
    d.mkdir(parents=True, exist_ok=True)
    for name in SPLITS:
        qs = load_split(name)
        with open(d / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for q in qs:
                f.write(json.dumps({"qid": q.qid, "path": q.path, "question": q.question, "answer": q.answer,
                                    "unit": q.unit, "type": q.type}, ensure_ascii=False) + "\n")
        print(f"{name}: {len(qs)} questions -> {d / (name + '.jsonl')} ({(d / (name + '.jsonl')).stat().st_size / 1e6:.1f} MB)")
    for sub in ("logs", "locks", "artifacts"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)
        (OUT / sub / ".keep").touch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
