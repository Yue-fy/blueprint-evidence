"""The scale of a vector PDF plan estimated from its door swings (CPU, no model, ~0.1 s per page).

Called by web/mock/stoaApi.ts (GET /api/doorscale?path=&page=), one process per request:

    $JUNCTION_VENV/bin/python web/server/door_scale.py /tmp/stoa-uploads/ab12/plan.pdf 0

Why: Stoa's visitors upload plans with no printed scale (X1's Australian house PDFs: labels only), and the captain
wants a best-estimate number with 'Scale unknown — set it' next to it, never an empty answer (14:39). A door swing is
drawn as a quarter circle whose radius is the door leaf, and door leaves are ~0.85 m wide almost everywhere (0.8-0.9 m
interior doors), so the most common quarter-circle radius on the sheet gives the scale. It also catches a printed scale
that is wrong for the sheet (X1 14:31: '222 Exhibition St' prints 1:100, its door swings say 1:200).
How: quarter arcs are found both as Bezier curves (PyMuPDF 'c' items: radius from chord and tangent turn) and as
flattened polylines (CAD exports draw arcs as 8-30 short lines turning by the same angle each step); swept angle
75-105 deg, radius 4-130 pt (a 0.85 m door from 1:20 to 1:500). The radii's densest cluster (+-10 %, at least 3 arcs)
is taken as the door leaf; scale 1:N = 0.85 m / radius, snapped to a common scale (1:50, 1:100, 1:200, 1:500) when
within 22 %, else to another standard one within 12 %. Swiss dataset sheets (60, printed 1:100 / 1:200): 59 estimated,
52 within 15 % of the printed scale before snapping, the rest 15-31 % (door leaves 0.7-1.0 m); snapped: see the bench.
Output: {"ok": true, "ratio": N (snapped), "raw": N (unsnapped), "doors": k (arcs in the cluster), "arcs": all quarter
arcs, "radius_pt", "boxes": [[x0, y0, x1, y1], ...] (pdf_pt, the cluster's door swings, ≤ 40), "leaf_m": 0.85} or
{"ok": false, "why": "..."}.
"""

from __future__ import annotations

import json
import math
import sys

LEAF_M = 0.85
PT_PER_MM = 72 / 25.4
STANDARD = [10, 20, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500]
COMMON = [50, 100, 200, 500]


def _turn(t0: tuple[float, float], t1: tuple[float, float]) -> float:
    return math.atan2(t0[0] * t1[1] - t0[1] * t1[0], t0[0] * t1[0] + t0[1] * t1[1])


