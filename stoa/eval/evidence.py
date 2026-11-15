"""Evidence precision report (E1): are the boxes on the right objects?

    $JUNCTION_VENV/bin/python -m stoa.eval.evidence <run_id | predictions.jsonl> [--by source|solver|kind] [--json]

Per answer (non-abstained): has evidence, has a 2D box. Per evidence item with ground truth (stoa.evidence.groundtruth:
ifc_models plan SVG twins, swiss_dwellings DXF twins): right / wrong_object / off (stoa.evidence.check). Items are
split exact vs approximate by `exact` when the item has it, else by the same rule as the web UI
(web/src/customer.ts approxEvidence): image-recognition notes (detector, segmentation, door swing, projected ...)
or a raster/VLM solver -> approximate.

Headline numbers (also merged into a run's metrics by `evidence_metrics(rows)`):
- evidence_rate / evidence_2d_rate: share of answers with any evidence / a 2D box
- ev_checked_n: items that have ground truth; ev_right: share of them on the right object
- ev_right_exact / ev_right_approx: the same for exact / approximate items
- ev_answer_all_right: share of answers with checked items whose items are ALL right
- ev_count_exact: count answers on ground-truth drawings whose distinct right objects == the answer
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from stoa.evidence.check import check_item, expected_kinds

def exactness(ev: dict, solver: str = "") -> bool | None:
    """The item's `exact` when set (D25), else stoa.evidence.exactness.classify; None = unknown."""
    if ev.get("exact") is not None:
        return bool(ev["exact"])
    from stoa.evidence.exactness import classify

    return classify(ev.get("note") or "", solver, bool(ev.get("bbox")), bool(ev.get("ifc_guid")))


def is_exact(ev: dict, solver: str = "") -> bool:
    """Unknown counts as exact here (kept for the release numbers' continuity); ev_unknown_n / ev_right_exact_known
    (T1 21:16) keep the exact-box precision honest."""
    return exactness(ev, solver) is not False


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_QPATH: dict[str, str] = {}
_HPATH: dict[str, str] = {}


def _load_maps() -> None:
    """qid → dataset path, and sha1(path)[:10] → path (F's neutralize.py names copies '<mode>/<h>/d<h>.<ext>')."""
    if _QPATH:
        return
    import hashlib

    try:
        from stoa.data import load_questions

        for q in load_questions():
            _QPATH[q.qid] = q.path
            _HPATH.setdefault(hashlib.sha1(q.path.encode()).hexdigest()[:10], q.path)
    except Exception:  # noqa: BLE001
        _QPATH["_"] = ""


def _dataset_twin(r: dict, file: str) -> str:
    """An uploaded copy (S0/G1/P1 upload probes: hard links under a hashed name, no siblings) → the dataset file it
    is a copy of, so its evidence is checked against the same ground truth. Same bytes (same inode) only; else file.
    Candidates: the row's meta.orig_path (P1), its qid (S0), the neutral folder's path hash (F/G1, new questions)."""
    import os
    import re

    from stoa.data import abspath

    _load_maps()
    origs = [(r.get("meta") or {}).get("orig_path"), _QPATH.get(r.get("qid") or "")]
    for part in Path(file).parts[-3:]:
        m = re.fullmatch(r"d?([0-9a-f]{10})(?:\.\w+)?", part)
        if m and m.group(1) in _HPATH:
            origs.append(_HPATH[m.group(1)])
    ext = Path(file).suffix
    for orig in [o for o in origs if o]:
        for rel in (orig, str(Path(orig).with_suffix(ext))):
            c = abspath(rel)
            try:
                if c.exists() and str(c) != file and os.path.samefile(c, file):
                    return rel
            except OSError:
                continue
    return file


def annotate(rows: list[dict]) -> list[dict]:
    """Adds row['ev_check'] = [{status, kind, ident, exact}, ...] (one per evidence item)."""
    from stoa.data import abspath

    root = str(abspath("."))
    for r in rows:
        if r.get("abstained"):
            r["ev_check"] = []
            continue
        kinds = expected_kinds(r.get("question") or "")
        out = []
        for ev in r.get("evidence") or []:
            f = ev.get("file") or ""
            if f.startswith("/") and not f.startswith(root):  # an upload copy outside the dataset
                t = _dataset_twin(r, f)
                if t != f:
                    ev = {**ev, "file": t}
            c = check_item(ev, r.get("question") or "", kinds)
            out.append({"status": c.status, "kind": c.kind, "ident": c.ident, "alts": list(c.alts), "gt": c.gt_source,
                        "exact": is_exact(ev, r.get("solver") or ""),
                        "known": exactness(ev, r.get("solver") or "") is not None})
        r["ev_check"] = out
    return rows


def _rate(k, n):
    return round(k / n, 4) if n else None


