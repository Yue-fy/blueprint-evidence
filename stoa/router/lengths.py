"""Lengths on a Swiss-style floor plan (DXF or its PDF alone), deterministic, no model call (G1, Stoa 12:40: site
engineers ask areas > LENGTHS > counts).

W5's 13:13 upload probe (site1000_bldg2082_floor9385 as plan.pdf, alone and with its DXF): 'How long is the corridor?'
-> 40.41 m² (an area), 'How long is the longest wall?' -> the largest room's perimeter, 'How wide is the corridor?' /
'perimeter of the largest room' -> right-ish numbers with unit None (the table-QA path drops 'm'), 'outer dimensions
of the building' -> vlm_direct 'Not specified'. Those questions came only from the LLM table-QA fallback; this reads
them from the floor model the dxf / pdf_vector solvers already build:

- room length / width: sides of the room outline's minimum rotated rectangle when the room fills >= 90% of it
  (exact for rectangular rooms); a corridor-like strip (L-/U-shaped) -> its centre-line length from area A and
  perimeter P (a strip of length L and width w has A = L*w, P = 2(L + w)); other shapes -> the outline's extent,
  said so in the assumptions.
- room perimeter: the outline's perimeter (net, inside the walls).
- building outer dimensions / length / width: the minimum rotated rectangle around the walls (DXF WALL layer, or the
  PDF's wall outlines) and the rooms.
- 'longest wall' / 'length of the walls': not answered here (wall centre lines are not in this floor model; E1's
  wall axes cover IFC sheets) -> None, so the old path runs.
Evidence: the measured line (Evidence.polyline, D28) on the drawing, plus the room box.
Only for drawings the dxf / pdf_vector readers load (the Swiss layer/drawing style); the dataset has no length
questions on those files, so dataset answers are unchanged.
"""

from __future__ import annotations

import functools
import math
import re
from pathlib import Path

from stoa.contract import Answer, Evidence

_HOW = r"(?:how|wie|quelle|comment|quanto|kuinka|hoe|hur)"
WIDTH_Q = re.compile(r"\bhow (?:wide|narrow)\b|\bwidth\b|\bbreite\b|\bwie breit\b|\blargeur\b|\blarghezza\b|"
                     r"\bleveys\b|\bbreedte\b|\bbredd\b", re.I)
LONG_Q = re.compile(r"\bhow long\b|\blength\b|\bwie lang\b|\bl[äa]nge\b|\blongueur\b|\blunghezza\b|\bpituus\b|"
                    r"\blengte\b|\bl[äa]ngd\b", re.I)
DIMS_Q = re.compile(r"\bdimensions?\b|\babmessung\w*|\bma(?:ß|ss)e\b|\bmitat\b|\bafmeting\w*|\bdimensioni\b|"
                    r"\bhow (?:big|large) is the (?:building|house)\b.*\b(?:m|metres|meters)\b(?!²|2)", re.I)
PERIM_Q = re.compile(r"\bperimeter\b|\bumfang\b|\bp[ée]rim[èe]tre\b|\bperimetro\b|\bpiiri\b|\bomtrek\b|\bomkrets\b",
                     re.I)
BUILDING_Q = re.compile(r"\b(?:building|house|gebäude|haus|bâtiment|edificio|rakennu\w*|gebouw|byggnad\w*)\b|"
                        r"\b(?:outer|outside|overall|external|exterior|total|gross)\s+(?:dimensions?|length|width|size)\b|"
                        r"\bfootprint\b|\baussenma(?:ß|ss)e?\b|\bgrundfläche des gebäudes\b", re.I)
WALL_Q = re.compile(r"\bwalls?\b|\bwand\b|\bwände\b|\bmurs?\b|\bmur[oi]\b|\bseinä\w*|\bväggar?\b|\bmuren?\b", re.I)
AREA_Q = re.compile(r"\barea\b|\bm²|\bm2\b|\bsquare\b|\bfläche\b|\bsurface\b|\bsuperficie\b|\bpinta-?ala\b|"
                    r"\boppervlakte\b|\byta\b|\bhow (?:big|large)\b(?!.*\b(?:long|wide)\b)", re.I)
COUNT_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bwie viele\b|\bcombien\b|\bkuinka monta\b|\bhoeveel\b", re.I)
SUPER_Q = re.compile(r"\b(?P<big>largest|biggest|longest|widest|größte\w*|grösste\w*|plus grand\w*|più grand\w*)\b|"
                     r"\b(?P<small>smallest|shortest|narrowest|kleinste\w*|plus petit\w*|più piccol\w*)\b", re.I)
ROOMNO = re.compile(r"\b(\d{1,2}\.\d{2})\b")
APT = re.compile(r"\b(?:apartment|wohnung|appartement|appartamento|unit|asunto|woning|lägenhet|whg\.?)\s+"
                 r"([A-Z]{1,2}\d?)\b", re.I)
CORRIDOR = ["Korridor", "Gang", "Flur", "Diele", "Couloir", "Corridoio", "Corridor", "Hall", "Halle", "Entrée",
            "Vorplatz", "Disimpegno", "Ingresso", "Eingang"]
