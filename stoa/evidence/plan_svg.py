"""Answers from a LONE IfcConvert plan SVG (an .svg uploaded without its .ifc or PDF) — E1, captain 12:52 (all formats).

An IfcConvert plan SVG is the model's storey drawn on paper: one `<g class="[cut ]IfcDoor" data-guid=… data-name=…>`
per element, its outline as paths in paper millimetres (the viewBox units), the room name/number as <text>, and the
sheet scale ('1:100' / '1:200') printed as text. So, without the model:
- room areas = the IfcSpace outline's area on paper × N² (same as the PDF reader, stoa.evidence.ifcplan_pdf);
- counts of doors / windows / walls / columns / stairs / spaces on this sheet = distinct GUIDs of that class (exact);
- the total length of the walls on this sheet = the sum of each wall's centre line (stoa.evidence.shapes.wall_axis).
Evidence is in bbox_space "svg" (D29: the file's own user units, y down), exact=True, rooms with their polygon,
walls with their centre line.

    from stoa.evidence.plan_svg import answer
    answer(svg_path, question)   # Answer, or None when this is not such an SVG / not a question it reads
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from stoa.contract import Answer, Evidence
from stoa.data import abspath

DEFAULT_SCALE = 100
_SVG = "{http://www.w3.org/2000/svg}"
_CLS = re.compile(r"\b(Ifc\w+)\b")
# 'in the model X' / 'bevat het model' / 'enthält das Modell' / 'w modelu' / 'mallissa' / 'the building': a whole-model
# question, which one storey's sheet cannot answer for sure
MODEL_SCOPE = re.compile(r"\b(?:model\w*|modell\w*|modèle|malli\w*|building|gebäude\w*|gebouw\w*|budyn\w*|"
                         r"rakennu\w*|bâtiment)\b", re.I)


@functools.lru_cache(maxsize=32)
def read_svg(svg_abs: str) -> dict | None:
    """{'scale', 'scale_printed', 'elements': [{guid, cls, name, rings, points, bbox}], 'texts': [(s, x, y)]}."""
    import xml.etree.ElementTree as ET

    from stoa.solvers.ifc.plan2d import _path_points

    try:
        root = ET.parse(svg_abs).getroot()
    except Exception:  # noqa: BLE001
        return None
    texts = []
    for t in root.iter(f"{_SVG}text"):
        s = "".join(t.itertext()).strip()
        try:
            x, y = float(t.get("x", "nan")), float(t.get("y", "nan"))
        except ValueError:
            continue
        if s:
            texts.append((s, x, y))
    elements = []
    for g in root.iter(f"{_SVG}g"):
        guid = g.get("data-guid")
        m = _CLS.search(g.get("class") or "")
        if not guid or not m:
            continue
        rings = []
        for p in g.iter():
            tag = p.tag.replace(_SVG, "")
            try:
                if tag == "path":
                    rings += [r for r in _path_points(p.get("d", "")) if len(r) >= 2]
                elif tag == "circle" or tag == "ellipse":  # columns are drawn as circles
                    cx, cy = float(p.get("cx", 0)), float(p.get("cy", 0))
                    rx = float(p.get("r") or p.get("rx") or 0)
                    ry = float(p.get("r") or p.get("ry") or 0)
                    rings.append([(cx + rx * c, cy + ry * s_) for c, s_ in
                                  ((1, 0), (0.7071, 0.7071), (0, 1), (-0.7071, 0.7071), (-1, 0), (-0.7071, -0.7071),
                                   (0, -1), (0.7071, -0.7071))])
                elif tag == "rect":
                    x, y = float(p.get("x", 0)), float(p.get("y", 0))
                    w, h = float(p.get("width", 0)), float(p.get("height", 0))
                    rings.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
                elif tag in ("polygon", "polyline"):
                    v = [float(t) for t in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", p.get("points", ""))]
                    pts_ = list(zip(v[0::2], v[1::2]))
                    if len(pts_) >= 2:
                        rings.append(pts_)
                elif tag == "line":
                    rings.append([(float(p.get("x1", 0)), float(p.get("y1", 0))),
                                  (float(p.get("x2", 0)), float(p.get("y2", 0)))])
            except (TypeError, ValueError):
                continue
        pts = [pt for r in rings for pt in r]
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        elements.append({"guid": guid, "cls": m.group(1), "name": g.get("data-name") or "", "rings": rings,
                         "points": pts, "bbox": [min(xs), min(ys), max(xs), max(ys)]})
    if not elements:
        return None
    sm = re.search(r"\b1\s*:\s*(\d{2,4})\b", " ".join(s for s, _, _ in texts))
    return {"scale": int(sm.group(1)) if sm else DEFAULT_SCALE, "scale_printed": bool(sm), "elements": elements,
            "texts": texts}


def _spaces(svg: dict) -> list[dict]:
    """IfcSpace outlines with their text labels and m² (stacked near-identical copies kept: callers dedupe)."""
    from shapely.geometry import Point, Polygon

    out = []
    for e in svg["elements"]:
        if e["cls"] != "IfcSpace":
            continue
        best = None
        for r in e["rings"]:
            if len(r) < 3:
                continue
            P = Polygon(r)
            P = P if P.is_valid else P.buffer(0)
            if not P.is_empty and (best is None or P.area > best[0].area):
                best = (P, r)
        if best is not None:
            P = best[0]
            for r in e["rings"]:  # inner rings = holes (a shaft, a column inside the room)
                if r is best[1] or len(r) < 3:
                    continue
                H = Polygon(r)
                H = H if H.is_valid else H.buffer(0)
                if not H.is_empty and H.area < P.area and P.contains(H.representative_point()) and \
                        H.within(P.buffer(1e-6)):
                    P = P.difference(H)
            out.append({**e, "poly": P, "ring": best[1], "labels": []})
    for s, x, y in svg["texts"]:
        c = Point(x, y)
        cont = [sp for sp in out if sp["poly"].contains(c)]
        if cont:
            min(cont, key=lambda sp: sp["poly"].centroid.distance(c))["labels"].append(s)
    k = (svg["scale"] / 1000.0) ** 2  # paper mm² → model m²
    for sp in out:
        sp["label"] = " ".join(sp["labels"]) or sp["name"]
        sp["m2"] = round(sp["poly"].area * k, 2)
    return out


def _scale_line(svg: dict) -> str:
    n = svg["scale"]
    if svg["scale_printed"]:
        return (f"scale: 1:{n} printed on the sheet; measured on the SVG's own outlines (paper mm × {n}) — exact if the "
                "printed scale is right")
    return f"scale: NOT printed — 1:{n} assumed (IfcConvert plan default); approximate until the scale is confirmed"


def _ev(path: str, e: dict, note: str, polygon=None, polyline=None) -> Evidence:
    return Evidence(file=path, page=None, bbox=[round(v, 3) for v in e["bbox"]], bbox_space="svg", note=note,
                    exact=True, polygon=polygon, polyline=polyline)


def answer(path: str, question: str) -> Answer | None:
    from stoa.evidence.ifcplan_pdf import TOTAL_Q, WALLISH, _tokens

    if Path(str(path)).suffix.lower() != ".svg":
        return None
    svg = read_svg(str(abspath(str(path))))
    if not svg:
        return None
    q = question
    scope_model = bool(MODEL_SCOPE.search(q))
    conf_k = 0.25 if scope_model else 1.0  # one sheet answers for one storey, not the whole model (dev: 0/22 right)
    scope_note = (["scope: this sheet (one storey) only — the model may have more on other storeys"]
                  if scope_model else [])
    base_trace = [f"plan_svg: {len(svg['elements'])} IFC elements drawn on the SVG; scale 1:{svg['scale']}"
                  f"{' (printed)' if svg['scale_printed'] else ' (assumed)'}"]
    from stoa.evidence.check import expected_kinds
    from stoa.evidence.groundtruth import ifc_kind
    from stoa.router.intent import parse

    it = parse(q)
    kinds = expected_kinds(q) or set()
    scale_c = 1.0 if svg["scale_printed"] else 0.7
    # ---- counts: distinct GUIDs of the asked classes
    if it.task == "count" or re.match(r"\s*(how many|wie viele|hoeveel|ile|kuinka monta|combien|quant[ie])", q, re.I):
        if not kinds:
            return None
        hits = [e for e in svg["elements"] if ifc_kind(e["cls"]) in kinds]
        seen, uniq = set(), []
        for e in hits:
            if e["guid"] not in seen:
                seen.add(e["guid"])
                uniq.append(e)
        cls = sorted({e["cls"] for e in uniq}) or sorted(kinds)
        none_note = [] if uniq else [f"none of {', '.join(sorted(kinds))} is drawn on this sheet as its own IFC "
                                     "element (it may be drawn as part of another element, or be on another storey)"]
        ev = [_ev(path, e, f"{e['cls']} {e['name'] or e['guid']}") for e in uniq[:200]]
        return Answer(value=len(uniq), unit="count", confidence=round((0.9 if uniq else 0.3) * conf_k, 2), evidence=ev,
                      solver="plan_svg",
                      trace=base_trace + [f"counted {len(uniq)} distinct GUIDs of {', '.join(cls)} on this sheet"],
                      assumptions=[f"one drawn IFC element per GUID ({', '.join(sorted(kinds))}) on this sheet",
                                   *scope_note, *none_note],
                      missing=["the IFC model, for elements on other storeys"] if scope_model else [])
    # ---- total wall length
    if it.task == "length" and WALLISH.search(q) and TOTAL_Q.search(q):
        from stoa.evidence.shapes import wall_axis

        walls, total = [], 0.0
        for e in svg["elements"]:
            if ifc_kind(e["cls"]) != "wall":
                continue
            ax = wall_axis(e["points"])
            if not ax:
                continue
            L = ((ax[0][0] - ax[1][0]) ** 2 + (ax[0][1] - ax[1][1]) ** 2) ** 0.5 * svg["scale"] / 1000.0
            total += L
            walls.append(_ev(path, e, f"{e['cls']} {e['name'] or e['guid']}: {L:.2f} m", polyline=ax))
        if not walls:
            return None
        return Answer(value=round(total, 2), unit="m", confidence=round(0.55 * scale_c * conf_k, 2), evidence=walls,
                      solver="plan_svg",
                      trace=base_trace + [f"sum of {len(walls)} wall centre lines = {total:.2f} m"],
                      assumptions=[_scale_line(svg), "wall length = the long axis of each wall's drawn outline "
                                   "(centre line; corners overlap by a wall thickness)", *scope_note],
                      missing=["the IFC model's wall quantities (Qto) would make the length exact"])
    if (it.task != "area" and not re.search(r"m²|m2|\barea|fläche|oppervlak|powierzch|pinta-?ala|surface", q, re.I)) \
            or WALLISH.search(q):
        return None
    spaces = _spaces(svg)
    if not spaces:
        return None
    if TOTAL_Q.search(q):
        kept: list[dict] = []
        for s in spaces:
            if any(s["poly"].intersection(t["poly"]).area >= 0.95 * max(s["poly"].area, t["poly"].area) for t in kept):
                continue
            kept.append(s)
        total = round(sum(s["m2"] for s in kept), 2)
        ev = [_ev(path, s, f"space '{s['label']}': {s['m2']:.2f} m²", polygon=[[round(x, 3), round(y, 3)]
                                                                            for x, y in s["ring"]]) for s in kept[:120]]
        return Answer(value=total, unit="m2", confidence=round(0.6 * scale_c * conf_k, 2), evidence=ev,
                      solver="plan_svg",
                      trace=base_trace + [f"total of {len(kept)} IfcSpace outlines ({len(spaces) - len(kept)} stacked "
                                          f"duplicates dropped) = {total} m²"],
                      assumptions=[_scale_line(svg), "total = sum of every space outline on this sheet", *scope_note],
                      missing=[])
    qt = _tokens(q)
    quoted = {t.lower() for t in re.findall(r"['\"‘’“”„«»]([^'\"‘’“”„«»]{1,40})['\"‘’“”„«»]", q)}

    def score(s) -> float:
        lt = _tokens(s["label"]) | _tokens(s["name"])
        sc = sum(3.0 if re.search(r"\d", t) else 1.0 for t in lt & qt)
        if s["label"].lower() in quoted or s["name"].lower() in quoted:
            sc += 5.0
        return sc

    ranked = sorted(spaces, key=score, reverse=True)
    best = ranked[0]
    if score(best) <= 0 or (len(ranked) > 1 and score(ranked[1]) == score(best)
                            and abs(ranked[1]["m2"] - best["m2"]) > 0.02 * best["m2"]):
        return None
    poly = [[round(x, 3), round(y, 3)] for x, y in best["ring"]]
    # stacked spaces (gymzaal: three outlines on top of each other per room) make the label → outline match a guess
    stacked = [s for s in spaces if s is not best and abs(s["m2"] - best["m2"]) > 0.02 * best["m2"]
               and s["poly"].intersection(best["poly"]).area >= 0.5 * min(s["poly"].area, best["poly"].area)]
    conf = 0.85 * scale_c * (0.5 if stacked else 1.0)
    return Answer(value=best["m2"], unit="m2", confidence=round(conf, 2), solver="plan_svg",
                  evidence=[_ev(path, best, f"space '{best['label']}': {best['m2']:.2f} m²", polygon=poly)],
                  trace=base_trace + [f"matched space '{best['label']}' ({best['name']}) → {best['m2']} m²"],
                  assumptions=[_scale_line(svg), "net area = the drawn IfcSpace outline (the model's space boundary)",
                               *([f"{len(stacked)} other space outline(s) overlap this one (stacked spaces): the label "
                                  "may belong to one of them"] if stacked else [])],
                  missing=["which of the overlapping space outlines carries this label"] if stacked else [])