def _right_idents(r: dict) -> set[str]:
    """Distinct objects an answer's evidence shows correctly: the GT object, or the IFC GUID for an item that has no
    2D ground truth (3D-only, or an element of the model that no plan draws)."""
    out = set()
    for ev, c in zip(r.get("evidence") or [], r["ev_check"]):
        if c["status"] == "right":
            if ev.get("ifc_guid"):
                out.add(ev["ifc_guid"])
            else:  # one-to-one: the first of the identical objects not taken yet
                out.add(next((i for i in [c["ident"], *c.get("alts", [])] if i not in out), c["ident"]))
        elif c["status"] == "n/a" and ev.get("ifc_guid"):
            out.add(ev["ifc_guid"])
    return out


def _sum_adds_up(r: dict) -> bool | None:
    """An area answer whose evidence lists several terms, each with its m² in the note (Ziying 13:23: 'list every
    term of a sum with its area so the evidence adds up to the answer'): do the terms add up (1%)? None = not a sum.
    The same element shown twice (2D sheet box + 3D GUID) counts once; context items without m² make it None."""
    import re

    v = _num(r.get("value"))
    if v is None or (r.get("unit") or "") not in ("m2", "m²"):
        return None
    terms: dict[str, float] = {}
    for e in r.get("evidence") or []:
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*m(?:²|2)\b", e.get("note") or "")
        if not m or re.match(r"\s*(?:compared|searched|not in)", e.get("note") or "", re.I):
            continue
        terms[e.get("ifc_guid") or re.split(r":\s*-?\d", e.get("note") or "")[0]] = float(m.group(1))
    if len(terms) < 2 or any(abs(t - v) <= 0.01 * max(abs(v), 1) for t in terms.values()):
        return None  # one room (with context items), not a sum
    return abs(sum(terms.values()) - v) <= 0.01 * max(abs(v), 1)


def evidence_metrics(rows: list[dict]) -> dict:
    """Headline evidence-precision numbers for a list of prediction rows (annotates them if needed)."""
    if rows and "ev_check" not in rows[0]:
        annotate(rows)
    ans = [r for r in rows if not r.get("abstained")]
    items = [c for r in ans for c in r["ev_check"] if c["status"] != "n/a"]
    ex = [c for c in items if c["exact"]]
    ap = [c for c in items if not c["exact"]]
    checked = [r for r in ans if any(c["status"] != "n/a" for c in r["ev_check"])]
    all_right = [r for r in checked if all(c["status"] in ("right", "n/a") for c in r["ev_check"])]
    # plain counts only: a composed count (doors minus windows, per-room ratios) shows every operand's objects
    cnt = [r for r in checked if r.get("type") == "count" and _num(r.get("value")) is not None
           and "compose" not in (r.get("solver") or "")]
    cnt_ok = [r for r in cnt if len(_right_idents(r)) == round(_num(r["value"]))]
    hi = [r for r in ans if (r.get("confidence") or 0) >= 0.8]
    areas = [r for r in ans if r.get("type") == "area"]
    lengths = [r for r in ans if r.get("type") == "length"]
    return {
        "answered_n": len(ans),
        "evidence_rate": _rate(sum(1 for r in ans if r.get("evidence")), len(ans)),
        "evidence_2d_rate": _rate(sum(1 for r in ans if any(e.get("bbox") for e in r.get("evidence") or [])), len(ans)),
        "hi_conf_no_evidence_n": sum(1 for r in hi if not r.get("evidence")),
        "ev_items_n": sum(len(r["ev_check"]) for r in ans),
        "ev_checked_n": len(items),
        "ev_right": _rate(sum(c["status"] == "right" for c in items), len(items)),
        "ev_wrong_object": _rate(sum(c["status"] == "wrong_object" for c in items), len(items)),
        "ev_off": _rate(sum(c["status"] == "off" for c in items), len(items)),
        "ev_exact_n": len(ex), "ev_right_exact": _rate(sum(c["status"] == "right" for c in ex), len(ex)),
        "ev_unknown_n": sum(1 for c in ex if c.get("known") is False),
        "ev_right_exact_known": _rate(sum(c["status"] == "right" for c in ex if c.get("known") is not False),
                                      sum(1 for c in ex if c.get("known") is not False)),
        "ev_approx_n": len(ap), "ev_right_approx": _rate(sum(c["status"] == "right" for c in ap), len(ap)),
        "ev_answers_checked_n": len(checked), "ev_answer_all_right": _rate(len(all_right), len(checked)),
        "ev_count_n": len(cnt), "ev_count_exact": _rate(len(cnt_ok), len(cnt)),
        # captain 12:44: areas show the room outline, lengths the measured line (D28 fields)
        "area_n": len(areas), "area_polygon_rate": _rate(sum(1 for r in areas if any(
            e.get("polygon") for e in r.get("evidence") or [])), len(areas)),
        "length_n": len(lengths), "length_polyline_rate": _rate(sum(1 for r in lengths if any(
            e.get("polyline") for e in r.get("evidence") or [])), len(lengths)),
        "sum_n": len(sums := [x for x in (_sum_adds_up(r) for r in ans) if x is not None]),
        "sum_adds_up_rate": _rate(sum(sums), len(sums)),
    }


