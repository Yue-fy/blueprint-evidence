"""Is an evidence item on the right object? (E1)

    check_item(ev, question) -> ItemCheck(status, kind, ident, iou)

status:
- "right"        : the box sits on a ground-truth object of a kind the question asks about (or on any object when
                   the question's target kind is unknown), or the IFC GUID is that kind and its box matches the SVG
- "wrong_object" : the box sits on ground-truth objects, but none of the asked kind (a wall box for a door question)
- "off"          : the box sits on no ground-truth object (estimated transform, model-detected region, misplaced)
- "n/a"          : no ground truth for that file (cubicasa, scans, schedules, IFC-only 3D items, file-level items)

A box "sits on" an object when IoU >= 0.5 (both padded by a 2 pt tolerance), or it is a small box inside an area object (a room label / area text
inside the room's outline), or it tightly encloses a small object (centre of the object inside, area <= 4x), or it is a part of one
(>= 80% of the box inside the object: a door leaf without its swing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from stoa.evidence.groundtruth import GTObject, ground_truth, ifc_kind

# intent target -> object kinds that count as "the right object"
TARGET_KINDS: dict[str, set[str]] = {
    "door": {"door"}, "window": {"window"},
    "room": {"room"}, "bedroom": {"room"}, "kitchen": {"room", "washbasin"}, "bathroom": {"room"},
    "balcony": {"room"}, "sauna": {"room"}, "wc_room": {"room"},
    "apartment": {"apartment", "room"},
    "wc": {"wc", "sanitary"}, "sink": {"washbasin", "sanitary"}, "washbasin": {"washbasin", "sanitary"},
    "bathtub": {"bathtub", "shower", "sanitary"}, "shower": {"shower", "bathtub", "sanitary"},
    "shower_bathtub": {"shower", "bathtub", "sanitary"},
    "sanitary": {"wc", "washbasin", "bathtub", "shower", "sanitary"},
    "stair": {"stair", "railing", "member"}, "column": {"column"}, "beam": {"beam"}, "slab": {"slab"},
    "wall": {"wall"}, "light": {"light"}, "furniture": {"furniture"},
}
AREA_KINDS = {"room", "apartment", "slab"}
# boxes that are not meant to sit on a drawn object: the area searched for a count of 0, and the footprint of a model
# element the plan does not draw (ifc 'projected: not drawn'); the metric skips them (S0 12:48)
NOT_OBJECT_BOX = re.compile(r"searched area:|projected: not drawn|printed here", re.I)


def expected_kinds(question: str) -> set[str] | None:
    """Kinds the question is about; None = unknown target (any object counts). Compositions ('how many more doors
    than windows', 'doors per bedroom') name several targets: every one of them counts."""
    try:
        from stoa.router.intent import _TARGETS, parse

        it = parse(question)
    except Exception:  # noqa: BLE001
        return None
    kinds = set(TARGET_KINDS.get(it.target or "", set()))
    if it.task == "area":
        kinds |= {"room", "apartment", "slab"}
    if kinds:
        for t, rx in _TARGETS:  # other targets named in the same question
            if t in TARGET_KINDS and t != "storey" and rx.search(question):
                kinds |= TARGET_KINDS[t]
    return kinds or None


@dataclass
class ItemCheck:
    status: str
    kind: str = ""
    ident: str = ""
    iou: float = 0.0
    gt_source: str = ""
    alts: tuple[str, ...] = ()  # other right objects with the same box (DXF double-door leaves drawn twice)


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = _area(a) + _area(b) - inter
    if u <= 0:  # two degenerate boxes (points / lines): same place within 1 unit
        return 1.0 if abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1 and abs(a[2] - b[2]) <= 1 \
            and abs(a[3] - b[3]) <= 1 else 0.0
    return inter / u


def _inside_poly(x: float, y: float, poly) -> bool:
    n, inside = len(poly), False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _pad(b, t: float):
    return (b[0] - t, b[1] - t, b[2] + t, b[3] + t)


def _sits_on(b, o: GTObject, tol: float = 0.0) -> float:
    """Score in [0, 1] (0 = not on it). IoU of both boxes padded by `tol` when it is >= 0.5 (thin window lines and
    a solver's own 1 pt padding would fail a plain IoU); 0.5 for the containment rules."""
    v = iou(_pad(b, tol), _pad(o.box, tol))
    if v >= 0.5:
        return v
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    if o.kind in AREA_KINDS:
        inside = _inside_poly(cx, cy, o.polygon) if o.polygon and len(o.polygon) >= 3 else (
            o.box[0] <= cx <= o.box[2] and o.box[1] <= cy <= o.box[3])
        if inside and _area(b) <= 1.05 * _area(o.box) + 1:
            return 0.5
        return 0.0
    ox, oy = (o.box[0] + o.box[2]) / 2, (o.box[1] + o.box[3]) / 2
    if b[0] <= ox <= b[2] and b[1] <= oy <= b[3] and _area(b) <= 4 * max(_area(o.box), 4.0):
        return 0.5
    # a part of the object (a door leaf without its swing, one pane of a window): >= 80% of the box inside it
    pb = _pad(o.box, tol)
    ix = max(0.0, min(b[2], pb[2]) - max(b[0], pb[0]))
    iy = max(0.0, min(b[3], pb[3]) - max(b[1], pb[1]))
    if _area(b) > 0 and ix * iy >= 0.8 * _area(b) and _area(b) >= 0.02 * _area(o.box):
        return 0.5
    return 0.0


def check_item(ev: dict, question: str, kinds: set[str] | None | str = "auto") -> ItemCheck:
    """`ev` is an Evidence as a dict (as in predictions jsonl). `kinds="auto"` derives them from the question."""
    if kinds == "auto":
        kinds = expected_kinds(question)
    bbox, space = ev.get("bbox"), ev.get("bbox_space") or "pdf_pt"
    if not bbox:
        return ItemCheck("n/a")
    if NOT_OBJECT_BOX.search(ev.get("note") or ""):
        return ItemCheck("n/a", gt_source="not an object box")  # a searched area / an undrawn element's footprint
    gt = ground_truth(ev.get("file", ""), space)
    if gt is None or ev.get("page") not in (0, None):
        return ItemCheck("n/a")
    guid = ev.get("ifc_guid")
    if guid and guid in gt.by_ident:  # IFC element drawn on this plan: its own box must match
        o = gt.by_ident[guid]
        v = _sits_on(bbox, o, gt.tol)
        ok_kind = kinds is None or o.kind in kinds
        if v >= 0.5:
            return ItemCheck("right" if ok_kind else "wrong_object", o.kind, o.ident, v, gt.source)
        return ItemCheck("off", o.kind, o.ident, v, gt.source)
    best: tuple[float, GTObject | None] = (0.0, None)
    best_right: tuple[float, GTObject | None] = (0.0, None)
    for o in gt.objects:
        v = _sits_on(bbox, o, gt.tol)
        if v <= 0:
            continue
        # prefer the smallest object at equal score (a door inside a room: the door)
        key = v - 1e-9 * _area(o.box)
        if key > best[0]:
            best = (key, o)
        if (kinds is None or o.kind in kinds) and key > best_right[0]:
            best_right = (key, o)
    if best_right[1] is not None:
        o = best_right[1]
        alts = tuple(x.ident for x in gt.objects if x is not o and x.kind == o.kind and x.box == o.box)
        return ItemCheck("right", o.kind, o.ident, round(best_right[0], 3), gt.source, alts)
    if best[1] is not None:
        o = best[1]
        return ItemCheck("wrong_object", o.kind, o.ident, round(best[0], 3), gt.source)
    return ItemCheck("off", gt_source=gt.source)


__all__ = ["check_item", "expected_kinds", "ItemCheck", "iou", "ifc_kind", "TARGET_KINDS"]
