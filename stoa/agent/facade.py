"""Facade-scoped window / door counts on a vector plan sheet (captain 20:47, Ziying's hand gold dfe-08: 'How many windows
are on the west facade of the classroom wing?' = 18; we answered 0 from a CubiCasa5K reading of a 150-dpi render).

What it does, in the expert's own steps (human/questions.jsonl h-5939f2951c86):
1. SIDE: the compass word of the question ('west facade', 'north side', 'windows facing east', 'left side'). North comes
   from a north arrow when one is printed (a lone 'N' next to a small symbol: the arrow points from the symbol's centre
   to the letter), else the top of the sheet is north - and the answer says so in its assumptions.
2. SCOPE: the wing / block the question names = the room labels with those words ('classroom wing' -> every label with
   'Classroom'); no name -> the whole building. The building footprint is the plan ink closed over door-sized gaps and
   flooded from the paper edge; from the labels' middle the scan goes outward (west = toward -x when top = north) to
   the paper -> the outer face of the wall on each row. The facade = the run of rows whose outer face stays within
   ~1 m of the wing's own (it ends where the wall steps out: a walkway, another block).
3. COUNT: that wall strip is re-rendered at ~400 dpi (Ziying's addendum: at 150 dpi a 1.3 m window is ~38 px and its
   glazing lines 1-2 px) and walked along its length. Each row across the wall band is SOLID (pier: mostly ink),
   GLAZED (>= 2 thin lines with paper between: the glazing), or OPEN (no ink: a doorway). A window = one glazed run
   between piers / doorways (two windows split by a solid pier >= 0.15 m count twice; a 1-px frame line does not
   split); a door = an open run 0.6-3.0 m (louvred / zig-zag leaves outside the wall band leave it open, as the
   expert counts them: doors, not windows).
Evidence: one box per counted window (or door) on the PDF page (pdf_pt, unrotated), plus the searched strip.
$0, no model, ~1-3 s. Abstains (None) when the question has no side, the sheet is not a vector PDF, or the strip
cannot be found; the caller goes on with its other readers."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

_SIDES = {"north": (0.0, -1.0), "south": (0.0, 1.0), "east": (1.0, 0.0), "west": (-1.0, 0.0)}
_SIDE_WORDS = {
    "north": r"north(?:ern)?|pohjois\w*|nord\w*|norr\w*",
    "south": r"south(?:ern)?|etelä\w*|etela\w*|süd\w*|sud\w*|söder\w*|syd\w*",
    "east": r"east(?:ern)?|itä\w*|ita\w*|ost\w*|öst\w*|est",
    "west": r"west(?:ern)?|länsi\w*|lansi\w*|läns\w*|väst\w*|vast\w*|ouest",
}
_SHEET_SIDES = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "top": (0.0, -1.0), "bottom": (0.0, 1.0)}
_FACE = r"(?:facades?|façades?|fassade\w*|julkisivu\w*|fasad\w*|elevations?|sides?|walls?|exterior walls?|outer walls?|" \
        r"external walls?|frontage|face|edge)"
_KIND = re.compile(r"\b(windows?|glazing|ikkun\w*|fenster\w*|fönster\w*|doors?|doorways?|exits?|ov(?:i|e)\w*|"
                   r"türen?|tür|dörr\w*)\b", re.I)
_WING = re.compile(r"((?:[a-zà-ÿ][\w'-]*\s+){1,3})(wing|block|building|module|house|extension|annexe?|range)\b", re.I)
_NOT_NAME = re.compile(r"^(?:of|on|in|at|along|for|to|by|the|a|an|is|are|there|facades?|façades?|sides?|walls?|"
                       r"elevations?|exterior|external|outer|facing|north\w*|south\w*|east\w*|west\w*|left|right|top|"
                       r"bottom|how|many|windows?|doors?)$", re.I)
_STOP = {"the", "a", "an", "main", "whole", "entire", "this", "that", "new", "old", "school", "plan"}


@dataclass
class Ask:
    side: str                      # 'west' / 'left' …
    vec: tuple[float, float]       # outward direction in SHEET terms (x right, y down) before north is applied
    by_compass: bool               # True = compass word (needs north); False = sheet side ('left side')
    kind: str                      # 'window' | 'door'
    wing: list[str] = field(default_factory=list)  # name words of the wing / block ('classroom'); [] = the building
    wing_text: str = ""


def parse(question: str) -> Ask | None:
    """The facade scope of a count question, or None. 'How many windows are on the west facade of the classroom wing?'
    -> Ask('west', (-1, 0), True, 'window', ['classroom'])."""
    q = question.strip()
    k = _KIND.search(q)
    if not k or not re.search(r"\bhow many\b|\bnumber of\b|\bcount\b|\bkuinka monta\b|\bwie viele\b|\bhur många\b",
                              q, re.I):
        return None
    kind = "window" if re.match(r"(window|glazing|ikkun|fenster|fönster)", k.group(1), re.I) else "door"
    side = None
    for name, pat in _SIDE_WORDS.items():
        m = re.search(rf"\b(?:{pat})(?:[- ]?(?:facing|side|most))?\s+{_FACE}\b|\bfacing\s+(?:the\s+)?(?:{pat})\b|"
                      rf"\b{_FACE}\s+(?:facing|on the|to the)\s+(?:{pat})\b|\b(?:{pat})[- ]facing\b|"
                      rf"\b(?:face|faces|facing|look|looks|looking|point|points|pointing|open|opens)\s+(?:to(?:wards?)?\s+)?"
                      rf"(?:the\s+)?(?:{pat})\b|\bon the (?:{pat})\b", q, re.I)
        if m:
            side = (name, _SIDES[name], True)
            break
    if side is None:
        for name, vec in _SHEET_SIDES.items():
            if re.search(rf"\b{name}(?:[- ]hand)?\s+{_FACE}\b|\bon the {name}\b", q, re.I):
                side = (name, vec, False)
                break
    if side is None:
        return None
    wing, text = [], ""
    m = _WING.search(q)
    if not m:
        # DR 22:12: 'the north facade of the Multi-Purpose Hall' named no wing word -> the whole building was measured
        # (6 = right by luck): the room / part named after 'facade / side of (the)' is the scope
        r = re.search(rf"\b(?:{_FACE})\s+of\s+(?:the\s+)?((?:[A-Za-z][\w'-]*\s*){{1,4}}?)\s*(?:\?|$|,|\bon\b|\bin\b)", q,
                      re.I)
        if r and not re.fullmatch(r"(?i)(?:building|school|house|block|plan|sheet|drawing|whole building|"
                                  r"entire building)", r.group(1).strip()):
            m = re.match(r"((?:\S+\s+){0,3})(\S+)", r.group(1).strip() + " ")
            room = m.group(2).lower().strip("'") if m else ""
    else:
        room = ""
    if m:
        words = m.group(1).split()
        keep: list[str] = []
        for w in reversed(words):  # the words right before 'wing', up to the first preposition / side word
            if _NOT_NAME.match(w):
                break
            keep.insert(0, w)
        text = " ".join([*keep, m.group(2)])
        names = [w.lower().strip("'") for w in keep if w.lower() not in _STOP and len(w) > 2]
        wing = [re.sub(r"'s$", "", w) for w in names]
        wing = [w[:-1] if w.endswith("s") and len(w) > 4 else w for w in wing]
        if room and room not in _STOP and len(room) > 2:
            wing.append(room)  # a room's own name: its last word ('Hall') is part of the label, not a 'wing' noun
    return Ask(side[0], side[1], side[2], kind, wing, text)


# ---------------------------------------------------------------------------------------------------- north arrow

def north_vector(page) -> tuple[tuple[float, float], str]:
    """(north as a unit vector in DISPLAY coordinates (page /Rotate applied, y down), how it was found).
    A north arrow = a whole printed line 'N' / 'NORTH' / 'PLAN NORTH' / 'TRUE NORTH' with a compact drawn symbol
    (curves or a fill, 8-60 pt) within 50 pt: the arrow is taken to point from the symbol's centre to the label
    (labels sit at the arrow's tip by convention), snapped to the sheet axes within 15°. No arrow -> the top of the
    sheet is north, and the answer says so."""
    import pymupdf

    M = page.rotation_matrix
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = " ".join(sp["text"] for sp in ln["spans"]).strip().upper()
            if t:
                r = pymupdf.Rect(ln["bbox"]) * M
                lines.append((r.x0, r.y0, r.x1, r.y1, t))
    # a north label is a whole printed LINE: words would catch 'NORTH WING DORMITORY'
    labels = [w for w in lines if re.fullmatch(r"(?:PLAN |TRUE |PROJECT )?(?:N|NORTH)\.?|PN|TN", w[4])]
    if labels:
        try:
            draws = page.get_drawings()
        except Exception:  # noqa: BLE001
            draws = []
        syms, segs = [], []
        for d in draws:
            r = d["rect"] * M
            fill = d.get("fill")
            dark = fill is not None and sum(fill) < 2.4  # white text backgrounds are not symbols
            kinds = {it[0] for it in d["items"]}
            if 8 <= max(r.width, r.height) <= 60 and ("c" in kinds or (dark and kinds != {"re"})):
                syms.append(r)
            for it in d["items"]:
                if it[0] == "l":
                    segs.append((it[1] * M, it[2] * M))
        best = None
        for w in labels:
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            for r in syms:
                sx, sy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                dist = math.hypot(sx - cx, sy - cy)
                if dist < 50 and dist >= 3 and (best is None or dist < best[0]):
                    best = (dist, (cx - sx) / dist, (cy - sy) / dist, w[4], r)
        if best is not None:
            _, vx, vy, txt, r = best
            # a needle: a straight line from the symbol's centre to its rim -> the rim end is the tip
            sx, sy, rad = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, max(r.width, r.height) / 2
            for a, b in segs:
                for p0, p1 in ((a, b), (b, a)):
                    if math.hypot(p0.x - sx, p0.y - sy) < 0.3 * rad and \
                            0.7 * rad < math.hypot(p1.x - sx, p1.y - sy) < 1.3 * rad:
                        L = math.hypot(p1.x - p0.x, p1.y - p0.y)
                        vx, vy = (p1.x - p0.x) / L, (p1.y - p0.y) / L
                        break
                else:
                    continue
                break
            ang = math.degrees(math.atan2(vx, -vy))
            snap = round(ang / 90) * 90
            if abs(ang - snap) <= 15:
                ang = snap
                vx, vy = round(math.sin(math.radians(ang)), 6), round(-math.cos(math.radians(ang)), 6)
            return (vx, vy), (f"north arrow ('{txt}' printed next to a symbol): north is {ang:+.0f}° from the top "
                              f"of the sheet")
    return (0.0, -1.0), "no north arrow on the sheet: the top of the sheet is taken as north"


def outward(ask: Ask, north: tuple[float, float]) -> tuple[float, float]:
    """The facade's outward direction in display coordinates (x right, y down)."""
    if ask.by_compass:
        nx, ny = north
        east = (-ny, nx)
        return {"north": (nx, ny), "south": (-nx, -ny), "east": east, "west": (-east[0], -east[1])}[ask.side]
    return {"top": (0.0, -1.0), "bottom": (0.0, 1.0), "right": (1.0, 0.0), "left": (-1.0, 0.0)}[ask.side]


# ---------------------------------------------------------------------------------------------------- geometry

def _ink(page, dpi: float, clip=None) -> np.ndarray:
    """Gray render in display space (page /Rotate applied; clip in display coordinates)."""
    import pymupdf

    z = dpi / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, colorspace="gray", alpha=False)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return a