CONCEPTS = {
    "corridor": (r"\bcorridor|\bhallway|\bhall\b|\bpassage|\bkorridor|\bflur\b|\bgang\b|\bcouloir|\bcorridoio|"
                 r"\bentr(?:ance|y) hall|\bkäytävä|\bgang\b|\bhal\b", CORRIDOR),
    "kitchen": (r"\bkitchen|\bküche|\bcuisine|\bcucina|\bkeittiö|\bkeuken|\bkök\b", ["Küche", "Cuisine", "Cucina",
                                                                                    "Kochen", "Kitchen"]),
    "bathroom": (r"\bbath(?:room)?|\bwc\b|\btoilet|\bbad\b|\bbadezimmer|\bsalle de bain|\bbagno|\bkylpyhuone|"
                 r"\bbadkamer|\bshower", ["Bad", "Bain", "Bagno", "Badezimmer", "Dusche", "WC", "Douche", "Bath"]),
    "living": (r"\bliving|\blounge|\bwohn|\bséjour|\bsalon|\bsoggiorno|\bolohuone|\bwoonkamer|\bvardagsrum",
               ["Wohnen", "Wohnen/Essen", "Wohnzimmer", "Séjour", "Salon", "Soggiorno", "Living", "Essen"]),
    "bedroom": (r"\bbed ?room|\bschlafzimmer|\bchambre|\bcamera da letto|\bmakuuhuone|\bslaapkamer|\bsovrum|"
                r"\bzimmer\b|\bstanza", ["Zimmer", "Schlafzimmer", "Pièce", "Chambre", "Stanza", "Camera", "Bedroom"]),
    "storage": (r"\bstorage|\bstore ?room|\breduit|\babstell|\bdébarras|\bripostiglio|\bvarasto|\bberging|\bförråd",
                ["Reduit", "Abstellraum", "Réduit", "Ripostiglio", "Keller", "Storage"]),
    "stairs": (r"\bstair|\btreppe|\bescalier|\bscala\b|\bporras|\btrap\b|\btrappa",
               ["Treppenhaus", "Treppe", "Cage d'escalier", "Escalier", "Scala", "Vano scala", "Stairs"]),
    "balcony": (r"\bbalcon|\bbalkon|\bloggia|\bterrace|\bterrasse|\bterrazza|\bparveke|\bbalkong",
                ["Balkon", "Balcon", "Balcone", "Loggia", "Terrasse", "Terrazza", "Sitzplatz"]),
}


OBJ_Q = re.compile(r"\b(?:doors?|windows?|pip(?:e|es|ing)|duct\w*|beams?|columns?|stairs?|stairways?|ramps?|"
                   r"furniture|cables?|slabs?|roofs?|railings?|members?|plates?|footings?|piles?|guid|"
                   r"türen|fenster|treppen?|rohr\w*|leitung\w*|portes?|fenêtres?|escaliers?|tuyau\w*|"
                   r"porte|finestre|scale\b|tubi|deuren|ramen|trappen|leidingen|ovet|ikkunat|portaat|putket)\b", re.I)
WALL_Q2 = re.compile(r"\bwand\w*|\bwände\w*|\bmur(?:s|o|i)?\b|\bseinä\w*|\bvägg\w*|\bmuur\w*", re.I)
BUILDING_DIM = re.compile(
    r"\bhow (?:long|wide|deep|big|large) is the (?:whole |entire )?(?:building|house)\b|"
    r"\b(?:length|width|depth|dimensions?|size|footprint|extent) of the (?:whole |entire )?(?:building|house)\b|"
    r"\b(?:building|house)(?:'s)? (?:length|width|depth|dimensions|footprint|extent)\b|"
    r"\b(?:outer|outside|overall|external|exterior|gross) (?:building )?(?:dimensions?|length|width|size|extent)\b|"
    r"\bfootprint dimensions\b|\bwie (?:lang|breit|tief|gross|groß) ist das (?:ganze )?(?:gebäude|haus)\b|"
    r"\b(?:länge|breite|abmessungen|aussenma(?:ß|ss)e|außenma(?:ß|ss)e) des (?:gebäudes|hauses)\b|"
    r"\baussenma(?:ß|ss)e\b|\baußenma(?:ß|ss)e\b|\b(?:longueur|largeur|dimensions) (?:du|de l'|extérieures du) ?bâtiment\b|"
    r"\b(?:lunghezza|larghezza|dimensioni) dell'edificio\b|\b(?:lengte|breedte|afmetingen) van het gebouw\b|"
    r"\brakennuksen (?:pituus|leveys|mitat|ulkomitat)\b", re.I)


