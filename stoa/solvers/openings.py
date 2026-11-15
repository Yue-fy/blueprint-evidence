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
_SCHED_COUNT_Q = re.compile(r"\bdoor\s+schedule|\bschedule\s+of\s+doors|\baccording to the schedule|\bin the schedule"
                            r"|\bschedule\s+lists?\b|\blisted\s+in\s+the\s+schedule", re.I)
_SCHED_ATTR = re.compile(r"\b(?:type|types|hardware|fire|rat(?:ed|ing)|frame|width|wide|height|tall|material|glaz\w*|"
                         r"finish|label|thick\w*|single|double|pairs?|leaves|leaf|high|size)\b", re.I)
# words of a schedule question that neither scope nor filter the rows
_SCHED_GENERIC = {"marked", "mark", "actual", "total", "many", "rows", "row", "list", "listed", "lists", "give",
                  "number", "numbers", "count", "counted", "counting", "are", "there", "with", "have", "has", "does",
                  "doors", "door", "entries", "entry", "in", "as", "all", "every", "each", "drawing", "drawings", "set"}
_SCHED_WORDS = {"according", "schedule", "schedules", "listed", "list", "lists", "door", "doors", "rows", "row",
                "entries", "entry", "contain", "contains"}
_EXCL_Q = (
    re.compile(r"\s*,?\s*\b(?:if|when)\s+(?:the\s+)?(?P<x>[^?.,;]+?)\s+(?:are|is)\s+(?:not|n't)\s+(?:counted|included)\b",
               re.I),
    re.compile(r"\s*,?\s*\b(?:excluding|except(?:\s+for)?|not\s+counting|other\s+than|apart\s+from|but\s+not)\s+(?:the\s+)?"
               r"(?P<x>[^?.,;]+)", re.I),
    re.compile(r"\s*,?\s*\bwithout\s+(?:the\s+)?(?P<x>[^?.,;]*\b(?:doors?|windows?)\b[^?.,;]*)", re.I),
)


def split_exclusion(question: str) -> tuple[str, str] | None:
    """'How many doors are there on the whole plan if WC and shower cubicle doors are not counted?' (X1 sl-268, Ziying
    104) -> ('How many doors are there on the whole plan?', 'How many WC and shower cubicle doors are drawn?'); None
    when nothing is excluded."""
    q = question or ""
    if "?" in q.rstrip()[:-1]:
        return None  # only the ASKED clause: 'Count doorways ...; open doorways without a door leaf do not count' is a rule
    for rx in _EXCL_Q:
        m = rx.search(q)
        if not m:
            continue
        x = m.group("x").strip()
        tgt = "doors" if _DOOR.search(q[:m.start()] or q) else "windows"
        if not (_DOOR.search(x) or _WINDOW.search(x)):
            x = f"{x} {tgt}"
        main = (q[:m.start()] + q[m.end():]).strip()
        main = re.sub(r"\s+([?.])", r"\1", main)
        if not main.endswith("?"):
            main = main.rstrip(".") + "?"
        if not _COUNT_Q.search(main):
            return None
        return main, f"How many {x} are drawn?"
    return None


def _sched_count_q(question: str) -> bool:
    """'How many doors are listed in the door schedule?' / 'According to the door schedule, how many doors does module
    07 have?' (no attribute filter: fire-rated / type / hardware rows are the schedule readers')."""
    q = re.sub(r"\([^)]*\)", " ", question or "")  # '(no door leaf)', '(confinement)': asides, not filters
    return bool(_SCHED_COUNT_Q.search(q) and _COUNT_Q.search(q) and _DOOR.search(q) and not _SCHED_ATTR.search(q)
                and not _WINDOW.search(q))


_BOTH_Q = re.compile(r"\b(?:doors?\s+(?:and|&|or|plus)\s+windows?|windows?\s+(?:and|&|or|plus)\s+doors?)\b", re.I)
_CLOSET_W = re.compile(r"(?:closet|wardrobe|cupboard)s?", re.I)
_EACH_Q = re.compile(r"\b(?:each|every|per|a|one)(?:\s+of\s+the)?\s+(?:typical\s+|single\s+|individual\s+)?"
                     r"(?P<room>(?:[a-z]+\s+){0,2}"
                     r"(?:room|dorm|dormitory|bedroom|unit|module|apartment|flat|suite|cell|cabin|office|classroom|"
                     r"guest\s?room))s?\b", re.I)
_CLOSET_ONLY = re.compile(r"\b(?:closet|wardrobe|cupboard)s?\s+doors?\b", re.I)
_CLOSET_WITH = re.compile(r"\b(?:including|incl\.?|with|counting|plus|and)\s+(?:the\s+|all\s+|any\s+)?(?:closet|wardrobe|"
                          r"cupboard)s?\b|\b(?:closet|wardrobe|cupboard)s?(?:\s+doors?)?\s+(?:included|too|as well)\b",
                          re.I)
_CLOSET_X = re.compile(r"^(?:the\s+|all\s+|any\s+)?(?:closet|wardrobe|cupboard)s?(?:\s+(?:and|or|/)\s+(?:closet|wardrobe|"
                       r"cupboard)s?)*(?:\s+doors?)?$", re.I)
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
    # scope words come from the ASKED clause: 'How many door leaves does the support module have? Count both leaves
    # of a pair; an overhead door counts as one.' (X1 sl-252) names the module, not a room 'overhead one'
    asked = q.split("?")[0] if "?" in q.rstrip()[:-1] else q
    words = [w for w in re.findall(r"[A-Za-z]+|\d+[A-Za-z]?", asked.lower())]
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
    closets = None
    if door and os.environ.get("STOA_DR_CLOSET_Q", "1") != "0":
        # our convention leaves closet / wardrobe / cupboard doors out (Ziying), and the answer says how many it left
        # out, so the next question is 'how many closet doors?' / '... including the closets?': answer THAT count
        if _CLOSET_WITH.search(asked):
            closets = "with"
        elif _CLOSET_ONLY.search(asked):
            closets = "only"
        if closets:
            scope = [w for w in scope if not _CLOSET_W.fullmatch(w)]
    each = _EACH_Q.search(asked) if door and os.environ.get("STOA_DR_EACH", "1") != "0" else None
    if each:
        # 'How many doors does each dorm room have?': the repetition check's doors per identical room, not a scope
        drop = set(re.findall(r"[a-z]+", each.group(0).lower()))
        scope = [w for w in scope if w not in drop and w.rstrip("s") not in drop]
        each = each.group("room").strip().lower()
    wing = _WING_Q.search(asked)
    if wing and not facade:
        wing = (wing.group(1) or wing.group(2)).lower()
        scope = [w for w in scope if w not in ("wing", "wings", "arm", "block", wing, wing + "ern")]
    else:
        wing = None
    return {"target": "door" if door else "window", "side": side, "facade": facade, "wing": wing,
            "swing_only": bool(re.search(r"\bswing", q, re.I)),
            "double": bool(door and _DOUBLE.search(q) and not _SINGLE.search(q)),
            "single": bool(door and _SINGLE.search(q) and not _DOUBLE.search(q)),
            "leaves": bool(door and _LEAVES.search(q)),
            "closets": closets,
            "each": each or None,
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
                    out.append({"c": (cx, cy), "r": r, "p0": pts[0], "p1": pts[-1], "sweep": sweep, "res": res,
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


def _unit_on_edge(poly: list, box: list, tol: float) -> bool:
    """A window unit (box, pdf_pt) belongs to the room when it lies ALONG one of the room's edges: parallel to it
    (within 15 deg), its centre line within tol of the edge, and >= half its length over the edge (S0 21:33, d28
    sealed: a room-scoped window count 2 -> 4; the centre-only test took units of the next room's wall near a corner
    and of perpendicular walls)."""
    w, h = box[2] - box[0], box[3] - box[1]
    ax = (1.0, 0.0) if w >= h else (0.0, 1.0)
    ln = max(w, h)
    c = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    n = len(poly)
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        L = math.hypot(x1 - x0, y1 - y0)
        if L <= 0:
            continue
        ex, ey = (x1 - x0) / L, (y1 - y0) / L
        if abs(ex * ax[1] - ey * ax[0]) > 0.26:            # not parallel
            continue
        if abs((c[0] - x0) * ey - (c[1] - y0) * ex) > tol:  # centre line too far from the edge line
            continue
        t0 = (c[0] - ax[0] * ln / 2 - x0) * ex + (c[1] - ax[1] * ln / 2 - y0) * ey
        t1 = (c[0] + ax[0] * ln / 2 - x0) * ex + (c[1] + ax[1] * ln / 2 - y0) * ey
        lo, hi = min(t0, t1), max(t0, t1)
        if min(hi, L) - max(lo, 0.0) >= 0.5 * max(hi - lo, 1e-6):
            return True
    return False


def _tangent_both(arc: dict, grid: _Grid, tol: float, own: bool = False) -> bool:
    """Rounded corner of a box / legend frame: at BOTH ends a straight line runs on along the arc's tangent."""
    hits = 0
    # the arc's OWN chord segments (an exploded arc: AFCEC A-2 draws each swing as 4 loose 2 pt segments, 0.39 r long)
    # end ON its circle; a line running on along the tangent ends outside it (>= 1.04 r away at 0.3 r long)
    # and lies inside the arc's own span. own=True only on pages find_swings picks (AFCEE A-103 has a few exact circle
    # fits through framing corners that only this test drops: 10 -> 19 'swings' when applied to every page)
    c, r = arc["c"], arc["r"]
    chord = math.dist(arc["p0"], arc["p1"]) + 0.3
    for p, th in ((arc["p0"], arc["th0"] + 180.0), (arc["p1"], arc["th1"])):
        ok = False
        for s in grid.near(p, tol):
            a, b = (s[0], s[1]), (s[2], s[3])
            for u, v in ((a, b), (b, a)):
                if own and abs(math.dist(v, c) - r) <= 0.02 * r + 0.05 and math.dist(v, arc["p0"]) <= chord \
                        and math.dist(v, arc["p1"]) <= chord:
                    continue
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
    drop = [_tangent_both(a, grid, tol) for a in keep]
    if os.environ.get("STOA_DR_TANGENT_OWN", "1") != "0" and sum(drop) >= 20:
        # a page that draws its swings as a few loose straight pieces (AFCEC A-2: 4 x 2 pt per swing): the rounded-
        # corner rule mistakes each arc's own end pieces for lines running on. Used for the whole page only when it
        # brings back a population (>= 2 x the arcs and >= 20 more: AFCEC 87 -> 201; AFCEE A-103 +9 stays as it was)
        drop2 = [d and _tangent_both(a, grid, tol, own=True) for a, d in zip(keep, drop)]
        n1, n2 = len(keep) - sum(drop), len(keep) - sum(drop2)
        if n2 >= 2 * n1 and n2 - n1 >= 20:
            drop = drop2
            notes.append(f"door swings drawn as a few loose straight pieces: {n2 - n1} arcs kept that a rounded-corner "
                         "test would have dropped")
    for a, d in zip(keep, drop):
        if d:
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
            steps = (0.5, 2 ** -0.5, 2 ** 0.5, 2.0, 2.0 * 2 ** 0.5, 4.0)

            def n_band(k: float) -> int:  # arcs (leaf line or not) of a door-leaf radius at 1:(scale_n x k)
                return sum(1 for a in out if 0.6 <= a["r"] * PT_M * scale_n * k <= 1.15)

            n1 = n_band(1.0) if not 0.6 <= med_m <= 1.15 else 0
            if n1 >= 8 and n1 >= 0.67 * max(n_band(k) for k in steps) and \
                    os.environ.get("STOA_DR_SCALE_BAND", "1") != "0":
                # AFCEC A-2 (1" = 40' = 1:480 on 17 x 11): 48 rooms' desk / arm chairs carry a 'leaf' line and set the
                # median (0.28 m), while the door swings (0.76 / 0.9 m) have none: the printed scale holds a whole
                # population of door-size arcs, so it stands (the old step to 1:1357 counted 81 chairs as doors)
                notes.append(f"printed scale 1:{scale_n:g} kept: {n1} arcs have a door-leaf radius at it (the median "
                             f"arc, {med_m:.2f} m, is furniture)")
            elif not 0.6 <= med_m <= 1.15:
                # a printed '1:200 @ A1' on an A3 page, or a reduced print: try sqrt(2) paper steps
                best = min((abs(med_m * k - 0.85), k) for k in steps)
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


@functools.lru_cache(maxsize=64)
def _page_wh_cached(pdf: str, page: int, mtime: float) -> tuple:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        r = doc[page].rect
        return (r.width, r.height)


def _page_wh(pdf: str, page: int) -> tuple:
    """The displayed page size (pt), as view_cells takes it."""
    return _page_wh_cached(str(pdf), int(page), os.stat(pdf).st_mtime)


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
    many = _list_match(sc, cells, toks)
    if many:
        return many
    best, out = 0.0, []
    for c, t in zip(cells, toks):
        s = 0.0
        if not any(w in t for w in sc if not w.isdigit()):
            continue  # a bare number ('English 3') never names 'Secondary Type 3 600 First Floor': a word must match
        if any(w not in t and w not in _WHOLE for w in sc if not w.isdigit()):
            continue  # 'Hygiene Room Primary' is a room, not the view 'Primary School GA Plan Level 00
        nums, tn = {w for w in sc if w.isdigit()}, {w for w in t if w.isdigit()}
        if nums and tn and not nums & tn:
            continue  # 'module 6' never names 'MODULE 7 - ...' (X1 sl-271: the word 'module' alone matched)
        for w in sc:
            if w in t:
                share = sum(1 for tt in toks if w in tt) / len(toks)
                s += 1.0 if share < 0.6 or len(toks) == 1 else 0.25
        if s > best + 1e-9:
            best, out = s, [c]
        elif s >= best - 1e-9 and s > 0:
            out.append(c)
    return out if best >= 1.0 else []


_LIST_GENERIC = set("""module modules view views part parts area areas zone zones block blocks section sections unit units
total wing wings""".split())


def _list_match(sc: list[str], cells: list[dict], toks: list[set]) -> list[dict]:
    """A LIST of views ('the restrooms, stairs and support modules', 'modules 05, 06 and 07'; X1 sl-253): every
    word names some view (no word on >= 60 % of them), >= 2 views together; a word naming no view -> not a list."""
    if len(cells) < 2:
        return []
    gen = any(w in _LIST_GENERIC for w in sc)
    words = [w for w in sc if (not w.isdigit() and w not in _WHOLE and w not in _LIST_GENERIC) or (w.isdigit() and gen)]
    if len(words) < 2:
        return []
    hit: set[int] = set()
    for w in words:
        alt = {w, w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w}
        m = [i for i, t in enumerate(toks) if alt & t]
        if not m or len(m) >= 0.6 * len(toks):
            return []
        hit.update(m)
    return [cells[i] for i in sorted(hit)] if len(hit) >= 2 else []


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
        keys, tkeys = [], []
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
            # the storey in a PLAN title ('SECOND FLOOR PLAN' in A-2's title block), not in a note on another sheet
            # ('TO SECOND FLOOR DORMITORY MODULE WING' on the first-floor common-area sheet)
            tkeys.append(_storey_keys(" | ".join(t for t, sz in lines if sz >= 1.15 * med
                                                 and re.search(r"\bplan\b", t, re.I))))
    if want:
        hit = [i for i, k in enumerate(keys) if k & want]
        titled = [i for i in hit if tkeys[i] & want]
        if titled and len(titled) < len(hit) and os.environ.get("STOA_DR_PLAN_TITLE", "1") != "0":
            hit = titled
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


# ---------------------------------------------------------------------------------------------------------------------
# wings ('How many doors are in the north wing?': H 22:24, Stoa's AFCEC dorm sheet, a T-shaped plan whose wings are
# named only in its area table). A wing = the ARM of the building's footprint at that side of the sheet.
_WING_Q = re.compile(r"\b(north|south|east|west)(?:ern)?\s+(?:wing|arm|block)s?\b|"
                     r"\b(?:wing|arm|block)\s+(?:to|on|at)\s+the\s+(north|south|east|west)\b", re.I)
WING_LONG = 2000  # the footprint is read on a copy of the sheet with this long side (px)


def footprint_mask(gray, k_close: int = 5, k_open: int = 13):
    """The building's footprint on a sheet picture (uint8 0/1, same size as `gray`, long side ~WING_LONG): ink + fills
    closed over door gaps, rooms (white areas a wall encloses) filled, then thin lines / text / dimension strings opened
    away; the largest blob = the building (a key plan or a legend swatch is smaller). None when nothing is left."""
    import cv2
    import numpy as np

    # ink = darker than the PAPER: a clean render's paper is 255 (-> < 235 as before); a scan's grey paper (X1 sc-315:
    # the whole sheet read as ink -> 'no distinct north wing') sets its own level (the brightest well-filled grey)
    hist = np.bincount(gray.ravel(), minlength=256)
    paper = int(np.argmax(hist[128:])) + 128
    ink = (gray < min(235, paper - 20)).astype(np.uint8)
    m = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k_close, k_close)))
    h, w = m.shape
    n, lab, stats, _ = cv2.connectedComponentsWithStats((m == 0).astype(np.uint8), 4)
    small = np.zeros(n, bool)  # holes = rooms; the white field inside the sheet frame (big, or touching the edge) is not
    for i in range(1, n):
        x, y, bw, bh, a = stats[i]
        small[i] = a < 0.03 * h * w and x > 0 and y > 0 and x + bw < w and y + bh < h
    m = (m | small[lab]).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k_open, k_open)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (lab == i).astype(np.uint8)


