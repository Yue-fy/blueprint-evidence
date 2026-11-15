"""SLAB AREA BY TYPE on foundation / slab plans (E1, captain 16:15 item (d); Stoa kind 4; X1 stoa-like USACE S-100F).

A slab plan prints one CALLOUT per slab type inside the slab ('300 SLAB-ON-GRADE W/ D16 @ 300 TOP FACE …',
'150 SLAB-ON-GRADE W/ D13 @ 300'). The question names the type by thickness and/or bars; the matching callout is the
seed, and the slab is the region around it traced on the sheet's lines at the printed 1:N, where the lines that do
NOT bound a slab are left out:

  - control / construction joints (long lines within 20 pt of a 'CJ' label): they divide a slab into panels;
  - text frames around callouts / notes;
  - dimension lines: segments within 12 pt of a 3-5 digit number that have no same-pen parallel twin 0.1-0.45 m away
    (a twin = the other face of a wall / grade beam; dimension rows sit >= 0.5 m apart), and segments with an arrow
    head at both ends.

Area = the traced region (holes filled) + half a line width along its outline. Approximate (exact=False), conf 0.4;
abstains when the callout is not inside a closed region of 2-5000 m² or the region also holds another type's
callout. X1 sl-014: 300 D16 slab 212 m² (gold 214.5), sl-015: 150 D13 slab 60.4 m² (gold 61.69).

    from stoa.evidence.slabs import answer
    answer(pdf_path, question, page=0) -> Answer | None    (never raises)
"""

from __future__ import annotations

import functools
import os
import math
import re

from stoa.contract import Answer, Evidence

SOLVER = "slab_area"
_SLAB_Q = re.compile(r"\b(slabs?|slab-on-grade|sog|mat foundation|foundation pad|equipment pad|bodenplatte\w*|"
                     r"betonplatte\w*|laat(?:ta|an|at|oi|oj)\w*|platta\w*|plattor\w*)\b", re.I)  # 'laatan', 'plattan'
_AREA_Q = re.compile(r"\b(area|how (?:big|large)|size|square|m²|m2|sq\.? ?(?:m|ft)|fläche|pinta-ala|footprint)\b", re.I)
# H 23:51 (e)-(g): 'Sum of all slab areas?', 'How many m² is the 300 mm slab …', a weaker m² answer re-checked
SLAB_WORDING = os.environ.get("E1_SLAB_WORDING", "1") != "0"
if SLAB_WORDING:
    _AREA_Q = re.compile(_AREA_Q.pattern.replace(r"\b(area|", r"\b(areas?|", 1), re.I)
_HOWMANY_NOT_AREA = (r"\b(how many|number of)\b(?!\s+(?:square|sq|m²|m2|ft²|ft2|sf\b|met(?:re|er)s|feet))" if SLAB_WORDING
                     else r"\b(how many|number of)\b(?!\s+(?:square|sq))")
_THK = re.compile(r"(?<![\d.])(\d{2,4})\s*(?:mm\b)?\s*(?:thk\.?|thick)?\s*(?:conc(?:rete)?\.?\s*)?(?:structural\s+)?"
                  r"slab", re.I)
_THK_Q = re.compile(r"(?<![\d.])(\d{2,4})\s*(?:mm|millimet\w*)\b|(?<![\d.])(\d{2,4})\s*(?:thk|thick)", re.I)
_BAR = re.compile(r"(?<![A-Za-z0-9])([#D]\s?\d{1,2})\b", re.I)  # 'W/#5' has no word boundary before '#'
# imperial thickness: 1'-9" / 1’-6" / 12" SLAB (D1 16:48: a '\d+" SLAB' regex reads 1'-9" as 9")
_IMP = r"(?<![\d'’])(?:(\d{1,2})\s*['’]\s*-?\s*)?(\d{1,2})\s*(?:\"|”|'')\s*(?:thk\.?\s*|thick\s+)?(?:conc\w*\.?\s*)?"
_IMP_THK = re.compile(_IMP + r"(?:slab|sog)", re.I)
# a printed extent in the callout: '(9'-0"X9'-11")'
_EXTENT = re.compile(r"\(\s*(\d{1,3})\s*['’]\s*-?\s*(\d{1,2})?\s*(?:\"|”)?\s*[X×x]\s*(\d{1,3})\s*['’]\s*-?\s*"
                     r"(\d{1,2})?\s*(?:\"|”)?\s*\)")
FT2_M2 = 0.09290304


def _imp_mm(m) -> int:
    return int(round(((int(m.group(1)) if m.group(1) else 0) * 12 + int(m.group(2))) * 25.4))
_SPACING = re.compile(r"@\s*(\d{2,4})")
_PANELS_Q = re.compile(r"\bhow many\b.*\b(panels?|bays?|pours?|sections?|pieces?)\b.*\b(joints?|cjs?|c\.j\.?)\b|"
                       r"\b(joints?|cjs?)\b.*\binto how many\b|\binto how many\s+(?:panels?|bays?|pours?)\b", re.I)
_CJ = re.compile(r"^(?:C\.?J\.?|CJ\d*|C\.J)$")
_NUM = re.compile(r"^\d{3,5}$")


def _norm_bar(s: str) -> str:
    return s.upper().replace(" ", "")


@functools.lru_cache(maxsize=8)
def callouts(pdf_abs: str, page: int = 0) -> tuple:
    """((text, thickness_mm|None, bars tuple, spacings tuple, bbox), ...) for every text block naming a SLAB."""
    import pymupdf

    out = []
    with pymupdf.open(pdf_abs) as d:
        if page >= len(d):
            return ()
        for b in text_blocks(d[page]):
            t = " ".join(str(b[4]).split())
            if not re.search(r"\bSLAB", t, re.I):
                continue
            m = _THK.search(t)
            mi = None if m else _IMP_THK.search(t)
            bars = tuple(_norm_bar(x) for x in _BAR.findall(t))
            sp = tuple(int(x) for x in _SPACING.findall(t))
            out.append((t, int(m.group(1)) if m else _imp_mm(mi) if mi else None, bars, sp, tuple(b[:4])))
    return tuple(out)


def ocr_layer(pg) -> bool:
    """The page's text is an INVISIBLE layer only (render mode 3: P1's traced copy of a PNG / an OCR'd scan)."""
    if os.environ.get("E1_OCR_BLOCKS", "1") == "0":
        return False
    try:
        tt = pg.get_texttrace()
    except Exception:  # noqa: BLE001
        return False
    return bool(tt) and all(s.get("type") == 3 for s in tt)


def text_blocks(pg) -> list:
    """[(x0, y0, x1, y1, text), ...] text blocks of a page. A real PDF: PyMuPDF's blocks. An invisible OCR layer
    (ocr_layer): PyMuPDF joins its lines into page-wide blocks (S-100F PNG copy: the D13 callout's 3 lines + a pipe
    trench note 1300 pt away -> the slab was seeded between them, 229.55 m² for 61.69), so the lines are stacked here:
    a line joins the block whose last row it continues (same row: x-overlap or a gap < 1 text height; next row: top
    within 0.6 h of the row's bottom and left edges within 1.5 h or x-overlap >= half the narrower line)."""
    if not ocr_layer(pg):
        return [tuple(b[:5]) for b in pg.get_text("blocks") if b[6] == 0]
    lines = []
    for b in pg.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = " ".join(s["text"] for s in ln["spans"]).strip()
            if t:
                lines.append((*ln["bbox"], t))
    lines.sort(key=lambda r: (r[1], r[0]))
    blocks = []  # [x0, y0, x1, y1, [rows: [x0, y0, x1, y1, text]]]
    for x0, y0, x1, y1, t in lines:
        h = max(1.0, y1 - y0)
        best = None
        for bl in blocks:
            r = bl[4][-1]
            rh = max(1.0, r[3] - r[1])
            hh = min(h, rh)
            ov = min(x1, r[2]) - max(x0, r[0])
            if abs(y0 - r[1]) <= 0.4 * hh and abs(y1 - r[3]) <= 0.4 * hh and ov > -1.0 * hh:
                best = (bl, "row")
                break
            if 0.4 * hh <= y0 - r[1] and y0 - r[3] <= 0.6 * hh and (
                    abs(x0 - bl[0]) <= 1.5 * hh or ov >= 0.5 * min(x1 - x0, r[2] - r[0])):
                best = (bl, "next")
                break
        if best is None:
            blocks.append([x0, y0, x1, y1, [[x0, y0, x1, y1, t]]])
            continue
        bl, how = best
        if how == "row":
            r = bl[4][-1]
            r[0], r[1], r[2], r[3], r[4] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1), \
                (r[4] + " " + t) if x0 >= r[0] else (t + " " + r[4])
        else:
            bl[4].append([x0, y0, x1, y1, t])
        bl[0], bl[1], bl[2], bl[3] = min(bl[0], x0), min(bl[1], y0), max(bl[2], x1), max(bl[3], y1)
    return [(b[0], b[1], b[2], b[3], "\n".join(r[4] for r in b[4])) for b in blocks]


DASH_GAP_PT = float(os.environ.get("E1_DASH_GAP_PT", 4.0))  # traced copies: close gaps up to 2x this (dashes ~6 pt)


# a traced copy's OCR words are inserted as horizontal text fitted into the OCR box: a VERTICAL dimension number
# ('9950' along the S-100F D13 slab) gets a box shrunk around its centre (19 pt from its line; 11 pt on the PDF)
NUM_PT_TRACED = float(os.environ.get("E1_NUM_PT_TRACED", 20.0))


@functools.lru_cache(maxsize=16)
def _traced(pdf_abs: str, page: int) -> bool:
    """A picture traced into lines (stoa.formats.vectorize): an invisible OCR text layer, no images, and every
    stroke drawn with ONE pen (the traced ink outlines). A CAD PDF that only hides its text (glyphs drawn as paths,
    invisible text on top) has several pens and is read as a PDF."""
    try:
        import pymupdf

        with pymupdf.open(pdf_abs) as d:
            pg = d[page]
            if not ocr_layer(pg) or pg.get_images():
                return False
            ws = {round(dr.get("width") or 0, 3) for dr in pg.get_drawings()}
            return len(ws) == 1
    except Exception:  # noqa: BLE001
        return False


def _pick(cs: tuple, q: str):
    q_t = _SPACING.sub(" ", _BAR.sub(" ", q))  # 'the D13 slab': '13' is a bar mark, '@ 300' a spacing, not thicknesses
    qt = {int(a or b) for a, b in _THK_Q.findall(q_t)} | {int(x) for x in _THK.findall(q_t)} | \
        {_imp_mm(m) for m in _IMP_THK.finditer(q_t)}
    qb = {_norm_bar(x) for x in _BAR.findall(q)}
    qs = {int(x) for x in _SPACING.findall(q)}
    if not (qt or qb):
        return None
    hits = [c for c in cs if (not qt or c[1] in qt) and (not qb or qb <= set(c[2])) and (not qs or qs <= set(c[3]))
            and (c[1] is not None or c[2])]
    if not hits and qs:  # the spacing may be printed in the callout's next text block ('… W/ D22' / '@ 300 TOP OF …')
        hits = [c for c in cs if (not qt or c[1] in qt) and (not qb or qb <= set(c[2])) and (c[1] is not None or c[2])]
    if len(hits) > 1:  # 'the D13 slab': the slab-type callout (thickness + bars), not a rebar note that names D13
        typed = [c for c in hits if c[1] is not None]
        hits = typed if typed else hits
    return hits[0] if len(hits) == 1 else None


HATCH_RINGS = bool(int(os.environ.get("E1_HATCH_RINGS", "1")))
HATCH_RING_PT = float(os.environ.get("E1_HATCH_RING_PT", "2.5"))