def kind(question: str) -> str | None:
    """'room_len' | 'room_width' | 'room_dims' | 'perimeter' | 'building_len' | 'building_width' | 'building_dims' |
    None (not a length question this reader answers). Lengths of other objects (doors, windows, pipes, walls,
    stairs, ...) are not room outlines -> None."""
    q = question
    if COUNT_Q.search(q) or (AREA_Q.search(q) and not PERIM_Q.search(q)) or OBJ_Q.search(q) or WALL_Q.search(q) \
            or WALL_Q2.search(q) or re.search(r"\baverage\b|\bmean\b|\bdurchschnitt|\bmoyenne\b|\bmedia\b", q, re.I):
        return None
    if PERIM_Q.search(q):
        return None if re.search(r"\b(?:building|house|gebäude|bâtiment|edificio|gebouw)\b", q, re.I) \
            and not ROOMNO.search(q) else "perimeter"
    w, ln, d = bool(WIDTH_Q.search(q)), bool(LONG_Q.search(q)), bool(DIMS_Q.search(q))
    if not (w or ln or d):
        return None
    part = "dims" if d or (w and ln) else ("width" if w else "len")
    if BUILDING_DIM.search(q) and not ROOMNO.search(q) and not _concept(q):
        return f"building_{part}"
    if re.search(r"\b(?:building|house|gebäude|bâtiment|edificio|gebouw|model|modell)\b", q, re.I) and \
            not (_concept(q) or ROOMNO.search(q) or re.search(r"\broom\b|\bzimmer\b|\braum\b", q, re.I)):
        return None  # 'length of X in the building' about something else
    return {"len": "room_len", "width": "room_width", "dims": "room_dims"}[part]


def _concept(q: str) -> str | None:
    for c, (rx, _) in CONCEPTS.items():
        if re.search(rx, q, re.I):
            return c
    return None


def _rect_sides(poly) -> tuple[float, float, list]:
    """(long side, short side, rectangle corner points) of the minimum rotated rectangle."""
    pts = list(poly.minimum_rotated_rectangle.exterior.coords)[:4]
    a = math.dist(pts[0], pts[1])
    b = math.dist(pts[1], pts[2])
    return (a, b, pts) if a >= b else (b, a, [pts[1], pts[2], pts[3], pts[0]])


def room_dims(room, is_corridor: bool) -> tuple[float, float, str, list, list]:
    """(length, width, method, length line, width line) of a room outline, in metres."""
    poly = room.poly
    L, W, pts = _rect_sides(poly)
    fill = poly.area / max(L * W, 1e-9)
    long_line = [list(pts[0]), list(pts[1])]
    wide_line = [list(pts[1]), list(pts[2])]
    if fill >= 0.9:
        return L, W, f"sides of the room's outline (rectangular: fills {100 * fill:.0f}% of its rectangle)", \
            long_line, wide_line
    P, A = poly.length, poly.area
    disc = P * P / 16 - A
    if (is_corridor or L / max(W, 1e-9) >= 2.5) and disc > 0:
        cl = P / 4 + math.sqrt(disc)
        cw = A / cl
        return cl, cw, (f"centre line of the corridor: the outline is L-/U-shaped (fills {100 * fill:.0f}% of its "
                        f"{L:.2f} × {W:.2f} m rectangle); a strip with its area {A:.2f} m² and perimeter {P:.2f} m "
                        f"is {cl:.2f} m long and {cw:.2f} m wide"), long_line, wide_line
    return L, W, (f"extent of the room's outline: it is not rectangular (fills {100 * fill:.0f}% of its "
                  f"{L:.2f} × {W:.2f} m rectangle)"), long_line, wide_line


def _match_rooms(fm, question: str) -> tuple[list, str]:
    """Rooms the question names, and how they were found."""
    rooms = [r for r in fm.rooms if r.number or r.name]
    apt = APT.search(question)
    if apt:
        a = apt.group(1).upper()
        scoped = [r for r in rooms if (r.unit or "").upper() == a]
        if scoped:
            rooms = scoped
    nos = ROOMNO.findall(question)
    if nos:
        hit = [r for r in rooms if r.number in nos]
        return hit, f"room {', '.join(nos)}"
    quoted = re.findall(r"[\"'“„«‘]([^\"'”“»’]{2,40})[\"'”“»’]", question)
    for qn in quoted:
        hit = [r for r in rooms if r.name and r.name.lower() == qn.strip().lower()]
        if hit:
            return hit, f"rooms named '{qn}'"
    c = _concept(question)
    if c:
        names = {n.lower() for n in CONCEPTS[c][1]}
        hit = [r for r in rooms if r.name and (r.name.lower() in names or any(
            r.name.lower().startswith(n) for n in names if len(n) >= 5))]
        return hit, f"{c} (labels {', '.join(CONCEPTS[c][1][:4])} …)"
    if SUPER_Q.search(question) and re.search(r"\broom|\bzimmer|\braum|\bpièce|\bstanza|\bhuone", question, re.I):
        return rooms, "every labelled room"
    return [], ""


@functools.lru_cache(maxsize=64)
def _dxf_walls(dxf_abspath: str):
    """Shapely geometry of the DXF's WALL layer (hatch boundaries / polylines), metres; None if there is none."""
    import ezdxf
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    polys = []
    try:
        doc = ezdxf.readfile(dxf_abspath)
    except Exception:  # noqa: BLE001
        return None
    for e in doc.modelspace().query('*[layer=="WALL"]'):
        try:
            if e.dxftype() == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points()]
                if len(pts) >= 3:
                    polys.append(Polygon(pts).buffer(0))
            elif e.dxftype() == "HATCH":
                for path in e.paths:
                    vs = getattr(path, "vertices", None)
                    if vs and len(vs) >= 3:
                        polys.append(Polygon([(v[0], v[1]) for v in vs]).buffer(0))
        except Exception:  # noqa: BLE001
            continue
    polys = [p for p in polys if not p.is_empty]
    return unary_union(polys) if polys else None


