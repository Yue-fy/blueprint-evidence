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
import math
import re

from stoa.contract import Answer, Evidence

SOLVER = "slab_area"
_SLAB_Q = re.compile(r"\b(slabs?|slab-on-grade|sog|mat foundation|foundation pad|equipment pad|bodenplatte|"
                     r"betonplatte|laatta|platta)\b", re.I)
_AREA_Q = re.compile(r"\b(area|how (?:big|large)|size|square|m²|m2|sq\.? ?(?:m|ft)|fläche|pinta-ala)\b", re.I)
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
        for b in d[page].get_text("blocks"):
            if b[6] != 0:
                continue
            t = " ".join(str(b[4]).split())
            if not re.search(r"\bSLAB", t, re.I):
                continue
            m = _THK.search(t)
            mi = None if m else _IMP_THK.search(t)
            bars = tuple(_norm_bar(x) for x in _BAR.findall(t))
            sp = tuple(int(x) for x in _SPACING.findall(t))
            out.append((t, int(m.group(1)) if m else _imp_mm(mi) if mi else None, bars, sp, tuple(b[:4])))
    return tuple(out)


def _pick(cs: tuple, q: str):
    qt = {int(a or b) for a, b in _THK_Q.findall(q)} | {int(x) for x in _THK.findall(q)} | \
        {_imp_mm(m) for m in _IMP_THK.finditer(q)}
    qb = {_norm_bar(x) for x in _BAR.findall(q)}
    qs = {int(x) for x in _SPACING.findall(q)}
    if not (qt or qb):
        return None
    hits = [c for c in cs if (not qt or c[1] in qt) and (not qb or qb <= set(c[2])) and (not qs or qs <= set(c[3]))
            and (c[1] is not None or c[2])]
    if not hits and qs:  # the spacing may be printed in the callout's next text block ('… W/ D22' / '@ 300 TOP OF …')
        hits = [c for c in cs if (not qt or c[1] in qt) and (not qb or qb <= set(c[2])) and (c[1] is not None or c[2])]
    return hits[0] if len(hits) == 1 else None


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
    m_per_pt = n * L.PT_TO_M_AT_1
    with pymupdf.open(pdf_abs) as d:
        p = d[page]
        ws = p.get_text("words")
        blocks = [b for b in p.get_text("blocks") if b[6] == 0]
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
    for a, w, _dark in pg["strokes"]:
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

    m = np.zeros(pg["shape"], np.uint8)
    widths, joints = [], []
    for a, w, _dark in pg["strokes"]:
        P = np.asarray(a, float) / ppt
        ln = LineString(P)
        if ln.length * m_per_pt > 2 and any(ln.distance(c) < 20 for c in cj):
            joints.append((np.asarray(a, np.int32), w))
            continue  # a control joint divides a slab into panels
        x0, y0, x1, y1 = ln.bounds
        if any(box(b[0] - 12, b[1] - 12, b[2] + 12, b[3] + 12).contains(ln) and
               ((x1 - x0) > 0.8 * (b[2] - b[0]) or (y1 - y0) > 0.8 * (b[3] - b[1])) for b in blocks):
            continue  # a frame around a callout / note
        keep = []
        for i in range(len(P) - 1):
            s = segs[0].__class__([P[i], P[i + 1]])
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
            r"\b(how many|number of)\b(?!\s+(?:square|sq))", q, re.I):
        pdf = str(abspath(path))
        return _label_dims(path, pdf, page, q) if pdf.lower().endswith(".pdf") else None
    if not _SLAB_Q.search(q) or not (_PANELS_Q.search(q) or (_AREA_Q.search(q) and not re.search(
            r"\b(how many|number of)\b(?!\s+(?:square|sq))", q, re.I))):
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
    if not (0 <= seed[0] < W and 0 <= seed[1] < H) or m[seed[1], seed[0]]:
        return _leader_dims(path, pdf, page, n, c) if area_q else None
    ff = m.copy()
    flood = np.zeros((H + 2, W + 2), np.uint8)
    cv2.floodFill(ff, flood, seed, 128)
    reg = (ff == 128).astype(np.uint8)
    ys, xs = np.nonzero(reg)
    if not len(xs) or xs.min() <= 1 or ys.min() <= 1 or xs.max() >= W - 2 or ys.max() >= H - 2:
        # ran off the sheet: not a closed slab -- the callout may sit OUTSIDE its slab with a leader into it
        return _leader_dims(path, pdf, page, n, c) if area_q else None
    cnts, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ct = max(cnts, key=cv2.contourArea)
    full = np.zeros_like(reg)
    cv2.drawContours(full, [ct], -1, 1, -1)
    per_px = cv2.arcLength(ct, True)
    a_m2 = (float(full.sum()) + per_px * (wmed * ppt / 2 + 0.5)) * m_per_px ** 2
    if not 2.0 <= a_m2 <= 5000:
        return _leader_dims(path, pdf, page, n, c) if area_q else None
    for o in cs:  # another slab type's callout inside: the region leaked across a slab edge
        if o is c or o[1] is None or (o[1] == c[1] and o[2] == c[2]):
            continue  # notes ('TOP OF SLAB EL.', a rebar note) are not another slab type
        ox, oy = int((o[4][0] + o[4][2]) / 2 * ppt), int((o[4][1] + o[4][3]) / 2 * ppt)
        if 0 <= ox < W and 0 <= oy < H and full[oy, ox]:
            return _leader_dims(path, pdf, page, n, c) if area_q else None
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
    if r is not None and abs(r["area_m2"] - a_m2) <= 0.05 * a_m2:
        # the traced slab IS the rectangle two printed dimensions span (S-100F D13: 9950 x 6200 = 61.69 vs traced
        # 60.77): the printed numbers are the answer
        return _dims_answer(path, page, n, c, r, f"the slab traced around its callout ({val} m², extent {bx:.2f} x "
                                                 f"{by:.2f} m) is the rectangle these dimensions span", poly)
    ev = [Evidence(file=path, page=page, bbox=[min(xsd), min(ysd), max(xsd), max(ysd)], bbox_space="pdf_pt",
                   exact=False, polygon=poly, note=f"{name}: {val} m² traced at 1:{n:g} (approximate)"),
          Evidence(file=path, page=page, bbox=[round(v, 1) for v in c[4]], bbox_space="pdf_pt", exact=True,
                   note=f"slab callout: {name}")]
    return Answer(value=val, unit="m2", confidence=0.4, evidence=ev, solver=SOLVER,
                  assumptions=[f"scale: 1:{n:g} printed on the sheet; slab traced from the drawn lines (approximate)",
                               f"slab type = the printed callout '{name}'",
                               "slab = the region around that callout bounded by slab edges / beams; control joints "
                               f"(CJ), dimension lines and text frames ignored; extent {bx:.2f} x {by:.2f} m"],
                  missing=["slab areas are not printed: the dimension strings along the slab edges would make it exact"],
                  trace=[f"slab_area: {len(cs)} slab callouts on the sheet; '{name}' matches the question; region "
                         f"{val} m² (extent {bx:.2f} x {by:.2f} m) at 1:{n:g}"])


