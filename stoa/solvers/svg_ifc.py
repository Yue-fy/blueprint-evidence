"""IfcConvert-style SVG plan sheets, read deterministically (owner: P1; captain 14 Nov 12:52 'SVG uploads').

An SVG exported from an IFC model (IfcConvert; the dataset's ifc_models/*_plan-*.svg) keeps the model's semantics:
every element is a <g class="IfcSpace|cut IfcDoor|cut IfcWall|..." data-name=... data-guid=...> with its outline as
paths. Uploaded alone (no IFC, no PDF twin) the pipeline had no reader for it: 30 dev questions re-asked on lone SVG
copies scored 3.3% (prior) vs 100% on the PDF with its model (P1 run 20261114-130418-pipeline-up_fmt-2c38).

Answers (0 model calls): counts of one IFC class on the sheet (deduplicated by GUID), the net area of one named space
(IfcSpace polygon), the total area of all spaces. Scale: coordinates are paper millimetres (width="…mm" = viewBox) at a
sheet scale that varies (1:100 on the Esplanades sheets: gold / polygon area matched 40/40 rooms at 0.1 m per unit;
1:200 on others), so scale_of() measures it from the door symbols and snaps it to a standard scale (stated as an
assumption). Volumes need thicknesses the SVG does not carry -> abstain naming the model.
Evidence: each counted element / the space outline in bbox_space "svg" (D29: the SVG's user coordinates), exact.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from stoa.contract import Answer, Evidence
from stoa.data import abspath, dataset_rel
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

SVG = "{http://www.w3.org/2000/svg}"
M_PER_UNIT = 0.1  # 1:100 on paper mm (see module doc)
CLASSES = {"door": "IfcDoor", "window": "IfcWindow", "room": "IfcSpace", "space": "IfcSpace", "wall": "IfcWall",
           "slab": "IfcSlab", "column": "IfcColumn", "stair": "IfcStair", "beam": "IfcBeam", "railing": "IfcRailing"}
# plurals too (H 23:52: 'Sum of all slab areas?' did not read as an area: the slab question then abstained as a volume)
_VOL = re.compile(r"\b(volumes?|volumen|volymer|volym|tilavuus|tilavuudet|cubage)\b|m³|m3\b", re.I)
_AREA = re.compile(r"\b(areas?|flächen|fläche|flachen|flache|surfaces?|superficies?|pinta-alat|pinta-ala|ala|ytor|yta|"
                   r"oppervlakten|oppervlakte|arealer|areal)\b|m²|m2\b|"
                   r"nettofläche|grundfläche|wohnfläche", re.I)


_PICK = re.compile(r"\b(which|where|who|whose|name|names|list|largest|smallest|biggest|most|least|compare)\b", re.I)


_TOK = re.compile(r"[MmLlHhVvAaCcSsQqTtZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_NARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "A": 7, "C": 6, "S": 4, "Q": 4, "T": 2, "Z": 0}


def parse_path(d: str) -> tuple[list[tuple[float, float]], list[float]]:
    """(vertices, arc radii) of an SVG path: M/L/H/V vertices, the END point of A/C/S/Q/T segments (never arc
    parameters or control points), absolute and relative commands."""
    pts, radii, _ = _parse(d)
    return pts, radii


def parse_rings(d: str) -> list[list[tuple[float, float]]]:
    """The path's subpaths (one ring per M), e.g. a space outline and its holes."""
    pts, _, starts = _parse(d)
    cuts = [*starts, len(pts)]
    return [pts[a:b] for a, b in zip(cuts, cuts[1:]) if b - a >= 1]


def _parse(d: str) -> tuple[list[tuple[float, float]], list[float], list[int]]:
    toks = _TOK.findall(d or "")
    starts: list[int] = []
    pts: list[tuple[float, float]] = []
    radii: list[float] = []
    x = y = sx = sy = 0.0
    cmd, i = "M", 0
    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd, i = t, i + 1
            if cmd in "Zz":
                x, y = sx, sy
                continue
        n = _NARGS[cmd.upper()]
        if n == 0 or i + n > len(toks) or any(v.isalpha() for v in toks[i:i + n]):
            i += 1
            continue
        a = [float(v) for v in toks[i:i + n]]
        i += n
        rel = cmd.islower()
        C = cmd.upper()
        if C == "H":
            x = a[0] + (x if rel else 0)
        elif C == "V":
            y = a[0] + (y if rel else 0)
        else:
            if C == "A":
                radii += [abs(a[0]), abs(a[1])]
            ex, ey = a[-2], a[-1]
            x, y = (x + ex, y + ey) if rel else (ex, ey)
            if C == "M":
                sx, sy = x, y
                starts.append(len(pts))
                cmd = "l" if rel else "L"  # implicit lineto after a moveto
        pts.append((x, y))
    return pts, radii, starts or [0]