def _pdf_walls(pdf_abspath: str):
    """The Swiss PDF style's wall outlines (black strokes ~1 pt) as metre points, or None."""
    import pymupdf
    from shapely.geometry import MultiPoint

    try:
        with pymupdf.open(pdf_abspath) as d:
            drs = d[0].get_drawings()
    except Exception:  # noqa: BLE001
        return None
    pts = []
    for dr in drs:
        col = tuple(round(x, 2) for x in (dr.get("color") or ()))
        if dr.get("type") == "s" and col == (0.0, 0.0, 0.0) and 0.9 <= (dr.get("width") or 0) <= 1.1:
            r = dr["rect"]
            pts += [(r.x0, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x1, r.y0)]
    return MultiPoint(pts) if pts else None


def answer(path: str, question: str, fm, mapper, src_file: str, tf_inv=None) -> Answer | None:
    """A length answer from a loaded floor model, or None (not a length question this reader covers)."""
    k = kind(question)
    if k is None or fm is None:
        return None
    feet = bool(re.search(r"\b(?:feet|foot|ft)\b", question, re.I))
    if feet:
        return None
    trace = [f"lengths (G1): {k.replace('_', ' ')} read from the floor's outlines ({Path(src_file).suffix[1:]})"]

    def line_ev(line, note, bb=None):
        e = mapper.ev(bb or _bb(line), note)
        tf = mapper.tf
        if tf is not None and e.bbox_space in ("pdf_pt", "image_px"):
            kk = getattr(mapper, "k", 1.0)
            e.polyline = [[round(c * kk, 1) for c in tf.pt(x, y)] for x, y in line]
        elif e.bbox_space == "dxf":
            e.polyline = [[round(x, 3), round(y, 3)] for x, y in line]
        e.exact = True
        return e

    if k.startswith("building"):
        from shapely.geometry import MultiPoint, Point
        from shapely.ops import unary_union

        geoms = [r.poly for r in fm.rooms]
        how = "room outlines"
        walls = None
        if src_file.lower().endswith(".dxf"):
            from stoa.data import abspath

            walls = _dxf_walls(str(abspath(src_file)))
            if walls is not None:
                geoms.append(walls)
                how = "the WALL layer and the room outlines"
        if not geoms:
            return None
        hull = unary_union(geoms).convex_hull if len(geoms) > 1 else geoms[0].convex_hull
        if walls is None:
            pw = None
            try:
                from stoa.data import abspath

                if src_file.lower().endswith(".pdf") and mapper.tf is not None:
                    pw = _pdf_walls(str(abspath(src_file)))
            except Exception:  # noqa: BLE001
                pw = None
            if pw is not None:
                mp = pw
                inv = _inverse(mapper.tf)
                if inv is not None:
                    near = hull.buffer(1.0)  # exterior walls are < 1 m thick; the sheet frame / title block are not
                    pts_m = [inv(g.x, g.y) for g in mp.geoms]
                    quads = [pts_m[i:i + 4] for i in range(0, len(pts_m), 4)]
                    hx0, hy0, hx1, hy1 = hull.bounds
                    span = 1.2 * max(hx1 - hx0, hy1 - hy0)

                    def framing(q4) -> bool:  # a wall: centre near the rooms, not wider than the building
                        xs, ys = [p[0] for p in q4], [p[1] for p in q4]
                        c = Point((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
                        return near.contains(c) and max(max(xs) - min(xs), max(ys) - min(ys)) <= span

                    keep = [p for q4 in quads if framing(q4) for p in q4]
                    if keep:
                        hull = unary_union([hull, MultiPoint(keep).convex_hull]).convex_hull
                        how = "the wall outlines and the room outlines"
        L, W, pts = _plan_axes_box(hull, fm.rooms)
        long_line, wide_line = [list(pts[0]), list(pts[1])], [list(pts[1]), list(pts[2])]
        has_walls = "wall" in how.lower()
        ev = [line_ev(long_line, f"building length {L:.2f} m (outer, {how})"),
              line_ev(wide_line, f"building width {W:.2f} m (outer, {how})")]
        assumptions = [f"outer dimensions = the smallest rectangle around {how} of this floor"]
        missing = [] if has_walls else ["the exterior walls' outer faces (only room outlines were found: add "
                                            "about the wall thickness, ~0.3-0.5 m, on each side)"]
        conf = 0.8 if has_walls else 0.5
        if k == "building_len":
            value, unit, ev = round(L, 2), "m", ev[:1]
        elif k == "building_width":
            value, unit, ev = round(W, 2), "m", ev[1:]
        else:
            value, unit = f"{L:.2f} m × {W:.2f} m", None
        trace.append(f"building: {L:.2f} × {W:.2f} m from {how}")
        return Answer(value=value, unit=unit, confidence=conf, evidence=ev, trace=trace, solver="lengths",
                      assumptions=assumptions, missing=missing)

    hit, how = _match_rooms(fm, question)
    if not hit:
        return None
    sup = SUPER_Q.search(question)
    corridor_names = {n.lower() for n in CORRIDOR}
    rows = []
    for r in hit:
        is_c = bool(r.name and r.name.lower() in corridor_names)
        L, W, method, ll, wl = room_dims(r, is_c)
        rows.append((r, L, W, r.poly.length, method, ll, wl))
    metric = {"perimeter": 3, "room_width": 2}.get(k, 1)
    if sup:
        pick = (max if sup.group("big") else min)(rows, key=lambda t: (t[0].poly.area if re.search(
            r"room|zimmer|raum|pièce|stanza|huone", sup.string[sup.end():sup.end() + 12], re.I) else t[metric]))
        chosen, note = [pick], f"the {sup.group(0).lower()} of {len(rows)} ({how})"
    else:
        chosen, note = rows, how
    r, L, W, P, method, ll, wl = max(chosen, key=lambda t: t[0].poly.area)
    label = f"room {r.number or ''} '{r.name or ''}'".replace("  ", " ")
    assumptions, missing = [], []
    if k == "perimeter":
        value, unit = round(P, 2), "m"
        ring = [list(c) for c in r.poly.exterior.coords]
        ev = [line_ev(ring, f"{label}: perimeter {P:.2f} m (inside face of the walls)", bb=r.bbox)]
        assumptions.append("perimeter = length of the room's outline (inside face of the walls, door openings "
                           "closed)")
    elif k == "room_width":
        value, unit = round(W, 2), "m"
        ev = [line_ev(wl, f"{label}: width {W:.2f} m", bb=None)]
        assumptions.append(method)
    elif k == "room_dims":
        value, unit = f"{L:.2f} m × {W:.2f} m", None
        ev = [line_ev(ll, f"{label}: length {L:.2f} m"), line_ev(wl, f"{label}: width {W:.2f} m")]
        assumptions.append(method)
    else:
        value, unit = round(L, 2), "m"
        ev = [line_ev(ll, f"{label}: length {L:.2f} m")]
        assumptions.append(method)
    ev.append(mapper.ev(r.bbox, f"{label}: {r.area:.2f} m² outline"))
    conf = 0.9 if "rectangular" in method or k == "perimeter" else 0.7
    if len(chosen) > 1:
        others = sorted(chosen, key=lambda t: -t[0].poly.area)
        vals = "; ".join(f"{t[0].number or '?'} {t[0].name or ''}: "
                         + (f"{t[3]:.2f}" if k == "perimeter" else f"{t[2]:.2f}" if k == "room_width" else
                            f"{t[1]:.2f} × {t[2]:.2f}" if k == "room_dims" else f"{t[1]:.2f}") + " m"
                         for t in others[:8])
        assumptions.insert(0, f"the plan has {len(chosen)} rooms matching {note}: {vals}; answered for the largest "
                              f"({label})")
        missing.append("which of them is meant (name the room number or the apartment)")
        conf = min(conf, 0.5)
        for t in others[1:6]:
            ev.append(mapper.ev(t[0].bbox, f"room {t[0].number or ''} '{t[0].name or ''}': "
                                           + (f"{t[3]:.2f}" if k == "perimeter" else
                                              f"{t[2]:.2f}" if k == "room_width" else f"{t[1]:.2f}") + " m"))
    elif sup:
        assumptions.insert(0, f"{label} is {note}")
    if Path(src_file).suffix.lower() == ".pdf":
        assumptions.append("measured on the PDF's vector outlines at its printed scale")
        conf = min(conf, 0.85)
    trace.append(f"{label}: {method}; length {L:.2f} m, width {W:.2f} m, perimeter {P:.2f} m")
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, trace=trace, solver="lengths",
                  assumptions=assumptions, missing=missing)


def _plan_axes_box(geom, rooms) -> tuple[float, float, list]:
    """(long side, short side, corners) of the rectangle around `geom` aligned with the plan's own axes: the
    orientation of the largest rooms' rectangles (a non-rectangular building's minimum-area rectangle tilts: the
    site1000 floor gave 42.81 m along a diagonal for a 40.7 m long building)."""
    from shapely import affinity

    angles = []
    for r in sorted(rooms, key=lambda r: -r.poly.area)[:8]:
        pts = list(r.poly.minimum_rotated_rectangle.exterior.coords)
        if len(pts) >= 2:
            angles.append(math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0])) % 90.0)
    if not angles:
        return _rect_sides(geom)
    angles.sort()
    ang = angles[len(angles) // 2]
    if min(ang, 90.0 - ang) < 1.0:  # an axis-aligned plan: no hairline tilt from rounding
        ang = 0.0
    g = affinity.rotate(geom, -ang, origin=(0, 0))
    x0, y0, x1, y1 = g.bounds
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    back = [affinity.rotate(__import__("shapely.geometry", fromlist=["Point"]).Point(c), ang, origin=(0, 0))
            for c in corners]
    pts = [(b.x, b.y) for b in back]
    a, b = x1 - x0, y1 - y0
    return (a, b, pts) if a >= b else (b, a, [pts[1], pts[2], pts[3], pts[0]])


def _bb(line) -> list[float]:
    xs = [p[0] for p in line]
    ys = [p[1] for p in line]
    return [min(xs), min(ys), max(xs), max(ys)]


def _inverse(tf):
    """pdf_pt -> metres for an affine Transform (pt(x, y) = (a*x + b, c*y + d) style); None if unknown."""
    try:
        x0, y0 = tf.pt(0.0, 0.0)
        x1, _ = tf.pt(1.0, 0.0)
        _, y1 = tf.pt(0.0, 1.0)
        x2, y2 = tf.pt(1.0, 1.0)
        if abs((x2 - x0) - (x1 - x0)) > 1e-6 or abs((y2 - y0) - (y1 - y0)) > 1e-6:
            return None  # rotation / shear: not supported
        sx, sy = x1 - x0, y1 - y0
        if abs(sx) < 1e-9 or abs(sy) < 1e-9:
            return None
        return lambda x, y: ((x - x0) / sx, (y - y0) / sy)
    except Exception:  # noqa: BLE001
        return None


# --- IFC models -------------------------------------------------------------------------------------------------
class _Space:
    """An IfcSpace as a 'room' for room_dims / the matcher: footprint polygon (XY, metres) + names."""

    def __init__(self, e, poly, storey_name):
        self.guid, self.poly, self.storey = e.guid, poly, storey_name
        self.number = None
        self.name = " ".join(x for x in (e.long_name, e.name) if x) or None
        self.area = poly.area
        self.bbox = poly.bounds
        self.unit = None


def _ifc_spaces(idx, question: str) -> tuple[list, str]:
    from shapely.geometry import Polygon

    out = []
    for e in idx.by_class("IfcSpace"):
        fp = e.geo.footprint if e.geo else None
        if not fp or len(fp[0]) < 3:
            continue
        poly = Polygon(fp[0]).buffer(0)
        if poly.is_empty or poly.area < 0.5:
            continue
        st = idx.storey(e.storey) if e.storey else None
        out.append(_Space(e, poly, st.name if st else None))
    q = question.lower()
    storeys = {s.name.lower() for s in idx.storeys if s.name}
    named = [s for s in storeys if re.search(rf"['\"‘“„«]{re.escape(s)}['\"’”»]", q) or
             (len(s) >= 4 and re.search(rf"\b{re.escape(s)}\b", q))]
    if named:
        out = [r for r in out if (r.storey or "").lower() in named]
    quoted = [x.strip().lower() for x in re.findall(r"[\"'“„«‘]([^\"'”“»’]{2,60})[\"'”“»’]", question)]
    for qn in quoted:
        if qn in storeys:
            continue
        hit = [r for r in out if r.name and qn in r.name.lower()]
        if hit:
            return hit, f"spaces named '{qn}'"
    c = _concept(question)
    if c:
        rx = CONCEPTS[c][0]
        hit = [r for r in out if r.name and (re.search(rx, r.name, re.I) or any(
            n.lower() in r.name.lower() for n in CONCEPTS[c][1] if len(n) >= 4))]
        return hit, f"{c} spaces" + (f" on {', '.join(named)}" if named else "")
    if SUPER_Q.search(question) and re.search(r"\broom|\bspace|\bzimmer|\braum|\bpièce|\bruimte", question, re.I):
        return out, "every space" + (f" on {', '.join(named)}" if named else "")
    return [], ""


def wall_kind(question: str) -> str | None:
    """'wall_longest' / 'wall_shortest' for 'How long is the longest wall?' (IFC: wall axis lengths), else None."""
    q = question
    if not (WALL_Q.search(q) or WALL_Q2.search(q)) or COUNT_Q.search(q) or AREA_Q.search(q):
        return None
    sup = SUPER_Q.search(q)
    if not sup or not (LONG_Q.search(q) or re.search(r"\blongest\b|\bshortest\b|\bl[äa]ngste|\bk[üu]rzeste", q, re.I)):
        return None
    return "wall_longest" if sup.group("big") else "wall_shortest"


def _ifc_wall_extreme(path: str, question: str, idx, wk: str) -> Answer | None:
    walls = [e for e in idx.by_class("IfcWall", "IfcWallStandardCase") if e.axis_length]
    if not walls:
        return None
    q = question.lower()
    named = [s for s in idx.storeys if s.name and (re.search(rf"['\"‘“„«]{re.escape(s.name.lower())}['\"’”»]", q)
                                                   or (len(s.name) >= 4 and s.name.lower() in q))]
    if named:
        g = {s.guid for s in named}
        walls = [e for e in walls if e.storey in g] or walls
    pick = (max if wk == "wall_longest" else min)(walls, key=lambda e: e.axis_length)
    st = idx.storey(pick.storey) if pick.storey else None
    return Answer(value=round(pick.axis_length, 2), unit="m", confidence=0.85,
                  evidence=[Evidence(file=path, ifc_guid=pick.guid, exact=True,
                                     note=f"{pick.cls} {pick.name or ''}: axis {pick.axis_length:.2f} m"
                                          + (f" ({st.name})" if st else ""))],
                  trace=[f"lengths (G1): {wk.replace('_', ' ')} of {len(walls)} walls with an axis "
                         f"(IFC 'Axis' representation)"], solver="lengths",
                  assumptions=["wall length = the wall's axis (centre line) length in the model"
                               + (f", on {', '.join(s.name for s in named)}" if named else ", every storey")])


def answer_ifc(path: str, question: str, idx) -> Answer | None:
    """Room length / width / perimeter (IfcSpace footprints) and building outer dimensions (walls' footprints) of an
    IFC model, or None. Evidence = the element GUIDs used (3D)."""
    wk = wall_kind(question)
    if wk and idx is not None:
        return _ifc_wall_extreme(path, question, idx, wk)
    k = kind(question)
    if k is None or idx is None or re.search(r"\b(?:feet|foot|ft)\b", question, re.I):
        return None
    trace = [f"lengths (G1): {k.replace('_', ' ')} read from the IFC model's footprints"]
    if k.startswith("building"):
        from shapely.geometry import MultiPoint, Polygon

        walls = [e for e in idx.by_class("IfcWall", "IfcWallStandardCase", "IfcCurtainWall")
                 if e.geo and e.geo.footprint]
        src = "walls"
        if not walls:
            walls = [e for e in idx.by_class("IfcSlab") if e.geo and e.geo.footprint]
            src = "slabs"
        if not walls:
            return None
        polys = []
        for e in walls:
            try:
                p = Polygon(e.geo.footprint[0]).buffer(0)
                if not p.is_empty:
                    polys.append((e, p))
            except Exception:  # noqa: BLE001
                continue
        if not polys:
            return None
        hull = MultiPoint([c for _, p in polys for c in p.exterior.coords]).convex_hull

        class _R:  # orientation from the longest walls
            def __init__(self, p):
                self.poly = p

        L, W, pts = _plan_axes_box(hull, [_R(p) for _, p in sorted(polys, key=lambda t: -t[1].length)[:8]])
        # the walls that touch the rectangle's sides = the evidence (outer faces)
        edge = [e for e, p in polys if p.exterior.distance(hull.exterior) < 0.05][:12]
        ev = [Evidence(file=path, ifc_guid=e.guid, note=f"{e.cls} {e.name or ''} (outer face)".strip(), exact=True)
              for e in edge]
        assumptions = [f"outer dimensions = the smallest rectangle (along the plan's axes) around all {len(polys)} "
                       f"{src}' footprints, every storey"]
        if k == "building_len":
            value, unit = round(L, 2), "m"
        elif k == "building_width":
            value, unit = round(W, 2), "m"
        else:
            value, unit = f"{L:.2f} m × {W:.2f} m", None
        trace.append(f"building: {L:.2f} × {W:.2f} m around {len(polys)} {src}")
        return Answer(value=value, unit=unit, confidence=0.8 if src == "walls" else 0.6, evidence=ev, trace=trace,
                      solver="lengths", assumptions=assumptions,
                      missing=[] if src == "walls" else ["no walls with geometry: slab edges used"])
    hit, how = _ifc_spaces(idx, question)
    if not hit:
        return None
    sup = SUPER_Q.search(question)
    rows = []
    for r in hit:
        is_c = bool(re.search(CONCEPTS["corridor"][0], r.name or "", re.I))
        L, W, method, _, _ = room_dims(r, is_c)
        rows.append((r, L, W, r.poly.length, method))
    metric = {"perimeter": 3, "room_width": 2}.get(k, 1)
    if sup:
        by_area = re.search(r"room|space|zimmer|raum|pièce|ruimte", sup.string[sup.end():sup.end() + 12], re.I)
        pick = (max if sup.group("big") else min)(rows, key=lambda t: t[0].poly.area if by_area else t[metric])
        chosen = [pick]
    else:
        chosen = rows
    r, L, W, P, method = max(chosen, key=lambda t: t[0].poly.area)
    label = f"space '{r.name}'" + (f" ({r.storey})" if r.storey else "")
    if k == "perimeter":
        value, unit, what = round(P, 2), "m", f"perimeter {P:.2f} m"
        assumptions = ["perimeter = length of the space's footprint outline"]
    elif k == "room_width":
        value, unit, what = round(W, 2), "m", f"width {W:.2f} m"
        assumptions = [method]
    elif k == "room_dims":
        value, unit, what = f"{L:.2f} m × {W:.2f} m", None, f"{L:.2f} × {W:.2f} m"
        assumptions = [method]
    else:
        value, unit, what = round(L, 2), "m", f"length {L:.2f} m"
        assumptions = [method]
    ev = [Evidence(file=path, ifc_guid=r.guid, note=f"{label}: {what}", exact=True)]
    conf = 0.85 if "rectangular" in method or k == "perimeter" else 0.65
    missing = []
    if len(chosen) > 1:
        others = sorted(chosen, key=lambda t: -t[0].poly.area)
        vals = "; ".join(f"{t[0].name} ({t[0].storey}): " + (f"{t[3]:.2f}" if k == "perimeter" else f"{t[2]:.2f}"
                                                              if k == "room_width" else f"{t[1]:.2f}") + " m"
                         for t in others[:8])
        assumptions.insert(0, f"the model has {len(chosen)} {how}: {vals}" + (" …" if len(others) > 8 else "")
                           + f"; answered for the largest ({label})")
        missing.append("which of them is meant (name the space or the storey)")
        conf = min(conf, 0.5)
        ev += [Evidence(file=path, ifc_guid=t[0].guid, note=f"space '{t[0].name}' ({t[0].storey})") for t in others[1:8]]
    trace.append(f"{label}: {method}")
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, trace=trace, solver="lengths",
                  assumptions=assumptions, missing=missing)


