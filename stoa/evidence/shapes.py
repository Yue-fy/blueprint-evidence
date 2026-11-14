"""Room outlines for evidence boxes (E1; captain 12:44: "areas = the room polygon outlined and filled with the m²").

    from stoa.evidence.shapes import add_polygons
    add_polygons(answer)   # fills Evidence.polygon for boxes that ARE a room / apartment / IFC space

A box gets the outline of the room whose own bounding box it is (IoU >= 0.85 after a 2 pt tolerance), from geometry
we already have, no model call:
- Swiss-style drawings with a DXF twin and ifc_models plans with an SVG twin: stoa.evidence.groundtruth (exact);
- a lone vector PDF (an upload with no twin): the floor model pdf_vector reads from the PDF itself (pdffloor), else
  the sheet's own light-filled closed paths (IFC-viewer / ArchiCAD style sheets draw each room as a fill: G1's
  plan_rooms and E1's ifcplan_pdf measure exactly those, and report their bounding box).
Only room-like objects (room / apartment / IfcSpace); symbols keep their box. Never changes value or confidence.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from stoa.contract import Answer
from stoa.data import abspath

MIN_IOU = 0.85


@functools.lru_cache(maxsize=256)
def _room_shapes(file: str, space: str) -> tuple:
    """((box, polygon), ...) for the room-like objects of a drawing in `space`; () when unknown."""
    from stoa.evidence.groundtruth import TOL_PT, ground_truth

    gt = ground_truth(file, space)
    if gt is not None:
        return tuple((o.box, tuple(map(tuple, o.polygon)), gt.tol) for o in gt.objects
                     if o.kind in ("room", "apartment") and o.polygon and len(o.polygon) >= 3)
    if space != "pdf_pt" or Path(file).suffix.lower() != ".pdf":
        return ()
    out = []
    try:
        from stoa.solvers.vector.pdffloor import load_pdf_floor

        fm, tf, _ = load_pdf_floor(str(abspath(file)))
    except Exception:  # noqa: BLE001
        fm = tf = None
    if fm is not None and tf is not None:
        for r in [*fm.rooms, *fm.units]:
            poly = getattr(r, "poly", None)
            if poly is None or poly.is_empty or not hasattr(poly, "exterior"):
                continue
            pts = tuple(tf.pt(x, y) for x, y in poly.exterior.coords)
            b = tf.box(r.bbox)
            out.append(((b[0], b[1], b[2], b[3]), pts, TOL_PT))
    return tuple(out)


@functools.lru_cache(maxsize=256)
def _fill_shapes(file: str, space: str) -> tuple:
    """Fallback for a vector PDF without a twin: its light-filled closed paths (rooms drawn as fills)."""
    from stoa.evidence.groundtruth import TOL_PT

    if space != "pdf_pt" or Path(file).suffix.lower() != ".pdf":
        return ()
    return tuple((b, pts, TOL_PT) for b, pts in _light_fills(str(abspath(file))))


def _is_light_fill(f) -> bool:
    """A room-like fill: light (mean >= 0.6) but not white; walls / symbols are dark or white."""
    return bool(f) and len(f) == 3 and sum(f) / 3 >= 0.6 and min(f) < 0.985


@functools.lru_cache(maxsize=64)
def _light_fills(pdf_abs: str) -> tuple:
    """((bbox, points), ...) of every LIGHT-FILLED closed path on page 0 of a vector PDF, in pdf_pt.

    Room-area readers that measure the sheet's own filled room polygons (G1's plan_rooms on IFC-viewer / ArchiCAD
    style sheets, E1's ifcplan_pdf) report the polygon's bounding box; this gives that box its outline back."""
    import pymupdf
    from shapely.geometry import Polygon

    from stoa.evidence.ifcplan_pdf import _drawing_points

    with pymupdf.open(pdf_abs) as doc:
        if len(doc) == 0:
            return ()
        page = doc[0]
        W, H = page.rect.width, page.rect.height
        drawings = [d for d in page.get_drawings() if d.get("type") in ("f", "fs") and _is_light_fill(d.get("fill"))]
    out = []
    for d in drawings:
        pts = _drawing_points(d)
        if len(pts) < 3:
            continue
        P = Polygon(pts)
        if P.area < 4 or P.area > 0.5 * W * H:
            continue
        x0, y0, x1, y1 = P.bounds
        out.append(((x0, y0, x1, y1), tuple(pts)))
    return tuple(out)


def add_polygons(ans: Answer) -> Answer:
    if not ans.evidence:
        return ans
    from stoa.evidence.check import _pad, iou

    out, changed = [], False
    for e in ans.evidence:
        if e.bbox and e.polygon is None and e.bbox_space:
            best = (0.0, None)
            for source in (_room_shapes, _fill_shapes):  # twin / floor model first, then the sheet's own fills
                try:
                    shapes = source(e.file, e.bbox_space) if e.page in (0, None) else ()
                except Exception:  # noqa: BLE001
                    shapes = ()
                best = max(((iou(_pad(e.bbox, t), _pad(b, t)), pts) for b, pts, t in shapes), default=(0.0, None),
                           key=lambda x: x[0])
                if best[0] >= MIN_IOU:
                    break
            if best[0] >= MIN_IOU and best[1]:
                e = e.model_copy(update={"polygon": [[round(x, 2), round(y, 2)] for x, y in best[1]]})
                changed = True
        out.append(e)
    if changed:
        ans.evidence = out
    return ans


@functools.lru_cache(maxsize=128)
def _svg_points(plan_path: str) -> dict:
    """GUID -> all path points (pdf_pt) of an ifc_models plan's SVG twin; {} without one."""
    import xml.etree.ElementTree as ET

    from stoa.solvers.ifc.plan2d import _NUM, MM_TO_PT, _path_points

    svg = abspath(str(Path(plan_path).with_suffix(".svg")))
    if not svg.exists():
        return {}
    root = ET.parse(svg).getroot()
    ns = "{http://www.w3.org/2000/svg}"
    vb = [float(v) for v in _NUM.findall(root.get("viewBox", "0 0 0 0"))]
    ox, oy = (vb[0], vb[1]) if len(vb) >= 2 else (0.0, 0.0)
    out: dict[str, list] = {}
    for g in root.iter(f"{ns}g"):
        guid = g.get("data-guid")
        if not guid:
            continue
        pts = out.setdefault(guid, [])
        for p in g.iter(f"{ns}path"):
            for ring in _path_points(p.get("d", "")):
                pts += [((x - ox) * MM_TO_PT, (y - oy) * MM_TO_PT) for x, y in ring]
    return out


