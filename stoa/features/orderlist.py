"""Order list (F3, captain 21:51 'idea 3'): the step from reading the drawing to ordering material on site.

Built on F2's takeoff (stoa.features.takeoff: one niced background job per sheet, cached by file content) — no new
answer logic, no model calls:
  (a) skirting: each room's net skirting length (the takeoff's 'baseboard' row = wall-face perimeter minus door
      openings) -> + waste % (default 10 %) -> pieces of a stock length (default 2.4 m), rounded up PER ROOM (an
      offcut rarely fits the next room) -> total pieces. Tiled wet rooms (WC, bathroom, shower, sauna, KPH / PH / WC,
      utility) and stairs / shafts / voids get no skirting. A reading under 0.3 confidence, a room with no length,
      or a length that is not a length (a count) goes to the REVIEW list, outside the totals (estimators trust a
      total only when doubtful items are visible and left out: artifacts/RESEARCH/engineer-research.md).
  (b) doors by size: printed size tags ('9x21', '10x21' = width x height in dm, Finnish practice) when the sheet
      prints them, else the leaf width measured from each door's swing arc at the printed scale (DR's openings
      reader), rounded to 100 mm; a double door = ONE set with two leaves. Height is not on a plan: 2.1 m assumed.
  (c) floor covering: m² by room type (the room name without its number / copy) + waste % (default 10 %).
Every default is labelled as a default and editable; every line keeps the evidence of the rooms / doors behind it.

    from stoa.features.orderlist import build
    build(takeoff_result, {"skirting_waste_pct": 10, "stock_m": 2.4, "floor_waste_pct": 10}, doors=door_sizes(...))

Router (mounted under /feat/orderlist when STOA_FEATURES names 'orderlist' or 'all'):
    POST /feat/orderlist/start {path, page?}  -> the takeoff job's status (starts it when needed)
    GET  /feat/orderlist/build?job=&skirting_waste_pct=&stock_m=&floor_waste_pct=  -> the order list (or progress)
    GET  /feat/orderlist/export?job=&fmt=csv|xlsx&...  -> the order list as a file
"""

from __future__ import annotations

import csv
import functools
import io
import math
import os
import re
from pathlib import Path

VERSION = "orderlist-1"
DEFAULTS = {"skirting_waste_pct": 10.0, "stock_m": 2.4, "floor_waste_pct": 10.0, "door_height_m": 2.1,
            "wet_skirting": 0.0, "deduct_openings": 1.0}
# switches (0 / 1): wet_skirting 1 = tiled wet rooms get skirting too; deduct_openings 0 = the whole outline (F4's
# takeoff conventions tab sets both; the panel follows it)
BOOLS = ("wet_skirting", "deduct_openings")
MIN_CONF = 0.3  # = takeoff.SUM_MIN_CONF: a rough reading stays out of the totals (review list)
FT_M = 0.3048
FT2_M2 = FT_M * FT_M

# rooms that get no (timber) skirting: tiled wet rooms + circulation cores / voids (lineplan._NO_SKIRTING covers the
# second group). Finnish / Swedish / German names too: KPH / PH / WC / SK / KHH / löylyh. / pesuh., Bad / Dusche.
WET = re.compile(r"\b(wc|w\.c\.|toilets?|lavatory|lavatories|bath\w*|shower\w*|sauna|steam|wet ?room|washroom|"
                 r"wash|utility|laundry|kph|ph|khh|löylyh\w*|pesuh\w*|kylpyh\w*|suihk\w*|bad|dusche|toalett\w*|"
                 r"tvätt\w*|pesutila|pesu\w*|tualett\w*|vannitub\w*|duš\w*|saun\w*|changing|cleaner'?s?|janitor)\b", re.I)
NO_SKIRT_OTHER = re.compile(r"\b(stair\w*|lift\w*|elevator\w*|shaft\w*|riser\w*|void\w*|duct\w*|plant|walkway|"
                            r"canopy|terrace\w*|balcon\w*|courtyard|external|open to below|parvek\w*|terass\w*|"
                            r"porras\w*|hissi\w*|treppe\w*|balkon\w*|loggia)\b", re.I)


def opts_of(o: dict | None) -> tuple[dict, list[str]]:
    """Options with defaults filled in, and which ones are still the defaults (the UI labels those)."""
    out, default = dict(DEFAULTS), []
    for k, v in DEFAULTS.items():
        x = (o or {}).get(k)
        try:
            x = float(x) if x not in (None, "") else None
        except (TypeError, ValueError):
            x = None
        if k in BOOLS and x is not None and math.isfinite(x):
            x = 1.0 if x >= 0.5 else 0.0
            if x == v:
                x = None
        if x is None or not math.isfinite(x) or x < 0 or (k in ("stock_m", "door_height_m") and x <= 0.05):
            default.append(k)
        else:
            out[k] = min(x, 100.0) if k.endswith("_pct") else x
    return out, default


def room_type(name: str) -> str:
    """'Zimmer (2.01)' -> 'Zimmer', 'Bedroom 2' -> 'Bedroom', 'Whg. A Wohnen/Essen (2.05)' -> 'Wohnen/Essen',
    'Group Room' -> 'Group Room'."""
    s = re.sub(r"\([^)]*\)", " ", str(name or ""))
    s = re.sub(r"^\s*(whg|apt|apartment|unit|asunto|as)\.?\s+[A-Z0-9]{1,3}\b\.?\s*", " ", s, flags=re.I)
    s = re.sub(r"\b(no\.?\s*)?\d+[a-z]?(\.\d+)*[a-z]?\b", " ", s, flags=re.I)  # room numbers 2.01 / 104B / 3
    s = re.sub(r"\s+[A-Z]\s*$", " ", s)  # 'Bedroom A'
    s = re.sub(r"\s+", " ", s).strip(" -–:,.")
    return s or str(name or "").strip() or "Room"


# a landing / lobby / hall next to a lift or stair is an ordinary floor (skirting + covering): 'Lift Landing'
LANDING = re.compile(r"\b(landing|lobby|hall\w*|aula|eteinen|käytävä)\b", re.I)
# no floor covering to order: lift cars / shafts / voids / ducts / outdoor spaces
NO_FLOOR = re.compile(r"\b(lift|lifts|elevator\w*|shaft\w*|riser\w*|void\w*|duct\w*|open to below|canopy|courtyard|"
                      r"external|balcon\w*|terrace\w*|parvek\w*|terass\w*|hissi\w*|kuilu\w*|loggia)\b", re.I)