def _bezier_arcs(items):
    """Chains of consecutive cubic curves with the same radius: (radius, sweep_deg, bbox)."""
    out = []
    chain = []

    def flush():
        if not chain:
            return
        tot, rs, xs, ys = 0.0, [], [], []
        for p0, c1, c2, p3 in chain:
            t0 = (c1.x - p0.x, c1.y - p0.y)
            t1 = (p3.x - c2.x, p3.y - c2.y)
            if math.hypot(*t0) < 1e-6 or math.hypot(*t1) < 1e-6:
                return
            a = _turn(t0, t1)
            if abs(a) < 1e-3:
                return
            rs.append(math.hypot(p3.x - p0.x, p3.y - p0.y) / (2 * math.sin(abs(a) / 2)))
            tot += a
            xs += [p0.x, p3.x]
            ys += [p0.y, p3.y]
        m = sum(rs) / len(rs)
        if max(rs) - min(rs) > 0.16 * m:
            return
        p0, c1, c2, p3 = chain[len(chain) // 2]
        mid = ((p0.x + 3 * c1.x + 3 * c2.x + p3.x) / 8, (p0.y + 3 * c1.y + 3 * c2.y + p3.y) / 8)  # B(0.5) of the middle curve
        ends = ((chain[0][0].x, chain[0][0].y), mid, (chain[-1][3].x, chain[-1][3].y))
        out.append((m, abs(math.degrees(tot)), (min(xs), min(ys), max(xs), max(ys)), ends))

    for it in items:
        if it[0] == "c":
            chain.append(it[1:5])
        else:
            flush()
            chain = []
    flush()
    return out


def _polyline_arcs(items):
    """Flattened arcs: runs of >= 4 connected short lines, each turning the same way by 1-25 deg, similar lengths."""
    out = []
    pts = []

    def scan(pts):
        if len(pts) < 5:
            return
        segs = [(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1)]
        lens = [math.hypot(*s) for s in segs]
        turns = [math.degrees(_turn(segs[i], segs[i + 1])) if lens[i] > 1e-6 and lens[i + 1] > 1e-6 else 0.0 for i in range(len(segs) - 1)]
        i = 0
        while i < len(turns):
            sgn = 1 if turns[i] > 0 else -1
            j = i
            while j < len(turns) and 1.0 <= sgn * turns[j] <= 25 and 0.5 <= lens[j + 1] / max(lens[i], 1e-9) <= 2.0:
                j += 1
            n = j - i  # turns in the run; the run has n + 1 segments
            if n >= 3:
                d = sum(abs(t) for t in turns[i:j]) / n
                sweep = d * (n + 1)
                seg = sum(lens[i:j + 1]) / (n + 1)
                r = seg / (2 * math.sin(math.radians(d) / 2))
                xs = [p[0] for p in pts[i:j + 2]]
                ys = [p[1] for p in pts[i:j + 2]]
                out.append((r, sweep, (min(xs), min(ys), max(xs), max(ys)), (pts[i], pts[(i + j + 1) // 2], pts[j + 1])))
                i = j
            else:
                i += 1

    for it in items:
        if it[0] == "l":
            a, b = (it[1].x, it[1].y), (it[2].x, it[2].y)
            if pts and abs(pts[-1][0] - a[0]) < 0.05 and abs(pts[-1][1] - a[1]) < 0.05:
                pts.append(b)
            else:
                scan(pts)
                pts = [a, b]
        else:
            scan(pts)
            pts = []
    scan(pts)
    return out


def _centre(a, b, c):
    """Circumcentre of three points (None when they are in a line)."""
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-9:
        return None
    sa, sb, sc = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    return ((sa * (b[1] - c[1]) + sb * (c[1] - a[1]) + sc * (a[1] - b[1])) / d,
            (sa * (c[0] - b[0]) + sb * (a[0] - c[0]) + sc * (b[0] - a[0])) / d)


def _segments(drawings):
    """Every straight piece on the page (lines, rectangle and quad edges): door leaves are drawn as one of them."""
    segs = []
    for d in drawings:
        for it in d["items"]:
            if it[0] == "l":
                segs.append(((it[1].x, it[1].y), (it[2].x, it[2].y)))
            elif it[0] == "re":
                r = it[1]
                c = [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
                segs += [(c[k], c[(k + 1) % 4]) for k in range(4)]
            elif it[0] == "qu":
                q = it[1]
                c = [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y)]
                segs += [(c[k], c[(k + 1) % 4]) for k in range(4)]
    return segs


def _rounded_corner(r, ends, index, cell) -> bool:
    """Both ends of the arc continue into a straight piece along the arc's tangent there (a box's rounded corner)."""
    c = _centre(*ends)
    if c is None:
        return False
    tol = max(0.08 * r, 0.6)

    def goes_on(e) -> bool:
        rx, ry = e[0] - c[0], e[1] - c[1]  # radial; the tangent is perpendicular to it
        n = math.hypot(rx, ry) or 1.0
        gx, gy = int(e[0] // cell), int(e[1] // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for p, o in index.get((gx + dx, gy + dy), ()):
                    if math.hypot(p[0] - e[0], p[1] - e[1]) > tol:
                        continue
                    vx, vy = o[0] - p[0], o[1] - p[1]
                    m = math.hypot(vx, vy)
                    # along the tangent (not radial), leaving the arc (pointing away from the centre's side of e)
                    if m >= 0.3 * r and abs(vx * rx + vy * ry) / (m * n) <= 0.2:
                        return True
        return False

    return goes_on(ends[0]) and goes_on(ends[-1])


def _has_leaf(r, ends, index, cell):
    """A door swing's leaf: a straight piece from the hinge (the arc's centre) to one end of the arc (+-15 % of r)."""
    c = _centre(*ends)
    if c is None or abs(math.hypot(c[0] - ends[0][0], c[1] - ends[0][1]) / r - 1) > 0.2:
        return False
    tol = max(0.15 * r, 0.8)
    gx, gy = int(c[0] // cell), int(c[1] // cell)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for p, q in index.get((gx + dx, gy + dy), ()):
                if math.hypot(p[0] - c[0], p[1] - c[1]) > tol:
                    continue
                if abs(math.hypot(q[0] - p[0], q[1] - p[1]) / r - 1) > 0.2:
                    continue
                if min(math.hypot(q[0] - e[0], q[1] - e[1]) for e in (ends[0], ends[2])) <= tol:
                    return True
    return False


def estimate(path: str, page: int = 0) -> dict:
    import pymupdf

    with pymupdf.open(path) as doc:
        if not len(doc):
            return {"ok": False, "why": "empty PDF"}
        pg = doc[min(max(page, 0), len(doc) - 1)]
        drawings = pg.get_drawings()
        arcs = []
        for d in drawings:
            arcs += _bezier_arcs(d["items"]) + _polyline_arcs(d["items"])
    # one arc drawn twice (a fill and a stroke, two layers) counts once
    seen, q = set(), []
    for r, s, b, ends in arcs:
        k = tuple(round(v) for v in b)
        if 75 <= s <= 105 and 4 <= r <= 130 and k not in seen:
            seen.add(k)
            q.append((r, b, ends))
    # door swings have a leaf (a line from the hinge); seats, rounded corners and fixtures do not. Where the sheet draws
    # leaves, only arcs with one count (X1's Italian cinema hall: ~100 small seat-row arcs said 1:500, the sheet is 1:100)
    cell = 8.0
    index: dict = {}
    for sg in _segments(drawings):
        for p, o in (sg, sg[::-1]):
            index.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append((p, o))
    # a rounded corner (title-block and legend boxes: USACE S-100F / A-101F, 20 corners of 9.0 pt said 1:250 on a 1:50
    # sheet) runs on into straight lines along its tangent at BOTH ends; a door swing does not (its leaf is radial)
    q = [x for x in q if not _rounded_corner(x[0], x[2], index, cell)]
    leafed = [(r, b) for r, b, ends in q if _has_leaf(r, ends, index, cell)]
    if len(leafed) >= 3 or len(leafed) >= 0.2 * len(q):
        q = leafed
    else:
        q = [(r, b) for r, b, _ in q]
    if len(q) < 3:
        return {"ok": False, "why": f"{len(q)} door swing{'s' if len(q) != 1 else ''} found (need 3)", "arcs": len(q)}
    # the densest cluster of radii (+-10 %); ties: the larger radius (cupboard doors are smaller than room doors)
    best = None
    for r0, _ in q:
        n = sum(1 for r, _ in q if abs(r / r0 - 1) <= 0.10)
        if best is None or n > best[0] or (n == best[0] and r0 > best[1]):
            best = (n, r0)
    n, r0 = best
    if n < 3:
        return {"ok": False, "why": f"door swings of {len(q)} different sizes", "arcs": len(q)}
    member = sorted([(r, b) for r, b in q if abs(r / r0 - 1) <= 0.10], key=lambda x: x[0])
    rad = member[len(member) // 2][0]
    raw = LEAF_M * 1000 / (rad / PT_PER_MM)
    common = min(COMMON, key=lambda s: abs(math.log(raw / s)))
    snap = min(STANDARD, key=lambda s: abs(math.log(raw / s)))
    ratio = common if abs(math.log(raw / common)) <= math.log(1.22) else snap if abs(raw / snap - 1) <= 0.12 else round(raw)
    return {"ok": True, "ratio": ratio, "raw": round(raw, 1), "doors": len(member), "arcs": len(q), "radius_pt": round(rad, 2),
            "leaf_m": LEAF_M, "boxes": [[round(v, 1) for v in b] for _, b in member[:40]]}


def main() -> None:
    path = sys.argv[1]
    page = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    try:
        out = estimate(path, page)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"could not read the PDF ({type(e).__name__})"}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
