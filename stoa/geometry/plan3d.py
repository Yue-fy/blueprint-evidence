"""2D floor plan -> simple 3D geometry (W5, demo web app: "3D from the 2D plan" for Swiss floors).

Customers mostly have only 2D drawings. For a `swiss_dwellings` floor (PDF + DXF + PNG, no IFC) this reads the DXF
and returns plain geometry the web app extrudes with three.js:

- walls:   WALL closed polylines (DXF metres), extruded to `height` (estimated, not in the drawing)
- rooms:   ROOM_OUTLINE polygons with number / name / printed area / apartment (stoa.solvers.vector.floor)
- doors:   DOOR 5-vertex leaf polylines (drawn open, as on the plan), with the leaf+arc box used as evidence, and
           `gap`: the wall opening the door sits in (rotated rectangle), for a lintel from the door head up
- windows: WINDOW polylines grouped like the dxf solver (3 per window), sill/head heights estimated
- units:   apartment outlines ("Whg. B")
- pdf/png: the DXF -> PDF points affine (pdf_x = s*x + a, pdf_y = -s*y + b) and the PNG pixels per PDF point, so
           answer evidence on the drawing can be mapped back onto the 3D objects.

Reuses the dxf solver's parsing (stoa.solvers.vector.floor.load_floor, pdfmap.fit_transform); nothing is changed
there. Everything is derived from the drawing; heights are assumptions and the payload says so.

CLI (used by the web app's dev server, cached there):  python -m stoa.geometry.plan3d data/swiss_dwellings/x.pdf
"""

from __future__ import annotations

import functools
import json
import math
import sys
from pathlib import Path

WALL_H = 2.7      # storey height assumed for the walls (m)
DOOR_H = 2.1      # door head (m)
SILL_H = 0.9      # window sill (m)
HEAD_H = 2.1      # window head (m)
# fixture blocks (FIXTURE layer): counter height in kitchens, WC / basin / bath height in bathrooms, else low furniture
FIXTURE_H = {"kitchen": 0.9, "bath": 0.55, "other": 0.5}

# room kind from the printed name (DE / FR / IT / EN), for the floor colours
KINDS = [
    ("outdoor", ("balkon", "balcon", "loggia", "terrasse", "terrazza", "sitzplatz", "veranda", "garten", "jardin",
                 "giardino", "laubengang", "terrace", "balcony", "patio", "porch", "wintergarten")),
    ("bath", ("bad", "dusche", "wc", "du/wc", "bain", "douche", "bagno", "doccia", "toilette", "bath", "shower",
              "lavabo", "sanitär")),
    ("kitchen", ("küche", "kueche", "kochen", "cuisine", "cucina", "kitchen")),
    ("living", ("wohnen", "wohnzimmer", "essen", "séjour", "sejour", "salon", "soggiorno", "pranzo", "living",
                "wohnküche", "dining")),
    ("corridor", ("korridor", "gang", "flur", "entrée", "entree", "eingang", "dégagement", "degagement", "hall",
                  "corridoio", "disimpegno", "ingresso", "vorplatz", "diele", "corridor", "treppenhaus")),
    ("storage", ("reduit", "réduit", "abstell", "keller", "cave", "ripostiglio", "cantina", "storage", "estrich",
                 "schacht", "gaine", "lift", "technik")),
    ("bedroom", ("zimmer", "schlafen", "schlafzimmer", "chambre", "camera", "stanza", "bedroom", "room")),
]


def room_kind(name: str | None) -> str:
    n = (name or "").strip().lower()
    if not n:
        return "other"
    for kind, words in KINDS:
        if any(n == w or n.startswith(w) or w in n.split() for w in words):
            return kind
    return "other"


def _r(v: float) -> float:
    return round(float(v), 3)


def _poly(pts) -> list[list[float]]:
    out = [[_r(x), _r(y)] for x, y in pts]
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _bbox(pts) -> list[float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]


def _inside(b, box, tol: float = 0.02) -> bool:
    return b[0] >= box[0] - tol and b[1] >= box[1] - tol and b[2] <= box[2] + tol and b[3] <= box[3] + tol


