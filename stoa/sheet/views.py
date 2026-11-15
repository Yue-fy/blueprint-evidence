"""Views (viewports) of a multi-view drawing sheet, and which of them a question means (VP, captain fix list 18:21 (3)).

One US/UK sheet often holds several drawings: floor plans, reflected ceiling plans (RCP), axonometrics, sections,
elevations, key plans, schedules and legends. Counting or measuring the whole sheet then counts every room label twice
(plan + RCP: AFCEE A-101 'total area of module 01' summed both) and every door in the axonometric too. This module:

  views(path, page=0)     -> [View]: titled views from the text layer (view-kind words in a title-like line: short,
                             upper case or large print, not a 'SEE DETAIL…' note, outside the title block), each with its
                             number bubble, its own scale note ('1/8" = 1'-0"' under the title) and a paper-size note
                             ('SCALES … BASED ON A FULL 22X34 SHEET', '1:200 @ A1') turned into the scale on THIS page;
                             the drawing extent = the ink blobs (low-res render, whitespace clustering) that sit just
                             above / around the title. All geometry is computed on the DISPLAYED page and returned in
                             PyMuPDF's unrotated page space (pdf_pt, CONTRACT evidence space): 5 of Stoa's 9 PDFs have
                             /Rotate 90.
  pick_views(q, views)    -> Pick: the view(s) a question means: kind words (ceiling/RCP, elevation, section, axon,
                             roof, site, foundation/slab, piping) else the plan views; then the question's named subject
                             ('module 02', 'administration module', 'second floor', 'level 1'); no subject = every plan
                             view of the asked kind ('whole building'). Never RCP / axon / section for counts or areas
                             unless the question asks for them.
  scope_for(path, q)      -> Scope | None: the hook for readers (G1 router, E1 evidence, DR openings). None = the sheet
                             has < 2 views or nothing to narrow: use the whole sheet as before.

Deterministic, text layer + one low-res render, ~0.1-0.4 s per page, $0.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------------------------------------------------
# view kinds

# order matters: the first match wins ('REFLECTED CEILING PLAN' is an RCP, not a floor plan)
_KINDS: list[tuple[str, re.Pattern]] = [
    ("notes", re.compile(r"\bNOTES?\b|\bLEGEND\b|\bABBREVIATIONS?\b|\bSYMBOLS?\b", re.I)),
    ("schedule", re.compile(r"\bSCHEDULES?\b|\bTABLE\b|\bMATRIX\b", re.I)),
    ("rcp", re.compile(r"REFLECTED\s+CEILING|\bRCP\b|\bCEILING\s+PLAN|\bALAKATTO", re.I)),
    ("axon", re.compile(r"\bAXON|\bISOMETRIC|\bISO\s+VIEW|\b3D\b|\bPERSPECTIVE|\bBIRD'?S?\s*EYE", re.I)),
    ("section", re.compile(r"\bSECTIONS?\b|\bLEIKKAUS|\bSCHNITT|\bCOUPE\b|\bSEKTION\b", re.I)),
    ("elevation", re.compile(r"\bELEVATIONS?\b|\bJULKISIVU|\bANSICHT|\bFA[CÇ]ADE|\bFASAD(?:ER)?\b", re.I)),
    ("detail", re.compile(r"\bDETAILS?\b|\bENLARGED\s+(?!PLAN)", re.I)),
    ("key_plan", re.compile(r"\bKEY\s*PLAN\b|\bKEYPLAN\b|\bLOCATION\s+PLAN\b|\bVICINITY", re.I)),
    ("site_plan", re.compile(r"\bSITE\s+PLAN\b|\bMASTER\s*-?\s*PLAN\b|\bLANDSCAPE\s+(?:PLAN|LAYOUT|GENERAL\s+ARRANGEMENT)\b|"
                             r"\bPLOT\s+PLAN\b|\bASEMAPIIRUS|\bLAGEPLAN|\bSITUATIONSPLAN", re.I)),
    ("roof_plan", re.compile(r"\bROOF\s+(?:FRAMING\s+)?PLAN\b|\bVESIKATTO", re.I)),
    ("foundation_plan", re.compile(r"\bFOUNDATION\b.*\bPLAN\b|\bSLAB\s+PLAN\b|\bFOOTING\s+PLAN\b|"
                                   r"\b(?:FLOOR\s+)?FRAMING\s+PLAN|\bPERUSTUS", re.I)),
    ("services_plan", re.compile(r"\b(?:PIPING|PLUMBING|MECHANICAL|HVAC|DUCT(?:WORK)?|ELECTRICAL|LIGHTING|POWER|"
                                 r"FIRE\s+PROTECTION|SPRINKLER|SANITARY|DRAINAGE|WATER)\b.*\bPLAN\b|\bLVI\b", re.I)),
    ("floor_plan", re.compile(r"\bFLOOR\s+PLAN\b|\bPLAN\b|\bPOHJA(?:PIIRUSTUS)?\b|\bKERROS\b|\b\d\.?\s*KRS\b|\bV[ÅA]NINGSPLAN|\bGRUNDRISS|\bPLAN\s+DE\b|"
                              r"\bPLATTEGROND|\bPLANTA\b|\bLEVEL\s+\w+\s*$|\bGROUND\s+FLOOR\s*$|\bUPPER\s+FLOOR\s*$|"
                              r"\b(?:FIRST|SECOND|THIRD|FOURTH|FIFTH)\s+FLOOR\s*$", re.I)),
]
# kinds that are drawings of the building's floor area (count / measure candidates)
PLAN_KINDS = ("floor_plan", "drawing", "services_plan", "foundation_plan", "roof_plan", "site_plan")
DRAWING_KINDS = PLAN_KINDS + ("rcp", "axon", "section", "elevation", "detail", "key_plan")

_NOT_TITLE = re.compile(r"^\s*\*(?!\s*(?:GENERAL\s+)?NOTES?\b)|^\s*(?:SEE|REFER|FOR|NOTE|TYP|PROVIDE|VERIFY|COORDINATE|CONTRACTOR|ALL|THIS|THE|WHERE|IF|"
                        r"WHICH|THAT|BE|PER|INDICATED|EXTEND|STEP|PLACE|INSTALL|UNDER|TOTAL|SUBTOTAL|SUM)\b|"
                        # a heading of notes / a table row label: 'STANDARD DESIGN PLAN:', 'SECOND FLOOR ='
                        r"[:=]\s*$|"
                        # a wrapped note line: ends on a conjunction / preposition, or reads like a spec sentence
                        r"\b(?:AND|OR|OF|TO|WITH|UNDER|FOR|AT|IN|ON|BY|THE|A)\s*$|\bACCORDANCE\b|\bMINIMUMS?\b|"
                        r"\bMAY\b|\bREQ(?:UIRED)?\b|\b(?:IS|ARE|WAS|WERE|WILL|MUST|SHOULD)\b|"
                        r"\bSEE\b|\bSHEET\s+[A-Z]{0,3}-?\d|\bON\s+SHEET\b|\bREFER\s+TO\b|\bIS\s+BASED\b|\bSHALL\b|"
                        r"\bDO(?:ES)?\s+NOT\b|\bAS\s+NOTED\b|\bTHIS\s+SHEET\b|\(SEE\b|\bPER\s+PLAN\b|"
                        r"\bPER\s+(?:DETAIL|SECTION|SCHEDULE)\b|"
                        # a drawing-index row on a cover sheet: 'A-101 LIFE SAFETY PLAN - FIRST FLOOR', 'CS100 SITE PLAN'
                        r"^\s*[A-Z]{1,3}-?\d{3}(?:\.\d+)?[A-Z]?\s|^\s*[A-Z]{1,2}-?\d{1,2}\.\d{1,2}[A-Z]?\s|"
                        # symbol legends: 'SECTION MARKER', 'ELEVATION MARKER', 'DETAIL CALLOUT', 'DOOR TAG'
                        r"\bMARKER\b|\bCALLOUT\b|\bTAG\b|\bSYMBOLS?\b|\bINDICATOR\b|"
                        # a wrapped note fragment: '(SEE) PLAN & GENERAL (NOTES)'
                        r"^\s*PLANS?\s*(?:&|AND)\s", re.I)
_TITLEBLOCK = re.compile(r"\bDRAWN\b|\bCHECKED\b|\bDESIGNED\b|\bAPPROVED\b|\bPROJECT\s*(?:NO|NUMBER|#)|\bDRAWING\s*(?:NO|"
                         r"NUMBER|TITLE)|\bSHEET\s*(?:NO|NUMBER|TITLE)|\bREVISIONS?\b|\bDATE\s*:|\bJOB\s*NO|"
                         r"\bSUBMITTED\b|\bISSUE\s*(?:DATE|STATUS)|\bCLIENT\b|\bPIIRT\.|\bPIIRTÄJÄ|\bMITTAKAAVA\b|\bPHASE\s*:|"
                         r"^\s*SCALE\s*:?\s*$|^\s*SHEET\s*$|^\s*NUMBER\s*$|\bAS\s+INDICATED\b|\bNOT\s+FOR\s+CONSTRUCTION|"
                         r"\bDWG\s*NO|\bCAD\s+FILE|\bPLOT\s+DATE", re.I)
_KEY_PLAN = re.compile(r"\bKEY\s*PLAN\b|\bKEYPLAN\b", re.I)
_STOREY_ONLY = re.compile(r"^\s*(?:(?:FIRST|SECOND|THIRD|FOURTH|FIFTH|GROUND|UPPER|LOWER)\s+FLOOR|LEVEL\s+\w+)\s*[:=.]?\s*$",
                          re.I)
_NUMBER_BUBBLE = re.compile(r"^\s*[A-Z]?\d{1,2}[A-Z]?\s*$")
_SCALE_WORD = re.compile(r"^\s*SCALE\s*:?\s*$", re.I)
# paper-size notes: 'SCALE(S) AS NOTED ON THIS SHEET ARE BASED ON A FULL 22X34 SHEET', 'ORIGINAL SHEET SIZE 30x42',
# '1:200 @ A1', '1:100 at A3', 'A1 size'
_PAPER_IN = re.compile(r"\b(?:FULL|ORIGINAL|SHEET\s+SIZE)\D{0,25}?(\d{2})\s*\"?\s*[X×]\s*(\d{2})\s*\"?|"
                       r"\b(\d{2})\s*\"?\s*[X×]\s*(\d{2})\s*\"?\s*(?:FULL\s+)?(?:SHEET|PAPER|PRINT)", re.I)
_AT_ISO = re.compile(r"1\s*:\s*(\d{2,4})\s*(?:@|at|auf|på|paperilla)\s*\(?\s*(A[0-4])\b", re.I)
_ISO_LONG_IN = {"A0": 1189 / 25.4, "A1": 841 / 25.4, "A2": 594 / 25.4, "A3": 420 / 25.4, "A4": 297 / 25.4}

_KIND_WORDS = re.compile(r"\b(?:REFLECTED|CEILING|FLOOR|PLANS?|RCP|AXON(?:OMETRIC)?|ISOMETRIC|3D|VIEW|PERSPECTIVE|"
                         r"SECTIONS?|ELEVATIONS?|DETAILS?|KEY|SCHEDULES?|LEGEND|PIPING|FOUNDATION|AND|SLAB|"
                         r"OVERALL|PARTIAL|ENLARGED|ARCHITECTURAL|SCALE|NTS)\b\.?", re.I)


def view_kind(title: str) -> str | None:
    for k, rx in _KINDS:
        if rx.search(title):
            return k
    return None


def subject_of(title: str) -> str:
    """'MODULE 01- ENTRY VESTIBULE - FLOOR PLAN' -> 'MODULE 01 ENTRY VESTIBULE' (the named part, for matching)."""
    s = _KIND_WORDS.sub(" ", title)
    s = re.sub(r"[-–—:,/.()]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------------------------------------------------------------------------
# data


@dataclass
class View:
    kind: str                      # floor_plan | rcp | axon | section | elevation | detail | key_plan | site_plan | ...
    title: str
    bbox: tuple | None             # drawing extent, page space (pdf_pt unrotated for PDFs; pixels for images); None = unknown
    title_bbox: tuple              # page space
    number: str | None = None      # the view's number bubble ('4')
    scale_text: str | None = None  # printed scale note of this view
    ratio: float | None = None     # printed 1:N
    page_ratio: float | None = None  # 1:N on THIS page (paper-size note applied); == ratio when no note
    subject: str = ""              # title without kind words ('MODULE 02 ADMINISTRATION')
    confidence: float = 0.5
    display_bbox: tuple | None = None  # extent on the displayed (rotated) page, for layout / UI
    notes: list = field(default_factory=list)
    bar_ratio: float | None = None  # 1:N measured on a graphic scale bar of this page (None = no bar read)
    px_per_m: float | None = None   # page units (pdf pt / image px) per real metre on THIS page; images: from a scale
    #                                 bar, else the printed 1:N + a paper-size note (the image = the whole sheet)

    def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
        if not self.bbox:
            return False
        x0, y0, x1, y1 = self.bbox
        return x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("kind", "title", "bbox", "title_bbox", "number", "scale_text", "ratio",
                                              "page_ratio", "subject", "confidence", "display_bbox", "notes", "bar_ratio",
                                              "px_per_m")}


@dataclass
class Pick:
    views: list                    # the views the question means ([] = no scoping)
    reason: str
    confidence: float
    asked_kind: str = "plan"


@dataclass
class Scope:
    """What a reader needs: the boxes (page space) the question means, why, and the scale there."""
    views: list
    reason: str
    confidence: float
    all_views: list = field(default_factory=list)

    @property
    def bboxes(self) -> list:
        return [v.bbox for v in self.views if v.bbox]

    @property
    def ratio(self) -> float | None:
        rs = {v.page_ratio or v.ratio for v in self.views if (v.page_ratio or v.ratio)}
        return rs.pop() if len(rs) == 1 else None

    def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
        return any(v.contains(x, y, pad) for v in self.views)

    def contains_box(self, box, pad: float = 0.0) -> bool:
        return self.contains((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, pad)

    def note(self) -> str:
        names = "; ".join(f"'{v.title}'" for v in self.views)
        return f"scoped to view(s) {names} ({self.reason})"


@dataclass
class _Line:
    text: str
    box: tuple       # displayed page, pt (y down)
    src: tuple       # page space (pdf_pt unrotated / image px)
    size: float      # font size (pt) or box height

    @property
    def h(self) -> float:
        return max(1.0, min(self.box[3] - self.box[1], self.size * 1.4 if self.size else 1e9))

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2


# ----------------------------------------------------------------------------------------------------------------------
# page reading


def _reading_turn(pg, blocks) -> int:
    """Degrees (0/90/180/270) to turn the DISPLAYED page so that most of its text reads left-to-right: a sheet whose
    drawing is placed sideways on the page (X1 sl-148 rot90 / sl-150 rot270, landscape CAD sheets exported portrait)
    has its view titles beside the drawings, not under them, until it is turned."""
    import pymupdf

    rot = pg.rotation_matrix
    lin = pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
    votes = {0: 0, 90: 0, 180: 0, 270: 0}
    for b in blocks:
        for ln in b.get("lines", []):
            n = sum(len(sp.get("text", "").strip()) for sp in ln.get("spans", []))
            if not n:
                continue
            d = pymupdf.Point(ln.get("dir", (1, 0))) * lin
            for k in votes:
                q = d * pymupdf.Matrix(k)
                if q.x > 0.9:
                    votes[k] += n
                    break
    best = max(votes, key=votes.get)
    return best if votes[best] > 1.5 * max(1, votes[0]) else 0


def _pdf_page(path: str, page: int):
    """-> (lines, ink grid uint8 (reading frame, 255 = ink), px_per_pt, frame w/h pt, to_src fn, to_disp fn).
    Layout runs in the READING frame (the displayed page turned so that its text reads left-to-right; usually the
    displayed page itself); to_src maps back to PyMuPDF's unrotated page space (pdf_pt), to_disp to displayed points."""
    import numpy as np
    import pymupdf

    with pymupdf.open(path) as doc:
        if page >= len(doc):
            return None
        pg = doc[page]
        blocks = pg.get_text("dict").get("blocks", [])
        turn = _reading_turn(pg, blocks)
        m = pymupdf.Matrix(turn)
        R = pg.rect * m
        T = m * pymupdf.Matrix(1, 0, 0, 1, -R.x0, -R.y0)   # displayed -> reading
        full = pg.rotation_matrix * T                       # unrotated -> reading
        back, undisp = ~full, ~T
        W, H = R.width, R.height
        lines = []
        for b in blocks:
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                t = re.sub(r"\s+", " ", " ".join(s.get("text", "") for s in spans)).strip()
                if not t:
                    continue
                src = tuple(ln["bbox"])
                r = pymupdf.Rect(src) * full
                r.normalize()
                lines.append(_Line(t, (r.x0, r.y0, r.x1, r.y1), src, max((s.get("size", 0) for s in spans), default=0)))
        s = max(0.25, 640.0 / max(W, H))
        pix = pg.get_pixmap(matrix=m * pymupdf.Matrix(s, s), colorspace=pymupdf.csGRAY, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
        ink = (img < 235).astype(np.uint8) * 255

    def _map(box, mat):
        r = pymupdf.Rect(box) * mat
        r.normalize()
        return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))

    return _merge_lines(lines), ink, s, W, H, (lambda b: _map(b, back)), (lambda b: _map(b, undisp))