@functools.lru_cache(maxsize=4)
def _mask(pdf_abs: str, page: int, n: float):
    """The sheet's lines that bound slabs (see module doc), rasterised on linework's px grid."""
    import cv2
    import numpy as np
    import pymupdf
    from shapely.geometry import LineString, Point, box
    from shapely.strtree import STRtree

    from stoa.evidence import linework as L

    pg = L._page(pdf_abs, page)
    if pg is None:
        return None
    ppt = pg["ppt"]
    strokes = list(pg["strokes"]) + list(pg.get("dashes") or ())  # a traced copy's dashed lines are slab edges
    m_per_pt = n * L.PT_TO_M_AT_1
    with pymupdf.open(pdf_abs) as d:
        p = d[page]
        ws = p.get_text("words")
        blocks = text_blocks(p)
        heads = []
        for dr in p.get_drawings():
            r = dr["rect"]
            if dr.get("fill") is not None and max(r.width, r.height) <= 8 and r.width * r.height <= 40:
                heads.append(box(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5))
    cj = [Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) for w in ws if _CJ.match(w[4])]
    nums = [box(*w[:4]) for w in ws if _NUM.match(w[4])]
    ntree = STRtree(nums) if nums else None
    htree = STRtree(heads) if heads else None
    segs, segw = [], []
    for a, w, _dark in strokes:
        P = np.asarray(a, float) / ppt
        for i in range(len(P) - 1):
            segs.append(LineString([P[i], P[i + 1]]))
            segw.append(w)
    stree = STRtree(segs) if segs else None
    lo, hi = 0.1 / m_per_pt, 0.45 / m_per_pt

    def ang(s):
        (x0, y0), (x1, y1) = s.coords
        return math.atan2(y1 - y0, x1 - x0) % math.pi

    def has_twin(s, w) -> bool:
        a = ang(s)
        for j in stree.query(s.buffer(hi)):
            t = segs[j]
            if t.length < 0.5 * s.length or abs(segw[j] - w) > 0.05:
                continue
            da = abs(ang(t) - a)
            if min(da, math.pi - da) > 0.03:
                continue
            if not lo <= t.distance(Point(s.interpolate(0.5, normalized=True))) <= hi:
                continue
            (ax, ay), (bx, by) = s.coords
            ux, uy = (bx - ax) / s.length, (by - ay) / s.length
            pr = sorted((x - ax) * ux + (y - ay) * uy for x, y in t.coords)
            if min(pr[-1], s.length) - max(pr[0], 0) >= 0.9 * s.length:
                return True
        return False

    def on_head(pt) -> bool:
        if htree is None:
            return False
        q = Point(pt)
        return any(heads[i].distance(q) <= 2.0 for i in htree.query(q.buffer(2.0)))

    traced = _traced(pdf_abs, page)
    if traced:  # lines traced from a picture: every line is the OUTLINE of its ink, cut into short pieces wherever
        # another line touches it (S-100F PNG: the 2.16 pt joint across the D13 slab = 14 pieces < 0.4 m, so the
        # dimension test below, which the PDF's one-stroke line fails, never saw it) -> test the whole ink RUN
        allm = np.zeros(pg["shape"], np.uint8)
        for a, w, _dark in strokes:
            cv2.polylines(allm, [np.asarray(a, np.int32)], False, 1, max(1, int(round(w * ppt))))
        allm = cv2.dilate(allm, np.ones((3, 3), np.uint8))
        runs: dict = {}

        def run_of(s):
            """The straight ink run (pt) through segment s: the line it lies on, sampled px by px, gaps <= 3 px."""
            (ax, ay), (bx, by) = s.coords
            L_ = s.length
            ux, uy = (bx - ax) / L_, (by - ay) / L_
            if ux < 0 or (ux == 0 and uy < 0):
                ux, uy = -ux, -uy
            mx, my = (ax + bx) / 2 * ppt, (ay + by) / 2 * ppt
            key = (round(math.atan2(uy, ux), 2), round((mx * uy - my * ux) / 2))
            t0 = mx * ux + my * uy
            for lo_, hi_, seg in runs.get(key, ()):
                if lo_ <= t0 <= hi_:
                    return seg
            Hh, Ww = allm.shape
            ts = np.arange(-max(Hh, Ww), max(Hh, Ww), 1.0)
            xs_, ys_ = np.rint(mx + ts * ux).astype(int), np.rint(my + ts * uy).astype(int)
            ok = (xs_ >= 0) & (xs_ < Ww) & (ys_ >= 0) & (ys_ < Hh)
            ink = np.zeros(len(ts), bool)
            ink[ok] = allm[ys_[ok], xs_[ok]] > 0
            i0 = int(np.searchsorted(ts, 0.0))
            if not ink[i0]:
                return s
            j0 = j1 = i0
            gap = 0
            while j0 > 0 and gap <= 3:
                j0 -= 1
                gap = 0 if ink[j0] else gap + 1
            j0 += gap
            gap = 0
            while j1 < len(ts) - 1 and gap <= 3:
                j1 += 1
                gap = 0 if ink[j1] else gap + 1
            j1 -= gap
            seg = LineString([((mx + ts[j0] * ux) / ppt, (my + ts[j0] * uy) / ppt),
                              ((mx + ts[j1] * ux) / ppt, (my + ts[j1] * uy) / ppt)])
            runs.setdefault(key, []).append((t0 + ts[j0], t0 + ts[j1], seg))
            return seg

        def twin_ink(r) -> bool:
            """Another line parallel to run r, lo..hi away, along >= 90 % of it (raster test: pieces don't matter)."""
            (ax, ay), (bx, by) = r.coords
            L_ = r.length
            if L_ <= 0:
                return False
            ux, uy = (bx - ax) / L_, (by - ay) / L_
            ts = np.linspace(0, L_, max(2, int(L_ * ppt / 2)))
            Hh, Ww = allm.shape
            for sg in (1, -1):
                for d in np.arange(lo, hi, 1.0 / ppt):
                    px_ = np.rint((ax + ts * ux - sg * d * uy) * ppt).astype(int)
                    py_ = np.rint((ay + ts * uy + sg * d * ux) * ppt).astype(int)
                    ok = (px_ >= 0) & (px_ < Ww) & (py_ >= 0) & (py_ < Hh)
                    if ok.mean() < 0.9:
                        continue
                    if (allm[py_[ok], px_[ok]] > 0).mean() >= 0.9:
                        return True
            return False

        dim_cache: dict = {}
        joint_cache: dict = {}
        # HATCH (a PDF reader drops hatch fills; a traced picture keeps every hatch stroke as a line): a diagonal
        # stroke (20-70 deg off the axes) with >= 3 parallel diagonal strokes (same angle +-3 deg) within 1.5 m.
        # S-100F PNG D16 slab: hatched patches at the slab edge cut 22 m² out of it (187 vs 214.5).
        diag = []
        for i_, (a, w, _dark) in enumerate(strokes):
            P_ = np.asarray(a, float) / ppt
            d_ = P_[-1] - P_[0]
            if len(P_) > 1 and not np.hypot(*d_):  # a closed outline (a traced line): its longest axis
                j_ = int(np.argmax(np.hypot(*(P_ - P_[0]).T)))
                d_ = P_[j_] - P_[0]
            L_ = float(np.hypot(*d_))
            if L_ * m_per_pt < 0.1:
                continue
            an = math.degrees(math.atan2(d_[1], d_[0])) % 180
            if 20 <= an % 90 <= 70:
                diag.append((i_, an, P_.mean(axis=0)))
        hatch = set()
        if len(diag) >= 4:
            pts_ = np.array([d[2] for d in diag])
            ans_ = np.array([d[1] for d in diag])
            r_pt = 1.5 / m_per_pt
            for k_, (i_, an, c_) in enumerate(diag):
                near = np.hypot(*(pts_ - c_).T) <= r_pt
                da = np.abs(ans_ - an)
                same = near & (np.minimum(da, 180 - da) <= 3)
                if int(same.sum()) - 1 >= 3:
                    hatch.add(i_)
        if HATCH_RINGS:
            # HATCH RINGS (cycle 10): dense hatch traced from a picture merges into thick diagonal ink; its outline is a
            # closed ring running >= 85 % diagonal (20-70 deg) with ink >= HATCH_RING_PT thick (2 x area / perimeter).
            # A traced slab edge is a thin ring along an axis. S-100F PNG D16: these bands cut a triangle off the slab.
            for i_, (a, w, _dark) in enumerate(strokes):
                P_ = np.asarray(a, float) / ppt
                if i_ in hatch or len(P_) < 4 or float(np.hypot(*(P_[-1] - P_[0]))) > 1.0:
                    continue
                sg_ = np.diff(P_, axis=0)
                l_ = np.hypot(*sg_.T)
                per = float(l_.sum())
                if per * m_per_pt < 1.0:
                    continue
                an_ = np.degrees(np.arctan2(sg_[:, 1], sg_[:, 0])) % 90
                if float(l_[(an_ >= 20) & (an_ <= 70)].sum()) < 0.85 * per:
                    continue
                area_ = 0.5 * abs(float(np.dot(P_[:, 0], np.roll(P_[:, 1], 1)) - np.dot(P_[:, 1], np.roll(P_[:, 0], 1))))
                if 2 * area_ / per >= HATCH_RING_PT:
                    hatch.add(i_)

    m = np.zeros(pg["shape"], np.uint8)
    widths, joints = [], []
    for i_s, (a, w, _dark) in enumerate(strokes):
        if traced and i_s in hatch:
            continue
        P = np.asarray(a, float) / ppt
        ln = LineString(P)
        if not traced and ln.length * m_per_pt > 2 and any(ln.distance(c) < 20 for c in cj):
            joints.append((np.asarray(a, np.int32), w))
            continue  # a control joint divides a slab into panels
        x0, y0, x1, y1 = ln.bounds
        # X1 22:31: bounds test instead of a new shapely box per (stroke, block): on a traced PNG copy that was 1.2 M
        # box() calls = 30 s of the first slab question. An axis-aligned box contains a line iff the line's bounds lie
        # inside it (shapely's contains() also needs an interior point: a line lying ON the box edge is the only change)
        if any(b[0] - 12 <= x0 and b[1] - 12 <= y0 and x1 <= b[2] + 12 and y1 <= b[3] + 12 and
               ((x1 - x0) > 0.8 * (b[2] - b[0]) or (y1 - y0) > 0.8 * (b[3] - b[1])) for b in blocks):
            continue  # a frame around a callout / note
        keep = []
        for i in range(len(P) - 1):
            s = segs[0].__class__([P[i], P[i + 1]])
            if traced and s.length >= 1.0:
                r = run_of(s)
                k_ = r.wkb
                if k_ not in joint_cache:  # a slab edge and the CJ lines that end on it are ONE traced outline
                    joint_cache[k_] = r.length * m_per_pt > 2 and any(r.distance(c_) < 20 for c_ in cj)
                if joint_cache[k_]:
                    joints.append((np.asarray([a[i], a[i + 1]], np.int32), w))
                    continue
                if k_ not in dim_cache:
                    dim_cache[k_] = (r.length * m_per_pt > 0.4 and ntree is not None and
                                     any(nums[k].distance(r) < NUM_PT_TRACED for k in
                                         ntree.query(r.buffer(NUM_PT_TRACED))) and
                                     not twin_ink(r))
                if dim_cache[k_]:
                    continue  # a piece of a dimension line / a line the dimension numbers sit on (as on the PDF)
                keep.append(np.asarray([a[i], a[i + 1]], np.int32))
                continue
            long_ = s.length * m_per_pt > 0.4
            if long_ and ntree is not None and any(nums[k].distance(s) < 12 for k in ntree.query(s.buffer(12))) \
                    and not has_twin(s, w):
                continue  # a dimension line (its number beside it, no wall face parallel to it)
            if long_ and on_head(P[i]) and on_head(P[i + 1]):
                continue  # a dimension line (arrow heads at both ends)
            keep.append(np.asarray([a[i], a[i + 1]], np.int32))
        if keep:
            th = max(1, int(round(w * ppt)))
            cv2.polylines(m, keep, False, 255, thickness=th)
            widths.append(w)
    return m, ppt, (sorted(widths)[len(widths) // 2] if widths else 0.25), tuple(joints)


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader never breaks the cascade
        return None


_THICK_Q = re.compile(r"\b(how thick|thickness|thick is|depth of the slab|slab depth)\b", re.I)
_TYPES_Q = re.compile(r"\bhow many\s+(?:different\s+|distinct\s+)?(?:slab\s+types|types\s+of\s+slabs?|kinds\s+of\s+"
                      r"slabs?|slabs?\s+types?)\b", re.I)


def _typed(cs: tuple) -> list:
    """Slab-type callouts: a thickness AND bars ('150 SLAB-ON-GRADE W/ D13 @ 300'), one per (thickness, bars)."""
    out, seen = [], set()
    for c in cs:
        if c[1] is None or not c[2]:
            continue
        k = (c[1], c[2])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _callout_answer(path: str, pdf: str, page: int, q: str, cs: tuple) -> Answer | None:
    """Read straight from the printed callouts: a slab's thickness (named by its bars), the number of slab types."""
    typed = _typed(cs)
    if _TYPES_Q.search(q):
        if len(typed) < 2:
            return None
        ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in c[4]], bbox_space="pdf_pt", exact=True,
                       note=f"slab type {i + 1}: {c[0][:60]}") for i, c in enumerate(typed)]
        return Answer(value=len(typed), unit="count", confidence=0.7, evidence=ev, solver=SOLVER,
                      assumptions=["a slab type = one printed callout with its own thickness and reinforcement: " +
                                   "; ".join(f"{c[1]} mm {'/'.join(c[2])}" for c in typed)],
                      trace=[f"slab_area: {len(typed)} distinct slab callouts (thickness + bars) on the sheet"])
    if _THICK_Q.search(q):
        qb = {_norm_bar(x) for x in _BAR.findall(q)}
        qs = {int(x) for x in _SPACING.findall(q)}
        if not qb:
            return None
        hits = [c for c in typed if qb <= set(c[2]) and (not qs or qs <= set(c[3]))]
        if len({c[1] for c in hits}) != 1:
            return None
        c = hits[0]
        want_mm = bool(re.search(r"\b(in mm|millimet\w*)\b", q, re.I))
        val, unit = (float(c[1]), "mm") if want_mm else (round(c[1] / 1000.0, 3), "m")
        return Answer(value=val, unit=unit, confidence=0.8, solver=SOLVER,
                      evidence=[Evidence(file=path, page=page, bbox=[round(v, 1) for v in c[4]], bbox_space="pdf_pt",
                                         exact=True, note=f"slab callout: {c[0][:60]}")],
                      assumptions=[f"thickness as printed in the slab callout '{c[0][:60]}' ({c[1]} mm)"],
                      trace=[f"slab_area: the callout with {'/'.join(sorted(qb))} reads {c[1]} mm"])
    return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    import cv2
    import numpy as np

    from stoa.data import abspath
    from stoa.evidence import linework as L

    q = question
    if _SLAB_Q.search(q) and (_THICK_Q.search(q) or _TYPES_Q.search(q)):
        pdf = str(abspath(path))
        if not pdf.lower().endswith(".pdf"):
            return None
        cs = callouts(pdf, page)
        return _callout_answer(path, pdf, page, q, cs) if cs else None
    if not _SLAB_Q.search(q) and _ITEM_Q.search(q) and _AREA_Q.search(q) and not re.search(
            _HOWMANY_NOT_AREA, q, re.I):
        pdf = str(abspath(path))
        return _label_dims(path, pdf, page, q) if pdf.lower().endswith(".pdf") else None
    if not _SLAB_Q.search(q) or not (_PANELS_Q.search(q) or (_AREA_Q.search(q) and not re.search(
            _HOWMANY_NOT_AREA, q, re.I))):
        return None
    pdf = str(abspath(path))
    if not pdf.lower().endswith(".pdf"):
        return None
    cs = callouts(pdf, page)
    c = _pick(cs, q) if cs else None
    if c is None:
        if cs and _TOTAL_Q.search(q) and not _PANELS_Q.search(q):
            return _total_answer(path, pdf, page, cs)
        return None
    ex = _EXTENT.search(c[0])
    if ex and not _PANELS_Q.search(q):
        return _extent_answer(path, page, q, c, ex)
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return None
    n = scales[0]
    mk = _mask(pdf, page, n)
    if mk is None:
        return None
    m, ppt, wmed, joints = mk
    m_per_px = n * L.PT_TO_M_AT_1 / ppt
    x0, y0, x1, y1 = c[4]
    seed = (int((x0 + x1) / 2 * ppt), int((y0 + y1) / 2 * ppt))
    H, W = m.shape
    area_q = not _PANELS_Q.search(q)
    traced = _traced(pdf, page)
    g = int(round(DASH_GAP_PT * ppt)) if traced else 0
    if g:  # a picture traced into lines: a DASHED slab edge is dashes with real gaps (a PDF strokes it whole)
        k_ = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1))
        mf = cv2.dilate(m, k_)
    else:
        mf = m
    tctx = (mf, m, cs) if traced else None
    if not (0 <= seed[0] < W and 0 <= seed[1] < H) or mf[seed[1], seed[0]]:
        return _leader_dims(path, pdf, page, n, c, tctx) if area_q else None
    ff = mf.copy()
    flood = np.zeros((H + 2, W + 2), np.uint8)
    cv2.floodFill(ff, flood, seed, 128)
    reg = (ff == 128).astype(np.uint8)
    if g:
        reg = (cv2.dilate(reg, k_) & (m == 0)).astype(np.uint8)
    ys, xs = np.nonzero(reg)
    if not len(xs) or xs.min() <= 1 or ys.min() <= 1 or xs.max() >= W - 2 or ys.max() >= H - 2:
        # ran off the sheet: not a closed slab -- the callout may sit OUTSIDE its slab with a leader into it
        return _leader_dims(path, pdf, page, n, c, tctx) if area_q else None
    cnts, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ct = max(cnts, key=cv2.contourArea)
    full = np.zeros_like(reg)
    cv2.drawContours(full, [ct], -1, 1, -1)
    per_px = cv2.arcLength(ct, True)
    a_m2 = (float(full.sum()) + per_px * (wmed * ppt / 2 + 0.5)) * m_per_px ** 2
    if not 2.0 <= a_m2 <= 5000:
        return _leader_dims(path, pdf, page, n, c, tctx) if area_q else None
    for o in cs:  # another slab type's callout inside: the region leaked across a slab edge
        if o is c or o[1] is None or (o[1] == c[1] and o[2] == c[2]):
            continue  # notes ('TOP OF SLAB EL.', a rebar note) are not another slab type
        ox, oy = int((o[4][0] + o[4][2]) / 2 * ppt), int((o[4][1] + o[4][3]) / 2 * ppt)
        if 0 <= ox < W and 0 <= oy < H and full[oy, ox]:
            return _leader_dims(path, pdf, page, n, c, tctx) if area_q else None
    if _PANELS_Q.search(q):  # 'into how many panels do the control joints divide the D16 slab'
        if not joints:
            return None
        cut = full.copy()
        for arr, w in joints:
            cv2.polylines(cut, [arr], False, 0, thickness=max(2, int(round(w * ppt))))
        k, lab_, stats, _cent = cv2.connectedComponentsWithStats(cut, connectivity=4)
        big = [i for i in range(1, k) if stats[i, cv2.CC_STAT_AREA] * m_per_px ** 2 >= 1.0]
        if len(big) < 2:
            return None
        ev = []
        for i in big:
            x, y, w_, h_ = (int(v) for v in stats[i, :4])
            ev.append(Evidence(file=path, page=page, bbox=[round(x / ppt, 1), round(y / ppt, 1), round((x + w_) / ppt, 1),
                                                         round((y + h_) / ppt, 1)], bbox_space="pdf_pt", exact=False,
                               note=f"panel {len(ev) + 1}: {stats[i, cv2.CC_STAT_AREA] * m_per_px ** 2:.1f} m²"))
        return Answer(value=len(big), unit="count", confidence=0.6, evidence=ev, solver=SOLVER,
                      assumptions=[f"slab = the printed callout '{c[0][:60]}', traced at 1:{n:g}",
                                   "panels = the parts of that slab between its control-joint (CJ) lines (>= 1 m²)"],
                      trace=[f"slab_area: '{c[0][:50]}' slab cut by {len(joints)} CJ lines into {len(big)} panels"])
    eps = 1.5
    poly = [[round(float(x) / ppt, 2), round(float(y) / ppt, 2)] for x, y in
            cv2.approxPolyDP(ct, eps, True)[:, 0, :]]
    if len(poly) < 3:
        return None
    xsd, ysd = [p[0] for p in poly], [p[1] for p in poly]
    bx, by = (max(xsd) - min(xsd)) * n * L.PT_TO_M_AT_1, (max(ysd) - min(ysd)) * n * L.PT_TO_M_AT_1
    val = round(a_m2, 2)
    name = c[0][:60]
    try:
        from stoa.evidence import dimrect as D

        r = D.rect_spanning(pdf, page, n, (min(xsd), min(ysd), max(xsd), max(ysd)))
    except Exception:  # noqa: BLE001
        r = None
    if r is None and traced:
        r = _ocr_sides(pdf, page, n, (min(xsd), min(ysd), max(xsd), max(ysd)), bx, by, a_m2)
    me = _main_ext(pdf, page, n, full, ppt, a_m2, traced=traced) if MAIN_EXT and \
        (r is None or abs(r["area_m2"] - a_m2) > 0.05 * a_m2) else None
    if me is not None:
        return _main_ext_answer(path, page, n, c, me, q, a_m2)
    grid = _dim_grid(pdf, page, n, ct, ppt, a_m2) if DIM_GRID and not traced and \
        (r is None or abs(r["area_m2"] - a_m2) > 0.05 * a_m2) else None
    if grid is not None:
        return _grid_answer(path, page, n, c, grid, poly, a_m2)
    if r is not None and abs(r["area_m2"] - a_m2) <= 0.05 * a_m2:
        # the traced slab IS the rectangle two printed dimensions span (S-100F D13: 9950 x 6200 = 61.69 vs traced
        # 60.77): the printed numbers are the answer
        how = "the rectangle these dimensions span" if not r.get("ocr") else \
            "its sides equal these printed numbers (OCR'd from the picture) within 1.5 %"
        return _dims_answer(path, page, n, c, r, f"the slab traced around its callout ({val} m², extent {bx:.2f} x "
                                                 f"{by:.2f} m) is {how}", poly)
    ev = [Evidence(file=path, page=page, bbox=[min(xsd), min(ysd), max(xsd), max(ysd)], bbox_space="pdf_pt",
                   exact=False, polygon=poly, note=f"{name}: {val} m² traced at 1:{n:g} (approximate)"),
          Evidence(file=path, page=page, bbox=[round(v, 1) for v in c[4]], bbox_space="pdf_pt", exact=True,
                   note=f"slab callout: {name}")]
    conf, pic = (0.25, ["lines traced from a picture of the sheet: hatching / symbols merged into the lines can cut "
                        "into the slab"]) if traced else (0.4, [])
    return Answer(value=val, unit="m2", confidence=conf, evidence=ev, solver=SOLVER,
                  assumptions=[f"scale: 1:{n:g} printed on the sheet; slab traced from the drawn lines (approximate)",
                               *pic,
                               f"slab type = the printed callout '{name}'",
                               "slab = the region around that callout bounded by slab edges / beams; control joints "
                               f"(CJ), dimension lines and text frames ignored; extent {bx:.2f} x {by:.2f} m"],
                  missing=["slab areas are not printed: the dimension strings along the slab edges would make it exact"],
                  trace=[f"slab_area: {len(cs)} slab callouts on the sheet; '{name}' matches the question; region "
                         f"{val} m² (extent {bx:.2f} x {by:.2f} m) at 1:{n:g}"])


