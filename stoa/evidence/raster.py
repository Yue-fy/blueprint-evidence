"""Evidence for raster (CubiCasa-style) answers that came back without any (E1).

A count from the LoRA reader or the prior has no boxes. R1's room graph (cached pretrained-CubiCasa5K segmentation,
no LLM, ~0.1 s, $0) still knows where the candidates are: the separate drawings on the sheet (floors), room regions
of a kind (bedrooms, saunas, balconies ...) and fixtures (WC seats, sinks, bathtubs). We attach them as APPROXIMATE
evidence so a human has somewhere to look, and say in the note whether their number equals the answer:

    "floor drawing A (left) — segmentation finds 2 drawings = the answer ✓"
    "candidate: sink (segmentation) — it finds 2; the answer says 3, check the others"

No confidence change (D8: the segmenter saw most dataset drawings in pretraining, so agreement is not independent).
Every note contains "segmentation", which the web UI (customer.ts approxEvidence) draws dashed as "approximate".
"""

from __future__ import annotations

import re

from stoa.contract import Answer, Evidence

MAX_BOXES = 40

# intent target -> room-graph room test
_ROOM_TEST = {
    "bedroom": lambda r: r.get("cls") == "bedroom",
    "kitchen": lambda r: r.get("cls") == "kitchen",
    "sauna": lambda r: r.get("cls") == "sauna",
    "balcony": lambda r: r.get("cls") == "outdoor",
    "wc_room": lambda r: r.get("cls") == "bath" and bool(re.search(r"\bWC\b", r.get("label") or "")),
    "bathroom": lambda r: r.get("cls") == "bath" and not re.fullmatch(r"\s*WC\s*", r.get("label") or ""),
}
# intent target -> room-graph fixture kinds (CC5K icon classes; it has no shower class)
_FIXTURES = {"wc": ("toilet",), "sink": ("sink",), "washbasin": ("sink",), "bathtub": ("bathtub",),
             "shower_bathtub": ("bathtub",), "shower": ("bathtub",)}