def _footprint(gray: np.ndarray, close_px: int) -> np.ndarray:
    """Building footprint: dark ink closed over door-sized gaps, holes filled = everything the paper flood from the
    border does not reach."""
    import cv2

    ink = (gray < 100).astype(np.uint8)  # black wall ink; grey canopy / grid dashes and hairlines drop out
    k = max(3, close_px | 1)
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    h, w = closed.shape
    pad = np.zeros((h + 2, w + 2), np.uint8)
    pad[1:-1, 1:-1] = closed
    mask = np.zeros((h + 4, w + 4), np.uint8)
    flood = pad.copy()
    cv2.floodFill(flood, mask, (0, 0), 2)
    # a drawn sheet frame closes the paper inside it (O1 / F6 PITFALL 20:12 / 20:50): seed the flood on a ring 3 % in
    for f in (0.03, 0.06):
        dy, dx = int(f * h), int(f * w)
        ring = [(x, dy) for x in range(dx, w - dx, 15)] + [(x, h - dy) for x in range(dx, w - dx, 15)] + \
               [(dx, y) for y in range(dy, h - dy, 15)] + [(w - dx, y) for y in range(dy, h - dy, 15)]
        for x, y in ring:
            if 0 <= y + 1 < h + 2 and 0 <= x + 1 < w + 2 and flood[y + 1, x + 1] == 0:
                cv2.floodFill(flood, mask, (x + 1, y + 1), 2)
    return (flood[1:-1, 1:-1] != 2)