# a label whose LAST word is a building element is not a room: most often a misread ('Enclosed Walkway' read
# 'Enclosed Wall' by OCR on Stoa's DfE L00 picture: 64.6 m of skirting / 119 m² of floor ordered for a corridor outside)
ELEMENT = re.compile(r"\b(wall|walls|fence\w*|roof\w*|glazing|screen|column\w*|beam\w*|kerb|curb|parapet|"
                     r"cladding|boundary|line|seinä\w*|seina\w*|aita|katto|wand|mauer|zaun)\s*$", re.I)


def not_a_room(name: str) -> str | None:
    """Why a takeoff 'room' is probably no room at all (kept out of every total, listed under 'Check by hand')."""
    s = room_type(name or "")
    if ELEMENT.search(s):
        return f"'{s}' reads like a building element, not a room (a misread label?) — check by hand"
    return None


def no_skirting(name: str) -> str | None:
    """Why a room gets no skirting (None = it does)."""
    if WET.search(name or ""):
        return "tiled wet room (no skirting: the wall tiles run to the floor)"
    if NO_SKIRT_OTHER.search(name or "") and not LANDING.search(name or ""):
        return "no timber skirting here (stair, lift, shaft, void, plant room or outdoor space)"
    return None


def no_floor(name: str) -> str | None:
    """Why a room gets no floor covering on the order (None = it does)."""
    if NO_FLOOR.search(name or "") and not LANDING.search(name or ""):
        return "lift / shaft / void / outdoor space"
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _label(r: dict) -> str:
    c, n = r.get("copy") or 1, r.get("copies") or 1
    return f"{r.get('item')}" + (f" ({c}/{n})" if n and n > 1 else "")


def _ev(r: dict) -> list[dict]:
    return [e for e in (r.get("evidence") or []) if isinstance(e, dict)][:8]


def skirting(rows: list[dict], o: dict) -> dict:
    """Per room: net m -> + waste -> pieces of stock length (ceil per room); review list for doubtful rooms."""
    by_room: dict[tuple, dict] = {}
    for r in rows:
        if r.get("section") != "room":
            continue
        k = (r.get("item"), r.get("copy") or 1)
        by_room.setdefault(k, {"name": r.get("item"), "label": _label(r), "rows": {}})["rows"][r.get("quantity")] = r
    lines, review, excluded = [], [], []
    no_gross = 0
    w, stock = o["skirting_waste_pct"] / 100.0, o["stock_m"]
    for (_name, _copy), room in by_room.items():
        why = no_skirting(room["name"] or "")
        if why is None and any(r.get("wet") for r in room["rows"].values()):  # F2's wet flag (night-F2 2a5567cb)
            why = "tiled wet room (no skirting: the wall tiles run to the floor)"
        wet = bool(why and why.startswith("tiled wet room"))
        if wet and o.get("wet_skirting"):
            why = None  # the visitor's scope: wet rooms get skirting too (listed, flagged 'wet room')
        b = room["rows"].get("baseboard")
        odd = not_a_room(room["name"] or "")
        if odd:
            review.append({"room": room["label"], "why": odd, "evidence": _ev(b or room["rows"].get("area") or {})})
            continue
        if why:
            excluded.append({"room": room["label"], "why": why})
            continue
        v = _num(b.get("value")) if b else None
        unit = str((b or {}).get("unit") or "").lower()
        conf = float((b or {}).get("confidence") or 0.0)
        if b is None or v is None or v <= 0 or unit not in ("m", "ft", "lm", "jm"):
            got = "no skirting length read" if b is None or b.get("value") is None else \
                f"read '{b.get('value')} {b.get('unit') or ''}'".strip() + " — not a length"
            review.append({"room": room["label"], "why": got, "evidence": _ev(b or room["rows"].get("area") or {})})
            continue
        net = v * FT_M if unit == "ft" else v
        gross = (b.get("components") or {}).get("perimeter_m")  # F2: the whole inner outline, openings included
        if not o.get("deduct_openings", 1.0):
            if isinstance(gross, (int, float)) and gross > 0:
                net = float(gross)
            else:
                no_gross += 1
        if b.get("review"):  # the takeoff's own reason to keep it out of the totals (F2: implausible length, rough …)
            review.append({"room": room["label"], "why": f"{net:.2f} m — {str(b['review'])[:120]}", "net_m": round(net, 2),
                           "evidence": _ev(b)})
            continue
        if conf < MIN_CONF:
            review.append({"room": room["label"], "why": f"rough reading {net:.2f} m (confidence {conf:.2f})",
                           "net_m": round(net, 2), "evidence": _ev(b)})
            continue
        withw = net * (1 + w)
        pieces = max(1, math.ceil(withw / stock - 1e-9))
        lines.append({"room": room["label"], "type": room_type(room["name"] or ""), "net_m": round(net, 2),
                      "with_waste_m": round(withw, 2), "pieces": pieces, "confidence": round(conf, 2),
                      "method": b.get("method") or "", "evidence": _ev(b), "wet": wet,
                      "assumptions": [str(a) for a in (b.get("assumptions") or [])][:3]})
    lines.sort(key=lambda x: (x["type"], x["room"]))
    total_net = sum(x["net_m"] for x in lines)
    total_w = total_net * (1 + w)
    return {"lines": lines, "review": review, "excluded": excluded,
            "total_net_m": round(total_net, 2), "total_with_waste_m": round(total_w, 2),
            "pieces": sum(x["pieces"] for x in lines),
            "pieces_if_offcuts_reused": math.ceil(total_w / stock - 1e-9) if lines else 0,
            "rule": "whole outline, door openings NOT deducted" if not o.get("deduct_openings", 1.0)
                    else "outline minus door openings",
            "no_gross": no_gross,
            "confidence": round(min((x["confidence"] for x in lines), default=0.0), 2)}


def _category(r: dict) -> str | None:
    """F2's room category label ('Teaching', 'Wet rooms', …) when the takeoff rows carry it (display only: compare the
    row's room_type key, never this label — F2 renamed them at 00:11)."""
    k = r.get("room_type")
    if not k:
        return None
    try:
        from stoa.features.takeoff import TYPE_LABEL  # night-F2 2a5567cb

        return TYPE_LABEL.get(k, str(k))
    except ImportError:
        return str(k)