def exterior_units(pdf: str, page: int, units: list, arcs: list, scale_n: float, max_in_m: float = 1.2):
    """Windows sit in EXTERIOR walls. AFCEC A-2 (Stoa's dorm sheet): 289 'window units' = the thin double-line
    partitions between bed room, bathroom and wardrobe (2-3 parallel lines sharing one span), none in the outer walls
    -> the building's outline on a render of the page (footprint_mask, as for wings); units whose centre lies more than
    `max_in_m` inside it are interior. Trusted only when the outline holds >= 90 % of >= 8 door swings (one building
    on the sheet; a multi-view sheet's largest blob is one view). Courtyards larger than 3 % of the sheet stay holes,
    so their windows count. -> (kept, interior) or None (outline not trusted)."""
    import cv2
    import numpy as np
    import pymupdf

    if len(arcs) < 8 or not units:
        return None
    with pymupdf.open(pdf) as doc:
        pg = doc[page]
        r = pg.rect
        rm = pg.rotation_matrix
        sc = WING_LONG / max(r.width, r.height)
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(sc, sc), colorspace=pymupdf.csGRAY, alpha=False)
        gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
    F = footprint_mask(gray)
    if F is None:
        return None
    H, W = F.shape

    def px(p):
        q = pymupdf.Point(p) * rm
        return int(q.x * sc), int(q.y * sc)

    Fd = cv2.dilate(F, np.ones((5, 5), np.uint8))
    pts = [px(a["c"]) for a in arcs]
    if sum(1 for x, y in pts if 0 <= y < H and 0 <= x < W and Fd[y, x]) < 0.9 * len(pts):
        return None
    ppm = sc / (PT_M * scale_n)
    dist = cv2.distanceTransform(F, cv2.DIST_L2, 3) / ppm  # metres to the outline, inside the building
    ds = []
    for u in units:
        b = u["box"]
        x, y = px(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
        ds.append(float(dist[y, x]) if 0 <= y < H and 0 <= x < W else 0.0)
    # the outline can take in the dimension strings round the building (EFA / DfE 1:200 sheets: the band between the
    # walls and the dimension lines is closed off by the grid lines and filled): the outer walls are then 2-3 m inside
    # it. The outermost units mark the outer wall ring -> depth is measured from them
    ref = float(np.percentile(ds, 3))
    kept = [u for u, d in zip(units, ds) if d <= ref + max_in_m]
    inner = [u for u, d in zip(units, ds) if d > ref + max_in_m]
    return kept, inner


def wing_arm(F, side: str, ref_frac: float = 0.15) -> tuple:
    """The arm of footprint F at display side 'top' / 'bottom' / 'left' / 'right': walk from that extreme inwards while
    the footprint stays as wide as the arm is 15 % of the way in (a narrower link corridor / stair stays in); stop where
    it gets wider (> 1.3 x: the body the arm joins) or, after a narrow neck, grows back (> 0.8 x: the core behind a
    link). -> (mask | None, info dict). AFCEC T-plan: north wing = dorm block + its stair + the 40' link corridor (its
    area table's 'NORTH WING CIRCULATION' holds the link), cut at the common core; 'south wing' = the whole footprint ->
    the caller says there is none."""
    import cv2
    import numpy as np

    G = {"top": F, "bottom": F[::-1, :], "left": F.T, "right": F.T[::-1, :]}[side]
    rows = np.where(G.any(axis=1))[0]
    if len(rows) < 10:
        return None, {"why": "no building outline"}
    y0, y1 = int(rows[0]), int(rows[-1])
    yr = y0 + int(ref_frac * (y1 - y0))
    xs = np.where(G[yr])[0]
    runs = [r for r in np.split(xs, np.where(np.diff(xs) > 1)[0] + 1) if len(r) >= 0.02 * G.shape[1]]
    if len(runs) != 1:
        return None, {"why": f"{len(runs)} separate parts of the building reach that side" if runs else
                      "no building outline"}
    lo, hi = int(runs[0][0]), int(runs[0][-1])
    wref, neck, stop, wmin = hi - lo + 1, False, y1 + 1, hi - lo + 1
    for y in range(yr, y1 + 1):
        xs = np.where(G[y])[0]
        if not len(xs):
            continue
        ov = [r for r in np.split(xs, np.where(np.diff(xs) > 1)[0] + 1) if r[-1] >= lo and r[0] <= hi]
        if not ov:
            stop = y
            break
        wid = int(max(r[-1] for r in ov) - min(r[0] for r in ov) + 1)
        wmin = min(wmin, wid)
        if wid < 0.6 * wref:
            neck = True
        if wid > 1.3 * wref or (neck and wid > 0.8 * wref):
            stop = y
            break
    A = np.zeros_like(G)
    A[y0:stop] = G[y0:stop]
    _, lab = cv2.connectedComponents(A, connectivity=8)
    A = (lab == lab[yr, (lo + hi) // 2]).astype(np.uint8)
    back = {"top": A, "bottom": A[::-1, :], "left": A.T, "right": A[::-1, :].T}[side]
    return back, {"frac": float(A.sum()) / max(1.0, float(F.sum())), "length": stop - y0, "extent": y1 - y0 + 1,
                  "width": wref, "neck": neck, "neck_width": wmin}


def _floor_spaces(gray, boxes: list) -> tuple:
    """Free floor of a plan crop split into spaces: (n, labels, stats) of cv2.connectedComponentsWithStats (4-conn).
    Every door box is blocked so rooms stay apart at their doorways. Cached on the last call (closet_doors and
    repeated_modules read the same crop)."""
    import cv2
    import numpy as np

    key = (id(gray), gray.shape, tuple(tuple(int(v) for v in b[:4]) for b in boxes))
    if _FLOOR_CACHE and _FLOOR_CACHE[0] == key and _FLOOR_CACHE[1] is gray:
        return _FLOOR_CACHE[2]
    # floor = anything lighter than the linework: white, light fills AND darker module fills (AFCEC: ABA rooms' green
    # is grey 174; lines / door arcs / antialiasing <= 160)
    # relative to the paper (a scan's paper is ~231, its lines lighter): paper - 90 = 165 on a clean render; a 3 px
    # median first so scan speckle does not punch holes in a closet's floor
    hist = np.bincount(gray.ravel(), minlength=256)
    paper = int(np.argmax(hist[128:])) + 128
    g = cv2.medianBlur(gray, 3) if paper < 250 else gray
    free = (g >= paper - 90).astype(np.uint8)
    for b in boxes:
        x0, y0, x1, y1 = (int(v) for v in b[:4])
        free[max(0, y0 - 2):y1 + 3, max(0, x0 - 2):x1 + 3] = 0
    n, lab, stats, _ = cv2.connectedComponentsWithStats(free, 4)
    _FLOOR_CACHE[:] = [key, gray, (n, lab, stats)]
    return n, lab, stats


_FLOOR_CACHE: list = []


def repeated_modules(gray, boxes: list, counted: list, ppm: float) -> dict | None:
    """Captain 23:04 (Ziying's tip 3, AFCEC north wing = 16 rooms x (entry + bathroom) + 2): REPEATED room modules in a
    plan crop and the counted doors of each. A module = the largest free floor of one room (beds / desks split a
    room's floor; its biggest piece keeps the same size and box in every copy of the room): >= 4 spaces of 4-80 m²
    whose area and box sides agree within 10 % (+ variants: one box side the same, area 0.7-1.6 x, e.g. the 2
    accessible rooms). Each counted door goes to the nearest module within 1.5 m (entry door from the corridor,
    bathroom door inside the room); doors near no module are the 'other' doors (stair, link). -> {n, alike, variants,
    m2, k (doors of the usual module), dev [(module #, doors)], other, own [module # | None per box]} or None."""
    import collections

    import cv2
    import numpy as np

    n, lab, stats = _floor_spaces(gray, boxes)
    H, W = lab.shape
    px2 = ppm * ppm
    cand = []
    for k in range(1, n):
        x, y, bw, bh, a = (int(v) for v in stats[k])
        if x == 0 or y == 0 or x + bw >= W or y + bh >= H:
            continue
        if 4.0 <= a / px2 <= 80.0:
            cand.append((k, a / px2, sorted((bw / ppm, bh / ppm)), (x + bw / 2, y + bh / 2)))
    best: list = []
    for k, m2, dm, _ in cand:
        g = [c for c in cand if abs(c[1] - m2) <= 0.1 * m2 and all(abs(c[2][i] - dm[i]) <= 0.1 * dm[i] for i in (0, 1))]
        if len(g) > len(best) or (len(g) == len(best) and best and m2 > best[0][1]):
            best = g
    if len(best) < 4:
        return None
    med = float(np.median([c[1] for c in best]))
    dmed = [float(np.median([c[2][i] for c in best])) for i in (0, 1)]
    ids = {c[0] for c in best}
    var = [c for c in cand if c[0] not in ids and 0.7 * med <= c[1] <= 1.6 * med and max(c[2]) <= 1.35 * dmed[1]
           and any(abs(c[2][i] - dmed[j]) <= 0.08 * dmed[j] for i in (0, 1) for j in (0, 1))]

    def assign(mods):
        mask = np.ones(lab.shape, np.uint8)
        for c in mods:
            mask[lab == c[0]] = 0
        dist, dl = cv2.distanceTransformWithLabels(mask, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_CCOMP)
        cc2mod = {}
        for i, c in enumerate(mods, 1):
            ys, xs = np.where(lab == c[0])
            cc2mod[int(dl[ys[0], xs[0]])] = i
        per: collections.Counter = collections.Counter()
        own: list = []
        near: list = []  # the module of EVERY box (closet ones too)
        for b, c in zip(boxes, counted):
            m = None
            cx, cy = min(W - 1, max(0, int((b[0] + b[2]) / 2))), min(H - 1, max(0, int((b[1] + b[3]) / 2)))
            if dist[cy, cx] <= 1.5 * ppm:
                m = cc2mod.get(int(dl[cy, cx]))
            near.append(m)
            if c and m is not None:
                per[m] += 1
            own.append(m if c else None)
        assign.near = near  # type: ignore[attr-defined]
        return per, own

    # modules numbered in reading order (rows of ~half a module, then left to right) so a note can name one
    def order(cs):
        return sorted(cs, key=lambda c: (round(c[3][1] / (0.5 * dmed[0] * ppm)), c[3][0]))

    mods = order(best + var)
    per, own = assign(mods)
    if var:  # a variant is a room of the row only with a door of its own (not a leftover space at the row's end)
        vids = {c[0] for c in var if per[mods.index(c) + 1] > 0}
        if len(vids) < len(var):
            var = [c for c in var if c[0] in vids]
            mods = order(best + var)
            per, own = assign(mods)
    ks = [per[i] for i in range(1, len(mods) + 1)]
    k = collections.Counter(ks).most_common(1)[0][0]
    dev = [(i, per[i]) for i in range(1, len(mods) + 1) if per[i] != k]
    # ODD symbols (AFCEC east wing PNG, v5: an armchair in rooms 8 and 16 boxed as a door -> 35 vs 33): in a module
    # with ONE door too many, the box at a spot where no usual copy of the room has a door. Spot = |offset| from the
    # module's box centre (mirrored copies of a room give the same |dx|, |dy|).
    odd: list = []
    restore: list = []
    near = assign.near  # type: ignore[attr-defined]
    if k and dev and len(dev) <= 0.25 * len(mods) and all(d in (k - 1, k + 1) for _, d in dev):
        ctr = {i: (stats[c[0]][0] + stats[c[0]][2] / 2, stats[c[0]][1] + stats[c[0]][3] / 2)
               for i, c in enumerate(mods, 1)}

        def spot(j):
            b = boxes[j]
            cx, cy = ctr[near[j]]
            return abs((b[0] + b[2]) / 2 - cx) / ppm, abs((b[1] + b[3]) / 2 - cy) / ppm

        usual = [spot(j) for j, o in enumerate(own) if o is not None and per[o] == k]

        def gap(j):
            sj = spot(j)
            return min(math.hypot(sj[0] - u[0], sj[1] - u[1]) for u in usual)

        for i, d in dev:
            if d == k + 1:
                # ODD symbol (AFCEC east wing PNG, v5: an armchair boxed as a door in rooms 8 / 16 -> 35 vs 33): the box
                # at a spot where no usual copy of the room has a door
                js = [j for j, o in enumerate(own) if o == i]
                dist_j = [gap(j) for j in js]
                far = [j for j, dj in zip(js, dist_j) if dj > 0.6]
                if len(far) == 1 and sorted(dist_j)[-2] <= 0.3:
                    odd.append(far[0])
            else:
                # a door LEFT OUT (closet test) at the spot where every usual copy has a counted door (AFCEC west wing
                # PDF: rooms 4 / 12's entry doors taken for closet doors -> 32 vs 34)
                js = [j for j, (o, c) in enumerate(zip(near, counted)) if o == i and not c and gap(j) <= 0.3]
                if len(js) == 1:
                    restore.append(js[0])
    return {"n": len(mods), "alike": len(best), "variants": len(var), "m2": med, "dims": dmed, "k": k, "dev": dev,
            "other": sum(1 for o, c in zip(own, counted) if c and o is None), "own": own, "odd": odd,
            "restore": restore, "near": near}


def settle_modules(gray, boxes: list, fl: list, ppm: float) -> tuple:
    """closet flags -> the repetition check, then its two repairs: ODD symbols out (a box where no copy of the room has a
    door) and doors the closet test LEFT OUT at a spot where every usual copy has a counted door put back; the check is
    run again on the result. -> (flags (True = not counted), rep | None, {"odd": [(box #, module #)], "restored": [...]})"""
    rep = repeated_modules(gray, boxes, [not c for c in fl], ppm)
    fix: dict = {"odd": [], "restored": []}
    if rep and (rep["odd"] or rep["restore"]) and os.environ.get("STOA_DR_ODD", "1") != "0":
        fix = {"odd": [(j, rep["near"][j]) for j in sorted(rep["odd"])],
               "restored": [(j, rep["near"][j]) for j in sorted(rep["restore"])]}
        odd, rest = set(rep["odd"]), set(rep["restore"])
        fl = [(c and j not in rest) or j in odd for j, c in enumerate(fl)]
        rep = repeated_modules(gray, boxes, [not c for c in fl], ppm) or rep
    return fl, rep, fix


def fix_notes(fix: dict, n: int, what: str = "door") -> list[str]:
    """Assumption lines for settle_modules' repairs."""
    out = []
    for key, verb in (("odd", "left out"), ("restored", "counted after all")):
        got = fix.get(key) or []
        if not got:
            continue
        k = len(got)
        mods = ", ".join(str(m) for m in sorted(m for _, m in got))
        mods = " and ".join(mods.rsplit(", ", 1))
        why = (f"{'each sits' if k > 1 else 'it sits'} where none of the other {n - k} identical rooms has a {what} "
               "(furniture read as a door?)" if key == "odd" else
               f"the closet test had taken {'them for closet doors' if k > 1 else 'it for a closet door'}, but every "
               "other copy of the "
               f"room has a counted {what} at that spot")
        out.append(f"{k} {what} symbol{'s' if k > 1 else ''} {verb} (room module{'s' if k > 1 else ''} {mods}): {why}")
    return out


_SIDES = ("NORTH", "SOUTH", "EAST", "WEST")


def _text_rows(path: str, page: int = 0) -> list[str]:
    """The sheet's text as display rows (upper case, left to right): PDF lines (display space) or an image's CACHED
    text layer (never a cold OCR); OCR tiles that cut a word ('WEST WING D' + 'ING DORMITORY =') are joined on their
    overlap."""
    items: list = []  # (x0, y0, x1, y1, text) in display space
    try:
        if str(path).lower().endswith(".pdf"):
            import pymupdf

            with pymupdf.open(path) as doc:
                pg = doc[page]
                rm = pg.rotation_matrix
                for b in pg.get_text("dict").get("blocks", []):
                    for ln in b.get("lines", []):
                        t = " ".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                        if t:
                            r = pymupdf.Rect(ln["bbox"]) * rm
                            items.append((r.x0, r.y0, r.x1, r.y1, t))
        else:
            from stoa.sheet.sets import _cached_layer

            layer = _cached_layer(os.path.abspath(path))
            for it in getattr(layer, "items", None) or []:
                if it.text.strip():
                    items.append((*[float(v) for v in it.box[:4]], it.text.strip()))
    except Exception:  # noqa: BLE001
        return []
    items.sort(key=lambda t: ((t[1] + t[3]) / 2, t[0]))
    rows: list = []
    for it in items:
        h, cy = max(1.0, it[3] - it[1]), (it[1] + it[3]) / 2
        if rows and abs(rows[-1][0] - cy) <= 0.5 * h:
            rows[-1][1].append(it)
        else:
            rows.append([cy, [it]])
    out = []
    for _, its in rows:
        its.sort(key=lambda t: t[0])
        # a row is one text run only while its pieces are near each other (two tables side by side = two rows)
        run, last_x1 = "", None
        for x0, y0, x1, y1, t in its:
            t = t.upper()
            if last_x1 is not None and x0 - last_x1 > 6 * max(1.0, y1 - y0):
                out.append(run)
                run = ""
            if run:
                k = next((k for k in range(min(len(run), len(t)), 1, -1) if run.endswith(t[:k])), 0)
                run = run + t[k:] if k else run + " " + t
            else:
                run = t
            last_x1 = x1 if last_x1 is None else max(last_x1, x1)
        out.append(run)
    return [re.sub(r"\s+", " ", r).strip() for r in out if r.strip()]


def printed_room_count(path: str, page: int, wing: str, question: str = "") -> dict | None:
    """The printed ROOM COUNT table's rooms for this floor and the wing's share of them (AFCEC A-2: 'SECOND FLOOR =
    48 DORMS'; 'NORTH WING DORMITORY = 6,285 SF' of 3 wings' 18,472 -> ~16; without the areas: 48 / 3 wings).
    -> {count, word, floor, expected, how} or None."""
    rows = _text_rows(path, page)
    if not rows:
        return None
    cnt = []
    for r in rows:
        for m in re.finditer(r"\b([A-Z0-9]+)\s+FLOOR\s*=\s*(\d{1,4})\s+([A-Z]{3,})", r):
            cnt.append((m.group(1), int(m.group(2)), m.group(3)))
    if not cnt:
        return None
    floor_words = [w for r in rows for w in re.findall(r"\b([A-Z0-9]{3,})\s+FLOOR\s+PLAN\b", r)]
    floor_words += re.findall(r"\b([a-z0-9]{3,})\s+floor\b", question.lower())
    pick = [c for c in cnt if any(c[0].endswith(f.upper()) or f.upper().endswith(c[0]) for f in floor_words)]
    if pick:
        floor, count, word = pick[0]
    elif len({c[1] for c in cnt}) == 1:
        floor, count, word = "every", cnt[0][1], cnt[0][2]
    else:
        return None
    stem = word[:-1] if word.endswith("S") else word
    areas: dict = {}
    sides, aword = set(), stem
    for r in rows:
        sides |= set(re.findall(r"\b(NORTH|SOUTH|EAST|WEST)\s+WING\b", r))
        for m in re.finditer(r"\b(NORTH|SOUTH|EAST|WEST)\s+WING\s+([A-Z]{3,})\s*=\s*([\d,]{3,})\s*(?:SF|SQ|M)", r):
            if m.group(2)[:4] == stem[:4]:
                areas[m.group(1)] = float(m.group(3).replace(",", ""))
                aword = m.group(2)
    side = (wing or "").upper()
    where = f"'{floor} FLOOR = {count} {word}'" if floor != "every" else f"'{count} {word}' on every floor"
    if not wing:  # the whole floor
        exp = float(count)
        how = f"the printed ROOM COUNT {where}"
    elif side in areas and len(areas) >= 2 and len(areas) >= len(sides):
        # every wing the sheet names has its area read (a fresh upload's OCR missed 'NORTH WING DORMITORY = 6,285 SF':
        # east's share over 2 wings = 24 'differs' from 16 -> conf 0.45; otherwise the equal split below)
        tot = sum(areas.values())
        exp = count * areas[side] / tot
        how = (f"the printed ROOM COUNT {where} x the {wing} wing's share of the {aword} area "
               f"({areas[side]:,.0f} of {tot:,.0f} SF over {len(areas)} wings) = {exp:.1f}")
    elif side in sides and len(sides) >= 2:
        exp = count / len(sides)
        how = (f"the printed ROOM COUNT {where} over the {len(sides)} wings the sheet names "
               f"({' / '.join(s for s in _SIDES if s in sides)}) = {exp:.1f} per wing if they hold equal numbers")
    else:
        return None
    return {"count": count, "word": word, "floor": floor, "expected": exp, "how": how}


def repetition_line(rep: dict, total: int, printed: dict | None, wing: str) -> tuple[str, bool]:
    """'16 room modules x 2 doors = 32 + 2 other doors = 34 (= the 34 doors counted); ...' + whether it all agrees."""
    n, k, other = rep["n"], rep["k"], rep["other"]
    kinds = f"{rep['alike']} alike" + (f" + {rep['variants']} larger / smaller variant{'s' if rep['variants'] > 1 else ''}"
                                       if rep["variants"] else "")
    s = (f"Repetition check: {n} repeated room modules {f'in the {wing} wing' if wing else 'on the plan'} ({kinds}, "
         f"~{rep['m2']:.0f} m² of free floor "
         f"each) x {k} door{'s' if k != 1 else ''} = {n * k}")
    dev_sum = sum(d - k for _, d in rep["dev"])
    if rep["dev"]:
        s += (f"; {'; '.join(f'module {i} shows {d}' for i, d in rep['dev'][:6])}"
              f" ({'+' if dev_sum >= 0 else ''}{dev_sum})")
    s += f" + {other} other door{'s' if other != 1 else ''} (stair, links, outer doors) = {n * k + dev_sum + other}"
    s += f"; matched {total} door symbols"
    if rep["dev"]:
        # the odd modules are where to look: a closet door taken for a room door (+1) or a door symbol missed (-1)
        s += (f" ({n * k + other} if {'that module has' if len(rep['dev']) == 1 else 'those modules have'} {k} like "
              f"the other {n - len(rep['dev'])}: check {'it' if len(rep['dev']) == 1 else 'them'})")
    ok = not rep["dev"] and n * k + other == total
    if printed:
        agree = abs(printed["expected"] - n) <= 0.5
        s += f"; {printed['how']} -> {'agrees' if agree else f'differs from the {n} modules found'}"
        ok = ok and agree
    return s, ok


def rep_scattered(rep: dict | None, what: str = "door") -> str | None:
    """H 00:4x (AFCEC A-2 on the gates' detector v2: west wing 45 @0.55, north 36 @0.55, Ziying 34): when more than a
    quarter of the identical rooms show a different count than the usual one, the symbols found are not consistent
    across identical rooms (v2: armchairs boxed, corridor-cupboard leaves missed, so the closet test drops nothing) ->
    the line to say, and the caller caps the confidence at 0.3. None when the rooms agree well enough."""
    if not rep or rep["n"] < 4 or len(rep["dev"]) <= 0.25 * rep["n"] or os.environ.get("STOA_DR_SCATTER", "1") == "0":
        return None
    return (f"the repetition check found {len(rep['dev'])} of the {rep['n']} identical rooms with a different {what} "
            f"count than the usual {rep['k']}: the {what} symbols found are not consistent across identical rooms here "
            f"(closet / wardrobe doors or furniture may have been taken for {what}s, or {what}s missed), so this is a "
            "rough count")


def keyplan_check(path: str, question: str, page: int, arm_box: list, wing: str) -> tuple:
    """Captain 23:04 tip 1 (the wing is shaded on the sheet's KEY PLAN): VP's stoa.sheet.keyplan.zone_for -> the key
    plan's zone mapped onto the main plan; compare it with the arm found from the building's outline. -> (assumption
    line | None, True agree / False differ / None no key plan). Boxes in evidence space (pdf_pt / image px)."""
    if os.environ.get("STOA_DR_KEYPLAN", "1") == "0":
        return None, None
    try:
        from stoa.sheet.keyplan import zone_for

        z = zone_for(path, question, page)
    except Exception:  # noqa: BLE001  (not on this branch yet / no text layer)
        return None, None
    pb = (z or {}).get("plan_box")
    if not pb:
        return None, None
    ix = max(0.0, min(pb[2], arm_box[2]) - max(pb[0], arm_box[0]))
    iy = max(0.0, min(pb[3], arm_box[3]) - max(pb[1], arm_box[1]))
    inter = ix * iy
    area = lambda b: max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))  # noqa: E731
    iou = inter / (area(pb) + area(arm_box) - inter)
    agree = iou >= 0.5
    name = z.get("name") or f"{wing} wing"
    return (f"the sheet's KEY PLAN shades the {name}: mapped onto the plan it "
            + (f"matches the wing found from the building's outline (overlap {iou:.0%})" if agree else
               f"differs from the wing found from the building's outline (overlap {iou:.0%}; key plan "
               f"{', '.join(f'{v:.0f}' for v in pb)}): check which part is meant")), agree


def full_res(img: str) -> tuple[str, float]:
    """The full-resolution original of a shrunk sheet picture (captain 23:04: never count small symbols on the shrunk
    copy) -> (path, k = original px per copy px); (img, 1.0) when there is none. Two conventions: the eval / CLI copy
    '<stem>.sNNNN.png' next to '<stem>.png', and the web upload shrunk IN PLACE with the original kept as
    '<stem>.full.png' (web/server/normalize_upload.py). The original must have the copy's aspect ratio."""
    if os.environ.get("STOA_DR_WING_FULLRES", "1") == "0":
        return img, 1.0
    cands = []
    mo = re.match(r"^(.*)\.s\d+(\.\w+)$", img)
    if mo:
        cands += [mo.group(1) + ext for ext in dict.fromkeys((mo.group(2), ".png", ".jpg", ".jpeg", ".tif", ".tiff"))]
    root, ext = os.path.splitext(img)
    if ext and not root.endswith(".full"):
        cands.append(f"{root}.full{ext}")
    for orig in cands:
        if os.path.exists(orig):
            try:
                from PIL import Image

                with Image.open(img) as a, Image.open(orig) as o:
                    if o.size[0] > a.size[0] and abs(o.size[0] / a.size[0] - o.size[1] / a.size[1]) < 0.01:
                        return orig, o.size[0] / a.size[0]
            except Exception:  # noqa: BLE001
                pass
            if mo:
                break
    return img, 1.0


def cut_line_leaves(outs: list, inside: list, counted: list, on_edge, ppm: float) -> list:
    """A wing's boundary DOUBLE door whose leaves swing different ways (AFCEC side wings: a double-egress pair in the
    corridor's end wall, one leaf into the corridor, one into the core). The detector boxes each leaf; the core-side
    leaf's centre is outside the wing (dropped), and the corridor-side leaf sits next to the corridor-end cupboards, so
    the closet test takes it (or its box also holds a cupboard leaf). -> the boxes OUTSIDE the wing that touch (gap
    <= 0.2 m) a box inside it on the cut line, when none of those touching boxes is counted: one door per such
    leaf (leaves touching each other = one door). A counted partner = that door is counted already (north wing link).
    `outs` / `inside` / `counted`: [x0, y0, x1, y1, ...] in one pixel space; on_edge(box) -> bool."""
    if os.environ.get("STOA_DR_CUT_LEAF", "1") == "0":
        return []
    gap = 0.2 * ppm

    def apart(a, b) -> float:
        dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
        dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
        return (dx * dx + dy * dy) ** 0.5

    done = {id(b) for b in counted}
    edge_in = [b for b in inside if on_edge(b)]
    got: list = []
    for o in outs:
        partners = [b for b in edge_in if apart(o, b) <= gap]
        if not partners or any(id(b) in done for b in partners) or any(apart(o, g) <= gap for g in got):
            continue
        got.append(o)
    return got


def closet_doors(gray, boxes: list, ppm: float, max_m2: float = 2.6) -> list[bool]:
    """Which door boxes open onto a CLOSET: a small EMPTY enclosed space (0.4-2.6 m², mean width >= 0.45 m, its free
    floor >= 80 % of its bounding box) on one side of
    the door (Ziying's convention on Stoa's sheets: cupboard / closet / wardrobe doors are not counted as doors; DfE
    115 = 128 drawn - 13 cupboards; AFCEC north wing 34 = 16 rooms x (entry + bathroom) + 2). The symbol cannot tell
    (a wardrobe pair is a normal double swing): the space behind it can. `gray` = the crop the boxes are in (px),
    every door box is blocked so rooms stay apart at their doorways; seeds just outside each box side."""
    import numpy as np

    n, lab, stats = _floor_spaces(gray, boxes)
    H, W = lab.shape
    px2 = ppm * ppm
    out = []
    for b in boxes:
        x0, y0, x1, y1 = (float(v) for v in b[:4])
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hit = False
        for dx, dy, sx, sy in ((0, -1, cx, y0), (0, 1, cx, y1), (-1, 0, x0, cy), (1, 0, x1, cy)):
            for step in np.arange(0.12, 0.42, 0.06):  # walk out of the jamb / wall ink up to 0.4 m
                px, py = int(sx + dx * (3 + step * ppm)), int(sy + dy * (3 + step * ppm))
                if not (0 <= px < W and 0 <= py < H):
                    break
                k = lab[py, px]
                if k == 0:
                    continue
                x, y, bw, bh, a = stats[k]
                edge = x == 0 or y == 0 or x + bw >= W or y + bh >= H  # runs off the crop: a big space
                m2, width = a / px2, a / max(bw, bh) / ppm
                # an EMPTY box: a closet's free floor fills its bounding box (AFCEC 0.93-1.0); a bathroom's pocket
                # next to its door wraps round the fixtures (0.47-0.49 at 1.8-1.9 m²)
                if not edge and 0.4 <= m2 <= max_m2 and width >= 0.45 and a >= 0.8 * bw * bh:
                    hit = True
                break
            if hit:
                break
        out.append(hit)
    return out


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
    longs: dict = {"v": [], "h": []}  # stroked axis lines in a wall pen: a wall face running THROUGH a unit = no opening
    with pymupdf.open(pdf) as doc:
        drawings = doc[page].get_drawings()
    face_pen = os.environ.get("STOA_DR_FACE_PEN", "1") != "0"
    for dr in drawings:
        if dr.get("type") == "f":
            continue
        if face_pen and (dr.get("width") or 0) <= 0.36:
            # F2 23:58: only a WALL pen running through closes the opening; the facade's fine sill / outline line (DfE
            # SEN L01, 0.24 pt over 3-29 m) dropped 32 of 47 window units (48 -> 15)
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
    if os.environ.get("STOA_DR_MULLION", "1") == "0":
        return keep
    # S0 21:33 (d28 sealed: a room's windows 2 -> 4): lights that touch end to end along one wall line (a mullion,
    # gap < 0.15 m; EFA: 0.95 + 1.5 m sashes 5 cm apart) are ONE window; a solid pier (>= 0.3 m) separates windows
    # (X1 EFA: 'window units split by a solid pier'; Ziying dfe-08: 'between solid piers counts once')
    keep.sort(key=lambda u: (u["a"], round(u["offs"][0] / (0.1 * m)), u["lo"]))
    merged: list[dict] = []
    for u in keep:
        prev = merged[-1] if merged else None
        if prev is not None and prev["a"] == u["a"] and abs(prev["offs"][0] - u["offs"][0]) < 0.1 * m and \
                abs(prev["offs"][-1] - u["offs"][-1]) < 0.1 * m and -0.05 * m <= u["lo"] - prev["hi"] < 0.15 * m:
            lo, hi = prev["lo"], max(prev["hi"], u["hi"])
            o0, o1 = min(prev["offs"][0], u["offs"][0]), max(prev["offs"][-1], u["offs"][-1])
            box = (o0, lo, o1, hi) if u["a"] == "v" else (lo, o0, hi, o1)
            merged[-1] = dict(prev, hi=hi, box=box, len_m=round((hi - lo) / m, 2), lights=prev.get("lights", 1) + 1)
            continue
        merged.append(u)
    return merged


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
# the set's DOOR SCHEDULE (evidence order (a); captain 21:13: 'plan 115 · schedule 112 · tags missing: ...')
# door numbers: '101A', 'D12', 'D-12', and the US module form '7.04B' / '19.01A' (AFCEE A-602; xref's row key misses it)
_SCHED_KEY = re.compile(r"^(?:\d{1,3}[A-Z]?\.\d{1,3}[A-Z]?|\d{2,4}[A-Z]?|[A-Z]{1,2}-?\d{1,3}[A-Z]?)$")  # + SFO '9A.1A'
_ODD_KEY = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-]{0,7}$")  # any short id INSIDE a door-number column (SFO A-303 '1z')
_DOOR_SCHED = re.compile(r"\bDOOR\s+(?:AND\s+FRAME\s+|&\s+FRAME\s+)?SCHEDULE\b", re.I)
_FTIN = re.compile(r"^(\d{1,2})\s*['’]\s*-?\s*(\d{1,2})?(?:\s+(\d{1,2})/(\d{1,2}))?\s*[\"”]?$")