def _rot(a: np.ndarray, d: tuple[float, float]) -> tuple[np.ndarray, int]:
    """Rotate so that the outward direction d points to -x (left). Returns (array, k) with np.rot90 count k."""
    ax = max(((-1, 0), (1, 0), (0, -1), (0, 1)), key=lambda v: v[0] * d[0] + v[1] * d[1])
    k = {(-1, 0): 0, (0, -1): 1, (1, 0): 2, (0, 1): 3}[ax]
    # np.rot90 rotates counter-clockwise (in image coords, y down: a +y vector becomes +x after one turn)
    return np.rot90(a, k), k


def rot_fwd(x: float, y: float, k: int, H: int, W: int) -> tuple[float, float]:
    """(col x, row y) of an H x W array -> the same point after np.rot90(a, k) (counter-clockwise turns)."""
    h, w = H, W
    for _ in range(k % 4):
        x, y = y, w - 1 - x
        h, w = w, h
    return x, y


def rot_back(x: float, y: float, k: int, H: int, W: int) -> tuple[float, float]:
    """Inverse of rot_fwd: a point of np.rot90(a, k) (a: H x W) -> (col, row) in a."""
    h, w = (H, W) if k % 2 == 0 else (W, H)
    for _ in range(k % 4):
        x, y = h - 1 - y, x
        h, w = w, h
    return x, y