def wall_axis(points) -> list[list[float]] | None:
    """Centre line of a wall drawn as an outline: the long axis of its minimum rotated rectangle."""
    from shapely.geometry import MultiPoint

    if len(points) < 3:
        return None
    r = MultiPoint(points).minimum_rotated_rectangle
    if not hasattr(r, "exterior"):
        return None
    c = list(r.exterior.coords)[:4]
    sides = [(c[i], c[(i + 1) % 4]) for i in range(4)]
    (a0, a1), (b0, b1) = (sides[0], sides[2]) if \
        ((sides[0][0][0] - sides[0][1][0]) ** 2 + (sides[0][0][1] - sides[0][1][1]) ** 2) < \
        ((sides[1][0][0] - sides[1][1][0]) ** 2 + (sides[1][0][1] - sides[1][1][1]) ** 2) else (sides[1], sides[3])
    m0 = ((a0[0] + a1[0]) / 2, (a0[1] + a1[1]) / 2)  # midpoints of the two SHORT sides
    m1 = ((b0[0] + b1[0]) / 2, (b0[1] + b1[1]) / 2)
    return [[round(m0[0], 2), round(m0[1], 2)], [round(m1[0], 2), round(m1[1], 2)]]


def add_wall_axes(ans: Answer) -> Answer:
    """Length answers over IFC walls: each wall box on a plan gets its centre line as `polyline`."""
    if not ans.evidence or (ans.unit or "") not in ("m", "ft", "mm", "cm"):
        return ans
    out, changed = [], False
    for e in ans.evidence:
        if e.ifc_guid and e.bbox and e.polyline is None and e.page in (0, None) and \
                str(e.note or "").startswith(("IfcWall", "IfcCurtainWall")) and "projected" not in (e.note or ""):
            try:
                pts = _svg_points(e.file).get(e.ifc_guid) or []
                axis = wall_axis(pts)
            except Exception:  # noqa: BLE001
                axis = None
            if axis:
                e = e.model_copy(update={"polyline": axis})
                changed = True
        out.append(e)
    if changed:
        ans.evidence = out
    return ans