DIM_GRID = bool(int(__import__("os").environ.get("E1_DIM_GRID", "0")))  # c12 experiment: OFF (D16 210.6, see board NEXT)


def _dim_grid(pdf: str, page: int, n: float, ct, ppt: float, a_m2: float) -> dict | None:
    """The slab measured on its PRINTED DIMENSION GRID (captain 22:40 / Ziying S-100F: 'Read the dimensions; do not
    trace'): the dimension ticks (dimrect.dims) over the traced region's extent cut it into grid cells; a cell >= 50 %
    covered by the region (holes filled: landings, stair symbols and pipe trenches are not voids) is slab; each cell's
    sides come from the printed values (a chain of dimensions between two ticks) or, where no printed chain links two
    ticks, from the drawing at 1:N. S-100F D16: 11000 x 19500 + the stair pocket 1750 x (150 + 2200) = 218.61 m² (traced
    212.45). Kept when the extent's outer ticks are printed, >= 80 % of the area comes from printed sides and the result
    is within -3 / +10 % of the trace. -> {"area_m2", "main_m2", "cells", "dims", "printed_share"} or None."""
    import cv2
    import numpy as np

    from stoa.evidence import dimrect as D
    from stoa.evidence import linework as L

    try:
        dl = D.dims(pdf, page, n)
    except Exception:  # noqa: BLE001
        return None
    if not dl:
        return None
    x, y, w, h = cv2.boundingRect(ct)
    bb = (x / ppt, y / ppt, (x + w) / ppt, (y + h) / ppt)
    mm_pt = n * L.PT_TO_M_AT_1 * 1000
    tol = max(3.0, 150 / mm_pt)

    def axis(kind, lo, hi):
        segs = [(min(d[2], d[3]), max(d[2], d[3]), float(d[0]), d) for d in dl if d[1] == kind and
                lo - tol <= min(d[2], d[3]) and max(d[2], d[3]) <= hi + tol]
        vals = sorted(v for a, b, _, _d in segs for v in (a, b))
        cl: list = []
        for t in vals:
            if cl and t - cl[-1][-1] <= 1.0:
                cl[-1].append(t)
            else:
                cl.append([t])
        ts = [sum(c_) / len(c_) for c_ in cl]
        if len(ts) < 2 or abs(ts[0] - lo) > tol or abs(ts[-1] - hi) > tol:
            return None
        def ix(v):
            return min(range(len(ts)), key=lambda i: abs(ts[i] - v))
        adj: dict = {i: [] for i in range(len(ts))}
        for a, b, v, _d in segs:
            i, j = ix(a), ix(b)
            if i != j:
                adj[i].append((j, v))
                adj[j].append((i, -v))
        pos: dict = {}
        comp: dict = {}
        for r0 in range(len(ts)):
            if r0 in pos:
                continue
            pos[r0], comp[r0], st = 0.0, r0, [r0]
            while st:
                u = st.pop()
                for v_, dv in adj[u]:
                    if v_ not in pos:
                        pos[v_], comp[v_] = pos[u] + dv, r0
                        st.append(v_)
        # the MAIN component (printed chains linked through shared ticks) must hold both outer ticks of the extent:
        # the overall size is printed; other chains are placed by the drawing at 1:N (they only subdivide)
        main = comp[0]
        if comp[len(ts) - 1] != main:
            return None
        mt = sorted((ts[i], pos[i]) for i in range(len(ts)) if comp[i] == main)
        def interp(t):
            for (t0, p0), (t1, p1) in zip(mt, mt[1:]):
                if t0 <= t <= t1:
                    return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p0
            return mt[0][1] + (t - mt[0][0]) * mm_pt if t < mt[0][0] else mt[-1][1] + (t - mt[-1][0]) * mm_pt
        gp = {}
        for k in set(comp.values()):
            idx_k = [i for i in range(len(ts)) if comp[i] == k]
            off = 0.0 if k == main else float(np.median([interp(ts[i]) - pos[i] for i in idx_k]))
            for i in idx_k:
                gp[i] = pos[i] + off
        sizes = [max(0.0, gp[i + 1] - gp[i]) for i in range(len(ts) - 1)]
        printed = [comp[i] == main and comp[i + 1] == main for i in range(len(ts) - 1)]
        return ts, sizes, printed, [d for *_x, d in segs]

    X, Y = axis("h", bb[0], bb[2]), axis("v", bb[1], bb[3])
    if __import__("os").environ.get("E1_DEBUG_GRID"):
        print("GRID bb", [round(v, 1) for v in bb], "tol", round(tol, 1), "X", X and [round(t, 1) for t in X[0]],
              X and [round(v) for v in X[1]], "Y", Y and [round(t, 1) for t in Y[0]], Y and [round(v) for v in Y[1]])
    if X is None or Y is None or len(X[0]) > 40 or len(Y[0]) > 40:
        return None
    mask = np.zeros((h + 2, w + 2), np.uint8)
    cv2.fillPoly(mask, [ct.reshape(-1, 2) - np.array([x - 1, y - 1])], 1)
    kk = max(3, int(round(1000 / mm_pt * ppt)) | 1)  # ~1 m: notches cut by symbols / landings close, corners stay
    mask = cv2.morphologyEx(cv2.copyMakeBorder(mask, kk, kk, kk, kk, cv2.BORDER_CONSTANT, value=0), cv2.MORPH_CLOSE,
                            np.ones((kk, kk), np.uint8))[kk:-kk, kk:-kk]
    cov = np.zeros((len(X[0]) - 1, len(Y[0]) - 1), bool)
    for i in range(len(X[0]) - 1):
        px0, px1 = int(round(X[0][i] * ppt)) - x + 1, int(round(X[0][i + 1] * ppt)) - x + 1
        for j in range(len(Y[0]) - 1):
            py0, py1 = int(round(Y[0][j] * ppt)) - y + 1, int(round(Y[0][j + 1] * ppt)) - y + 1
            sub = mask[max(0, py0):max(0, py1), max(0, px0):max(0, px1)]
            cov[i, j] = sub.size > 0 and float(sub.mean()) >= 0.5
    if not cov.any():
        return None
    xs_, ys_ = np.array(X[1]), np.array(Y[1])
    cell = np.outer(xs_, ys_) / 1e6
    area = float((cell * cov).sum())
    pr = np.outer(np.array(X[2], float), np.array(Y[2], float)).astype(bool)
    share = float((cell * cov * pr).sum()) / area if area else 0.0
    if __import__("os").environ.get("E1_DEBUG_GRID"):
        print("GRID area", round(area, 2), "share", round(share, 3), "trace", a_m2, "cov", cov.astype(int).tolist())
    if not 0.97 * a_m2 <= area <= 1.10 * a_m2:
        return None
    main = 0.0  # the largest rectangle of covered cells ('without the stair pocket')
    nx_, ny_ = cov.shape
    for i0 in range(nx_):
        for i1 in range(i0, nx_):
            for j0 in range(ny_):
                for j1 in range(j0, ny_):
                    if cov[i0:i1 + 1, j0:j1 + 1].all():
                        main = max(main, float(xs_[i0:i1 + 1].sum() * ys_[j0:j1 + 1].sum() / 1e6))
    used = [d for d in X[3] + Y[3]]
    return {"area_m2": round(area, 2), "main_m2": round(main, 2), "dims": used, "printed_share": round(share, 3),
            "w_mm": round(float(xs_[cov.any(axis=1)].sum())), "h_mm": round(float(ys_[cov.any(axis=0)].sum()))}