def _poly_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    s = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _rings_area(rings: list[list[tuple[float, float]]]) -> float:
    """Outer ring minus the rings inside it (holes: E1 13:33, 'Comunication (-1.1)' 11.03 -> 10.43 m² gold);
    rings outside it are separate pieces and add."""
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return 0.0
    outer = max(rings, key=_poly_area)
    tot = _poly_area(outer)
    for r in rings:
        if r is not outer:
            tot += -_poly_area(r) if _inside(*r[0], outer) else _poly_area(r)
    return max(tot, 0.0)


def _shape_rings(el) -> list[list[tuple[float, float]]]:
    """Rings of a <path>/<polygon>/<polyline>/<rect>/<circle>/<ellipse> (columns are circles: E1 13:33)."""
    import math

    tag = el.tag.replace(SVG, "")
    f = lambda k: float(el.get(k) or 0)  # noqa: E731
    if tag == "path":
        return parse_rings(el.get("d", ""))
    if tag in ("polygon", "polyline"):
        v = [float(t) for t in re.findall(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", el.get("points") or "")]
        return [list(zip(v[0::2], v[1::2]))]
    if tag == "rect":
        x, y, w, h = f("x"), f("y"), f("width"), f("height")
        return [[(x, y), (x + w, y), (x + w, y + h), (x, y + h)]] if w > 0 and h > 0 else []
    if tag in ("circle", "ellipse"):
        cx, cy = f("cx"), f("cy")
        rx = f("r") if tag == "circle" else f("rx")
        ry = f("r") if tag == "circle" else f("ry")
        if rx <= 0 or ry <= 0:
            return []
        return [[(cx + rx * math.cos(2 * math.pi * k / 48), cy + ry * math.sin(2 * math.pi * k / 48)) for k in range(48)]]
    return []


_SHAPES = {f"{SVG}{t}" for t in ("path", "polygon", "polyline", "rect", "circle", "ellipse")}
_SCALE_TXT = re.compile(r"\b1\s*:\s*(\d{2,4})\b")


def _length_px(v: str | None, fallback: float) -> float:
    if not v:
        return fallback
    m = re.match(r"([\d.]+)\s*(mm|cm|in|px|pt)?", v)
    if not m:
        return fallback
    x, u = float(m.group(1)), (m.group(2) or "px")
    return x * {"mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pt": 96 / 72, "px": 1.0}[u]


_DOOR_MM = re.compile(r"(\d{3,4})\s*[xX×]\s*(\d{3,4})\s*mm")
STANDARD = (50, 100, 200, 250, 500)


def scale_of(svg: dict) -> tuple[float, str]:
    """(metres per SVG unit, how): from the doors drawn on the sheet. A door's swing arc radius is its leaf width (or,
    without an arc, the symbol is about one leaf wide); the leaf width comes from the door's name when it carries one
    (Revit: '0762 x 2134mm'), else 0.9 m.
    Snapped to a standard scale (paper mm units): 1:50, 1:100, 1:200, 1:250, 1:500. Fallback 1:100."""
    import statistics

    printed = sorted({int(m.group(1)) for _, _, t in svg.get("labels") or [] for m in _SCALE_TXT.finditer(t)} &
                     (set(STANDARD) | {20, 25, 75}))
    ratios, named = [], 0
    for e in svg["elements"]:
        if not e["cls"].startswith("IfcDoor"):
            continue
        x0, y0, x1, y1 = e["bbox"]
        side = max(e.get("radii") or [0]) or max(x1 - x0, y1 - y0)  # swing radius = leaf width, else the symbol
        if side <= 0:
            continue
        m = _DOOR_MM.search(e["name"])
        named += bool(m)
        ratios.append((int(m.group(1)) / 1000 if m else 0.9) / side)
    if len(printed) == 1:  # E1 13:33: every dataset IfcConvert sheet prints 'Scale 1:100' / '1:200'
        n = printed[0]
        how = f"scale 1:{n} printed on the sheet"
        if len(ratios) >= 3:
            off = statistics.median(ratios) / (n / 1000)
            how += (f" (door symbols agree: {off:.2f}x)" if 0.75 <= off <= 1.33 else
                    f" (door symbols measure {off:.2f}x that: check the scale)")
        return n / 1000, how
    if len(ratios) < 3:
        return 0.1, "fewer than 3 doors to measure: assumed 1:100"
    est = statistics.median(ratios)  # metres per unit
    best = min(STANDARD, key=lambda d: abs(__import__("math").log(est / (d / 1000))))
    off = est / (best / 1000)
    how = (f"scale 1:{best} from {len(ratios)} door symbols ({named} with their width in the name; "
           f"measured {off:.2f}x the standard scale)")
    if not 0.75 <= off <= 1.33:
        how += "; doors fit no standard scale well, the area may be off"
    return best / 1000, how


def read_svg(path: str) -> dict | None:
    """{'elements': [{'cls', 'name', 'guid', 'area_units', 'bbox'}], 'px_per_unit', 'origin'}; None if not IFC-style."""
    try:
        root = ET.parse(abspath(path)).getroot()
    except Exception:  # noqa: BLE001
        return None
    vb = [float(v) for v in (root.get("viewBox") or "0 0 0 0").replace(",", " ").split()]
    if len(vb) != 4 or vb[2] <= 0:
        return None
    px_per_unit = _length_px(root.get("width"), vb[2]) / vb[2]
    els = []
    for g in root.iter(f"{SVG}g"):
        cls = g.get("class") or ""
        ifc = next((c for c in cls.split() if c.startswith("Ifc")), None)
        if not ifc:
            continue
        pts_all, area, radii, polys = [], 0.0, [], []
        for p in g.iter():
            if p.tag not in _SHAPES:
                continue
            rings = _shape_rings(p)
            if p.tag == f"{SVG}path":
                radii += parse_path(p.get("d", ""))[1]
            for r in rings:
                pts_all += r
            if p.tag != f"{SVG}polyline":
                area += _rings_area(rings)
            if ifc == "IfcSpace" and rings:
                polys.append(max(rings, key=_poly_area))
        if not pts_all:
            continue
        xs, ys = [x for x, _ in pts_all], [y for _, y in pts_all]
        els.append({"cls": ifc, "name": g.get("data-name") or "", "guid": g.get("data-guid") or "",
                    "area_units": area, "bbox": [min(xs), min(ys), max(xs), max(ys)], "radii": radii, "polys": polys,
                    "pts": pts_all if ifc.startswith("IfcWall") else None})
    if not els:
        return None
    labels = []  # IfcConvert prints each space's long name above its number at the label point: (x, y, text)
    for t in root.iter(f"{SVG}text"):
        try:
            x, y = float(t.get("x")), float(t.get("y"))
        except (TypeError, ValueError):
            continue
        txt = "".join(t.itertext()).strip()
        if txt:
            labels.append((x, y, txt))
    return {"elements": els, "px_per_unit": px_per_unit, "origin": (vb[0], vb[1]), "labels": labels}


def _hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo: list = []
    up: list = []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def min_rect(pts: list[tuple[float, float]]):
    """(area, long side, short side, centre line [(x, y), (x, y)]) of the minimum-area rotated rectangle, or None."""
    import math

    h = _hull(pts)
    if len(h) < 3:
        return None
    best = None
    for i in range(len(h)):
        (x1, y1), (x2, y2) = h[i], h[(i + 1) % len(h)]
        a = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(a), math.sin(a)
        us, vs = [x * c + y * s for x, y in h], [-x * s + y * c for x, y in h]
        w, t = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * t < best[0]:
            best = (w * t, a, min(us), max(us), min(vs), max(vs))
    _, a, u0, u1, v0, v1 = best
    c, s = math.cos(a), math.sin(a)
    w, t = u1 - u0, v1 - v0

    def xy(u, v):
        return (u * c - v * s, u * s + v * c)

    if w >= t:  # centre line along u
        line = [xy(u0, (v0 + v1) / 2), xy(u1, (v0 + v1) / 2)]
    else:
        line = [xy((u0 + u1) / 2, v0), xy((u0 + u1) / 2, v1)]
    return best[0], max(w, t), min(w, t), line


def _inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    hit, n = False, len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def long_names(svg: dict, e: dict) -> set[str]:
    """Long names printed inside a space's outline right above its number ('gang' above '15'), lower case."""
    labels = svg.get("labels") or []
    out = set()
    for x, y, txt in labels:
        if txt.lower() != e["name"].strip().lower() or not any(_inside(x, y, p) for p in e.get("polys") or []):
            continue
        out |= {t2.lower() for x2, y2, t2 in labels if abs(x2 - x) < 0.6 and 0 < y - y2 <= 4}
    return out


@register("svg_ifc")
class SvgIfc(BaseSolver):
    name = "svg_ifc"

    def can_handle(self, path: str, question: str) -> bool:
        return Path(path).suffix.lower() == ".svg"

    def _ev(self, path: str, svg: dict, e: dict, note: str) -> Evidence:
        # D29: bbox_space "svg" = the SVG's own user coordinates (the numbers in its paths, y down); the drawing's
        # own geometry, so exact; a space carries its outline (largest ring) as the polygon
        poly = max(e.get("polys") or [[]], key=_poly_area)
        return Evidence(file=dataset_rel(path), bbox=[round(v, 3) for v in e["bbox"]], bbox_space="svg", note=note,
                        exact=True, polygon=[[round(x, 3), round(y, 3)] for x, y in poly] if len(poly) >= 3 else None)

    def solve(self, path: str, question: str) -> Answer:
        from stoa.router.intent import parse

        svg = read_svg(path)
        if svg is None:
            return Answer.abstain(self.name, "not an IFC-style SVG (no class=Ifc* groups)",
                                  missing=["an SVG exported from an IFC model, or the drawing as PDF/PNG"])
        it = parse(question)
        task = it.task
        if task == "volume" and not _VOL.search(question):  # 'CUBICLES' is not a volume (intent parser reads 'cubic')
            task = "area" if _AREA.search(question) else task
        elif task == "text" and _AREA.search(question) and not _VOL.search(question) and not _PICK.search(question):
            task = "area"  # H 23:52: the parser reads the plural 'Sum of all slab areas?' as a text question
        trace = [f"IfcConvert-style SVG: {len(svg['elements'])} IFC elements "
                 f"({', '.join(sorted({e['cls'] for e in svg['elements']}))})", f"question: {task} {it.target} [{it.scope}]"]
        mpu, how = scale_of(svg)
        trace.append(how)
        assume = ["SVG exported from the IFC model (IfcConvert): one sheet = one storey; coordinates in paper mm; " + how]
        storey = (it.filters or {}).get("storey")
        if storey:
            assume.append(f"'{storey}' taken to be the storey this sheet shows")
        if task == "volume":
            return Answer.abstain(self.name, "an SVG plan has no thicknesses", trace,
                                  missing=["the IFC model (element thicknesses) for volumes"])
        if task == "count":
            cls = CLASSES.get(it.target or "")
            if not cls:
                return Answer.abstain(self.name, f"no IFC class for '{it.target}'", trace)
            seen, items = set(), []
            for e in svg["elements"]:
                if e["cls"].startswith(cls) and (e["guid"] or id(e)) not in seen:
                    seen.add(e["guid"] or id(e))
                    items.append(e)
            trace.append(f"{len(items)} distinct {cls} on the sheet (by GUID)")
            ev = [self._ev(path, svg, e, f"{cls} {i + 1}: {e['name'][:40]} (guid {e['guid']})") for i, e in enumerate(items)]
            conf, missing = 0.85, []
            if it.scope in ("model", "building"):  # the whole model asked, one storey's sheet given
                conf = 0.3
                assume.append("the question asks about the whole model; this sheet shows one storey, so this counts "
                              "only what is drawn here (a lower bound)")
                missing.append("the IFC model or a sheet for every storey")
            return Answer(value=len(items), unit="count", confidence=conf, evidence=ev, trace=trace, solver=self.name,
                          assumptions=assume, missing=missing)
        if task == "area":
            other = CLASSES.get(it.target or "")
            if other and other != "IfcSpace":  # 'Sum of all slab areas?' was answered with the ROOMS' sum @0.85
                return Answer.abstain(self.name, f"the area of {it.target}s is not the rooms' area, and a plan cut "
                                      f"shows only the {it.target}s it cuts", trace,
                                      missing=[f"the IFC model ({other} quantities) for {it.target} areas"])
            spaces = [e for e in svg["elements"] if e["cls"] == "IfcSpace" and e["area_units"] > 0]
            if not spaces:
                return Answer.abstain(self.name, "no IfcSpace outlines on this sheet", trace)
            f = it.filters or {}
            want = f.get("room") or f.get("label") or f.get("room_no")
            if want and it.scope == "room":
                inner = [x.strip().lower() for x in re.findall(r"\(([^()]+)\)", want)]
                hit = []
                for keys in (inner, [want.strip().lower()], [want.split(" (")[0].strip().lower()]):  # number first
                    hit = [e for e in spaces if keys and e["name"].strip().lower() in keys]
                    if hit:
                        break
                if len({e["guid"] for e in hit}) == 1 and len(hit) > 1:  # one space drawn as several groups
                    hit = [{**hit[0], "area_units": sum(e["area_units"] for e in hit)}]
                conf = 0.9
                if len(hit) > 1:
                    areas = [e["area_units"] for e in hit]
                    long = want.split(" (")[0].strip().lower() if "(" in want else ""
                    by_label = [e for e in hit if long and long in long_names(svg, e)]
                    if max(areas) <= 1.02 * min(areas):  # the same room drawn more than once (e.g. body + footprint)
                        trace.append(f"{len(hit)} outlines named '{hit[0]['name']}' with the same area: one room")
                        hit, conf = hit[:1], 0.7
                    elif len(by_label) == 1:  # several spaces numbered '15': the one labelled 'gang' inside
                        trace.append(f"{len(hit)} spaces numbered '{hit[0]['name']}'; the one with '{long}' printed "
                                     "in its outline")
                        hit, conf = by_label, 0.85
                if len(hit) != 1:
                    return Answer.abstain(self.name, f"{len(hit)} spaces named like '{want}'", trace,
                                          missing=[f"a space named '{want}' on this sheet"])
                a = hit[0]["area_units"] * mpu ** 2
                trace.append(f"space '{hit[0]['name']}' (guid {hit[0]['guid']}): outline {a:.2f} m²")
                return Answer(value=round(a, 2), unit="m2", confidence=conf, solver=self.name, trace=trace,
                              evidence=[self._ev(path, svg, hit[0], f"space {hit[0]['name']}: {a:.2f} m²")],
                              assumptions=assume)
            tot = sum(e["area_units"] for e in spaces) * mpu ** 2
            trace.append(f"sum of {len(spaces)} IfcSpace outlines = {tot:.2f} m²")
            ev = [self._ev(path, svg, e, f"space {e['name']}: {e['area_units'] * mpu ** 2:.2f} m²") for e in spaces]
            return Answer(value=round(tot, 2), unit="m2", confidence=0.85, evidence=ev, trace=trace, solver=self.name,
                          assumptions=assume)
        if task == "length" and it.target == "wall" and it.scope not in ("room",):
            return self._wall_length(path, svg, mpu, trace, assume, it)
        return Answer.abstain(self.name, f"'{task}' questions are not read from SVG sheets yet", trace)

    def _wall_length(self, path, svg, mpu, trace, assume, it) -> Answer:
        """Total wall length on the sheet: per wall (GUID), the long side of the minimum rotated rectangle around its
        cut outline minus half its thickness (walls overlap at corners). 9 dev storeys (6 sheets) vs the IFC's wall
        lengths: within 2% on 6 (0.99-1.01x), 0.97x, 0.89x (attic) and 0.86x (walls below the 1.2 m cut are not
        drawn) -> conf 0.5, never 'exact'."""
        walls: dict[str, list] = {}
        for e in svg["elements"]:
            if e["cls"].startswith("IfcWall"):
                walls.setdefault(e["guid"] or str(id(e)), []).extend(e.get("pts") or [])
        axes = [(g, min_rect(pts)) for g, pts in walls.items() if len(pts) >= 3]
        axes = [(g, r) for g, r in axes if r and r[1] > 0]
        if not axes:
            return Answer.abstain(self.name, "no IfcWall outlines on this sheet", trace,
                                  missing=["the IFC model (wall lengths)"])
        units = sum(long - thick / 2 for _, (_, long, thick, _) in axes)
        total = units * mpu
        trace.append(f"{len(axes)} walls (by GUID): sum of (outline long side - half thickness) = {total:.2f} m")
        assume = [*assume, "wall length = long side of each wall's cut outline minus half its thickness (corner "
                           "overlaps); walls not cut by the plan (below 1.2 m) are not drawn and not counted"]
        conf, missing = 0.5, ["the IFC model's wall quantities for an exact total"]
        if it.scope in ("model", "building"):
            conf = 0.3
            missing.append("a sheet for every storey (this one shows one storey)")
        ev = [Evidence(file=dataset_rel(path), bbox=[round(min(x for x, _ in ax), 3), round(min(y for _, y in ax), 3),
                                                     round(max(x for x, _ in ax), 3), round(max(y for _, y in ax), 3)],
                       bbox_space="svg", exact=False, polyline=[[round(x, 3), round(y, 3)] for x, y in ax],
                       note=f"wall {i + 1}: {(long - thick / 2) * mpu:.2f} m")
              for i, (_, (_, long, thick, ax)) in enumerate(axes)]
        return Answer(value=round(total, 2), unit="m", confidence=conf, evidence=ev, trace=trace, solver=self.name,
                      assumptions=assume, missing=missing)