def _image_page(path: str, layer):
    """A raster sheet (PNG / JPEG / photo): lines from the caller's text layer (stoa.solvers.raster.textlayer
    TextLayer, image px; never read here: OCR is the caller's cost), ink from the image. Page space = image px."""
    import cv2
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    W, H = (float(v) for v in layer.size)
    lines = []
    for it in layer.items:
        t = re.sub(r"\s+", " ", it.text or "").strip()
        if not t:
            continue
        b = tuple(float(v) for v in it.box)
        w, h = b[2] - b[0], b[3] - b[1]
        # vertical text (a title block's drawing title along the right edge): its print size is the box WIDTH
        size = w if len(t) >= 4 and h > 2.5 * w else h
        lines.append(_Line(_repair_cut_words(t), b, b, size))
    lines = _join_tile_cuts(lines)
    im = np.asarray(Image.open(path).convert("L"))
    thr = min(235, float(np.median(im)) - 25)          # phone photos: grey paper
    full = (im < thr).astype(np.uint8) * 255
    s = max(0.05, 640.0 / max(W, H))
    ink = cv2.resize(full, (max(1, int(W * s)), max(1, int(H * s))), interpolation=cv2.INTER_AREA)
    ink = (ink > 8).astype(np.uint8) * 255

    def to_src(box):
        return tuple(round(float(v), 2) for v in box)

    return _merge_lines(lines), ink, s, W, H, to_src, to_src


