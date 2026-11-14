"""Printed DIMENSION STRINGS on vector CAD sheets: each number paired with its own dimension line, and the rectangle
two of them dimension around a point (E1, 14 Nov cycle 7).

Stoa's sheets dimension what they draw ('6200' and '4272' around the S-100F genset foundation; '1900' / '2054' round
the A-101F toilet), and a person answering 'how big is it' reads those numbers, not a ruler: the printed value wins
over the measured one (site rule: never scale a drawing). A number counts as a dimension only when the line it sits on
measures it at the printed 1:N: the piece of that line between the arrow heads / extension lines on either side of the
number, within DIM_TOL (10 %: the A-101F toilet's 2054 is drawn 1914 long).

    dims(pdf, page, n)            -> ((value_mm, 'h'|'v', a_pt, b_pt, line_pt, text_bbox), ...)
    rect_at(pdf, page, n, (x, y)) -> {"w_mm", "h_mm", "area_m2", "rect", "dims": (h, v), "ink": (4 edge shares)} | None
                                     the nearest horizontal and vertical dimension whose spans hold the point, kept
                                     only when the rectangle they span is drawn (ink along each of its 4 edges)
    leader_end(pdf, page, bbox)   -> the far end (pt) of a leader line drawn from a text block, or None
"""

from __future__ import annotations

import functools
import math
import re

DIM_TOL = 0.10
EDGE_INK = 0.4  # share of each rectangle edge with ink on it (dashed slab edges / isolation joints: ~0.5)
_NUM = re.compile(r"^\d{3,5}$")


@functools.lru_cache(maxsize=4)
def _geometry(pdf_abs: str, page: int) -> tuple:
    """(horizontal segs [(y, x0, x1)], vertical segs [(x, y0, y1)], arrow heads [(x0, y0, x1, y1)], words) in pt."""
    import pymupdf

    hs, vs, heads = [], [], []
    with pymupdf.open(pdf_abs) as d:
        if page >= len(d):
            return (), (), (), ()
        p = d[page]
        words = tuple(w[:5] for w in p.get_text("words"))
        for dr in p.get_drawings():
            r = dr["rect"]
            if dr.get("fill") is not None and max(r.width, r.height) <= 16 and r.width * r.height <= 80:
                heads.append((r.x0, r.y0, r.x1, r.y1))
            if dr.get("color") is None and dr.get("fill") is None:
                continue
            for it in dr["items"]:
                if it[0] != "l":
                    if it[0] == "re":
                        q = it[1]
                        hs += [(q.y0, q.x0, q.x1), (q.y1, q.x0, q.x1)]
                        vs += [(q.x0, q.y0, q.y1), (q.x1, q.y0, q.y1)]
                    continue
                a, b = it[1], it[2]
                if abs(a.y - b.y) <= 0.3 and abs(a.x - b.x) > 0.8:
                    hs.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
                elif abs(a.x - b.x) <= 0.3 and abs(a.y - b.y) > 0.8:
                    vs.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
    return tuple(hs), tuple(vs), tuple(heads), words


def _dim_of(word, hs, vs, heads, mm_per_pt):
    x0, y0, x1, y1, t = word
    horiz = (x1 - x0) >= (y1 - y0)
    if not horiz:  # work in a frame where the dimension runs along u: swap axes
        hs, vs = vs, hs
        heads = [(b, a, d, c) for a, b, c, d in heads]
        x0, y0, x1, y1 = y0, x0, y1, x1
    cu, cv = (x0 + x1) / 2, (y0 + y1) / 2
    tw, th = x1 - x0, y1 - y0
    # the dimension line: the nearest run of collinear segments (within ~1.3 text heights) that passes the number
    rows: dict = {}
    for v, a, b in hs:
        if abs(v - cv) <= 1.3 * th + 2 and b >= x0 - 60 and a <= x1 + 60:
            rows.setdefault(round(v * 4) / 4, []).append((a, b))
    tried = 0
    for v in sorted(rows, key=lambda v: abs(v - cv)):
        iv = sorted(rows[v])
        runs, cur = [], list(iv[0])
        for a, b in iv[1:]:
            if a <= cur[1] + tw + 6:  # the number may sit in a gap of its own line
                cur[1] = max(cur[1], b)
            else:
                runs.append(cur)
                cur = [a, b]
        runs.append(cur)
        hit = [r for r in runs if r[0] - 1 <= cu <= r[1] + 1 and r[1] - r[0] >= tw]
        if not hit:
            continue
        # the nearest line may be another one (A-101F: a dashed outline 6 pt above '1900', its dimension line 11.5 pt
        # below): the first of the 3 nearest lines whose piece measures the number
        got = _measure(v, hit[0], iv, vs, heads, cu, tw, t, mm_per_pt, horiz, (x0, y0, x1, y1))
        if got is not None:
            return got
        tried += 1
        if tried >= 3:
            break
    return None


