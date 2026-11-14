"""Door and window counts that generalise (DR, captain 18:27; Stoa kind #3 'counts of doors and windows').

Order of evidence (role prompt DR):
  (b) vector geometry on PDFs: door SWINGS = a circular arc of a door-leaf radius sweeping ~90 degrees, drawn as a
      Bezier, as a polyline of short segments, or EXPLODED into one-segment paths (USACE A-101F); double doors (two
      leaves whose closed positions meet on one doorway) = 1 door with 2 leaves; a double-acting leaf (two arcs on one
      hinge) = 1 leaf; full circles (grid bubbles = 4 quarter arcs on one centre) and rounded box corners (the arc runs
      on tangentially at both ends) are not doors; exact duplicates are dropped.
  (c) V1's door/window detector (Faster R-CNN v2, CPU service $STOA_DET_URL, default :8140) on a render of the plan
      viewport(s) at ~50 px per metre: windows on PDFs, and doors + windows on images.
Scope (KNOWLEDGE DECISION 18:21 item 5): plan viewports only (RCP / axon / elevation / section / detail views of the
same sheet are excluded); a viewport named by the question ('module 02', 'the filter building') = only that one.
Convention: a double door = 1 door (2 leaves) -> 'N doors (M leaves)'. Evidence: one box per counted door / window.

Door tags / schedules (evidence order (a)) are read by label_count (G1) earlier in the pipeline; this solver reports
when its swing count agrees with them.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import time
from pathlib import Path

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

PT_M = 0.0254 / 72  # metres per PDF point at 1:1
DOOR_R_M = (0.55, 1.60)  # one leaf, metres (US 2'-0" .. 5'-0", EU 0.6 .. 1.5; Swiss entrance leaves up to 1.5 m)
SWEEP = (55.0, 115.0)  # degrees
WIN_SCORE = 0.7  # detector score for a window box

_COUNT_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b|\btotal\b|\bwie viele\b|\bmontako\b|\bkuinka monta\b", re.I)
_DOOR = re.compile(r"\bdoors?\b|\bdoorways?\b|\bdoorsets?\b|\bovi[ae]?\b|\bovien\b|\bt[üu]ren?\b", re.I)
_WINDOW = re.compile(r"\bwindows?\b|\bikkun(?:a|aa|oita|ojen)\b|\bfenster\b", re.I)
_DOUBLE = re.compile(r"\bdouble[- ]?(?:leaf\s+)?doors?\b|\bpairs? of doors\b|\bdouble[- ]leaf\b|\bdouble\b", re.I)
_SINGLE = re.compile(r"\bsingle[- ]?(?:leaf\s+)?doors?\b|\bsingle\b", re.I)
_LEAVES = re.compile(r"\bleaves\b|\bleafs\b|\bdoor panels?\b", re.I)

# view titles (CAD sheet conventions): kind by keyword, first match wins
_VIEW_KIND = [
    ("rcp", re.compile(r"\bRCP\b|REFLECTED|CEILING", re.I)),
    ("axon", re.compile(r"\bAXON|ISOMETRIC|\bISO\b|PERSPECTIVE|3D VIEW", re.I)),
    ("elevation", re.compile(r"ELEVATION", re.I)),
    ("section", re.compile(r"\bSECTION", re.I)),
    ("detail", re.compile(r"\bDETAIL", re.I)),
    ("schedule", re.compile(r"SCHEDULE|LEGEND", re.I)),
    ("other_plan", re.compile(r"ROOF PLAN|FOUNDATION|FRAMING|SLAB|SITE PLAN|KEY PLAN|LOCATION PLAN|DEMOLITION", re.I)),
    ("plan", re.compile(r"\bPLAN\b|\bLAYOUT\b|\bLEVEL\s+\d|\bGROUND FLOOR\b|\bFIRST FLOOR\b|\bSECOND FLOOR\b", re.I)),
]
# note headers / sheet-index lines that mention a plan but title no view ('GENERAL NOTES - STANDARD DESIGN PLAN:')
# a sentence ('... SEE FOUNDATION PLAN FOR FOUNDATION DIMENSIONS.') is a note: on X1's A-101F render it was taken as a
# view title one row above the floor plan's own, which squeezed the plan's cell to a band and dropped all 6 doors
_NOT_TITLE = re.compile(r"\bNOTES?\b|\bGENERAL\b|:\s*$|(?<!AXON)(?<!PLAN)(?<!RCP)(?<!ELEVATION)(?<!SECTION)\.\s*$|"
                        r"\bINDEX\b|\bLIST OF\b|\bSHEET\b|\bDIMENSIONS?\b", re.I)
_STOP = set("""how many much number count total of the a an are is there in on at to for this that these those does do
have has with within inside drawing drawings sheet plan plans shown show drawn its it whole entire all every building
which what door doors window windows double single leaf leaves doorway doorways doorset doorsets pair pairs swing swings
opening openings please give me tell floor including include included only exactly and or by per type types sheet's
plan's level arc arcs symbol symbols together altogether combined overall""".split())


# ---------------------------------------------------------------------------------------------------------------------
# question
_BOTH_Q = re.compile(r"\b(?:doors?\s+(?:and|&|or|plus)\s+windows?|windows?\s+(?:and|&|or|plus)\s+doors?)\b", re.I)
_TYPE_Q = re.compile(
    r"\b(?:sliding|slide|folding|bi-?fold(?:ing)?|revolving|garage|overhead|roller|rolling|roll-up|automatic|"
    r"glass|glazed|fire(?:-?rated)?|pocket|barn|french|patio|emergency|exit|steel|timber|wooden|metal|aluminium|"
    r"aluminum|fixed|casement|awning|hopper|tilt(?:-and-turn)?|louvred|louvered|clerestory|dormer|bay|bow|"
    r"skylight|transom|hung|double-hung|single-hung|operable|opening)\s+(?:doors?|windows?|doorsets?)\b", re.I)


_FACADE_Q = re.compile(r"\b(north|south|east|west|left|right|top|bottom|front|rear|back)(?:ern|-?facing)?\s+"
                       r"(?:facade|fa\u00e7ade|side|elevation|wall|face|front|exterior wall|outer wall)s?\b", re.I)
_FACING_Q = re.compile(r"\b(north|south|east|west)(?:ern)?[-\s]?facing\b|\bfac(?:e|es|ing)\s+(?:the\s+|to\s+the\s+)?"
                       r"(north|south|east|west)\b", re.I)
_FACADE_W = set("""north south east west northern southern eastern western left right top bottom front rear back facing
facade façade side sides elevation wall walls face faces facing exterior outer external wing wings block part section
northern southern eastern western""".split())


def parse_question(question: str) -> dict | None:
    q = question or ""
    if not _COUNT_Q.search(q):
        return None
    door, window = bool(_DOOR.search(q)), bool(_WINDOW.search(q))
    if door == window:  # neither, or a list ('doors and windows': compose answers each part)
        return None
    # 'excluding the door openings' / 'skirting' / 'door width' are not counts of doors
    if re.search(r"\bskirting|baseboard|perimeter|\bwidth|\bheight|\barea\b|\bm2\b|m²|\bfire\b|\brating|\bprice|\bcost",
                 q, re.I):
        return None
    # schedule rows, door TYPES and whole-model counts are read elsewhere (schedule / IFC readers), not from swings
    if re.search(r"\bschedul|\btype\b|\btypes\b|\bin the (?:bim |ifc )?model\b|\bmanufactur|\bfiberglass|\baluminium|"
                 r"\baluminum|\bhardware|\bmark(?:ed)?\b", q, re.I):
        return None
    words = [w for w in re.findall(r"[A-Za-z]+|\d+[A-Za-z]?", q.lower())]
    side = None
    both = re.search(r"\b(?:interior|internal|inside)\b.*\b(?:entrance|exterior|external|outside)\b|"
                     r"\b(?:entrance|exterior|external|outside)\b.*\b(?:interior|internal|inside)\b", q, re.I)
    if both:  # 'interior doors and entrance doors together' = all doors (the Swiss dataset wording)
        pass
    elif door and re.search(r"\b(?:exterior|external|outside|outer|entrance|entry)\s+doors?\b|\bdoors?\s+(?:to|leading to)"
                          r"\s+(?:the\s+)?outside\b", q, re.I):
        side = "exterior"
    elif door and re.search(r"\b(?:interior|internal|inside|inner)\s+doors?\b", q, re.I) and \
            not re.search(r"\binterior\s+(?:and|&)\s+(?:entrance|exterior)", q, re.I):
        side = "interior"
    side_words = {"exterior", "external", "outside", "outer", "interior", "internal", "inside", "inner", "leading"}
    if side == "exterior" and re.search(r"\bentrance\s+doors?\b", q, re.I):
        side_words |= {"entrance"}
    scope = [w for w in words if w not in _STOP and len(w) >= 2 or w.isdigit()]
    scope = [w for w in scope if w not in _STOP and not ((side or both) and w in side_words | {"entrance", "exterior",
                                                                                           "external"})]
    fac = _FACADE_Q.search(q) or _FACING_Q.search(q)
    facade = None
    if fac:
        # 'the west facade of the classroom wing': the side + the part's own words (labels that place the wing)
        sw = (fac.group(1) or fac.group(2)).lower() if fac.re is _FACING_Q else fac.group(1).lower()
        facade = {"side": {"left": "west", "right": "east", "top": "north", "bottom": "south", "front": "south",
                           "rear": "north", "back": "north"}.get(sw, sw), "word": sw,
                  "part": [(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w) for w in scope
                           if w not in _FACADE_W and (w not in _WHOLE or w.isdigit()) and w != sw]}
    return {"target": "door" if door else "window", "side": side, "facade": facade,
            "swing_only": bool(re.search(r"\bswing", q, re.I)),
            "double": bool(door and _DOUBLE.search(q) and not _SINGLE.search(q)),
            "single": bool(door and _SINGLE.search(q) and not _DOUBLE.search(q)),
            "leaves": bool(door and _LEAVES.search(q)),
            "scope": scope}


# ---------------------------------------------------------------------------------------------------------------------
# geometry helpers
def _fit_circle(pts):
    import numpy as np

    P = np.asarray(pts, dtype=float)
    A = np.c_[2 * P[:, 0], 2 * P[:, 1], np.ones(len(P))]
    b = (P ** 2).sum(1)
    (cx, cy, c), *_ = np.linalg.lstsq(A, b, rcond=None)
    r = math.sqrt(max(c + cx * cx + cy * cy, 0.0))
    res = float(np.abs(np.hypot(P[:, 0] - cx, P[:, 1] - cy) - r).max()) if r else 1e9
    return cx, cy, r, res


def _ang(dx, dy) -> float:
    return math.degrees(math.atan2(dy, dx))


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _bez(p0, c1, c2, p3, n: int = 8):
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t ** 3 * p3.x,
                    u ** 3 * p0.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t ** 3 * p3.y))
    return out