def _grid_answer(path: str, page: int, n: float, c, g: dict, poly, a_m2: float) -> Answer:
    name = c[0][:60]
    val = g["area_m2"]
    ev = [Evidence(file=path, page=page, bbox=[min(p[0] for p in poly), min(p[1] for p in poly),
                                               max(p[0] for p in poly), max(p[1] for p in poly)],
                   bbox_space="pdf_pt", exact=False, polygon=poly,
                   note=f"{name}: {val} m² on its printed dimension grid (traced outline {round(a_m2, 2)} m²)"),
          Evidence(file=path, page=page, bbox=[round(v, 1) for v in c[4]], bbox_space="pdf_pt", exact=True,
                   note=f"slab callout: {name}")]
    for d in sorted(g["dims"], key=lambda d: -d[0])[:12]:
        ev.append(Evidence(file=path, page=page, bbox=[round(t, 1) for t in d[5]], bbox_space="pdf_pt", exact=True,
                           note=f"dimension {d[0]} mm (printed; measures {d[6]} mm at 1:{n:g})"))
    alt = g["main_m2"]
    assumptions = [f"slab type = the printed callout '{name}'",
                   f"area from the PRINTED dimension strings: the slab's traced outline laid on the grid of its "
                   f"dimension lines ({g['printed_share']:.0%} of the area from printed numbers, the rest measured at "
                   f"1:{n:g}); landings, stair symbols and trenches inside the outline are not cut out (not drawn as "
                   "voids)"]
    if alt and abs(alt - val) > 0.005 * val:
        assumptions.append(f"its main rectangle alone (without the {val - alt:.2f} m² part outside it, e.g. a pocket "
                           f"round a corner) = {alt:.2f} m²")
    return Answer(value=val, unit="m2", confidence=0.6, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=[], trace=[f"slab_area: '{name}' on its printed dimension grid: {val} m² (main rectangle "
                                     f"{alt} m²; traced {round(a_m2, 2)} m²)"])


MAIN_EXT = bool(int(os.environ.get("E1_MAIN_EXT", "1")))
MAIN_COVER = 0.90  # the traced slab must fill its printed main rectangle (notches by landings / stair symbols: ~2 %)
MAIN_EDGE = 0.50  # ... and each side lies ON the slab edge: the 300 mm band inside it this full (S-100F: 0.83 with the
# DN landing; the foundation-grid chain one tick past the slab edge: 0.12)
EXT_COVER = 0.50  # a part outside it counts when the traced slab fills its snapped rectangle this much
EXT_PRINTED = 0.5  # ... and its area must come at least this much from printed numbers (S-100F pocket: 0.94)
_POCKET_Q = re.compile(r"\b(without|excluding|excl\.?|not (?:counting|including)|minus|less)\b[^.?!]{0,40}?"
                       r"\b(pocket|recess|notch|extension|bump|stair)", re.I)


def _chains(dl: tuple, kind: str, lo: float, hi: float, tol: float) -> tuple:
    """Ticks and printed edges of one axis: the dimensions of `kind` whose spans lie in [lo, hi] (+- tol); ticks within
    1 pt are one. -> (ticks [pt], edges [(i, j, value_mm, dim)] with i < j)."""
    segs = [d for d in dl if d[1] == kind and lo - tol <= min(d[2], d[3]) and max(d[2], d[3]) <= hi + tol]
    vals = sorted(v for d in segs for v in (min(d[2], d[3]), max(d[2], d[3])))
    cl: list = []
    for t in vals:
        if cl and t - cl[-1][-1] <= 1.0:
            cl[-1].append(t)
        else:
            cl.append([t])
    ts = [sum(c_) / len(c_) for c_ in cl]

    def ix(v):
        return min(range(len(ts)), key=lambda i: abs(ts[i] - v))
    edges = []
    for d in segs:
        i, j = ix(min(d[2], d[3])), ix(max(d[2], d[3]))
        if i < j:
            edges.append((i, j, float(d[0]), d))
    return ts, edges


def _printed_runs(ts: list, edges: list) -> list:
    """Every tick pair (a, b) linked by a CONTIGUOUS run of printed dimensions (a chain 6300 + 7400 + 5800, or one
    overall 19500) -> [(t_a, t_b, total_mm, dims)], one per pair (fewest dimensions)."""
    out: dict = {}
    fw: dict = {}
    for i, j, v, d in edges:
        fw.setdefault(i, []).append((j, v, d))
    for a in range(len(ts)):
        best: dict = {a: (0.0, [])}
        for u in range(a, len(ts)):  # ticks are sorted, edges go forward: one pass in order
            if u not in best:
                continue
            tot, ds = best[u]
            for j, v, d in fw.get(u, ()):
                if j not in best or len(best[j][1]) > len(ds) + 1:
                    best[j] = (tot + v, ds + [d])
        for b, (tot, ds) in best.items():
            if b != a and (a, b) not in out:
                out[(a, b)] = (ts[a], ts[b], tot, ds)
    return list(out.values())


def _side_mm(ts: list, edges: list, a: float, b: float, mm_pt: float, tol: float) -> tuple:
    """Length a -> b (pt) on one axis from the printed dimensions where they link ticks, the rest measured at 1:N
    (shortest measured remainder). -> (mm, printed_mm, dims)."""
    pts = sorted({a, b} | {t for t in ts if a + 0.5 < t < b - 0.5})
    nodes = pts
    def near(t):
        k = min(range(len(ts)), key=lambda i: abs(ts[i] - t)) if ts else None
        return k if k is not None and abs(ts[k] - t) <= 1.0 else None
    # DP: best[i] = (measured_mm, total_mm, printed_mm, dims) reaching nodes[i]
    best = {0: (0.0, 0.0, 0.0, [])}
    idx = {near(t): i for i, t in enumerate(nodes) if near(t) is not None}
    for i in range(len(nodes)):
        if i not in best:
            continue
        me, to, pr, ds = best[i]
        cand = []
        if i + 1 < len(nodes):
            g = (nodes[i + 1] - nodes[i]) * mm_pt
            cand.append((i + 1, me + g, to + g, pr, ds))
        ki = near(nodes[i])
        if ki is not None:
            for e in edges:
                if e[0] == ki and e[1] in idx and idx[e[1]] > i:
                    cand.append((idx[e[1]], me, to + e[2], pr + e[2], ds + [e[3]]))
        for j, me2, to2, pr2, ds2 in cand:
            if j not in best or me2 < best[j][0] - 1e-6:
                best[j] = (me2, to2, pr2, ds2)
    me, to, pr, ds = best.get(len(nodes) - 1, ((b - a) * mm_pt, (b - a) * mm_pt, 0.0, []))
    return to, pr, ds


def _side_txt(mm: float, pr: float, ds: list) -> str:
    """'2350 (2200 printed + 150 measured)' / '1750 (printed)'."""
    rest = mm - pr
    if not ds:
        return f"{mm:.0f} (measured)"
    if rest < 1:
        return f"{mm:.0f} (printed)" if len(ds) == 1 else f"{mm:.0f} ({' + '.join(f'{d[0]}' for d in ds)} printed)"
    return f"{mm:.0f} ({' + '.join(f'{d[0]}' for d in ds)} printed + {rest:.0f} measured)"