@dataclass
class Run:
    kind: str     # 'window' | 'door' | 'pier'
    a: float      # start along the wall (pt, page coords along the wall axis)
    b: float


def _runs(strip: np.ndarray, px_per_m: float) -> tuple[list[Run], dict]:
    """strip: gray array oriented so rows run ALONG the wall, the outer face toward column 0 -> window / door runs.
    Row classes across the wall band: 2 solid (pier, >= 60 % ink), 1 glazed (>= 2 thin ink runs, < 60 % ink),
    0 open (no ink), 3 other (one line: a sill, a door leaf / hinge, a frame end)."""
    ink = strip < 160
    rows, cols = ink.shape
    min_w = max(3, int(0.15 * px_per_m))
    firsts, widths = [], []
    for r in range(rows):
        nz = np.flatnonzero(ink[r])
        if nz.size == 0:
            continue
        c0 = int(nz[0])
        stop = np.flatnonzero(~ink[r, c0:])
        w = int(stop[0]) if stop.size else cols - c0
        if w >= min_w:
            firsts.append(c0)
            widths.append(w)
    if len(widths) < 3:
        return [], {"band": None}
    c0 = int(np.median(firsts))
    T = int(np.median(widths))
    lo, hi = max(0, c0 - 1), min(cols, c0 + T + 1)
    band = ink[:, lo:hi]
    frac = band.mean(axis=1)
    nrun = (np.diff(np.pad(band.astype(np.int8), ((0, 0), (1, 1))), axis=1) == 1).sum(axis=1)
    cls = np.where(frac >= 0.6, 2, np.where(nrun >= 2, 1, np.where(nrun == 0, 0, 3)))
    segs: list[list[int]] = []
    for r in range(rows):
        c = int(cls[r])
        if c == 3:
            c = 0  # one line across an opening (leaf / sill / hinge): an opening, not glazing
        if segs and segs[-1][0] == c:
            segs[-1][2] = r + 1
        else:
            segs.append([c, r, r + 1])
    # a short solid / open break inside glazing (a frame line, a mullion, an end stop < 0.15 m) joins the two parts
    min_pier = 0.15 * px_per_m
    out_segs: list[list[int]] = []
    for seg in segs:
        if len(out_segs) >= 2 and seg[0] == 1 and out_segs[-2][0] == 1 and \
                out_segs[-1][2] - out_segs[-1][1] < min_pier:
            out_segs.pop()
            out_segs[-1][2] = seg[2]
            continue
        out_segs.append(seg)
    out: list[Run] = []
    for c, s, e in out_segs:
        L = (e - s) / px_per_m
        if c == 1 and 0.3 <= L <= 6.0:
            out.append(Run("window", s, e))
        elif c == 0 and 0.6 <= L <= 3.0:
            out.append(Run("door", s, e))
    return out, {"band": (lo, hi), "thick_px": T, "classes": cls}