_PERIM = re.compile(r"perimeter|umfang|périmètre|perimetro|omkrets|piiri|ympärys|obwód|omtrek|circumference", re.I)
_WIDTH = re.compile(r"\bwide|width|breit|breite|largeur|larghezza|breedte|leveys|bredd|szeroko", re.I)
_BOTH = re.compile(r"dimension|abmessung|mitat|afmeting|wymiar|\bx\b|×", re.I)


def _rect_sides(polygon) -> tuple[list, list] | None:
    """(long side, short side) of a polygon's minimum rotated rectangle, as two-point lines."""
    from shapely.geometry import Polygon

    try:
        r = Polygon(polygon).minimum_rotated_rectangle
    except Exception:  # noqa: BLE001
        return None
    if not hasattr(r, "exterior"):
        return None
    c = list(r.exterior.coords)[:4]
    a, b = [list(c[0]), list(c[1])], [list(c[1]), list(c[2])]

    def ln(s):
        return ((s[0][0] - s[1][0]) ** 2 + (s[0][1] - s[1][1]) ** 2) ** 0.5

    a, b = ([[round(x, 2) for x in p] for p in a], [[round(x, 2) for x in p] for p in b])
    return (a, b) if ln(a) >= ln(b) else (b, a)


def add_dimension_lines(ans: Answer, question: str) -> Answer:
    """A room's length / width / perimeter answer (its one evidence item is the room outline) gets the measured line:
    the long or short side of the outline's smallest enclosing rectangle, or the outline itself for a perimeter
    (captain 12:44: lengths = the measured polyline with its value). 'L x W' answers get both sides."""
    unit = (ans.unit or "").lower()
    dims = isinstance(ans.value, str) and bool(re.search(r"\d\s*m\s*[x×]\s*\d", ans.value))
    if ans.value is None or not (unit in ("m", "ft", "mm", "cm") or dims) or len(ans.evidence) != 1:
        return ans
    e = ans.evidence[0]
    if not e.polygon or e.polyline is not None or len(e.polygon) < 3:
        return ans
    if _PERIM.search(question):
        ring = [list(p) for p in e.polygon]
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        ans.evidence = [e.model_copy(update={"polyline": ring})]
        return ans
    sides = _rect_sides(e.polygon)
    if not sides:
        return ans
    long_, short = sides
    if dims or _BOTH.search(question):
        ans.evidence = [e.model_copy(update={"polyline": long_}),
                        e.model_copy(update={"polyline": short, "polygon": None,
                                             "note": f"{e.note} — the other side (width)".strip(" —")})]
    else:
        ans.evidence = [e.model_copy(update={"polyline": short if _WIDTH.search(question) else long_})]
    return ans