_FIXTURE_WORD = {"toilet": "WC seat", "sink": "sink", "bathtub": "bathtub"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _graph(path: str):
    try:
        from stoa.extras.room_graph.graph import room_graph

        return room_graph(path)
    except Exception:  # noqa: BLE001
        return None


def candidates(path: str, target: str | None, task: str = "count") -> tuple[list[tuple[list[float], str]], str]:
    """-> ([(bbox image_px, label)], what) for an intent target on a raster plan; ([], "") when there is nothing."""
    g = _graph(path)
    if not g or not target:
        return [], ""
    rooms = g.get("rooms") or []
    if target == "storey":
        parts = g.get("parts") or []
        if parts:
            return [([float(v) for v in p["bbox"]], f"floor drawing {p['part']} ({p['where']})") for p in parts], \
                "separate drawings"
        boxes = [r["bbox"] for r in rooms if r.get("bbox")]
        if not boxes:
            return [], ""
        u = [float(min(b[0] for b in boxes)), float(min(b[1] for b in boxes)),
             float(max(b[2] for b in boxes)), float(max(b[3] for b in boxes))]
        return [(u, "the one floor drawing on this sheet")], "separate drawings"
    if target in _ROOM_TEST:
        rs = [r for r in rooms if _ROOM_TEST[target](r)]
        if not rs and target in _WET_TARGETS:
            return _wet_rooms(rooms, target)
        return [([float(v) for v in r["bbox"]], f"{target} region {r['id']}"
                 + (f", printed label '{r['label']}'" if r.get("label") else "")) for r in rs], f"{target} regions"
    if target in _FIXTURES:
        out = []
        for r in rooms:
            for kind in _FIXTURES[target]:
                for b in (r.get("fixtures") or {}).get(kind, []):
                    out.append(([float(v) for v in b], f"{_FIXTURE_WORD[kind]} in room {r['id']}"))
        what = "/".join(_FIXTURE_WORD[k] for k in _FIXTURES[target]) + " symbols"
        if target in ("shower", "shower_bathtub"):
            what += " (showers are not a class the segmenter knows)"
            if not out:  # no bathtub symbol: show the bathrooms, where showers and bathtubs are drawn
                rs = [r for r in rooms if r.get("cls") == "bath"]
                return [([float(v) for v in r["bbox"]], f"bathroom region {r['id']}"
                         + (f", printed label '{r['label']}'" if r.get("label") else "")) for r in rs], \
                    "bathroom regions (where showers and bathtubs are drawn; the segmenter has no shower class)"
        if not out and target in _WET_TARGETS:
            return _wet_rooms(rooms, target)
        return out, what
    return [], ""


# no symbol / region of the asked kind found: show the WET ROOMS (bath-class regions, or any region with a WC seat,
# bathtub or sink symbol that is not a kitchen), where such things are drawn — dev: 20 confident CubiCasa WC /
# bathroom counts had no evidence because the detector missed the WC seat or the bathroom was segmented as 'undefined'
_WET_TARGETS = {"wc", "wc_room", "bathroom", "sink", "washbasin", "bathtub", "shower", "shower_bathtub"}


def _wet_rooms(rooms: list, target: str) -> tuple[list, str]:
    def wet(r) -> bool:
        fx = r.get("fixtures") or {}
        return r.get("cls") == "bath" or bool(fx.get("toilet") or fx.get("bathtub")) or \
            (bool(fx.get("sink")) and r.get("cls") not in ("kitchen", "sauna"))

    rs = [r for r in rooms if r.get("bbox") and wet(r)]
    return [([float(v) for v in r["bbox"]], f"wet room region {r['id']}"
             + (f", printed label '{r['label']}'" if r.get("label") else "")) for r in rs], \
        f"wet-room regions (fallback: the segmenter found no {target.replace('_', ' ')} of its own; these are the " \
        "rooms where they are drawn)"


def attach_candidates(path: str, target: str | None, best: Answer, trace: list[str], task: str = "count") -> bool:
    """Adds approximate evidence to an evidence-less raster answer. True if anything was attached."""
    if best.value is None or best.evidence:
        return False
    boxes, what = candidates(path, target, task)
    if not boxes:
        return False
    v = _num(best.value)
    n = len(boxes)
    agree = v is not None and task == "count" and round(v) == n and "bathroom regions" not in what and \
        "fallback" not in what
    if agree:
        tail = f"segmentation finds {n} {what} = the answer ✓"
    elif v is not None and task == "count":
        tail = f"candidate (segmentation finds {n} {what}; the answer says {best.value}: check the others)"
    else:
        tail = f"segmentation: {what}"
    if "bathroom regions" in what or "fallback" in what:
        tail = f"segmentation: {what}"
    best.evidence = [Evidence(file=path, bbox=b, bbox_space="image_px", note=f"{lab} — {tail}")
                     for b, lab in boxes[:MAX_BOXES]]
    best.missing = [m for m in best.missing if m != "no located evidence from this solver"]
    best.missing = [*best.missing, "the answering model gives no locations; the boxes are the segmenter's "
                                   "candidates (approximate)"]
    trace.append(f"evidence (E1): {n} {what} from the cached CubiCasa5K segmentation attached as approximate boxes"
                 + (" (= the answer)" if agree else " (candidates; their number differs from the answer)" if task ==
                    "count" else ""))
    return True


def _search_boxes(path: str) -> tuple[list[list[float]], str | None, int | None]:
    """The drawn plan area(s) of a sheet -> ([bbox], bbox_space, page). Raster: the room graph's separate drawings
    (or the union of its rooms); vector sheets with ground truth (Swiss DXF twin, ifc_models SVG twin): the union
    of the rooms/spaces on the asked file."""
    from pathlib import Path

    ext = Path(path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg"):
        from stoa.evidence.groundtruth import objects_for

        objs = [o for o in objects_for(path, None, "image_px") if o.kind in ("room", "apartment")]
        if objs:
            bs = [o.box for o in objs]
            return [[min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]], \
                "image_px", None
        boxes, _ = candidates(path, "storey")
        return [b for b, _ in boxes], "image_px", None
    if ext == ".pdf":
        from stoa.evidence.groundtruth import objects_for

        objs = [o for o in objects_for(path, 0, "pdf_pt") if o.kind in ("room", "apartment", "wall")]
        if objs:
            bs = [o.box for o in objs]
            return [[min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]], \
                "pdf_pt", 0
    return [], None, None


def attach_search_area(path: str, target: str | None, best: Answer, trace: list[str]) -> bool:
    """A count of 0 has nothing to box: show WHERE we looked ('searched here: no sauna found'), approximate."""
    v = _num(best.value)
    if best.evidence or v is None or round(v) != 0:
        return False
    boxes, space, page = _search_boxes(path)
    if not boxes:
        return False
    what = (target or "matching object").replace("_", " ")
    best.evidence = [Evidence(file=path, page=page, bbox=[round(float(x), 2) for x in b], bbox_space=space,
                              note=f"searched area: no {what} found here (answer 0; approximate extent of the plan)",
                              exact=False) for b in boxes[:MAX_BOXES]]
    trace.append(f"evidence (E1): answer 0 → the searched plan area attached ({len(boxes)} box(es))")
    return True