_STOREY_WORDS = ("FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "GROUND", "BASEMENT", "UPPER", "LOWER")


def _repair_cut_words(t: str) -> str:
    """A tile-cut OCR line that starts mid-word on a storey name ('OND FLOOR PLAN' <- 'SECOND FLOOR PLAN', 'IRST
    FLOOR' <- 'FIRST FLOOR'): the cut word is completed when it is the tail (>= 3 letters) of exactly one storey word."""
    m = re.match(r"^([A-Za-z]{2,7})(\s+(?:FLOOR|LEVEL|STOREY|STORY)\b)", t, re.I)
    if not m or m.group(1).upper() in _STOREY_WORDS:
        return t
    tail = m.group(1).upper()
    if len(tail) == 2:  # '1ST' / '2ND' / '3RD' with the digit cut off
        hits = {"ST": ["FIRST"], "ND": ["SECOND"], "RD": ["THIRD"]}.get(tail, [])
    else:
        hits = [w for w in _STOREY_WORDS if w.endswith(tail) and w != tail]
    if len(hits) != 1:
        return t
    w = hits[0] if m.group(1).isupper() else hits[0].capitalize()
    return w + t[m.end(1):]


def _cut_overlap(a: str, b: str, max_k: int) -> int | None:
    """Characters of `a`'s end that `b` repeats at its start (a tile cut reads the overlap twice; the cut letter at
    `a`'s end may be misread: 'ENTRY VESTIE' + 'VESTIBULE - FLOOR PLAN'). -> number of chars of `a` to drop."""
    A, B = a.upper(), b.upper()
    for drop_last in (0, 1):
        a2 = A[:len(A) - drop_last] if drop_last else A
        for k in range(min(len(a2), len(B), max_k), 2, -1):
            sa, sb = a2[-k:], B[:k]
            bad = sum(x != y for x, y in zip(sa, sb))
            if bad <= (k - 1) // 4 and sa.strip() and sa[0] == sb[0]:
                return k + drop_last
    return None


def _join_tile_cuts(lines: list[_Line]) -> list[_Line]:
    """Image text layers read in overlapping tiles cut a printed line at the tile border into two items that OVERLAP
    on one baseline ('MODULE 01- ENTRY VESTIE' | 'VESTIBULE - FLOOR PLAN'); join them, dropping the repeated text."""
    lines = sorted(lines, key=lambda l: (l.box[0], l.cy))
    used = [False] * len(lines)
    out: list[_Line] = []
    for i, a in enumerate(lines):
        if used[i]:
            continue
        cur = a
        while True:
            best = None
            for j, b in enumerate(lines):
                if used[j] or j == i or b is cur:
                    continue
                if not (abs(b.cy - cur.cy) < 0.35 * min(cur.h, b.h) and abs(b.h - cur.h) < 0.3 * max(cur.h, b.h)):
                    continue
                if not (cur.box[0] + 0.2 * (cur.box[2] - cur.box[0]) < b.box[0] < cur.box[2] < b.box[2]):
                    continue
                cw = (cur.box[2] - cur.box[0]) / max(1, len(cur.text))
                k = _cut_overlap(cur.text, b.text, int(2 * (cur.box[2] - b.box[0]) / max(cw, 1e-6)) + 3)
                if k is not None and (best is None or b.box[0] < best[1].box[0]):
                    best = (j, b, k)
            if best is None:
                break
            j, b, k = best
            used[j] = True
            box = (cur.box[0], min(cur.box[1], b.box[1]), b.box[2], max(cur.box[3], b.box[3]))
            cur = _Line(cur.text[:len(cur.text) - k] + b.text, box, box, max(cur.size, b.size))
        used[i] = True
        out.append(cur)
    return out


def join_tile_cut_items(items: list) -> list:
    """The same join for raw text-layer items (stoa.solvers.raster.textlayer.TextItem: .text, .box image px, .tile):
    a line cut at a tile border into two overlapping items becomes one item ('ING DORMITORY =' + ',095 SF' rows of
    Stoa's dorm PNG area table -> 'WEST WING DORMITORY =' / '6,095 SF'; '300 SLAB-ON-GRADE W/' + 'ADE W/ D16').
    Measured on 208 cached VLM layers: 1,092 of 29,659 items (3.7 %) were halves; 40 / 40 sampled joins right.
    Returns new item objects (copies; the input is not changed); items without a partner are returned as they are."""
    import copy

    lines, src = [], {}
    for it in items:
        t = re.sub(r"\s+", " ", getattr(it, "text", "") or "").strip()
        b = tuple(float(v) for v in getattr(it, "box", ()) or ())
        if not t or len(b) != 4:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        ln = _Line(t, b, b, w if len(t) >= 4 and h > 2.5 * w else h)
        lines.append(ln)
        src[id(ln)] = it
    joined = _join_tile_cuts(lines)
    first = {(l.box[0], l.box[1]): src[id(l)] for l in lines}
    out, kept = [], set()
    for ln in joined:
        it = src.get(id(ln))
        if it is not None:
            out.append(it)
            kept.add(id(it))
            continue
        # a joined line: copy the left-most part item it starts with, new text and box
        base = first.get((ln.box[0], ln.box[1])) or next(
            (src[id(l)] for l in lines if l.box[0] == ln.box[0] and abs(l.cy - ln.cy) < 0.5 * ln.h), None)
        new = copy.copy(base) if base is not None else None
        if new is None:
            continue
        new.text = ln.text
        new.box = [round(v, 1) for v in ln.box]
        out.append(new)
    # items that were never lines (empty text, odd boxes) stay
    as_line = {id(v) for v in src.values()}
    out += [it for it in items if id(it) not in kept and id(it) not in as_line]
    return out


def _merge_lines(lines: list[_Line]) -> list[_Line]:
    """PyMuPDF splits one printed title into several lines when the spans are far apart ('MODULE 01' | '- FLOOR PLAN');
    join lines on the same baseline whose gap is < 2 character heights."""
    lines = sorted(lines, key=lambda l: (round(l.cy / max(l.h, 1)), l.box[0]))
    out: list[_Line] = []
    for ln in lines:
        p = out[-1] if out else None
        if (p and abs(p.cy - ln.cy) < 0.3 * min(p.h, ln.h) and abs(p.h - ln.h) < 0.25 * max(p.h, ln.h)
                and 0 <= ln.box[0] - p.box[2] < 1.2 * p.h):
            box = (p.box[0], min(p.box[1], ln.box[1]), ln.box[2], max(p.box[3], ln.box[3]))
            src = (min(p.src[0], ln.src[0]), min(p.src[1], ln.src[1]), max(p.src[2], ln.src[2]), max(p.src[3], ln.src[3]))
            out[-1] = _Line(p.text + " " + ln.text, box, src, max(p.size, ln.size))
        else:
            out.append(ln)
    return out


def _parse_scales(text: str) -> list[dict]:
    from stoa.solvers.raster.textlayer import parse_scales

    # 'SCALE:1-1/2"-1'' is 1 1/2" = 1'-0" (1:8); parse_scales reads 1/2" = 1' (1:24) from it (FTC sheet 6 scale bar
    # 0-1' measures 1:8.1)
    text = re.sub(r"(\d+)-(\d+/\d+)(\s*[\"”])", r"\1 \2\3", text)
    return [s for s in parse_scales(text) if 5 <= s["ratio"] <= 5000]


# ----------------------------------------------------------------------------------------------------------------------
# views


def _titleblock_zone(lines: list[_Line], W: float, H: float) -> tuple | None:
    """The title block (displayed page box): where 'DRAWN BY / CHECKED / PROJECT NO / DATE / SHEET' lines sit, along
    the right or bottom edge."""
    hits = [l for l in lines if _TITLEBLOCK.search(l.text)]
    if len(hits) < 2:
        return None
    right = [l for l in hits if l.box[0] > 0.7 * W]
    bottom = [l for l in hits if l.box[1] > 0.7 * H]
    if len(right) >= max(2, len(bottom)):
        return (min(l.box[0] for l in right) - 0.01 * W, 0, W, H)
    if len(bottom) >= 2:
        return (0, min(l.box[1] for l in bottom) - 0.01 * H, W, H)
    return None


def _in(box, zone) -> bool:
    if not zone:
        return False
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return zone[0] <= cx <= zone[2] and zone[1] <= cy <= zone[3]


def _median_size(lines: list[_Line]) -> float:
    import statistics

    sizes = [l.size for l in lines if l.size]
    return statistics.median(sizes) if sizes else 0.0


def _title_candidates(lines: list[_Line], zone, med: float) -> list[tuple[_Line, str]]:
    out = []
    for l in lines:
        t = l.text.strip()
        words = t.split()
        if not (1 <= len(words) <= 12) or len(t) > 90:
            continue
        if _in(l.box, zone) and not _KEY_PLAN.search(t):  # a key plan often sits in the title-block column
            continue
        if _NOT_TITLE.search(t) or _parse_scales(t) and len(words) <= 4 or re.match(r"^(?:AND|OR|WITH|TO)\b", t, re.I):
            continue
        k = view_kind(t)
        if not k:
            continue
        letters = [c for c in t if c.isalpha()]
        upper = sum(c.isupper() for c in letters) / max(1, len(letters))
        big = med and l.size >= 1.4 * med
        if upper < 0.7 and not big:
            continue
        if len(words) == 1 and not big:
            continue  # a lone 'PLAN' / 'SECTION' tag in a drawing
        out.append((l, k))
    return out


def _sheet_title(lines: list[_Line], zone) -> tuple[_Line, str] | None:
    """The drawing title in the title block ('SECOND FLOOR PLAN'): names the sheet's main, often untitled, view."""
    best = None
    for l in lines:
        if not _in(l.box, zone) or len(l.text.split()) > 8 or _NOT_TITLE.search(l.text):
            continue
        k = view_kind(l.text)
        if k in DRAWING_KINDS and (best is None or l.size > best[0].size):
            best = (l, k)
    return best


def _near_scale(title: _Line, lines: list[_Line]) -> tuple[_Line | None, dict | None]:
    """The scale note of this view: in the title itself, right of it on the same row, or just under it."""
    own = _parse_scales(title.text)
    if own:
        return title, own[0]
    h = title.h
    best, bd = None, 1e18
    for l in lines:
        if l is title:
            continue
        sc = _parse_scales(l.text)
        if not sc:
            continue
        dy = l.cy - title.cy
        if abs(dy) < 0.6 * h and 0 <= l.box[0] - title.box[2] < 20 * h:      # same row, to the right
            d = l.box[0] - title.box[2]
        elif 0 < dy < 3.5 * h and title.box[0] - 6 * h <= l.box[0] <= title.box[2] + h:  # below
            d = dy * 2 + abs(l.box[0] - title.box[0]) * 0.2
        else:
            continue
        if d < bd:
            best, bd = (l, sc[0]), d
    return best if best else (None, None)


def _near_number(title: _Line, lines: list[_Line]) -> str | None:
    h = title.h
    best, bd = None, 1e18
    for l in lines:
        if l is title or not _NUMBER_BUBBLE.match(l.text):
            continue
        dx = title.box[0] - l.box[2]
        if -0.5 * h <= dx < 6 * h and abs(l.cy - title.cy) < 1.6 * h and dx < bd:
            best, bd = l.text.strip(), dx
    return best


def _paper_factor(lines: list[_Line], W: float, H: float, ratio: float | None) -> tuple[float, str | None]:
    """Printed-for-paper / this-page linear factor (> 1 = this page is smaller than the paper the scales are for)."""
    long_in = max(W, H) / 72.0
    for iso in _ISO_LONG_IN.values():  # an ISO page: use its nominal size (1:200 @ A1 on A3 = exactly 1:400)
        if abs(long_in - iso) / iso < 0.03:
            long_in = iso
    for l in lines:
        m = _PAPER_IN.search(l.text)
        if m:
            a, b = [int(g) for g in m.groups() if g][:2]
            paper = max(a, b)
            f = _snap(paper / long_in)
            if abs(f - 1) > 0.05 and 0.2 < f < 5:
                return f, f"scales are printed for a {a}x{b} in sheet; this page is {W / 72:.0f}x{H / 72:.0f} in"
            return 1.0, None
    for l in lines:
        m = _AT_ISO.search(l.text)
        if m and (ratio is None or float(m.group(1)) == float(ratio)):
            paper = _ISO_LONG_IN[m.group(2).upper()]
            f = _snap(paper / long_in)
            if abs(f - 1) > 0.05 and 0.2 < f < 5:
                return f, f"'{m.group(0)}' printed on a {W / 72 * 25.4:.0f}x{H / 72 * 25.4:.0f} mm page"
            return 1.0, None
    return 1.0, None


_BAR_LABEL = re.compile(r"^\s*(\d{1,4}(?:\.\d+)?)\s*('|’|ft\.?|feet|m|metres|meters|km)?\s*$", re.I)
_BAR_UNIT_WORD = re.compile(r"\b(FEET|FT|METRES|METERS|METRE|METER|M)\b", re.I)
_IN_PER = {"ft": 12.0, "m": 39.37007874, "km": 39370.07874}


def scale_bars(lines: list[_Line]) -> list[dict]:
    """Graphic scale bars from their labels: >= 3 numbers on one row (or 2 plus a '0'), increasing left to right and
    placed linearly (fit within 2 % of the span), with a unit on a label or a FEET / METRES word on the row. The bar
    shrinks with the print, so 1:N measured here is the scale ON THIS PAGE. -> [{ratio, unit, box, labels}]."""
    nums = []
    for l in lines:
        m = _BAR_LABEL.match(l.text)
        if not m:
            continue
        u = (m.group(2) or "").lower()
        unit = "ft" if u in ("'", "’", "ft", "ft.", "feet") else ("m" if u in ("m", "metres", "meters") else
                                                                  ("km" if u == "km" else None))
        digits = len(m.group(1))
        # centre of the digits (a '10m' label's centre is off its tick by half the unit's width)
        cx = l.box[0] + (l.box[2] - l.box[0]) * (digits / max(1, len(l.text.strip()))) / 2
        nums.append((l, float(m.group(1)), unit, cx))
    rows: list[list] = []
    for it in sorted(nums, key=lambda t: t[0].cy):
        for r in rows:
            if abs(r[0][0].cy - it[0].cy) < 0.6 * min(r[0][0].h, it[0].h) and abs(r[0][0].h - it[0].h) < 0.4 * r[0][0].h:
                r.append(it)
                break
        else:
            rows.append([it])
    out = []
    for r in rows:
        r = sorted(r, key=lambda t: t[3])
        # split a row at big gaps (two bars / a bar and a dimension string on one line)
        groups, cur = [], [r[0]]
        for a, b in zip(r, r[1:]):
            if b[3] - a[3] > 25 * max(a[0].h, b[0].h):
                groups.append(cur)
                cur = [b]
            else:
                cur.append(b)
        groups.append(cur)
        for g in groups:
            vals = [t[1] for t in g]
            if len(g) < 3 and not (len(g) == 2 and vals[0] == 0):
                continue
            if any(b <= a for a, b in zip(vals, vals[1:])):
                continue
            if vals == list(range(1, len(vals) + 1)) or vals == [float(i) for i in range(1, len(vals) + 1)]:
                continue  # sheet grid references 1 2 3 4 …
            units = {t[2] for t in g if t[2]}
            if len(units) > 1:
                continue
            unit = units.pop() if units else None
            if unit is None:
                h = g[0][0].h
                y = g[0][0].cy
                x0, x1 = g[0][3], g[-1][3]
                for l in lines:
                    if abs(l.cy - y) < 3 * h and x0 - 8 * h <= l.cx <= x1 + 8 * h and _BAR_UNIT_WORD.search(l.text) \
                            and not _BAR_LABEL.match(l.text) and len(l.text) < 30:
                        w = _BAR_UNIT_WORD.search(l.text).group(1).upper()
                        unit = "ft" if w.startswith("F") else "m"
                        break
            if unit is None:
                continue
            xs = [t[3] for t in g]
            n = len(g)
            mx, my = sum(vals) / n, sum(xs) / n
            sxx = sum((v - mx) ** 2 for v in vals)
            if sxx <= 0:
                continue
            slope = sum((v - mx) * (x - my) for v, x in zip(vals, xs)) / sxx   # pt per unit on paper
            if slope <= 0:
                continue
            span = xs[-1] - xs[0]
            res = max(abs(my + slope * (v - mx) - x) for v, x in zip(vals, xs))
            if n >= 3 and res > 0.02 * span + 0.5:
                continue
            ratio = _IN_PER[unit] * 72.0 / slope
            if not 5 <= ratio <= 20000:
                continue
            box = (min(t[0].box[0] for t in g), min(t[0].box[1] for t in g), max(t[0].box[2] for t in g),
                   max(t[0].box[3] for t in g))
            out.append({"ratio": round(ratio, 2), "unit": unit, "box": box, "labels": [t[0].text for t in g],
                        "px_per_unit": round(slope, 4)})
    return out


def _blobs(ink, s: float, zone, drop_boxes: list, W: float, H: float):
    """Ink clusters of the displayed page separated by whitespace -> [(x0, y0, x1, y1) displayed pt, ink px]."""
    import cv2
    import numpy as np

    g = ink.copy()
    hh, ww = g.shape
    # the sheet frame and long border / title-block rules: any straight run longer than 45 % of the page
    long_h = cv2.morphologyEx(g, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(0.45 * ww), 1)))
    long_v = cv2.morphologyEx(g, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(0.45 * hh))))
    g[(long_h > 0) | (long_v > 0)] = 0
    # page margin (frame ticks, grid reference letters) and the title block
    m = max(2, int(0.015 * min(ww, hh)))
    g[:m, :] = 0
    g[-m:, :] = 0
    g[:, :m] = 0
    g[:, -m:] = 0
    if zone:
        g[int(zone[1] * s):int(zone[3] * s) + 1, int(zone[0] * s):int(zone[2] * s) + 1] = 0
    for b in drop_boxes:  # titles / scale notes / number bubbles must not bridge a drawing to its neighbour
        g[max(0, int(b[1] * s) - 1):int(b[3] * s) + 2, max(0, int(b[0] * s) - 1):int(b[2] * s) + 2] = 0
    gap = max(3, int(round(0.012 * max(ww, hh))))   # ~0.4 in on a 34 in sheet
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (gap, gap))
    d = cv2.dilate(g, k)
    n, lab, stats, _ = cv2.connectedComponentsWithStats((d > 0).astype(np.uint8), connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        mine = (lab[y:y + h, x:x + w] == i) & (g[y:y + h, x:x + w] > 0)
        px = int(mine.sum())
        if px < 6:
            continue
        ys, xs = np.nonzero(mine)  # the component's own ink (not the dilated halo), +1 px for anti-aliased edges
        out.append(((float(max(0, x + xs.min() - 1)) / s, float(max(0, y + ys.min() - 1)) / s,
                     float(min(ww, x + xs.max() + 2)) / s, float(min(hh, y + ys.max() + 2)) / s), px))
    return out, g


def _area_of(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inside(b, e) -> float:
    """Share of box b's area inside box e."""
    ix = max(0.0, min(b[2], e[2]) - max(b[0], e[0]))
    iy = max(0.0, min(b[3], e[3]) - max(b[1], e[1]))
    a = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / a


def _snap_loose(f: float) -> float:
    """Nearest whole sqrt(2) step (print factors measured on a scale bar)."""
    k = round(math.log(f, math.sqrt(2)))
    return round(math.sqrt(2) ** k, 6)


def _snap(f: float) -> float:
    """Paper-size factors are whole ISO steps (sqrt 2) or simple ratios (22x34 -> 11x17 = 2): snap within 2 %."""
    import math

    for k in range(-6, 7):
        v = math.sqrt(2) ** k
        if abs(f - v) / v < 0.02:
            return round(v, 6)
    return f


def _assign(blobs, titles, W, H):
    """Blob -> index of the title it belongs to (or None). A view's title sits just under its drawing (US / UK
    practice) or, less often, just above it; the title's x-range overlaps the drawing's. Notes / schedule / legend
    titles head a left-aligned block below them and never own a drawing beside them."""
    owner = {}
    for bi, (b, px) in enumerate(blobs):
        bw, bh = b[2] - b[0], b[3] - b[1]
        best, bc = None, 1e18
        for ti, (tl, k) in enumerate(titles):
            t, h = tl.box, tl.h
            ov = min(b[2], t[2]) - max(b[0], t[0])
            hgap = max(0.0, -ov)
            tcx = (t[0] + t[2]) / 2
            if k == "key_plan" and (bw * bh > 0.04 * W * H or bw > 0.3 * W or bh > 0.4 * H):
                continue                           # a key plan is a small diagram, never the sheet's main drawing
            if k in ("notes", "schedule"):
                if t[3] <= b[1] + 2 * h and abs(t[0] - b[0]) < 5 * h:
                    cost = max(0.0, b[1] - t[3])
                else:
                    continue
            elif t[1] >= b[3] - 0.25 * bh:        # title below (or overlapping the bottom quarter)
                vgap = max(0.0, t[1] - b[3])
                cost = vgap + 1.5 * hgap
            elif t[3] <= b[1] + 0.1 * bh:          # title above: centred over it or left-aligned with it, close
                vgap = b[1] - t[3]
                if vgap > 8 * h or not (b[0] + 0.1 * bw <= tcx <= b[2] - 0.1 * bw or abs(t[0] - b[0]) < 4 * h):
                    continue
                cost = 2.5 * vgap + 1.5 * hgap + 0.02 * H
            else:                                  # title beside / inside the blob
                cost = 1.5 * hgap + 0.05 * H + (0 if ov > 0 else 0.05 * H)
            if cost < bc:
                best, bc = ti, cost
        lim = 0.12 * H + 0.15 * max(bw, bh)
        if best is not None and bc < lim:
            owner[bi] = (best, bc)
    return owner


@functools.lru_cache(maxsize=32)
def _views_cached(path: str, page: int) -> tuple:
    r = _pdf_page(path, page)
    if r is None:
        return ()
    out = _detect(*r)
    if _DIMSCALE:
        try:
            _apply_dims(path, page, out)
        except Exception:  # noqa: BLE001 - the check is optional: never break the views
            pass
    return tuple(out)


_DIMSCALE = __import__("os").environ.get("STOA_VP_DIMSCALE", "1") != "0"


def _apply_dims(path: str, page: int, out: list) -> None:
    """Expert rule xr-11 (Ziying, dfe-01: 'a printed 1:N holds only at its paper size; check the scale on a second
    length'): each printed scale of the page's drawing views checked on the sheet's own dimension strings
    (stoa.sheet.scalecheck). They agree -> a note ('scale checked on a second length: 85 printed dimension strings
    measure 1:50 ✓'); they CLEARLY measure the printed ratio x a paper-size step (sqrt 2, 2, 2 sqrt 2: an A1 sheet
    printed on A2 / A3 without a note) -> page_ratio / px_per_m rescaled with a note; any other disagreement is noted
    ('check'). With views at several scales only the strings inside each view's extent count. Kill switch
    STOA_VP_DIMSCALE=0."""
    from stoa.sheet.scalecheck import STEPS, dimension_scale

    groups: dict = {}
    for v in out:
        if v.kind in DRAWING_KINDS and (v.page_ratio or v.ratio):
            groups.setdefault(round(v.page_ratio or v.ratio, 3), []).append(v)
    for r, vs in groups.items():
        boxes = [v.bbox for v in vs if v.bbox] if len(groups) > 1 else None
        if len(groups) > 1 and not boxes:
            continue
        d = dimension_scale(path, page, r, within=boxes)
        if not d or d.get("agrees") is None:
            continue
        step = next((s for s in STEPS[1:] if abs(d["factor"] / s - 1) <= 0.03), None)
        for v in vs:
            if d["agrees"]:
                v.notes.append(d["note"])
                v.confidence = round(min(0.97, v.confidence + 0.02), 2)
            elif step and not (v.bar_ratio and abs(v.bar_ratio / r - 1) <= 0.05):  # a bar agreeing with 1:r wins
                v.page_ratio = round(r * step, 2)
                v.px_per_m = round(72.0 / 0.0254 / v.page_ratio, 4)
                v.notes.append(d["note"] + f" -> 1:{v.page_ratio:g} used on this page")
            else:
                v.notes.append(d["note"] + ": check")


_IMG_CACHE: dict = {}


def page_reading(path: str, page: int = 0, layer=None):
    """(lines, ink, px_per_unit, W, H, to_src) of a PDF page, or of an image given its text layer; None otherwise."""
    if str(path).lower().endswith(".pdf"):
        return _pdf_page(str(path), int(page))
    if layer is None or not getattr(layer, "items", None):
        return None
    return _image_page(str(path), layer)


def views(path: str, page: int = 0, layer=None) -> list[View]:
    """Titled views on a PDF page, or on an image when its text layer is given (see module doc). [] when nothing is
    titled (a single plain drawing) or the sheet cannot be read."""
    try:
        if str(path).lower().endswith(".pdf"):
            return list(_views_cached(str(path), int(page)))
        if layer is None or not getattr(layer, "items", None):
            return []
        key = (str(path), tuple(layer.size), len(layer.items))
        if key not in _IMG_CACHE:
            r = _image_page(str(path), layer)
            _IMG_CACHE[key] = tuple(_detect(*r, image=True, snap=lambda: _raster_bar(str(path), layer)))
            if len(_IMG_CACHE) > 32:
                _IMG_CACHE.pop(next(iter(_IMG_CACHE)))
        return list(_IMG_CACHE[key])
    except Exception:  # noqa: BLE001 - a broken page must never break a solver
        return []


def _raster_bar(path: str, layer) -> float | None:
    """px per metre of raster's scale bar reader (stoa.solvers.raster.scalebar: label fit SNAPPED to the bar's ink,
    ~0.1 % on the DfE PNG where label centres alone are 1.7 % short); None when it finds none or fails."""
    try:
        from stoa.solvers.raster import scalebar as SB

        b = SB.best_bar(path, layer=layer, reread=False)
        return float(b.px_per_m) if b is not None and b.px_per_m else None
    except Exception:  # noqa: BLE001
        return None


def _detect(lines, ink, s, W, H, to_src, to_disp=None, image: bool = False, snap=None) -> list[View]:
    to_disp = to_disp or (lambda b: tuple(round(float(v), 2) for v in b))
    zone = _titleblock_zone(lines, W, H)
    med = _median_size(lines)
    raw = _title_candidates(lines, zone, med)
    sheet_t = _sheet_title(lines, zone)
    if not raw and not sheet_t:
        return []
    # a view title is STRONG when it has a number bubble, its own scale note or large print; a weak one ('GENSET
    # FOUNDATION' inside the slab plan, 'FOUNDATION DIMENSIONS.' ending a note) only counts when no strong one exists
    cands, meta = [], []
    for tl, k in raw:
        sl, sc = _near_scale(tl, lines)
        num = _near_number(tl, lines)
        strong = bool(num or sc or (med and tl.size >= 1.4 * med) or k in ("notes", "schedule"))
        cands.append((tl, k))
        meta.append((sl, sc, num, strong))
    # a bare storey name ('FIRST FLOOR', 'LEVEL 2': no PLAN word) is a view title only with a scale, a number or big
    # print; otherwise it is a key-plan / legend label ('FIRST FLOOR = ...', OCR cut the '=' off)
    keep = [i for i, ((tl, k), m) in enumerate(zip(cands, meta)) if m[3] or not _STOREY_ONLY.match(tl.text)]
    cands = [cands[i] for i in keep]
    meta = [meta[i] for i in keep]
    if any(m[3] and k in DRAWING_KINDS for (_, k), m in zip(cands, meta)):
        keep = [i for i, m in enumerate(meta) if m[3]]
        cands = [cands[i] for i in keep]
        meta = [meta[i] for i in keep]
    drop = []
    for (tl, k), (sl, sc, num, _st) in zip(cands, meta):
        drop.append(tl.box)
        if sl is not None:
            drop.append(sl.box)
    for l in lines:  # number bubbles' text and 'SCALE:' words next to titles
        if _NUMBER_BUBBLE.match(l.text) or _SCALE_WORD.match(l.text):
            if any(abs(l.cy - t.cy) < 2 * t.h and t.box[0] - 8 * t.h < l.cx < t.box[2] + 8 * t.h for t, _ in cands):
                drop.append(l.box)
    blobs, _g = _blobs(ink, s, zone, drop, W, H)
    # titles of notes / schedules / legends own their text blocks but are not views to count in; keep them so their
    # blocks are not handed to a neighbouring plan
    owner = _assign(blobs, cands, W, H) if cands else {}
    ratios = [m[1]["ratio"] for m in meta if m[1]]
    if not ratios:
        ratios = [sc["ratio"] for l in lines for sc in _parse_scales(l.text)]
    sheet_ratio = ratios[0] if len(set(ratios)) == 1 else None
    # the sheet's main drawing is often untitled (the title block names it: 'SECOND FLOOR PLAN'); big ink blobs no
    # title owns become that view
    # a blob inside a titled drawing's extent (the building inside its own dimension / grid lines) is part of it
    ext = {}
    for bi, (ti, _c) in owner.items():
        b = blobs[bi][0]
        e = ext.get(ti)
        ext[ti] = b if e is None else (min(e[0], b[0]), min(e[1], b[1]), max(e[2], b[2]), max(e[3], b[3]))
    for bi, (b, px) in enumerate(blobs):
        if bi in owner:
            continue
        for ti, e in ext.items():
            if cands[ti][1] in DRAWING_KINDS and _inside(b, e) >= 0.8:
                owner[bi] = (ti, 0.0)
                break
    free = [bi for bi, (b, px) in enumerate(blobs) if bi not in owner and (b[2] - b[0]) * (b[3] - b[1]) > 0.03 * W * H]
    if free:
        if sheet_t:
            tl, k = sheet_t
        else:
            b0 = blobs[free[0]][0]
            tl, k = _Line("(untitled drawing)", b0, to_src(b0), 0.0), "drawing"
        cands.append((tl, k))
        meta.append((None, None, None, False))
        for bi in free:
            owner[bi] = (len(cands) - 1, 0.0)
    # one titled plan whose title owns no real drawing (a header title above / beside the sheet's only drawing) and one
    # untitled drawing: they are the same view
    if free and not sheet_t:
        ui = len(cands) - 1
        big_owned = {ti for bi, (ti, _c) in owner.items() if ti != ui and
                     _area_of(blobs[bi][0]) > 0.03 * W * H}
        titled = [ti for ti, (tl, k) in enumerate(cands[:-1]) if k in PLAN_KINDS]
        others = [ti for ti, (tl, k) in enumerate(cands[:-1]) if k in DRAWING_KINDS and ti not in titled]
        if len(titled) == 1 and not others and titled[0] not in big_owned:
            for bi, (ti, c) in list(owner.items()):
                if ti == ui:
                    owner[bi] = (titled[0], c)
            cands.pop()
            meta.pop()
    out: list[View] = []
    for ti, (tl, k) in enumerate(cands):
        mine = [blobs[bi][0] for bi, (t, _c) in owner.items() if t == ti]
        big = [b for b in mine if (b[2] - b[0]) * (b[3] - b[1]) > 0.0005 * W * H] or mine
        sl, sc, num, strong = meta[ti]
        conf = 0.5 if strong or k in ("notes", "schedule") else 0.35
        if k == "drawing" or (sheet_t and tl is sheet_t[0]):
            conf = 0.45
        if num:
            conf += 0.15
        if sc:
            conf += 0.15
        if big:
            conf += 0.15
        disp = None
        if big:
            disp = (min(b[0] for b in big), min(b[1] for b in big), max(b[2] for b in big), max(b[3] for b in big))
        ratio = sc["ratio"] if sc else None
        notes = []
        if ratio is None and sheet_ratio and k in DRAWING_KINDS:
            ratio = sheet_ratio
            notes.append("scale from the sheet's only scale note")
            conf -= 0.1
        # an image has no physical page size: its scale on the image is px per metre (below), not a 1:N
        f, why = _paper_factor(lines, W, H, ratio) if not image else (1.0, None)
        page_ratio = round(ratio * f, 3) if ratio and not image else None
        if why and ratio:
            notes.append(why + f" -> 1:{page_ratio:g} on this page")
        out.append(View(kind=k, title=tl.text, bbox=to_src(disp) if disp else None, title_bbox=tl.src, number=num,
                        scale_text=(sc or {}).get("text"), ratio=ratio, page_ratio=page_ratio,
                        subject=subject_of(tl.text), confidence=round(min(conf, 0.95), 2),
                        display_bbox=to_disp(disp) if disp else None, notes=notes))
    if image:
        _image_scales(out, lines, W, H, snap)
    else:
        _apply_bars(out, cands, lines)
        for v in out:
            if v.page_ratio and v.kind in DRAWING_KINDS:
                v.px_per_m = round(72.0 / 0.0254 / v.page_ratio, 4)
    return out


_M_PER = {"m": 1.0, "ft": 0.3048, "km": 1000.0}


def _paper_long_in(lines: list[_Line], ratio: float | None) -> tuple[float, str] | None:
    """The long side (in) of the paper the printed scales are for, from a paper-size note."""
    for l in lines:
        m = _PAPER_IN.search(l.text)
        if m:
            a, b = [int(g) for g in m.groups() if g][:2]
            return float(max(a, b)), f"{a}x{b} in sheet"
    for l in lines:
        m = _AT_ISO.search(l.text)
        if m and (ratio is None or float(m.group(1)) == float(ratio)):
            return _ISO_LONG_IN[m.group(2).upper()], m.group(0)
    return None


def _image_scales(out: list[View], lines: list[_Line], W: float, H: float, snap=None) -> None:
    """Scale ON THIS IMAGE (px per real metre) per view: a graphic scale bar (inside / nearest the view; it shrinks
    with the picture) first, else the printed 1:N with a paper-size note when the image is the whole sheet (dpi = the
    image's long side / the paper's). Both: they must agree within 5 %, else the bar wins and the view says 'check'."""
    bars = []
    for b in scale_bars(lines):
        if b.get("px_per_unit") and b["unit"] in _M_PER:
            bars.append((b, b["px_per_unit"] / _M_PER[b["unit"]]))
    if bars and snap is not None and any(v.kind in DRAWING_KINDS for v in out):
        fine = snap()  # the same bar measured on its ink: replaces a label fit within 3 % of it
        if fine:
            bars = [(b, fine if abs(ppm / fine - 1) <= 0.03 else ppm) for b, ppm in bars]
    for v in out:
        if v.kind not in DRAWING_KINDS:
            continue
        paper = _paper_long_in(lines, v.ratio) if v.ratio else None
        note_ppm = (max(W, H) / paper[0]) / 0.0254 / v.ratio if paper else None
        bar_ppm = None
        if bars:
            def dist(bb):
                bx, by = (bb[0]["box"][0] + bb[0]["box"][2]) / 2, (bb[0]["box"][1] + bb[0]["box"][3]) / 2
                if v.bbox and v.bbox[0] <= bx <= v.bbox[2] and v.bbox[1] <= by <= v.bbox[3]:
                    return 0.0
                tb = v.title_bbox
                return math.hypot((tb[0] + tb[2]) / 2 - bx, (tb[1] + tb[3]) / 2 - by)
            bar_ppm = min(bars, key=dist)[1]
        if bar_ppm and note_ppm:
            # label centres are only ~2 % accurate (AFCEE A-101 PNG: bar 121.35 vs exact 123.03 px/m): when the bar
            # agrees, the paper-size note gives the exact value and the bar is the check
            if abs(bar_ppm / note_ppm - 1) <= 0.05:
                v.notes.append(f"printed 1:{v.ratio:g} for a {paper[1]}, the image = the whole sheet: "
                               f"{note_ppm:.2f} px/m; the scale bar agrees ({bar_ppm:.2f})")
                v.confidence = round(min(0.97, v.confidence + 0.02), 2)
                v.px_per_m = round(note_ppm, 4)
            else:
                v.notes.append(f"scale bar gives {bar_ppm:.2f} px/m, the printed 1:{v.ratio:g} on a {paper[1]} "
                               f"{note_ppm:.2f}: bar used, check (the image may be cropped or resized)")
                v.px_per_m = round(bar_ppm, 4)
        elif bar_ppm:
            v.px_per_m = round(bar_ppm, 4)
            v.notes.append(f"scale from the graphic scale bar: {bar_ppm:.2f} px/m on this image")
        elif note_ppm:
            v.px_per_m = round(note_ppm, 4)
            v.notes.append(f"printed 1:{v.ratio:g} for a {paper[1]}, the image taken as the whole sheet "
                           f"({max(W, H) / paper[0]:.0f} px/in): {note_ppm:.2f} px/m (no scale bar to check it)")
        elif v.ratio:
            v.notes.append(f"printed 1:{v.ratio:g}, but the image's px per inch is unknown (no scale bar, no paper "
                           f"size): measure a printed dimension to fix the scale")


def _apply_bars(out: list[View], cands, lines) -> None:
    """Scale bars vs printed notes: the bar's 1:N divided by the printed 1:N nearest to it = the print factor of the
    page. ~1 -> the printed scales hold ('scale bar agrees'); another value (a sheet printed smaller / larger without
    a paper-size note) -> every view's page_ratio is rescaled by it."""
    bars = scale_bars(lines)
    if not bars or not out:
        return
    # pair each bar with ONE view (F3 20:49: the last bar used to win for every view sharing a printed ratio): the view
    # whose title is nearest the bar; if the bar disagrees with that view's scale but matches another view's printed
    # scale (within 5 %), that view; per view the closest bar is kept
    vt = [(v, tl) for v, (tl, _k) in zip(out, cands) if v.kind in DRAWING_KINDS and v.ratio]
    if not vt:
        return
    factors, best = [], {}
    for b in bars:
        bx, by = (b["box"][0] + b["box"][2]) / 2, (b["box"][1] + b["box"][3]) / 2
        dist = lambda x: math.hypot(x[1].cx - bx, x[1].cy - by)  # noqa: E731
        v, tl = min(vt, key=dist)
        if abs(b["ratio"] / v.ratio - 1) > 0.05:
            alt = [x for x in vt if abs(b["ratio"] / x[0].ratio - 1) <= 0.05]
            if alt:
                v, tl = min(alt, key=dist)
        d = dist((v, tl))
        if id(v) not in best or d < best[id(v)][0]:
            best[id(v)] = (d, b["ratio"])
        factors.append(b["ratio"] / v.ratio)
    for v, _tl in vt:
        if id(v) in best:
            v.bar_ratio = best[id(v)][1]
    if not factors:
        return
    factors.sort()
    f = factors[len(factors) // 2]
    distinct = {v.ratio for v in out if v.kind in DRAWING_KINDS and v.ratio}
    # a print factor rescales the page only when it is unambiguous: one printed scale on the page (or every bar
    # agrees) and the page is SMALLER than designed (f > 1; half-size prints are common, larger prints are not)
    sure = (len(distinct) == 1 or (len(factors) >= 2 and factors[-1] / factors[0] < 1.05)) and f > 1
    for v in out:
        if v.kind not in DRAWING_KINDS or not v.ratio:
            continue
        # label centres are only ~2 % accurate: the bar CHECKS the printed scale; it rescales only on a clear print
        # factor (a 22x34 sheet printed on 11x17 = 2, A1 on A3 = 2, A1 on A2 = sqrt 2)
        fv = v.bar_ratio / v.ratio if v.bar_ratio else f      # the view's own bar, else the page's factor
        if abs(fv - 1) <= 0.05 and (v.bar_ratio or len(distinct) == 1):
            v.notes.append(f"scale bar agrees with the printed scale (bar 1:{(v.bar_ratio or v.ratio * f):g})")
            v.confidence = round(min(0.97, v.confidence + 0.02), 2)
        elif sure and abs(f - 1) >= 0.10 and f < 5 and abs(_snap_loose(f) - f) / f < 0.05:
            fs = _snap_loose(f)
            base = v.page_ratio if v.page_ratio and abs(v.page_ratio / v.ratio - fs) / fs < 0.05 else v.ratio
            v.page_ratio = round(v.ratio * fs, 2)
            if base == v.ratio:
                v.notes.append(f"scale bar measures {f:.2f}x the printed scale: the page is printed at another size, "
                               f"1:{v.page_ratio:g} on this page")
        elif v.bar_ratio:
            v.notes.append(f"scale bar (1:{v.bar_ratio:g}) differs from the printed scale by {abs(fv - 1):.0%}: check")


# ----------------------------------------------------------------------------------------------------------------------
# question -> views

_ASK = [  # (asked kind, question regex, view kinds that answer it)
    ("rcp", re.compile(r"\bceiling|\bRCP\b|\breflected|\blight(?:ing)?\s+fixtures?|\bluminaires?|\bsoffit", re.I),
     ("rcp",)),
    ("elevation", re.compile(r"\b(?:north|south|east|west|front|rear|side|exterior|interior)\s+elevations?\b|"
                                         r"\belevation\s+(?:view|drawing|(?-i:[A-Z0-9])\b)|\b(?:on|in)\s+the\s+elevations?\b|"
                                         r"\bfa[cç]ades?\b|\bjulkisivu", re.I), ("elevation",)),
    ("section", re.compile(r"\bcross[- ]?sections?\b|\b(?:building|wall|longitudinal|transverse)\s+sections?\b|"
                                       r"\bsection\s+(?:view|drawing|(?-i:[A-Z0-9]{1,2})(?:\s*[-–]\s*(?-i:[A-Z0-9]{1,2}))?\b)|"
                                       r"\b(?:on|in)\s+the\s+sections?\b|\bleikkaus", re.I), ("section",)),
    ("axon", re.compile(r"\baxon|\bisometric|\b3d\b|\bperspective", re.I), ("axon",)),
    ("key_plan", re.compile(r"\bkey\s*plan", re.I), ("key_plan",)),
    ("roof_plan", re.compile(r"\broof\b", re.I), ("roof_plan",)),
    ("site_plan", re.compile(r"\bsite\b|\bparking\b|\bplot\b|\blandscap|\bmaster\s*plan", re.I), ("site_plan",)),
    ("foundation_plan", re.compile(r"\bfoundations?\b|\bslab\b|\bfootings?\b|\bpiles?\b|\bframing\b", re.I),
     ("foundation_plan",)),
    ("services_plan", re.compile(r"\bpip(?:e|es|ing)\b|\bpumps?\b|\bvalves?\b|\bducts?\b|\bsprinkler|\bplumbing|"
                                 r"\boutlets?\b|\bsockets?\b|\bfuel\b|\bdrains?\b", re.I), ("services_plan",)),
]
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5,
        "5th": 5, "ground": 0, "groud": 0, "grnd": 0, "basement": -1, "upper": 99}
_KEYED = re.compile(r"\b(module|level|floor|storey|story|building|bldg|unit|wing|block|zone|area|phase|house|apartment|"
                    r"flat|kerros|taso)\s*[-#:]?\s*0*(\d{1,3}|[A-H])\b", re.I)
_NUM_KEYED = re.compile(r"\b0*(\d{1,3}|[A-H])\s*[-–]?\s*(module|level|floor|storey|building|wing|block)\b", re.I)
_STOREY = re.compile(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|ground|groud|grnd|basement|upper)\s+"
                     r"(?:floor|level|storey|story)\b", re.I)
_STOP = set("""a an the of in on at to for and or is are be by with what how many much does do have has there this that
which whose total combined all every each area areas floor plan plans sheet drawing room rooms size big large square
feet foot metres meters sf m2 ft2 number count counts door doors window windows its it their them combined overall
long wide width length perimeter entire whole building module level storey story shown include including
private one two three four five six seven eight nine ten""".split())


_WHOLE = re.compile(r"\bwhole\b|\bentire\b|\ball\s+(?:the\s+)?(?:floors|storeys|stories|levels|buildings|sheets)\b|"
                    r"\b(?:in|of)\s+(?:the\s+)?project\b|\bkoko\b|\bgesamte?n?\b|\bhela\b", re.I)


def _keys(text: str) -> dict:
    out = {}
    for m in _KEYED.finditer(text):
        out[m.group(1).lower().replace("story", "storey").replace("bldg", "building")] = m.group(2).upper().lstrip("0") or "0"
    for m in _NUM_KEYED.finditer(text):
        out.setdefault(m.group(2).lower(), m.group(1).upper().lstrip("0") or "0")
    for m in _STOREY.finditer(text):
        out["floor"] = str(_ORD[m.group(1).lower()])
    if "level" in out and "floor" not in out:
        out["floor"] = out["level"]
    if "floor" in out and "level" not in out:
        out["level"] = out["floor"]
    return out


def _tail_keys(subject: str, qk: dict, vk: dict) -> None:
    """OCR tile cuts drop the start of a title ('MODULE 01-…' read as 'E 01-…'): when the question names a keyed part
    ('module 01') and the title starts with a tail of that keyword followed by a number, read it as that key."""
    m = re.match(r"^\s*([A-Za-z]{1,6})\s*[-#:]?\s*0*(\d{1,3})\b", subject or "")
    if not m:
        return
    frag = m.group(1).lower()
    for k in qk:
        if k not in vk and k.endswith(frag) and frag != k:
            vk[k] = m.group(2).lstrip("0") or "0"


def _words(text: str) -> set:
    ws = {w.lower() for w in re.findall(r"[^\W\d_]{3,}", text)}
    return {w.rstrip("s") for w in ws if w not in _STOP}


def pick_views(question: str, vs: list[View]) -> Pick:
    """The view(s) of the sheet the question means. Pick([]) = no scoping (single view / nothing matches)."""
    drawings = [v for v in vs if v.kind in DRAWING_KINDS]
    if not drawings:
        return Pick([], "no titled drawing views on the sheet", 0.0)
    m = _CHOSEN.search(question)  # a re-ask from a clarification chip: '... (view: MODULE 02-ADMINISTRATION - FLOOR PLAN)'
    if m:
        want = _norm_title(m.group(1))
        chosen = [v for v in drawings if _norm_title(v.title) == want]
        if chosen:
            return Pick(chosen, f"the view the user chose: {chosen[0].title}", 0.95, chosen[0].kind)
        question = question[:m.start()] + question[m.end():]
    asked, kinds = "plan", None
    for name, rx, ks in _ASK:
        if rx.search(question) and any(v.kind in ks for v in drawings):
            asked, kinds = name, ks
            break
    if kinds is None:
        plans = [v for v in drawings if v.kind in ("floor_plan", "drawing")]
        if not plans:
            plans = [v for v in drawings if v.kind in PLAN_KINDS]
        cands = plans
    else:
        cands = [v for v in drawings if v.kind in kinds]
    if not cands:
        return Pick([], "no plan view among the titled views", 0.0, asked)
    qk, qw = _keys(question), _words(question)
    # 'the whole building / project, all floors': the asked object's name ('toilets') is not a part to scope to
    whole = bool(_WHOLE.search(question))
    storey_note = ""
    floors = [_keys(v.subject + " " + v.title).get("floor") for v in cands]
    nums = sorted({int(f) for f in floors if f and f.lstrip("-").isdigit() and f != "99"})
    if qk.get("floor") == "0" and nums and 0 not in nums and nums[0] == 1:
        # US numbering: no GROUND / LEVEL 0 plan on the sheet, FIRST FLOOR is the ground floor
        qk["floor"] = qk["level"] = "1"
        storey_note = " (no ground-floor title: FIRST FLOOR read as the ground floor, US numbering)"
    elif qk.get("floor") == "99" and len(nums) >= 2:
        qk["floor"] = qk["level"] = str(nums[-1])
        storey_note = f" ('upper floor' read as the highest plan on the sheet, floor {nums[-1]})"
    scored = []
    for v in cands:
        vk, vw = _keys(v.subject + " " + v.title), _words(v.subject)
        _tail_keys(v.subject, qk, vk)
        clash = any(k in vk and vk[k] != val for k, val in qk.items())
        hit = sum(1 for k, val in qk.items() if vk.get(k) == val)
        words = 0 if whole else len(qw & vw)
        scored.append((v, clash, 2 * hit + words))
    named = [(v, s) for v, clash, s in scored if not clash and s > 0]
    if named:
        top = max(s for _, s in named)
        chosen = [v for v, s in named if s == top]
        why = ("the question names " + ", ".join(sorted({v.subject for v in chosen})) if chosen else "") + storey_note
        conf = (0.85 if len(chosen) == 1 else 0.7) - (0.15 if storey_note else 0.0)
        return Pick(chosen, why, conf, asked)
    unclashed = [v for v, clash, _ in scored if not clash]
    if qk and not unclashed:
        return Pick([], "the question names a part that no view title names", 0.2, asked)
    chosen = unclashed or cands
    # the rest of the sheet's views of the same kind = the whole floor area on this sheet
    return Pick(chosen, f"all {asked.replace('_', ' ')} views on the sheet (no part named)",
                0.75 if len(chosen) < len(drawings) else 0.5, asked)


_CHOSEN = re.compile(r"\(\s*view\s*:\s*([^()]{2,120})\)", re.I)
_PER_VIEW_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b|\b(?:this|the)\s+(?:plan|sheet|drawing|floor|level|"
                         r"view)\b|\bon\s+(?:the\s+)?plan\b", re.I)
_KIND_SAY = {"plan": "plan", "rcp": "ceiling plan", "axon": "axonometric view", "key_plan": "key plan",
             "services_plan": "services plan", "foundation_plan": "foundation plan", "roof_plan": "roof plan",
             "site_plan": "site plan"}
_ALL_ASKED = re.compile(r"\b(?:all|total|every|combined|both|altogether|overall|whole|entire|each)\b", re.I)


def _norm_title(t: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", t.upper()).strip()


def view_choices(path: str, question: str, page: int = 0, layer=None, max_options: int = 5) -> dict | None:
    """Interactive clarification (captain 18:50, W5's Answer.clarify shape): when the question does not say WHICH view
    and the sheet has several of the asked kind (A-101: module 01 / module 02 plans), or the view was read under an
    assumption ('ground floor' -> FIRST FLOOR), or the part it names is on no title -> {ask, why, answered_for,
    options: [{label, question, value: None, unit: None, kind, evidence: {file, page, bbox, bbox_space, note}}],
    click: None}. Each option's `question` re-asks in plain words ('… in Module 02 Administration?') when they pick exactly that view, else with '(view: TITLE)', which pick_views honours exactly. None when
    the question is unambiguous ('in total', 'both', a named module) or the sheet has one such view."""
    vs = views(path, page, layer=layer)
    p = pick_views(question, vs)
    drawings = [v for v in vs if v.kind in DRAWING_KINDS and v.bbox]
    if _CHOSEN.search(question) or len(drawings) < 2:
        return None
    # a named room / object ('How big is the gym?') is found by its own label in one view: no need to ask; counts and
    # 'this plan / sheet' questions add up over views, so the user may well mean one of them
    if not _PER_VIEW_Q.search(question) and p.views:
        return None
    kinds = {k for name, _rx, ks in _ASK if name == p.asked_kind for k in ks} or {"floor_plan", "drawing"}
    cands = [v for v in drawings if v.kind in kinds] or [v for v in drawings if v.kind in PLAN_KINDS]
    if len({_norm_title(v.title) for v in cands}) < 2 and p.views:
        return None
    if p.reason.startswith("all ") and not _ALL_ASKED.search(question):
        why = f"the question names no view; answered for all {len(p.views)} {p.asked_kind.replace('_', ' ')} views"
    elif "read as" in p.reason:  # 'ground floor' -> FIRST FLOOR (US numbering), 'upper floor' -> highest
        why = p.reason.split("(", 1)[-1].rstrip(")") if "(" in p.reason else p.reason
    elif not p.views and "names a part that no view title names" in p.reason:
        why = "no view title names the part the question asks about"
    elif len(p.views) >= 2 and p.reason.startswith("the question names") and not _ALL_ASKED.search(question):
        why = "several views match the question's words"
    else:
        return None
    if len(cands) < 2:
        return None
    image = not str(path).lower().endswith(".pdf")
    base = question.strip().rstrip("?").strip()
    opts = []
    for v in cands[:max_options]:
        # the re-ask in plain words ('... in Module 02 Administration?') when that alone picks exactly this view: other
        # readers match view titles by words too (DR's openings read '(view: … FLOOR PLAN)' as a room name and
        # abstained); else the exact '(view: TITLE)' marker, which only stoa.sheet understands
        ask = f"{base} (view: {v.title})?"
        subj = v.subject.strip()
        if subj and len(subj) >= 3:
            plain = f"{base} in {subj.title() if subj.isupper() else subj}?"
            got = pick_views(plain, vs).views
            if len(got) == 1 and got[0] is v:
                ask = plain
        opts.append({"label": v.title if v.kind != "drawing" or not v.title.startswith("(") else
                     "the sheet's main drawing (untitled)", "value": None, "unit": None, "kind": v.kind,
                     "question": ask,
                     "evidence": {"file": str(path), "page": int(page), "bbox": [round(float(c), 2) for c in v.bbox],
                                  "bbox_space": "image_px" if image else "pdf_pt", "note": f"view: {v.title}"}})
    what = _KIND_SAY.get(p.asked_kind, p.asked_kind.replace("_", " "))
    return {"ask": f"Which {what} do you mean?", "why": why, "answered_for": [v.title for v in p.views],
            "options": opts, "click": None}


def attach_view_choices(ans, path: str, question: str, page: int = 0, layer=None):
    """Adds view_choices() to an Answer the way G1's stoa.agent.vision.set_clarify does (options' evidence = indices
    into ans.evidence; Answer.clarify is set only when the contract has the field; one 'clarify:' trace line always).
    Leaves the answer unchanged when it already carries a clarification or the views are unambiguous."""
    try:
        if getattr(ans, "clarify", None) or any(str(t).startswith("clarify:") for t in (ans.trace or [])):
            return ans
        c = view_choices(path, question, page, layer=layer)
        if not c:
            return ans
        from stoa.contract import Evidence

        n = len(ans.evidence or [])
        ans.evidence = [*(ans.evidence or []), *[Evidence(**o["evidence"]) for o in c["options"]]]
        for i, o in enumerate(c["options"]):
            o["evidence"] = [n + i]
        if "clarify" in type(ans).model_fields:
            ans.clarify = c
        ans.trace = [*(ans.trace or []), f"clarify: {c['ask']} ({c['why']}; options: "
                                         + " | ".join(o["label"] for o in c["options"]) + ")"]
    except Exception:  # noqa: BLE001 - a clarification must never break an answer
        pass
    return ans


_WING_Q = re.compile(r"\b(?:north|south|east|west)(?:ern)?(?:[\s-]?(?:east|west)(?:ern)?)?[\s-]+(?:wing|block|arm|leg)\b|"
                     r"\b(?:central|centre|center|middle)\s+(?:core|block|part|section)\b", re.I)
_FACADE_Q = re.compile(r"\b(?:facade|façade|elevation|face|side|frontage|exterior wall|outside wall)s?\b", re.I)


def _wing_scope(path: str, question: str, page: int, layer, vs) -> Scope | None:
    """'the north wing' on a sheet with a KEY PLAN (stoa.sheet.keyplan): the main plan's part the key plan's block on
    that side maps to, as one synthetic plan view. Facade / side questions are not wings (G1's facade reader)."""
    if not _WING_Q.search(question or "") or _FACADE_Q.search(question or "") or \
            not any(v.kind == "key_plan" for v in vs):
        return None
    try:
        from stoa.sheet.keyplan import zone_for

        z = zone_for(path, question, page, layer)
    except Exception:  # noqa: BLE001
        return None
    if not z or not z.get("plan_box") or z.get("confidence", 0) < 0.55:
        return None                    # 0.4 = the key plan's shape differs from the plan's (an enlarged part): no map
    plans = [v for v in vs if v.kind in PLAN_KINDS and v.bbox]
    base = max(plans, key=lambda v: (v.bbox[2] - v.bbox[0]) * (v.bbox[3] - v.bbox[1])) if plans else None
    v = View(kind="floor_plan", title=f"{z['name'].upper()} (key plan)", bbox=tuple(z["plan_box"]),
             title_bbox=tuple(z["key_box"]), scale_text=base.scale_text if base else None,
             ratio=base.ratio if base else None, page_ratio=base.page_ratio if base else None,
             subject=z["name"].upper(), confidence=z.get("confidence", 0.5),
             px_per_m=base.px_per_m if base else None)
    return Scope([v], z["why"], z.get("confidence", 0.5), list(vs))


def scope_for(path: str, question: str, page: int = 0, min_views: int = 2, layer=None) -> Scope | None:
    """The hook for readers: the views the question means on this page, or None when there is nothing to narrow
    (fewer than `min_views` titled drawing views, or the pick keeps every drawing view). Images need their text
    layer (`layer`, image px boxes); the scope's boxes are then in the layer's image px.

    Use it for what is DRAWN (room tags, door swings, traced rooms, symbols). Never filter schedule / table / notes
    readers by it: area tables and 'ROOM COUNT' tables sit outside the plan views and answer whole-floor questions."""
    vs = views(path, page, layer=layer)
    zs = _wing_scope(path, question, page, layer, vs)
    if zs is not None:
        return zs
    drawings = [v for v in vs if v.kind in DRAWING_KINDS]
    if len(drawings) < min_views:
        return None
    p = pick_views(question, vs)
    if not p.views or not any(v.bbox for v in p.views):
        return None
    if len(p.views) == len(drawings):
        return None
    return Scope([v for v in p.views if v.bbox], p.reason, p.confidence, list(vs))