def _measure(v, run, iv, vs, heads, cu, tw, t, mm_per_pt, horiz, box):
    x0, y0, x1, y1 = box
    ra, rb = run
    # breaks on that line: a dimension CHAIN drawn one segment per piece (S-100F: 318-560 | 560-577 | 577-859 pt for
    # 4272 | 300 | 4975), extension lines / ticks crossing it, arrow-head tips on it; segment ends next to the number
    # are the gap the number sits in
    left, right = [ra], [rb]
    for a, b in iv:
        for e in (a, b):
            if ra - 0.5 <= e <= rb + 0.5 and abs(e - cu) > tw / 2 + 4:
                (left if e < cu else right).append(e)
    for u, a, b in vs:  # an extension line / tick ENDS at the line (a wall or outline drawn across it does not)
        if a - 1.0 <= v <= b + 1.0 and ra - 1 <= u <= rb + 1 and (b - a) >= 2.0 and min(abs(a - v), abs(b - v)) <= 4.0:
            (left if u < cu - tw / 2 else right if u > cu + tw / 2 else []).append(u)
    on_line = [(a, c) for a, b, c, d in heads if b - 1.5 <= v <= d + 1.5]

    def ext(e, side):  # a line that stops at its arrow head's base: the tip is the measured point
        for a, c in on_line:
            if side < 0 and abs(c - e) <= 1.5 and a < e - 1.0:
                return a
            if side > 0 and abs(a - e) <= 1.5 and c > e + 1.0:
                return c
        return e

    # the breaks either side of the number, nearest first; several sources (chain segment ends, extension lines,
    # arrow tips; dashes of an outline may touch the line too): the pair whose span measures the number is its piece
    ls = sorted(set(left), reverse=True)[:4]
    rs = sorted(set(right))[:4]
    val = int(t)
    cands = []
    for i_l, l0 in enumerate(ls):
        for i_r, r0 in enumerate(rs):
            for lo in {l0, ext(l0, -1)}:
                for hi in {r0, ext(r0, 1)}:
                    if hi > lo:
                        cands.append((abs((hi - lo) * mm_per_pt - val), i_l + i_r, lo, hi))
    if not cands:
        return None
    err, _rank, lo, hi = min(cands)
    meas = (hi - lo) * mm_per_pt
    if err > DIM_TOL * val:
        return None
    return (val, "h" if horiz else "v", lo, hi, v, (x0, y0, x1, y1) if horiz else (y0, x0, y1, x1), round(meas))


@functools.lru_cache(maxsize=8)
def dims(pdf_abs: str, page: int, n: float) -> tuple:
    from stoa.evidence import linework as L

    hs, vs, heads, words = _geometry(pdf_abs, page)
    mm_per_pt = n * L.PT_TO_M_AT_1 * 1000
    out = []
    for w in words:
        if not _NUM.match(w[4]):
            continue
        try:
            dm = _dim_of(w, hs, vs, heads, mm_per_pt)
        except Exception:  # noqa: BLE001
            dm = None
        if dm is not None:
            out.append(dm)
    return tuple(out)


def _edge_ink(pdf_abs: str, page: int, rect) -> tuple:
    from stoa.evidence import linework as L

    d = L.ink_dist(pdf_abs, page)
    pg = L._page(pdf_abs, page)
    if d is None or pg is None:
        return (0.0, 0.0, 0.0, 0.0)
    ppt = pg["ppt"]
    H, W = d.shape
    x0, y0, x1, y1 = rect
    out = []
    for (ax, ay), (bx, by) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x0, y1), (x1, y1)), ((x0, y0), (x0, y1))):
        k, on = 60, 0
        for i in range(k):
            t = (i + 0.5) / k
            xi, yi = int(round((ax + (bx - ax) * t) * ppt)), int(round((ay + (by - ay) * t) * ppt))
            if 0 <= xi < W and 0 <= yi < H and d[yi, xi] <= 2.5:
                on += 1
        out.append(round(on / k, 2))
    return tuple(out)