def _ocr_chains(pdf: str, page: int, n: float) -> tuple:
    """Dimension CHAINS read from a picture's OCR'd numbers alone (P1's traced copies: the dimension lines come out
    broken, their arrow heads lost): a printed number sits in the middle of its own dimension segment, so two
    neighbours on one line are (v1 + v2) / 2 apart at 1:N (S-100F PNG: 5800 -> 7400 centres 374.1 pt = 6599 mm,
    7400 -> 6300 6858 mm). Pairs within 3 % (>= 2 pt) on a common line (+- 3 pt) link; each chain of >= 2 numbers
    becomes dimrect-style tuples (value, 'h'|'v', a, b, line, bbox, value, 'ocr') whose shared ticks sit half-way
    between the neighbours' implied ends. Orientation from the pair, not the OCR box (rotated text gets flat boxes)."""
    import pymupdf

    from stoa.evidence import linework as L

    mm_pt = n * L.PT_TO_M_AT_1 * 1000
    with pymupdf.open(pdf) as d:
        if page >= len(d):
            return ()
        ws = [w for w in d[page].get_text("words") if _NUM.match(w[4]) and 100 <= int(w[4]) <= 60000]
    pts = [((w[0] + w[2]) / 2, (w[1] + w[3]) / 2, int(w[4]), tuple(w[:4])) for w in ws]
    out = []
    for kind, ax in (("h", 0), ("v", 1)):
        links: dict = {}
        order = sorted(range(len(pts)), key=lambda i: pts[i][ax])
        for ii, i in enumerate(order):
            for j in order[ii + 1:]:
                gap = pts[j][ax] - pts[i][ax]
                want = (pts[i][2] + pts[j][2]) / 2 / mm_pt
                if gap > 1.04 * want + 2:
                    if gap > 1.04 * 60000 / mm_pt:
                        break
                    continue
                if abs(pts[j][1 - ax] - pts[i][1 - ax]) <= 3.0 and abs(gap - want) <= max(2.0, 0.03 * want):
                    if i not in links:  # nearest partner along the line only
                        links[i] = j
        nxt = {}
        for i, j in links.items():
            if j not in nxt.values():
                nxt[i] = j
        heads = [i for i in nxt if i not in nxt.values()]
        for h_ in heads:
            ch = [h_]
            while ch[-1] in nxt and len(ch) < 30:
                ch.append(nxt[ch[-1]])
            if len(ch) < 2:
                continue
            ends = []
            for a, b in zip(ch, ch[1:]):
                ea = pts[a][ax] + pts[a][2] / 2 / mm_pt
                eb = pts[b][ax] - pts[b][2] / 2 / mm_pt
                ends.append((ea + eb) / 2)
            ticks = [pts[ch[0]][ax] - pts[ch[0]][2] / 2 / mm_pt] + ends + [pts[ch[-1]][ax] + pts[ch[-1]][2] / 2 / mm_pt]
            line = sum(pts[i][1 - ax] for i in ch) / len(ch)
            for k_, i in enumerate(ch):
                out.append((pts[i][2], kind, ticks[k_], ticks[k_ + 1], line, pts[i][3], pts[i][2], "ocr"))
    return tuple(out)


def _main_ext(pdf: str, page: int, n: float, full, ppt: float, a_m2: float, traced: bool = False) -> dict | None:
    """The slab from its PRINTED dimension strings (captain 22:40, Ziying S-100F: 'Read the dimensions; do not trace'):
    (a) MAIN rectangle = a printed run on each axis (a chain 6300 + 7400 + 5800 = 19500 by 5500 + 5500 = 11000) whose
    rectangle the traced slab fills >= MAIN_COVER (holes and notches by landings / stair symbols are not voids) -- the
    largest such rectangle; (b) EXTENSIONS = the traced slab outside it (>= 1 m², slivers opened away), each snapped to
    the nearest printed ticks per side and counted when the slab fills >= EXT_COVER of the snapped rectangle; its sides
    from printed values where ticks are linked, else measured at 1:N (S-100F stair pocket 1750 x (150 + 2200)).
    Kept when the total is within -3 / +10 % of the trace. -> {"area_m2", "main_m2", "main", "exts", "dims",
    "printed_share", "outline"} or None."""
    import cv2
    import numpy as np

    from stoa.evidence import dimrect as D
    from stoa.evidence import linework as L

    try:
        dl = D.dims(pdf, page, n)
    except Exception:  # noqa: BLE001
        return None
    if traced:  # a picture: its OCR'd chains (the traced dimension lines are broken), lines-found ones kept apart
        try:
            dl = tuple(dl) + _ocr_chains(pdf, page, n)
        except Exception:  # noqa: BLE001
            pass
    if not dl:
        return None
    ys_, xs_ = np.nonzero(full)
    if not len(xs_):
        return None
    bb = (xs_.min() / ppt, ys_.min() / ppt, (xs_.max() + 1) / ppt, (ys_.max() + 1) / ppt)
    mm_pt = n * L.PT_TO_M_AT_1 * 1000
    tol = max(3.0, 150 / mm_pt)
    TX, EX = _chains(dl, "h", bb[0], bb[2], tol)
    TY, EY = _chains(dl, "v", bb[1], bb[3], tol)
    if len(TX) < 2 or len(TY) < 2 or len(TX) > 60 or len(TY) > 60:
        return None
    rx = [r for r in _printed_runs(TX, EX) if r[1] - r[0] >= 0.4 * (bb[2] - bb[0])]
    ry = [r for r in _printed_runs(TY, EY) if r[1] - r[0] >= 0.4 * (bb[3] - bb[1])]
    if not rx or not ry:
        return None
    H, W = full.shape

    def px(v):
        return int(round(v * ppt))

    def cover(x0, y0, x1, y1):
        sub = full[max(0, px(y0)):min(H, px(y1)), max(0, px(x0)):min(W, px(x1))]
        return float(sub.mean()) if sub.size else 0.0
    trace_px = float(full.sum())
    bw = max(3.0, 300 / mm_pt)  # each side of the main rectangle lies ON the slab edge: the band inside it is slab

    ins = max(3.0, 100 / mm_pt) if traced else 0.0  # a picture: ticks from OCR'd number centres (+- 3-6 pt) and
    # thick traced edge lines -- the band starts a little inside

    def edges_ok(x0, y0, x1, y1):
        x0, y0, x1, y1 = x0 + ins, y0 + ins, x1 - ins, y1 - ins
        return min(cover(x0, y0, x0 + bw, y1), cover(x1 - bw, y0, x1, y1), cover(x0, y0, x1, y0 + bw),
                   cover(x0, y1 - bw, x1, y1)) >= MAIN_EDGE
    best = None
    for a in rx:
        for b in ry:
            area = a[2] * b[2] / 1e6
            if best is not None and area <= best[0]:
                continue
            rect_px = (px(a[1]) - px(a[0])) * (px(b[1]) - px(b[0]))
            if rect_px < 0.6 * trace_px or cover(a[0], b[0], a[1], b[1]) < MAIN_COVER or \
                    not edges_ok(a[0], b[0], a[1], b[1]):
                continue
            best = (area, a, b)
    if best is None:
        return None
    main_m2, ra, rb = best
    mx0, mx1, my0, my1 = ra[0], ra[1], rb[0], rb[1]
    rem = full.copy()
    sb = max(2, int(round(100 / mm_pt * ppt)))  # slivers along the main edges (half a slab-edge line) go; a part's
    # thin arms round a notch stay (S-100F pocket: 150 / 300 mm strips either side of a stair cut-out)
    rem[max(0, px(my0) - sb):px(my1) + sb, max(0, px(mx0) - sb):px(mx1) + sb] = 0
    rem = cv2.morphologyEx(rem, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    nlab, lab, stats, _c = cv2.connectedComponentsWithStats(rem, connectivity=4)
    m2_px = (mm_pt / ppt / 1000) ** 2
    ticks_x = sorted(set(TX) | {mx0, mx1})
    ticks_y = sorted(set(TY) | {my0, my1})
    # every tick of the sheet on the axis may bound an extension (its own chains sit outside the main extent)
    allx = sorted({t for d in dl if d[1] == "h" for t in (d[2], d[3])} | set(ticks_x))
    ally = sorted({t for d in dl if d[1] == "v" for t in (d[2], d[3])} | set(ticks_y))

    def snap(v, ticks):
        t = min(ticks, key=lambda t_: abs(t_ - v)) if ticks else v
        return (t, True) if abs(t - v) <= tol else (v, False)
    exts, used = [], list(ra[3]) + list(rb[3])
    nums = []
    if traced:
        import pymupdf

        with pymupdf.open(pdf) as d_:
            nums = [(int(w[4]), (w[0] + w[2]) / 2, (w[1] + w[3]) / 2, tuple(w[:4])) for w in d_[page].get_text("words")
                    if _NUM.match(w[4])]
    for i in range(1, nlab):
        if stats[i, cv2.CC_STAT_AREA] * m2_px < 1.0:
            continue
        x, y, w, h = (int(v) for v in stats[i, :4])
        x0, y0, x1, y1 = x / ppt, y / ppt, (x + w) / ppt, (y + h) / ppt
        (sx0, kx0), (sx1, kx1) = snap(x0, allx), snap(x1, allx)
        (sy0, ky0), (sy1, ky1) = snap(y0, ally), snap(y1, ally)
        # clip to the main rectangle's edges where the part adjoins it
        if mx0 - tol <= sx0 and sx1 <= mx1 + tol:
            sx0, sx1 = max(sx0, mx0), min(sx1, mx1)
        if my0 - tol <= sy0 and sy1 <= my1 + tol:
            sy0, sy1 = max(sy0, my0), min(sy1, my1)
        if sx1 - sx0 <= 1 or sy1 - sy0 <= 1 or cover(sx0, sy0, sx1, sy1) < EXT_COVER:
            continue
        inside_x = mx0 - 0.5 <= sx0 and sx1 <= mx1 + 0.5
        inside_y = my0 - 0.5 <= sy0 and sy1 <= my1 + 0.5
        if inside_x and inside_y:
            continue
        TXe, EXe = _chains(dl, "h", sx0, sx1, 1.0)
        TYe, EYe = _chains(dl, "v", sy0, sy1, 1.0)
        wmm, wpr, wds = _side_mm(TXe, EXe, sx0, sx1, mm_pt, tol)
        hmm, hpr, hds = _side_mm(TYe, EYe, sy0, sy1, mm_pt, tol)
        if traced:  # OCR'd ticks are noisy: a side that equals a printed number near the part (2 %) is that number
            for side in (0, 1):
                mm_ = (sx1 - sx0 if side == 0 else sy1 - sy0) * mm_pt
                pad = max(sx1 - sx0, sy1 - sy0)
                hit = [d for d in nums if abs(d[0] - mm_) <= 0.02 * mm_ and sx0 - pad <= d[1] <= sx1 + pad and
                       sy0 - pad <= d[2] <= sy1 + pad]
                if hit:
                    h_ = min(hit, key=lambda d: abs(d[0] - mm_))
                    pd = (h_[0], "h" if side == 0 else "v", 0.0, 0.0, 0.0, h_[3], h_[0], "ocr")
                    if side == 0:
                        wmm, wpr, wds = float(h_[0]), float(h_[0]), [pd]
                    else:
                        hmm, hpr, hds = float(h_[0]), float(h_[0]), [pd]
        a_ = wmm * hmm / 1e6
        if wpr * hpr < EXT_PRINTED * wmm * hmm:
            return None  # a part the printed strings do not dimension (a leak of the trace, X1's scanned S-100F D13:
            # 300 printed of 3316 x 1356) -- the main rectangle alone would not be the slab either: no answer here
        exts.append({"rect": (sx0, sy0, sx1, sy1), "w_mm": round(wmm), "h_mm": round(hmm), "area_m2": a_,
                     "sides": (_side_txt(wmm, wpr, wds), _side_txt(hmm, hpr, hds)),
                     "printed": (wpr * hpr) / (wmm * hmm) if wmm and hmm else 0.0, "snapped": (kx0, ky0, kx1, ky1),
                     "dims": wds + hds})
        used += wds + hds
    area = main_m2 + sum(e["area_m2"] for e in exts)
    if __import__("os").environ.get("E1_DEBUG_GRID"):
        print("MAINEXT main", round(main_m2, 3), [round(v, 1) for v in (mx0, my0, mx1, my1)], "exts",
              [(e["w_mm"], e["h_mm"], round(e["area_m2"], 3), [round(v, 1) for v in e["rect"]]) for e in exts],
              "area", round(area, 3), "trace", a_m2)
    if not 0.97 * a_m2 <= area <= 1.10 * a_m2:
        return None
    share = (main_m2 + sum(e["area_m2"] * e["printed"] for e in exts)) / area
    try:
        from shapely.geometry import box
        from shapely.ops import unary_union

        u = unary_union([box(mx0, my0, mx1, my1)] + [box(*e["rect"]) for e in exts]).buffer(0.01).buffer(-0.01)
        outline = [[round(float(x_), 1), round(float(y_), 1)] for x_, y_ in
                   (u.exterior.coords if u.geom_type == "Polygon" else max(u.geoms, key=lambda g: g.area).exterior.coords)][:-1]
    except Exception:  # noqa: BLE001
        outline = [[mx0, my0], [mx1, my0], [mx1, my1], [mx0, my1]]
    return {"area_m2": round(area, 2), "main_m2": round(main_m2, 2), "main": (ra[2], rb[2], (mx0, my0, mx1, my1)),
            "exts": exts, "dims": used, "printed_share": round(share, 3), "outline": outline, "traced": traced}


def _main_ext_answer(path: str, page: int, n: float, c, g: dict, q: str, a_m2: float) -> Answer:
    name = c[0][:60]
    wm, hm, mr = g["main"]
    exts = g["exts"]
    excl = bool(exts) and bool(_POCKET_Q.search(q))
    val = g["main_m2"] if excl else g["area_m2"]
    parts = " + ".join(f"{e['w_mm']} x {e['h_mm']} mm" for e in exts)
    ev = [Evidence(file=path, page=page, bbox=[min(p[0] for p in g["outline"]), min(p[1] for p in g["outline"]),
                                               max(p[0] for p in g["outline"]), max(p[1] for p in g["outline"])],
                   bbox_space="pdf_pt", exact=False, polygon=g["outline"],
                   note=f"{name}: {val} m² from its printed dimensions ({wm:g} x {hm:g} mm" +
                        (f" + {parts}" if exts and not excl else "") + f"; traced outline {round(a_m2, 2)} m²)"),
          Evidence(file=path, page=page, bbox=[round(v, 1) for v in mr], bbox_space="pdf_pt", exact=False,
                   polygon=[[round(mr[0], 1), round(mr[1], 1)], [round(mr[2], 1), round(mr[1], 1)],
                            [round(mr[2], 1), round(mr[3], 1)], [round(mr[0], 1), round(mr[3], 1)]],
                   note=f"main rectangle {wm:g} x {hm:g} mm = {g['main_m2']} m²")]
    for e in ([] if excl else exts):
        r = e["rect"]
        ev.append(Evidence(file=path, page=page, bbox=[round(v, 1) for v in r], bbox_space="pdf_pt", exact=False,
                           polygon=[[round(r[0], 1), round(r[1], 1)], [round(r[2], 1), round(r[1], 1)],
                                    [round(r[2], 1), round(r[3], 1)], [round(r[0], 1), round(r[3], 1)]],
                           note=f"part beyond the main rectangle {e['sides'][0]} x {e['sides'][1]} mm = "
                                f"{e['area_m2']:.2f} m²"))
    ev.append(Evidence(file=path, page=page, bbox=[round(v, 1) for v in c[4]], bbox_space="pdf_pt", exact=True,
                       note=f"slab callout: {name}"))
    seen = set()
    for d in g["dims"]:
        if id(d) in seen:
            continue
        seen.add(id(d))
        ocr = len(d) > 7 and d[7] == "ocr"
        ev.append(Evidence(file=path, page=page, bbox=[round(t, 1) for t in d[5]], bbox_space="pdf_pt", exact=not ocr,
                           note=f"dimension {d[0]} mm (printed; OCR'd from the picture, placed by its chain's spacing at "
                                f"1:{n:g})" if ocr else f"dimension {d[0]} mm (printed; measures {d[6]} mm at 1:{n:g})"))
    assumptions = [f"slab type = the printed callout '{name}'",
                   f"area from the PRINTED dimension strings: main rectangle {wm:g} x {hm:g} mm = {g['main_m2']} m²" +
                   (f" + the part(s) beyond it {parts} = {sum(e['area_m2'] for e in exts):.2f} m² (sides snapped to "
                    f"the printed dimension ticks: " + "; ".join(f"{e['sides'][0]} x {e['sides'][1]} mm" for e in exts) +
                    (f"; {g['printed_share']:.1%} of the area from printed numbers, the rest measured at 1:{n:g})"
                     if g["printed_share"] < 0.9995 else ")") if exts else "") +
                   "; landings, stair symbols and trenches inside the outline are not cut out (not drawn as voids)"]
    if exts and not excl:
        assumptions.append(f"without the {parts} part(s) beyond the main rectangle (e.g. a stair pocket round a corner) "
                           f"it is {g['main_m2']} m²")
    elif excl:
        assumptions.append(f"the {parts} part(s) beyond the main rectangle left out as asked; with them "
                           f"{g['area_m2']} m²")
    conf = (0.6 if g["printed_share"] >= 0.9 else 0.5) - (0.1 if g.get("traced") else 0.0)
    if g.get("traced"):
        assumptions.append("a picture traced into lines: the dimension numbers are OCR'd; each chain is accepted only "
                           "when its numbers' spacing matches their values at the printed scale")
    return Answer(value=val, unit="m2", confidence=conf, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=[], trace=[f"slab_area: '{name[:50]}' from its printed dimensions: main {wm:g} x {hm:g} mm = "
                                     f"{g['main_m2']} m²" + (f" + {parts}" if exts else "") +
                                     f" = {g['area_m2']} m² (traced {round(a_m2, 2)} m²)"])


_ITEM_Q = re.compile(r"\b(foundations?|footings?|pads?|plinths?|pits?|sumps?|platforms?|pedestals?|"
                     r"fundament\w*|perustu\w*)\b", re.I)
_Q_STOP = {"how", "big", "large", "is", "are", "the", "a", "an", "in", "on", "of", "plan", "what", "area", "size",
           "its", "this", "sheet", "drawing", "square", "metres", "meters", "feet", "m2", "sf", "total", "floor", "much",
           "does", "take", "up", "cover", "footprint", "what's", "whats", "kuinka", "suuri", "pinta", "ala", "mikä",
           "wie", "groß", "gross", "ist", "der", "die", "das", "hur", "stor", "är", "och", "the"}


_ITEM_SYN_RE = re.compile(r"\bgenerator\s+sets?\b|\bgenerators?\b|\bgeneraattori\w*|\bgenerator\w*|"
                          r"\bperustu\w*|\bfundament\w*|\bpads?\b", re.I)


def _item_syn(w: str) -> str:
    """'generator set' / 'generaattorin' -> 'genset' (the printed GENSET FOUNDATION, G1 22:21); 'perustuksen' /
    'Fundament' -> 'foundation'."""
    w = w.lower()
    return "foundation" if w.startswith(("perustu", "fundament", "pad")) else "genset"


def _label_dims(path: str, pdf: str, page: int, q: str) -> Answer | None:
    """'How big is the genset foundation in plan?' (X1 sl-016, S-100F): the ONE text block naming every content word
    of the question, a leader from it into the drawing, and the rectangle two printed dimensions span there."""
    import pymupdf

    from stoa.evidence import linework as L

    ql = _ITEM_SYN_RE.sub(lambda m_: _item_syn(m_.group(0)), q.lower())
    qw = list(dict.fromkeys(w for w in re.findall(r"[a-zà-ÿ0-9]+", ql) if w not in _Q_STOP and len(w) >= 3))
    if not qw:
        return None
    with pymupdf.open(pdf) as d:
        if page >= len(d):
            return None
        # a traced picture's OCR lines come as page-wide blocks: stack them into labels as for slab callouts
        blocks = list(text_blocks(d[page])) if ocr_layer(d[page]) else \
            [b for b in d[page].get_text("blocks") if b[6] == 0]
    hits = []
    for b in blocks:
        bw = set(re.findall(r"[a-zà-ÿ0-9]+", str(b[4]).lower()))
        if all(any(w == x or (len(w) > 4 and x.startswith(w[:-1])) for x in bw) for w in qw):
            hits.append(b)
    if len(hits) != 1:
        return None
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return None
    n = scales[0]
    b = hits[0]
    c = (" ".join(str(b[4]).split()), None, (), (), tuple(b[:4]))
    tctx = None
    if _traced(pdf, page):  # a picture traced into lines: follow the leader in the ink (as for a slab callout)
        import cv2

        mk = _mask(pdf, page, n)
        if mk is None:
            return None
        m, ppt = mk[0], mk[1]
        g = int(round(DASH_GAP_PT * ppt))
        mf = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1))) if g else m
        tctx = (mf, m, callouts(pdf, page))
    a = _leader_dims(path, pdf, page, n, c, tctx)
    if a is None:
        return None
    name = c[0][:60]
    a.assumptions = [f"the item = the drawing's label '{name}' and the outline its leader points into",
                     *a.assumptions[1:2]]
    a.evidence[-1].note = f"label: {name}"
    a.confidence = 0.55
    return a