class _Grid:
    """Endpoints of straight segments on a coarse grid: 'what starts / ends near this point?'."""

    def __init__(self, cell: float = 2.0):
        self.cell = cell
        self.at: dict = {}

    def add(self, p, item):
        self.at.setdefault((int(p[0] // self.cell), int(p[1] // self.cell)), []).append(item)

    def near(self, p, tol: float):
        cx, cy = int(p[0] // self.cell), int(p[1] // self.cell)
        k = int(tol // self.cell) + 1
        out = []
        for dx in range(-k, k + 1):
            for dy in range(-k, k + 1):
                out += self.at.get((cx + dx, cy + dy), [])
        return out


@functools.lru_cache(maxsize=8)
def _page_geometry(pdf: str, page: int):
    """Polylines of one page (PDF points, unrotated, as PyMuPDF reports them) + every straight segment."""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        if page >= len(doc):
            return None
        pg = doc[page]
        drawings = pg.get_drawings()
        rot = pg.rotation
        mat = tuple(pg.rotation_matrix)
        rect = tuple(pg.rect)
    if len(drawings) > 400_000:
        return None
    polys: list[list] = []  # [(x, y), ...] connected runs
    segs: list[tuple] = []  # straight segments (x0, y0, x1, y1)
    singles: list[tuple] = []  # paths that are ONE straight segment (exploded arcs)
    for d in drawings:
        typ = d.get("type") or ""
        if not (("s" in typ and d.get("color") is not None) or ("f" in typ and d.get("fill") is not None)):
            continue  # stroked lines, or a filled swing sector (Swiss sheets fill the entrance door's swing grey)
        cur: list = []
        items = d.get("items", [])
        n_l = 0
        for it in items:
            op = it[0]
            if op == "l":
                a, b = it[1], it[2]
                segs.append((a.x, a.y, b.x, b.y))
                n_l += 1
                if cur and abs(cur[-1][0] - a.x) < 0.05 and abs(cur[-1][1] - a.y) < 0.05:
                    cur.append((b.x, b.y))
                else:
                    if len(cur) >= 2:
                        polys.append(cur)
                    cur = [(a.x, a.y), (b.x, b.y)]
            elif op == "c":
                pts = _bez(it[1], it[2], it[3], it[4])
                if cur and abs(cur[-1][0] - pts[0][0]) < 0.05 and abs(cur[-1][1] - pts[0][1]) < 0.05:
                    cur += pts[1:]
                else:
                    if len(cur) >= 2:
                        polys.append(cur)
                    cur = pts
            else:
                if op == "re":
                    r = it[1]
                    segs += [(r.x0, r.y0, r.x1, r.y0), (r.x1, r.y0, r.x1, r.y1), (r.x1, r.y1, r.x0, r.y1),
                             (r.x0, r.y1, r.x0, r.y0)]
                if len(cur) >= 2:
                    polys.append(cur)
                cur = []
        if len(cur) >= 2:
            polys.append(cur)
        if len(items) == 1 and n_l == 1:
            a, b = items[0][1], items[0][2]
            singles.append(((a.x, a.y), (b.x, b.y)))
    return {"polys": polys, "segs": segs, "singles": singles, "rot": rot, "mat": mat, "rect": rect}


def _chain_singles(singles: list, max_len: float) -> list[list]:
    """One-segment paths chained end to end through joints shared by exactly two short segments (USACE A-101F draws a
    door swing as ~12 separate 2-point lines)."""
    short = [(a, b) for a, b in singles if 0.05 < math.dist(a, b) <= max_len]
    if len(short) < 4:
        return []
    g = _Grid(1.0)
    for i, (a, b) in enumerate(short):
        g.add(a, (i, 0))
        g.add(b, (i, 1))

    def others(p, i):
        return [(j, e) for j, e in g.near(p, 0.3) if j != i and math.dist(short[j][e], p) <= 0.12]

    used = set()
    chains = []
    for i0 in range(len(short)):
        if i0 in used:
            continue
        used.add(i0)
        pts = [short[i0][0], short[i0][1]]
        for forward in (True, False):
            last = i0
            while True:
                end = pts[-1] if forward else pts[0]
                nb = others(end, last)
                if len(nb) != 1 or nb[0][0] in used:
                    break
                j, e = nb[0]
                used.add(j)
                nxt = short[j][1 - e]
                if forward:
                    pts.append(nxt)
                else:
                    pts.insert(0, nxt)
                last = j
        if len(pts) >= 4:
            chains.append(pts)
    return chains


def _arc_runs(poly: list, rmin: float, rmax: float) -> list[dict]:
    """Maximal runs of a polyline that turn steadily one way (an arc drawn as short segments / a sampled Bezier), fitted
    with a circle; kept when round, of a door-leaf radius and sweeping SWEEP degrees."""
    n = len(poly)
    if n < 4:
        return []
    L = [math.dist(poly[i], poly[i + 1]) for i in range(n - 1)]
    th = [_ang(poly[i + 1][0] - poly[i][0], poly[i + 1][1] - poly[i][1]) for i in range(n - 1)]
    turn = [_wrap(th[i + 1] - th[i]) for i in range(n - 2)]
    out = []
    i = 0
    while i < len(turn):
        s = 1 if turn[i] > 0 else -1
        if not (0.3 <= abs(turn[i]) <= 40 and L[i] > 1e-3):
            i += 1
            continue
        j = i
        while (j + 1 < len(turn) and (1 if turn[j + 1] > 0 else -1) == s and 0.3 <= abs(turn[j + 1]) <= 40
               and max(L[j + 1], L[j + 2]) <= 3.0 * min(L[j + 1], L[j + 2]) + 1e-6):
            j += 1
        seg0, seg1 = i, j + 1  # segments seg0..seg1 inclusive
        pts = poly[seg0:seg1 + 2]
        if len(pts) >= 4:
            cx, cy, r, res = _fit_circle(pts)
            if rmin <= r <= rmax and res <= 0.06 * r + 0.3:
                a0 = _ang(pts[0][0] - cx, pts[0][1] - cy)
                a1 = _ang(pts[-1][0] - cx, pts[-1][1] - cy)
                tsum = abs(sum(turn[seg0:j + 1]))  # chord turning (> 160: a circle / half circle, not a swing)
                sweep = abs(_wrap(a1 - a0))
                if tsum <= 160 and SWEEP[0] <= sweep <= SWEEP[1]:
                    out.append({"c": (cx, cy), "r": r, "p0": pts[0], "p1": pts[-1], "sweep": sweep,
                                "tan_prev": th[seg0 - 1] if seg0 >= 1 else None,
                                "tan_next": th[seg1 + 1] if seg1 + 1 < len(th) else None,
                                "th0": th[seg0], "th1": th[seg1],
                                "len_prev": L[seg0 - 1] if seg0 >= 1 else 0.0,
                                "len_next": L[seg1 + 1] if seg1 + 1 < len(L) else 0.0})
        i = j + 1
    return out


def _leaf_end(arc: dict, grid: _Grid, tol: float) -> int | None:
    """Which arc end (0 = p0, 1 = p1) a straight line from the hinge (arc centre), about one radius long, reaches = the
    open leaf; the other end is the leaf's closed position on the doorway. None = no leaf line drawn."""
    c, r = arc["c"], arc["r"]
    for k, p in enumerate((arc["p0"], arc["p1"])):
        for s in grid.near(c, tol) + grid.near(p, tol):
            a, b = (s[0], s[1]), (s[2], s[3])
            for u, v in ((a, b), (b, a)):
                if math.dist(u, c) <= tol and math.dist(v, p) <= max(tol, 0.08 * r):
                    return k
                # a leaf drawn as a thin rectangle: one long side runs from near the hinge along the radius to the arc end
                if math.dist(u, c) <= max(tol, 0.12 * r) and math.dist(v, p) <= max(tol, 0.12 * r) \
                        and abs(math.dist(u, v) - r) <= 0.15 * r:
                    return k
    return None


def doorways(door: dict) -> list[tuple]:
    """Candidate doorway segments (in the wall line) of a grouped door: hinge -> hinge for a pair; hinge -> the arc end
    that is NOT the leaf tip for a single leaf (both ends when no leaf line is drawn)."""
    if door.get("doorway"):  # a door drawn without a swing (folding pair / angled leaf)
        return [door["doorway"]]
    arcs = door["arcs"]
    if door["kind"] == "double":
        return [(arcs[0]["c"], arcs[1]["c"])]
    a = arcs[0]
    ends = [a["p1"]] if a.get("leaf_end") == 0 else [a["p0"]] if a.get("leaf_end") == 1 else [a["p0"], a["p1"]]
    return [(a["c"], e) for e in ends]


_PASSAGE = re.compile(r"\b(?:corridor|circulation|lobby|hall(?:way)?|landing|walkway|stair|lift|void|wc lobby)\b", re.I)


def leaf_doors(pdf: str, page: int, scale_n: float, drawings: list | None = None, labels: list | None = None) -> list:
    """Doors drawn WITHOUT a swing arc (captain 21:11, Ziying's DfE count 115 vs our 101): a half-open leaf = a short
    polyline from a jamb on the wall line out to an apex 0.1-0.45 m off it and back onto the wall line, chord
    0.5-1.3 m, both ends at wall ink. Two leaves sharing an end = a folding / louvred pair (1 door, 2 leaves); a lone
    leaf = an angled-leaf door, counted only when a labelled room other than a corridor / lobby lies within 4 m on
    either side of it (Ziying: 'an unlabelled shallow recess off the corridor is a cupboard', not a door).
    -> door dicts like group_doors' ({"kind": 'folding'|'angled', "leaves", "bbox", "center", "doorway", "arcs": []})."""
    import pymupdf

    m = 1.0 / (PT_M * scale_n)
    if drawings is None or labels is None:
        with pymupdf.open(pdf) as doc:
            pg = doc[page]
            drawings = pg.get_drawings() if drawings is None else drawings
            labels = [(b[4].replace("\n", " ").strip(), tuple(b[:4])) for b in pg.get_text("blocks") if b[4].strip()] \
                if labels is None else labels
    ink = [dr["rect"] for dr in drawings if (dr.get("fill") is not None and sum(dr["fill"]) < 1.0) or
           (dr.get("width") or 0) >= 0.5]
    def at_wall(p):
        d = 0.12 * m
        return any(r.x0 - d <= p[0] <= r.x1 + d and r.y0 - d <= p[1] <= r.y1 + d for r in ink)
    leaves = []
    for dr in drawings:
        if dr.get("type") == "f":
            continue
        pts = []
        for it in dr["items"]:
            if it[0] != "l":
                pts = None
                break
            a, b = (it[1].x, it[1].y), (it[2].x, it[2].y)
            if not pts:
                pts = [a, b]
            elif math.dist(pts[-1], a) < 0.05:
                pts.append(b)
            else:
                pts = None
                break
        if not pts or not 3 <= len(pts) <= 5:
            continue
        s0, e0 = pts[0], pts[-1]
        ch = math.dist(s0, e0)
        if not 0.5 * m <= ch <= 1.3 * m:
            continue
        ux, uy = (e0[0] - s0[0]) / ch, (e0[1] - s0[1]) / ch
        if not (abs(ux) < 0.02 or abs(uy) < 0.02):
            continue  # the wall line of a CAD plan runs along a sheet axis
        dev = [(p[0] - s0[0]) * uy - (p[1] - s0[1]) * ux for p in pts]
        if not 0.1 * m <= max(abs(v) for v in dev) <= 0.45 * m or (max(dev) > 0.02 * m and min(dev) < -0.02 * m):
            continue  # one-sided bulge (a leaf), not a wiggle / zigzag partition
        top = max(abs(v) for v in dev)
        if sum(1 for v in dev if abs(v) >= 0.8 * top) != 1:
            continue  # a leaf is a V with ONE apex; out-along-back brackets are cabinets / wall-mounted items (MFC A-101)
        ws, we = at_wall(s0), at_wall(e0)
        if not (ws or we):
            continue
        side = 1.0 if max(dev, key=abs) > 0 else -1.0
        leaves.append({"s": s0, "e": e0, "u": (ux, uy), "side": side, "ch": ch, "ws": ws, "we": we})
    out, used = [], set()
    for i, a in enumerate(leaves):
        if i in used:
            continue
        pair = None
        for j in range(i + 1, len(leaves)):
            b = leaves[j]
            if j in used:
                continue
            ends = [(math.dist(p, q), pi, qi) for pi, p in enumerate((a["s"], a["e"]))
                    for qi, q in enumerate((b["s"], b["e"]))]
            d0, pi, qi = min(ends)
            if d0 < 0.08 * m and abs(abs(a["u"][0]) - abs(b["u"][0])) < 0.05:
                # the two OUTER ends are the jambs: wall ink there (the shared middle is in the open doorway)
                if (a["we"] if pi == 0 else a["ws"]) and (b["we"] if qi == 0 else b["ws"]):
                    pair = j
                    break
        pts = [a["s"], a["e"]] + ([leaves[pair]["s"], leaves[pair]["e"]] if pair is not None else [])
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if pair is not None:
            used.add(pair)
            far = max(((p, q) for p in pts for q in pts), key=lambda pq: math.dist(*pq))
            out.append({"kind": "folding", "leaves": 2, "arcs": [], "doorway": far,
                        "bbox": (min(xs) - 0.25 * m, min(ys) - 0.25 * m, max(xs) + 0.25 * m, max(ys) + 0.25 * m),
                        "center": ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)})
            continue
        # a lone half-open leaf: a walk-in room's door when the NEAREST label is that room's, within 2.5 m, and not
        # a passage; a cupboard in a corridor wall has the corridor or a bigger room 2.7-3 m away as its nearest
        # label (DfE: 7 store doors kept, 6 corridor cupboards out = Ziying's 128 - 13 cupboards)
        if not (a["ws"] and a["we"]):
            continue  # a lone leaf spans its whole opening: wall ink at both ends
        cx, cy = (a["s"][0] + a["e"][0]) / 2, (a["s"][1] + a["e"][1]) / 2
        near = sorted((math.dist((cx, cy), ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)), t) for t, bb in labels
                      if re.search(r"[A-Za-z]{3}", t))
        if not near or near[0][0] > 2.5 * m or _PASSAGE.search(near[0][1]):
            continue
        named = [near[0][1]]
        out.append({"kind": "angled", "leaves": 1, "arcs": [], "doorway": (a["s"], a["e"]), "room": named[0][:30],
                    "bbox": (min(xs) - 0.3 * m, min(ys) - 0.3 * m, max(xs) + 0.3 * m, max(ys) + 0.3 * m),
                    "center": (cx, cy)})
    return out


def sliding_doors(pdf: str, page: int, scale_n: float, drawings: list | None = None) -> list:
    """Sliding doors drawn as '<- ->' (Ziying: 'two opposite arrows = sliding door'; DfE Entrance Lobby x2): two short
    arrows on ONE axis line pointing apart, shafts 0.15-0.8 m, their tails 0.05-0.8 m apart with nothing joining them
    (a dimension line's arrows sit at the two ends of one continuous line). One door per pair, box round both."""
    import pymupdf

    m = 1.0 / (PT_M * scale_n)
    if drawings is None:
        with pymupdf.open(pdf) as doc:
            drawings = doc[page].get_drawings()
    segs = []
    for dr in drawings:
        if dr.get("type") == "f":
            continue
        for it in dr["items"]:
            if it[0] == "l":
                segs.append(((it[1].x, it[1].y), (it[2].x, it[2].y)))
    def key(p):
        return (round(p[0] / (0.02 * m)), round(p[1] / (0.02 * m)))
    heads: dict = {}
    for a, b in segs:
        L = math.dist(a, b)
        if 0.02 * m <= L <= 0.15 * m:
            for tip, tail in ((a, b), (b, a)):
                heads.setdefault(key(tip), []).append((tip, tail))
    arrows = []
    for a, b in segs:  # shafts along a sheet axis, 0.15-0.8 m
        L = math.dist(a, b)
        if not 0.15 * m <= L <= 0.8 * m or not (abs(a[0] - b[0]) < 0.05 or abs(a[1] - b[1]) < 0.05):
            continue
        for tip, tail in ((a, b), (b, a)):
            hs = [h for h in heads.get(key(tip), []) if math.dist(h[0], tip) < 0.02 * m]
            ux, uy = (tip[0] - tail[0]) / L, (tip[1] - tail[1]) / L
            # two barbs pointing back from the tip, one on each side of the shaft
            sides = set()
            for h in hs:
                vx, vy = h[1][0] - tip[0], h[1][1] - tip[1]
                lv = math.hypot(vx, vy) or 1.0
                back = -(vx * ux + vy * uy) / lv
                if 0.6 <= back <= 0.97:
                    sides.add(1 if (vx * uy - vy * ux) > 0 else -1)
            if sides == {1, -1}:
                arrows.append({"tip": tip, "tail": tail, "u": (ux, uy)})
    out, used = [], set()
    for i, a in enumerate(arrows):
        for j in range(i + 1, len(arrows)):
            b = arrows[j]
            if i in used or j in used:
                continue
            if a["u"][0] * b["u"][0] + a["u"][1] * b["u"][1] > -0.98:
                continue  # not opposite
            gap = math.dist(a["tail"], b["tail"])
            off = abs((b["tail"][0] - a["tail"][0]) * a["u"][1] - (b["tail"][1] - a["tail"][1]) * a["u"][0])
            if not 0.05 * m <= gap <= 0.8 * m or off > 0.05 * m:
                continue
            # pointing APART: each tip lies beyond its own tail as seen from the other arrow
            if math.dist(a["tip"], b["tail"]) <= math.dist(a["tail"], b["tail"]):
                continue
            mid = ((a["tail"][0] + b["tail"][0]) / 2, (a["tail"][1] + b["tail"][1]) / 2)
            if any(_on_seg(mid, s0, s1, 0.01 * m) for s0, s1 in segs if math.dist(s0, s1) > 0.05 * m and
                   min(math.dist(s0, a["tail"]), math.dist(s1, a["tail"])) > 0.01 * m):
                continue  # a line runs between the tails: a dimension line, not a sliding symbol
            used |= {i, j}
            if any(math.dist(mid, o["center"]) < 0.3 * m for o in out):
                continue  # the same symbol drawn twice
            xs = [a["tip"][0], b["tip"][0]]
            ys = [a["tip"][1], b["tip"][1]]
            out.append({"kind": "sliding", "leaves": 2, "arcs": [], "doorway": (a["tip"], b["tip"]),
                        "bbox": (min(xs) - 0.2 * m, min(ys) - 0.2 * m, max(xs) + 0.2 * m, max(ys) + 0.2 * m),
                        "center": mid})
    return out


def _on_seg(p, a, b, tol) -> bool:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return False
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2
    if not 0.0 < t < 1.0:
        return False
    return abs((p[0] - ax) * dy - (p[1] - ay) * dx) / math.sqrt(L2) <= tol


def _arcless(pdf: str, page: int, scale_n: float | None) -> list:
    """leaf_doors() + sliding_doors() as the door reader uses them: folding / louvred pairs (DfE: 3 plant-room louvres
    + 2 stair doors), lone angled leaves of labelled rooms (DfE: 7 stores; STOA_DR_ANGLED=0 = off), sliding '<- ->'
    (DfE lobby: 2; STOA_DR_SLIDING=0 = off). STOA_DR_ARCLESS=0 = none of them."""
    if not scale_n or os.environ.get("STOA_DR_ARCLESS", "1") == "0":
        return []
    try:
        ds = leaf_doors(pdf, page, scale_n)
    except Exception:  # noqa: BLE001
        return []
    keep = ("folding", "angled") if os.environ.get("STOA_DR_ANGLED", "1") == "1" else ("folding",)
    out = [d for d in ds if d["kind"] in keep]
    if os.environ.get("STOA_DR_SLIDING", "1") != "0":
        try:
            out += sliding_doors(pdf, page, scale_n)
        except Exception:  # noqa: BLE001
            pass
    return out


def opening_width_m(door: dict, scale_n: float) -> float:
    """The clear opening a door fills, in metres at 1:scale_n: hinge to hinge for a pair (the WHOLE opening, captain
    20:52: skirting deducts it once), else the leaf radius."""
    if door.get("doorway"):  # drawn without a swing: the leaf's chord (pair: end to end)
        return round(math.dist(*door["doorway"]) * PT_M * scale_n, 2)
    if door["kind"] == "double":
        a, b = door["arcs"]
        w = math.dist(a["c"], b["c"])
    else:
        w = door["arcs"][0]["r"]
    return round(w * PT_M * scale_n, 2)


def door_openings(pdf: str, page: int = 0, polygon: list | None = None, scale_n: float | None = None) -> dict:
    """Door openings for skirting / baseboard deductions (G1 / E1): every door drawn with a swing on the page, or only
    those whose doorway lies on an edge of `polygon` (pdf_pt, D1's rule, 0.45 m), with its opening width.
    -> {"scale_n", "doors": [{"bbox", "width_m", "leaves", "kind"}], "notes"}; doors [] when no scale / no swings."""
    sw = find_swings(pdf, page, scale_n)
    n = sw["scale_n"]
    if not n:
        return {"scale_n": None, "doors": [], "notes": sw["notes"]}
    out = []
    tol = 0.45 / (PT_M * n)
    for d in group_doors(sw["arcs"]) + _arcless(pdf, page, n):
        if polygon and not any(_on_edge(polygon, ((u[0] + v[0]) / 2, (u[1] + v[1]) / 2), tol) for u, v in doorways(d)):
            continue
        out.append({"bbox": [round(v, 1) for v in d["bbox"]], "width_m": opening_width_m(d, n), "leaves": d["leaves"],
                    "kind": d["kind"]})
    return {"scale_n": n, "doors": out, "notes": sw["notes"]}


def _on_edge(poly: list, p: tuple, tol: float) -> bool:
    """p lies within tol of an edge of the polygon AND projects inside that edge (D1's door-to-room rule)."""
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        if L2 <= 0:
            continue
        t = ((p[0] - x0) * dx + (p[1] - y0) * dy) / L2
        if 0.0 <= t <= 1.0 and math.hypot(p[0] - x0 - t * dx, p[1] - y0 - t * dy) <= tol:
            return True
    return False


def _tangent_both(arc: dict, grid: _Grid, tol: float) -> bool:
    """Rounded corner of a box / legend frame: at BOTH ends a straight line runs on along the arc's tangent."""
    hits = 0
    for p, th in ((arc["p0"], arc["th0"] + 180.0), (arc["p1"], arc["th1"])):
        ok = False
        for s in grid.near(p, tol):
            a, b = (s[0], s[1]), (s[2], s[3])
            for u, v in ((a, b), (b, a)):
                if math.dist(u, p) <= tol and math.dist(u, v) >= 0.3 * arc["r"]:
                    d = _ang(v[0] - u[0], v[1] - u[1])
                    if abs(_wrap(d - th)) <= 20 or abs(_wrap(d - th + 180)) <= 20:
                        ok = True
        hits += ok
    return hits == 2


def find_swings(pdf: str, page: int, scale_n: float | None) -> dict:
    """Door-swing arcs on one PDF page -> {"arcs": [...], "scale_n", "notes"} (PDF points, unrotated)."""
    import numpy as np

    geo = _page_geometry(pdf, page)
    if geo is None:
        return {"arcs": [], "scale_n": scale_n, "notes": ["page has no usable vector paths"]}
    notes: list[str] = []
    # radius window in points: wide first, then the printed scale (or the radii themselves) decide
    if scale_n:
        lo, hi = 0.25 / (PT_M * scale_n), 2.6 / (PT_M * scale_n)
    else:
        lo, hi = 4.0, 250.0
    max_single = (0.4 / (PT_M * scale_n)) if scale_n else 12.0
    polys = list(geo["polys"]) + _chain_singles(geo["singles"], max_single)
    cand = []
    for poly in polys:
        cand += _arc_runs(poly, lo, hi)
    grid = _Grid(2.0)
    for s in geo["segs"]:
        grid.add((s[0], s[1]), s)
        grid.add((s[2], s[3]), s)
    # dedupe exact copies (the same arc drawn twice: Swiss DXF duplicates, PDF overprint)
    uniq: list[dict] = []
    for a in cand:
        if any(math.dist(a["c"], b["c"]) <= 0.02 * a["r"] + 0.3 and abs(a["r"] - b["r"]) <= 0.03 * a["r"] + 0.3
               and min(math.dist(a["p0"], b["p0"]) + math.dist(a["p1"], b["p1"]),
                       math.dist(a["p0"], b["p1"]) + math.dist(a["p1"], b["p0"])) <= 0.1 * a["r"] + 0.6
               for b in uniq):
            continue
        uniq.append(a)
    # full circles: >= 3 arcs on one centre with one radius (grid bubbles, section markers)
    keep = []
    for a in uniq:
        same = sum(1 for b in uniq if math.dist(a["c"], b["c"]) <= 0.04 * a["r"] + 0.3
                   and abs(a["r"] - b["r"]) <= 0.05 * a["r"] + 0.3)
        if same >= 3:
            continue
        keep.append(a)
    tol = 0.6
    out = []
    for a in keep:
        if _tangent_both(a, grid, tol):
            continue
        a["leaf_end"] = _leaf_end(a, grid, max(tol, 0.03 * a["r"]))
        a["leaf"] = a["leaf_end"] is not None
        out.append(a)
    # scale check: door leaves are ~0.7-1.0 m. Use the arcs WITH a leaf line when there are enough of them.
    radii = [a["r"] for a in out if a["leaf"]] or [a["r"] for a in out]
    eff = scale_n
    if radii:
        med_pt = float(np.median(radii))
        if scale_n:
            med_m = med_pt * PT_M * scale_n
            if not 0.6 <= med_m <= 1.15:
                # a printed '1:200 @ A1' on an A3 page, or a reduced print: try sqrt(2) paper steps
                best = min((abs(med_m * k - 0.85), k) for k in (0.5, 2 ** -0.5, 2 ** 0.5, 2.0, 2.0 * 2 ** 0.5, 4.0))
                if 0.6 <= med_m * best[1] <= 1.15 and len(radii) >= 4:
                    eff = scale_n * best[1]
                    notes.append(f"door leaves measure {med_m:.2f} m at the printed 1:{scale_n:g}: the sheet is printed at "
                                 f"another paper size, so used 1:{eff:g} (door leaves {med_m * best[1]:.2f} m)")
        else:
            eff = 0.85 / (med_pt * PT_M)
            notes.append(f"no printed scale: assumed the typical door leaf is 0.85 m (≈ 1:{eff:.0f})")
    if eff:
        rlo, rhi = DOOR_R_M[0] / (PT_M * eff), DOOR_R_M[1] / (PT_M * eff)
        out = [a for a in out if rlo <= a["r"] <= rhi]
    # when most arcs carry a leaf line, an arc without one is something else (a curved wall, a revolving symbol)
    with_leaf = sum(a["leaf"] for a in out)
    leafless = []
    if out and with_leaf >= 0.6 * len(out):
        leafless = [a for a in out if not a["leaf"]]
        out = [a for a in out if a["leaf"]]
    return {"arcs": out, "scale_n": eff, "notes": notes, "rot": geo["rot"], "mat": geo["mat"], "rect": geo["rect"],
            "leafless": leafless}


def group_doors(arcs: list[dict]) -> list[dict]:
    """Arcs -> doors. A double door = two leaves whose closed positions meet on one doorway (hinges ~ r1 + r2 apart,
    each arc ending on the hinge-hinge line next to the other); two arcs on one hinge = one double-acting leaf."""
    n = len(arcs)
    used = [False] * n
    doors = []

    def closed_end_toward(a, target):  # the arc end that lies on the hinge -> target line
        c = a["c"]
        d = math.dist(c, target) or 1.0
        ux, uy = (target[0] - c[0]) / d, (target[1] - c[1]) / d
        exp = (c[0] + ux * a["r"], c[1] + uy * a["r"])
        return min(math.dist(a["p0"], exp), math.dist(a["p1"], exp))

    # double-acting leaf: same hinge, same radius
    for i in range(n):
        if used[i]:
            continue
        for j in range(i + 1, n):
            if used[j]:
                continue
            a, b = arcs[i], arcs[j]
            if math.dist(a["c"], b["c"]) <= 0.06 * a["r"] + 0.5 and abs(a["r"] - b["r"]) <= 0.08 * a["r"]:
                used[i] = used[j] = True
                doors.append({"arcs": [a, b], "leaves": 1, "kind": "double-acting"})
                break
    # pairs
    pairs = []
    for i in range(n):
        if used[i]:
            continue
        for j in range(i + 1, n):
            if used[j]:
                continue
            a, b = arcs[i], arcs[j]
            d = math.dist(a["c"], b["c"])
            if abs(d - (a["r"] + b["r"])) > 0.15 * (a["r"] + b["r"]):
                continue
            if max(a["r"], b["r"]) > 2.2 * min(a["r"], b["r"]):
                continue
            e1, e2 = closed_end_toward(a, b["c"]), closed_end_toward(b, a["c"])
            if e1 <= 0.15 * a["r"] + 0.5 and e2 <= 0.15 * b["r"] + 0.5:
                pairs.append((e1 + e2, i, j))
    for _, i, j in sorted(pairs):
        if used[i] or used[j]:
            continue
        used[i] = used[j] = True
        doors.append({"arcs": [arcs[i], arcs[j]], "leaves": 2, "kind": "double"})
    for i in range(n):
        if not used[i]:
            doors.append({"arcs": [arcs[i]], "leaves": 1, "kind": "single"})
    for d in doors:
        xs = [p[0] for a in d["arcs"] for p in (a["c"], a["p0"], a["p1"])]
        ys = [p[1] for a in d["arcs"] for p in (a["c"], a["p0"], a["p1"])]
        d["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
        d["center"] = ((d["bbox"][0] + d["bbox"][2]) / 2, (d["bbox"][1] + d["bbox"][3]) / 2)
    return doors


def exterior_flags(pdf: str, page: int, doors: list[dict], scale_n: float, ppm: float = 12.0) -> list[bool] | None:
    """Per door: does its doorway open to the OUTSIDE? Render the building (door swings + 6 m) at ~12 px/m, close every
    doorway with a line, flood the free space from the crop's border = outside; a door is exterior when a point 0.8 m
    to one side of its doorway is outside and the point on the other side is not."""
    import cv2
    import numpy as np
    import pymupdf

    if not doors:
        return []
    pts = [p for d in doors for a in d["arcs"] for p in (a["c"], a["p0"], a["p1"])]
    m = 6.0 / (PT_M * scale_n)
    x0, y0 = min(p[0] for p in pts) - m, min(p[1] for p in pts) - m
    x1, y1 = max(p[0] for p in pts) + m, max(p[1] for p in pts) + m
    dpi = int(max(20, min(300, round(ppm * 0.0254 * scale_n))))
    f = dpi / 72.0
    with pymupdf.open(pdf) as doc:
        pg = doc[page]
        rot = pg.rotation
        if rot:
            pg.set_rotation(0)
        pix = pg.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=pymupdf.Rect(x0, y0, x1, y1))
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    ink = (img < 160).astype(np.uint8)
    to_px = lambda p: (int(round((p[0] - x0) * f)), int(round((p[1] - y0) * f)))  # noqa: E731
    for d in doors:
        for u, v in doorways(d):
            cv2.line(ink, to_px(u), to_px(v), 1, max(2, int(round(0.15 * ppm))))
    ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))  # close hairline gaps (window lines, anti-aliasing)
    free = (1 - ink).astype(np.uint8)
    h, w = free.shape
    mask = np.zeros((h + 2, w + 2), np.uint8)
    out = free.copy()
    for x in range(0, w, 3):
        for y in (0, h - 1):
            if out[y, x] == 1:
                cv2.floodFill(out, mask, (x, y), 2)
    for y in range(0, h, 3):
        for x in (0, w - 1):
            if out[y, x] == 1:
                cv2.floodFill(out, mask, (x, y), 2)
    outside = out == 2
    if outside.mean() > 0.97:  # the building leaked: no closed outline
        return None
    flags = []
    off = 0.8 / (PT_M * scale_n)
    for d in doors:
        u, v = doorways(d)[0]
        mx, my = (u[0] + v[0]) / 2, (u[1] + v[1]) / 2
        L = math.dist(u, v) or 1.0
        nx, ny = -(v[1] - u[1]) / L, (v[0] - u[0]) / L
        sides = []
        for sgn in (1, -1):
            px, py = to_px((mx + sgn * off * nx, my + sgn * off * ny))
            sides.append(0 <= px < w and 0 <= py < h and bool(outside[py, px]))
        flags.append(sides[0] != sides[1])
    return flags


# ---------------------------------------------------------------------------------------------------------------------
# viewports (titles under each view; VP builds the general version in stoa/sheet/)
def _to_display(mat, p):
    a, b, c, d, e, f = mat
    return (p[0] * a + p[1] * c + e, p[0] * b + p[1] * d + f)


@functools.lru_cache(maxsize=8)
def view_titles(pdf: str, page: int = 0) -> tuple:
    """Horizontal text lines that name a view ('MODULE 02-ADMINISTRATION - FLOOR PLAN', 'FILTER BUILDING FLOOR PLAN',
    '... - RCP', '... AXON.') -> ({text, kind, box (display coords)}, ...); notes / title-block text are dropped (long
    lines, small type, the right-hand title-block strip)."""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        pg = doc[page]
        mat = tuple(pg.rotation_matrix)
        W, H = pg.rect.width, pg.rect.height
        blocks = pg.get_text("dict").get("blocks", [])
    lines = []
    for b in blocks:
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = " ".join(s["text"].strip() for s in spans)
            size = max(s.get("size", 0) for s in spans)
            dx, dy = ln.get("dir", (1, 0))
            ddx, ddy = dx * mat[0] + dy * mat[2], dx * mat[1] + dy * mat[3]
            x0, y0, x1, y1 = ln["bbox"]
            p0, p1 = _to_display(mat, (x0, y0)), _to_display(mat, (x1, y1))
            box = (min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1]))
            lines.append({"text": text, "size": size, "horiz": abs(ddx) > 0.9 and abs(ddy) < 0.2, "box": box})
    if not lines:
        return ()
    sizes = sorted(ln["size"] for ln in lines)
    # the body text size: the median, or the lower quartile when notes dominate the text (X1 21:22, AFCEE A-103: notes
    # 11.8 pt are the median, the 12.4 pt view titles were 'small type' -> a view title read as a room -> 0 doors)
    med = sizes[len(sizes) // 2] if len(sizes) < 20 else min(sizes[len(sizes) // 2], sizes[len(sizes) // 4] * 1.1)
    out = []
    for ln in lines:
        t = ln["text"]
        if not ln["horiz"] or len(t) > 70 or len(t) < 4 or ln["size"] < 1.15 * med:
            continue
        if ln["box"][0] > 0.86 * W:  # the title-block strip on the right
            continue
        if re.match(r"^\s*(?:\d+\.|NOTE|SEE |PROVIDE|ALL |REFER)", t, re.I) or _NOT_TITLE.search(t):
            continue
        kind = None
        for k, rx in _VIEW_KIND:
            if rx.search(t):
                kind = k
                break
        if kind is None:
            continue
        out.append({"text": t, "kind": kind, "box": ln["box"]})
    return tuple(out)


def view_cells(titles: tuple, W: float, H: float) -> list[dict]:
    """Each title's cell: the band above it up to the title row above, from its own left edge (less a margin) to the
    next title on its row (CAD sheets put a view's title under the view, left-aligned)."""
    rows: list[list[dict]] = []
    for t in sorted(titles, key=lambda t: t["box"][3]):
        if rows and abs(rows[-1][0]["box"][3] - t["box"][3]) <= 0.04 * H:
            rows[-1].append(t)
        else:
            rows.append([t])
    cells = []
    prev_bottom = 0.0
    for row in rows:
        row.sort(key=lambda t: t["box"][0])
        bottom = max(t["box"][3] for t in row)
        for k, t in enumerate(row):
            x0 = t["box"][0] - 0.04 * W
            x1 = (row[k + 1]["box"][0] - 0.04 * W) if k + 1 < len(row) else 0.88 * W
            cells.append({**t, "cell": (max(0.0, x0), prev_bottom, x1, bottom)})
        prev_bottom = bottom
    return cells


def _scope_match(scope: list[str], cells: list[dict]) -> list[dict]:
    """Plan cells the question names ('module 02', 'the entry vestibule module', 'the filter building')."""
    if not scope or not cells:
        return []
    toks = [set(re.findall(r"[a-z]+|\d+", c["text"].lower())) for c in cells]
    # numbers may be zero-padded in one place only: '2' ~ '02'
    norm = lambda w: w.lstrip("0") or "0" if w.isdigit() else w  # noqa: E731
    toks = [{norm(w) for w in t} for t in toks]
    sc = [norm(w) for w in scope]
    def fuzzy(t: set) -> set:  # OCR cuts words at tile borders: 'ADMINISTRATI' ~ 'administration'
        return t | {w for w in sc for x in t if len(x) >= 5 and len(w) >= 5 and (w.startswith(x) or x.startswith(w))}

    toks = [fuzzy(t) for t in toks]
    best, out = 0.0, []
    for c, t in zip(cells, toks):
        s = 0.0
        if not any(w in t for w in sc if not w.isdigit()):
            continue  # a bare number ('English 3') never names 'Secondary Type 3 600 First Floor': a word must match
        if any(w not in t and w not in _WHOLE for w in sc if not w.isdigit()):
            continue  # 'Hygiene Room Primary' is a room, not the view 'Primary School GA Plan Level 00
        for w in sc:
            if w in t:
                share = sum(1 for tt in toks if w in tt) / len(toks)
                s += 1.0 if share < 0.6 or len(toks) == 1 else 0.25
        if s > best + 1e-9:
            best, out = s, [c]
        elif s >= best - 1e-9 and s > 0:
            out.append(c)
    return out if best >= 1.0 else []


# ---------------------------------------------------------------------------------------------------------------------
# multi-page PDFs (a drawing set: one storey per page)
_STOREY = re.compile(r"\b(ground|first|second|third|fourth|fifth|basement|lower ground|upper ground|top|roof)\s+"
                     r"(?:floor|storey|story|level)\b|\blevel\s*0*(\d{1,2})\b|\b(\d)(?:st|nd|rd|th)\s+floor\b", re.I)
_ORD = {"1": "first", "2": "second", "3": "third", "4": "fourth", "5": "fifth"}


def _storey_keys(text: str) -> set[str]:
    out = set()
    for m in _STOREY.finditer(text):
        if m.group(1):
            out.add(m.group(1).lower() + " floor")
        elif m.group(2) is not None:
            out.add(f"level {int(m.group(2))}")
        elif m.group(3):
            out.add(_ORD.get(m.group(3), m.group(3)) + " floor")
    return out


def pick_pages(pdf: str, question: str, max_pages: int = 8) -> tuple[list[int], str]:
    """Pages to count on: one-page PDF -> [0]; a set -> the page(s) whose BIG text (titles, title block) names the
    storey the question asks for ('first floor', 'level 01'), else every page with door swings, summed."""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        n = len(doc)
        if n <= 1:
            return [0], ""
        want = _storey_keys(question)
        keys = []
        for i in range(min(n, max_pages)):
            lines = []
            for b in doc[i].get_text("dict").get("blocks", []):
                for ln in b.get("lines", []):
                    t = " ".join(sp.get("text", "") for sp in ln.get("spans", []))
                    sz = max((sp.get("size", 0) for sp in ln.get("spans", [])), default=0)
                    lines.append((t, sz))
            sizes = sorted(sz for _, sz in lines) or [0]
            med = sizes[len(sizes) // 2]
            keys.append(_storey_keys(" | ".join(t for t, sz in lines if sz >= 1.15 * med)))
    if want:
        hit = [i for i, k in enumerate(keys) if k & want]
        if hit:
            return hit, f"openings: page(s) {', '.join(str(i + 1) for i in hit)} of {n} name {', '.join(sorted(want))}"
    pages = list(range(min(n, max_pages)))
    try:  # X1 21:22 (AFCEE A-103 + A-602): when some pages title their floor-plan views, an untitled page (the door
        # schedule sheet, whose door elevations carry swings) is not a plan to count
        plans = [i for i in pages if any(t["kind"] == "plan" for t in view_titles(pdf, i))]
    except Exception:  # noqa: BLE001
        plans = []
    if plans and len(plans) < len(pages):
        return plans, (f"openings: page(s) {', '.join(str(i + 1) for i in plans)} of {n} title floor-plan views; the "
                       "other pages are not counted")
    return pages, f"openings: {n}-page PDF, every page counted"


# ---------------------------------------------------------------------------------------------------------------------
# rooms named by the question ('English 1', 'the office', 'English 1, English 2 and English 3')
_ROOM_STOP = _STOP | {"have", "has", "together", "combined", "altogether", "room", "rooms", "does", "do", "got"}


def named_rooms(pdf: str, page: int, question: str, scale_n: float) -> list[dict] | None:
    """[{label, polygon (pdf_pt)}] for every room the question names (a list 'A, B and C' -> each part), traced around
    its label at 1:scale_n; None when the question names no room label of this sheet; [] when a named room has no
    traceable outline."""
    from stoa.evidence import lineplan
    from stoa.evidence.linework import measure_room

    labs = lineplan.labels(pdf, page)
    if not labs:
        return None
    hits: list[dict] = []
    qwords = {w for w in re.findall(r"[^\W\d_]{3,}", question.lower()) if w not in _ROOM_STOP}
    whole = lineplan._match_labels(labs, question)
    if len(whole) == 1 and qwords <= set(whole[0]["words"]) | _ROOM_STOP:
        parts = [question]  # 'Entrance and Cloakroom' is ONE label: do not split it at 'and'
    else:
        parts = [p for p in re.split(r",|\band\b|&|\bplus\b", question) if p.strip()]
    missed = False
    for p in parts if len(parts) > 1 else [question]:
        words = [w for w in re.findall(r"[^\W\d_]{3,}", p.lower()) if w not in _ROOM_STOP]
        if not words:
            continue
        m = lineplan._match_labels(labs, p)
        if len(m) == 1 and m[0] not in hits:
            hits.append(m[0])
        elif len(m) > 1 and len(parts) == 1:
            return []  # 'the office' with three OFFICE labels: ambiguous
        elif (not m or m[0] in hits or len(m) > 1) and re.search(r"\d|\b[A-Z][a-z]", p.split("many", 1)[-1]):
            missed = True  # a named part ('English 1') with no single label of its own (an OCR misread on a scan)
    if hits and missed:
        return []  # 'English 1, English 2 and English 3' with one of them unreadable: no silent partial sum
    if not hits:
        return None
    out = []
    for lab in hits:
        r = measure_room(pdf, page, lab["pt"], scale_n, tag_box=tuple(lab["bbox"]))
        if not r or not r.get("polygon"):
            return []
        out.append({"label": lab["text"], "polygon": r["polygon"], "pt": lab["pt"]})
    return out


_WHOLE = set("""house home building buildings apartment flat dwelling school property ground first
second third fourth upper lower basement cellar attic level levels storey storeys story stories floor floors overall
entire whole sheet drawing plan shown drawn visible""".split())  # not exterior / interior: a different count


def _merge_line_items(items: list) -> list:
    """OCR / VLM text items cut at tile borders ('MODULE 01- ENTRY VESTIE' + 'VESTIBULE - FLOOR PLAN') -> whole lines
    (overlapping tiles read some words twice; the joined line still holds every word of the title for matching)."""
    out: list = []  # [text, box, last piece box]: rows are compared on the LAST piece (a circled view number '1'
    for t, b in sorted(items, key=lambda it: it[1][0]):  # merged in front makes the line box taller)
        h = b[3] - b[1]
        for k, (t2, b2, last) in enumerate(out):
            h2 = last[3] - last[1]
            same_row = abs((b[1] + b[3]) / 2 - (last[1] + last[3]) / 2) <= 0.4 * max(h, h2) \
                and 0.6 <= h / max(h2, 1e-6) <= 1.6
            gap = b[0] - last[2]
            if same_row and -0.9 * min(b[2] - b[0], last[2] - last[0]) <= gap <= 1.2 * max(h, h2):
                out[k] = (f"{t2} {t}", [b2[0], min(b[1], b2[1]), max(b[2], b2[2]), max(b[3], b2[3])], list(b))
                break
        else:
            out.append((t, list(b), list(b)))
    return [(t, b) for t, b, _ in out]


@functools.lru_cache(maxsize=32)
def _big_words(pdf: str, page: int) -> frozenset:
    """Words of the page's larger-than-median text lines (sheet / storey titles such as 'KG', '00 Begane grond')."""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        blocks = doc[page].get_text("dict").get("blocks", [])
    lines = []
    for b in blocks:
        for ln in b.get("lines", []):
            sp = ln.get("spans", [])
            t = " ".join(x.get("text", "") for x in sp)
            lines.append((t, max((x.get("size", 0) for x in sp), default=0)))
    sizes = sorted(z for _, z in lines) or [0]
    med = sizes[len(sizes) // 2]
    return frozenset(w for t, z in lines if z >= 1.15 * med for w in re.findall(r"[a-z]+|\d+", t.lower()))


def _sheet_named(pdf: str, page: int, scope: list[str]) -> bool:
    """A one-view sheet whose own title holds every scope word: 'the floor plan of KG' on the sheet titled 'KG'."""
    try:
        big = _big_words(pdf, page)
    except Exception:  # noqa: BLE001
        return False
    norm = lambda w: (w.lstrip("0") or "0") if w.isdigit() else w  # noqa: E731
    bign = {norm(w) for w in big}
    return bool(scope) and all(norm(w) in bign or w in _WHOLE for w in scope)


def _whole_scope(scope: list[str]) -> bool:
    """'the house', 'the ground floor', 'this level': words that mean the whole drawing on a one-view sheet."""
    return all(w in _WHOLE or w.isdigit() for w in scope)


def _inside(poly: list, p: tuple) -> bool:
    x, y, ins = p[0], p[1], False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            ins = not ins
    return ins


def vector_windows(pdf: str, page: int, scale_n: float) -> list[dict]:
    """Window units read from the vector linework itself (no render, no detector, resolution-independent): a window in a
    plan is a wall opening bridged by 2-4 thin parallel lines that share the SAME span (outer face, glass, inner face),
    0.4-3.5 m long, packed within <= 0.7 m across the wall. Ziying's rule (dfe-08): 'a gap in the wall with thin
    parallel lines inside (glazing); wall piers between gaps mean separate windows'. Stair treads (a long evenly spaced
    run of equal lines), single lines, stubs < 0.55 m (EFA: 0.43 m partition ends), stacks holding a wall pen (> 0.36 pt:
    USACE A-101F pilasters, M-101A pipes) and boxes whose wall face line runs on through them are not windows. Axis-parallel units only (CAD plans in sheet axes).
    -> [{"a": 'v'|'h' (direction of the lines), "lo", "hi" (span), "offs" (line positions across), "box", "len_m"}]."""
    import pymupdf

    m = 1.0 / (PT_M * scale_n)  # PDF points per metre
    groups: dict = {}
    longs: dict = {"v": [], "h": []}  # every stroked axis line (any pen): a wall face running THROUGH a unit = no opening
    with pymupdf.open(pdf) as doc:
        drawings = doc[page].get_drawings()
    for dr in drawings:
        if dr.get("type") == "f":
            continue
        for it in dr["items"]:
            if it[0] == "l":
                (x0, y0), (x1, y1) = (it[1].x, it[1].y), (it[2].x, it[2].y)
                if abs(x0 - x1) < 0.05 and abs(y0 - y1) > 1.0 * m:
                    longs["v"].append((x0, min(y0, y1), max(y0, y1)))
                elif abs(y0 - y1) < 0.05 and abs(x0 - x1) > 1.0 * m:
                    longs["h"].append((y0, min(x0, x1), max(x0, x1)))
    for dr in drawings:
        w = dr.get("width") or 0
        if dr.get("type") == "f" or w > 0.05 * m:  # fills / heavy pens are walls, not glazing
            continue
        segs = []
        for it in dr["items"]:
            if it[0] == "l":
                segs.append((it[1].x, it[1].y, it[2].x, it[2].y))
            elif it[0] == "re":
                r = it[1]
                segs += [(r.x0, r.y0, r.x1, r.y0), (r.x0, r.y1, r.x1, r.y1), (r.x0, r.y0, r.x0, r.y1),
                         (r.x1, r.y0, r.x1, r.y1)]
        for x0, y0, x1, y1 in segs:
            if abs(x0 - x1) < 0.05 and abs(y0 - y1) > 0.55 * m:
                a, off, lo, hi = "v", x0, min(y0, y1), max(y0, y1)
            elif abs(y0 - y1) < 0.05 and abs(x0 - x1) > 0.55 * m:
                a, off, lo, hi = "h", y0, min(x0, x1), max(x0, x1)
            else:
                continue
            if (hi - lo) > 3.5 * m:
                continue
            groups.setdefault(a, []).append((off, lo, hi, w))
    tol = 0.03 * m
    spans: list[tuple[str, list]] = []  # lines sharing one span (both ends within 3 cm)
    for a, rows in groups.items():
        rows.sort(key=lambda r: (r[1], r[2]))
        open_: list[list] = []
        for r in rows:
            open_ = [g for g in open_ if r[1] - g[0][1] <= tol]
            g = next((g for g in open_ if abs(g[0][2] - r[2]) <= tol), None)
            if g is None:
                g = [r]
                open_.append(g)
                spans.append((a, g))
            else:
                g.append(r)
    out = []
    for a, rows in spans:
        pen: dict = {}  # finest pen drawn at each offset
        for o, _l, _h, w in rows:
            pen[round(o, 2)] = min(pen.get(round(o, 2), 99.0), w)
        offs = sorted(pen)
        runs = [[offs[0]]]
        for o in offs[1:]:  # consecutive lines <= 0.35 m apart belong to one wall band
            if o - runs[-1][-1] <= 0.35 * m:
                runs[-1].append(o)
            else:
                runs.append([o])
        lo = min(r[1] for r in rows)
        hi = max(r[2] for r in rows)
        for rn in runs:
            spread = rn[-1] - rn[0]
            if not (3 <= len(rn) <= 4 and 0.08 * m <= spread <= 0.7 * m):
                continue  # 1-2 lines = a wall's faces / furniture; > 4 evenly spaced = stair treads
            if max(pen[o] for o in rn) > 0.36:
                continue  # glazing is drawn with the fine pen (DfE 0.24, EFA 0 / 0.28 pt); wall pens (USACE 0.48-1.56)
                # in the stack = a pilaster / wall thickening / pipe, not a window
            ext = 0.15 * m
            if any(abs(o - f) <= 0.02 * m and l0 <= lo - ext and h0 >= hi + ext
                   for f in (rn[0], rn[-1]) for o, l0, h0 in longs[a]):
                continue  # the wall's face line runs on through: a pilaster / column box on a wall (USACE A-101F)
            box = (rn[0], lo, rn[-1], hi) if a == "v" else (lo, rn[0], hi, rn[-1])
            out.append({"a": a, "lo": lo, "hi": hi, "offs": rn, "box": box, "len_m": round((hi - lo) / m, 2)})
    # one window drawn twice (two layers / rounding) -> one unit
    out.sort(key=lambda u: (u["a"], u["offs"][0], u["lo"]))
    keep: list[dict] = []
    for u in out:
        if any(k["a"] == u["a"] and abs(k["offs"][0] - u["offs"][0]) < 0.1 * m and
               min(k["hi"], u["hi"]) - max(k["lo"], u["lo"]) > 0.5 * min(k["hi"] - k["lo"], u["hi"] - u["lo"])
               for k in keep):
            continue
        keep.append(u)
    return keep


def _part_hit(words: list[str], text: str, every: bool = True) -> bool:
    """The part's words in a label: words as prefixes ('classroom' ~ 'Classrooms'), numbers as whole tokens ('1' is
    not '12'), every word (or any WORD when every=False; a number alone never names a part)."""
    t = text.lower()
    ok = [bool(re.search(rf"(?<![\d.]){w}(?![\d.])" if w.isdigit() else rf"\b{re.escape(w)}", t)) for w in words]
    if every:
        return bool(words) and all(ok)
    return any(o for w, o in zip(words, ok) if not w.isdigit())


def facade_units(units: list[dict], facade: dict, labels: list, scale_n: float,
                 disp: str | None = None, region: tuple | None = None) -> tuple[list[dict], str]:
    """Windows on one side of the building (or of a named part: 'the classroom wing' = where the 'Classroom' labels
    are): the side's lines run across it (west / east -> vertical lines); among the units level with the part, the
    outermost wall band on that side; then every unit on that same straight wall (chained, gaps <= 8 m). Top of the
    sheet = north unless told otherwise (the caller says so)."""
    m = 1.0 / (PT_M * scale_n)
    # the side of the DISPLAYED sheet that faces the asked direction (north arrow; default top = north)
    disp = disp or {"west": "left", "east": "right", "north": "top", "south": "bottom"}.get(facade["side"], "left")
    a = "v" if disp in ("left", "right") else "h"
    cand = [u for u in units if u["a"] == a]
    if not cand:
        return [], "no window units with lines along that side"
    span, why, ext = None, "the whole drawing", None
    if region is not None:  # a named room's traced outline (x0, y0, x1, y1)
        hits = [region]
        why = f"the room '{' '.join(facade['part'])}' (traced outline)"
    elif facade["part"] and labels:
        hits = [b for t, b in labels if _part_hit(facade["part"], t)] or \
            [b for t, b in labels if _part_hit(facade["part"], t, every=False)]
        if not hits:
            return [], f"no label names '{' '.join(facade['part'])}'"
        why = f"the part labelled '{' '.join(facade['part'])}' ({len(hits)} labels)"
    else:
        hits = []
    if hits:
        span = (min(b[1] for b in hits), max(b[3] for b in hits)) if a == "v" else \
            (min(b[0] for b in hits), max(b[2] for b in hits))
        # the part's own edge on the asked side: its facade lies beyond it, but not past another wing (<= 25 m)
        ext = {"left": min(b[0] for b in hits), "right": max(b[2] for b in hits), "top": min(b[1] for b in hits),
               "bottom": max(b[3] for b in hits)}[disp]
    level = [u for u in cand if span is None or (u["hi"] >= span[0] and u["lo"] <= span[1])]
    if ext is not None:
        slack = 1.0 * m  # a window in the room's own wall sits on its outline
        reach = slack if region is not None else 25 * m  # a room: its own wall only; a wing: its outer wall beyond
        if disp in ("left", "top"):
            level = [u for u in level if ext - reach <= u["offs"][0] <= ext + slack]
        else:
            level = [u for u in level if ext - slack <= u["offs"][-1] <= ext + reach]
    if not level:
        return [], "no window unit level with the named part on that side"
    outer = min if disp in ("left", "top") else max
    edge = outer(u["offs"][0] for u in level)
    band = [u for u in cand if abs(u["offs"][0] - edge) <= 0.3 * m]
    band.sort(key=lambda u: u["lo"])
    if region is not None:  # one room: its own wall, not the neighbours' along the same facade
        return [u for u in band if u in level], why
    seeds = [u for u in band if u in level]
    lo_i = band.index(seeds[0])
    hi_i = band.index(seeds[-1])
    while lo_i > 0 and band[lo_i]["lo"] - band[lo_i - 1]["hi"] <= 8 * m:
        lo_i -= 1
    while hi_i < len(band) - 1 and band[hi_i + 1]["lo"] - band[hi_i]["hi"] <= 8 * m:
        hi_i += 1
    return band[lo_i:hi_i + 1], why


def windows_profile(pdf: str, page: int, rooms: list[dict], scale_n: float, ppm: float = 40.0,
                    arcs: list[dict] | None = None) -> dict | None:
    """Window units in the walls round each traced room outline, read from a render (no detector, < 1 s): walk every
    outline edge and look across the wall band just outside it. A station is SOLID when an ink run starts at the wall
    face and runs through the wall band (fill, pier, jamb), GLAZED when the band holds >= 2 lines with paper between
    (glass / frame lines parallel to the wall), else OPEN (doorway, void). A window unit = a run of glazed stations >= 0.4 m; solid pieces
    shorter than 0.2 m (mullions, jamb ends) are bridged, longer ones (piers) split units. Only for FILLED walls: when
    under 25 % of the perimeter is solid, the walls themselves are line pairs that look like glazing -> None.
    Units within 0.4 m of a door swing (frames, thresholds), whose line pattern changes along the run, or with no
    solid wall / pier / jamb at either end (dashes of a grid line over a partition: Staff Room 4 -> 1) are dropped.
    -> {"units": [{"room", "bbox" (pdf_pt), "len_m"}], "solid_share", "band_m"}"""
    import numpy as np
    import pymupdf

    mpt = 1.0 / (PT_M * scale_n)  # PDF points per metre
    doors_at = [p for a in (arcs or []) for p in (a["c"], a["p0"], a["p1"])]
    xs = [p[0] for r in rooms for p in r["polygon"]]
    ys = [p[1] for r in rooms for p in r["polygon"]]
    with pymupdf.open(pdf) as doc:
        pg = doc[page]
        if pg.rotation:
            pg.set_rotation(0)
        W, H = pg.rect.width, pg.rect.height
        clip = (max(0.0, min(xs) - mpt), max(0.0, min(ys) - mpt), min(W, max(xs) + mpt), min(H, max(ys) + mpt))
        dpi = int(min(600, max(72, ppm * 0.0254 * scale_n)))
        pix = pg.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=pymupdf.Rect(*clip))
    g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    ink = g < 160
    k = dpi / 72.0  # px per pt
    ppm = mpt * k  # px per metre
    Hh, Ww = ink.shape

    def at(x, y):  # pdf pt -> ink
        c, r = int(round((x - clip[0]) * k)), int(round((y - clip[1]) * k))
        return bool(ink[r, c]) if 0 <= r < Hh and 0 <= c < Ww else False

    step = 1.0 / k  # one px in pt
    depth = int(0.65 * ppm)
    out_edges = []
    for room in rooms:
        poly = [tuple(p) for p in room["polygon"]]
        for a, b in zip(poly, poly[1:] + poly[:1]):
            L = math.dist(a, b)
            if L < 0.5 * mpt:
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            nx, ny = -uy, ux
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if _inside(poly, (mid[0] + nx * 0.3 * mpt, mid[1] + ny * 0.3 * mpt)):
                nx, ny = -nx, -ny  # outward
            st = []
            t = 0.1 * mpt
            while t <= L - 0.1 * mpt:
                px, py = a[0] + ux * t, a[1] + uy * t
                prof = [at(px + nx * (j - 2) * step, py + ny * (j - 2) * step) for j in range(depth)]
                runs, j = [], 0
                while j < len(prof):
                    if prof[j]:
                        j0 = j
                        while j < len(prof) and prof[j]:
                            j += 1
                        runs.append((j0, j))
                    j += 1
                st.append((t, runs))
                t += step
            out_edges.append((room, a, (ux, uy), (nx, ny), st))
    # wall band depth = median depth of the first ink run at the face, over stations where it is >= 0.08 m deep
    firsts = [r[0][1] - r[0][0] for *_, st in out_edges for _, r in st if r and r[0][0] <= 0.06 * ppm + 2]
    solid_runs = [d for d in firsts if d >= 0.08 * ppm]
    total = sum(len(st) for *_, st in out_edges)
    if not total or len(solid_runs) < 0.25 * total:
        return None
    band = float(np.median(solid_runs)) + 3
    units = []
    for room, a, (ux, uy), (nx, ny), st in out_edges:
        kinds = []
        sigs = {}
        for t, runs in st:
            inb = [(r0, min(r1, band)) for r0, r1 in runs if r0 < band]
            fill = sum(r1 - r0 for r0, r1 in inb) / band
            if inb and inb[0][0] <= 0.06 * ppm + 2 and inb[0][1] - inb[0][0] >= 0.85 * (band - 3):
                kinds.append((t, "S"))  # ink right through the wall band: fill, pier, jamb
            elif len(inb) >= 2 and fill <= 0.8:
                kinds.append((t, "G"))  # several lines across the band with paper between: glass / frame lines
                sigs[t] = tuple(int(r0 // 3) for r0, _ in inb)
            else:
                kinds.append((t, "O"))
        # bridge short solid / open pieces inside glazing; split at long solid pieces
        i, n = 0, len(kinds)
        while i < n:
            if kinds[i][1] != "G":
                i += 1
                continue
            j0 = i
            last_g = i
            j = i + 1
            while j < n:
                if kinds[j][1] == "G":
                    last_g = j
                elif (kinds[j][0] - kinds[last_g][0]) > (0.2 if kinds[j][1] == "S" else 0.15) * mpt:
                    break
                j += 1
            t0, t1 = kinds[j0][0], kinds[last_g][0]
            gs = [sigs[kinds[m][0]] for m in range(j0, last_g + 1) if kinds[m][1] == "G"]
            steady = max(gs.count(x) for x in set(gs)) / len(gs) if gs else 0.0
            near_door = any(_poly_dist([(a[0] + ux * t0, a[1] + uy * t0), (a[0] + ux * t1, a[1] + uy * t1)], c)
                            <= 0.4 * mpt for c in doors_at)
            ends = [kinds[m][1] for m in range(n) if kinds[m][1] == "S" and (
                0 < t0 - kinds[m][0] <= 0.2 * mpt or 0 < kinds[m][0] - t1 <= 0.2 * mpt)]
            if (t1 - t0) >= 1.0 * mpt and (t0 <= 0.3 * mpt or t1 >= kinds[-1][0] - 0.2 * mpt):
                ends.append("corner")  # a big run reaching the edge's end (a corner window; a leaky outline vertex)
            if (t1 - t0) >= 0.4 * mpt and steady >= 0.5 and not near_door and ends:
                pts = [(a[0] + ux * tt + nx * dd, a[1] + uy * tt + ny * dd) for tt in (t0, t1)
                       for dd in (0.0, band / ppm * mpt)]
                units.append({"room": room["label"], "len_m": round((t1 - t0) / mpt, 2),
                              "bbox": [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                                       max(p[1] for p in pts)]})
            i = last_g + 1
    return {"units": units, "solid_share": round(len(solid_runs) / total, 2), "band_m": round(band / ppm, 2)}


def _poly_dist(poly: list, p: tuple) -> float:
    """Distance from p to the polygon's boundary (pdf_pt)."""
    best = 1e18
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / L2))
        best = min(best, math.hypot(p[0] - x0 - t * dx, p[1] - y0 - t * dy))
    return best


def _door_tags_in(pdf: str, cells, sel, mat) -> list | None:
    """G1's US door-tag reader (label_count.door_tags) on page 0, kept to the selected view cells -> [(tag, box)], or
    None when the sheet shows no door-tag convention."""
    try:
        from stoa.solvers.label_count import door_tags, text_lines

        got = door_tags(text_lines(pdf, os.stat(pdf).st_mtime))
    except Exception:  # noqa: BLE001
        return None
    if not got:
        return None
    tags = got[0]
    if cells is not None and sel is not None:
        keep = []
        for t, bb in tags:
            ctr = _to_display(mat, ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2))
            c = OpeningsSolver._in_cells(ctr, cells)
            if c is not None and any(c is s_ for s_ in sel):
                keep.append((t, bb))
        tags = keep
    return tags or None


# ---------------------------------------------------------------------------------------------------------------------
# V1's detector (CPU service)
_DET_DEADLINE: list[float] = [0.0]  # per-question detector budget (monotonic deadline), set by OpeningsSolver.solve
_DET_NOTE: list[str] = [""]  # why the last detector call gave nothing ('busy', 'over budget', 'down')


def _det_budget_start() -> None:
    # S0 19:45 (d25 up_lone p95 36 -> 142 s): every lone window row waited the full 240 s on a CPU service with 27
    # requests in flight. One budget per question (STOA_DR_DET_TIMEOUT, default 25 s) shared by all calls.
    _DET_DEADLINE[0] = time.monotonic() + float(os.environ.get("STOA_DR_DET_TIMEOUT", "25"))
    _DET_NOTE[0] = ""


def _det_health(url: str) -> dict | None:
    """V1's /health (None when it does not answer within 2 s)."""
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _det_busy(url: str) -> int | None:
    """Requests in flight when the service is FULL, else 0 (None = down). V1 7eb7f72 reports max_queue and answers 503
    at once when full; older servers (the demo's :8143) do not, so our own limit STOA_DR_DET_MAX_QUEUE applies."""
    h = _det_health(url)
    if h is None:
        return None
    n = int(h.get("inflight", 0) or 0)
    full = int(h.get("max_queue") or os.environ.get("STOA_DR_DET_MAX_QUEUE", "4"))
    return n if n >= full else 0


def _det_client(path: str, zoom: float, min_score: float, timeout: float | None = None) -> dict | None:
    import urllib.error
    import urllib.request

    url = os.environ.get("STOA_DET_URL", "http://127.0.0.1:8140")
    left = (_DET_DEADLINE[0] - time.monotonic()) if _DET_DEADLINE[0] else 25.0
    if timeout is not None:
        left = min(left, timeout)
    if left < 2:
        _DET_NOTE[0] = "the question's detector budget is spent"
        return None
    try:  # P1 690a8be: in private mode a detector URL off this machine / our network is refused (no request)
        from stoa import llm as _llm

        if hasattr(_llm, "check_private_url"):
            _llm.check_private_url(url, "door detector")
    except ImportError:
        pass
    except RuntimeError as e:
        _DET_NOTE[0] = str(e)[:120]
        return None
    h = _det_health(url)
    if h is None:
        _DET_NOTE[0] = "the detector service is down"
        return None
    busy = int(h.get("inflight", 0) or 0)
    if h.get("max_queue") is None and busy >= int(os.environ.get("STOA_DR_DET_MAX_QUEUE", "4")):
        # an old service queues everything: a queued request would not come back within the budget, and an abandoned
        # one still costs it CPU (a service with max_queue answers 503 itself and still serves its cache hits)
        _DET_NOTE[0] = f"the detector service is busy ({busy} requests in flight)"
        return None
    req = urllib.request.Request(url + "/detect", data=json.dumps({"path": path, "zoom": zoom, "min_score": min_score})
                                 .encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=left) as r:
            res = json.loads(r.read())
        _det_charge(res)
        return res
    except urllib.error.HTTPError as e:
        _DET_NOTE[0] = ("the detector service is busy (it turned the request away)" if e.code == 503 else
                        f"the detector failed (HTTP {e.code})")
        return None
    except TimeoutError:
        _DET_NOTE[0] = f"the detector did not answer within {left:.0f} s"
        return None
    except Exception as e:  # noqa: BLE001
        _DET_NOTE[0] = f"the detector failed ({type(e).__name__}: {str(e)[:60]})"
        return None


def _det_charge(res: dict) -> None:
    """P1 cc7c363: the detector's CPU time is charged to the question (stoa.cost.record_service, DET_THREADS 16). The
    service's reply carries the model run's infer_seconds also on its own cache hits, so only a 'computed' reply pays."""
    try:
        from stoa import cost as _cost

        if hasattr(_cost, "record_service") and res.get("cache", "computed") == "computed":
            _cost.record_service(res.get("infer_seconds") or 0, threads=16)
    except Exception:  # noqa: BLE001 - metering must never fail an answer
        pass


def _det_why() -> str:
    return f"door/window detector gave nothing: {_DET_NOTE[0] or 'no answer'}"


def detect_pdf_clip(pdf: str, page: int, clip_unrot: tuple, scale_n: float, min_score: float = 0.5,
                    target_ppm: float = 50.0) -> dict | None:
    """V1's recipe: render the clip at >= 150 dpi, let the service shrink it to ~50 px/m, boxes back in PDF points."""
    import hashlib

    import pymupdf

    need = target_ppm * 0.0254 * scale_n
    dpi = int(min(600, max(150, need)))
    cache = Path(os.environ.get("JUNCTION_SHARED", "/tmp")) / "cache" / "det"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{os.path.abspath(pdf)}|{os.path.getmtime(pdf)}|{page}|{dpi}|{clip_unrot}".encode()).hexdigest()
    png = cache / f"dr_{key[:16]}.png"
    with pymupdf.open(pdf) as doc:
        pg = doc[page]
        rot = pg.rotation
        if not png.exists():
            if rot:
                pg.set_rotation(0)
            pg.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=pymupdf.Rect(*clip_unrot)).save(str(png))
    ppm = dpi / 0.0254 / scale_n
    zoom = min(1.0, target_ppm / ppm)
    r = _det_client(str(png), zoom, min_score)
    if r is None:
        return None
    f = 72.0 / dpi
    boxes = []
    for b in r.get("boxes", []):
        bb = [b[0] * f + clip_unrot[0], b[1] * f + clip_unrot[1], b[2] * f + clip_unrot[0], b[3] * f + clip_unrot[1]]
        boxes.append(bb + [b[4], b[5]])
    return {"boxes": boxes, "ppm": ppm * zoom, "seconds": r.get("seconds")}