def _dim_m(t: str) -> float | None:
    """A schedule cell -> metres: 3' - 0" / 10'-0" / 3' - 1 1/2" (ft-in), 900 / 900 mm, 0.9 m; else None."""
    t = (t or "").strip()
    m = _FTIN.match(t)
    if m:
        frac = int(m.group(3)) / int(m.group(4)) if m.group(3) and int(m.group(4)) else 0.0
        return round(((int(m.group(1)) * 12) + int(m.group(2) or 0) + frac) * 0.0254, 3)
    m = re.match(r"^(\d{3,4})\s*(?:mm)?$", t)
    if m and 400 <= int(m.group(1)) <= 6000:
        return int(m.group(1)) / 1000
    m = re.match(r"^([0-5]\.\d{1,3})\s*m?$", t)
    return float(m.group(1)) if m else None


@functools.lru_cache(maxsize=16)
def _door_schedule_cached(pdf: str, mtime: float, skip: int, max_pages: int) -> tuple:
    from stoa.features import xref
    import pymupdf

    with pymupdf.open(pdf) as doc:
        n = len(doc)
        pages = [i for i in range(min(n, max_pages)) if i != skip and _DOOR_SCHED.search(doc[i].get_text("text") or "")]
    rows: dict[str, dict] = {}
    for p in pages:
        got = xref._lines_of(pdf, p)
        if not got or not got[0]:
            continue
        try:  # this sheet's number only (a whole-set index of a 121-page set costs seconds)
            sheet = xref._sheet_from(got[0], got[1], got[2], p).number
        except Exception:  # noqa: BLE001
            sheet = None
        lines = [l for l in got[0] if l.dir == (1, 0)]
        heads = [l for l in lines if _DOOR_SCHED.search(l.text)]
        keys = [l for l in lines if _SCHED_KEY.match(l.text.strip()) and any(ch.isdigit() for ch in l.text)]
        # key columns: keys whose centres line up, split where the rows stop following each other
        cols: list[list] = []
        for l in sorted(keys, key=lambda l: (l.box[0] + l.box[2]) / 2):
            c = (l.box[0] + l.box[2]) / 2
            if cols and c - (cols[-1][-1].box[0] + cols[-1][-1].box[2]) / 2 <= max(4.0, 0.5 * (l.box[3] - l.box[1])):
                cols[-1].append(l)
            else:
                cols.append([l])
        runs = []
        for col in cols:
            col.sort(key=lambda l: l.box[1])
            h = sorted(l.box[3] - l.box[1] for l in col)[len(col) // 2]
            cur = [col[0]]
            for a, b in zip(col, col[1:]):
                if b.box[1] - a.box[1] > 3.5 * h + 6:
                    runs.append((cur, h))
                    cur = []
                cur.append(b)
            runs.append((cur, h))
        good = []  # (column, h, its DOOR SCHEDULE heading): door-id columns only
        for col, h in runs:
            if len(col) < 3:
                continue
            top = col[0]
            above = [hd for hd in heads if hd.box[3] <= top.box[1] + 1 and top.box[1] - hd.box[3] < 30 * h
                     and hd.box[0] - 60 * h <= top.box[0] <= hd.box[2] + 10 * h]
            if not above:
                continue                                  # numbers under no DOOR SCHEDULE heading
            # the column's own header must name a DOOR id ('Door' / 'Number', 'MARK', 'NO.'); a 'Room Number',
            # 'Door Type', 'Hardware Group', 'Frame' or 'WIDTH' column holds codes / sizes (A-801: B2, 01, AL-1)
            kx = (top.box[0] + top.box[2]) / 2
            own = " ".join(l.text for l in sorted(lines, key=lambda l: l.box[1])
                           if top.box[1] - 4.5 * h <= l.box[1] and l.box[3] <= top.box[1] + 1
                           and abs((l.box[0] + l.box[2]) / 2 - kx) <= max(30.0, 2 * h))
            strong = re.search(r"\bdoor\s*(?:no\b\.?|number|num\b|#|mark|tag)|\bmark\b|\bopening\s*(?:no\b|number)",
                               own, re.I)  # AW A-601: one merged header line 'DOOR NO. TYPE'
            weak = re.search(r"\b(?:door|no\.?|number|num|opening|tag)\b|#", own, re.I)
            neg = re.search(r"\b(?:room|type|hardware|group|frame|finish|keynote|set|matl|material)\b", own, re.I)
            if strong or (weak and not neg):
                hd = max(above, key=lambda x: x.box[3])
                # odd ids in the same column between its header and its last row ('1z'): rows too
                x0, x1 = min(l.box[0] for l in col), max(l.box[2] for l in col)
                have = {id(l) for l in col}
                for l in lines:
                    if id(l) in have or not _ODD_KEY.match(l.text.strip()) or not any(ch.isdigit() for ch in l.text):
                        continue
                    ov = min(x1, l.box[2]) - max(x0, l.box[0])
                    if ov >= 0.5 * min(x1 - x0, l.box[2] - l.box[0]) and top.box[1] - 1 <= l.box[1] and \
                            l.box[3] <= col[-1].box[3] + 1:
                        col.append(l)
                col.sort(key=lambda l: l.box[1])
                good.append((col, h, hd))
        starts = sorted(c[0].box[0] for c, _, _ in good)
        for col, h, hd in good:
            top = col[0]
            right = min([x for x in starts if x > top.box[2] + 5] or [top.box[0] + 80 * h])  # the next table's keys
            hdr = [l for l in lines if hd.box[3] - 1 <= l.box[1] and l.box[3] <= top.box[1] + 1
                   and top.box[0] - 10 <= l.box[0] < right]
            wcol = next(((l.box[0] + l.box[2]) / 2 for l in hdr if re.match(r"^(?:width|w)\b", l.text.strip(), re.I)), None)
            hcol = next(((l.box[0] + l.box[2]) / 2 for l in hdr if re.match(r"^(?:height|h)\b", l.text.strip(), re.I)), None)
            seen = {}
            for l in col:
                k = l.text.strip().upper()
                seen[k] = seen.get(k, 0) + 1
            for l in col:
                yc = (l.box[1] + l.box[3]) / 2
                band = sorted([m for m in lines if abs((m.box[1] + m.box[3]) / 2 - yc) <= 0.6 * h
                               and l.box[0] - 4 <= m.box[0] < right - 4], key=lambda m: m.box[0])
                dims = [(m, _dim_m(m.text)) for m in band if m is not l]
                dims = [(m, v) for m, v in dims if v]

                def at(cx):
                    if cx is None:
                        return None
                    best = min(dims, key=lambda mv: abs((mv[0].box[0] + mv[0].box[2]) / 2 - cx), default=None)
                    return best[1] if best and abs((best[0].box[0] + best[0].box[2]) / 2 - cx) <= 40 else None

                w = at(wcol) if wcol is not None else (dims[0][1] if dims else None)
                ht = at(hcol) if hcol is not None else (dims[1][1] if len(dims) > 1 else None)
                k = l.text.strip().upper()
                if k in rows:
                    continue
                src = [m.src for m in band] or [l.src]
                # one door number on 2 rows = a pair listed leaf by leaf (AW A-601: 101A x2, 915 mm each)
                rows[k] = {"key": k, "page": p, "sheet": sheet, "width_m": w, "height_m": ht,
                           "leaves": min(seen.get(k, 1), 2), "text": " | ".join(m.text for m in band)[:160],
                           "bbox": [round(min(s[0] for s in src) - 2, 1), round(min(s[1] for s in src) - 1, 1),
                                    round(max(s[2] for s in src) + 2, 1), round(max(s[3] for s in src) + 1, 1)]}
    return tuple(rows.values())


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def set_of(pdf: str, page: int = 0) -> tuple[str, int]:
    """The drawing SET a one-page copy was cut from (stoa.formats.pages.one_page_copy writes '<stem>.p<N>.pdf' next to
    the upload): -> (the set's PDF, the copy's page in it); else (pdf, page). The upload path solves the plan page
    alone (d5e6b1c4d3.p1.pdf), and the door schedule is on another page of the set (A-602 = page 2)."""
    try:  # P1 b559b0e: every one_page_copy writes '<copy>.json' = {original, page} (also in cache dirs)
        d = json.loads(Path(str(pdf) + ".json").read_text())
        orig, k = str(d["original"]), int(d["page"])
        if Path(orig).exists() and not _same_file(orig, pdf):
            return orig, k
    except (OSError, ValueError, KeyError, TypeError):
        pass
    m = re.match(r"^(.+)\.p(\d{1,3})\.pdf$", Path(pdf).name, re.I)
    if not m:
        return pdf, page
    cand = Path(pdf).with_name(m.group(1) + ".pdf")
    k = int(m.group(2)) - 1
    try:
        import pymupdf

        if not cand.exists():
            return pdf, page
        with pymupdf.open(str(cand)) as d, pymupdf.open(pdf) as c:
            if len(d) <= k or len(c) != 1 or tuple(round(v) for v in d[k].rect) != tuple(round(v) for v in c[0].rect):
                return pdf, page
        return str(cand), k
    except Exception:  # noqa: BLE001
        return pdf, page


def door_schedule(pdf: str, skip_page: int = -1, max_pages: int = 150) -> list[dict]:
    """The door rows of every DOOR SCHEDULE table in this PDF (other than skip_page): [{key '7.04B', page, sheet
    'A-602', width_m, height_m, text, bbox (pdf_pt, the row)}], one per door number. Text only, cached."""
    if os.environ.get("STOA_DR_SCHEDULE", "1") == "0" or not str(pdf).lower().endswith(".pdf"):
        return []
    try:
        return [dict(r) for r in _door_schedule_cached(str(pdf), os.stat(pdf).st_mtime, int(skip_page), int(max_pages))]
    except Exception:  # noqa: BLE001 — the schedule is a cross-check, never a reason to fail the count
        return []


def schedule_scope(pdf: str, page: int, rows: list[dict], cells, sel) -> list[dict]:
    """Schedule rows whose door is in the counted view(s) of `page`: the door's own tag printed there ('101A'), or
    the US convention door number = ROOM number + letter ('7.04B' -> room '7.04' labelled in the view). Each row
    gets 'at' = the printed tag / room number box (pdf_pt) and 'how' ('tag' | 'room')."""
    from stoa.features import xref

    got = xref._lines_of(pdf, page)
    if not got or not got[0] or not rows:
        return []
    toks: dict[str, list] = {}
    for l in got[0]:
        ctr = ((l.box[0] + l.box[2]) / 2, (l.box[1] + l.box[3]) / 2)
        if sel is not None and cells is not None:
            c = OpeningsSolver._in_cells(ctr, cells)
            if c is None or not any(c is s for s in sel):
                continue
        for t in re.split(r"\s+", l.text.strip().upper()):
            toks.setdefault(t.strip(".,;:()"), []).append(l.src)
    out = []
    for r in rows:
        k = r["key"]
        how, at = None, None
        if k in toks:
            how, at = "tag", toks[k][0]
        elif k[-1].isalpha() and any(ch.isdigit() for ch in k[:-1]) and len(k) >= 3 and k[:-1].rstrip("-") in toks:
            how, at = "room", toks[k[:-1].rstrip("-")][0]
        if how:
            out.append(dict(r, how=how, at=[round(v, 1) for v in at]))
    # the room convention needs the schedule to follow it: >= 2 rows by room number, or every row by its own tag
    if sum(1 for r in out if r["how"] == "room") == 1 and not any(r["how"] == "tag" for r in out):
        return []
    return out


def match_schedule(doors: list[dict], rows: list[dict], scale_n: float | None) -> tuple[list, list, list]:
    """Drawn doors <-> schedule rows in the same view: by opening width (schedule width vs the drawn leaf / pair
    opening, <= 0.3 m) and nearness to the row's tag / room number, then leftovers by nearness alone (<= 8 m).
    -> (pairs [(door index, row)], rows with no drawn door, door indices with no row)."""
    mpt = 1.0 / (PT_M * scale_n) if scale_n else None  # pdf_pt per metre
    cand = []
    for i, d in enumerate(doors):
        cx, cy = d["center"]
        wd = opening_width_m(d, scale_n) if scale_n else None
        for j, r in enumerate(rows):
            ax, ay = (r["at"][0] + r["at"][2]) / 2, (r["at"][1] + r["at"][3]) / 2
            dist_m = math.hypot(cx - ax, cy - ay) / mpt if mpt else 0.0
            rw = r.get("width_m")
            if rw and (r.get("leaves") or 1) >= 2 and rw < 1.3:
                rw = 2 * rw  # a pair listed leaf by leaf: the drawn opening is both leaves
            dw = abs(wd - rw) if wd and rw else None
            cand.append((i, j, dw, dist_m))
    pairs, used_d, used_r = [], set(), set()
    for limit_w, limit_d in ((0.3, 15.0), (None, 8.0)):
        pool = sorted((c for c in cand if (limit_w is None or (c[2] is not None and c[2] <= limit_w)) and c[3] <= limit_d),
                      key=lambda c: ((c[2] or 0) / 0.3 + c[3] / 5.0) if limit_w is not None else c[3])
        for i, j, _, _ in pool:
            if i in used_d or j in used_r:
                continue
            pairs.append((i, rows[j]))
            used_d.add(i)
            used_r.add(j)
    return pairs, [r for j, r in enumerate(rows) if j not in used_r], [i for i in range(len(doors)) if i not in used_d]


def _ftin(m: float | None) -> str:
    if not m:
        return "?"
    inch = round(m / 0.0254)
    return f"{inch // 12}'-{inch % 12}\""


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


def _fullres_for_det(img: str, ppm: float) -> tuple[str, float]:
    """(original, k) for a whole-plan detector pass on a SHRUNK picture whose copy draws doors below 46 px/m even at
    zoom 1 (captain 23:04: never count small symbols on the shrunk copy), else (img, 1.0). Only when the detector runs
    on a GPU (its /health names a 'remote' offload): on the CPU service a 10200 px sheet takes minutes, beyond the
    question's 25 s budget, so the copy is read as before. STOA_DR_FULLRES_DET=0 off, =1 also on a CPU service."""
    mode = os.environ.get("STOA_DR_FULLRES_DET", "auto")
    if mode == "0" or ppm >= 46:
        return img, 1.0
    src, k = full_res(img)
    if k <= 1.0:
        return img, 1.0
    if mode != "1":
        h = _det_health(os.environ.get("STOA_DET_URL", "http://127.0.0.1:8140")) or {}
        if not h.get("remote"):
            return img, 1.0
    return src, k


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
                                                       or bool(_BOTH_Q.search(question)) or _sched_count_q(question))

    def solve(self, path: str, question: str) -> Answer:
        both = _BOTH_Q.search(question)
        if both:
            return self._both(path, question, both)
        if _sched_count_q(question):
            return self._schedule_count(path, question)
        ex = split_exclusion(question) if os.environ.get("STOA_DR_EXCLUDE", "1") != "0" else None
        if ex and os.environ.get("STOA_DR_CLOSET_Q", "1") != "0" and \
                _CLOSET_X.match(ex[1][len("How many "):-len(" are drawn?")].strip()):
            # 'How many doors are in the north wing, excluding closets?' = our convention's count (closets are left
            # out anyway); all minus 'closet doors' would subtract them a second time
            a = self.solve(path, ex[0])
            if a.value is not None:
                a.assumptions = ["closet / wardrobe / cupboard doors are left out, as asked", *(a.assumptions or [])]
            return a
        if ex:
            return self._excluding(path, question, *ex)
        q = parse_question(question)
        if q is None:
            return self.abstain("not a door / window count")
        # the type named in the ASKED clause ('How many overhead doors ...?'), not in a convention clause after it
        # ('How many door leaves ...? ... an overhead door counts as one': X1 sl-252)
        asked = question.split("?")[0] if "?" in question[:-1] else question
        kind = _TYPE_Q.search(asked)
        # door types whose SYMBOL this reader reads itself (captain 21:11 / Ziying's legend xr-24..28): two opposite
        # arrows = sliding; a zigzag / V pair in the wall = folding / louvred. Vector PDFs only (images: detector boxes)
        if kind and q["target"] == "door" and str(path).lower().endswith(".pdf") and \
                os.environ.get("STOA_DR_TYPED", "1") != "0":
            w = kind.group(0).lower()
            dtype = ("sliding" if re.search(r"slid", w) else
                     "folding" if re.search(r"fold|louv", w) else
                     "overhead" if re.search(r"overhead|roll|garage", w) else None)
            others = [m.group(0).lower() for m in _TYPE_Q.finditer(asked)]
            if dtype == "overhead" and all(re.search(r"overhead|roll|garage", o) for o in others):
                # no wall symbol to read: the set's DOOR SCHEDULE rows of the view (>= 2.6 m, or 'OVERHEAD' / 'COILING')
                q["dtype"] = ["overhead"]
                q["scope"] = [w for w in q["scope"] if not re.match(r"overhead|roll|rolling|roller|up|garage|or$", w)]
                q["dtype_word"] = kind.group(0).strip()
                kind = None
            elif dtype and dtype != "overhead" and all(re.search(r"slid|fold|louv", o) for o in others):
                q["dtype"] = sorted({"sliding" if "slid" in o else "folding" for o in others})
                q["scope"] = [w for w in q["scope"] if not re.match(r"slid|slide|fold|bi|louv|or$", w)]
                q["dtype_word"] = kind.group(0).strip()
                kind = None
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
        if q.get("each"):
            # the vector path would count the whole sheet (103 on AFCEC A-2 for 'each dorm room'): not read here yet
            return self.abstain(f"a door count per room ('each {q['each']}') is read from identical rooms on a picture "
                                "only, not on a vector PDF yet")
        if q.get("closets") == "only":
            # the vector reader drops closet doors only on plans of repeated rooms, and does not box them: its count is
            # the walk-through doors, never the closet doors asked for
            return self.abstain("closet / wardrobe doors alone are not counted on a vector PDF yet (only walk-through "
                                "doors are)")
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

    # -- 'how many doors ... if WC cubicle doors are not counted?' = all - the excluded part ----------------------------
    def _excluding(self, path: str, question: str, main_q: str, part_q: str) -> Answer:
        a_all = self.solve(path, main_q)
        a_ex = self.solve(path, part_q)
        trace = [f"openings: '{question[:90]}' = ({main_q}) - ({part_q})"]
        if a_all.value is None:
            return self.abstain("the whole count is not read: " + "; ".join(a_all.trace[-1:]), trace)
        if a_ex.value is None or int(a_ex.value) > int(a_all.value):
            return self.abstain("the excluded doors / windows are not told apart here: "
                                + "; ".join((a_ex.trace or ["?"])[-1:]), trace)
        gone = {tuple(round(v) for v in e.bbox) for e in a_ex.evidence if e.bbox}
        ev = [e for e in a_all.evidence if not (e.bbox and tuple(round(v) for v in e.bbox) in gone)]
        value = int(a_all.value) - int(a_ex.value)
        if len(ev) != value:  # the excluded boxes are not all among the counted ones: keep the arithmetic, say so
            trace.append(f"openings: {len(a_all.evidence) - len(ev)} of the {a_ex.value} excluded boxes are among the "
                         "counted ones")
            ev = ev[:value] if len(ev) > value else ev
        trace += [f"[all] {t}" for t in a_all.trace[-3:]] + [f"[excluded] {t}" for t in a_ex.trace[-3:]]
        return Answer(value=value, unit="count", confidence=round(max(0.0, min(a_all.confidence, a_ex.confidence) - 0.1), 2),
                      evidence=ev, trace=trace,
                      assumptions=[f"{a_all.value} counted in all, minus {a_ex.value} excluded "
                                   f"({part_q[9:-12] if part_q.startswith('How many ') else part_q}) = {value}",
                                   *[x for x in a_all.assumptions if not x.startswith("Counted ")][:3],
                                   *[f"excluded part: {x}" for x in a_ex.assumptions[:1]]],
                      missing=list(dict.fromkeys([*a_all.missing, *a_ex.missing])))

    # -- 'how many doors does the door schedule list (for module 07)?' -----------------------------------------------
    def _schedule_count(self, path: str, question: str) -> Answer:
        """Counted from the DOOR SCHEDULE rows (one per door number), whole schedule or the rows of a named view
        (room number + letter, or the door's own tag printed in it) / 'module N' (door numbers 'N.xx'). X1 sl-249 /
        254 / 255 (A-602: 95 rows; P1 22:00: the table reader found 76)."""
        trace: list[str] = []
        pdf = str(self.abspath(path))
        if not pdf.lower().endswith(".pdf"):
            return self.abstain("a door schedule is read from a PDF's text layer", trace)
        spdf, _ = set_of(pdf)  # an upload may solve one page of the set (the schedule's or the plan's) alone
        rows = door_schedule(spdf)
        if not rows:
            return self.abstain("no DOOR SCHEDULE table with door numbers found in this PDF", trace)
        if spdf != pdf:
            trace.append(f"openings: read the whole set {Path(spdf).name} (this page was cut from it)")
            pdf, path = spdf, spdf
        sheet = rows[0].get("sheet") or f"page {rows[0]['page'] + 1}"
        trace.append(f"openings: {len(rows)} door rows in the DOOR SCHEDULE ({sheet})")
        asked = question.split("?")[0] if "?" in question.rstrip()[:-1] else question
        asked = re.sub(r"\([^)]*\)", " ", asked)
        excl_words: list[str] = []
        mx = re.search(r"\b(?:not\s+counting|excluding|except(?:\s+for)?|other\s+than|apart\s+from|without)\s+(?:the\s+)?"
                       r"([^?.,;]+)", asked, re.I)
        if mx:
            excl_words = [w for w in re.findall(r"[a-z]+", mx.group(1).lower())
                          if w not in _STOP and w not in _SCHED_WORDS and w not in _SCHED_GENERIC]
            asked = asked[:mx.start()] + asked[mx.end():]
        words = [w for w in re.findall(r"[a-z]+|\d+", asked.lower()) if w not in _STOP and w not in _SCHED_WORDS
                 and w not in _SCHED_GENERIC]

        def hits(w: str) -> list[dict]:  # rows whose own text (not the id) carries the word ('sliding' ~ 'SLIDING')
            stem = w.upper() if len(w) < 5 else w.upper()[:max(4, len(w) - 3)]
            out = []
            for r in rows:
                toks = [t for t in re.findall(r"[A-Z0-9]+", (r.get("text") or "").upper()) if t != r["key"]]
                if any(t == w.upper() or (len(w) >= 5 and t.startswith(stem)) for t in toks):
                    out.append(r)
            return out

        quals = [w for w in words if not w.isdigit() and len(w) >= 3 and hits(w)]
        words = [w for w in words if w not in quals]
        if excl_words and not all(hits(w) for w in excl_words):
            return self.abstain(f"'{' '.join(excl_words)}' is not written in any row of the schedule", trace)
        scoped, how = rows, "the whole schedule"
        if words and not _whole_scope(words):
            got = None
            import pymupdf

            with pymupdf.open(pdf) as doc:
                n = len(doc)
            for p in range(min(n, 8)):
                cells = view_cells(view_titles(pdf, p), *_page_wh(pdf, p))
                plans = [c for c in cells if c["kind"] == "plan"]
                named = _scope_match(words, plans)
                if named:
                    inn = schedule_scope(pdf, p, rows, cells, named)
                    if inn:
                        got = (inn, "the rooms of " + "; ".join(repr(c["text"][:50]) for c in named) +
                               f" (sheet page {p + 1})")
                        break
            mod = re.search(r"\b(?:module|wing|block|zone|building|area)\s*0*(\d{1,2})\b", asked, re.I)
            if got is None and mod and sum("." in r["key"] for r in rows) >= 0.9 * len(rows):
                inn = [r for r in rows if r["key"].split(".")[0].lstrip("0") == mod.group(1)]
                if inn:
                    got = (inn, f"door numbers {int(mod.group(1))}.xx")
            if got is None:
                return self.abstain(f"'{' '.join(words)}' does not name a view or module the schedule rows can be "
                                    "scoped to (nor is it written in the rows)", trace)
            scoped, how = got
        if quals:  # rows that say it ('SLIDING', 'SECURE DOOR', 'CASED OPENING'): every word on the same row
            keep = {id(r) for r in rows}
            for w in quals:
                keep &= {id(r) for r in hits(w)}
            scoped = [r for r in scoped if id(r) in keep]
            how += " whose row says " + " + ".join(repr(w.upper()) for w in quals)
        if excl_words:
            gone = {id(r) for r in rows}
            for w in excl_words:
                gone &= {id(r) for r in hits(w)}
            n0 = len(scoped)
            scoped = [r for r in scoped if id(r) not in gone]
            how += f", minus {n0 - len(scoped)} row(s) saying " + " + ".join(repr(w.upper()) for w in excl_words)
        ev = [Evidence(file=path, page=r["page"], bbox=list(r["bbox"]), bbox_space="pdf_pt", exact=True,
                       note=f"door {r['key']} · {sheet} schedule row ({_ftin(r.get('width_m'))} x "
                            f"{_ftin(r.get('height_m'))})") for r in scoped]
        keys = ", ".join(r["key"] for r in scoped[:16]) + (" ..." if len(scoped) > 16 else "")
        trace.append(f"openings: {len(scoped)} rows for {how}: {keys}")
        return Answer(value=len(scoped), unit="count",
                      confidence=0.9 if scoped is rows else (0.8 if quals or excl_words else 0.85), evidence=ev,
                      trace=trace,
                      assumptions=[f"Counted the door numbers the {sheet} DOOR SCHEDULE lists for {how}: one row = "
                                   "one door (a pair of leaves is one row)", "scale: none needed — read from the "
                                   "schedule's text, exact as printed"],
                      missing=[])

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
        wing_ev, wing_rep = None, None
        if q.get("wing") and os.environ.get("STOA_DR_WING", "1") != "0":
            got = self._wing_pdf(pdf, page, path, dict(q, scale_n_eff=sw["scale_n"] or scale_n), doors_in, mat, trace,
                                 assumptions)
            if isinstance(got, Answer):
                return got
            doors_in, wing_ev, wing_rep = got
            room_note = f" in the {q['wing']} wing"
        if q.get("wing"):
            pass
        elif q["scope"] and not q.get("view_named") and _whole_scope(q["scope"]):
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
        if (not room_note and not q.get("side") and not q.get("dtype") and not q["double"] and not q["single"]
                and not q.get("leaves") and sw["scale_n"] and len(doors_in) >= 16 and q.get("closets") != "with"
                and os.environ.get("STOA_DR_CLOSETS", "1") != "0" and os.environ.get("STOA_DR_CLOSETS_ALL", "1") != "0"):
            got = self._closets_vector(pdf, page, doors_in, mat, sw["scale_n"], question, trace)
            if got:
                doors_in, notes = got
                assumptions += notes
        if q.get("dtype") == ["overhead"]:
            chosen = []  # counted from the schedule below, or abstain
        elif q.get("dtype"):
            chosen = [(d, c) for d, c in doors_in if d["kind"] in q["dtype"]]
            if not chosen:  # none of this symbol here: the type may be drawn another way (pocket door, plain leaf)
                return self.abstain(f"'{q['dtype_word']}' is a door type: no {' / '.join(q['dtype'])} door symbol "
                                    "found in the counted scope (a door schedule or the tags would name the types)",
                                    trace)
        elif q["double"]:
            chosen = [(d, c) for d, c in doors_in if d["leaves"] == 2]
        elif q["single"]:
            chosen = [(d, c) for d, c in doors_in if d["leaves"] == 1]
        else:
            chosen = doors_in
        n_doors = len(chosen)
        n_leaves = sum(d["leaves"] for d, _ in chosen)
        n_double = sum(1 for d, _ in chosen if d["leaves"] == 2)
        # the set's DOOR SCHEDULE (captain 21:13; X1 21:22 A-103 + A-602): its rows for the counted view(s) vs the
        # drawn doors. A scheduled door with no symbol read (overhead / coiling doors: a dashed track, no swing) is
        # counted from its row; a drawn door with no row stays counted and is reported.
        sched = None
        if not room_note and not q.get("side") and not q["single"] and q.get("dtype") in (None, ["overhead"]):
            try:
                spdf, spage = set_of(pdf, page)  # an upload solves the plan page alone: its set has the schedule
                rows = door_schedule(spdf, skip_page=spage)
                inn = schedule_scope(pdf, page, rows, all_cells if sel is not None else None, sel) if rows else []
                if inn:
                    all_d = [d for d, _ in doors_in]
                    pairs, extra, undrawn = match_schedule(all_d, inn, sw["scale_n"])
                    sched = {"rows": inn, "pairs": pairs, "extra": extra, "undrawn": [all_d[i] for i in undrawn],
                             "key_of": {id(all_d[i]): r["key"] for i, r in pairs},
                             "sheet": inn[0].get("sheet") or f"page {inn[0]['page'] + 1}", "dropped": [],
                             "scale_n": sw["scale_n"]}
                    # the schedule COVERS these views (most drawn doors have their row): a drawn swing with no row is
                    # a toilet-cubicle partition / cupboard door, which a US door schedule leaves out (A-103 module 05:
                    # a 0.81 m stall door; X1's gold 2 = the schedule's) -> left out of the total, named, and offered
                    if undrawn and not q.get("swing_only") and len(pairs) >= max(2, 0.6 * len(all_d)) and \
                            os.environ.get("STOA_DR_SCHED_DROP", "1") != "0":
                        # only stall / cupboard widths (< 0.86 m; A-103 stall 0.81 m): a full-width door with no row is
                        # a schedule omission (SFO A-107: a 0.92 m door, no 7.08 row) and stays counted, flagged
                        gone = {id(all_d[i]) for i in undrawn
                                if sw["scale_n"] and opening_width_m(all_d[i], sw["scale_n"]) < 0.86}
                        sched["dropped"] = [(d, c) for d, c in chosen if id(d) in gone]
                        chosen = [(d, c) for d, c in chosen if id(d) not in gone]
                        n_doors = len(chosen)
                        n_leaves = sum(d["leaves"] for d, _ in chosen)
                        n_double = sum(1 for d, _ in chosen if d["leaves"] == 2)
            except Exception as e:  # noqa: BLE001 — a cross-check never sinks the count
                trace.append(f"openings: door schedule not read ({type(e).__name__}: {str(e)[:60]})")
                sched = None
        ev = []  # one box per counted object (a door, or a leaf when the question counts leaves)
        for k, (d, c) in enumerate(chosen, 1):
            label = {"double": "double door (2 leaves)", "single": "door", "double-acting": "double-acting door",
                     "folding": "folding / louvred door (2 leaves, no swing arc)",
                     "angled": "door drawn half-open (no swing arc)",
                     "sliding": "sliding door (arrows, no swing arc)"}[d["kind"]]
            where = f" · {c['text'][:40]}" if c else ""
            if sched and id(d) in sched["key_of"]:
                where += f" · schedule {sched['key_of'][id(d)]}"
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
        if q.get("dtype") and q["dtype"] != ["overhead"]:
            # sliding arrows are unambiguous; folding / louvred V pairs also catch stair-door pairs (DfE: 5 read vs
            # Ziying's 4 plant-room louvres) -> lower confidence, and the symbol rule is stated
            conf = 0.7 if q["dtype"] == ["sliding"] else 0.45
            assumptions.insert(1, "Door type read from its symbol: " + " and ".join(
                {"sliding": "sliding = two opposite arrows in the opening",
                 "folding": "folding / louvred = two half-open leaves (a V / zigzag) sharing a jamb, no swing arc"}[t]
                for t in q["dtype"]))
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
        clarify = None
        if q.get("dtype") == ["overhead"] and not sched:
            return self.abstain(f"'{q['dtype_word']}' is a door type with no symbol in the wall: counted from the set's "
                                "DOOR SCHEDULE, and none was read for this view", trace)
        if sched:
            value, conf, missing, clarify = self._schedule_merge(sched, q, path, page, value, conf, missing, n_doors, ev,
                                                                 trace, assumptions)
            if value is None:
                return self.abstain(missing[0] if missing else "the schedule does not answer this door type", trace)
        if wing_ev is not None:
            ev.append(wing_ev)
            conf = min(conf, 0.6)  # the wing's bounds are read from the building's shape, not from a label
            if wing_rep and wing_rep[0] and not sched:
                # every repeated room shows the same doors and they add up (+ the printed room count agrees)
                conf = 0.75 if wing_rep[1] else 0.65
            if wing_rep and wing_rep[2] and not sched:
                conf = min(conf, 0.3)
                assumptions.insert(1, wing_rep[2])
                trace.append("openings: " + wing_rep[2])
            missing = [*missing, "the wing's name on the plan itself (it was bounded by the building's shape)"]
        out = Answer(value=value, unit="count", confidence=round(conf, 2), evidence=ev,
                     trace=trace, assumptions=assumptions, missing=missing)
        if clarify and "clarify" in type(out).model_fields:
            out.clarify = clarify
        return out

    @staticmethod
    def _schedule_merge(sched, q, path, page, value, conf, missing, n_drawn, ev, trace, assumptions):
        """Reconcile the drawn count with the schedule rows of the same views ('plan 4 · schedule 6 · ...')."""
        def pairlike(r):  # a pair: listed leaf by leaf, or 5'-0" .. 8'-6" wide at door height (overhead = one leaf)
            return (r.get("leaves") or 1) >= 2 or bool(r.get("width_m") and 1.5 <= r["width_m"] <= 2.6
                                                       and (r.get("height_m") or 2.1) < 2.6)

        rows, extra, undrawn, sheet = sched["rows"], sched["extra"], sched["undrawn"], sched["sheet"]
        dropped = sched.get("dropped") or []
        if q.get("dtype") == ["overhead"]:
            def ohd(r):
                return bool((r.get("height_m") or 0) >= 2.6 or (r.get("width_m") or 0) > 2.6 or
                            re.search(r"OVERHEAD|COILING|ROLL|SECTIONAL|GARAGE", r.get("text") or "", re.I))

            n_all, n_pairs = len(rows), len(sched["pairs"])
            rows, extra, undrawn, dropped = [r for r in rows if ohd(r)], [r for r in extra if ohd(r)], [], []
            if not extra:
                if n_all and n_pairs >= max(1, 0.5 * n_all):  # the schedule covers this view and lists none
                    line = (f"Door schedule check ({sheet}): {n_all} rows for the counted view(s), {n_pairs} matched to "
                            "drawn doors, none >= 2.6 m or OVERHEAD / COILING / ROLL-UP -> 0 overhead doors")
                    assumptions.insert(1, line)
                    trace.append("openings: " + line)
                    return 0, 0.6, [], None
                return None, 0.0, ["no overhead / roll-up door row in the schedule for this view"], None
        if q["double"] and not any(pairlike(r) for r in rows):
            return value, conf, missing, None  # this schedule does not tell pairs from singles
        if q["double"]:
            rows = [r for r in rows if pairlike(r)]
            extra = [r for r in extra if pairlike(r)]
        # rows only become doors when the schedule really is this plan's: >= half of its rows here found their drawn
        # door (a stray code matching a grid bubble 'A1' must not add a door)
        trusted = len(sched["pairs"]) >= max(1, 0.5 * len(sched["rows"]))
        add = [] if (q.get("swing_only") or not trusted) else extra
        for r in add:
            big = (r.get("height_m") or 0) >= 2.6 or (r.get("width_m") or 0) > 2.6
            note = (f"door {r['key']} · {sheet} door schedule row ({_ftin(r.get('width_m'))} x {_ftin(r.get('height_m'))})"
                    f" · no door symbol read on the plan{' (overhead / coiling door?)' if big else ''}"
                    f" · placed at its {'tag' if r['how'] == 'tag' else 'room number'} on the plan")
            n_leaf = 2 if (q["leaves"] and pairlike(r)) else 1
            for j in range(n_leaf):
                ev.append(Evidence(file=path, page=page, bbox=list(r["at"]), bbox_space="pdf_pt", exact=False,
                                   note=note + (f" · leaf {j + 1} of 2" if n_leaf == 2 else "")))
        n_add = sum(2 if (q["leaves"] and pairlike(r)) else 1 for r in add)
        n_rows = len(rows)
        unit_w = "door leaves" if q["leaves"] else ("double doors" if q["double"] else "doors")
        keys = ", ".join(r["key"] for r in rows[:14]) + (" ..." if len(rows) > 14 else "")
        line = (f"Door schedule check ({sheet}, rows for the counted view(s): {keys}): plan {n_drawn + len(dropped)} · "
                f"schedule {n_rows}")
        clarify = None
        if not extra and not undrawn:
            line += " ✓ (every drawn door has its row, every row its drawn door)"
            conf, missing = max(conf, 0.9), []
        else:
            if extra:
                line += (f" · {len(extra)} scheduled door(s) with no symbol read on the plan: "
                         + ", ".join(f"{r['key']} ({_ftin(r.get('width_m'))} x {_ftin(r.get('height_m'))})"
                                     for r in extra[:8]))
                line += (" -> NOT added ('swing doors' counts swing arcs only)" if q.get("swing_only") else
                         " -> NOT added (fewer than half of these rows match a drawn door: the schedule may not be this "
                         "plan's)" if not add else
                         f" -> counted from the schedule ({value} drawn + {n_add} = {value + n_add} {unit_w})")
            if dropped:
                n_drop = sum(d["leaves"] for d, _ in dropped) if q["leaves"] else len(dropped)
                where = "; ".join(
                    f"{(c['text'][:32] + ' ') if c else ''}{opening_width_m(d, sched['scale_n']):.2f} m at pdf_pt "
                    f"[{', '.join(str(round(v)) for v in d['bbox'])}]" if sched.get("scale_n") else
                    f"pdf_pt [{', '.join(str(round(v)) for v in d['bbox'])}]" for d, c in dropped[:6])
                line += (f" · {len(dropped)} drawn door(s) with no schedule row LEFT OUT (a door schedule does not list "
                         f"toilet-cubicle partitions or cupboard doors): {where}; counting them gives "
                         f"{value + n_add + n_drop} {unit_w}")
                conf = 0.75 if not extra or add else min(conf, 0.7)
                missing = [f"{len(dropped)} drawn door(s) are not in the {sheet} schedule (left out; counting them "
                           f"gives {value + n_add + n_drop})"]
                clarify = {"ask": f"Count the {len(dropped)} drawn door(s) that the {sheet} door schedule does not list?",
                           "why": "the plan and the door schedule disagree",
                           "options": [
                               {"label": f"{value + n_add} {unit_w} (as in the {sheet} door schedule)",
                                "value": value + n_add, "unit": "count", "evidence": list(range(len(ev))),
                                "question": None},
                               {"label": f"{value + n_add + n_drop} {unit_w} (+ {len(dropped)} drawn with no schedule "
                                         "row: cubicle / cupboard doors)", "value": value + n_add + n_drop,
                                "unit": "count", "evidence": [], "question": None}],
                           "click": None}
            if undrawn and len(undrawn) > len(dropped):
                line += (f" · {len(undrawn) - len(dropped)} drawn door(s) of full width with no schedule row nearby "
                         "(kept: a door the schedule misses?)")
                conf = min(conf, 0.6)
                missing = [*(missing if dropped else []), f"{len(undrawn) - len(dropped)} drawn door(s) are not in "
                           f"the {sheet} schedule: check them"]
            elif add and not undrawn:
                conf, missing = max(conf, 0.8), []
        if q.get("dtype") == ["overhead"]:
            if not add:
                return None, 0.0, ["the schedule rows of this view do not match its drawn doors: overhead doors not "
                                   "told apart"], None
            conf = 0.7  # a size / text rule on schedule rows, not a symbol
            assumptions.insert(1, "Overhead / roll-up doors have no swing in the wall: counted from the door schedule "
                                  "rows of this view that the plan draws with no door symbol and that are >= 2.6 m "
                                  "high or wide, or say OVERHEAD / COILING / ROLL-UP")
        value += n_add
        assumptions.insert(1, line)
        trace.append("openings: " + line)
        if add:
            assumptions[:] = [a.replace("overhead / roll-up doors (no symbol in the wall) are not counted",
                                        "doors the set's door schedule lists for these rooms but the plan draws with no "
                                        "symbol read here (overhead / roll-up doors) are counted from their schedule row")
                              for a in assumptions]
        return value, conf, missing, clarify

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
        vsel, win_interior = None, 0
        if vu is not None:
            ctr = lambda u: ((u["box"][0] + u["box"][2]) / 2, (u["box"][1] + u["box"][3]) / 2)  # noqa: E731
            if rooms:
                tol = 0.7 / (PT_M * eff)
                if os.environ.get("STOA_DR_UNIT_EDGE", "1") != "0":
                    vsel = [u for u in vu if any(_unit_on_edge(r["polygon"], u["box"], tol) for r in rooms)]
                else:
                    vsel = [u for u in vu if any(_on_edge(r["polygon"], ctr(u), tol) for r in rooms)]
            else:
                vsel = [u for u in vu if any(cl[0] <= ctr(u)[0] <= cl[2] and cl[1] <= ctr(u)[1] <= cl[3]
                                             for _c, cl in clips)]
            trace.append(f"openings: {len(vsel)} of the {len(vu)} vector window units lie in the asked scope")
            if vsel and not rooms and os.environ.get("STOA_DR_WIN_EXTERIOR", "1") != "0":
                try:
                    ext = exterior_units(pdf, page, vsel, sw["arcs"], eff)
                except Exception as e:  # noqa: BLE001
                    ext = None
                    trace.append(f"openings: exterior-wall test skipped ({type(e).__name__}: {str(e)[:60]})")
                if ext and len(ext[1]) > 0.75 * len(vsel):
                    # only when the units are MOSTLY interior (AFCEC A-2: 250 of 289): the outermost-ring depth is one
                    # number for the whole building, and where the dimension band is deeper on one side (EFA ground
                    # floor: north wall windows 2 m deeper than the south wall's) real windows would go too (EFA 18 of
                    # 32, 733733b3fa 29 of 50 'interior': left alone)
                    vsel = ext[0]
                    win_interior = len(ext[1])
                    trace.append(f"openings: {win_interior} of them lie > 1.2 m deeper than the building's outer wall "
                                 "ring (interior partitions drawn with parallel lines): not windows")
                    assumptions.append(f"windows are counted in the EXTERIOR walls: {win_interior} glazing-like units on "
                                       "interior walls (> 1.2 m deeper than the outer wall ring: thin double-line "
                                       f"partitions) were left out ({len(vsel) + win_interior} with them)")
                elif ext:
                    trace.append(f"openings: exterior-wall test: {len(ext[1])} of {len(vsel)} units lie deeper than the "
                                 "outer wall ring (not acted on: <= 75 %)")
        wing_ev = None
        if q.get("wing") and not rooms:
            # 'How many windows are in the north wing?' on a vector sheet: the same arm-of-the-footprint zone as doors
            if vsel is None:
                return self.abstain(f"windows in the {q['wing']} wing are read from the vector window units; this sheet "
                                    "has none readable", trace)
            got = self._wing_units(pdf, page, path, q["wing"], vsel, trace, assumptions)
            if isinstance(got, Answer):
                return got
            vsel, wing_ev = got
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
            if win_interior > len(vsel):
                # most 'units' were interior partitions: this sheet's window drawing is not well read (AFCEC A-2: the
                # kept units sit on some outer walls only)
                conf = min(conf, 0.35)
            if wing_ev is not None:
                conf = min(conf, 0.45)  # the wing's bounds come from the building's shape, not from a label
                missing = [*missing, "the wing's name on the plan itself (it was bounded by the building's shape)"]
            ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in u["box"]], bbox_space="pdf_pt", exact=True,
                           note=f"window {k}: {len(u['offs'])} glazing lines, {u['len_m']} m" + (f" ({u['lights']} lights split by a mullion)" if u.get('lights', 1) > 1 else ""))
                  for k, u in enumerate(sorted(vsel, key=lambda u: (u["box"][1], u["box"][0])), 1)] + room_ev
            if wing_ev is not None:
                ev.append(wing_ev)
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
                       note=f"window {k}: {len(u['offs'])} glazing lines, {u['len_m']} m" + (f" ({u['lights']} lights split by a mullion)" if u.get('lights', 1) > 1 else ""))
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
    def _via_trace(self, img: str, path: str, question: str, q: dict, items: list, trace: list,
                   force: bool = False) -> Answer | None:
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
        if (not roomish or not clean) and not force:
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
            # P1 00:44: a fresh scan's trace takes p50 18 s, p90 46 s, max 365 s (175 cached traces); bound the wait
            # like upload.py does (the trace finishes in a daemon thread and is cached for the next question)
            wait = float(os.environ.get("STOA_TRACE_WAIT", "90"))
            within = None
            if wait > 0:
                try:
                    from stoa.solvers.upload import _solve_within as within
                except Exception:  # noqa: BLE001
                    within = None
            if within is not None:
                got, _secs = within(lambda: V.traced_copy(img, dpi), wait)
                if got is None:
                    trace.append(f"openings: tracing the image takes longer than {wait:.0f} s: it finishes in the "
                                 "background (ask again in a minute)")
                    return None
                out = Path(got)
            else:
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

        if q.get("side") and os.environ.get("STOA_DR_IMG_SIDE", "1") != "0":
            # the picture readers below count every door symbol: 'How many exterior doors does the building have?' on
            # Stoa's AFCEC A-2 upload answered all 113 doors @0.45. Inside / outside is not told apart on a picture
            # (the vector reader's test is off too): let the other readers try
            return self.abstain(f"{q['side']} doors are not told apart from the others on a picture yet", [])
        if q.get("wing") and not q.get("each") and os.environ.get("STOA_DR_WING", "1") != "0":
            return self._wing_image(img, path, question, q)
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
            src2, k2 = _fullres_for_det(img, ppm)
            if k2 > 1.0:
                # Stoa's AFCEC A-2 web upload (6000 px copy, ~35 px/m): the copy's symbols are incomplete (177 boxes, 20
                # of 48 identical rooms disagree -> closet test untrusted); the 10200 px original at ~59 px/m: 213 -> 113
                z2 = min(1.0, 60.0 / (ppm * k2))
                r2 = _det_client(src2, z2, 0.4)
                if r2 is not None:
                    r2 = dict(r2, boxes=[[b[0] / k2, b[1] / k2, b[2] / k2, b[3] / k2, *b[4:]] for b in r2["boxes"]])
                    trace.append(f"openings: pass 2 reads the full-resolution original {Path(src2).name} (x{k2:.2f}, "
                                 f"~{ppm * k2 * z2:.0f} px/m): the shrunk copy draws door symbols too small")
            else:
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
        closet_note, rep_ok, rep_info = None, None, {}
        cmode = q.get("closets") if cls == "door" and not q["double"] else None
        if cmode and boxes and os.environ.get("STOA_DR_CLOSETS", "1") != "0":
            if self._clean(img):
                try:
                    import cv2

                    src, kf = full_res(img)
                    gray = cv2.imread(src, cv2.IMREAD_GRAYSCALE)
                    fl = closet_doors(gray, [[v * kf for v in b[:4]] for b in boxes], ppm * kf)
                    if cmode == "only":
                        # the closet test is trusted on a whole plan only where it is applied to the count: repeated
                        # rooms with their own closets (DfE PNG 'How many cupboard doors?': 34 flagged, Ziying 13)
                        chk: dict = {}
                        _, _, kept = self._closets_whole(img, path, question, boxes, ppm, [], info=chk)
                        if not chk.get("gated"):
                            return self.abstain("closet doors are told apart on a whole plan only when it has repeated "
                                                "rooms with their own closets (a dormitory, a hotel floor)", trace)
                        kept_ids = {id(b) for b in kept}
                        odd_ids = {id(boxes[j]) for j in chk.get("odd") or []}
                        fl = [id(b) not in kept_ids and id(b) not in odd_ids for b in boxes]
                    return self._wing_closets(img, path, " on the plan", cmode, [b for b, c in zip(boxes, fl) if not c],
                                              [b for b, c in zip(boxes, fl) if c], 1.0, trace, conf=0.45)
                except Exception as e:  # noqa: BLE001
                    trace.append(f"openings: closet test skipped ({type(e).__name__}: {str(e)[:60]})")
            if cmode == "only":
                return self.abstain("closet doors could not be told apart on this picture", trace)
        if (cls == "door" and not q["double"] and boxes and self._clean(img) and cmode != "with"
                and os.environ.get("STOA_DR_CLOSETS", "1") != "0" and os.environ.get("STOA_DR_CLOSETS_ALL", "1") != "0"):
            # Ziying's convention (afcec-04, dfe-07: 'a door counts only if a person can walk through it') on the whole
            # plan too: closet / wardrobe doors out, then the repetition check over the floor's room modules
            all_boxes = boxes
            closet_note, rep_ok, boxes = self._closets_whole(img, path, question, boxes, ppm, trace, info=rep_info)
        if q.get("each") and cls == "door":
            return self._each_room(img, path, question, q, all_boxes if rep_info else [], rep_info, trace)
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
        if closet_note:
            assumptions[:0] = closet_note
            if rep_ok == "uncertain":
                conf = min(conf, 0.35)
            elif rep_ok:
                conf = max(conf, 0.6)
        if (whole_img and cls == "door" and not q["double"] and not self._clean(img)
                and os.environ.get("STOA_DR_SCAN_BOTH", "1") != "0"):
            # F7 23:35 (X1's scanned DfE L00: 105 when the detector was busy -> traced swings, 58 when it was free; the
            # PDF 115): on a scan BOTH readings miss faint symbols, so read both and keep the higher. X1 doors_scan3:
            # detector v5 6 / 10, traced 3 / 10, the higher 7 / 10 (sc-053 traced 9 = gold, detector 8)
            tt: list = []
            ta = self._via_trace(img, path, question, q, items, tt, force=True)
            if ta is not None and ta.value is not None:
                tv = int(ta.value)
                if tv > len(boxes):
                    ta.trace = [*trace, *tt[-3:], f"openings: traced swings {tv} > detector {len(boxes)}: kept the higher"]
                    ta.assumptions = [f"two readings of this scan: {tv} door swings traced from the picture, "
                                      f"{len(boxes)} door symbols found by the symbol detector; both miss faint "
                                      "symbols on a scan, so the higher one is kept", *(ta.assumptions or [])]
                    ta.confidence = round(max(ta.confidence or 0.0, 0.35), 2)
                    return ta
                trace.append(f"openings: traced swings on the picture: {tv} ({'agrees' if tv == len(boxes) else 'fewer'})")
                assumptions.insert(0, f"two readings of this scan: {len(boxes)} door symbols found by the symbol "
                                      f"detector, {tv} door swings traced from the picture"
                                      + (" (they agree)" if tv == len(boxes) else "; the higher one is kept"))
                if tv == len(boxes):
                    conf = max(conf, 0.55)
        return Answer(value=len(boxes), unit="count", confidence=conf, evidence=ev, trace=trace,
                      assumptions=assumptions, missing=[f"a {cls} schedule or a printed scale to confirm the count"])

    @staticmethod
    def _closets_vector(pdf, page, doors_in, mat, scale_n, question, trace):
        """Whole-plan door count on a vector sheet of REPEATED rooms (same gate as _closets_whole): closet / wardrobe
        doors out, read on a ~50 px/m render of the doors' extent. -> (kept doors_in, assumption lines) or None."""
        import numpy as np
        import pymupdf

        try:
            m = 1.0 / (PT_M * scale_n)  # pt per metre
            boxes = []
            for d, _ in doors_in:
                pts = [_to_display(mat, (d["bbox"][i], d["bbox"][j])) for i, j in ((0, 1), (2, 3), (0, 3), (2, 1))]
                boxes.append([min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                              max(p[1] for p in pts)])
            x0, y0 = min(b[0] for b in boxes) - 3 * m, min(b[1] for b in boxes) - 3 * m
            x1, y1 = max(b[2] for b in boxes) + 3 * m, max(b[3] for b in boxes) + 3 * m
            ppm = 50.0
            zm = ppm / m  # px per pt
            if max(x1 - x0, y1 - y0) * zm > 12000:
                zm = 12000 / max(x1 - x0, y1 - y0)
                ppm = zm * m
            if ppm < 30:
                return None
            with pymupdf.open(pdf) as doc:
                pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(zm, zm), clip=pymupdf.Rect(x0, y0, x1, y1),
                                           colorspace=pymupdf.csGRAY, alpha=False)
            g = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
            bx = [[(b[0] - x0) * zm, (b[1] - y0) * zm, (b[2] - x0) * zm, (b[3] - y0) * zm] for b in boxes]
            fl = closet_doors(g, bx, ppm)
            fl, rep, fix = settle_modules(g, bx, fl, ppm)
        except Exception as e:  # noqa: BLE001
            trace.append(f"openings: closet test skipped ({type(e).__name__}: {str(e)[:60]})")
            return None
        n_cl = sum(fl) - len(fix["odd"])
        if not rep or rep["alike"] < 8 or n_cl < 0.5 * rep["n"] or len(rep["dev"]) > 0.25 * rep["n"]:
            trace.append(f"openings: no closet check here (no repeated rooms with their own closets: "
                         f"{rep['n'] if rep else 0} modules, {n_cl} closet-like doors)")
            return None
        kept = [x for x, c in zip(doors_in, fl) if not c]
        printed = printed_room_count(pdf, page, None, question)
        line, _ok = repetition_line(rep, len(kept), printed, None)
        trace.append(f"openings: {n_cl} of {len(doors_in)} doors open onto a closet: not counted; " + line)
        return kept, [line, *fix_notes(fix, rep["n"]), f"closet / wardrobe / cupboard doors are not counted (a door counts if a person can walk "
                            f"through it): {n_cl} doors open onto a small empty enclosed space and were left out "
                            f"({len(doors_in)} with them)"]

    @staticmethod
    def _closets_whole(img: str, path: str, question: str, boxes: list, ppm: float, trace: list,
                       info: dict | None = None) -> tuple:
        """Whole-plan door count on a clean picture: closet / wardrobe doors out (closet_doors on the full-res image)
        and the repetition check over the floor's room modules (odd symbols out). -> (assumption lines | None,
        repetition agrees | None, kept boxes)."""
        import cv2

        try:
            # V1 00:44 (Stoa's AFCEC A-2 upload, 6000 px copy at ~35 px/m): the spaces behind the doors merge on the
            # shrunk copy -> 116 of 177 'closet doors', 58 @0.6 (full res: 113). The test runs on the original.
            src, kf = full_res(img)
            gray = cv2.imread(src, cv2.IMREAD_GRAYSCALE)
            if gray is None:
                return None, None, boxes
            bx = [[b[0] * kf, b[1] * kf, b[2] * kf, b[3] * kf, *b[4:]] for b in boxes] if kf > 1.0 else boxes
            if kf > 1.0:
                trace.append(f"openings: closet test on the full-resolution original {Path(src).name} (x{kf:.2f})")
            fl = closet_doors(gray, bx, ppm * kf)
            fl, rep, fix = settle_modules(gray, bx, fl, ppm * kf)
            odd = [j for j, _ in fix["odd"]]
            if info is not None:
                info.update(rep=rep, fl=fl, odd=odd)
        except Exception as e:  # noqa: BLE001
            trace.append(f"openings: closet test skipped ({type(e).__name__}: {str(e)[:60]})")
            return None, None, boxes
        n_cl = sum(fl) - len(odd)
        if not rep or rep["alike"] < 8 or n_cl < 0.5 * rep["n"] or len(rep["dev"]) > 0.25 * rep["n"]:
            # only on plans of REPEATED rooms whose closets belong to each room (dorms, hotels: AFCEC 48 rooms, 100
            # closet doors; >= 75 % of the rooms show the same doors). Elsewhere it cost golds (controls: MFC module 4 -> 3;
            # DfE PNG 87 -> 81 of 115: 11 loose "modules", 6 of them off)
            trace.append(f"openings: no closet check here (no repeated rooms with their own closets: "
                         f"{rep['n'] if rep else 0} modules, {n_cl} closet-like doors)")
            if (rep and rep["n"] >= 16 and rep["alike"] >= 12 and n_cl >= 0.5 * rep["n"]
                    and len(rep["dev"]) > 0.25 * rep["n"]
                    and os.environ.get("STOA_DR_CLOSET_SAY", "1") != "0"):
                # V1 00:44 (AFCEC A-2 upload, 6000 px copy): a plan of repeated rooms with closets, but the rooms do
                # not agree (the copy's symbols are incomplete) -> all symbols are counted, and the answer says so
                return ([f"{n_cl} of the {len(boxes)} door symbols open onto a small empty enclosed space (closet / "
                         f"wardrobe doors, not walk-through doors): they are INCLUDED here because the room-by-room "
                         f"check disagrees ({len(rep['dev'])} of {rep['n']} identical rooms show a different door "
                         f"count), so the closet test was not trusted; without them: {len(boxes) - n_cl}"],
                        "uncertain", boxes)
            return None, None, boxes
        kept = [b for j, (b, c) in enumerate(zip(boxes, fl)) if not c and j not in odd]
        if info is not None:
            info["gated"] = True
        notes, ok = [], None
        if n_cl:
            notes.append(f"closet / wardrobe / cupboard doors are not counted (a door counts if a person can walk "
                         f"through it): {n_cl} door symbols open onto a small empty enclosed space and were left out "
                         f"({len(kept) + n_cl + len(odd)} with them)")
            trace.append(f"openings: {n_cl} of {len(boxes)} door boxes open onto a closet: not counted")
        if rep:
            printed = printed_room_count(img, 0, None, question) or (
                printed_room_count(path, 0, None, question) if path != img else None)
            line, ok = repetition_line(rep, len(kept), printed, None)
            notes.insert(0, line)
            trace.append("openings: " + line)
            notes[1:1] = fix_notes(fix, rep["n"])
        return (notes or None), ok, kept

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

    @staticmethod
    def _wing_side(path: str, wing: str, page: int = 0) -> tuple[str, str]:
        """Display side of the compass wing: VP's north arrow when it reads one, else top = north (said)."""
        try:
            from stoa.sheet import north_arrow

            na = north_arrow(path, page)
            disp = (na.get("sides") or {}).get(wing)
            if na.get("found") and disp in ("top", "bottom", "left", "right"):
                return disp, f"north arrow on the sheet ({na.get('angle_deg', 0):.0f}°): the {wing} wing is at the " \
                             f"{disp} of the sheet"
        except Exception:  # noqa: BLE001
            pass
        disp = {"north": "top", "south": "bottom", "west": "left", "east": "right"}[wing]
        return disp, f"no north arrow read: the top of the sheet is taken as north, so the {wing} wing is at the {disp}"

    @staticmethod
    def _wing_zone(gray, disp: str, wing: str) -> tuple:
        """-> (zone dict | None, reason). zone: arm mask on the WING_LONG copy (f = copy / original), the arm's bbox in
        original px, and the search zone (arm + a band outside the building, not the rest of the building)."""
        import cv2
        import numpy as np

        f = WING_LONG / max(gray.shape)
        g = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else gray
        f = min(f, 1.0)
        F = footprint_mask(g)
        if F is None:
            return None, "no building outline found on the sheet"
        A, info = wing_arm(F, disp)
        if A is None:
            return None, info["why"]
        if info["frac"] > 0.7:
            return None, (f"the building has no distinct {wing} wing: walking in from the {disp} of the sheet, the "
                          f"footprint never narrows into an arm (it is {info['frac']:.0%} of the building)")
        if info["frac"] < 0.04:
            return None, f"the part at the {disp} of the building is too small to be a wing ({info['frac']:.0%})"
        k = max(3, int(0.12 * info["width"]))  # doors on the arm's outer walls / at its end swing outwards
        near = cv2.dilate(A, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * k + 1, 2 * k + 1)))
        rest = ((F > 0) & (A == 0)).astype(np.uint8)
        zone = ((near > 0) & (rest == 0)).astype(np.uint8)
        # the line where the wing meets the rest of the building: a door there is the wing's boundary door
        ke = max(2, int(0.04 * info["width"]))  # ~0.6 m on a 16 m wide wing: the cut line itself, not the last module
        edge = cv2.dilate(rest, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * ke + 1, 2 * ke + 1))) & zone
        ys, xs = np.where(zone > 0)
        bbox = (xs.min() / f, ys.min() / f, (xs.max() + 1) / f, (ys.max() + 1) / f)
        return {"arm": A, "zone": zone, "edge": edge, "f": f, "bbox": bbox, "info": info}, ""

    def _wing_pdf(self, pdf, page, path, q, doors_in, mat, trace, assumptions):
        """Vector sheets: the wing's zone from a render of the page (same footprint / arm rule as on pictures), then
        the doors read from the linework whose centre is in it. -> (doors_in, searched-area Evidence) or an Answer
        (abstain)."""
        import numpy as np
        import pymupdf

        wing = q["wing"]
        if re.search(r"\.vec\d+\.pdf$", str(pdf)):
            # a TRACED copy of a picture (X1 sc-314 scan: 280 traced 'arcs' -> 46 vs 34): the wing is counted on the
            # picture itself (detector + closet test), not on traced linework
            return self.abstain("a traced copy of a picture: the wing's doors are counted on the picture itself", trace)
        disp, side_note = self._wing_side(pdf, wing, page)
        with pymupdf.open(pdf) as doc:
            pg = doc[page]
            r = pg.rect  # displayed (rotated) page
            sc = WING_LONG / max(r.width, r.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(sc, sc), colorspace=pymupdf.csGRAY, alpha=False)
            gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
            inv = ~pg.rotation_matrix  # display -> unrotated page space (evidence space)
        z, why = self._wing_zone(gray, disp, wing)
        if z is None:
            return self.abstain(why, trace)
        zone, edge, info = z["zone"], z["edge"], z["info"]

        def px(d):
            x, y = _to_display(mat, d["center"])
            return int(x * sc), int(y * sc)

        def hit(m, d) -> bool:
            x, y = px(d)
            return 0 <= y < m.shape[0] and 0 <= x < m.shape[1] and bool(m[y, x])

        import cv2

        n0 = len(doors_in)
        near_cut = cv2.dilate(edge, np.ones((11, 11), np.uint8))  # +- ~0.5 m at a 2000-px sheet render

        def on_cut(d) -> bool:  # the door that closes the wing off: its doorway lies on the cut line (AFCEC link/core)
            for u, v in doorways(d):
                x, y = _to_display(mat, ((u[0] + v[0]) / 2, (u[1] + v[1]) / 2))
                x, y = int(x * sc), int(y * sc)
                if 0 <= y < near_cut.shape[0] and 0 <= x < near_cut.shape[1] and near_cut[y, x]:
                    return True
            return False

        kept = [(d, c) for d, c in doors_in if hit(zone, d) or on_cut(d)]
        n_closet, rep, fix_lines, n_odd = 0, None, [], 0
        if kept and q.get("scale_n_eff") and os.environ.get("STOA_DR_CLOSETS", "1") != "0" and q.get("closets") != "with":
            # Ziying's convention (captain 23:04): closet / wardrobe doors are not doors of the wing -> the same
            # 'small empty enclosed space behind the door' test as on pictures, on a ~50 px/m render of the wing
            try:
                ys_, xs_ = (z["arm"] > 0).nonzero()
                pad = 2.0 / (PT_M * q["scale_n_eff"])  # 2 m
                clip = pymupdf.Rect(xs_.min() / sc - pad, ys_.min() / sc - pad, (xs_.max() + 1) / sc + pad,
                                    (ys_.max() + 1) / sc + pad)
                zm = 50.0 * PT_M * q["scale_n_eff"]  # px per pt for 50 px per metre
                with pymupdf.open(pdf) as doc:
                    pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(zm, zm), clip=clip, colorspace=pymupdf.csGRAY,
                                               alpha=False)
                cg = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
                bx = []
                for d, _ in kept:
                    pts = [_to_display(mat, (d["bbox"][i], d["bbox"][j])) for i, j in ((0, 1), (2, 3), (0, 3), (2, 1))]
                    bx.append([(min(p[0] for p in pts) - clip.x0) * zm, (min(p[1] for p in pts) - clip.y0) * zm,
                               (max(p[0] for p in pts) - clip.x0) * zm, (max(p[1] for p in pts) - clip.y0) * zm])
                fl = closet_doors(cg, bx, 50.0)
                if os.environ.get("STOA_DR_REPEAT", "1") != "0" and not q.get("double"):
                    try:  # captain 23:04 tip 3: identical room modules -> 'N rooms x k doors + other = total'
                        fl, rep, fix = settle_modules(cg, bx, fl, 50.0)
                        fix_lines = fix_notes(fix, rep["n"]) if rep else []
                        n_odd = len(fix["odd"])
                    except Exception as e:  # noqa: BLE001
                        trace.append(f"openings: repetition check skipped ({type(e).__name__}: {str(e)[:60]})")
                n_closet = sum(fl) - n_odd
                kept = [k for k, c in zip(kept, fl) if not c]
            except Exception as e:  # noqa: BLE001
                trace.append(f"openings: closet test skipped ({type(e).__name__}: {str(e)[:60]})")
        nb = sum(1 for d, _ in kept if hit(edge, d) or on_cut(d))
        trace.append(f"openings: {wing} wing = the building's arm at the {disp} of the sheet ({info['frac']:.0%} of the "
                     f"footprint" + (", incl. a narrow link" if info["neck"] else "") + f"): {len(kept)} of {n0} doors"
                     + (f" ({n_closet} more open onto a closet: not counted)" if n_closet else ""))
        if n_closet:
            assumptions.append(f"closet / wardrobe / cupboard doors are not counted: {n_closet} doors open onto a small "
                               f"empty enclosed space and were left out ({len(kept) + n_closet} with them)")
        assumptions += [side_note,
                        f"the {wing} wing = the arm of the building that reaches the {disp} of the sheet, from its end "
                        "to where it meets the wider part of the building" + (" (incl. the narrow link corridor to "
                        "the core)" if info["neck"] else "") + "; doors on its outer walls and at its end count"]
        if nb:
            assumptions.append(f"{nb} of the {len(kept)} doors {'sits' if nb == 1 else 'sit'} on the line where the "
                               f"wing meets the rest of the building (counted; {len(kept) - nb} without "
                               f"{'it' if nb == 1 else 'them'})")
        ys, xs = (z["arm"] > 0).nonzero()
        a = pymupdf.Point(xs.min() / sc, ys.min() / sc) * inv
        b = pymupdf.Point((xs.max() + 1) / sc, (ys.max() + 1) / sc) * inv
        box = [round(min(a.x, b.x), 1), round(min(a.y, b.y), 1), round(max(a.x, b.x), 1), round(max(a.y, b.y), 1)]
        ev = Evidence(file=path, page=page, bbox=box, bbox_space="pdf_pt", exact=False,
                      note=f"searched area: the {wing} wing as bounded (not a door)")
        if not kept:
            return self.abstain(f"no door read in the {wing} wing (a wing without any door is unlikely)", trace)
        rep_ok = None
        kp_line, kp_ok = keyplan_check(pdf, f"How many doors are in the {wing} wing?", page, box, wing)
        if kp_line:
            trace.append("openings: " + kp_line)
            assumptions.insert(0, kp_line)
        if rep:
            printed = printed_room_count(pdf, page, wing, q.get("question") or "")
            line, rep_ok = repetition_line(rep, len(kept), printed, wing)
            rep_ok = (rep_ok and kp_ok is not False, bool(printed), rep_scattered(rep))
            trace.append("openings: " + line)
            assumptions[0:0] = [line, *fix_lines]
        return kept, ev, rep_ok

    def _wing_units(self, pdf, page, path, wing, units, trace, assumptions):
        """Vector WINDOW units of one wing (doors: _wing_pdf): the wing's zone from a render of the page, units whose
        centre is in it. -> (units, searched-area Evidence) or an Answer (abstain)."""
        import numpy as np
        import pymupdf

        disp, side_note = self._wing_side(pdf, wing, page)
        with pymupdf.open(pdf) as doc:
            pg = doc[page]
            r = pg.rect
            rm = pg.rotation_matrix
            sc = WING_LONG / max(r.width, r.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(sc, sc), colorspace=pymupdf.csGRAY, alpha=False)
            gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
            inv = ~rm
        z, why = self._wing_zone(gray, disp, wing)
        if z is None:
            return self.abstain(why, trace)
        zone, info = z["zone"], z["info"]

        def inside(u) -> bool:
            b = u["box"]
            q = pymupdf.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) * rm
            x, y = int(q.x * sc), int(q.y * sc)
            return 0 <= y < zone.shape[0] and 0 <= x < zone.shape[1] and bool(zone[y, x])

        kept = [u for u in units if inside(u)]
        trace.append(f"openings: {wing} wing = the building's arm at the {disp} of the sheet ({info['frac']:.0%} of the "
                     f"footprint" + (", incl. a narrow link" if info["neck"] else "") + f"): {len(kept)} of {len(units)} "
                     "window units")
        assumptions += [side_note,
                        f"the {wing} wing = the arm of the building that reaches the {disp} of the sheet, from its end "
                        "to where it meets the wider part of the building" + (" (incl. the narrow link corridor to "
                        "the core)" if info["neck"] else "") + "; windows in its outer walls and at its end count"]
        ys, xs = (z["arm"] > 0).nonzero()
        a = pymupdf.Point(xs.min() / sc, ys.min() / sc) * inv
        b = pymupdf.Point((xs.max() + 1) / sc, (ys.max() + 1) / sc) * inv
        box = [round(min(a.x, b.x), 1), round(min(a.y, b.y), 1), round(max(a.x, b.x), 1), round(max(a.y, b.y), 1)]
        ev = Evidence(file=path, page=page, bbox=box, bbox_space="pdf_pt", exact=False,
                      note=f"searched area: the {wing} wing as bounded (not a window)")
        return kept, ev

    def _each_room(self, img, path, question, q, boxes, info, trace) -> Answer:
        """'How many doors does each dorm room have?' = the repetition check's usual doors per identical room (closet /
        wardrobe doors out), boxed on one typical room; the printed ROOM COUNT confirms the rooms are the ones asked."""
        rep = (info or {}).get("rep")
        room = q["each"]
        if not rep or rep["n"] < 4 or rep["alike"] < 8 or rep_scattered(rep):
            return self.abstain(f"no repeated identical rooms found on this drawing to read 'each {room}' from (the "
                                "door count per room is read from rooms drawn alike)", trace)
        fl, own, near, k = info["fl"], rep["own"], rep["near"], rep["k"]
        devs = {i for i, _ in rep["dev"]}
        usual = [i for i in range(1, rep["n"] + 1) if i not in devs]
        m0 = next((i for i in usual if sum(1 for o in own if o == i) == k), None)
        if m0 is None:
            return self.abstain(f"the identical rooms do not agree on a door count ('each {room}' not read)", trace)
        doors = [b for b, o in zip(boxes, own) if o == m0]
        cl = [b for b, c, n_ in zip(boxes, fl, near) if c and n_ == m0]
        ncl = sorted(sum(1 for c, n_ in zip(fl, near) if c and n_ == i) for i in usual)
        ncl_med = ncl[len(ncl) // 2] if ncl else 0
        printed = printed_room_count(img, 0, None, question) or (
            printed_room_count(path, 0, None, question) if path != img else None)
        agree = bool(printed and abs(printed["expected"] - rep["n"]) <= 0.5)
        ev = [Evidence(file=path, page=0, bbox=[round(v, 1) for v in b[:4]], bbox_space="image_px", exact=False,
                       note=f"door {j} of room module {m0} (a typical one of the {rep['n']} identical rooms)")
              for j, b in enumerate(doors, 1)]
        ev += [Evidence(file=path, page=0, bbox=[round(v, 1) for v in b[:4]], bbox_space="image_px", exact=False,
                        note=f"closet / wardrobe door of room module {m0} (not counted)") for b in cl]
        line = (f"{rep['n']} identical room modules on the plan ({rep['alike']} alike, ~{rep['m2']:.0f} m² of free "
                f"floor each): {len(usual)} of them show {k} door{'s' if k != 1 else ''}"
                + (f"; {'; '.join(f'module {i} shows {d}' for i, d in rep['dev'][:6])}" if rep["dev"] else "")
                + (f"; plus {ncl_med} closet / wardrobe door{'s' if ncl_med != 1 else ''} per room (not counted: a "
                   "door counts if a person can walk through it)" if ncl_med else ""))
        trace.append("openings: each room: " + line)
        assumptions = [line,
                       (f"{printed['how']} -> agrees with the {rep['n']} identical rooms, so they are taken as the "
                        f"{room}s asked about" if agree else
                        f"the {rep['n']} identical rooms are taken as the {room}s asked about (their labels were not "
                        "matched)"),
                       "Convention: a double door counts as ONE door (one detector box per doorset)"]
        conf = 0.65 if agree else 0.5
        return Answer(value=k, unit="count", confidence=conf, evidence=ev, trace=trace, assumptions=assumptions,
                      missing=[f"the {room} label on each repeated room", "a door schedule to confirm the count"])

    def _wing_closets(self, img, path, where, cmode, walk, closets, back, trace, conf) -> Answer:
        """'How many closet doors are in the north wing?' (cmode 'only') / '... doors including the closets?' ('with'):
        the closet test's own split of the door symbols (walk-through doors vs doors onto a small empty enclosed
        space). A scan whose rooms do not close finds no closet: not told apart -> abstain for 'only'."""
        where_s = f" in the {where} wing" if where in ("north", "south", "east", "west") else (where or "")
        if cmode == "only" and not closets and conf < 0.45:
            return self.abstain("closet doors could not be told apart on this scan (walls too broken to close the "
                                "spaces behind the doors)", trace)
        boxes = closets if cmode == "only" else walk + closets
        ev = [Evidence(file=path, page=0, bbox=[round(v * back, 1) for v in b[:4]], bbox_space="image_px", exact=False,
                       note=("closet door" if any(b is c for c in closets) else "door") + f" {k}{where_s}"
                       + f" ({b[4]:.2f})") for k, b in enumerate(boxes, 1)]
        what = "closet / wardrobe / cupboard doors" if cmode == "only" else "doors including closet / wardrobe doors"
        trace.append(f"openings: {len(boxes)} {what}{where_s} ({len(walk)} walk-through + {len(closets)} onto closets)")
        assumptions = [
            f"a closet door = a door symbol with a small empty enclosed space behind it (0.4-2.6 m², no door of its "
            f"own): {len(closets)} of the {len(walk) + len(closets)} door symbols{where_s}; the other {len(walk)} are "
            "walk-through doors" + (" (counted too, as asked)" if cmode == "with" else "") + "; closets, wardrobes and "
            "cupboards are not told apart (each is a small empty space behind a door)",
            "Convention: a double door counts as ONE door (one detector box per doorset)"]
        if cmode == "only" and conf >= 0.45:
            assumptions.append("a closet behind a door that the detector missed, or a small store taken for a closet, "
                               "changes this count: check the marked doors")
        return Answer(value=len(boxes), unit="count", confidence=min(conf, 0.45 if cmode == "with" else 0.4),
                      evidence=ev, trace=trace, assumptions=assumptions,
                      missing=["a door schedule (door types) to confirm which doors are closet doors"])

    def _wing_image(self, img: str, path: str, question: str, q: dict) -> Answer:
        """'How many doors are in the north wing?' on a sheet picture (H 22:24, AFCEC dorm A-2): the wing = the arm of
        the building's footprint at that side (wing_arm), V1's detector on a crop of it (px per metre calibrated from
        its own door boxes, as in _image), one box per door whose centre is in the wing or just outside its outer
        walls. The answer says how the wing was bounded and shows the searched area as the last box."""
        import hashlib
        import statistics
        import tempfile

        import cv2

        trace: list[str] = []
        wing, cls = q["wing"], q["target"]
        if q.get("leaves") or q.get("dtype"):
            return self.abstain(f"{'leaves' if q.get('leaves') else 'door types'} in a wing are not read on images",
                                trace)
        disp, side_note = self._wing_side(img, wing)  # its own cached text layer; never a cold OCR (VP)
        img_in = img
        # captain 23:04 (Ziying): never count small symbols on the shrunk copy: the upload's '<stem>.s6000.png' comes
        # from '<stem>.png' next to it -> read the wing there and give the boxes back in the copy's pixels
        back = 1.0
        orig, kf = full_res(img)
        if kf > 1.0:
            back = 1.0 / kf
            trace.append(f"openings: read at full resolution from {Path(orig).name} (x{kf:.2f})")
            img = orig
        gray = cv2.imread(img, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            return self.abstain("could not read the image", trace)
        H, W = gray.shape
        z, why = self._wing_zone(gray, disp, wing)
        if z is None:
            return self.abstain(why, trace)
        info, f = z["info"], z["f"]
        x0, y0, x1, y1 = z["bbox"]
        trace.append(f"openings: {wing} wing = the building's arm at the {disp} of the sheet: {info['length']} of "
                     f"{info['extent']} px of the footprint along that axis ({info['frac']:.0%} of its area)"
                     + (", incl. a narrow link" if info["neck"] else "") + f"; crop {x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}")
        st = os.stat(img)
        key = hashlib.sha1(f"{os.path.abspath(img)}|{st.st_size}|{st.st_mtime_ns}|{disp}|v1".encode()).hexdigest()[:16]
        cdir = Path(tempfile.gettempdir()) / "stoa-dr-wing"
        cdir.mkdir(parents=True, exist_ok=True)
        crop = cdir / f"{key}.png"
        X0, Y0 = int(x0), int(y0)
        if not crop.exists():
            col = cv2.imread(img, cv2.IMREAD_COLOR)
            tmp = cdir / f"{key}.{os.getpid()}.tmp.png"
            cv2.imwrite(str(tmp), col[Y0:int(y1) + 1, X0:int(x1) + 1])
            os.replace(tmp, crop)
        cw, ch = int(x1) + 1 - X0, int(y1) + 1 - Y0

        def doors_of(r):
            return [b for b in r["boxes"] if b[5] == "door" and b[4] >= 0.5]

        z1 = min(1.0, 4000.0 / max(cw, ch))
        r1 = _det_client(str(crop), z1, 0.4)
        if r1 is not None and len(doors_of(r1)) < 2 and z1 == 1.0:
            # a small-scale sheet (AFCEC 1" = 40': ~25 px/m at 300 dpi) shows the detector symbols too small: zoom in
            r1b = _det_client(str(crop), 2.0, 0.4)
            if r1b is not None and len(doors_of(r1b)) >= 2:
                r1, z1 = r1b, 2.0
        if r1 is None:
            return self.abstain(_det_why(), trace)
        d1 = doors_of(r1)
        if len(d1) < 2:
            return self.abstain(f"detector found {len(d1)} door boxes in the {wing} wing: too few to calibrate the "
                                "scale", trace)
        leaf_px = statistics.median(min(b[2] - b[0], b[3] - b[1]) for b in d1)
        ppm = leaf_px / 0.85
        r = r1
        trace.append(f"openings: detector pass 1 at zoom {z1:.2f}: {len(d1)} door boxes, ~{ppm:.0f} px/m")
        if not 45 <= ppm * z1 <= 85:  # a wing crop is small: read it at ~60 px/m (v2 at 35 px/m: 51 of 66 doors)
            z2 = round(min(3.0, 60.0 / ppm), 2)
            r2 = _det_client(str(crop), z2, 0.4)
            if r2 is not None and len(doors_of(r2)) >= len(d1):
                r = r2
                trace.append(f"openings: detector pass 2 at zoom {z2:.2f} (~60 px/m): {len(doors_of(r2))} door boxes")
            elif r2 is not None:
                trace.append(f"openings: pass 2 at zoom {z2:.2f} found {len(doors_of(r2))} (< {len(d1)}): kept pass 1")
        boxes = [[b[0] + X0, b[1] + Y0, b[2] + X0, b[3] + Y0, b[4], b[5]] for b in r["boxes"] if b[5] == cls]
        if cls == "door":
            boxes = [b for b in boxes if b[4] >= 0.4 and max(b[2] - b[0], b[3] - b[1]) >= 0.6 * ppm]
        else:
            boxes = [b for b in boxes if b[4] >= WIN_SCORE and min(b[2] - b[0], b[3] - b[1]) >= 0.12 * ppm]
        zone = z["zone"]

        def in_zone(b) -> bool:
            cx, cy = int((b[0] + b[2]) / 2 * f), int((b[1] + b[3]) / 2 * f)
            return 0 <= cy < zone.shape[0] and 0 <= cx < zone.shape[1] and bool(zone[cy, cx])

        n0 = len(boxes)
        outs = [b for b in boxes if not in_zone(b)]
        boxes = [b for b in boxes if in_zone(b)]
        inside = list(boxes)
        n_sym, closets = len(boxes), []
        rep, mod_of, odd_boxes, fix = None, {}, [], {"odd": [], "restored": []}
        if cls == "door" and os.environ.get("STOA_DR_CLOSETS", "1") != "0":
            # Ziying's convention (captain 23:04; DfE 115 = 128 - 13 cupboards): closet / wardrobe / cupboard doors are
            # not doors of the wing. A door whose one side is a small empty enclosed space = a closet door.
            cg = cv2.imread(str(crop), cv2.IMREAD_GRAYSCALE)
            if cg is not None:
                cb = [[b[0] - X0, b[1] - Y0, b[2] - X0, b[3] - Y0] for b in boxes]
                fl = closet_doors(cg, cb, ppm)
                if not q["double"] and os.environ.get("STOA_DR_REPEAT", "1") != "0":
                    # captain 23:04 tip 3: identical room modules -> 'N rooms x k doors + other = total'
                    try:
                        fl, rep, fix = settle_modules(cg, cb, fl, ppm)
                    except Exception as e:  # noqa: BLE001
                        trace.append(f"openings: repetition check skipped ({type(e).__name__}: {str(e)[:60]})")
                    odd_boxes = [boxes[j] for j, _ in fix["odd"]]
                    if rep:
                        mod_of = {id(b): m for b, m in zip(boxes, rep["own"]) if m}
                closets = [b for b, c in zip(boxes, fl) if c and not any(b is o for o in odd_boxes)]
                boxes = [b for b, c in zip(boxes, fl) if not c]
        cmode = q.get("closets") if cls == "door" else None
        if cmode == "only" and (not rep or rep["alike"] < 8 or rep_scattered(rep)):
            # as on the whole plan: closet flags are trusted where identical rooms with their own closets agree
            return self.abstain(f"closet doors are told apart only where the {wing} wing has repeated rooms with their "
                                "own closets (a dormitory, a hotel floor)", trace)
        if cmode:
            return self._wing_closets(img, path, wing, cmode, boxes, closets, back, trace, conf=0.45 if self._clean(
                img) else 0.3)
        if cls == "door" and q["double"]:
            boxes = [b for b in boxes if max(b[2] - b[0], b[3] - b[1]) >= 1.6 * leaf_px
                     and max(b[2] - b[0], b[3] - b[1]) >= 1.25 * min(b[2] - b[0], b[3] - b[1])]
        trace.append(f"openings: {n_sym} of {n0} {cls} boxes of the crop lie in the {wing} wing"
                     + (f"; {len(closets)} of them open onto a closet (a small empty enclosed space): not counted"
                        if closets else ""))
        if not boxes and not q["double"]:
            return self.abstain(f"the detector found no {cls} in the {wing} wing (not answered: a wing without any is "
                                "unlikely)", trace)
        edge = z["edge"]

        def on_edge(b) -> bool:
            cx, cy = int((b[0] + b[2]) / 2 * f), int((b[1] + b[3]) / 2 * f)
            return 0 <= cy < edge.shape[0] and 0 <= cx < edge.shape[1] and bool(edge[cy, cx])

        cut_leaves = cut_line_leaves(outs, inside, boxes, on_edge, ppm) if cls == "door" and not q["double"] else []
        if cut_leaves:
            boxes = boxes + cut_leaves
            if rep:
                rep = dict(rep, other=rep["other"] + len(cut_leaves))
            trace.append(f"openings: {len(cut_leaves)} double door{'s' if len(cut_leaves) > 1 else ''} on the line where "
                         "the wing meets the rest of the building counted by the leaf that swings out of the wing: "
                         + "; ".join(",".join(f"{v * back:.0f}" for v in b[:4]) for b in cut_leaves))
        cut_ids = {id(b) for b in cut_leaves}
        nb = sum(1 for b in boxes if on_edge(b) or id(b) in cut_ids)
        ev = [Evidence(file=path, page=0, bbox=[round(v * back, 1) for v in b[:4]], bbox_space="image_px", exact=False,
                       note=f"{cls} {k} in the {wing} wing" + (" (on the line where the wing meets the rest of the "
                       "building)" if on_edge(b) else "") + (" (a double door on the line where the wing meets the rest "
                       "of the building: this leaf swings out of the wing, its other leaf opens into the wing)"
                       if id(b) in cut_ids else "") + (f", room module {mod_of[id(b)]} of {rep['n']}"
                       if id(b) in mod_of else "") + f" ({b[4]:.2f})") for k, b in enumerate(boxes, 1)]
        if nb:
            trace.append(f"openings: {nb} of them on the line where the wing meets the rest of the building")
        ys, xs = (z["arm"] > 0).nonzero()
        ev.append(Evidence(file=path, page=0, bbox=[round(xs.min() / f * back, 1), round(ys.min() / f * back, 1),
                                                     round((xs.max() + 1) / f * back, 1),
                                                     round((ys.max() + 1) / f * back, 1)],
                           bbox_space="image_px", exact=False,
                           note=f"searched area: the {wing} wing as bounded (not a {cls})"))
        kp_line, kp_ok = keyplan_check(img_in, question, 0, ev[-1].bbox, wing)
        L = info["length"] / f / ppm
        assumptions = [
            side_note,
            f"the {wing} wing = the arm of the building that reaches the {disp} of the sheet, from its end to where it "
            f"meets the wider part of the building (~{L:.0f} m long by the door-size scale"
            + (", incl. the narrow link corridor to the core" if info["neck"] else "") + "); "
            f"{cls}s on its outer walls and at its end count, {cls}s on the line where it meets the core count only "
            "when the symbol lies inside the wing",
            f"no scale used: ~{ppm:.0f} px per metre on the image, from the door symbols themselves (a door leaf ≈ "
            "0.85 m)"]
        if nb:
            assumptions.append(f"{nb} of the {len(boxes)} {cls}s {'sits' if nb == 1 else 'sit'} on the line where the wing meets the rest of the "
                               f"building (counted; {len(boxes) - nb} without {'them' if nb > 1 else 'it'})")
        if cls == "door":
            assumptions.append("Convention: a double door counts as ONE door (one detector box per doorset); closet / "
                               "wardrobe / cupboard doors are not counted" + (
                                   f": {len(closets)} door symbols open onto a closet (a small empty enclosed space "
                                   f"behind the door) and were left out ({len(boxes) + len(closets)} with them)"
                                   if closets else ""))
        conf = 0.45 if self._clean(img) else 0.35
        if conf < 0.45:
            assumptions.append(f"the image looks like a scan or photo: faint {cls} symbols are often missed")
            if cls == "door" and not closets and os.environ.get("STOA_DR_CLOSETS", "1") != "0":
                # X1 sc-314 / 315 (AFCEC scans: mottled fills, broken walls): rooms leak into each other, so no closet
                # is found and the count may hold closet doors (52 / 49 vs 34)
                conf = 0.3
                assumptions.append("closet doors could not be told apart on this scan (walls too broken to close the "
                                   "spaces behind the doors): the count may include closet / wardrobe doors")
        if rep:
            printed = None
            for tp in dict.fromkeys((img_in, path, img)):  # the upload's text layer is cached on the copy it read
                printed = printed_room_count(tp, 0, wing, question)
                if printed:
                    break
            line, ok = repetition_line(rep, len(boxes), printed, wing)
            trace.append("openings: " + line)
            assumptions.insert(0, line)
            assumptions[1:1] = fix_notes(fix, rep["n"])
            if odd_boxes:
                trace.append("openings: odd symbols left out at " + "; ".join(
                    ",".join(f"{v * back:.0f}" for v in b[:4]) for b in odd_boxes))
            if ok and conf >= 0.45:  # a clean sheet only: on a scan the closets are not told apart (k holds them)
                conf = 0.7 if printed else 0.6
        if kp_line:
            trace.append("openings: " + kp_line)
            assumptions.insert(1 if rep else 0, kp_line)
            conf = max(conf, 0.55) if kp_ok and conf >= 0.45 else (min(conf, 0.4) if kp_ok is False else conf)
        scat = rep_scattered(rep, cls) if rep else None
        if scat:
            conf = min(conf, 0.3)
            assumptions.insert(1, scat)
            trace.append("openings: " + scat)
        if cls == "window":
            # window symbols vary far more than door swings (AFCEC east wing: 11 boxes for 16 rooms' outer walls)
            conf = min(conf, 0.3)
            assumptions.append("window symbols on a picture are read by the symbol detector only (no glazing-line "
                               "check): likely low")
        return Answer(value=len(boxes), unit="count", confidence=conf, evidence=ev, trace=trace,
                      assumptions=assumptions,
                      missing=[f"the wing's name on the plan itself (here it was bounded by the building's shape)",
                               f"a {cls} schedule to confirm the count"])