def _extent_answer(path: str, page: int, q: str, c, ex) -> Answer:
    """The callout PRINTS the slab's extent ('1'-9" SLAB-ON-GRADE ... WITHIN LIMITS OF SHADED BOUNDARY (9'-0"X9'-11")',
    AW 078-24-29 S-201, D1 16:48): its product, in SF as printed + m²."""
    a_ft = int(ex.group(1)) + (int(ex.group(2)) if ex.group(2) else 0) / 12
    b_ft = int(ex.group(3)) + (int(ex.group(4)) if ex.group(4) else 0) / 12
    sf = round(a_ft * b_ft, 2)
    m2 = round(sf * FT2_M2, 2)
    want_m2 = bool(re.search(r"\b(m²|m2|square met\w*|sq\.? ?m\b|sqm)\b", q, re.I))
    val, unit = (m2, "m2") if want_m2 else (sf, "ft2")
    name = c[0][:90]
    return Answer(value=val, unit=unit, confidence=0.75, solver=SOLVER,
                  evidence=[Evidence(file=path, page=page, bbox=[round(t, 1) for t in c[4]], bbox_space="pdf_pt",
                                     exact=True, note=f"slab callout with its printed extent {ex.group(0)}: "
                                                      f"{a_ft:g} x {b_ft:.3f} ft = {sf:g} SF ({m2:g} m²)")],
                  assumptions=[f"slab type = the printed callout '{name}'",
                               f"area = the extent printed in the callout {ex.group(0)} = {sf:g} SF = {m2:g} m²"],
                  trace=[f"slab_area: '{name[:60]}' prints its extent {ex.group(0)} -> {a_ft:g} x {b_ft:.3f} ft = "
                         f"{sf:g} SF ({m2:g} m²)"])


_TOTAL_Q = re.compile(r"\b(total|all|overall|whole|entire|combined|sum|gesamt\w*|insgesamt|yhteensä)\b", re.I)


_ALT_RE = re.compile(r"^without the .* beyond the main rectangle .* it is ([\d.]+) m²")


def _short(c) -> str:
    """'300 SLAB-ON-GRADE W/ D16 @ 300' from '300 SLAB-ON-GRADE W/ D16 @ 300 TOP FACE. BOTTOM' (<= 40 chars, cut at a
    word; a hyphen broken over two lines joined)."""
    t = re.sub(r"-\s+", "-", c[0].split(". ")[0])
    m = re.match(r".*?\b[#D]\s?\d{1,2}\b(?:\s*@\s*\d+)?", t)  # up to the bar mark and its spacing
    if m:
        t = m.group(0)
    if len(t) <= 40:
        return t
    m = re.match(r".{0,40}(?=\s)", t)
    return (m.group(0) if m else t[:40]).rstrip(" ,;:/")