def rect_at(pdf_abs: str, page: int, n: float, pt) -> dict | None:
    x, y = pt
    ds = dims(pdf_abs, page, n)
    hh = [d for d in ds if d[1] == "h" and d[2] - 1 <= x <= d[3] + 1]
    vv = [d for d in ds if d[1] == "v" and d[2] - 1 <= y <= d[3] + 1]
    if not hh or not vv:
        return None
    for h in sorted(hh, key=lambda d: abs(d[4] - y))[:2]:
        for v in sorted(vv, key=lambda d: abs(d[4] - x))[:2]:
            rect = (h[2], v[2], h[3], v[3])
            ink = _edge_ink(pdf_abs, page, rect)
            if min(ink) >= EDGE_INK:
                return {"w_mm": h[0], "h_mm": v[0], "area_m2": round(h[0] * v[0] / 1e6, 3), "rect": rect,
                        "dims": (h, v), "ink": ink}
    return None


def rect_spanning(pdf_abs: str, page: int, n: float, bbox, tol_pt: float = 4.0) -> dict | None:
    """The printed dimensions whose spans coincide with a traced outline's extent (each end within tol_pt): a
    horizontal one for its width and a vertical one for its height."""
    x0, y0, x1, y1 = bbox
    ds = dims(pdf_abs, page, n)
    hh = [d for d in ds if d[1] == "h" and abs(d[2] - x0) <= tol_pt and abs(d[3] - x1) <= tol_pt]
    vv = [d for d in ds if d[1] == "v" and abs(d[2] - y0) <= tol_pt and abs(d[3] - y1) <= tol_pt]
    if not hh or not vv:
        return None
    h = min(hh, key=lambda d: abs(d[2] - x0) + abs(d[3] - x1))
    v = min(vv, key=lambda d: abs(d[2] - y0) + abs(d[3] - y1))
    rect = (h[2], v[2], h[3], v[3])
    return {"w_mm": h[0], "h_mm": v[0], "area_m2": round(h[0] * v[0] / 1e6, 3), "rect": rect, "dims": (h, v),
            "ink": _edge_ink(pdf_abs, page, rect)}


def leader_end(pdf_abs: str, page: int, bbox, min_len: float = 15.0, margin: float = 30.0):
    """Far end of the longest leader that starts near a text block: one line or a polyline of up to 4 segments with
    at least one OBLIQUE segment (walls and dimension lines are square to the sheet). The S-100F D22 callout's leader
    starts 27 pt off its first text block (at the callout's second block), runs up a 39 pt shoulder, then 144 pt
    diagonally into the genset slab."""
    import pymupdf

    bx0, by0, bx1, by1 = bbox
    segs = []
    with pymupdf.open(pdf_abs) as d:
        p = d[page]
        for dr in p.get_drawings():
            if dr.get("color") is None:
                continue
            for it in dr["items"]:
                if it[0] == "l":
                    segs.append(((it[1].x, it[1].y), (it[2].x, it[2].y)))

    def near_box(q, m=margin):
        return bx0 - m <= q[0] <= bx1 + m and by0 - m <= q[1] <= by1 + m

    def inside(q):
        return bx0 + 1 <= q[0] <= bx1 - 1 and by0 + 1 <= q[1] <= by1 - 1

    best, best_len = None, 0.0
    for a, b in segs:
        for s, e in ((a, b), (b, a)):
            if not near_box(s) or near_box(e, 1.0) or inside(s):
                continue
            pts, ln = [s, e], math.dist(s, e)
            for _ in range(3):  # follow a bent leader
                nxt = [(c, f) for c, f in segs if math.dist(c, pts[-1]) <= 0.8 and math.dist(f, pts[-2]) > 0.8] + \
                      [(f, c) for c, f in segs if math.dist(f, pts[-1]) <= 0.8 and math.dist(c, pts[-2]) > 0.8]
                if len(nxt) != 1:
                    break
                pts.append(nxt[0][1])
                ln += math.dist(nxt[0][0], nxt[0][1])
            oblique = any(5 <= abs(math.degrees(math.atan2(q[1] - p_[1], q[0] - p_[0]))) % 90 <= 85
                          for p_, q in zip(pts, pts[1:]))
            if oblique and ln >= min_len and ln > best_len and not near_box(pts[-1], 1.0):
                best, best_len = pts[-1], ln
    return best


__all__ = ["dims", "rect_at", "rect_spanning", "leader_end", "DIM_TOL"]