def add_scale_statement(ans: Answer) -> Answer:
    """Areas / lengths / volumes say which scale they were measured at and whether that is exact (captain 12:44):
    one 'scale: …' line first in assumptions. Read from the solver and its trace; nothing is re-measured."""
    import re

    unit = (ans.unit or "").lower()
    if ans.value is None or not re.match(r"^(m2|m²|ft2|m|ft|mm|cm|m3|m³|ft3)$", unit):
        return ans
    if any(a.lower().startswith("scale") for a in ans.assumptions):
        return ans
    solver = (ans.solver or "").split("/")[-1]
    tr = " | ".join(ans.trace)
    m = re.search(r"scale 1:(\d+)", tr)
    n = f"1:{m.group(1)}" if m else None
    if solver == "dxf":
        s = "scale: measured in the DXF's own coordinates (metres), exact" + (f"; the sheet is drawn at {n}" if n else "")
    elif solver == "ifc":
        s = "scale: taken from the IFC model's geometry and quantities (metres), exact"
    elif solver == "pdf_vector":
        s = (f"scale: {n} read from the sheet's title block; outlines measured on the PDF's vector paths — exact if "
             "the printed scale is right" if n else "scale: from the PDF's vector drawing; scale not confirmed")
        # pdf_vector reuses the DXF rules' wording; on a PDF-only model there is no DXF
        ans.assumptions = [a.replace("ROOM_OUTLINE polygon in the DXF (metres)", "room outline on the PDF (vector paths)")
                           for a in ans.assumptions]
    elif solver == "plan_rooms":
        m2 = re.search(r"printed scale 1:(\d+)", tr)  # plan_rooms' trace: 'printed scale 1:N' / 'no scale printed: 1:N …'
        g = re.search(r"no scale printed[^|]*?1:(\d+)", tr)
        if m2:
            s = (f"scale: 1:{m2.group(1)} printed on the sheet; areas measured on the sheet's filled room "
                 "outlines (vector paths) — exact if the printed scale is right")
        elif g:
            s = (f"scale: NOT printed on the sheet — 1:{g.group(1)} is an estimate (from typical room sizes); "
                 "approximate until you set the scale (two points + their real distance)")
        else:
            s = "scale: from the sheet's own drawing; scale not confirmed"
    elif solver == "svg_ifc":  # P1's IfcConvert SVG reader: scale from door symbols (or 1:100 assumed)
        d = re.search(r"scale 1:(\d+) from (\d+) door symbols", tr)
        if d:
            s = (f"scale: 1:{d.group(1)} measured from {d.group(2)} door symbols on the SVG (snapped to a standard "
                 "scale); outlines are the SVG's own IfcSpace paths — exact if the scale is right")
        elif "assumed 1:100" in tr:
            s = "scale: NOT measured — 1:100 assumed (too few doors on the sheet); approximate until the scale is set"
        else:
            s = "scale: from the SVG's own drawing; scale not confirmed"
    elif solver in ("schedule", "pdf_text"):
        s = "scale: none needed — the value is printed on the sheet (schedule / label), exact as printed"
    else:
        return ans  # raster / VLM / agent state their own basis (scale bar, door width, estimate)
    ans.assumptions = [s, *ans.assumptions]
    return ans


def mark_stacked(ans: Answer) -> Answer:
    """Count answers whose evidence holds several items with the SAME box (two DOOR entities drawn at one spot in the
    CAD file, a double-leaf door mapped to one box): a viewer draws them as one mark, so '64' showed 55 marks (M1
    15:12, tour step 7). Every item is kept (the count of items still equals the answer); each of a stack says
    'x2 at this spot' so the mark explains the number. Never changes value or confidence."""
    if ans.value is None or (ans.unit or "") != "count" or len(ans.evidence) < 2:
        return ans
    groups: dict = {}
    for i, e in enumerate(ans.evidence):
        if e.bbox and e.polygon is None:
            groups.setdefault((str(e.file), e.page, e.bbox_space, tuple(round(v, 1) for v in e.bbox)), []).append(i)
    stacks = [ix for ix in groups.values() if len(ix) > 1]
    if not stacks:
        return ans
    ev = list(ans.evidence)
    for ix in stacks:
        names = " + ".join(ev[i].note.split(":")[-1].strip()[:20] for i in ix)
        for i in ix:
            if "at this spot" not in ev[i].note:
                ev[i] = ev[i].model_copy(update={"note": f"{ev[i].note} (x{len(ix)} at this spot: {names} — drawn "
                                                         "twice in the file or a double element)"})
    ans.evidence = ev
    extra = sum(len(ix) - 1 for ix in stacks)
    ans.trace = [*ans.trace, f"evidence: {len(ans.evidence)} items on {len(ans.evidence) - extra} marks — {len(stacks)} "
                             f"spots hold 2+ counted elements (each says so)"]
    return ans