def _door_gap(wall_union, leaf) -> list[list[float]] | None:
    """The opening a door sits in, as a rectangle in the wall (DXF metres), so the web app can put a lintel over it
    (door head to wall top). Swiss DXFs draw the door panel closed, lying in the opening (a thin rectangle as long as
    the opening is wide); the wall's thickness is read at the two jambs just beyond its ends. None when no jamb is
    found or the panel does not look like one (the 3D view then keeps the full-height gap)."""
    from shapely.geometry import LineString, Point, Polygon

    if not leaf or len(leaf) < 3:
        return None
    r = Polygon(leaf).minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return None
    xy = list(r.exterior.coords)[:4]
    e = [((xy[(i + 1) % 4][0] - xy[i][0]), (xy[(i + 1) % 4][1] - xy[i][1])) for i in range(4)]
    lens = [(dx * dx + dy * dy) ** 0.5 for dx, dy in e]
    k = max(range(4), key=lambda i: lens[i])
    width, thick = lens[k], lens[(k + 1) % 4]
    if not (0.5 <= width <= 1.8 and thick <= 0.4):
        return None
    vx, vy = e[k][0] / width, e[k][1] / width
    ux, uy = -vy, vx
    cx, cy = sum(p[0] for p in xy) / 4, sum(p[1] for p in xy) / 4
    spans = []
    for t in (-width / 2 - 0.06, width / 2 + 0.06):
        jx, jy = cx + vx * t, cy + vy * t
        hit = LineString([(jx - ux * 0.8, jy - uy * 0.8), (jx + ux * 0.8, jy + uy * 0.8)]).intersection(wall_union)
        best = None
        for g in getattr(hit, "geoms", [hit]):
            if g.is_empty or g.geom_type != "LineString":
                continue
            ws = [(x - cx) * ux + (y - cy) * uy for x, y in g.coords]
            w0, w1 = min(ws), max(ws)
            d = 0.0 if w0 <= 0 <= w1 else min(abs(w0), abs(w1))
            if d <= 0.15 and (best is None or d < best[0]):
                best = (d, w0, w1)
        if best:
            spans.append(best[1:])
    # a jamb whose wall runs on across the probe is a perpendicular wall at a corner, not the wall the door is in
    spans = [sp for sp in spans if sp[1] - sp[0] <= 0.6]
    if not spans:
        return None
    w0, w1 = min(s[0] for s in spans), max(s[1] for s in spans)
    if not 0.05 <= w1 - w0 <= 0.8 or wall_union.contains(Point(cx, cy)):
        return None
    pt = lambda t, w: (cx + vx * t + ux * w, cy + vy * t + uy * w)  # noqa: E731
    return _poly([pt(-width / 2, w0), pt(width / 2, w0), pt(width / 2, w1), pt(-width / 2, w1)])