def answer(path: str, question: str, page_no: int = 0):
    """Answer | None."""
    ask = parse(question)
    if ask is None or not str(path).lower().endswith(".pdf"):
        return None
    import pymupdf

    from stoa.contract import Answer, Evidence

    try:
        doc = pymupdf.open(path)
        page = doc[page_no]
    except Exception:  # noqa: BLE001
        return None
    if len(page.get_drawings()) < 200:
        return None
    ratio = _ratio(path, page)
    if not ratio:
        return None
    pt_per_m = 72 / 0.0254 / ratio  # page points per real metre
    # everything below works in DISPLAY space (page /Rotate applied: renders come out that way); evidence boxes go
    # back to unrotated pdf_pt (W5 20:29: the space Evidence.bbox uses) at the end
    north, how_n = north_vector(page)
    try:  # VP 20:58: stoa.sheet.north_arrow (owner VP) wins when it found an arrow; mine is the fallback
        from stoa.sheet import north_arrow

        na = north_arrow(str(path), page_no)
        if na.get("found") and (na.get("sides") or {}).get("north") in ("top", "right", "bottom", "left"):
            north = {"top": (0.0, -1.0), "right": (1.0, 0.0), "bottom": (0.0, 1.0), "left": (-1.0, 0.0)}[na["sides"]["north"]]
            how_n = str(na.get("note") or f"north arrow (stoa.sheet.north_arrow): north = the {na['sides']['north']} of the sheet")
    except Exception:  # noqa: BLE001 — not merged yet / no arrow reader: keep mine
        pass
    d = outward(ask, north)
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = " ".join(s["text"] for s in ln["spans"]).strip()
            if t:
                lines.append((t, tuple(pymupdf.Rect(ln["bbox"]) * page.rotation_matrix)))
    if ask.wing:
        hits = [bb for t, bb in lines if all(re.search(rf"\b{re.escape(w)}", t, re.I) for w in ask.wing)]
        if not hits:  # 'classroom wing' where labels say 'Secondary' / 'Classroom' on two lines: any word
            hits = [bb for t, bb in lines if any(re.search(rf"\b{re.escape(w)}", t, re.I) for w in ask.wing)]
    else:
        hits = []
    # footprint at ~ 8 px per metre is enough to find the outer face; 1 m closing bridges doorways
    dpi = max(20.0, min(150.0, 72 * 12 / pt_per_m))
    z = dpi / 72
    gray = _ink(page, dpi)
    fp = _footprint(gray, int(round(2.0 * pt_per_m * z)))
    if not fp.any():
        return None
    if not hits:  # the whole building: every room label inside the footprint
        hits = [bb for t, bb in lines if 0 <= int((bb[1] + bb[3]) / 2 * z) < fp.shape[0] and
                0 <= int((bb[0] + bb[2]) / 2 * z) < fp.shape[1] and fp[int((bb[1] + bb[3]) / 2 * z),
                                                                        int((bb[0] + bb[2]) / 2 * z)]]
        if not hits:
            return None
    H, W = fp.shape
    fr, k = _rot(fp, d)
    # label points in the rotated frame
    pts = [rot_fwd((bb[0] + bb[2]) / 2 * z, (bb[1] + bb[3]) / 2 * z, k, H, W) for bb in hits]
    pts = [(x, y) for x, y in pts if 0 <= int(y) < fr.shape[0] and 0 <= int(x) < fr.shape[1] and fr[int(y), int(x)]]
    if not pts:
        return None
    ys = [p[1] for p in pts]
    xs = [p[0] for p in pts]
    x_out, x_far = int(min(xs)), int(max(xs))

    def face(row: int) -> int | None:
        """Outer face on this row: the footprint run that holds the outermost label column (or the first run to its
        far side within the wing) -> its outer end."""
        if row < 0 or row >= fr.shape[0]:
            return None
        line = fr[row]
        x0 = x_out
        if not line[x0]:
            nxt = np.flatnonzero(line[x0:x_far + 1])
            if nxt.size == 0:
                return None
            x0 = x0 + int(nxt[0])
        seg = line[:x0 + 1][::-1]
        gap = np.flatnonzero(~seg)
        return x0 - int(gap[0]) + 1 if gap.size else 0

    y0, y1 = int(min(ys)), int(max(ys))
    faces = {r: face(r) for r in range(y0, y1 + 1)}
    vals = [v for v in faces.values() if v is not None]
    if not vals:
        return None
    ref = int(np.median(vals))
    tol = max(2, int(round(1.0 * pt_per_m * z)))
    run_rows = [r for r, v in faces.items() if v is not None and abs(v - ref) <= tol]
    if not run_rows:
        return None
    straight = len(run_rows) / max(1, len(faces))  # share of the wing's rows whose outer face is THIS wall
    top_r, bot_r = min(run_rows), max(run_rows)
    for step, start in ((-1, top_r), (1, bot_r)):  # extend while the outer face stays the wing's own
        r, miss = start + step, 0
        while 0 <= r < fr.shape[0]:
            v = face(r)
            if v is not None and abs(v - ref) <= tol:
                if step < 0:
                    top_r = r
                else:
                    bot_r = r
                miss = 0
            else:
                miss += 1
                if miss > 2:
                    break
            r += step
    # the strip in rotated low-res coords: x in [ref - 0.6 m, ref + 1.2 m], rows [top_r, bot_r]
    pad_out, pad_in = int(round(0.6 * pt_per_m * z)), int(round(1.2 * pt_per_m * z))
    corners = [(ref - pad_out, top_r), (ref + pad_in, top_r), (ref - pad_out, bot_r + 1), (ref + pad_in, bot_r + 1)]
    orig = [rot_back(x, y, k, H, W) for x, y in corners]
    xs_o = [p[0] / z for p in orig]
    ys_o = [p[1] / z for p in orig]
    clip = pymupdf.Rect(min(xs_o), min(ys_o), max(xs_o), max(ys_o)) & page.rect  # display space
    if clip.is_empty or max(clip.width, clip.height) < 1.0 * pt_per_m:
        return None
    # hi-res strip: >= 400 dpi and >= 120 px per metre
    hdpi = max(400.0, 72 * 120 / pt_per_m)
    hdpi = min(hdpi, 72 * 20000 / max(clip.width, clip.height))
    hz = hdpi / 72
    sg = _ink(page, hdpi, clip)
    sr, _ = _rot(sg, d)
    px_per_m = pt_per_m * hz
    # rotated strip: rows run along the wall, outer face on the left. Make sure rows = along the wall:
    runs, info = _runs(sr, px_per_m)
    if info.get("band") is None:
        return None
    # boxes back to page points
    Hs, Ws = sg.shape
    lo, hi = info["band"]
    def box(rn: Run):
        ps = [rot_back(x, y, k, Hs, Ws) for x, y in ((lo, rn.a), (hi, rn.a), (lo, rn.b), (hi, rn.b))]
        xs_ = [clip.x0 + p[0] / hz for p in ps]
        ys_ = [clip.y0 + p[1] / hz for p in ps]
        r = pymupdf.Rect(min(xs_), min(ys_), max(xs_), max(ys_)) * page.derotation_matrix
        return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]
    wins = [r for r in runs if r.kind == "window"]
    doors = [r for r in runs if r.kind == "door"]
    want = wins if ask.kind == "window" else doors
    other = doors if ask.kind == "window" else wins
    side_txt = f"{ask.side} {'facade' if ask.by_compass else 'side'}" + (f" of the {ask.wing_text}" if ask.wing_text else
                                                                          " of the building")
    L = (bot_r - top_r + 1) / z / pt_per_m
    ev = [Evidence(file=str(path), page=page_no, bbox=box(r), bbox_space="pdf_pt", exact=True,
                   note=f"{ask.kind} {i + 1} on the {side_txt} ({(r.b - r.a) / px_per_m:.2f} m)")
          for i, r in enumerate(want)]
    ev.append(Evidence(file=str(path), page=page_no, bbox=[round(v, 2) for v in clip * page.derotation_matrix],
                       bbox_space="pdf_pt",
                       exact=False, note=f"searched area: the {side_txt} ({L:.1f} m of outer wall)"))
    sizes = sorted(round((r.b - r.a) / px_per_m, 2) for r in want)
    conf = 0.75 if want else 0.3
    if ask.kind == "window" and not want:
        conf = 0.2
    doubts = []
    if straight < 0.6:
        doubts.append(f"the {ask.side} side is not one straight wall along the {'wing' if ask.wing else 'building'} "
                      f"(stepped / L- or T-shaped outline: {100 * straight:.0f}% of it lies on the counted wall)")
    if any(w > 3.0 for w in sizes):
        doubts.append("glazed runs longer than 3 m (a curtain wall, or the wall drawing was misread): check the marks")
    band_m = info["thick_px"] / px_per_m
    if not 0.1 <= band_m <= 0.8:
        doubts.append(f"the wall band read {band_m:.2f} m thick: not a usual outer wall")
    if doubts:
        conf = min(conf, 0.35)
    a = Answer(value=len(want), unit="count", confidence=conf, evidence=ev, solver="facade",
               trace=[f"facade: '{side_txt}' -> {how_n}; outward = ({d[0]:+.2f}, {d[1]:+.2f}) on the page",
                      f"facade: wing = {len(hits)} label(s) matching {ask.wing or 'any room'}; outer wall run {L:.1f} m "
                      f"found on the footprint (openings closed at 2 m)",
                      f"facade: wall strip re-read at {hdpi:.0f} dpi ({px_per_m:.0f} px/m); band {info['thick_px'] / px_per_m:.2f} m "
                      f"thick; {len(wins)} glazed run(s) between piers, {len(doors)} doorway(s)",
                      f"facade: {ask.kind}s = {len(want)} (widths {sizes[:12]}{' …' if len(sizes) > 12 else ''} m)"],
               assumptions=[f"Includes: {ask.kind}s in the outer wall of the {side_txt} ({L:.1f} m)"
                            + (", each window between solid piers counted once" if ask.kind == "window" else
                               ", door SETS (a double door / louvred pair = 1 opening)"),
                            f"Excludes: interior {'glazing' if ask.kind == 'window' else 'doors'}, the other sides, "
                            f"and the {len(other)} {'door' if ask.kind == 'window' else 'window'}(s) in this wall",
                            how_n[0].upper() + how_n[1:],
                            f"the {side_txt} = the outer wall on that side, {L:.1f} m long; interior glazing not counted",
                            "window = a gap in the wall with thin glazing lines inside it; neighbouring windows split by a "
                            "solid pier (>= 0.15 m) count separately; door = an empty gap 0.6-3 m (leaves drawn outside "
                            "the wall, e.g. louvred doors, are doors)"],
               missing=doubts + ([] if want else [f"no {ask.kind} found on this wall: check the marked strip"]) +
                       [f"{len(other)} {'door' if ask.kind == 'window' else 'window'}(s) on the same wall, not counted"])
    return a


def _ratio(path: str, page) -> float | None:
    """The plan's printed scale denominator (1:200 -> 200), paper-size note applied when the shared reader has it."""
    try:
        from stoa.sheet import scope_for

        s = scope_for(path, "plan")
        if s is not None and getattr(s, "ratio", None):
            return float(s.ratio)
    except Exception:  # noqa: BLE001
        pass
    from collections import Counter

    txt = page.get_text()
    m = re.findall(r"\b1\s*:\s*(\d{2,4})\b", txt)
    if m:
        return float(Counter(m).most_common(1)[0][0])
    # US: 1" = 40'-0" -> 480; 1/8" = 1'-0" -> 96; 3/32" = 1'-0" -> 128
    imp = []
    for a, b, ft, inch in re.findall(r"(\d+)(?:\s*/\s*(\d+))?\s*\"\s*=\s*(\d+)\s*'\s*-?\s*(\d+)?", txt):
        paper = float(a) / float(b) if b else float(a)
        real = float(ft) * 12 + (float(inch) if inch else 0.0)
        if paper > 0 and real > 0:
            imp.append(round(real / paper, 3))
    if imp:
        return float(Counter(imp).most_common(1)[0][0])
    return None
