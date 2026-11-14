"""Lone DXF floor plans drawn as WALL LINES + room-name texts (no closed room outlines): rooms measured like a line-only
PDF sheet (E1; X1 wild-dev 'dyimba' DXF, P1 15:53: real Revit/AutoCAD DXFs carry rooms as MTEXT names only).

The Swiss DXF reader needs ROOM_OUTLINE polygons; on such files it 'measured' 0 m² @0.96. Here the DXF is drawn onto a
PDF sheet at a printed '1:N' (walls, doors and windows only — furniture, stairs, grids, hatches and dimensions would
cut rooms apart), every TEXT/MTEXT kept as real text at its insert point, and E1's lineplan answers on that sheet
(rooms by label, sums, positions, sides). Evidence is mapped back into DXF drawing units (bbox_space 'dxf', polygons
too), so a viewer of the DXF draws it where the walls are.

Drawing units are CHECKED, not trusted: $INSUNITS is often wrong ('mm' on a file drawn in metres). The unit is the one
under which the door blocks are door-sized (0.6-1.3 m), else the one under which the walls span a building (4-300 m).

    from stoa.evidence.dxfplan import answer, sheet
    answer('/uploads/house.dxf', 'How big is the kitchen?')
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import tempfile
from pathlib import Path

from stoa.contract import Answer, Evidence

SOLVER = "cad_lines"  # no "dxf" in the name: upload.py caps every "dxf" answer on renamed layers
UNIT_M = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 14: 0.1}  # $INSUNITS -> metres per drawing unit
CANDIDATE_UNITS = (1.0, 0.001, 0.01, 0.0254, 0.3048, 0.1)
SCALES = (50, 100, 200, 500, 1000)
MAX_PAGE_PT = 4200.0  # a page side beyond this would be rasterised coarser than ~1 px per pt by linework
# layers that bound rooms: walls, openings (doors close their gap, windows close the wall), columns, curtain walls.
# Whole words only ('A-Furniture' holds 'tur'); AIA 'A-WALL-FULL', 'A-Door', German 'Wände', Spanish 'muros' …
_W = r"(?<![a-zäöüé])(?:{})(?![a-zäöüé])"
_WALL_WORDS = (r"walls?|wand|w[aä]nde|waende|murs?|muros?|pared(?:es)?|paret[ei]|sein[aä]t?|v[aä]gg(?:ar)?|"
               r"column[s]?|st[uü]tzen?|pilar(?:es)?|curtain\w*|glaz\w*")
_OPEN_WORDS = (r"doors?|t[uü]r(?:en)?|tuer(?:en)?|puertas?|portes?|deur(?:en)?|ovi\w*|windows?|fenster|ventanas?|"
               r"fen[eê]tres?|ramen|raam|ikkuna\w*|openings?|[oö]ffnung\w*")
_WALL = re.compile(_W.format(_WALL_WORDS), re.I)
_OPEN = re.compile(_W.format(_OPEN_WORDS), re.I)
# never room boundaries even if the name matches (door TAGS, wall hatches, dimension layers)
_NOT_BOUND = re.compile(r"tag|text|txt|dim|hatch|patt|iden|anno|symb|grid|axis|achs|furn|m[oö]bel", re.I)
_NO_TEXT_LAYER = re.compile(r"grid|axis|achs|dim|cota|title|cartouche|schrift", re.I)
_DOOR_BLOCK = re.compile(_W.format(r"doors?|t[uü]r(?:en)?|tuer|puertas?|portes?|deur(?:en)?|ovi"), re.I)


def _bounds(msp) -> list:
    """Entities that bound rooms: anything on a wall layer; lines on an opening layer; door/window BLOCKS anywhere
    (an 'A-Draw' layer may hold doors; an 'A-Door' layer may hold sofas and sinks, which would cut a room apart)."""
    out = []
    for e in msp:
        lay = e.dxf.layer
        t = e.dxftype()
        if t in ("TEXT", "MTEXT", "DIMENSION", "HATCH", "ATTDEF", "POINT"):
            continue
        if t == "INSERT":
            name = e.dxf.name
            if (_OPEN.search(name) or _WALL.search(name)) and not _NOT_BOUND.search(name):
                out.append(e)
            elif _WALL.search(lay) and not _NOT_BOUND.search(lay) and not _OPEN.search(lay):
                out.append(e)  # wall blocks (column / wall segments as blocks)
            continue
        if (_WALL.search(lay) or _OPEN.search(lay)) and not _NOT_BOUND.search(lay):
            out.append(e)
    return out


def _cache_dir() -> Path:
    d = Path(os.environ.get("STOA_CACHE", tempfile.gettempdir())) / "stoa-dxfplan"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unit(doc, msp) -> tuple[float, str]:
    """Metres per drawing unit, checked against door blocks, else against the walls' span."""
    from ezdxf import bbox

    declared = UNIT_M.get(int(doc.header.get("$INSUNITS", 0) or 0))
    sizes = []
    for e in msp.query("INSERT"):
        if not _DOOR_BLOCK.search(e.dxf.name) or re.search(r"window|fenster|ventana", e.dxf.name, re.I):
            continue
        blk = doc.blocks.get(e.dxf.name)
        if blk is None:
            continue
        ext = bbox.extents(blk, fast=True)
        if ext.has_data:
            sx = abs(float(e.dxf.get("xscale", 1) or 1))
            sizes.append(max(ext.size.x, ext.size.y) * sx)
    order = ([declared] if declared else []) + [u for u in CANDIDATE_UNITS if u != declared]
    if len(sizes) >= 2:
        med = sorted(sizes)[len(sizes) // 2]
        for u in order:
            if 0.6 <= med * u <= 1.3:
                why = f"door blocks {med * u:.2f} m wide"
                return u, why + ("" if u == declared else f" (the file says {declared} m per unit)" if declared else "")
    walls = [e for e in msp if _WALL.search(e.dxf.layer) and not _NOT_BOUND.search(e.dxf.layer)]
    ext = bbox.extents(walls or msp, fast=True)
    span = max(ext.size.x, ext.size.y) if ext.has_data else 0.0
    for u in order:
        if 4 <= span * u <= 300:
            return u, f"the walls span {span * u:.1f} m"
    return (declared or 1.0), "drawing units as declared (not checked)"


@functools.lru_cache(maxsize=8)
def _sheet_cached(path: str, mtime: float) -> dict | None:
    import ezdxf
    import pymupdf
    from ezdxf import bbox
    from ezdxf import path as ezpath

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    bound = _bounds(msp)
    if sum(1 for e in bound if _WALL.search(e.dxf.layer)) < 8:
        return None  # no wall layer by name: not this reader's kind of file
    unit, unit_why = _unit(doc, msp)
    ext = bbox.extents(bound, fast=True)
    if not ext.has_data:
        return None
    x0, y0, x1, y1 = ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y
    margin = 40.0
    pt_per_m = lambda n: 1000 / n / 25.4 * 72  # noqa: E731
    n = next((k for k in SCALES if max(x1 - x0, y1 - y0) * unit * pt_per_m(k) + 2 * margin <= MAX_PAGE_PT), SCALES[-1])
    s = unit * pt_per_m(n)  # pt per drawing unit
    W, H = (x1 - x0) * s + 2 * margin, (y1 - y0) * s + 2 * margin + 20
    a, b = margin - s * x0, margin + s * y1  # x_pt = s x + a ; y_pt = b - s y

    def to(v):
        return pymupdf.Point(s * v[0] + a, b - s * v[1])

    out = pymupdf.open()
    page = out.new_page(width=W, height=H)
    shape = page.new_shape()

    def walk(entities, depth=0):
        for e in entities:
            t = e.dxftype()
            if t in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF", "HATCH", "DIMENSION", "POINT"):
                continue
            if t == "INSERT":
                if depth < 6:
                    try:
                        walk(e.virtual_entities(), depth + 1)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            try:
                pth = ezpath.make_path(e)
            except Exception:  # noqa: BLE001
                continue
            try:
                pts = [to(v) for v in pth.flattening(0.01 / unit)]
            except Exception:  # noqa: BLE001
                continue
            if len(pts) >= 2:
                shape.draw_polyline(pts)

    walk(bound)
    shape.finish(color=(0, 0, 0), width=0.5, closePath=False)
    shape.commit()
    n_txt = 0
    for e in msp.query("TEXT MTEXT"):
        if _NO_TEXT_LAYER.search(e.dxf.layer):
            continue
        try:
            txt = (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text).strip()
        except Exception:  # noqa: BLE001
            continue
        if not txt or not re.search(r"[^\W\d_]{2,}", txt):
            continue
        ins = e.dxf.insert
        if not (x0 - 1e-6 <= ins[0] <= x1 + 1e-6 and y0 - 1e-6 <= ins[1] <= y1 + 1e-6):
            continue
        fs = 6.0
        for k, line in enumerate(txt.splitlines()[:4]):
            w = pymupdf.get_text_length(line, fontname="helv", fontsize=fs)
            c = to((ins[0], ins[1]))
            page.insert_text((c.x - w / 2, c.y + fs * 0.35 + k * fs * 1.15), line, fontname="helv", fontsize=fs)
            n_txt += 1
    page.insert_text((margin, H - 12), f"SCALE 1:{n}", fontname="helv", fontsize=7)
    key = hashlib.sha1(f"{path}|{mtime}".encode()).hexdigest()[:12]
    dst = _cache_dir() / f"{Path(path).stem}.{key}.dxfplan.pdf"
    out.save(str(dst))
    return {"pdf": str(dst), "scale_n": n, "unit_m": unit, "unit_why": unit_why, "s": s, "a": a, "b": b,
            "n_bound": len(bound), "n_text": n_txt,
            "layers": sorted({e.dxf.layer for e in bound}), "blocks": sorted({e.dxf.name for e in bound
                                                                              if e.dxftype() == "INSERT"})}


def sheet(path: str) -> dict | None:
    """The DXF drawn as a PDF sheet for lineplan (cached by path + mtime), or None (no wall layer / unreadable)."""
    try:
        p = str(Path(path).resolve())
        return _sheet_cached(p, os.path.getmtime(p))
    except Exception:  # noqa: BLE001
        return None


def _back(sh: dict, x: float, y: float) -> tuple[float, float]:
    return round((x - sh["a"]) / sh["s"], 4), round((sh["b"] - y) / sh["s"], 4)


def _rect_area(sh: dict, a: Answer) -> None:
    """A RECTANGULAR traced room: area = long side x short side. The sides are measured face to face (pixel centres
    + 1 px); the pixel count of a room on a drawn sheet this size (~1.4 cm per px) runs 1-3 % low (X1 dyimba:
    bathroom 5.21 vs 5.29 = 3.552 x 1.49, gold 5.325)."""
    if str(a.unit or "").lower() not in ("m2", "m²") or len(a.evidence) != 1 or not a.evidence[0].polygon:
        return
    try:
        from shapely.geometry import Polygon

        from stoa.evidence.linework import PT_TO_M_AT_1, _page

        ppt = _page(sh["pdf"], 0)["ppt"]
        poly = Polygon(a.evidence[0].polygon)
        r = poly.minimum_rotated_rectangle
        xy = list(r.exterior.coords)[:4]
        d = sorted(((xy[i][0] - xy[i + 1][0]) ** 2 + (xy[i][1] - xy[i + 1][1]) ** 2) ** 0.5 for i in range(2))
        if poly.area < 0.96 * r.area:
            return  # not a rectangle (L-shape, notch): keep the traced area
        k = sh["scale_n"] * PT_TO_M_AT_1
        sides = [(d[1] + 1 / ppt) * k, (d[0] + 1 / ppt) * k]
        m = next((re.search(r"sides \[([\d.]+), ([\d.]+)\]", t) for t in a.trace if "sides [" in str(t)), None)
        if m:  # measure_room's own sides (from the unsimplified outline)
            sides = [float(m.group(1)), float(m.group(2))]
        v = sides[0] * sides[1]
        old = float(a.value)
        if not 0 < abs(v - old) <= 0.05 * old:
            return
    except Exception:  # noqa: BLE001
        return
    a.value = round(v, 2)
    e = a.evidence[0]
    a.evidence[0] = e.model_copy(update={"note": re.sub(rf"{old:g}\s*m²", f"{a.value:g} m²", e.note)})
    a.trace = [*a.trace, f"dxfplan: rectangular room -> area = {sides[0]:.3f} x {sides[1]:.3f} m = {a.value} m² "
                         f"(pixel count {old} m²)"]


def answer(path: str, question: str) -> Answer | None:
    """lineplan's answer on the DXF's drawn sheet, evidence in DXF units; None when it does not answer."""
    from stoa.evidence import lineplan

    sh = sheet(path)
    if sh is None:
        return None
    a = lineplan.answer(sh["pdf"], question)
    if a is None or a.value is None:
        return None
    _rect_area(sh, a)
    ev = []
    for e in a.evidence:
        upd = {"file": path, "bbox_space": "dxf", "page": None}
        if e.bbox:
            (ax, ay), (bx, by) = _back(sh, e.bbox[0], e.bbox[3]), _back(sh, e.bbox[2], e.bbox[1])
            upd["bbox"] = [ax, ay, bx, by]
        if e.polygon:
            upd["polygon"] = [list(_back(sh, p[0], p[1])) for p in e.polygon]
        if e.polyline:
            upd["polyline"] = [list(_back(sh, p[0], p[1])) for p in e.polyline]
        ev.append(e.model_copy(update=upd))
    a.evidence = ev
    a.solver = SOLVER
    lay = ", ".join(sh["layers"][:6])
    a.assumptions = [f"the DXF has no room outlines: rooms traced between its wall/door/window lines (layers {lay}) "
                     f"on a sheet drawn from it at 1:{sh['scale_n']}; drawing unit = {sh['unit_m']:g} m "
                     f"({sh['unit_why']})", *a.assumptions]
    a.trace = [f"dxfplan: DXF drawn as a line sheet ({sh['n_bound']} wall/opening entities, {sh['n_text']} texts, "
               f"1:{sh['scale_n']}, unit {sh['unit_m']:g} m: {sh['unit_why']}) -> lineplan", *a.trace]
    return a


def replace_nothing_found(path: str, question: str, ans: Answer) -> Answer:
    """Pipeline hook (E1 block): on a lone .dxf whose own reader found nothing (no value, the prior, a 0 area/length,
    a 0 count or an answer under 0.35), the rooms traced between its wall lines answer instead. Never touches a DXF
    the reader answered (Swiss ROOM_OUTLINE files: dataset gold)."""
    try:
        from stoa.data import abspath

        p = str(abspath(path))
    except Exception:  # noqa: BLE001
        p = str(path)
    if not p.lower().endswith(".dxf"):
        return ans
    try:
        v = float(ans.value) if ans.value is not None else None
    except (TypeError, ValueError):
        v = None
    weak = (ans.value is None or v == 0 or (ans.solver or "").endswith("prior") or (ans.confidence or 0) < 0.35)
    if not weak:
        return ans
    b = answer(p, question)
    if b is None:
        return ans
    b.solver = f"pipeline/{SOLVER}" if str(ans.solver or "").startswith("pipeline/") else SOLVER
    b.cost = b.cost.add(ans.cost)
    b.trace = [*(t for t in ans.trace[:2] if str(t).startswith("recognised:")),
               f"E1 cad_lines: the DXF reader found nothing ({ans.solver}: {str(ans.value)[:40]!r} @"
               f"{ans.confidence or 0:.2f}) -> rooms traced between the DXF's wall lines", *b.trace]
    b.recognition = ans.recognition
    return b