def _total_answer(path: str, pdf: str, page: int, cs: tuple) -> Answer | None:
    """'What is the total slab area?' (Ziying's s100f-04): every slab TYPE on the sheet (a thickness + bars callout)
    measured on its own (traced / printed dimensions / leader), summed; abstains when any type cannot be measured."""
    types = _typed(cs)
    if len(types) < 1 or len(types) > 8:
        return None
    parts = []
    for c in types:
        a = _answer(path, f"What is the area of the {c[0]} slab?", page)
        if a is None or not isinstance(a.value, (int, float)) or str(a.unit) != "m2":
            return None
        parts.append((c, a))
    total = round(sum(a.value for _, a in parts), 2)
    terms = " + ".join(f"{a.value:g} ({_short(c)})" for c, a in parts)
    ev = [e for _, a in parts for e in a.evidence]
    conf = round(min(0.55, min(a.confidence for _, a in parts)), 3)  # each part's own calibration; a sum risks more
    alts = []  # a part with a stated 'without the … part(s) beyond the main rectangle … it is X m²' (S-100F pocket)
    for c, a in parts:
        for t in a.assumptions:
            m_ = _ALT_RE.search(t)
            if m_:
                alts.append((c, a.value - float(m_.group(1))))
    alt = [f"without the part(s) beyond each main rectangle ({', '.join(f'{d:.2f} m² of {_short(c)}' for c, d in alts)}) "
           f"the total is {round(total - sum(d for _, d in alts), 2)} m²"] if alts else []
    return Answer(value=total, unit="m2", confidence=conf, evidence=ev, solver=SOLVER,
                  assumptions=[f"total slab area = the sum of the {len(parts)} slab types printed on the sheet: {terms} "
                               f"= {total} m²",
                               "by type: " + "; ".join(f"{_short(c)}: {a.value:g} m²" for c, a in parts),
                               *alt,
                               "each slab measured to its edge (printed dimensions where they span it, else traced at "
                               "the printed scale); openings deducted only where drawn as voids"],
                  missing=[m for _, a in parts for m in a.missing][:2],
                  trace=[f"slab_area: total of {len(parts)} slab types: {terms} = {total} m²",
                         *(f"slab_area: [{c[0][:30]}] {a.trace[-1][:160]}" for c, a in parts)])


def _ocr_sides(pdf: str, page: int, n: float, bb: tuple, bx: float, by: float, a_m2: float) -> dict | None:
    """A traced copy of a picture (no dimension LINES to pair with, only OCR'd numbers): two printed mm numbers near
    the traced slab (within 30 % of its size around its box) that equal its traced width and height within 1.5 %
    (S-100F PNG D13: 6.18 x 9.92 m traced, '6200' and '9950' printed -> 61.69 m², the PDF's answer). Same dict as
    dimrect.rect_spanning; the number boxes are OCR boxes."""
    import pymupdf

    x0, y0, x1, y1 = bb
    pad = 0.3 * max(x1 - x0, y1 - y0)
    with pymupdf.open(pdf) as d:
        ws = [w for w in d[page].get_text("words") if _NUM.match(w[4])
              and x0 - pad <= (w[0] + w[2]) / 2 <= x1 + pad and y0 - pad <= (w[1] + w[3]) / 2 <= y1 + pad]

    def best(side_m):
        hits = [(abs(int(w[4]) / 1000.0 - side_m) / side_m, w) for w in ws]
        hits = [h for h in hits if h[0] <= 0.015]
        return min(hits, key=lambda h: h[0])[1] if hits else None

    wh, wv = best(bx), best(by)
    if wh is None or wv is None or wh is wv:
        return None
    hv, vv = int(wh[4]), int(wv[4])
    area = round(hv * vv / 1e6, 3)
    if abs(area - a_m2) > 0.06 * a_m2:
        return None
    mm = n * 0.3527777778  # mm on the ground per pt at 1:n
    return {"w_mm": hv, "h_mm": vv, "area_m2": area, "rect": bb, "ink": "traced from the picture",
            "dims": ((hv, "h", x0, x1, None, tuple(wh[:4]), round(bx * 1000)),
                     (vv, "v", y0, y1, None, tuple(wv[:4]), round(by * 1000))), "ocr": True, "mm_per_pt": mm}


def _dims_answer(path: str, page: int, n: float, c, r: dict, how: str, poly=None) -> Answer:
    (hv, *_h), (vv, *_v) = r["dims"]
    h, v = r["dims"]
    x0, y0, x1, y1 = r["rect"]
    name = c[0][:60]
    val = r["area_m2"]
    ocr = bool(r.get("ocr"))
    ev = [Evidence(file=path, page=page, bbox=[round(t, 1) for t in h[5]], bbox_space="pdf_pt", exact=not ocr,
                   note=f"dimension {hv} mm (printed; {'the traced slab measures' if ocr else 'measures'} {h[6]} mm "
                        f"at 1:{n:g})"),
          Evidence(file=path, page=page, bbox=[round(t, 1) for t in v[5]], bbox_space="pdf_pt", exact=not ocr,
                   note=f"dimension {vv} mm (printed; {'the traced slab measures' if ocr else 'measures'} {v[6]} mm "
                        f"at 1:{n:g})"),
          Evidence(file=path, page=page, bbox=[round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                   bbox_space="pdf_pt", exact=False, polygon=[[round(x0, 1), round(y0, 1)], [round(x1, 1), round(y0, 1)],
                                                              [round(x1, 1), round(y1, 1)], [round(x0, 1), round(y1, 1)]],
                   note=f"{name}: {hv} x {vv} mm = {val} m²"),
          Evidence(file=path, page=page, bbox=[round(t, 1) for t in c[4]], bbox_space="pdf_pt", exact=True,
                   note=f"slab callout: {name}")]
    return Answer(value=val, unit="m2", confidence=0.6, evidence=ev, solver=SOLVER,
                  assumptions=[f"slab type = the printed callout '{name}'",
                               f"area = the printed dimensions {hv} x {vv} mm ({how}); printed dimensions win over "
                               f"the drawing measured at 1:{n:g}",
                               "slab = the plan area to the slab edge; nothing drawn as a void inside it"],
                  missing=[],
                  trace=[f"slab_area: '{name[:50]}': {how}; dimensions {hv} x {vv} mm " +
                         (f"(the traced slab's sides measure {h[6]} / {v[6]} mm at 1:{n:g}; numbers OCR'd from the "
                          f"picture) = {val} m²" if ocr else
                          f"(each measures its own line at 1:{n:g}: {h[6]} / {v[6]} mm; edges inked {r['ink']}) = "
                          f"{val} m²")])


@functools.lru_cache(maxsize=4)
def _ink(pdf_abs: str, page: int):
    """(every stroke rasterised and grown 1 px, ppt) on linework's px grid: the INK of a traced copy."""
    import cv2
    import numpy as np

    from stoa.evidence import linework as L

    pg = L._page(pdf_abs, page)
    if pg is None:
        return None
    ink = np.zeros(pg["shape"], np.uint8)
    for a, w, _d in list(pg["strokes"]) + list(pg.get("dashes") or ()):
        cv2.polylines(ink, [np.asarray(a, np.int32)], False, 1, max(1, int(round(w * pg["ppt"]))))
    return cv2.dilate(ink, np.ones((3, 3), np.uint8)), pg["ppt"]


def _ray(ink, p, ang: float, gap: int = 3) -> float:
    """Length (px) of the ink run leaving point p in direction ang (rad), gaps <= gap px."""
    import numpy as np

    H, W = ink.shape
    ux, uy = math.cos(ang), math.sin(ang)
    ts = np.arange(1, max(H, W))
    xs, ys = np.rint(p[0] + ts * ux).astype(int), np.rint(p[1] + ts * uy).astype(int)
    ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
    hit = np.zeros(len(ts), bool)
    hit[ok] = ink[ys[ok], xs[ok]] > 0
    last, miss = 0, 0
    for i, h in enumerate(hit):
        if h:
            last, miss = i + 1, 0
        else:
            miss += 1
            if miss > gap:
                break
    return float(last)


def _traced_leader(pdf: str, page: int, box_pt) -> tuple | None:
    """_traced_leader_once; when it finds nothing, once more for a 2-line label with a short tail and a DOTTED leader
    (S-100F PNG 'GENSET FOUNDATION / SEE A1/S-513': ~20 pt tail, dash-dot diagonal into the genset): half the row
    height and 7 px gaps along the runs."""
    got = _traced_leader_once(pdf, page, box_pt)
    return got if got is not None else _traced_leader_once(pdf, page, box_pt, row_k=0.5, gap=7)


def _traced_leader_once(pdf: str, page: int, box_pt, row_k: float = 1.0, gap: int = 3) -> tuple | None:
    """Tip (px, unit direction) of a leader drawn from a callout on a traced copy (no vector leader: the leader is ink
    outlines merged with every wall it crosses). From the ink nearest the callout's side, follow straight runs (<= 3,
    each >= 2 text heights, turning >= 20 deg) and return the end of the last one. S-100F PNG D22 callout -> the
    horizontal tail, then the diagonal into the genset slab."""
    import numpy as np

    got = _ink(pdf, page)
    if got is None:
        return None
    ink, ppt = got
    x0, y0, x1, y1 = (v * ppt for v in box_pt)
    hh = max(4.0, min(y1 - y0, 12 * ppt) * row_k)  # one text row is at most the block's height
    H, W = ink.shape
    best = None
    for side in ("r", "l"):
        xa, xb = (int(x1), int(x1 + 3 * hh)) if side == "r" else (int(x0 - 3 * hh), int(x0))
        ya, yb = int(y0), int(y1)
        xa, xb, ya, yb = max(0, xa), min(W, xb), max(0, ya), min(H, yb)
        if xb <= xa or yb <= ya:
            continue
        ys, xs = np.nonzero(ink[ya:yb, xa:xb])
        if not len(xs):
            continue
        d = (xs + xa - (x1 if side == "r" else x0)) ** 2
        i = int(np.argmin(d))
        p0 = (float(xs[i] + xa), float(ys[i] + ya))
        a0 = 0.0 if side == "r" else math.pi
        cand = [(_ray(ink, p0, a0 + math.radians(k), gap), a0 + math.radians(k)) for k in range(-80, 81, 5)]
        L0, ang = max(cand)
        if row_k != 1.0:  # thin long leaders: 5 deg drifts off them
            L0, ang = max((_ray(ink, p0, ang + math.radians(k / 2), gap), ang + math.radians(k / 2)) for k in range(-9, 10))
        if L0 >= 2 * hh and (best is None or L0 > best[0]):
            best = (L0, p0, ang)
    if best is None:
        return None
    L0, p, ang = best
    pts = [p]
    for _ in range(3):
        q = (p[0] + L0 * math.cos(ang), p[1] + L0 * math.sin(ang))
        pts.append(q)
        if row_k != 1.0 and len(pts) >= 3:
            break  # tail + one diagonal: past the arrow the ray would run on along a dimension line
        nxt = []
        for k in range(0, 360, 5):
            a2 = math.radians(k)
            dd = abs((a2 - ang + math.pi) % (2 * math.pi) - math.pi)
            if dd < math.radians(20) or dd > math.radians(160):
                continue  # straight on / back the way it came
            Ln = _ray(ink, q, a2, gap)
            if Ln >= 2 * hh:
                nxt.append((Ln, a2))
        if len(nxt) != 1 and not (nxt and max(nxt)[0] >= 2.5 * sorted(nxt)[-2][0]):
            break  # a clean bend continues one way only (a wall crossing would offer two)
        L0, ang = max(nxt)
        if row_k != 1.0:
            L0, ang = max((_ray(ink, q, ang + math.radians(k / 2), gap), ang + math.radians(k / 2)) for k in range(-9, 10))
        p = q
    if len(pts) < 3:
        return None  # a straight tail ending in open space is not a leader we can trust
    return pts[-1], (math.cos(ang), math.sin(ang)), ppt


def _traced_leader_answer(path: str, pdf: str, page: int, n: float, c, mf, m, cs) -> Answer | None:
    """Traced copy: the callout is outside its slab -> follow its leader in the ink, flood the slab at its tip, and
    answer only when two OCR'd printed numbers equal the traced sides (_ocr_sides)."""
    import cv2
    import numpy as np

    from stoa.evidence import linework as L

    lt = _traced_leader(pdf, page, c[4])
    if lt is None:
        return None
    (tx, ty), (ux, uy), ppt = lt
    H, W = mf.shape
    seed = None
    for k in range(0, 25):  # off the arrow head's ink, onward
        x, y = int(round(tx + k * ux)), int(round(ty + k * uy))
        if 0 <= x < W and 0 <= y < H and not mf[y, x]:
            seed = (x, y)
            break
    if seed is None:
        return None
    ff = mf.copy()
    cv2.floodFill(ff, np.zeros((H + 2, W + 2), np.uint8), seed, 128)
    reg = (ff == 128).astype(np.uint8)
    g = int(round(DASH_GAP_PT * ppt))
    k_ = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1))
    reg = (cv2.dilate(reg, k_) & (m == 0)).astype(np.uint8)
    ys, xs = np.nonzero(reg)
    if not len(xs) or xs.min() <= 1 or ys.min() <= 1 or xs.max() >= W - 2 or ys.max() >= H - 2:
        return None
    cnts, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ct = max(cnts, key=cv2.contourArea)
    full = np.zeros_like(reg)
    cv2.drawContours(full, [ct], -1, 1, -1)
    m_per_px = n * L.PT_TO_M_AT_1 / ppt
    a_m2 = float(full.sum()) * m_per_px ** 2
    if not 1.0 <= a_m2 <= 2000:
        return None
    for o in cs:
        if o[1] is None or o is c:
            continue
        ox, oy = int((o[4][0] + o[4][2]) / 2 * ppt), int((o[4][1] + o[4][3]) / 2 * ppt)
        if 0 <= ox < W and 0 <= oy < H and full[oy, ox]:
            return None
    poly = [[round(float(x) / ppt, 2), round(float(y) / ppt, 2)] for x, y in cv2.approxPolyDP(ct, 1.5, True)[:, 0, :]]
    xsd, ysd = [q[0] for q in poly], [q[1] for q in poly]
    bx, by = (max(xsd) - min(xsd)) * n * L.PT_TO_M_AT_1, (max(ysd) - min(ysd)) * n * L.PT_TO_M_AT_1
    r = _ocr_sides(pdf, page, n, (min(xsd), min(ysd), max(xsd), max(ysd)), bx, by, a_m2)
    if r is None:
        r = _traced_rect_at(pdf, page, n, (tx / ppt, ty / ppt), (min(xsd), min(ysd), max(xsd), max(ysd)))
        if r is not None:
            return _dims_answer(path, page, n, c, r, f"the callout's leader (followed in the traced ink) ends at "
                                                     f"({tx / ppt:.0f}, {ty / ppt:.0f}) pt inside the rectangle these "
                                                     f"printed numbers (OCR'd) dimension; its 4 edges are drawn and "
                                                     f"measure them at 1:{n:g} within 3 %", poly)
        return None
    return _dims_answer(path, page, n, c, r, f"the callout's leader (followed in the traced ink) ends at "
                                             f"({tx / ppt:.0f}, {ty / ppt:.0f}) pt; the slab traced there "
                                             f"({a_m2:.2f} m², extent {bx:.2f} x {by:.2f} m) has sides equal to these "
                                             f"printed numbers (OCR'd) within 1.5 %", poly)


