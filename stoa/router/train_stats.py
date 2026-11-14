"""TRAIN-split statistics for machines without the Stoa dataset (the public repo, a fresh clone, Stoa's own box).

The router learns four things from the TRAIN split at run time: the answer prior (stoa/router/prior.py), which question
shapes are known templates (prior.known_template), the canonical English wording per intent key (canonical.py) and the
CubiCasa count prior per class (stoa/solvers/raster). On the team box they read $JUNCTION_SHARED/results/splits/
train.jsonl joined with the dataset's questions.jsonl; without the dataset every question failed (FileNotFoundError).
This module stores what those readers derive, and NOTHING else, in train_stats.json next to it:
  prior        (level, source, intent key) -> typical NUMBER, unit, hit rate, n   (statistics; text answers dropped)
  templates    sha1 of each known question template (template_of), 16 hex chars    (no question text)
  canonical    (source, intent key) -> the most common English train wording      (20 generic template sentences;
               all 20 already appear verbatim in this repo's code/tests/fixtures, RL 16:40)
  class_prior  CubiCasa class -> (typical count, hit rate, n)
The readers use it ONLY when the split file is missing, so the team box behaves exactly as before.

    python -m stoa.router.train_stats --write      # needs STOA_DATA + the train split; rewrites train_stats.json
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
from pathlib import Path

PATH = Path(__file__).with_name("train_stats.json")


def tkey(template: str) -> str:
    return hashlib.sha1(template.encode("utf-8")).hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def load() -> dict:
    """The stats, or {} when the file is absent (then the readers fall back to their empty behaviour)."""
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def prior_table() -> dict:
    from stoa.router.prior import PriorAnswer

    return {(lv, src, key): PriorAnswer(v, u, hit, n, lv) for lv, src, key, v, u, hit, n in load().get("prior", [])}


def templates() -> set[str]:
    return set(load().get("templates", []))


def canonical() -> dict[tuple[str, str], str]:
    return {(src, key): q for src, key, q in load().get("canonical", [])}


def class_prior() -> dict:
    return {c: tuple(v) for c, v in load().get("class_prior", {}).items()}


def write() -> dict:
    from stoa.data import template_of
    from stoa.eval.splits import load_split
    from stoa.router import canonical as C
    from stoa.router import prior as P
    from stoa.solvers import raster as R

    qs = load_split("train")
    table = P._build()
    out = {
        "_about": "TRAIN-split statistics for machines without the dataset; see stoa/router/train_stats.py. "
                  f"Built from {len(qs)} train questions.",
        # numeric priors only: the string-valued ones are other buildings' free-text answers (IFC-Bench), i.e. dataset
        # text that would never be right for a new file anyway
        "prior": sorted([k[0], k[1], k[2], b.value, b.unit, round(b.hit_rate, 6), b.n] for k, b in table.items()
                        if isinstance(b.value, (int, float)) and not isinstance(b.value, bool)),
        "templates": sorted({tkey(template_of(q.question)) for q in qs}),
        "canonical": sorted([s, k, q] for (s, k), q in C._plain_templates().items()),
        "class_prior": {c: [v, round(h, 6), n] for c, (v, h, n) in sorted(R.class_prior().items())},
    }
    PATH.write_text(json.dumps(out, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    load.cache_clear()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    if a.write:
        o = write()
        print(f"{PATH}: prior {len(o['prior'])}, templates {len(o['templates'])}, canonical {len(o['canonical'])}, "
              f"class_prior {len(o['class_prior'])} ({PATH.stat().st_size / 1e3:.0f} kB)")
    else:
        d = load()
        print({k: len(v) for k, v in d.items() if k != "_about"} or "no train_stats.json")
