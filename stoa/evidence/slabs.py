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
_BAR = re.compile(r"\b([#D]\s?\d{1,2})\b", re.I)
_SPACING = re.compile(r"@\s*(\d{2,4})")
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
            bars = tuple(_norm_bar(x) for x in _BAR.findall(t))
            sp = tuple(int(x) for x in _SPACING.findall(t))
            out.append((t, int(m.group(1)) if m else None, bars, sp, tuple(b[:4])))
    return tuple(out)


def _pick(cs: tuple, q: str):
    qt = {int(a or b) for a, b in _THK_Q.findall(q)}
    qb = {_norm_bar(x) for x in _BAR.findall(q)}
    qs = {int(x) for x in _SPACING.findall(q)}
    if not (qt or qb):
        return None
    hits = [c for c in cs if (not qt or c[1] in qt) and (not qb or qb <= set(c[2])) and (not qs or qs <= set(c[3]))
            and (c[1] is not None or c[2])]
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
    widths = []
    for a, w, _dark in pg["strokes"]:
        P = np.asarray(a, float) / ppt
        ln = LineString(P)
        if ln.length * m_per_pt > 2 and any(ln.distance(c) < 20 for c in cj):
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
    return m, ppt, (sorted(widths)[len(widths) // 2] if widths else 0.25)


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader never breaks the cascade
        return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    import cv2
    import numpy as np

    from stoa.data import abspath
    from stoa.evidence import linework as L

    q = question
    if not (_SLAB_Q.search(q) and _AREA_Q.search(q)) or re.search(r"\b(how many|number of)\b(?!\s+(?:square|sq))",
                                                                   q, re.I):
        return None
    pdf = str(abspath(path))
    if not pdf.lower().endswith(".pdf"):
        return None
    cs = callouts(pdf, page)
    c = _pick(cs, q) if cs else None
    if c is None:
        return None
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return None
    n = scales[0]
    mk = _mask(pdf, page, n)
    if mk is None:
        return None
    m, ppt, wmed = mk
    m_per_px = n * L.PT_TO_M_AT_1 / ppt
    x0, y0, x1, y1 = c[4]
    seed = (int((x0 + x1) / 2 * ppt), int((y0 + y1) / 2 * ppt))
    H, W = m.shape
    if not (0 <= seed[0] < W and 0 <= seed[1] < H) or m[seed[1], seed[0]]:
        return None
    ff = m.copy()
    flood = np.zeros((H + 2, W + 2), np.uint8)
    cv2.floodFill(ff, flood, seed, 128)
    reg = (ff == 128).astype(np.uint8)
    ys, xs = np.nonzero(reg)
    if not len(xs) or xs.min() <= 1 or ys.min() <= 1 or xs.max() >= W - 2 or ys.max() >= H - 2:
        return None  # ran off the sheet: not a closed slab
    cnts, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ct = max(cnts, key=cv2.contourArea)
    full = np.zeros_like(reg)
    cv2.drawContours(full, [ct], -1, 1, -1)
    per_px = cv2.arcLength(ct, True)
    a_m2 = (float(full.sum()) + per_px * (wmed * ppt / 2 + 0.5)) * m_per_px ** 2
    if not 2.0 <= a_m2 <= 5000:
        return None
    for o in cs:  # another slab type's callout inside: the region leaked across a slab edge
        if o is c or o[1] is None or (o[1] == c[1] and o[2] == c[2]):
            continue  # notes ('TOP OF SLAB EL.', a rebar note) are not another slab type
        ox, oy = int((o[4][0] + o[4][2]) / 2 * ppt), int((o[4][1] + o[4][3]) / 2 * ppt)
        if 0 <= ox < W and 0 <= oy < H and full[oy, ox]:
            return None
    eps = 1.5
    poly = [[round(float(x) / ppt, 2), round(float(y) / ppt, 2)] for x, y in
            cv2.approxPolyDP(ct, eps, True)[:, 0, :]]
    if len(poly) < 3:
        return None
    xsd, ysd = [p[0] for p in poly], [p[1] for p in poly]
    bx, by = (max(xsd) - min(xsd)) * n * L.PT_TO_M_AT_1, (max(ysd) - min(ysd)) * n * L.PT_TO_M_AT_1
    val = round(a_m2, 2)
    name = c[0][:60]
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


def prefer_slab_area(path: str, question: str, ans: Answer) -> Answer:
    """Pipeline hook (E1 block): a slab-area question answered with no area / a weak area takes the traced slab."""
    try:
        v_ok = ans.value is not None and str(ans.unit or "").lower() in ("m2", "m²", "ft2") and \
            (ans.confidence or 0) >= 0.45
    except Exception:  # noqa: BLE001
        v_ok = False
    if v_ok:
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


__all__ = ["answer", "callouts", "prefer_slab_area"]