@functools.lru_cache(maxsize=64)
def plan3d(path: str) -> dict:
    """Geometry for one Swiss floor given any of its files (dataset-relative path, or absolute)."""
    import ezdxf
    from shapely.geometry import MultiPoint

    from stoa.data import abspath, siblings
    from stoa.solvers.vector.floor import load_floor

    sib = siblings(path) if not Path(path).is_absolute() else {}
    dxf_rel = sib.get(".dxf") or (path if path.lower().endswith(".dxf") else None)
    if not dxf_rel and Path(path).is_absolute():
        cand = Path(path).with_suffix(".dxf")
        dxf_rel = str(cand) if cand.exists() else None
    if not dxf_rel:
        return {"ok": False, "error": "no DXF next to this drawing: a 3D view is generated only from a CAD floor plan"}
    dxf_abs = str(abspath(dxf_rel)) if not Path(dxf_rel).is_absolute() else dxf_rel

    fm = load_floor(dxf_abs)
    doc = ezdxf.readfile(dxf_abs)
    walls, door_leaves, window_lines, fixture_shapes = [], [], [], []
    for e in doc.modelspace():
        if e.dxf.layer == "FIXTURE" and e.dxftype() == "CIRCLE":  # basins, WC bowls, hobs: a 16-gon
            cx, cy, rad = float(e.dxf.center[0]), float(e.dxf.center[1]), float(e.dxf.radius)
            if 0.03 <= rad <= 1.5:
                fixture_shapes.append(_poly([(cx + rad * math.cos(2 * math.pi * k / 16), cy + rad * math.sin(2 * math.pi * k / 16))
                                             for k in range(16)]))
            continue
        if e.dxftype() != "LWPOLYLINE":
            continue
        pts = [(float(p[0]), float(p[1])) for p in e.get_points("xy")]
        layer = e.dxf.layer
        if layer == "WINDOW" and len(pts) >= 2:  # a window's 3 lines are often 2-point polylines
            window_lines.append(_poly(pts) if len(pts) > 2 else [[_r(x), _r(y)] for x, y in pts])
            continue
        if len(pts) < 3:
            continue
        if layer == "WALL":
            walls.append(_poly(pts))
        elif layer == "DOOR" and len(pts) == 5:
            door_leaves.append(_poly(pts))
        elif layer == "FIXTURE":
            fixture_shapes.append(_poly(pts))

    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    wall_union = unary_union([Polygon(w).buffer(0) for w in walls if len(w) >= 3])
    doors = []
    for k, it in enumerate(fm.items.get("door", [])):
        box = [_r(v) for v in it.bbox]
        leaf = next((d for d in door_leaves if _inside(_bbox(d), box)), None)
        try:
            gap = _door_gap(wall_union, leaf)
        except Exception:  # noqa: BLE001 - a lintel is decoration: never fail the 3D view for it
            gap = None
        doors.append({"id": f"door {k + 1}", "bbox": box, "leaf": leaf, "gap": gap})
    windows = []
    for k, it in enumerate(fm.items.get("window", [])):
        box = [_r(v) for v in it.bbox]
        lines = [w for w in window_lines if _inside(_bbox(w), box)]
        # footprint in the wall gap: the smallest rotated rectangle around the window's lines (walls need not be
        # axis-aligned); the box when that degenerates
        foot = None
        pts = [p for w in lines for p in w]
        if len(pts) >= 3:
            rect = MultiPoint(pts).minimum_rotated_rectangle
            if rect.geom_type == "Polygon" and rect.area > 1e-4:
                foot = _poly(list(rect.exterior.coords))
        if foot is None:
            foot = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
        windows.append({"id": f"window {k + 1}", "bbox": box, "foot": foot})
    rooms = []
    for r in fm.rooms:
        geom = r.poly
        if geom.geom_type != "Polygon":  # make_valid may give a MultiPolygon: keep its largest part
            geom = max(getattr(geom, "geoms", [geom]), key=lambda g: g.area)
        rooms.append({
            "id": f"room {r.idx}", "number": r.number, "name": r.name, "area": _r(r.net), "unit": r.unit,
            "kind": room_kind(r.name), "bbox": [_r(v) for v in r.bbox],
            "poly": _poly(list(geom.exterior.coords)),
            "anchor": [_r(r.anchor[0]), _r(r.anchor[1])],
        })
    units = [{"label": u.label, "bbox": [_r(v) for v in u.bbox], "poly": _poly(list(u.poly.exterior.coords))}
             for u in fm.units if u.poly is not None and u.poly.geom_type == "Polygon"]
    # FIXTURE layer (captain 16:00: show what the drawing contains, nothing invented): closed outlines of kitchen
    # counters, baths, basins, WCs as low blocks; the height is an estimate by the room they stand in
    fixtures = []
    room_polys = [(Polygon(r["poly"]).buffer(0), r["kind"]) for r in rooms if len(r["poly"]) >= 3]
    for f in fixture_shapes:
        if len(f) < 3:
            continue
        try:
            fp = Polygon(f).buffer(0)
        except Exception:  # noqa: BLE001 - a broken outline is skipped, never fails the 3D view
            continue
        if fp.is_empty or fp.area < 0.01 or fp.area > 12:
            continue
        if fp.geom_type != "Polygon":
            fp = max(fp.geoms, key=lambda g: g.area)
        c = fp.representative_point()
        kind = next((k for rp, k in room_polys if rp.contains(c)), "other")
        h = FIXTURE_H.get(kind, FIXTURE_H["other"])
        fixtures.append({"poly": _poly(list(fp.exterior.coords)), "h": h, "room_kind": kind})

    allpts = [p for w in walls for p in w] + [p for r in rooms for p in r["poly"]]
    bb = _bbox(allpts) if allpts else [0, 0, 1, 1]

    pdf = png = None
    pdf_rel = sib.get(".pdf")
    if not pdf_rel and Path(path).is_absolute():  # an upload outside the dataset: the sheet next to its DXF
        cand = Path(dxf_abs).with_suffix(".pdf")
        pdf_rel = str(cand) if cand.exists() else None
    if pdf_rel:
        try:
            from stoa.solvers.vector.pdfmap import fit_transform, png_scale

            tf = fit_transform(str(abspath(pdf_rel)), dxf_abs)
            if tf is not None:
                pdf = {"file": pdf_rel, "s": tf.s, "a": tf.a, "b": tf.b, "page_w": tf.page_w, "page_h": tf.page_h,
                       "rms_pt": round(tf.rms_pt, 2), "scale": f"1:{round(tf.scale_ratio)}"}
                if sib.get(".png"):
                    png = {"file": sib[".png"], "k": png_scale(str(abspath(sib[".png"])), tf.page_w)}
        except Exception as ex:  # noqa: BLE001 - the 3D view works without the mapping (no evidence highlight)
            pdf = {"error": str(ex)[:200]}

    return {
        "ok": True, "dxf": dxf_rel, "bbox": bb,
        "heights": {"wall": WALL_H, "door": DOOR_H, "sill": SILL_H, "head": HEAD_H},
        "assumptions": [f"generated from the floor plan (DXF): walls extruded to {WALL_H} m, doors {DOOR_H} m, "
                        f"windows {SILL_H}–{HEAD_H} m; heights are estimates, not in the drawing"],
        "walls": walls, "rooms": rooms, "doors": doors, "windows": windows, "units": units, "fixtures": fixtures,
        "pdf": pdf, "png": png,
    }


if __name__ == "__main__":
    out = plan3d(sys.argv[1])
    sys.stdout.write(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