_ITEM_Q = re.compile(r"\b(foundations?|footings?|pads?|plinths?|pits?|sumps?|platforms?|pedestals?|"
                     r"fundament\w*|perustu\w*)\b", re.I)
_Q_STOP = {"how", "big", "large", "is", "are", "the", "a", "an", "in", "on", "of", "plan", "what", "area", "size",
           "its", "this", "sheet", "drawing", "square", "metres", "meters", "feet", "m2", "sf", "total", "floor", "much",
           "does", "take", "up", "cover", "footprint"}


def _label_dims(path: str, pdf: str, page: int, q: str) -> Answer | None:
    """'How big is the genset foundation in plan?' (X1 sl-016, S-100F): the ONE text block naming every content word
    of the question, a leader from it into the drawing, and the rectangle two printed dimensions span there."""
    import pymupdf

    from stoa.evidence import linework as L

    qw = [w for w in re.findall(r"[a-zà-ÿ0-9]+", q.lower()) if w not in _Q_STOP and len(w) >= 3]
    if not qw:
        return None
    with pymupdf.open(pdf) as d:
        if page >= len(d):
            return None
        blocks = [b for b in d[page].get_text("blocks") if b[6] == 0]
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
    a = _leader_dims(path, pdf, page, n, c)
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
    terms = " + ".join(f"{a.value:g} ({c[0][:28]})" for c, a in parts)
    ev = [e for _, a in parts for e in a.evidence]
    conf = round(min(0.45, min(a.confidence for _, a in parts)), 3)
    return Answer(value=total, unit="m2", confidence=conf, evidence=ev, solver=SOLVER,
                  assumptions=[f"total slab area = the sum of the {len(parts)} slab types printed on the sheet: {terms} "
                               f"= {total} m²",
                               "each slab measured to its edge (printed dimensions where they span it, else traced at "
                               "the printed scale); openings deducted only where drawn as voids"],
                  missing=[m for _, a in parts for m in a.missing][:2],
                  trace=[f"slab_area: total of {len(parts)} slab types: {terms} = {total} m²",
                         *(f"slab_area: [{c[0][:30]}] {a.trace[-1][:160]}" for c, a in parts)])


def _dims_answer(path: str, page: int, n: float, c, r: dict, how: str, poly=None) -> Answer:
    (hv, *_h), (vv, *_v) = r["dims"]
    h, v = r["dims"]
    x0, y0, x1, y1 = r["rect"]
    name = c[0][:60]
    val = r["area_m2"]
    ev = [Evidence(file=path, page=page, bbox=[round(t, 1) for t in h[5]], bbox_space="pdf_pt", exact=True,
                   note=f"dimension {hv} mm (printed; measures {h[6]} mm at 1:{n:g})"),
          Evidence(file=path, page=page, bbox=[round(t, 1) for t in v[5]], bbox_space="pdf_pt", exact=True,
                   note=f"dimension {vv} mm (printed; measures {v[6]} mm at 1:{n:g})"),
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
                  trace=[f"slab_area: '{name[:50]}': {how}; dimensions {hv} x {vv} mm (each measures its own line at "
                         f"1:{n:g}: {h[6]} / {v[6]} mm; edges inked {r['ink']}) = {val} m²"])


def _leader_dims(path: str, pdf: str, page: int, n: float, c) -> Answer | None:
    """The callout sits OUTSIDE its slab with a leader into it (S-100F '300 SLAB-ON-GRADE W/ D22 @ 300' -> the genset
    foundation): the rectangle two printed dimensions span around the leader's end."""
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
        return ans
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