def floor_covering(rows: list[dict], o: dict) -> dict:
    """m² by room type (+ waste); rooms with no / a rough area reading go to the review list."""
    groups: dict[str, dict] = {}
    review, excluded = [], []
    w = o["floor_waste_pct"] / 100.0
    for r in rows:
        if r.get("section") != "room" or r.get("quantity") != "area":
            continue
        v, unit, conf = _num(r.get("value")), str(r.get("unit") or "m2").lower(), float(r.get("confidence") or 0.0)
        if v is None or v <= 0 or unit not in ("m2", "m²", "ft2", "sf", "sqft"):
            review.append({"room": _label(r), "why": "no floor area read", "evidence": _ev(r)})
            continue
        m2 = v * FT2_M2 if unit in ("ft2", "sf", "sqft") else v
        odd = not_a_room(r.get("item") or "")
        if odd:
            review.append({"room": _label(r), "why": odd, "evidence": _ev(r)})
            continue
        why = no_floor(r.get("item") or "")
        if why:
            excluded.append({"room": _label(r), "why": why})
            continue
        if r.get("review"):
            review.append({"room": _label(r), "why": f"{m2:.2f} m² — {str(r['review'])[:120]}", "evidence": _ev(r)})
            continue
        if conf < MIN_CONF or r.get("status") == "estimate":
            review.append({"room": _label(r), "why": f"rough reading {m2:.2f} m² (confidence {conf:.2f})",
                           "evidence": _ev(r)})
            continue
        t = room_type(r.get("item") or "")
        g = groups.setdefault(t, {"type": t, "rooms": [], "m2": 0.0, "confidence": 1.0, "evidence": [],
                                  "category": _category(r), "wet_key": False})
        g["wet_key"] = g["wet_key"] or r.get("room_type") == "wet"  # F2's key, not its label (labels renamed 00:11)
        g["rooms"].append({"room": _label(r), "m2": round(m2, 2), "confidence": round(conf, 2)})
        g["m2"] += m2
        g["confidence"] = min(g["confidence"], conf)
        g["evidence"] += _ev(r)[:2]
    lines = []
    for g in sorted(groups.values(), key=lambda g: -g["m2"]):
        wet_key = g.pop("wet_key", False)
        lines.append({**g, "m2": round(g["m2"], 2), "with_waste_m2": round(g["m2"] * (1 + w), 2),
                      "confidence": round(g["confidence"], 2), "evidence": g["evidence"][:24],
                      "wet": bool(WET.search(g["type"])) or wet_key})
    tot = sum(g["m2"] for g in lines)
    return {"lines": lines, "review": review, "excluded": excluded, "total_m2": round(tot, 2),
            "total_with_waste_m2": round(tot * (1 + w), 2)}


# --------------------------------------------------------------------------------------------------- door sizes

# Finnish door size tags: width x height in dm ('9x21' = 900 x 2100 mm), also '10 x 21', '9×21', '2x9x21' (a pair)
SIZE_TAG = re.compile(r"(?<![\d.,])(?:(2)\s*[x×]\s*)?(\d{1,2})\s*[x×]\s*(\d{2})(?![\d.,])")