def _load(arg: str) -> tuple[list[dict], str]:
    p = Path(arg)
    if not p.exists():
        from stoa.eval.splits import shared_root

        p = shared_root() / "artifacts" / "eval" / f"{arg}.jsonl"
    return [json.loads(line) for line in p.open() if line.strip()], str(p)


def _group_key(r: dict, by: str) -> str:
    if by == "solver":
        return f"{r.get('source')}|{r.get('solver')}"
    if by == "kind":
        from stoa.router.intent import parse

        try:
            return f"{r.get('source')}|{parse(r.get('question') or '').target}"
        except Exception:  # noqa: BLE001
            return f"{r.get('source')}|?"
    return r.get("source") or "?"


def report(rows: list[dict], by: str = "source", file=sys.stdout) -> dict:
    annotate(rows)
    m = evidence_metrics(rows)
    groups = collections.defaultdict(list)
    for r in rows:
        groups[_group_key(r, by)].append(r)

    def pct(v):
        return "   –" if v is None else f"{100 * v:5.1f}"

    print(f"{'group':44s} {'n':>5s} {'ev%':>6s} {'2d%':>6s} {'items':>6s} {'right':>6s} {'wrong':>6s} {'off':>6s} "
          f"{'ex_n':>5s} {'ex_ok':>6s} {'ap_n':>5s} {'ap_ok':>6s} {'cnt=':>6s}", file=file)
    for k in sorted(groups, key=lambda k: -len(groups[k])):
        g = evidence_metrics(groups[k])
        print(f"{k[:44]:44s} {g['answered_n']:5d} {pct(g['evidence_rate']):>6s} {pct(g['evidence_2d_rate']):>6s} "
              f"{g['ev_checked_n']:6d} {pct(g['ev_right']):>6s} {pct(g['ev_wrong_object']):>6s} {pct(g['ev_off']):>6s} "
              f"{g['ev_exact_n']:5d} {pct(g['ev_right_exact']):>6s} {g['ev_approx_n']:5d} {pct(g['ev_right_approx']):>6s} "
              f"{pct(g['ev_count_exact']):>6s}", file=file)
    print(f"ALL: answers {m['answered_n']}, with evidence {pct(m['evidence_rate']).strip()}%, 2D "
          f"{pct(m['evidence_2d_rate']).strip()}%, conf>=0.8 without evidence {m['hi_conf_no_evidence_n']}; "
          f"items with ground truth {m['ev_checked_n']}/{m['ev_items_n']}: right {pct(m['ev_right']).strip()}% "
          f"(exact {pct(m['ev_right_exact']).strip()}% of {m['ev_exact_n']}, approximate "
          f"{pct(m['ev_right_approx']).strip()}% of {m['ev_approx_n']}); answers all-right "
          f"{pct(m['ev_answer_all_right']).strip()}% of {m['ev_answers_checked_n']}; counts with exactly the answer's "
          f"number of right objects {pct(m['ev_count_exact']).strip()}% of {m['ev_count_n']}; area answers with a "
          f"room outline {pct(m['area_polygon_rate']).strip()}% of {m['area_n']}, length answers with a measured line "
          f"{pct(m['length_polyline_rate']).strip()}% of {m['length_n']}; area sums whose listed terms add up "
          f"{pct(m.get('sum_adds_up_rate')).strip()}% of {m.get('sum_n', 0)}", file=file)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", help="run id (artifacts/eval/<id>.jsonl) or a predictions .jsonl path")
    ap.add_argument("--by", default="source", choices=["source", "solver", "kind"])
    ap.add_argument("--json", action="store_true", help="print the headline metrics as JSON")
    ap.add_argument("--failures", type=int, default=0, help="print N items that are not 'right'")
    a = ap.parse_args(argv)
    rows, path = _load(a.run)
    print(f"# evidence precision: {path} ({len(rows)} rows)")
    m = report(rows, a.by)
    if a.failures:
        k = 0
        for r in rows:
            for ev, c in zip(r.get("evidence") or [], r.get("ev_check") or []):
                if c["status"] in ("wrong_object", "off") and k < a.failures:
                    k += 1
                    print(f"- [{c['status']}] {r['source']} {r['solver']} | {r['question'][:80]} | value {r['value']} | "
                          f"hit {c['kind']} {c['ident']} | ev {ev.get('file', '')[-40:]} {ev.get('bbox')} "
                          f"{(ev.get('note') or '')[:80]}")
    if a.json:
        print(json.dumps(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