# ---------------------------------------------------------------------------------------------------------------------
@register("openings")
class OpeningsSolver(BaseSolver):
    name = "openings"
    extensions = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")

    def can_handle(self, path: str, question: str) -> bool:
        return super().can_handle(path, question) and (parse_question(question) is not None
                                                       or bool(_BOTH_Q.search(question)))

    def solve(self, path: str, question: str) -> Answer:
        both = _BOTH_Q.search(question)
        if both:
            return self._both(path, question, both)
        q = parse_question(question)
        if q is None:
            return self.abstain("not a door / window count")
        kind = _TYPE_Q.search(question)
        if kind:
            # a door / window TYPE is not visible in a swing or a window symbol ('garage' would even match a room
            # label 'Garage' and count its swing doors): a schedule or the door / window tags name the types
            return self.abstain(f"'{kind.group(0).strip()}' is a {q['target']} type: swings and window symbols do not "
                                "show it (a door / window schedule or the tags on the plan would)")
        _det_budget_start()
        pdf = str(self.abspath(path))
        if not pdf.lower().endswith(".pdf"):
            if os.environ.get("STOA_DR_IMAGES", "1") == "0":
                return self.abstain("images are off (STOA_DR_IMAGES=0)")
            return self._image(pdf, path, question, q)
        pages, how = pick_pages(pdf, question)
        answers = []
        for pno in pages:
            try:
                from stoa.evidence.linework import sheet_scales

                scales = sheet_scales(pdf, pno)
            except Exception:  # noqa: BLE001
                scales = ()
            scale_n = scales[0] if scales else None
            qq = dict(q, multi=len(pages) > 1)
            a = (self._doors if q["target"] == "door" else self._windows)(pdf, path, question, qq, scale_n, pno)
            answers.append((pno, a))
        if len(answers) == 1:
            a = answers[0][1]
            if how:
                a.trace = [how, *a.trace]
            return a
        got = [(p, a) for p, a in answers if a.value is not None]
        if not got:
            return answers[0][1]
        total = sum(int(a.value) for _, a in got)
        ev = [e for _, a in got for e in a.evidence]
        trace = [how] + [f"page {p + 1}: {a.value} ({(a.trace or [''])[-1][:80]})" for p, a in got]
        assumptions = [f"summed over {len(got)} plan pages of this PDF" + (f" ({len(answers) - len(got)} page(s) without"
                       " a reading)" if len(got) < len(answers) else "")]
        for _, a in got:
            assumptions += [x for x in a.assumptions if x not in assumptions]
        return Answer(value=total, unit="count", confidence=round(min(a.confidence for _, a in got) - 0.05, 2),
                      evidence=ev, trace=trace, assumptions=assumptions, missing=got[0][1].missing)

    # -- scope ------------------------------------------------------------------------------------------------------
    def _cells(self, pdf: str, q: dict, rect: tuple, trace: list, assumptions: list, page: int = 0):
        W, H = rect[2] - rect[0], rect[3] - rect[1]
        titles = view_titles(pdf, page)
        cells = view_cells(titles, W, H)
        plans = [c for c in cells if c["kind"] == "plan"]
        others = [c for c in cells if c["kind"] != "plan"]
        if not cells:
            trace.append("openings: no view titles found -> the whole sheet is one plan")
            return None, None
        if len(plans) == 1 and not others:
            # one plan title and nothing else: a single-view sheet whose title may sit in the title block (DfE 'Special
            # School Plan Level 00'), so its 'cell' says nothing about where the view is
            trace.append(f"openings: one view on the sheet ({plans[0]['text'][:50]!r}) -> the whole sheet")
            if _scope_match(q["scope"], plans):  # 'the filter building' = 'FILTER BUILDING FLOOR PLAN'
                q["view_named"] = True
            return None, None
        named = _scope_match(q["scope"], plans)
        if named:
            trace.append("openings: the question names view(s) " + "; ".join(repr(c["text"]) for c in named))
            q["view_named"] = True
            return named, cells
        if plans:
            trace.append(f"openings: {len(plans)} plan view(s): " + "; ".join(repr(c["text"]) for c in plans)
                         + (f"; left out {len(others)} other view(s) (RCP / axon / elevation / section / detail)"
                            if others else ""))
            if others:
                assumptions.append("counted only the floor-plan views (" + ", ".join(sorted({c['kind'] for c in others}))
                                   + " views of the same sheet left out)")
            return plans, cells
        trace.append("openings: view titles found but none is a floor plan -> whole sheet")
        q["no_plan"] = True
        return None, cells

    @staticmethod
    def _in_cells(pt_disp, cells) -> dict | None:
        for c in cells:
            x0, y0, x1, y1 = c["cell"]
            if x0 <= pt_disp[0] <= x1 and y0 <= pt_disp[1] <= y1:
                return c
        return None

    # -- doors ------------------------------------------------------------------------------------------------------
    def _doors(self, pdf, path, question, q, scale_n, page: int = 0) -> Answer:
        trace: list[str] = []
        assumptions: list[str] = []
        sw = find_swings(pdf, page, scale_n)
        arcs = sw["arcs"]
        trace.append(f"openings: {len(arcs)} door-swing arcs on the vector page (printed scale "
                     f"{'1:%g' % scale_n if scale_n else 'none'})")
        assumptions += sw["notes"]
        if not arcs:
            return self.abstain("no door-swing arcs in the vector paths", trace)
        if q.get("facade"):
            eff = sw["scale_n"] or scale_n
            if not eff:
                return self.abstain("no drawing scale to find the facade's wall", trace)
            vu = vector_windows(pdf, page, eff) if os.environ.get("STOA_DR_VWIN", "1") != "0" else []
            return self._facade(pdf, page, path, q, vu if len(vu) >= 4 else None, eff, trace, assumptions)
        doors = group_doors(arcs)
        # two leafless arcs whose closed positions meet = a double door drawn without leaf lines (DfE stair pair);
        # a lone leafless arc stays out (curved walls, revolving symbols)
        doors += [d for d in group_doors(sw.get("leafless") or []) if d["kind"] == "double"]
        n_swing = len(doors)
        if not q.get("swing_only"):  # 'doors drawn with a swing arc' / 'swing doors' = arcs only
            doors += _arcless(pdf, page, sw["scale_n"] or scale_n)
        if len(doors) > n_swing:
            trace.append(f"openings: + {len(doors) - n_swing} door(s) drawn without a swing arc ("
                         + ", ".join(sorted({d['kind'] for d in doors[n_swing:]})) + ")")
        rect = sw["rect"]
        mat = sw["mat"]
        sel, all_cells = self._cells(pdf, q, rect, trace, assumptions, page)
        if q.get("no_plan") and q.get("multi"):
            return self.abstain("a page of the set with no floor-plan view (sections / elevations / details)", trace)
        if sel is not None:
            # every door that falls in a cell of the WHOLE grid goes to that cell; keep the selected ones
            kept = []
            for d in doors:
                c = self._in_cells(_to_display(mat, d["center"]), all_cells)
                if c is not None and any(c is s for s in sel):
                    kept.append((d, c))
            doors_in = kept
        else:
            doors_in = [(d, None) for d in doors]
        room_note = ""
        if q["scope"] and not q.get("view_named") and _whole_scope(q["scope"]):
            assumptions.append(f"'{' '.join(q['scope'])}' read as the whole drawing: counted every floor-plan view")
        elif q["scope"] and not q.get("view_named"):
            rooms = named_rooms(pdf, page, question, sw["scale_n"]) if sw["scale_n"] else None
            if rooms == []:
                return self.abstain("the room named by the question has no traceable outline", trace)
            if rooms:
                tol = 0.45 / (PT_M * sw["scale_n"])  # hinge on the room's wall line (inner face + wall thickness)
                doors_in = [(d, c) for d, c in doors_in
                            if any(_on_edge(r["polygon"], ((u[0] + v[0]) / 2, (u[1] + v[1]) / 2), tol)
                                   for r in rooms for u, v in doorways(d))]
                if not doors_in:
                    # X1 21:22 (AFCEE A-103 'module 07 support floor plan': a VIEW title traced as a room -> 0 @0.70):
                    # a room is entered through a door, so an outline with no doorway on it is a wrong outline
                    return self.abstain("no door lies on the traced outline of " + " + ".join(
                        repr(r["label"]) for r in rooms) + " (a room has at least one door: the outline or the name "
                        "match is probably wrong)", trace)
                room_note = " in " + " + ".join(repr(r["label"]) for r in rooms)
                trace.append(f"openings: room scope{room_note}: doors whose doorway lies on an edge of the traced room "
                             "outline")
                for r in rooms:  # the outline is context, not a counted object: trace only (evidence = 1 box/door)
                    xs, ys = [p[0] for p in r["polygon"]], [p[1] for p in r["polygon"]]
                    trace.append(f"openings: room {r['label']!r} traced at pdf_pt "
                                 f"[{min(xs):.0f}, {min(ys):.0f}, {max(xs):.0f}, {max(ys):.0f}]")
            elif not _whole_scope(q["scope"]) and not (sel is None and _sheet_named(pdf, page, q["scope"])):
                return self.abstain(f"'{' '.join(q['scope'])}' names neither a plan view nor a room label found on this "
                                    "sheet", trace)
            else:
                assumptions.append(f"'{' '.join(q['scope'])}' read as the whole drawing: counted every floor-plan view")
        if q.get("side") and os.environ.get("STOA_DR_EXTERIOR", "0") != "1":
            # DR c1: the flood fill from the margin is stopped by stoops / dimension lines (A-101F: 3 of 5 exterior
            # doors found), so it is OFF until it is measured right; abstaining lets other readers try
            return self.abstain(f"{q['side']} doors are not separated reliably yet (STOA_DR_EXTERIOR=1 to try)", trace)
        if q.get("side"):
            ext = exterior_flags(pdf, page, [d for d, _ in doors_in], sw["scale_n"]) if sw["scale_n"] else None
            if ext is None:
                return self.abstain("could not tell inside from outside (no closed building outline)", trace)
            want = q["side"] == "exterior"
            doors_in = [(d, c) for (d, c), e in zip(doors_in, ext) if e == want]
            trace.append(f"openings: {q['side']} doors = doorway between the building and the outside "
                         f"({sum(ext)} of {len(ext)} doors open to the outside)")
            assumptions.append("exterior door = its doorway separates the outside (reached from the sheet margin round "
                               "the building) from a room; doors to an enclosed courtyard count as interior")
        if q["double"]:
            chosen = [(d, c) for d, c in doors_in if d["leaves"] == 2]
        elif q["single"]:
            chosen = [(d, c) for d, c in doors_in if d["leaves"] == 1]
        else:
            chosen = doors_in
        n_doors = len(chosen)
        n_leaves = sum(d["leaves"] for d, _ in chosen)
        n_double = sum(1 for d, _ in chosen if d["leaves"] == 2)
        ev = []  # one box per counted object (a door, or a leaf when the question counts leaves)
        for k, (d, c) in enumerate(chosen, 1):
            label = {"double": "double door (2 leaves)", "single": "door", "double-acting": "double-acting door",
                     "folding": "folding / louvred door (2 leaves, no swing arc)",
                     "angled": "door drawn half-open (no swing arc)",
                     "sliding": "sliding door (arrows, no swing arc)"}[d["kind"]]
            where = f" · {c['text'][:40]}" if c else ""
            if q["leaves"] and d["leaves"] == 2 and d["arcs"]:
                for j, a in enumerate(d["arcs"], 1):
                    xs, ys = [p[0] for p in (a["c"], a["p0"], a["p1"])], [p[1] for p in (a["c"], a["p0"], a["p1"])]
                    ev.append(Evidence(file=path, page=page, bbox=[round(min(xs), 1), round(min(ys), 1), round(max(xs), 1),
                                                                round(max(ys), 1)], bbox_space="pdf_pt", exact=True,
                                       note=f"leaf {j} of double door {k}{where}"))
                continue
            wid = f" · opening {opening_width_m(d, sw['scale_n']):.2f} m" if sw.get("scale_n") else ""
            ev.append(Evidence(file=path, page=page, bbox=[round(v, 1) for v in d["bbox"]], bbox_space="pdf_pt",
                               exact=True, note=f"{label} {k}{where}{wid}"))
        if q["leaves"]:
            value, what = n_leaves, f"{n_leaves} door leaves ({n_doors} doors)"
        elif q["double"]:
            value, what = n_doors, f"{n_doors} double doors"
        else:
            value = n_doors
            what = (f"{n_doors} doors ({n_leaves} leaves)" if n_double else f"{n_doors} door" + ("" if n_doors == 1 else "s"))
        trace.append(f"openings: {what}{room_note}; {n_double} double, door swings grouped from {len(arcs)} arcs")
        # captain 20:52: say what was counted (door sets or leaves) and where (room / view / sheet)
        scope_txt = (room_note.strip() if room_note else
                     ("in " + "; ".join(repr(c["text"][:50]) for c in sel)) if sel else "on the whole sheet")
        assumptions.insert(0, f"Counted {'door LEAVES' if q['leaves'] else 'door SETS (a double door = 1)'} "
                              f"{scope_txt}: {n_doors} door{'s' if n_doors != 1 else ''}, {n_leaves} "
                              f"lea{'ves' if n_leaves != 1 else 'f'}")
        assumptions.append("Convention: a double door (two leaves meeting on one doorway) counts as ONE door with 2 leaves; "
                           "doors are read from their symbols: swing arcs, folding / louvred leaves, half-open "
                           "leaves of labelled rooms (cupboards left out) and sliding arrows; overhead / roll-up "
                           "doors (no symbol in the wall) are not counted")
        conf = 0.7
        if sel is None and all_cells:
            conf = 0.55
        if any("assumed" in a or "another paper size" in a for a in sw["notes"]):
            conf -= 0.1
        missing = ["a door schedule to confirm the count"] if n_doors else []
        # cross-check (evidence order (a)): US door TAGS (room number + letter) in the same views; agreement = two
        # independent readings of the drawing (QA 18:56: a lone tag count @0.6 was overridden by Gemini's 5 vs gold 6)
        if page == 0 and not room_note and not (q["leaves"] or q["double"] or q["single"]):
            tags = _door_tags_in(pdf, all_cells if sel is not None else None, sel, mat)
            if tags is not None:
                if len(tags) == n_doors:
                    conf = max(conf, 0.9)
                    missing = []
                    trace.append(f"openings: agrees with the {len(tags)} door tags printed in the same view(s) "
                                 f"({', '.join(t for t, _ in tags[:12])})")
                else:
                    conf = min(conf, 0.55)
                    trace.append(f"openings: the sheet prints {len(tags)} door tags in the same view(s) "
                                 f"({', '.join(t for t, _ in tags[:12])}) vs {n_doors} swings: check")
                    missing = [f"the door schedule: {len(tags)} door tags vs {n_doors} drawn swings"]
        return Answer(value=value, unit="count", confidence=round(conf, 2), evidence=ev,
                      trace=trace, assumptions=assumptions, missing=missing)

    # -- windows ----------------------------------------------------------------------------------------------------
    def _windows(self, pdf, path, question, q, scale_n, page: int = 0) -> Answer:
        trace: list[str] = []
        assumptions: list[str] = []
        import pymupdf

        with pymupdf.open(pdf) as doc:
            pg = doc[page]
            rect = tuple(pg.rect)
            inv = tuple(~pymupdf.Matrix(pg.rotation_matrix))
            unrot = (pg.cropbox.width, pg.cropbox.height) if pg.rotation % 180 else (pg.rect.width, pg.rect.height)
        # effective scale from the door swings (also fixes '1:200 @ A1' on A3)
        sw = find_swings(pdf, page, scale_n)
        eff = sw["scale_n"] or scale_n
        assumptions += sw["notes"]
        if not eff:
            return self.abstain("no drawing scale for the window detector", trace)
        vu = None
        kind = None
        try:
            from stoa.router.recognize import recognize

            kind = recognize(pdf).get("kind")
        except Exception:  # noqa: BLE001
            pass
        if kind == "swiss_plan":
            # Swiss dwelling sheets (up_lone, DXF-derived golds): their furniture / balcony lines also stack in 3s
            # (vector units 1/16 right, 5 over-counts) and pdf_vector reads their windows: stand aside
            trace.append("openings: Swiss-style dwelling sheet: vector window units not used (pdf_vector reads these)")
        elif os.environ.get("STOA_DR_VWIN", "1") != "0":
            try:
                vu = vector_windows(pdf, page, eff)
            except Exception as e:  # noqa: BLE001
                trace.append(f"openings: vector window units failed ({type(e).__name__})")
            if vu is not None and len(vu) < 4:
                vu = None  # this sheet does not draw its windows as stacked glazing lines (or has almost none)
            if vu is not None:
                trace.append(f"openings: {len(vu)} window units drawn in the walls (2-4 parallel glazing lines sharing "
                             "one span), read from the vector linework")
        if q.get("facade"):
            return self._facade(pdf, page, path, q, vu, eff, trace, assumptions)
        sel, all_cells = self._cells(pdf, q, rect, trace, assumptions, page)
        rooms = None
        room_ev = []
        if q["scope"] and not q.get("view_named") and _whole_scope(q["scope"]):
            assumptions.append(f"'{' '.join(q['scope'])}' read as the whole drawing: counted every floor-plan view")
        elif q["scope"] and not q.get("view_named"):
            rooms = named_rooms(pdf, page, question, eff)
            if rooms == []:
                return self.abstain("the room named by the question has no traceable outline", trace)
            if rooms is None and not _whole_scope(q["scope"]):
                return self.abstain(f"'{' '.join(q['scope'])}' names neither a plan view nor a room label found on this "
                                    "sheet", trace)
            if rooms is None:
                assumptions.append(f"'{' '.join(q['scope'])}' read as the whole drawing: counted every floor-plan view")
        clips = []
        if rooms:
            # only the named rooms' neighbourhood goes to the detector (1.5 m round the traced outline: the walls)
            m = 1.5 / (PT_M * eff)
            xs = [p[0] for r in rooms for p in r["polygon"]]
            ys = [p[1] for r in rooms for p in r["polygon"]]
            clips.append((None, (max(0.0, min(xs) - m), max(0.0, min(ys) - m), min(float(unrot[0]), max(xs) + m),
                                 min(float(unrot[1]), max(ys) + m))))
            for r in rooms:  # context, not a counted object: trace only (evidence = one box per window)
                rx, ry = [p[0] for p in r["polygon"]], [p[1] for p in r["polygon"]]
                trace.append(f"openings: room {r['label']!r} traced at pdf_pt "
                             f"[{min(rx):.0f}, {min(ry):.0f}, {max(rx):.0f}, {max(ry):.0f}]")
        elif sel:
            for c in sel:
                x0, y0, x1, y1 = c["cell"]
                a, b = _to_display(inv, (x0, y0)), _to_display(inv, (x1, y1))
                clips.append((c, (max(0, min(a[0], b[0])), max(0, min(a[1], b[1])),
                                  min(unrot[0], max(a[0], b[0])), min(unrot[1], max(a[1], b[1])))))
        elif sw["arcs"]:
            # the building, not the sheet: door swings + 8 m (windows sit in walls near rooms; title blocks, notes and
            # margins stay out; EFA A3 whole page 224 s under load)
            m = 8.0 / (PT_M * eff)
            xs = [p[0] for a in sw["arcs"] for p in (a["c"], a["p0"], a["p1"])]
            ys = [p[1] for a in sw["arcs"] for p in (a["c"], a["p0"], a["p1"])]
            clips.append((None, (max(0.0, min(xs) - m), max(0.0, min(ys) - m), min(float(unrot[0]), max(xs) + m),
                                 min(float(unrot[1]), max(ys) + m))))
            assumptions.append("windows looked for within 8 m of the drawn doors (the building's extent)")
        else:
            clips.append((None, (0.0, 0.0, float(unrot[0]), float(unrot[1]))))
        prof = None
        if rooms and os.environ.get("STOA_DR_WIN_PROFILE", "1") != "0":
            # the wall drawing itself (< 1 s, no detector): glass lines between wall pieces round the room; used only
            # to CONFIRM the detector's count (agreement -> 0.8)
            prof = windows_profile(pdf, page, rooms, eff, arcs=sw["arcs"])
            if prof is not None:
                trace.append(f"openings: wall profile round {' + '.join(repr(r['label']) for r in rooms)}: "
                             f"{len(prof['units'])} window unit(s) ({', '.join(str(u['len_m']) for u in prof['units'])}"
                             f" m long; wall band {prof['band_m']} m, {100 * prof['solid_share']:.0f}% solid)")
            else:
                trace.append("openings: wall profile not usable here (walls are not filled: line-pair walls look like "
                             "glazing)")
        vsel = None
        if vu is not None:
            ctr = lambda u: ((u["box"][0] + u["box"][2]) / 2, (u["box"][1] + u["box"][3]) / 2)  # noqa: E731
            if rooms:
                tol = 0.7 / (PT_M * eff)
                vsel = [u for u in vu if any(_on_edge(r["polygon"], ctr(u), tol) for r in rooms)]
            else:
                vsel = [u for u in vu if any(cl[0] <= ctr(u)[0] <= cl[2] and cl[1] <= ctr(u)[1] <= cl[3]
                                             for _c, cl in clips)]
            trace.append(f"openings: {len(vsel)} of the {len(vu)} vector window units lie in the asked scope")
        if vsel is not None and not rooms and os.environ.get("STOA_DR_VWIN_DET", "0") != "1":
            clips = []  # views / whole plan: the vector units answer; no detector render of a whole view (minutes)
        boxes = []
        det_ok = True
        if vsel is not None and clips:
            # a cross-check only: the drawn units already answer, so the detector gets at most ~10 s of the budget
            _DET_DEADLINE[0] = min(_DET_DEADLINE[0] or 1e18, time.monotonic()
                                   + float(os.environ.get("STOA_DR_XCHECK_TIMEOUT", "10")))
        for c, clip in clips:
            r = detect_pdf_clip(pdf, page, clip, eff)
            if r is None:
                if vsel is not None:
                    det_ok = False
                    trace.append(f"openings: detector cross-check skipped ({_det_why()})")
                    break
                # the wall profile alone is not trusted: its count moves with the render resolution / scale (EFA
                # English 1-3 at 1:375 30/40/50 px/m: 5/6/6, at 1:400: 5/6/2; Staff Room 4/1/4)
                return self.abstain(_det_why(), trace)
            # a window unit is a strip IN a wall: score >= WIN_SCORE (V1: windows P 0.98 at >= 0.8) and not a hairline
            # (EFA English 1: a 0.14 m x 1 m sliver on a void edge scored 0.52)
            short = lambda b: min(b[2] - b[0], b[3] - b[1]) * PT_M * eff  # noqa: E731
            boxes += [(b, c) for b in r["boxes"] if b[5] == "window" and b[4] >= WIN_SCORE and short(b) >= 0.2]
            where = ("view " + repr(c["text"][:40])) if c else ("the named room(s)" if rooms else "the whole sheet")
            trace.append(f"openings: detector on {where} at {r['ppm']:.0f} px/m: "
                         f"{sum(1 for b in r['boxes'] if b[5] == 'window')} windows ({r.get('seconds')} s)")
        if rooms:
            tol = 0.7 / (PT_M * eff)  # a window unit sits in the room's wall: centre within ~wall thickness
            boxes = [(b, c) for b, c in boxes
                     if any(_on_edge(r["polygon"], ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), tol) for r in rooms)]
            trace.append("openings: room scope " + " + ".join(repr(r["label"]) for r in rooms)
                         + ": window boxes on an edge of the traced room outline")
        conf, missing = 0.5, ["a window schedule to confirm the count"]
        if vsel is not None:
            n_det = len(boxes) if (clips and det_ok) else None
            n_prof = len(prof["units"]) if prof is not None else None
            conf = 0.6 if rooms else 0.5  # a whole view / plan: glazed screens and partitions can look alike
            if n_det is not None and n_det == len(vsel):
                conf = 0.85
                trace.append(f"openings: V1's symbol detector agrees ({n_det})")
            elif n_det is not None:
                trace.append(f"openings: V1's symbol detector reads {n_det} (the drawn units are used)")
            if n_prof is not None and n_prof == len(vsel):
                conf = max(conf, 0.75)
            ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in u["box"]], bbox_space="pdf_pt", exact=True,
                           note=f"window {k}: {len(u['offs'])} glazing lines, {u['len_m']} m")
                  for k, u in enumerate(sorted(vsel, key=lambda u: (u["box"][1], u["box"][0])), 1)] + room_ev
            assumptions.append("windows = window units drawn in the walls: an opening bridged by parallel glazing lines; "
                               "piers between openings separate units")
            trace.append(f"openings: {len(vsel)} window{'s' if len(vsel) != 1 else ''}")
            return Answer(value=len(vsel), unit="count", confidence=conf, evidence=ev, trace=trace,
                          assumptions=assumptions, missing=missing)
        if prof is not None:
            n_prof = len(prof["units"])
            if n_prof == len(boxes):
                conf = 0.8  # two independent readings (symbol detector, wall drawing) give the same count
                trace.append(f"openings: the wall drawing agrees ({n_prof} glazed runs between wall pieces)")
            else:
                trace.append(f"openings: the wall drawing reads {n_prof} glazed runs (not used: resolution-sensitive)")
        ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in b[:4]], bbox_space="pdf_pt", exact=False,
                       note=f"window {k} ({b[4]:.2f})") for k, (b, _c) in enumerate(boxes, 1)] + room_ev
        assumptions.append("windows = window units drawn in the walls, found by a symbol detector (V1) on the plan view")
        return Answer(value=len(boxes), unit="count", confidence=conf, evidence=ev, trace=trace,
                      assumptions=assumptions, missing=missing)

    def _facade(self, pdf, page, path, q, vu, eff, trace, assumptions) -> Answer:
        """'How many windows are on the west facade of the classroom wing?' (Stoa dfe-08, expert 18): the vector window
        units on the outermost wall band of that side, level with the part the question names (its room labels)."""
        fac = q["facade"]
        if vu is None:
            return self.abstain("a facade count needs window units drawn in the walls; this sheet has none readable",
                                trace)
        import pymupdf

        with pymupdf.open(pdf) as doc:
            labels = [(b[4].replace("\n", " "), tuple(b[:4])) for b in doc[page].get_text("blocks") if b[4].strip()]
        disp, arrow = None, None
        if fac["word"] in ("north", "south", "east", "west"):
            try:  # VP: the sheet's north arrow -> which displayed side faces the asked direction
                from stoa.sheet import north_arrow

                arrow = north_arrow(pdf, page)
            except Exception:  # noqa: BLE001 - older stoa.sheet without it: top = north
                arrow = None
            if arrow and arrow.get("found"):
                disp = arrow["sides"].get(fac["side"])
                trace.append(f"openings: north arrow on the sheet ({arrow.get('angle_deg', 0):.0f} deg): "
                             f"{fac['side']} = the {disp} side of the sheet")
            else:
                assumptions.append(f"no north arrow read: the top of the sheet is taken as north, so '{fac['side']}' = "
                                   f"the {({'west': 'left', 'east': 'right', 'north': 'top', 'south': 'bottom'})[fac['side']]}"
                                   " side")
        region = None
        if fac["part"]:
            hits = [b for t, b in labels if _part_hit(fac["part"], t)]
            if 1 <= len(hits) <= 2:
                # one room named ('the multi-purpose hall'): its traced outline is the part, not its label's line
                try:
                    rooms = named_rooms(pdf, page, f"How many windows does the {' '.join(fac['part'])} have?", eff)
                except Exception:  # noqa: BLE001
                    rooms = None
                if rooms:
                    xs = [p[0] for r in rooms for p in r["polygon"]]
                    ys = [p[1] for r in rooms for p in r["polygon"]]
                    region = (min(xs), min(ys), max(xs), max(ys))
        units, why = facade_units(vu, fac, labels, eff, disp, region)
        if not units:
            return self.abstain(f"the {fac['word']} facade: {why}", trace)
        trace.append(f"openings: {fac['side']} facade of {why}: {len(units)} window units on its outermost wall band")
        if q["target"] == "door":
            # doors IN that wall: a swing whose doorway lies on the facade's band (found from its windows), along the
            # same stretch of wall (first window - 8 m .. last window + 8 m)
            m = 1.0 / (PT_M * eff)
            a = units[0]["a"]
            across = (min(u["offs"][0] for u in units) - 0.45 * m, max(u["offs"][-1] for u in units) + 0.45 * m)
            along = (min(u["lo"] for u in units) - 8 * m, max(u["hi"] for u in units) + 8 * m)
            sw = find_swings(pdf, page, eff)
            hits = []
            for d in group_doors(sw["arcs"]):
                for u, v in doorways(d):
                    mx, my = (u[0] + v[0]) / 2, (u[1] + v[1]) / 2
                    c_, l_ = (mx, my) if a == "v" else (my, mx)
                    if across[0] <= c_ <= across[1] and along[0] <= l_ <= along[1]:
                        hits.append(d)
                        break
            if q["double"]:
                hits = [d for d in hits if d["leaves"] == 2]
            n_leaves = sum(d["leaves"] for d in hits)
            trace.append(f"openings: {len(hits)} door swing(s) in that wall ({n_leaves} leaves)")
            assumptions.append(f"Counted door SETS (a double door = 1) in the {fac['side']} facade's wall: {len(hits)} "
                               f"doors, {n_leaves} leaves; doors drawn without a swing (louvred, sliding) are not counted")
            ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in d["bbox"]], bbox_space="pdf_pt", exact=True,
                           note=f"door {k} · opening {opening_width_m(d, sw['scale_n'] or eff):.2f} m")
                  for k, d in enumerate(hits, 1)]
            if not hits:
                assumptions.append("no swing door in that wall: an entrance with sliding / automatic doors has no swing "
                                   "arc, so 0 may be low")
            return Answer(value=len(hits), unit="count", confidence=0.55 if hits else 0.35, evidence=ev, trace=trace,
                          assumptions=assumptions, missing=["a north arrow or door schedule to confirm the side and count"])
        assumptions.append("windows = window units drawn in the walls: an opening bridged by parallel glazing lines; "
                           "piers between openings separate units, doors (empty openings with a swing) are not windows")
        ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in u["box"]], bbox_space="pdf_pt", exact=True,
                       note=f"window {k}: {len(u['offs'])} glazing lines, {u['len_m']} m")
              for k, u in enumerate(units, 1)]
        return Answer(value=len(units), unit="count", confidence=0.6, evidence=ev, trace=trace,
                      assumptions=assumptions, missing=["a north arrow or window schedule to confirm the side and count"])

    def _both(self, path: str, question: str, m) -> Answer:
        """'How many doors and windows does the Kitchen have?' = two counts, answered in the asked order in the team's
        multi-part format ('4 ; 2', pipeline._solve_multipart) without the LLM split; any part unanswered -> abstain."""
        first = "door" if m.group(0).lower().startswith("door") else "window"
        order = [first, "window" if first == "door" else "door"]
        trace, vals, confs, ev, assumptions, missing = [], [], [], [], [], []
        for i, kind in enumerate(order, 1):
            sub = question[:m.start()] + kind + "s" + question[m.end():]
            a = self.solve(path, sub)
            if a.value is None:
                return self.abstain(f"the {kind} part was not answered: {(a.trace or [''])[-1][:120]}", trace + a.trace)
            vals.append(int(a.value))
            confs.append(float(a.confidence or 0))
            trace.append(f"part {i}: \"{sub[:100]}\" -> {a.value} (conf {a.confidence:.2f})")
            ev += [e.model_copy(update={"note": f"{kind}s: {e.note}"}) for e in a.evidence[:60]]
            assumptions += [f"{kind}s: {x}" for x in a.assumptions]
            missing += [f"{kind}s: {x}" for x in a.missing]
        trace.append(f"openings: {vals[0]} {order[0]}{'s' if vals[0] != 1 else ''} and {vals[1]} {order[1]}"
                     f"{'s' if vals[1] != 1 else ''} ({sum(vals)} openings in all)")
        assumptions.append(f"two counts in the order asked ({order[0]}s ; {order[1]}s); together {sum(vals)} openings")
        return Answer(value=f"{vals[0]} ; {vals[1]}", unit=None, confidence=round(min(confs) * 0.95, 3), evidence=ev,
                      trace=trace, assumptions=list(dict.fromkeys(assumptions)), missing=list(dict.fromkeys(missing)))

    # -- images (renders, scans, photos) ------------------------------------------------------------------------------
    def _via_trace(self, img: str, path: str, question: str, q: dict, items: list, trace: list) -> Answer | None:
        """DOOR counts (and WINDOWS of a named room) on an image sheet whose paper size is known (DPI tag, or the sheet
        size it prints): read the
        image's traced vector copy (stoa.formats.vectorize.traced_copy, the same cached <stem>.vecN.pdf the upload's
        vector fallback reads; swings from the raster arc detector) with the PDF path -> named rooms, plan views,
        double doors, one box per door; evidence mapped back to image px. Measured on Stoa's DfE PNG vs its vector PDF:
        per-room doors 14 of 17 rooms equal (Hall 6 vs 8, Fitness / Medical Inspection 0 vs 1: arcs missed on the
        picture), whole plan 91 vs 101 (15 vs 22 pairs). None = no paper size / not doors / nothing read."""
        if q["target"] not in ("door", "window") or os.environ.get("STOA_DR_TRACE", "1") == "0":
            return None
        roomish = bool(q["scope"]) and not _whole_scope(q["scope"])
        clean = self._clean(img)
        if q["target"] == "window" and not (roomish and clean):
            # windows: only the named room's outline comes from the trace (the count is still V1's detector, on a clip
            # of the traced copy); whole-plan window counts stay with the image readers
            return None
        if not roomish or not clean:
            # a whole-plan count, or any count on a scan / photo, is V1's detector's job when it can answer (door_detect
            # / the detector pass): an answer here would make the pipeline skip it, and swings traced from a picture
            # are missed more often than its boxes (A-101F scan: 6 of 9 leaves traced)
            busy = _det_busy(os.environ.get("STOA_DET_URL", "http://127.0.0.1:8140"))
            if busy == 0 and not _DET_NOTE[0]:
                trace.append("openings: left to the door detector (it is up): " + (
                    "a scan / photo" if not clean else "a whole-plan count") + " on an image")
                return None
        try:
            from stoa.formats import vectorize as V

            hit, how = V.paper(img), "from its metadata"
            if hit is None:
                from stoa.formats.paper import paper_of_image

                hit, how = paper_of_image(img, [t for t, _ in items]), "from its pixel size and the sheet size it prints"
            if hit is None:
                trace.append("openings: no paper size for this image (no DPI tag, no printed sheet size): not traced")
                return None
            dpi, sheet = hit
            t0 = time.monotonic()
            out = Path(V.traced_copy(img, dpi))
        except Exception as e:  # noqa: BLE001
            trace.append(f"openings: tracing the image failed ({type(e).__name__}: {str(e)[:80]})")
            return None
        ans = self.solve(str(out), question)
        if ans.value is None:
            trace.append(f"openings: traced copy ({out.name}) gave no count: {(ans.trace or [''])[-1][:120]}")
            return None
        if ans.value == 0 and q["target"] == "door" and roomish and not q.get("double"):
            # a room is entered through a door: none traced = its swing was missed on the picture (DfE PNG 'Medical
            # Inspection Room' 0, vector PDF 1), not a doorless room
            trace.append(f"openings: traced copy ({out.name}) shows no door swing on the named room: not answered "
                         "(a faint swing is more likely than a room without a door)")
            return None
        f = dpi / 72.0
        for ev in ans.evidence:
            if ev.bbox_space == "pdf_pt" and ev.bbox:
                ev.bbox = [round(v * f, 1) for v in ev.bbox]
            ev.file, ev.page, ev.bbox_space = path, None, "image_px"
            for attr in ("polygon", "polyline"):
                pts = getattr(ev, attr, None)
                if pts:
                    setattr(ev, attr, [[x * f, y * f] for x, y in pts])
        ans.trace = [*trace, f"openings: image traced as vector lines ({sheet} at {dpi:.0f} dpi {how}, {out.name}, "
                             f"{time.monotonic() - t0:.0f} s incl. a cached trace)", *ans.trace]
        ans.assumptions = [*ans.assumptions, (
            "door swings were found on the picture (traced into vector lines): a faint or broken swing arc can be "
            "missed, so the count may be low" if q["target"] == "door" else
            "the room's outline was traced from the picture (vector copy); windows by the symbol detector on it")]
        cap = 0.5 if roomish else 0.4  # whole-plan totals on a picture: more arcs to miss (DfE 91 of 101)
        if not clean:
            # a scan / photo: faint or broken swings (X1's A-101F scan: 6 of 9 leaves traced -> 4 doors vs 6)
            cap -= 0.2
            ans.assumptions = [*ans.assumptions, "the image looks like a scan or photo: swing arcs are often too faint "
                               "to trace, so this count is likely low"]
        ans.confidence = min(ans.confidence, cap)
        return ans

    @staticmethod
    def _clean(img: str) -> bool:
        """A clean digital render (few flat grey levels) vs a scan / photo: the shrink's sidecar verdict when there is
        one (normalize.shrink_image), else raster.windows.clean_render."""
        try:
            side = Path(img + ".json")
            if side.exists():
                v = json.loads(side.read_text()).get("clean")
                if v is not None:
                    return bool(v)
            from stoa.solvers.raster.windows import clean_render

            return bool(clean_render(img)[0])
        except Exception:  # noqa: BLE001
            return True

    def _image(self, img: str, path: str, question: str, q: dict) -> Answer:
        """Multi-view sheets as images (a render / scan / photo of a CAD sheet with plan + RCP + axon views): plan views
        from the image's text layer (titles under the views; VP's stoa.sheet when present), then V1's detector with px
        per metre self-calibrated from the door boxes themselves (a doorset box is ~ one leaf, 0.75-1.0 m, on its short
        side), so no printed scale / DPI is needed. A single-view image abstains BEFORE any detector call (the raster
        readers / door_detect handle plain plans; measured: ks51 7 doors -> 5 here vs 7 on the current route)."""
        import statistics

        from PIL import Image

        trace: list[str] = []
        assumptions: list[str] = []
        with Image.open(img) as im:
            W, H = im.size
        # the image's text WITHOUT starting a slow reading of our own (label_count's rule, S0 16:48: a cold OCR of a
        # whole sheet costs minutes): a PDF render's own words, else the cached layer raster reads anyway
        try:
            from stoa.solvers.label_count import _image_lines

            items = [(t, list(b)) for t, b in (_image_lines(img) or ())]
        except Exception:  # noqa: BLE001
            items = []
        if not items:
            return self.abstain("no text layer ready for this image (view titles unknown)", trace)
        scope = None
        try:  # VP's view scoping (stoa.sheet) when it is on this branch; the layer is cached by now
            from stoa.sheet import scope_for
            from stoa.solvers.raster.textlayer import text_layer

            scope = scope_for(img, question, layer=text_layer(img))
            named_q = bool(q["scope"]) and not _whole_scope(q["scope"])
            if scope is not None and (all("untitled" in str(v.title) for v in scope.views) or (
                    named_q and "no part named" in scope.reason)):
                # VP found no titled view, or not the part the question names (full-res AFCEE A-101 PNG: 'module 02'
                # -> '(untitled drawing)', 13 doors instead of 9): our own view titles below decide
                trace.append(f"openings: stoa.sheet gave '{scope.reason[:60]}': using our own view titles")
                scope = None
        except Exception:  # noqa: BLE001
            scope = None
        cells, sel, plans = [], None, []
        if scope is None and items:
            items = _merge_line_items(items)
            hs = sorted(b[3] - b[1] for _, b in items)
            med = hs[len(hs) // 2] if hs else 0
            titles = []
            for t, b in items:
                if len(t) > 90 or len(t) < 4 or (b[3] - b[1]) < 1.15 * med or b[0] > 0.86 * W:
                    continue
                if re.match(r"^\s*(?:\d+\.|NOTE|SEE |PROVIDE|ALL |REFER)", t, re.I) or _NOT_TITLE.search(t):
                    continue
                kind = next((k for k, rx in _VIEW_KIND if rx.search(t)), None)
                if kind:
                    titles.append({"text": t, "kind": kind, "box": tuple(b)})
            cells = view_cells(tuple(titles), W, H)
            plans = [c for c in cells if c["kind"] == "plan"]
            others = [c for c in cells if c["kind"] != "plan"]
            if plans and (others or len(plans) > 1):
                named = _scope_match(q["scope"], plans)
                if q["scope"] and not named and not _whole_scope(q["scope"]):
                    tr = self._via_trace(img, path, question, q, items, trace)
                    if tr is not None:
                        return tr
                    return self.abstain(f"'{' '.join(q['scope'])}' names no plan view of this image", trace)
                sel = named or plans
                trace.append("openings: " + ("the question names view(s) " if named else "plan views ")
                             + "; ".join(repr(c["text"][:50]) for c in sel))
                if others and not named:
                    assumptions.append("counted only the floor-plan views (" + ", ".join(
                        sorted({c['kind'] for c in others})) + " views of the same sheet left out)")
        whole_img = False
        if scope is None and sel is None:
            whole_img = self._whole_image(img, q, plans, cells, trace)
            if not whole_img:
                tr = self._via_trace(img, path, question, q, items, trace)
                if tr is not None:
                    return tr
                if trace and "traced copy" in trace[-1]:
                    return self.abstain(trace[-1].replace("openings: ", "", 1), trace)
                return self.abstain("single-view image: no plan views to scope (raster readers count plain plans)",
                                    trace)
        z1 = min(1.0, 4000.0 / max(W, H))
        r1 = _det_client(img, z1, 0.4)
        if r1 is None:
            why = _det_why()
            tr = None if whole_img and _DET_NOTE[0].startswith("private") else \
                self._via_trace(img, path, question, q, items, trace)
            if tr is not None:
                return tr
            return self.abstain(why, trace)
        doors1 = [b for b in r1["boxes"] if b[5] == "door" and b[4] >= 0.5]
        if len(doors1) < 2:
            return self.abstain(f"detector found {len(doors1)} door boxes: too few to calibrate the scale", trace)
        leaf_px = statistics.median(min(b[2] - b[0], b[3] - b[1]) for b in doors1)  # original px
        ppm = leaf_px / 0.85
        trace.append(f"openings: detector pass 1 at zoom {z1:.2f}: {len(doors1)} door boxes, median leaf {leaf_px:.0f} px"
                     f" -> ~{ppm:.0f} px/m (door leaf taken as 0.85 m)")
        r = r1
        if not 35 <= ppm * z1 <= 85 and 25 / ppm <= 1.0:
            z2 = min(1.0, 60.0 / ppm)
            r2 = _det_client(img, z2, 0.4)
            if r2 is not None:
                n2 = sum(1 for b in r2["boxes"] if b[5] == "door" and b[4] >= 0.5)
                if n2 >= len(doors1) or z2 > z1:
                    r = r2
                    trace.append(f"openings: detector pass 2 at zoom {z2:.2f} (~60 px/m)")
                else:
                    # X1's A-101F render PNG (6000 px, 1:50): pass 1 found the 6 doorsets (= gold), the shrunk pass 2
                    # lost every thin swing (0 doors). Shrinking only loses ink: keep the pass that saw more doors.
                    trace.append(f"openings: detector pass 2 at zoom {z2:.2f} found {n2} door boxes (< {len(doors1)}):"
                                 " kept pass 1")
        assumptions.append(f"no scale used: the drawing's px per metre (~{ppm:.0f}) was estimated from the door symbols "
                           "themselves (a door leaf ≈ 0.85 m)")
        cls = q["target"]
        boxes = [b for b in r["boxes"] if b[5] == cls]
        if cls == "door":
            boxes = [b for b in boxes if b[4] >= 0.4 and max(b[2] - b[0], b[3] - b[1]) >= 0.6 * ppm]  # V1: >= 0.4 (v2)
        else:
            boxes = [b for b in boxes if b[4] >= WIN_SCORE and min(b[2] - b[0], b[3] - b[1]) >= 0.12 * ppm]
        if scope is not None:
            n0 = len(boxes)
            boxes = [b for b in boxes if scope.contains_box(b[:4])]
            trace.append(f"openings: {scope.note()} -> {len(boxes)} of {n0} boxes")
        elif whole_img:
            pass  # one plan on the sheet: every box counts (the detector is trained against title-block / tag look-alikes)
        else:
            def cell_of(b):
                return self._in_cells(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), cells)
            boxes = [b for b in boxes if any(cell_of(b) is c for c in sel)]
        if cls == "door" and q["double"]:
            # a pair's box is ~2 leaves long: long side >= 1.6 x the sheet's typical leaf (the median short side). The
            # box's own aspect is not enough: on X1's A-101F scan the 3 pairs are 274 x 198 px (frame + swing), the
            # singles 145-200 px long with a ~157 px leaf (old rule 'aspect >= 1.6': 1 of 3)
            boxes = [b for b in boxes if max(b[2] - b[0], b[3] - b[1]) >= 1.6 * leaf_px
                     and max(b[2] - b[0], b[3] - b[1]) >= 1.25 * min(b[2] - b[0], b[3] - b[1])]
        ev = [Evidence(file=path, page=0, bbox=[round(v, 1) for v in b[:4]], bbox_space="image_px", exact=False,
                       note=f"{cls} {k} ({b[4]:.2f})") for k, b in enumerate(boxes, 1)]
        trace.append(f"openings: {len(boxes)} {cls}{'s' if len(boxes) != 1 else ''} on the image")
        if cls == "door":
            assumptions.append("Convention: a double door counts as ONE door (one detector box per doorset)")
        conf = 0.45
        if not whole_img and not self._clean(img):
            # V1 20:18 / X1 scan_dev: on scans the detector misses faint swings (MFC scan module 02 7 of 9, module 01
            # 2 of 4): a lower bound
            conf = 0.35
            assumptions.append(f"the image looks like a scan or photo: faint {cls} symbols are often missed, so this "
                               "count is more likely low than high")
        if whole_img:
            if not boxes and not q["double"]:
                return self.abstain(f"the detector found no {cls} on the whole drawing (a plan without any is unlikely:"
                                    " not answered)", trace)
            if not self._clean(img):
                # V1 20:18: on scans the detector misses the faintest swings (A-101 scan 10/13, DfE scan 54 vs 89)
                conf = 0.35
                assumptions.append(f"the image looks like a scan or photo: faint {cls} symbols are often missed, so "
                                   "this count is more likely low than high")
            else:
                conf = 0.4
            assumptions.append("one plan on this sheet: every " + cls + " symbol on the drawing was counted")
        return Answer(value=len(boxes), unit="count", confidence=conf, evidence=ev, trace=trace,
                      assumptions=assumptions, missing=[f"a {cls} schedule or a printed scale to confirm the count"])

    @staticmethod
    def _whole_image(img: str, q: dict, plans: list, cells: list, trace: list) -> bool:
        """A whole-plan door / window count on a picture of a ONE-plan CAD sheet (X1's A-101F scans: one title 'A1
        FILTER BUILDING FLOOR PLAN', no RCP / axon): V1's detector over the whole image. The question names no part,
        only whole-drawing words, or the words of the sheet's one plan title ('the filter building'). Pictures that
        look like CubiCasa / Swiss renders keep their own readers (measured cycle 1: ks51 7 doors -> 5 here)."""
        if os.environ.get("STOA_DR_WHOLE_IMG", "1") == "0" or q["target"] not in ("door", "window"):
            return False
        sc = q["scope"]
        if len(plans) > 1 or any(c["kind"] in ("rcp", "axon", "elevation", "section", "other_plan") for c in cells):
            return False  # a titled non-plan view (or a key / site plan) on the sheet: its symbols must not count
        if sc and not _whole_scope(sc) and not (plans and _scope_match(sc, plans)):
            return False
        try:
            from stoa.style import guess_style

            style, pr = guess_style(img)
        except Exception:  # noqa: BLE001
            return False
        if style != "scan" and not plans:
            trace.append(f"openings: a plain plan picture (style {style} p={pr:.2f}): left to the raster readers")
            return False
        trace.append("openings: one plan on this sheet" + (f" ({plans[0]['text'][:50]!r})" if plans else
                     " (no view title)") + f": the detector counts the whole drawing (style {style} p={pr:.2f})")
        return True