def _traced_rect_at(pdf: str, page: int, n: float, tip: tuple, reg_bb: tuple) -> dict | None:
    """dimrect.rect_at for a picture traced into lines (S-100F PNG genset, 'D22' callout with a leader): the printed
    numbers are OCR'd and the dimension lines broken, so each number is placed by its chain (_ocr_chains) or, alone,
    as a span centred on it; a horizontal and a vertical one whose spans hold the leader tip span a rectangle, kept
    when the flooded piece at the tip lies inside it, each side slid <= 5 pt onto its traced ink line is >= 60 % inked,
    and the slid sides measure the printed values within 3 % at 1:N. Same dict as rect_spanning (+ 'ocr')."""
    import numpy as np
    import pymupdf

    from stoa.evidence import dimrect as D
    from stoa.evidence import linework as L

    mm_pt = n * L.PT_TO_M_AT_1 * 1000
    d_ = L.ink_dist(pdf, page)
    pg = L._page(pdf, page)
    if d_ is None or pg is None:
        return None
    ppt = pg["ppt"]
    H, W = d_.shape
    dl = [(*d, "dim") for d in D.dims(pdf, page, n)] + list(_ocr_chains(pdf, page, n))
    with pymupdf.open(pdf) as doc:
        ws = [w for w in doc[page].get_text("words") if _NUM.match(w[4]) and 200 <= int(w[4]) <= 30000]
    for w in ws:
        v = int(w[4])
        cx, cy, hl = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2, v / 2 / mm_pt
        dl.append((v, "h", cx - hl, cx + hl, cy, tuple(w[:4]), v, "ocr1"))
        dl.append((v, "v", cy - hl, cy + hl, cx, tuple(w[:4]), v, "ocr1"))
    x, y = tip
    def near(kind, u, w_):  # every chain-placed span holding the tip + the 8 nearest single numbers
        c_ = [d for d in dl if d[1] == kind and d[2] - 1 <= u <= d[3] + 1]
        return sorted([d for d in c_ if d[7] != "ocr1"], key=lambda d: abs(d[4] - w_))[:16] + \
            sorted([d for d in c_ if d[7] == "ocr1"], key=lambda d: abs(d[4] - w_))[:20]
    hh, vv = near("h", x, y), near("v", y, x)

    def share(ax, ay, bx, by):
        on, k = 0, 60
        for i in range(k):
            t = (i + 0.5) / k
            xi, yi = int(round((ax + (bx - ax) * t) * ppt)), int(round((ay + (by - ay) * t) * ppt))
            if 0 <= xi < W and 0 <= yi < H and d_[yi, xi] <= 2.5:
                on += 1
        return on / k

    def slide(rect):
        x0, y0, x1, y1 = rect
        offs = np.arange(-5.0, 5.01, 0.5)
        bx0 = max(offs, key=lambda o: share(x0 + o, y0, x0 + o, y1))
        bx1 = max(offs, key=lambda o: share(x1 + o, y0, x1 + o, y1))
        by0 = max(offs, key=lambda o: share(x0, y0 + o, x1, y0 + o))
        by1 = max(offs, key=lambda o: share(x0, y1 + o, x1, y1 + o))
        r = (x0 + bx0, y0 + by0, x1 + bx1, y1 + by1)
        ink = (share(r[0], r[1], r[2], r[1]), share(r[2], r[1], r[2], r[3]), share(r[0], r[3], r[2], r[3]),
               share(r[0], r[1], r[0], r[3]))
        return r, ink
    tol = max(3.0, 150 / mm_pt)
    best = None
    for h in hh:
        for v in vv:
            rect = (h[2], v[2], h[3], v[3])
            if not (rect[0] - tol <= reg_bb[0] and rect[1] - tol <= reg_bb[1] and reg_bb[2] <= rect[2] + tol and
                    reg_bb[3] <= rect[3] + tol):
                continue
            r, ink = slide(rect)
            wm, hm = (r[2] - r[0]) * mm_pt, (r[3] - r[1]) * mm_pt
            if min(ink) < 0.6 or abs(wm - h[0]) > 0.03 * h[0] or abs(hm - v[0]) > 0.03 * v[0]:
                continue
            # rank: the cleanest drawn edges (6200 x 4272 genset 0.98; a grid chain's 6000 slid onto a weak line 0.62;
            # 7200 = 6200 + 2 x 500 foundation band 0.7), then the closest measure (6200 measures 6200), then sides
            # shared with the flooded piece (a pad drawn inside the slab shares none), then chain-placed numbers
            co = sum(abs(a_ - b_) <= 3.0 for a_, b_ in zip(r, reg_bb))
            err = max(abs(wm - h[0]) / h[0], abs(hm - v[0]) / v[0])
            key = (round(min(ink), 1), -round(err * 200), co, -((h[7] == "ocr1") + (v[7] == "ocr1")))
            if __import__("os").environ.get("E1_DEBUG_GRID"):
                print("TRECT", h[0], h[7], v[0], v[7], [round(t, 1) for t in r], [round(t, 2) for t in ink], round(wm), round(hm), key)
            if best is None or key > best[0]:
                best = (key, r, ink, (h[0], "h", r[0], r[2], h[4], h[5], round(wm)), (v[0], "v", r[1], r[3], v[4], v[5],
                                                                                      round(hm)))
    if best is None:
        return None
    _k, r, ink, hd, vd = best
    return {"w_mm": hd[0], "h_mm": vd[0], "area_m2": round(hd[0] * vd[0] / 1e6, 3), "rect": r, "dims": (hd, vd),
            "ink": tuple(round(v, 2) for v in ink), "ocr": True}


def _leader_dims(path: str, pdf: str, page: int, n: float, c, traced_ctx=None) -> Answer | None:
    """The callout sits OUTSIDE its slab with a leader into it (S-100F '300 SLAB-ON-GRADE W/ D22 @ 300' -> the genset
    foundation): the rectangle two printed dimensions span around the leader's end."""
    if traced_ctx is not None:
        try:
            return _traced_leader_answer(path, pdf, page, n, c, *traced_ctx)
        except Exception:  # noqa: BLE001
            return None
    try:
        from stoa.evidence import dimrect as D

        e = D.leader_end(pdf, page, c[4])
        r = D.rect_at(pdf, page, n, e) if e is not None else None
    except Exception:  # noqa: BLE001
        return None
    if r is None:
        return None
    return _dims_answer(path, page, n, c, r, f"the callout's leader ends at ({e[0]:.0f}, {e[1]:.0f}) pt inside it")


_VAL_FTIN = re.compile(r"^\W*(\d{1,3})\s*['’]\s*-?\s*(\d{1,2})?\s*(?:\"|”|''|'|’)?\s*[x×X]\s*(\d{1,3})\s*['’]\s*-?\s*"
                       r"(\d{1,2})?\s*(?:\"|”|''|'|’)?\W*$")
_VAL_MM = re.compile(r"^\W*(\d{3,5})\s*(?:mm)?\s*[x×X]\s*(\d{3,5})\s*(?:mm)?\W*$", re.I)


def extent_product(question: str, ans: Answer) -> Answer:
    """Pipeline hook (E1 block): an AREA question answered with a printed extent STRING ('9'-0' x 9'-11'' from the
    raster reader on D1's AW 078-24-29 PNG; '6200 x 4272') -> its product, in the printed unit (SF / m²), the
    reading kept in the trace. Value and evidence otherwise unchanged."""
    v = ans.value
    if not isinstance(v, str) or not _AREA_Q.search(question) or re.search(r"\b(how many|number of)\b(?!\s+(?:square|sq))",
                                                                          question, re.I):
        return ans
    want_m2 = bool(re.search(r"\b(m²|m2|square met\w*|sq\.? ?m\b|sqm)", question, re.I))
    m = _VAL_FTIN.match(v)
    if m:
        a = int(m.group(1)) + (int(m.group(2)) if m.group(2) else 0) / 12
        b = int(m.group(3)) + (int(m.group(4)) if m.group(4) else 0) / 12
        sf = round(a * b, 2)
        val, unit = (round(sf * FT2_M2, 2), "m2") if want_m2 else (sf, "ft2")
        how = f"{a:g} x {b:.3f} ft = {sf:g} SF"
    else:
        m = _VAL_MM.match(v)
        if not m:
            return ans
        val, unit = round(int(m.group(1)) * int(m.group(2)) / 1e6, 3), "m2"
        how = f"{m.group(1)} x {m.group(2)} mm = {val:g} m²"
    ans.trace = [*ans.trace, f"E1 extent_product: the reading {v!r} is a printed extent: {how}"]
    ans.assumptions = [*ans.assumptions, f"area = the printed extent {v} ({how})"]
    ans.value, ans.unit = val, unit
    return ans


def prefer_slab_area(path: str, question: str, ans: Answer) -> Answer:
    """Pipeline hook (E1 block): a slab-area question answered with no area / a weak area takes the traced slab."""
    callout_q = bool(_SLAB_Q.search(question) and (_THICK_Q.search(question) or _TYPES_Q.search(question) or
                                                    _PANELS_Q.search(question)))
    try:
        v_ok = ans.value is not None and str(ans.unit or "").lower() in ("m2", "m²", "ft2") and \
            (ans.confidence or 0) >= 0.45
    except Exception:  # noqa: BLE001
        v_ok = False
    if v_ok and not callout_q:
        if not (SLAB_WORDING and _SLAB_Q.search(question) and _AREA_Q.search(question)):
            return ans
        # H 23:51 (g): pdf_text '750 m²' (the three thickness labels summed) @0.55 kept although the slabs reader
        # measures 306.79 from printed dimensions -- a dimension-confirmed slab reading (>= 0.5) replaces a different
        # m² value
        b = answer(path, question)
        try:
            def _m2(x):
                return float(x.value) * (FT2_M2 if str(x.unit or "").lower() in ("ft2", "sf", "ft²") else 1.0)
            same = b is not None and abs(_m2(b) - _m2(ans)) <= 0.02 * abs(_m2(b))
        except (TypeError, ValueError):
            same = False
        if b is None or b.confidence < 0.5 or same or b.confidence < (ans.confidence or 0) - 0.15:
            return ans
    else:
        b = answer(path, question)
    if b is None:
        return ans
    b.solver = f"pipeline/{SOLVER}" if str(ans.solver or "").startswith("pipeline/") else SOLVER
    b.cost = b.cost.add(ans.cost) if ans.cost is not None and b.cost is not None else (ans.cost or b.cost)
    b.trace = [*(t for t in ans.trace[:2] if str(t).startswith("recognised:")),
               f"E1 slab_area: replaces {ans.solver}: {str(ans.value)[:40]!r} @{ans.confidence}", *b.trace]
    b.recognition = getattr(ans, "recognition", None)
    return b


__all__ = ["answer", "callouts", "extent_product", "prefer_slab_area"]
