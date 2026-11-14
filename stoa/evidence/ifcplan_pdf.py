"""Room areas from a LONE IFC-plan PDF (an IfcConvert render uploaded without its .ifc / .svg) — E1.

IfcConvert draws every IfcSpace as a vector path filled #e3edf7 (stroke #8aa0b8) with the space's name/number as
text inside, at a fixed 1:100 (11 of 12 dataset models; one at 1:200). So a room's area is its polygon's area on
paper at the drawing scale: KORIDOR 119 on esplanades plan-01 → 63.82 m² vs 63.84 in the model.

    from stoa.evidence.ifcplan_pdf import space_polygons, room_area_answer
    space_polygons(pdf)            # [{'label', 'polygon' (pdf_pt), 'bbox', 'm2', 'scale'}]  ([] if not such a sheet)
    room_area_answer(pdf, question)  # Answer with the room outline as evidence, or None

No model call. The scale is read from a printed '1:N' when there is one, else 1:100 is assumed and said so.
"""

from __future__ import annotations

import functools
import re

from stoa.contract import Answer, Evidence
from stoa.data import abspath

SPACE_FILL = (0.89, 0.929, 0.969)  # #e3edf7
DEFAULT_SCALE = 100


def _is_space_fill(f) -> bool:
    return bool(f) and len(f) == 3 and all(abs(a - b) < 0.01 for a, b in zip(f, SPACE_FILL))