# --- CAD sheets: printed dimension strings ------------------------------------------------------------------------
_FTIN = re.compile(r"^(\d{1,4})\s*'\s*-?\s*(\d{1,2})?(?:\s+(\d{1,2})/(\d{1,2}))?\s*\"?$")


def _ft(t: str) -> float | None:
    m = _FTIN.match(t.strip().replace("’", "'").replace("”", '"'))
    if not m:
        return None
    ft, inch = int(m.group(1)), int(m.group(2) or 0)
    frac = int(m.group(3)) / int(m.group(4)) if m.group(3) and m.group(4) and int(m.group(4)) else 0.0
    return ft + (inch + frac) / 12


def answer_dimstrings(path: str, question: str) -> Answer | None:
    """'Outer dimensions / length / width of the building' on a CAD/Revit sheet: the largest horizontal and vertical
    printed dimension strings (overall dimensions are the outermost, longest strings). Accepted only when the
    rectangle can hold the sheet's tagged room areas (Sports Complex A-101 prints only 24'-34' strings next to a
    27,042 SF room -> no answer). Imperial ft-in strings only (metric CAD dimensions are bare numbers)."""
    k = kind(question)
    if k is None or not k.startswith("building"):
        return None
    import pymupdf

    from stoa.data import abspath

    with pymupdf.open(abspath(path)) as d:
        page = d[0]
        hs, vs = [], []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(s["text"] for s in ln["spans"]).strip()
                v = _ft(t) if "'" in t or "’" in t else None
                if v is None or v < 1:
                    continue
                dx, dy = ln.get("dir", (1.0, 0.0))
                (hs if abs(dx) >= 0.9 else vs if abs(dy) >= 0.9 else []).append((v, t, list(ln["bbox"])))
    if not hs or not vs:
        return None
    H, V = max(hs), max(vs)
    L, W = (H, V) if H[0] >= V[0] else (V, H)
    try:
        from stoa.solvers.area_tags import FT2_M2, read_tags

        tags = [t for t in read_tags(str(abspath(path))) if t["name"] or t["number"]]
        tag_ft2 = [t["value"] if t["unit"] == "ft2" else t["value"] / FT2_M2 for t in tags]
    except Exception:  # noqa: BLE001
        tag_ft2 = []
    box = L[0] * W[0]
    if tag_ft2 and (box < max(tag_ft2) or box < 0.6 * sum(tag_ft2)):
        return None  # the largest strings are partial dimensions (room / grid), not the building's
    conf = 0.65 if tag_ft2 else 0.45
    m = 0.3048
    want_ft = bool(re.search(r"\b(?:feet|foot|ft)\b", question, re.I)) or not re.search(r"\bmet(?:re|er)s?\b|\bm\b",
                                                                                         question, re.I)
    ev = [Evidence(file=path, page=0, bbox=[round(x, 1) for x in L[2]], bbox_space="pdf_pt", exact=True,
                   note=f"overall dimension {L[1]} = {L[0] * m:.2f} m"),
          Evidence(file=path, page=0, bbox=[round(x, 1) for x in W[2]], bbox_space="pdf_pt", exact=True,
                   note=f"overall dimension {W[1]} = {W[0] * m:.2f} m")]
    if k == "building_dims":
        value, unit = f"{L[1]} × {W[1]} ({L[0] * m:.2f} m × {W[0] * m:.2f} m)", None
    else:
        pick = L if k == "building_len" else W
        ev = ev[:1] if k == "building_len" else ev[1:]
        value, unit = (round(pick[0], 2), "ft") if want_ft else (round(pick[0] * m, 2), "m")
    assumptions = [f"overall dimensions = the longest printed dimension strings across ({H[1]}) and up/down ({V[1]}) "
                   f"the sheet"]
    if tag_ft2:
        assumptions.append(f"consistent with the sheet's {len(tag_ft2)} room area tags ({sum(tag_ft2):.0f} ft² fit in "
                           f"{box:.0f} ft²)")
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver="lengths",
                  trace=[f"lengths (G1): {len(hs)} horizontal + {len(vs)} vertical dimension strings; longest "
                         f"{H[1]} / {V[1]}"], assumptions=assumptions,
                  missing=["a dimension string drawn to a grid line or a wing, not the outer face, would change it"])