def printed_size_tags(pdf: str, page: int = 0) -> list[dict]:
    """Door size tags printed on the sheet: [{tag, w_dm, h_dm, pair, bbox}] (only plausible door sizes)."""
    import pymupdf

    out = []
    with pymupdf.open(pdf) as doc:
        if page >= len(doc):
            return []
        for x0, y0, x1, y1, word, *_ in doc[page].get_text("words"):
            for m in SIZE_TAG.finditer(word):
                w, h = int(m.group(2)), int(m.group(3))
                if 6 <= w <= 30 and 19 <= h <= 30:
                    out.append({"tag": f"{w}x{h}", "w_dm": w, "h_dm": h, "pair": bool(m.group(1)) or w >= 13,
                                "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]})
    return out


def measured_doors(pdf: str, page: int, scale_n: float | None) -> tuple[list[dict], list[str]]:
    """Doors from their swing arcs (DR's openings reader), plan views only: [{kind, leaves, leaf_m: [..], bbox}]."""
    from stoa.solvers import openings as O

    notes: list[str] = []
    if not scale_n:
        return [], ["no printed scale: door widths cannot be measured"]
    sw = O.find_swings(pdf, page, scale_n)
    doors = O.group_doors(sw["arcs"])
    eff = sw.get("scale_n") or scale_n
    rect, mat = sw.get("rect"), sw.get("mat")
    if rect is not None:
        W, H = rect[2] - rect[0], rect[3] - rect[1]
        try:
            cells = O.view_cells(O.view_titles(pdf, page), W, H)
        except Exception:  # noqa: BLE001
            cells = []
        plans = [c for c in cells if c["kind"] == "plan"]
        if plans and len(plans) < len(cells):
            def in_plan(d):
                p = O._to_display(mat, d["center"])
                return any(c["cell"][0] <= p[0] <= c["cell"][2] and c["cell"][1] <= p[1] <= c["cell"][3] for c in plans)
            n0 = len(doors)
            doors = [d for d in doors if in_plan(d)]
            if n0 != len(doors):
                notes.append(f"{n0 - len(doors)} door(s) in other views of the sheet (RCP / sections / details) left out")
    out = []
    for d in doors:
        arcs = d["arcs"]
        leaves = [a["r"] * O.PT_M * eff for a in (arcs if d["kind"] == "double" else arcs[:1])]
        pts = [p for a in arcs for p in (a["c"], a["p0"], a["p1"])]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        out.append({"kind": "pair" if d["kind"] == "double" else "single", "leaves": len(leaves),
                    "leaf_m": [round(x, 3) for x in leaves],
                    "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]})
    return out, notes


def ifc_doors(pdf: str, file: str) -> dict | None:
    """A floor plan rendered from an IFC model (its .ifc next to it, GlobalId tags in the SVG twin): the doors of that
    storey that the plan's cut crosses, grouped by the MODEL's OverallWidth x OverallHeight (exact), each boxed where
    the plan draws it. None when the sheet has no linked model."""
    from stoa.features.selfcheck import _pvm_model
    from stoa.kg.graph import CUT_HEIGHT, _crosses, floor_offset
    from stoa.solvers.ifc.index import load_index
    from stoa.solvers.ifc.plan2d import plan_items, project_box
    from stoa.solvers.ifc.solver import plan_storey

    if "_plan-" not in Path(pdf).stem:
        return None
    model, plan = _pvm_model(pdf)
    if not model:
        return None
    items = plan_items(plan)
    idx = load_index(model)
    st, _ = plan_storey(plan, idx)
    if st is None or not items:
        return None
    cut_z = (st.elevation or 0.0) + floor_offset(idx) + CUT_HEIGHT
    doors = [e for e in idx.in_storey(st.guid) if (e.cls == "IfcDoor" or e.eff_cls == "IfcDoor")
             and _crosses(e, cut_z) is not False]
    if not doors:
        return None
    groups: dict[tuple, dict] = {}
    for e in doors:
        d = e.dims or {}
        w, h = d.get("OverallWidth"), d.get("OverallHeight")
        if not w or not h:
            continue
        kind_txt = f"{e.type_name or ''} {e.name or ''}".lower()
        pair = "double" in kind_txt or "pair" in kind_txt
        sliding = "sliding" in kind_txt
        wm, hm = int(round(float(w) * 1000 / 10) * 10), int(round(float(h) * 1000 / 10) * 10)
        kind = "pair" if pair else ("sliding" if sliding else "single")
        g = groups.setdefault((kind, wm, hm), {"size": f"{wm} × {hm} mm", "kind": kind, "width_mm": wm, "height_mm": hm,
                                               "sets": 0, "leaves": 0, "evidence": [], "types": set()})
        g["sets"] += 1
        g["leaves"] += 2 if pair else 1
        g["types"].add((e.type_name or e.name or "door").split(":")[0][:40])
        it = items.get(e.guid)
        box = list(it.bbox) if it is not None else project_box(model, plan, e)
        if box is not None:
            g["evidence"].append({"file": file, "page": 0, "bbox": [round(v, 1) for v in box], "bbox_space": "pdf_pt",
                                  "ifc_guid": e.guid, "exact": it is not None,
                                  "note": f"{(e.name or 'door')[:50]}: {wm} × {hm} mm (IFC)"})
    if not groups:
        return None
    lines = sorted(groups.values(), key=lambda g: (g["kind"] != "single", g["width_mm"]))
    for g in lines:
        g["types"] = sorted(g["types"])
    return {"source": f"3D model {Path(model).name} (exact sizes)", "confidence": 0.9, "lines": lines,
            "sets": sum(g["sets"] for g in lines), "leaves": sum(g["leaves"] for g in lines),
            "notes": [f"doors of storey '{st.name}' that the plan's cut (floor + {CUT_HEIGHT:g} m) crosses, sized by "
                      "the model's OverallWidth x OverallHeight; double doors = one set of two leaves"]}


_FTIN = re.compile(r"(\d{1,2})\s*'\s*-?\s*(\d{1,2})(?:\s+(\d)/(\d{1,2}))?\s*\"?")
_PAIR = re.compile(r"\b(PR|PAIR|DBL|DOUBLE|DOUBLE[- ]LEAF)\b|\(2\)", re.I)


def _ftin(mm: float) -> str:
    """mm -> feet-inches to the 1/16" a schedule prints (2' - 11 3/16" stays apart from 2'-11")."""
    import math as _m

    s16 = int(round(mm / 25.4 * 16))
    i, f = divmod(s16, 16)
    if not f:
        return f"{i // 12}'-{i % 12}\""
    g = _m.gcd(f, 16)
    return f"{i // 12}'-{i % 12} {f // g}/{16 // g}\""


def _row_size(text: str) -> tuple[int, int, bool, str] | None:
    """(width mm, height mm, pair) from one door-schedule row's text: '101A F 915 2100 10 SP …' (metric, mm) or
    '… 3'-0\" 7'-0\" 1 3/4\" …' / '3'-0\" x 7'-0\"' (feet-inches). None when no width + height pair is printed."""
    pair = bool(_PAIR.search(text))
    ft = [(int(a) * 12 + int(b) + (int(c) / int(d) if c else 0.0)) * 25.4 for a, b, c, d in _FTIN.findall(text)]
    for i in range(len(ft) - 1):
        w, h = ft[i], ft[i + 1]
        if 450 <= w <= 3700 and 1800 <= h <= 3700:
            return int(round(w)), int(round(h)), pair or w >= 1500, f"{_ftin(w)} × {_ftin(h)}"
    nums = [int(x) for x in re.findall(r"(?<![\d./'-])(\d{3,4})(?![\d./'\"])", text)]
    for i in range(len(nums) - 1):
        w, h = nums[i], nums[i + 1]
        if 500 <= w <= 3500 and 1800 <= h <= 3500:
            return w, h, pair or w >= 1500, f"{w} × {h} mm"
    return None


@functools.lru_cache(maxsize=16)
def _schedule_pages(pdf: str, mtime: float, page: int) -> tuple[int, ...]:
    """This page first, then the other pages of the PDF that print a DOOR SCHEDULE (first 200 pages)."""
    import pymupdf

    out = []
    with pymupdf.open(pdf) as doc:
        for i in [page] + [k for k in range(min(len(doc), 200)) if k != page]:
            if i < len(doc) and "DOOR SCHEDULE" in doc[i].get_text().upper():
                out.append(i)
    return tuple(out)


_MARK = re.compile(r"^\d{2,4}[A-Z]?\.[A-Z]\d{0,2}$")  # Revit door numbers: room 103 + leaf A -> '103.A', '100.A1'


def _mark_rows(P) -> list[dict]:
    """Door rows of a Revit-style DOOR SCHEDULE printed one word per cell (Sports Complex A-801, /Rotate 90), which
    xref.schedule_rows does not split into rows: every door-number word ('103.A') and the words on its VISUAL row (word
    boxes x the page's rotation matrix), cut after the row's second inch-terminated token (width, height) so the notes /
    title-block text further right stay out. {'kind', 'table', 'key', 'bbox' (unrotated pdf_pt), '_text'}."""
    import pymupdf

    if "DOOR SCHEDULE" not in P.get_text().upper():
        return []
    M = P.rotation_matrix
    words = []
    for w in P.get_text("words"):
        r = pymupdf.Rect(w[:4]) * M
        words.append((r.x0, r.y0, r.x1, r.y1, w[4], w[:4]))
    out, seen = [], set()
    for m in words:
        if not _MARK.match(m[4]) or m[4] in seen:
            continue
        yc, h = (m[1] + m[3]) / 2, m[3] - m[1]
        row = sorted((w for w in words if abs((w[1] + w[3]) / 2 - yc) < 0.5 * h and w[0] >= m[0] - 1), key=lambda w: w[0])
        inch = [i for i, w in enumerate(row[:20]) if w[4].endswith('"')]
        row = row[:inch[1] + 1] if len(inch) >= 2 else row[:16]
        seen.add(m[4])
        rect = pymupdf.Rect(row[0][5])
        for w in row[1:]:
            rect |= pymupdf.Rect(w[5])
        out.append({"kind": "door", "table": "Door Schedule", "key": m[4], "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "_text": " ".join(w[4] for w in row)})
    return out if len(out) >= 2 else []


def schedule_doors(pdf: str, page: int, file: str) -> dict | None:
    """Doors by size from a printed DOOR SCHEDULE (F8's stoa.features.xref.schedule_rows finds the rows; the size is
    read from each row's own text). One row per mark (a schedule printed twice counts once)."""
    import pymupdf

    from stoa.features.xref import schedule_rows

    pages = _schedule_pages(pdf, Path(pdf).stat().st_mtime, page)
    for pg in pages[:4]:
        rows = [r for r in schedule_rows(pdf, pg) if r.get("kind") == "door"]
        for attempt in (0, 1):
            got = _schedule_groups(pdf, pg, page, file, rows) if len(rows) >= 2 else None
            if got:
                return got
            if attempt == 0:  # xref found no rows with a readable size: a one-word-per-cell Revit schedule?
                with pymupdf.open(pdf) as doc:
                    rows = _mark_rows(doc[pg])
    return None


def _schedule_groups(pdf: str, pg: int, page: int, file: str, rows: list[dict]) -> dict | None:
    """One schedule page's door rows -> doors grouped by size (None when no row has a readable size)."""
    import pymupdf

    seen, groups, unread, openings = set(), {}, 0, 0
    with pymupdf.open(pdf) as doc:
        P = doc[pg]
        for r in rows:
            if (r["table"], r["key"]) in seen:  # a schedule printed twice; two tables may reuse a mark
                continue
            seen.add((r["table"], r["key"]))
            if r.get("opening_only"):  # F8: a CASED OPENING row: an opening with no door leaf to order
                openings += 1
                continue
            if r.get("_text"):  # read in visual order already (_mark_rows)
                got = _row_size(r["_text"])
            else:
                ws = P.get_text("words", clip=pymupdf.Rect(*r["bbox"]))
                toks = [w[4] for w in sorted(ws, key=lambda w: (round(w[1]), w[0]))]
                # a rotated sheet gives the row's words in reverse ('0" - 7' 0" - 3'' = 3'-0" x 7'-0", AW-28 p94)
                got = _row_size(" ".join(toks)) or _row_size(" ".join(reversed(toks)))
            if got is None:
                unread += 1
                continue
            w, h, pair, label = got
            g = groups.setdefault((pair, w, h), {"size": label, "kind": "pair" if pair else "single",
                                                 "width_mm": w, "height_mm": h, "sets": 0, "leaves": 0,
                                                 "evidence": [], "marks": []})
            g["sets"] += 1
            g["leaves"] += 2 if pair else 1
            g["marks"].append(r["key"])
            g["evidence"].append({"file": file, "page": pg, "bbox": r["bbox"], "bbox_space": "pdf_pt",
                                  "exact": True, "note": f"door {r['key']}: {w} × {h} mm ({r['table'][:40]})"})
    if not groups:
        return None
    lines = sorted(groups.values(), key=lambda g: (g["kind"] != "single", g["width_mm"], g["height_mm"]))
    tables = sorted({r["table"] for r in rows})
    notes = [f"sizes as printed in the {' + '.join(t.lower() for t in tables)[:120]} (page {pg + 1}): "
             f"{len(seen)} marks"
             + (f", {unread} without a readable size" if unread else "")
             + (f", {openings} cased opening{'s' if openings > 1 else ''} without a door (not ordered)" if openings else "")]
    if pg != page:
        notes.append("the schedule lists the doors of the whole set, not only this sheet's plan")
    return {"source": f"door schedule, page {pg + 1}", "confidence": 0.85, "lines": lines,
            "sets": sum(g["sets"] for g in lines), "leaves": sum(g["leaves"] for g in lines), "notes": notes}


def picture_doors(img: str, scale_n: float | None, file: str) -> dict:
    """A picture / scan: door SETS and LEAVES from the swing arcs on P1's traced vector copy (the one F2's takeoff
    reads), boxes back in image px. No sizes: traced arcs read short (DfE scan: 38 doors at ≈0.7 m where the vector
    sheet has 14; door count 95 vs 101 = -6 %)."""
    none = {"source": None, "confidence": 0.0, "lines": [], "sets": 0, "leaves": 0}
    # cache-only: the traced copy the takeoff / upload step already wrote next to the picture (never a cold trace or
    # OCR read inside a request); px per pt = the picture's width / the traced page's width
    vecs = sorted(Path(img).parent.glob(Path(img).stem + ".vec*.pdf"), key=lambda q: q.stat().st_mtime)
    if not vecs:
        return {**none, "notes": ["door sizes are measured on vector PDFs; this picture has no traced copy yet"]}
    vec = str(vecs[-1])
    try:
        import pymupdf
        from PIL import Image

        with Image.open(img) as im:
            wpx = im.size[0]
        with pymupdf.open(vec) as doc:
            f = wpx / doc[0].rect.width
    except Exception:  # noqa: BLE001
        return {**none, "notes": ["the picture's traced copy could not be read"]}
    how = f"{Path(vec).name}, {f * 72:.0f} px per inch"
    doors, notes = measured_doors(vec, 0, scale_n)
    if not doors:
        return {**none, "notes": notes + ["no door swings found on the picture's traced lines"]}
    lines = []
    for kind, label in (("single", "single leaf (size not measured on a picture)"),
                        ("pair", "double door (size not measured on a picture)")):
        ds = [d for d in doors if d["kind"] == kind]
        if ds:
            lines.append({"size": label, "kind": kind, "width_mm": 0, "height_mm": 0, "sets": len(ds),
                          "leaves": sum(d["leaves"] for d in ds),
                          "evidence": [{"file": file, "page": 0, "bbox": [round(v * f, 1) for v in d["bbox"]],
                                        "bbox_space": "image_px", "exact": False, "note": "door swing (traced)"}
                                       for d in ds]})
    return {"source": "door swings on the picture's traced lines", "confidence": 0.45, "lines": lines,
            "sets": sum(g["sets"] for g in lines), "leaves": sum(g["leaves"] for g in lines),
            "notes": notes + [f"picture read as traced lines ({how}); door widths are not given because traced arcs "
                              "read about 0.1-0.2 m short: take sizes from the door schedule"]}


def _scan(pdf: str, page: int) -> tuple[str, float] | None:
    """(render PNG, px per pt) when that PDF page is a scanned print (the self-check's test: the same render the upload
    readers and F2's takeoff read), else None."""
    try:
        from stoa.features.selfcheck import _scan_of

        return _scan_of(pdf, page)
    except Exception:  # noqa: BLE001
        return None


def door_sizes(pdf: str, page: int, scale_n: float | None, file: str, o: dict | None = None,
               printed_only: bool = False) -> dict:
    """Doors grouped by size: the linked IFC model (exact), else a printed door schedule, else printed size tags,
    else measured leaf widths (not with printed_only: those need the takeoff's scale)."""
    o = o or DEFAULTS
    ev = lambda b, note: {"file": file, "page": page, "bbox": b, "bbox_space": "pdf_pt", "note": note}  # noqa: E731
    try:
        got = ifc_doors(pdf, file) if pdf.lower().endswith(".pdf") and page == 0 else None
    except Exception:  # noqa: BLE001 - fall back to the drawing
        got = None
    if got:
        return got
    try:
        got = schedule_doors(pdf, page, file) if pdf.lower().endswith(".pdf") else None
    except Exception:  # noqa: BLE001
        got = None
    if got:
        return got
    try:
        tags = printed_size_tags(pdf, page) if pdf.lower().endswith(".pdf") else []
    except Exception:  # noqa: BLE001
        tags = []
    if len(tags) >= 2:
        groups: dict[str, dict] = {}
        for t in tags:
            g = groups.setdefault(t["tag"], {"size": t["tag"], "kind": "pair" if t["pair"] else "single",
                                             "width_mm": t["w_dm"] * 100, "height_mm": t["h_dm"] * 100,
                                             "sets": 0, "leaves": 0, "evidence": []})
            g["sets"] += 1
            g["leaves"] += 2 if t["pair"] else 1
            g["evidence"].append(ev(t["bbox"], f"door size tag {t['tag']}"))
        lines = sorted(groups.values(), key=lambda g: (g["kind"], g["width_mm"]))
        return {"source": "printed size tags", "confidence": 0.85, "lines": lines,
                "sets": sum(g["sets"] for g in lines), "leaves": sum(g["leaves"] for g in lines),
                "notes": ["sizes as printed on the plan (width x height in dm: 9x21 = 900 x 2100 mm); "
                          "a tag of 13 dm or wider (or '2x..') is read as a double door"]}
    if printed_only:
        return {"source": None, "lines": [], "sets": 0, "leaves": 0, "notes": []}
    if not pdf.lower().endswith(".pdf"):
        return picture_doors(pdf, scale_n, file)
    scan = _scan(pdf, page)
    if scan:  # a SCANNED page has no lines to measure: its picture's traced copy (F2's takeoff traces that render)
        got = picture_doors(scan[0], scale_n, file)
        for g in got.get("lines") or []:
            for e in g.get("evidence") or []:
                if e.get("bbox_space") == "image_px" and e.get("bbox"):
                    e["bbox"] = [round(v / scan[1], 1) for v in e["bbox"]]
                    e["bbox_space"], e["page"] = "pdf_pt", page
        got["notes"] = ["a scanned PDF: read as its picture"] + list(got.get("notes") or [])
        return got
    try:
        doors, notes = measured_doors(pdf, page, scale_n)
    except Exception as e:  # noqa: BLE001
        doors, notes = [], [f"door swings could not be read ({type(e).__name__})"]
    groups = {}
    h = o.get("door_height_m", 2.1)
    for d in doors:
        leaf = [round(x * 10) / 10 for x in d["leaf_m"]]
        if d["kind"] == "pair":
            size = f"pair ≈ {leaf[0]:.1f} + {leaf[1]:.1f} m" if leaf[0] != leaf[1] else f"pair ≈ 2 × {leaf[0]:.1f} m"
            width = sum(leaf)
        else:
            size, width = f"≈ {leaf[0]:.1f} m", leaf[0]
        g = groups.setdefault(size, {"size": size, "kind": d["kind"], "width_mm": int(round(width * 1000)),
                                     "height_mm": int(round(h * 1000)), "sets": 0, "leaves": 0, "evidence": []})
        g["sets"] += 1
        g["leaves"] += d["leaves"]
        g["evidence"].append(ev(d["bbox"], f"door swing: leaf {' + '.join(f'{x:.2f}' for x in d['leaf_m'])} m"))
    lines = sorted(groups.values(), key=lambda g: (g["kind"] != "single", g["width_mm"]))
    return {"source": "measured door swings" if doors else None, "confidence": 0.6 if doors else 0.0,
            "lines": lines, "sets": sum(g["sets"] for g in lines), "leaves": sum(g["leaves"] for g in lines),
            "notes": notes + ([f"leaf width = the swing arc's radius at 1:{scale_n:g}, rounded to 100 mm (about ±0.1 m: "
                               "an arc may be drawn to the frame, not the leaf; check against the door schedule); a "
                               "double door = one set of two leaves; sliding doors draw no swing and are not counted; "
                               f"height is not on a plan: {h:g} m assumed (default)"] if doors else
                              ["no door swings found on this sheet"])}


@functools.lru_cache(maxsize=32)
def _doors_cached(pdf: str, mtime: float, page: int, scale_n: float | None, file: str, h: float) -> dict:
    return door_sizes(pdf, page, scale_n, file, {"door_height_m": h})


def build(result: dict, opts: dict | None = None, doors: dict | None = None) -> dict:
    """The order list from a finished takeoff result (takeoff.status(job) with status 'done')."""
    o, default = opts_of(opts)
    rows = result.get("rows") or []
    sk = skirting(rows, o)
    fl = floor_covering(rows, o)
    return {"version": VERSION, "file": result.get("file"), "page": result.get("page", 0),
            "scale_n": result.get("scale_n"), "options": o, "defaults": default,
            "skirting": sk, "floor": fl, "doors": doors or {"source": None, "lines": [], "sets": 0, "leaves": 0,
                                                             "notes": []},
            "takeoff_seconds": result.get("seconds"),
            "cost": {"tier": "cheap", "api_calls": 0, "llm_tokens_in": 0, "llm_tokens_out": 0}}


def answer_text(ol: dict) -> str:
    """One line for 'How much skirting should we order?'."""
    sk, o = ol["skirting"], ol["options"]
    d = set(ol.get("defaults") or [])
    wtxt = f"{o['skirting_waste_pct']:g} % waste" + (" (default)" if "skirting_waste_pct" in d else "")
    stxt = f"{o['stock_m']:g} m lengths" + (" (default)" if "stock_m" in d else "")
    s = (f"{sk['pieces']} pieces of {stxt}: {len(sk['lines'])} rooms, {sk['total_net_m']:.1f} m net + {wtxt} = "
         f"{sk['total_with_waste_m']:.1f} m")
    if sk["review"]:
        s += f"; {len(sk['review'])} room(s) to check by hand are NOT included"
    return s


# ------------------------------------------------------------------------------------------------------- export

def table(ol: dict) -> tuple[list[str], list[list]]:
    head = ["Section", "Item", "Quantity", "Unit", "Order", "Order unit", "How sure", "Details"]
    body: list[list] = []
    o = ol["options"]
    for x in ol["skirting"]["lines"]:
        body.append(["Skirting", x["room"], x["net_m"], "m net", x["pieces"], f"pcs × {o['stock_m']:g} m",
                     x["confidence"], f"+{o['skirting_waste_pct']:g} % = {x['with_waste_m']} m"])
    sk = ol["skirting"]
    body.append(["Skirting", "TOTAL", sk["total_net_m"], "m net", sk["pieces"], f"pcs × {o['stock_m']:g} m",
                 sk["confidence"], f"+{o['skirting_waste_pct']:g} % = {sk['total_with_waste_m']} m; "
                                   f"{sk['pieces_if_offcuts_reused']} pcs if offcuts are reused across rooms"])
    for x in ol["doors"]["lines"]:
        body.append(["Doors", x["size"], x["sets"], "door sets", x["leaves"], "leaves", ol["doors"].get("confidence"),
                     f"{x['kind']}; " + (f"{x['width_mm']} x {x['height_mm']} mm" if x["width_mm"] else "size not measured")
                     + f" ({ol['doors'].get('source')})"])
    for x in ol["floor"]["lines"]:
        body.append(["Floor covering", x["type"], x["m2"], "m²", x["with_waste_m2"], "m² incl. waste",
                     x["confidence"], f"{len(x['rooms'])} room(s): " + ", ".join(r["room"] for r in x["rooms"])[:200]])
    fl = ol["floor"]
    body.append(["Floor covering", "TOTAL", fl["total_m2"], "m²", fl["total_with_waste_m2"], "m² incl. waste", "",
                 f"+{o['floor_waste_pct']:g} % waste"])
    for sec, key in (("Check by hand (not in totals)", "skirting"), ("Check by hand (not in totals)", "floor")):
        for x in ol[key]["review"]:
            body.append([sec, x["room"], "", "", "", "", "", f"{key}: {x['why']}"])
    d = set(ol.get("defaults") or [])
    body.append(["Defaults", "waste skirting / floor, stock length, door height", "", "", "", "", "",
                 ", ".join(f"{k} = {o[k]:g}" + (" (default)" if k in d else "") for k in DEFAULTS)])
    return head, body


def to_csv(ol: dict) -> str:
    head, body = table(ol)
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(head)
    w.writerows(body)
    return s.getvalue()


def to_xlsx(ol: dict) -> bytes:
    """One sheet 'Order list' via F2's standard-library SpreadsheetML writer (no openpyxl on this machine)."""
    import zipfile

    from stoa.features import takeoff as T

    head, body = table(ol)
    sheet = T._sheet_xml(head, body)
    src = T.to_xlsx([], ol.get("file") or "", "Order list")  # the package skeleton (styles, rels), 2 sheets
    bio_in, bio = io.BytesIO(src), io.BytesIO()
    with zipfile.ZipFile(bio_in) as zi, zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            data = zi.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = sheet.encode()
            zo.writestr(item, data)
    return bio.getvalue()


# ------------------------------------------------------------------------------------------------------- router

def enabled() -> bool:
    raw = {w.strip() for w in os.environ.get("STOA_FEATURES", "").split(",") if w.strip()}
    return "orderlist" in raw or "all" in raw


_VISITOR = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _visitor(v) -> str | None:
    """The browser's 'stoa.visitor' id (F2's takeoff keeps each visitor's Measure fixes apart), or None."""
    return v if isinstance(v, str) and _VISITOR.fullmatch(v) else None


def _status(T, key: str, virtual: str | None = None, visitor: str | None = None) -> dict:
    """takeoff.status with THIS visitor's fixes (night-F2 1ef28b65); an older takeoff without the parameter: as before."""
    if visitor:
        try:
            return T.status(key, virtual, visitor=visitor)
        except TypeError:
            pass
    return T.status(key, virtual) if virtual is not None else T.status(key)


def for_job(job: str, opts: dict | None, visitor: str | None = None) -> dict:
    """Takeoff status -> {status, progress...} or {status 'done', order: <build>}; with the visitor's own fixes."""
    from stoa.features import takeoff as T

    st = _status(T, job, None, _visitor(visitor))
    if st.get("status") != "done":
        return {k: st.get(k) for k in ("job", "status", "done", "total", "current", "error", "file", "page")}
    doors = None
    try:
        pdf, virtual = T.resolve(st.get("file") or "")
        h = opts_of(opts)[0]["door_height_m"]
        doors = _doors_cached(pdf, Path(pdf).stat().st_mtime, int(st.get("page") or 0), st.get("scale_n"), virtual, h)
    except Exception as e:  # noqa: BLE001 - the order list without door sizes is still useful
        doors = {"source": None, "lines": [], "sets": 0, "leaves": 0,
                 "notes": [f"door sizes unavailable ({type(e).__name__})"]}
    return {"job": job, "status": "done", "order": build(st, opts, doors)}


def printed_doors(path: str, page: int = 0, door_height_m: float | None = None) -> dict:
    """Doors from what the drawing PRINTS or links, without a takeoff: the linked IFC model, a door schedule (this page
    or the set's schedule page), printed size tags. Measured swings need the takeoff's scale and are left to it, so
    'How many doors do we need to order?' on a schedule sheet answers in a second instead of after a room takeoff."""
    from stoa.features import takeoff as T

    abs_path, virtual = T.resolve(path)
    none = {"source": None, "lines": [], "sets": 0, "leaves": 0, "notes": []}
    if not abs_path.lower().endswith(".pdf"):
        return none
    h = opts_of({"door_height_m": door_height_m})[0]["door_height_m"]
    return _printed_cached(abs_path, Path(abs_path).stat().st_mtime, int(page), virtual, h)


@functools.lru_cache(maxsize=32)
def _printed_cached(pdf: str, mtime: float, page: int, file: str, h: float) -> dict:
    return door_sizes(pdf, page, None, file, {"door_height_m": h}, printed_only=True)


def peek(path: str, page: int = 0, visitor: str | None = None) -> dict:
    """The takeoff of this sheet page if one exists (done, or still running / queued), else {'status': 'none'}: never
    starts one. The order-list tab uses it to fill itself when the takeoff sheet's Quantities tab already read the
    rooms, without the visitor clicking 'Make order list' (and without a cold run when it did not)."""
    from stoa.features import takeoff as T

    abs_path, virtual = T.resolve(path)
    key = T.job_key(abs_path, int(page))
    if not (T.cache_dir() / key).exists():
        return {"status": "none"}
    st = _status(T, key, virtual, _visitor(visitor))
    if st.get("status") not in ("done", "running", "queued"):
        return {"status": "none"}
    return {**st, "job": key}


try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import Response
    from pydantic import BaseModel

    router = APIRouter(prefix="/feat/orderlist", tags=["orderlist"])

    class StartReq(BaseModel):
        path: str
        page: int = 0
        peek: bool = False  # only find a takeoff that already exists (the takeoff sheet's Quantities tab started it)
        visitor: str = ""  # the browser's 'stoa.visitor' id: its own Measure fixes (F2 per-visitor fixes)

    def _guard() -> None:
        if not enabled():
            raise HTTPException(404, "orderlist feature is off (STOA_FEATURES)")

    def _opts(skirting_waste_pct, stock_m, floor_waste_pct, door_height_m, wet_skirting=None,
              deduct_openings=None) -> dict:
        return {"skirting_waste_pct": skirting_waste_pct, "stock_m": stock_m, "floor_waste_pct": floor_waste_pct,
                "door_height_m": door_height_m, "wet_skirting": wet_skirting, "deduct_openings": deduct_openings}

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "enabled": enabled(), "version": VERSION, "defaults": DEFAULTS}

    @router.post("/start")
    def start_ep(req: StartReq) -> dict:
        _guard()
        from stoa.features import takeoff as T

        try:
            v = _visitor(req.visitor)
            if req.peek:
                return peek(req.path, req.page, v)
            if v:
                try:
                    return T.start(req.path, req.page, visitor=v)
                except TypeError:  # an older takeoff without per-visitor fixes
                    pass
            return T.start(req.path, req.page)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/doors")
    def doors_ep(path: str = Query(...), page: int = 0, door_height_m: float | None = None) -> dict:
        _guard()
        try:
            return printed_doors(path, page, door_height_m)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/build")
    def build_ep(job: str = Query(...), skirting_waste_pct: float | None = None, stock_m: float | None = None,
                 floor_waste_pct: float | None = None, door_height_m: float | None = None,
                 wet_skirting: float | None = None, deduct_openings: float | None = None, visitor: str = "") -> dict:
        _guard()
        try:
            return for_job(job, _opts(skirting_waste_pct, stock_m, floor_waste_pct, door_height_m, wet_skirting,
                                      deduct_openings), visitor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/export")
    def export_ep(job: str = Query(...), fmt: str = "csv", skirting_waste_pct: float | None = None,
                  stock_m: float | None = None, floor_waste_pct: float | None = None,
                  door_height_m: float | None = None, wet_skirting: float | None = None,
                  deduct_openings: float | None = None, visitor: str = ""):
        _guard()
        try:
            got = for_job(job, _opts(skirting_waste_pct, stock_m, floor_waste_pct, door_height_m, wet_skirting,
                                     deduct_openings), visitor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if got.get("status") != "done":
            raise HTTPException(409, f"takeoff is {got.get('status')}")
        ol = got["order"]
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(ol.get("file") or "sheet").stem)[:60]
        if fmt == "xlsx":
            return Response(to_xlsx(ol), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="{stem}-order-list.xlsx"'})
        if fmt == "csv":
            return Response(to_csv(ol), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{stem}-order-list.csv"'})
        raise HTTPException(400, "fmt must be csv or xlsx")
except ImportError:  # the library works without FastAPI
    router = None


if __name__ == "__main__":  # python -m stoa.features.orderlist <takeoff result.json> [abs pdf]
    import json
    import sys

    res = json.loads(Path(sys.argv[1]).read_text())
    dz = door_sizes(sys.argv[2], int(res.get("page") or 0), res.get("scale_n"), res.get("file") or "") \
        if len(sys.argv) > 2 else None
    ol = build(res, None, dz)
    sk, fl, dr = ol["skirting"], ol["floor"], ol["doors"]
    print(f"skirting: {sk['pieces']} pcs, {len(sk['lines'])} rooms, {sk['total_net_m']} m net; review {len(sk['review'])}; "
          f"no skirting {len(sk['excluded'])}")
    for x in sk["lines"]:
        print(f"   {x['room'][:40]:40s} {x['net_m']:7.2f} m  {x['pieces']:3d} pcs  conf {x['confidence']}")
    for x in sk["review"]:
        print(f"   REVIEW {x['room'][:40]:40s} {x['why']}")
    for x in sk["excluded"]:
        print(f"   NONE   {x['room'][:40]:40s} {x['why'][:50]}")
    print(f"floor: {fl['total_m2']} m2 ({fl['total_with_waste_m2']} incl. waste), review {len(fl['review'])}")
    for g in fl["lines"]:
        print(f"   {g['type'][:40]:40s} {g['m2']:8.2f} m2  {len(g['rooms'])} rooms  conf {g['confidence']}")
    print(f"doors: {dr.get('source')} {dr['sets']} sets / {dr['leaves']} leaves; {dr.get('notes')}")
    for g in dr["lines"]:
        print(f"   {g['size']:24s} {g['kind']:6s} {g['sets']:3d} sets {g['leaves']:3d} leaves")
    print(answer_text(ol))