def _drawing_points(d) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for it in d.get("items", []):
        op = it[0]
        if op == "l":
            pts += [(it[1].x, it[1].y), (it[2].x, it[2].y)]
        elif op == "c":
            pts += [(it[1].x, it[1].y), (it[4].x, it[4].y)]
        elif op == "re":
            r = it[1]
            pts += [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
        elif op == "qu":
            q = it[1]
            pts += [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


ORPHAN_LABEL_PT = 12.0  # ~4 mm on paper


@functools.lru_cache(maxsize=64)
def _spaces(pdf_abs: str) -> tuple:
    import pymupdf
    from shapely.geometry import Point, Polygon

    with pymupdf.open(pdf_abs) as doc:
        if len(doc) == 0:
            return ()
        page = doc[0]
        drawings = [d for d in page.get_drawings() if _is_space_fill(d.get("fill"))]
        if not drawings:
            return ()
        text = page.get_text("text")
        words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, wno
    m = re.search(r"\b1\s*:\s*(\d{2,4})\b", text)
    scale = int(m.group(1)) if m else DEFAULT_SCALE
    pt_per_m = 72 / 0.0254 / scale
    lines: dict[tuple, list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)
    tlines = []
    for ws in lines.values():
        ws.sort(key=lambda w: w[0])
        tlines.append((" ".join(w[4] for w in ws), min(w[0] for w in ws), min(w[1] for w in ws),
                       max(w[2] for w in ws), max(w[3] for w in ws)))
    polys = []
    for d in drawings:
        pts = _drawing_points(d)
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 1:
            continue
        polys.append((poly, pts))
    # each text line belongs to ONE space: of the outlines containing it, the one whose centre is nearest
    # (overlapping spaces, e.g. three gymzaal outlines on top of each other, would otherwise share every label)
    owner: dict[int, list] = {}
    orphans = []
    for t in tlines:
        c = Point((t[1] + t[3]) / 2, (t[2] + t[4]) / 2)
        cont = [i for i, (pl, _) in enumerate(polys) if pl.contains(c)]
        if cont:
            i = min(cont, key=lambda k: polys[k][0].centroid.distance(c))
            owner.setdefault(i, []).append(t)
        else:
            orphans.append((t, c))
    # a tiny space's label does not fit inside it and is printed next to it (H 14:17: 'Storage 414B-19-001-009',
    # 0.52 m² at 1:200): a line outside every outline goes to the nearest outline WITHOUT a label of its own, if close
    for t, c in orphans:
        near = [(polys[i][0].distance(c), i) for i in range(len(polys)) if i not in owner or owner[i][0][5:] == ("near",)]
        near = [(d, i) for d, i in near if d <= ORPHAN_LABEL_PT]
        if near:
            d, i = min(near)
            owner.setdefault(i, []).append((*t, "near"))
    out = []
    for i, (poly, pts) in enumerate(polys):
        inside = sorted(owner.get(i, []), key=lambda t: (t[2], t[1]))
        ring = list(poly.exterior.coords) if hasattr(poly, "exterior") else pts
        out.append({"label": " ".join(t[0] for t in inside), "polygon": [[round(x, 2), round(y, 2)] for x, y in ring],
                    "bbox": [round(v, 2) for v in poly.bounds], "m2": round(poly.area / pt_per_m ** 2, 2),
                    "scale": scale, "scale_printed": bool(m)})
    return tuple(out)


def space_polygons(pdf_path: str) -> list[dict]:
    try:
        return [dict(s) for s in _spaces(str(abspath(pdf_path)))]
    except Exception:  # noqa: BLE001
        return []


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[\w./-]+", s or "") if len(t) >= 2}


def room_area_answer(pdf_path: str, question: str) -> Answer | None:
    """'What is the area of <room>?' on a lone IFC-plan PDF: the space whose label shares the most specific tokens
    with the question (a number like '119' or '3-1' outweighs a name). None when no space matches clearly."""
    spaces = space_polygons(pdf_path)
    if not spaces:
        return None
    qt = _tokens(question)
    quoted = {t.lower() for t in re.findall(r"['\"‘’“”„«»]([^'\"‘’“”„«»]{1,40})['\"‘’“”„«»]", question)}

    def score(s) -> float:
        lt = _tokens(s["label"])
        sc = sum(3.0 if re.search(r"\d", t) else 1.0 for t in lt & qt)
        if s["label"].lower() in quoted:
            sc += 5.0
        return sc

    ranked = sorted(spaces, key=score, reverse=True)
    best = ranked[0]
    if score(best) <= 0 or (len(ranked) > 1 and score(ranked[1]) == score(best)):
        return None
    sc = f"1:{best['scale']}" + (" (printed on the sheet)" if best["scale_printed"] else
                                  " (assumed: IfcConvert plan sheets are drawn at 1:100; no scale printed)")
    ev = Evidence(file=pdf_path, page=0, bbox=best["bbox"], bbox_space="pdf_pt", polygon=best["polygon"], exact=True,
                  note=f"space '{best['label']}': {best['m2']:.2f} m² = outline area on the sheet at {sc.split(' (')[0]}")
    return Answer(value=best["m2"], unit="m2", confidence=0.8 if best["scale_printed"] else 0.65, evidence=[ev],
                  solver="ifcplan_pdf",
                  trace=[f"ifcplan_pdf: {len(spaces)} IfcSpace outlines (fill #e3edf7) on the sheet; matched "
                         f"'{best['label']}' → {best['m2']} m² at {sc}"],
                  assumptions=[f"scale {sc}", "net area = the drawn IfcSpace outline (as the model's space boundary)"],
                  missing=[] if best["scale_printed"] else ["a printed scale or the IFC model to confirm 1:100"])


TOTAL_Q = re.compile(r"\b(?:total|all (?:the )?(?:rooms|spaces)|sum|gesamt\w*|alle[nr]?\b|totale?|łączn\w*|wszystkich|"
                     r"yhteensä|kaikki\w*|samman\w*|somma|tous|toutes|todas?|todos)\b", re.I)
WALLISH = re.compile(r"\b(?:wall|walls|wand|wände|wanden|muur|mur|murs|ściany|seinä\w*|side area|zijoppervlakte)\b", re.I)


def total_area_answer(pdf_path: str, question: str) -> Answer | None:
    """'Total net floor area of all rooms (spaces) on this sheet': the sum of EVERY space outline. Stacked outlines
    with the same footprint are separate IfcSpaces (Gymzaal '00 Begane grond': 48 outlines, 20 stacked, sum 1401.45
    vs the model's 1401.76 m²; counting them once gave 903.93): on the 72 dataset total-area questions on IFC sheets,
    all outlines are right on 55, the deduplicated sum on 53 and never better. None for wall/side areas or when the
    question is not a total."""
    if not TOTAL_Q.search(question) or WALLISH.search(question):
        return None
    spaces = space_polygons(pdf_path)
    if not spaces:
        return None
    kept = [(None, s) for s in spaces]
    stacked = len(spaces) - len(distinct_spaces(pdf_path))
    total = round(sum(s["m2"] for _, s in kept), 2)
    sc = spaces[0]
    scale = f"1:{sc['scale']}" + (" (printed on the sheet)" if sc["scale_printed"] else " (assumed)")
    ev = [Evidence(file=pdf_path, page=0, bbox=s["bbox"], bbox_space="pdf_pt", polygon=s["polygon"], exact=True,
                   note=f"space '{s['label'] or '?'}': {s['m2']:.2f} m²") for _, s in kept[:60]]
    return Answer(value=total, unit="m2", confidence=0.6 if sc["scale_printed"] else 0.45, evidence=ev,  # dev 6/8
                  solver="ifcplan_pdf",
                  trace=[f"ifcplan_pdf: total of {len(kept)} IfcSpace outlines ({stacked} of them stacked on another "
                         f"with the same footprint, counted: separate spaces) at {scale} = {total} m²"],
                  assumptions=[f"scale {scale}", "total = sum of every space outline drawn on this sheet"],
                  missing=[] if sc["scale_printed"] else ["a printed scale or the IFC model to confirm 1:100"])


ROOMISH = re.compile(r"\b(?:rooms?|spaces?|räume?|raum|zimmer|huone\w*|tilo\w*|ruimtes?|vertrek\w*|lokal\w*|pi[eè]ces?|"
                     r"stanz[ae]|locali|pomieszcze\w*|rum)\b", re.I)
NOT_ROOMS = re.compile(r"\b(?:doors?|windows?|walls?|türen?|fenster|wände|wand|ovi\w*|ikkun\w*|seinä\w*|deuren|ramen|"
                       r"muren|portes?|fenêtres?|murs?|stairs?|columns?|slabs?)\b", re.I)


def distinct_spaces(pdf_path: str) -> list[dict]:
    """The sheet's space outlines with near-identical stacked copies counted once."""
    from shapely.geometry import Polygon

    kept: list[tuple] = []
    for s in space_polygons(pdf_path):
        p = Polygon(s["polygon"]).buffer(0)
        if any(p.intersection(q).area >= 0.95 * max(p.area, q.area) for q, _ in kept):
            continue
        kept.append((p, s))
    return [s for _, s in kept]


def crosscheck_value(pdf_path: str, question: str, unit: str) -> tuple[float, str] | None:
    """An independent reading of the same quantity from the sheet's IfcSpace outlines, for checking another reader
    (G1's plan_rooms): (value, how) or None when this sheet / question is not one we can read."""
    unit = (unit or "").lower()
    if unit == "count":
        if not ROOMISH.search(question) or NOT_ROOMS.search(question):
            return None
        sp = space_polygons(pdf_path)
        if not sp:
            return None
        return float(len(sp)), f"{len(sp)} IfcSpace outlines (fill #e3edf7) on the sheet"
    if unit in ("m2", "m²"):
        a = total_area_answer(pdf_path, question) or (None if TOTAL_Q.search(question) else
                                                       room_area_answer(pdf_path, question))
        if a is None or a.value is None:
            return None
        return float(a.value), (a.trace[0] if a.trace else "ifcplan_pdf")
    return None


DISAGREE_CAP = 0.4  # lone probe d4: when the two readings disagree plan_rooms is right on 4 of 17


def check_plan_rooms(ans: Answer, path: str, question: str) -> Answer:
    """Second, independent reading of a plan_rooms answer on an IfcConvert-style sheet (G1 13:03 + S0 13:11).

    Agree (areas within 2 %, counts exact) → '✓' in trace + a 'checked:' assumption, confidence unchanged (both read
    the same scale, so agreement says nothing about the scale). Disagree → confidence capped at DISAGREE_CAP and the
    other value named in `missing`. No change on any other solver, sheet style or question kind."""
    if ans.value is None or (ans.solver or "").split("/")[-1] != "plan_rooms" or not str(path).lower().endswith(".pdf"):
        return ans
    try:
        ours = crosscheck_value(str(path), question, ans.unit or "")
        v = float(ans.value)
    except Exception:  # noqa: BLE001
        return ans
    if ours is None:
        return ans
    o, how = ours
    count = (ans.unit or "").lower() == "count"
    agree = v == o if count else abs(v - o) <= 0.02 * max(abs(o), 1e-9)
    shown = f"{o:g}" if count else f"{o:.2f} m²"
    if agree:
        ans.trace = [*ans.trace, f"cross-check ✓ ifcplan_pdf: {how} → {shown}"]
        ans.assumptions = [*ans.assumptions, f"checked: a second reading of the sheet's IfcSpace outlines gives the "
                                              f"same {shown}"]
    else:
        ans.trace = [*ans.trace, f"cross-check ✗ ifcplan_pdf: {how} → {shown} (plan_rooms {ans.value}); confidence "
                                 f"capped at {DISAGREE_CAP}"]
        ans.confidence = min(ans.confidence or 0.0, DISAGREE_CAP)
        ans.missing = [*ans.missing, f"a second reading of the sheet's IfcSpace outlines gives {shown}, not "
                                     f"{ans.value}: the IFC model (or a look at the outlined rooms) would settle it"]
    return ans
