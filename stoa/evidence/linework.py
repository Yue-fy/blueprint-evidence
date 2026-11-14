"""Room outlines traced from a vector PDF's LINEWORK, verified by the printed area (E1).

For sheets that draw rooms with lines only (Revit / AutoCAD exports: no room fills, no CAD twin), the outline of the
room around a printed area tag ('U120 - 1102 SF', 'Zimmer 14.2 m²'):

    from stoa.evidence.linework import outline_for_area
    outline_for_area(pdf_abs, page, (x, y), area_m2, scale_n)
        -> {"polygon": [[x, y], ...] (pdf_pt), "area_m2", "err", "variant", "gap_m", "scale_n"} | None

How: the page's stroked / dark-filled paths are drawn into a bitmap (text is not drawn), in a few variants (every
line; only dark lines; only the heavier dark stroke widths, which drops furniture, fixture and hatch lines). Door
openings are closed by growing the lines by a SCALE-AWARE gap (0 … 0.6 m in the real world, so 1:50 and 1:200 sheets
behave alike), the free region around the tag is flood-filled and grown back. A candidate is kept ONLY when its area
at the sheet's scale agrees with the printed area (±TOL): the printed number says which region is the room, and a
wrong region rarely has the right area. The result is approximate (exact=False wherever it is used); its note says
how well the areas agree.

Guards (each measured): the outline must hold the tag; it must not hold another printed area tag (leaked into the
next room); and at least MIN_SUPPORT readings (line set x gap) must trace the same outline (IoU >= 0.9), because a
leaked region that happens to have the right size changes with the line set / gap.

MEASURED (E1, 14:45; NOT wired into the pipeline):
- Swiss dev sheets vs the DXF room polygons (experiments/e1/linework_area.py 8 --tol 0.10; DXF areas include part
  of the walls, so 10 % here ~ 6 % on printed interior areas): 64 of 200 rooms get an outline, 0 on a wrong room,
  IoU median 0.914 (53 >= 0.9). At a deliberately wrong scale (x1.12) outlines still sit on the right room.
- Real Revit sheet (opening_design CTR A101, 300k paths, unit + room tags 'U116 - 642 SF', furniture, floor patterns;
  experiments/e1/linework_revit.py): only the key plan's 1" = 30'-0" is printed as text, so no usable scale; at
  1/8" = 1'-0" 3 of 39 tags get an outline and 2 of the 3 are visibly wrong (stair outline leaks into a unit);
  wrong scales give 0-2. => not reliable on dense commercial sheets; usable on clean line drawings at a known scale.
"""

from __future__ import annotations

import functools
import re

PT_TO_M_AT_1 = 0.0254 / 72  # 1 pt on paper = this many metres at 1:1
TOL = 0.06  # traced area vs printed area
MAX_SIDE_PX = 4500
GAPS_M = (0.0, 0.08, 0.2, 0.35, 0.5, 0.65)  # half door-opening widths tried, in metres (closes openings up to 1.3 m)
_MAX_PATHS = 600_000


@functools.lru_cache(maxsize=4)
def _page(pdf_abs: str, page: int) -> dict | None:
    """Rasterisable paths of one page: (points px, thickness px, dark, fill) per path item, plus the px/pt factor."""
    import numpy as np
    import pymupdf

    with pymupdf.open(pdf_abs) as doc:
        if page >= len(doc):
            return None
        pg = doc[page]
        W, H = pg.rect.width, pg.rect.height
        drawings = pg.get_drawings()
    if not drawings or len(drawings) > _MAX_PATHS:
        return None
    ppt = min(4.0, MAX_SIDE_PX / max(W, H))
    strokes: list[tuple] = []  # (pts int32 (n,2), width_pt, dark)
    curves: list[tuple] = []  # (pts, width_pt) of Bezier segments
    fills: list = []
    for d in drawings:
        typ = d.get("type") or ""
        col, fill = d.get("color"), d.get("fill")
        w = float(d.get("width") or 0.0)
        dark = bool(col) and sum(col) / len(col) < 0.45
        polys: list[list] = []
        arcs: list[list] = []
        cur: list = []
        for it in d.get("items", []):
            op = it[0]
            if op == "l":
                a, b = it[1], it[2]
                if cur and abs(cur[-1][0] - a.x) < 1e-3 and abs(cur[-1][1] - a.y) < 1e-3:
                    cur.append((b.x, b.y))
                else:
                    if len(cur) >= 2:
                        polys.append(cur)
                    cur = [(a.x, a.y), (b.x, b.y)]
            elif op == "c":
                p0, c1, c2, p3 = it[1], it[2], it[3], it[4]
                pts = []
                for i in range(9):
                    t = i / 8
                    u = 1 - t
                    pts.append((u ** 3 * p0.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t ** 3 * p3.x,
                                u ** 3 * p0.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t ** 3 * p3.y))
                if len(cur) >= 2:
                    polys.append(cur)
                cur = []
                arcs.append(pts)  # door swings / curved walls: kept apart (the 'all' variant draws them)
            elif op == "re":
                r = it[1]
                if len(cur) >= 2:
                    polys.append(cur)
                cur = []
                polys.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)])
            elif op == "qu":
                q = it[1]
                if len(cur) >= 2:
                    polys.append(cur)
                cur = []
                polys.append([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y),
                              (q.ul.x, q.ul.y)])
        if len(cur) >= 2:
            polys.append(cur)
        if "s" in typ and col is not None:
            for p in polys:
                strokes.append((np.round(np.asarray(p, dtype=float) * ppt).astype(np.int32), w, dark,
                                _angle(p) if len(p) == 2 else None, col))
            curves += [(np.round(np.asarray(p, dtype=float) * ppt).astype(np.int32), w) for p in arcs]
        if "f" in typ and fill and sum(fill) / len(fill) < 0.5:  # wall poché; light room fills are not walls
            ring = [q for p in (polys + arcs) for q in p]  # the whole path is one filled region (a hatch's disjoint
            if len(ring) >= 3:                     # segments enclose ~nothing and are dropped here)
                xs, ys = np.asarray(ring, dtype=float).T
                if abs(float(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))) / 2 >= 2.0:
                    fills.append(np.round(np.asarray(ring, dtype=float) * ppt).astype(np.int32))
    # hatching: many straight single segments at one oblique angle with one width / colour (Swiss sheets hatch whole
    # apartments at 45°): they would cut every room into slivers, so they are not walls
    from collections import Counter

    cls = Counter((round(a), round(w, 2), tuple(c)) for _, w, _, a, c in strokes if a is not None and _oblique(a))
    hatch = {k for k, n in cls.items() if n >= HATCH_MIN}
    strokes = [(arr, w, dark) for arr, w, dark, a, c in strokes
               if a is None or not _oblique(a) or (round(a), round(w, 2), tuple(c)) not in hatch]
    return {"W": W, "H": H, "ppt": ppt, "strokes": strokes, "fills": fills, "curves": curves,
            "shape": (int(H * ppt) + 2, int(W * ppt) + 2), "hatch_classes": len(hatch)}


HATCH_MIN = 25


def _angle(seg) -> float:
    import math

    (x0, y0), (x1, y1) = seg[0], seg[-1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180


def _oblique(a: float) -> bool:
    return min(a % 90, 90 - a % 90) > 3


def _variants(pg: dict) -> list[str]:
    """'all', 'dark', then 'w>=X' for the heavier dark stroke widths (most common light widths dropped first)."""
    from collections import Counter

    levels = Counter(round(w, 2) for _, w, dark in pg["strokes"] if dark)
    ws = sorted(w for w, n in levels.items() if n >= 20)
    out = ["all", "dark"]
    out += [f"w>={w}" for w in ws[1:4]]
    return out


DOOR_R_M = (0.45, 1.45)  # door leaf radius (one leaf), metres
LINE_GROW_PT = 1.0  # a room grows into its bounding lines up to their middle, at most this deep (pt)


def _door_spokes(pg: dict, scale_n: float) -> list:
    """Door swings drawn as arcs -> the two radii (hinge -> each arc end), in px: one radius is the open leaf, the other
    the closed position = the doorway itself, so the opening is closed exactly at the wall line and the swing sector
    stays part of the room. An arc counts as a door swing when it is circular, sweeps 30-100 degrees and its radius is a
    door leaf (DOOR_R_M) at the sheet's scale; other curves (curved walls) are left out of this variant."""
    import math

    import numpy as np

    return _door_arcs(pg, scale_n)[0]


def door_radii_m(pdf_abs: str, page: int, scale_n: float) -> list[float]:
    """Radii (m at 1:scale_n) of every door-swing-like arc (circular, 30-100 degrees) between 0.3 and 2.5 m: a door
    leaf is ~0.7-1.0 m, so their median checks the printed scale (X1 wild 'exh222': median 0.45 m at the printed
    1:100 -> the view is really 1:200; LEVEL_11 0.81, German 1:100 sheet 0.90)."""
    import numpy as np

    pg = _page(pdf_abs, page)
    if pg is None or not scale_n:
        return []
    global DOOR_R_M
    keep = DOOR_R_M
    try:
        DOOR_R_M = (0.3, 2.5)
        spokes, _ = _door_arcs(pg, scale_n)
    finally:
        DOOR_R_M = keep
    px_per_m = pg["ppt"] / (scale_n * PT_TO_M_AT_1)
    return sorted(float(np.hypot(*(sp[1] - sp[0]).astype(float))) / px_per_m for sp in spokes[::2])


def _door_arcs(pg: dict, scale_n: float) -> tuple[list, set]:
    """(spokes px, {stroke index: [(first, last) point index of an arc run]} for door arcs drawn as polylines: CAD
    exports draw a door as ONE path 'arc as ~20 short segments + leaf + doorway chord' (X1's German sheet))."""
    import math

    import numpy as np

    px_per_m = pg["ppt"] / (scale_n * PT_TO_M_AT_1)
    rmin, rmax = DOOR_R_M[0] * px_per_m, DOOR_R_M[1] * px_per_m
    out: list = []
    arc_ids: dict = {}
    items = [(None, 0, np.asarray(arr, dtype=float)) for arr, _w in pg["curves"]]
    for i, (arr, _w, _d) in enumerate(pg["strokes"]):
        if len(arr) < 6:
            continue
        P = np.asarray(arr, dtype=float)
        seg = np.hypot(*(P[1:] - P[:-1]).T)
        if not len(seg):
            continue
        short = seg <= 3 * float(np.median(seg)) + 1e-9  # an arc drawn as many short segments; the leaf / doorway
        j = 0                                            # lines of the same path are long ones
        while j < len(seg):
            if not short[j]:
                j += 1
                continue
            k = j
            while k < len(seg) and short[k]:
                k += 1
            if k - j >= 4:
                items.append((i, j, P[j:k + 1]))
            j = k
    for sid, j0, pts in items:
        if len(pts) < 5:
            continue
        (ax, ay), (bx, by), (cx, cy) = pts[0], pts[len(pts) // 2], pts[-1]
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-6:
            continue
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
        rr = np.hypot(pts[:, 0] - ux, pts[:, 1] - uy)
        r = float(rr.mean())
        if not rmin <= r <= rmax or float(rr.max() - rr.min()) > 0.08 * r + 2:
            continue
        a0, a1 = math.atan2(ay - uy, ax - ux), math.atan2(cy - uy, cx - ux)
        sweep = abs((math.degrees(a1 - a0) + 180) % 360 - 180)
        if not 30 <= sweep <= 100:
            continue
        c = (int(round(ux)), int(round(uy)))
        out.append(np.array([c, (int(ax), int(ay))], dtype=np.int32))
        out.append(np.array([c, (int(cx), int(cy))], dtype=np.int32))
        if sid is not None:
            arc_ids.setdefault(sid, []).append((j0, j0 + len(pts) - 1))  # point index range of the arc's segments
    return out, arc_ids


@functools.lru_cache(maxsize=6)
def _dist(pdf_abs: str, page: int, variant: str):
    """Distance (px, clipped at 255) from every pixel to the nearest drawn line of the variant; 0 on the lines."""
    import cv2
    import numpy as np

    pg = _page(pdf_abs, page)
    m = np.zeros(pg["shape"], dtype=np.uint8)
    variant, _, dsuf = variant.partition("|")  # '<base>|doors@N': the base line set with door swings -> radii
    doors = dsuf.startswith("doors@")
    wmin = float(variant[3:]) if variant.startswith("w>=") else None
    by_th: dict[int, list] = {}
    arc_ids: dict = {}
    if doors:  # every stroke, door swings replaced by their two radii (_door_arcs)
        spokes, arc_ids = _door_arcs(pg, float(dsuf[6:]))
        by_th.setdefault(max(1, int(round(0.25 * pg["ppt"]))), []).extend(spokes)
    for i, (a, w, dark) in enumerate(pg["strokes"]):
        if i in arc_ids:  # the path's other segments (leaf, doorway chord) are kept
            keep = [a[j:j + 2] for j in range(len(a) - 1) if not any(j0 <= j < j1 for j0, j1 in arc_ids[i])]
            by_th.setdefault(max(1, int(round(w * pg["ppt"]))), []).extend(keep)
            continue
        if variant != "all" and not dark:
            continue
        if wmin is not None and round(w, 2) < wmin:
            continue
        by_th.setdefault(max(1, int(round(w * pg["ppt"]))), []).append(a)
    if variant == "all" and not doors:  # door swings close a sector off each room; only 'all' keeps curves
        for a, w in pg["curves"]:
            by_th.setdefault(max(1, int(round(w * pg["ppt"]))), []).append(a)
    for th, arrs in by_th.items():
        cv2.polylines(m, arrs, False, 255, thickness=th, lineType=cv2.LINE_8)
    if pg["fills"]:
        cv2.fillPoly(m, pg["fills"], 255)
    d = cv2.distanceTransform((m == 0).astype(np.uint8), cv2.DIST_L2, 3)
    return np.minimum(d, 255).astype(np.uint8)


def _region(dist, seed, k: int):
    """Free region around `seed` after growing the lines by k px, grown back by k, holes (furniture drawn as closed
    outlines inside the room) filled; (mask, bbox (x0, y0, x1, y1) px) or None when it leaks to the page edge."""
    import cv2
    import numpy as np

    h, w = dist.shape
    x, y = seed
    if not (0 <= x < w and 0 <= y < h):
        return None
    free = (dist > k).astype(np.uint8)
    if not free[y, x]:  # the tag sits on / near a line: nearest free pixel within a small window
        r = 2 * k + 4
        y0, y1, x0, x1 = max(0, y - r), min(h, y + r + 1), max(0, x - r), min(w, x + r + 1)
        cand = np.argwhere(free[y0:y1, x0:x1] > 0)
        if not len(cand):
            return None
        d = (cand[:, 0] + y0 - y) ** 2 + (cand[:, 1] + x0 - x) ** 2
        fy, fx = cand[int(np.argmin(d))]
        x, y = int(fx + x0), int(fy + y0)
    ff = np.zeros((h + 2, w + 2), dtype=np.uint8)
    _, _, _, rect = cv2.floodFill(free, ff, (x, y), 2, flags=4 | (1 << 8) | cv2.FLOODFILL_MASK_ONLY)
    rx, ry, rw, rh = rect
    if rx <= 0 or ry <= 0 or rx + rw >= w or ry + rh >= h:
        return None
    m = k + 2
    X0, Y0, X1, Y1 = max(0, rx - m), max(0, ry - m), min(w, rx + rw + m), min(h, ry + rh + m)
    reg = ff[1 + Y0:1 + Y1, 1 + X0:1 + X1].copy()
    if k:
        dd = cv2.distanceTransform((reg == 0).astype(np.uint8), cv2.DIST_L2, 3)
        reg = ((dd <= k) & (dist[Y0:Y1, X0:X1] > 0)).astype(np.uint8)
    cs, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    filled = np.zeros_like(reg)
    cv2.drawContours(filled, [c], -1, 1, thickness=-1)
    return filled, c + np.array([[X0, Y0]]), (X0, Y0, X1, Y1)


def _polygon(contour, ppt: float) -> list | None:
    import cv2

    c = cv2.approxPolyDP(contour, 1.0, True)
    if len(c) < 3:
        return None
    pts = [[round(float(p[0][0]) / ppt, 2), round(float(p[0][1]) / ppt, 2)] for p in c]
    return pts + [pts[0]]


_AREA_TAG = re.compile(r"(?<![\d.,])(\d{1,3}(?:[,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*"
                       r"(SF|sq\.?\s*ft|ft²|m²|m2|M2|qm|㎡)(?![a-z])", re.I)


def _num(s: str) -> float:
    s = s.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        return float(s.replace(",", ""))
    return float(s.replace(",", "."))


@functools.lru_cache(maxsize=16)
def area_tags(pdf_abs: str, page: int = 0) -> tuple:
    """Every printed area on the page: ((m², text line, bbox pdf_pt), ...) ('U120 - 1102 SF', 'OH 25,5 m²')."""
    import pymupdf

    with pymupdf.open(pdf_abs) as doc:
        if page >= len(doc):
            return ()
        blocks = doc[page].get_text("dict").get("blocks", [])
    out = []
    for b in blocks:
        for ln in b.get("lines", []):
            text = " ".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            for m in _AREA_TAG.finditer(text):
                try:
                    v = _num(m.group(1))
                except ValueError:
                    continue
                unit = m.group(2).lower()
                m2 = v * 0.09290304 if unit.startswith(("sf", "sq", "ft")) else v
                if m2 > 0:
                    out.append((round(m2, 3), text, tuple(round(x, 2) for x in ln["bbox"])))
    return tuple(out)


def outline_for_area(pdf_abs: str, page: int, pt: tuple[float, float], area_m2: float, scale_n: float,
                     tol: float = TOL, tag_box: tuple | None = None, others: list | None = None) -> dict | None:
    """The traced outline of the room around `pt` whose area at 1:scale_n agrees with `area_m2`, or None.

    `tag_box` (pdf_pt): the printed tag; seeds just outside it are tried too (tags drawn inside their own frame).
    `others` (pdf_pt points): other rooms' tags; an outline that holds one of them leaked into that room and is
    dropped (default: every other printed area tag on the page)."""
    import cv2

    if not area_m2 or area_m2 <= 0 or not scale_n or scale_n <= 0:
        return None
    pg = _page(pdf_abs, page)
    if pg is None:
        return None
    ppt = pg["ppt"]
    m_per_px = scale_n * PT_TO_M_AT_1 / ppt
    seeds = [(int(pt[0] * ppt), int(pt[1] * ppt))]
    if tag_box:
        x0, y0, x1, y1 = tag_box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for d in (3.0, 12.0):  # just outside the tag, then past a frame / table the tag was placed on
            seeds += [(int(cx * ppt), int((y0 - d) * ppt)), (int(cx * ppt), int((y1 + d) * ppt)),
                      (int((x0 - d) * ppt), int(cy * ppt)), (int((x1 + d) * ppt), int(cy * ppt))]
    if others is None:
        others = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for _, _, b in area_tags(pdf_abs, page)]
    others_px = [(float(x * ppt), float(y * ppt)) for x, y in others
                 if (x - pt[0]) ** 2 + (y - pt[1]) ** 2 > 4.0 ** 2
                 and not (tag_box and tag_box[0] - 1 <= x <= tag_box[2] + 1 and tag_box[1] - 1 <= y <= tag_box[3] + 1)]
    variants = _variants(pg)
    cands = []  # every (variant, gap) whose outline passes: (gap, seed, err, area, contour, variant, shapely poly)
    for gap in GAPS_M:
        k = int(round(gap / m_per_px))
        if gap and k < 1:
            continue
        if k > 250:
            break
        for variant in variants:
            dist = _dist(pdf_abs, page, variant)
            for si, seed in enumerate(seeds):
                r = _region(dist, seed, k)
                if r is None:
                    continue
                filled, contour, _ = r
                if cv2.pointPolygonTest(contour, (float(seeds[0][0]), float(seeds[0][1])), False) < 0:
                    continue  # a seed that slipped into the next room (or past it): the outline must hold the tag
                if any(cv2.pointPolygonTest(contour, q, False) >= 0 for q in others_px):
                    continue  # leaked into a room with its own tag
                a = float(filled.sum()) * m_per_px ** 2
                err = abs(a / area_m2 - 1)
                if err <= tol:
                    cands.append((gap, si, err, a, contour, variant, _shape(contour)))
                if si == 0 and a >= 0.5 * area_m2:
                    break  # the tag is not boxed in: no need for the outside seeds
    # stability: a coincidental match (a region that leaked somewhere and happens to have the right size) changes with
    # the line set / gap; the room itself is traced the same way by at least MIN_SUPPORT readings
    best = None
    for c in cands:
        support = sum(1 for d in cands if d is not c and d[5:7] != c[5:7] and _iou(c[6], d[6]) >= 0.9)
        key = (support >= MIN_SUPPORT - 1, -c[0], c[1] == 0, support, -c[2])
        if best is None or key > best[0]:
            best = (key, c, support)
    if best is None or not best[0][0]:
        return None
    gap, si, err, a, contour, variant, _ = best[1]
    poly = _polygon(contour, ppt)
    if not poly:
        return None
    return {"polygon": poly, "area_m2": round(a, 2), "err": round(err, 4), "variant": variant, "gap_m": gap,
            "scale_n": scale_n, "seed": si, "support": best[2] + 1}


MIN_SUPPORT = 2  # readings (line set x gap) that trace the same outline (IoU >= 0.9), the chosen one included


def _shape(contour):
    from shapely.geometry import Polygon

    pts = contour.reshape(-1, 2)
    if len(pts) < 3:
        return None
    P = Polygon(pts)
    return P if P.is_valid else P.buffer(0)


def _iou(a, b) -> float:
    if a is None or b is None:
        return 0.0
    try:
        u = a.union(b).area
        return a.intersection(b).area / u if u else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


@functools.lru_cache(maxsize=16)
def sheet_scales(pdf_abs: str, page: int = 0) -> tuple[float, ...]:
    """Every drawing scale printed on the page ('1:100', 1/8" = 1'-0" -> 96), most frequent first."""
    from collections import Counter

    import pymupdf

    from stoa.solvers.raster.textlayer import parse_scales

    with pymupdf.open(pdf_abs) as doc:
        if page >= len(doc):
            return ()
        blocks = doc[page].get_text("dict").get("blocks", [])
    c: Counter = Counter()
    for b in blocks:
        for ln in b.get("lines", []):
            text = "".join(s.get("text", "") for s in ln.get("spans", []))
            for s in parse_scales(text):
                if 10 <= s["ratio"] <= 2000:
                    c[float(s["ratio"])] += 1
    return tuple(n for n, _ in c.most_common())


_LABEL_WORD = re.compile(r"[^\W\d_]{3,}")


@functools.lru_cache(maxsize=16)
def text_lines(pdf_abs: str, page: int = 0) -> tuple:
    """((text, bbox pdf_pt), ...) of every text line on the page."""
    import pymupdf

    with pymupdf.open(pdf_abs) as doc:
        if page >= len(doc):
            return ()
        blocks = doc[page].get_text("dict").get("blocks", [])
    return tuple((" ".join(s.get("text", "") for s in ln.get("spans", [])).strip(), tuple(ln["bbox"]))
                 for b in blocks for ln in b.get("lines", []))


def measure_room(pdf_abs: str, page: int, pt: tuple[float, float], scale_n: float, tag_box: tuple | None = None,
                 own_radius_pt: float = 25.0) -> dict | None:
    """A room's outline and area MEASURED around its label when no area is printed (line-only CAD sheets, X1 14:31).
    X1 wild LEVEL_11 (A2, 1:100): comms room 16.41 m² (gold 16.83, -2.5 %), long side 5.24 m (5.253), conference room
    7.72 m (7.73). KNOWN: a side across a door opening bulges by up to the gap (grow-back after the flood fill; synthetic
    +5 %) — next: close the lines morphologically (dilate + erode) BEFORE the flood fill instead. NOT wired.

    of every traced region that holds the label, holds no other word label (the next room's name) and does not run
    off the page, the one most readings (line set x gap) agree on (IoU >= 0.9). Returns {"polygon", "area_m2",
    "support", "n_readings", "gap_m", "variant", "sides_m"} or None. Approximate (exact=False): wall faces, door
    openings closed at `gap_m` x 2."""
    import cv2
    import numpy as np

    if not scale_n or scale_n <= 0:
        return None
    pg = _page(pdf_abs, page)
    if pg is None:
        return None
    ppt = pg["ppt"]
    m_per_px = scale_n * PT_TO_M_AT_1 / ppt
    seeds = [(int(pt[0] * ppt), int(pt[1] * ppt))]
    others_px = []
    for text, b in text_lines(pdf_abs, page):
        c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        if not _LABEL_WORD.search(text) or (c[0] - pt[0]) ** 2 + (c[1] - pt[1]) ** 2 <= own_radius_pt ** 2:
            continue
        if tag_box and tag_box[0] - 2 <= c[0] <= tag_box[2] + 2 and tag_box[1] - 2 <= c[1] <= tag_box[3] + 2:
            continue
        others_px.append((float(c[0] * ppt), float(c[1] * ppt)))
    variants = _variants(pg)
    if _door_spokes(pg, scale_n):  # every line set with the door swings replaced by their radii (see _door_arcs)
        variants = [f"{v}|doors@{scale_n:g}" for v in variants]
    cands = []
    for gap in GAPS_M:
        k = int(round(gap / m_per_px))
        if (gap and k < 1) or k > 250:
            continue
        for variant in variants:
            r = _region(_dist(pdf_abs, page, variant), seeds[0], k)
            if r is None:
                continue
            filled, contour, _ = r
            if cv2.pointPolygonTest(contour, (float(seeds[0][0]), float(seeds[0][1])), False) < 0:
                continue
            if any(cv2.pointPolygonTest(contour, q, False) >= 0 for q in others_px):
                continue
            a = float(filled.sum()) * m_per_px ** 2
            if not 0.8 <= a <= 3000:
                continue
            cands.append((gap, variant, a, contour, _shape(contour), filled, r[2]))
    if not cands:
        return None
    best = None
    for c in cands:
        support = 1 + sum(1 for d in cands if d is not c and _iou(c[4], d[4]) >= 0.9)
        key = (support, -c[0])
        if best is None or key > best[0]:
            best = (key, c)
    support = best[0][0]
    # within the agreeing readings, the largest one: a door swing drawn as an arc cuts a sector off the room in the
    # readings that keep curves (X1 comms room 16.01 -> 16.6 m² of 16.83)
    cluster = [d for d in cands if d is best[1] or _iou(best[1][4], d[4]) >= 0.9]
    # door swings closed at the doorway line (no sector cut off, no bulge into the opening): that reading, if it agrees
    dr = [d for d in cluster if "doors@" in str(d[1])]
    pick = min(dr, key=lambda d: (d[0], -d[2])) if dr else max(cluster, key=lambda d: d[2])
    gap, variant, a, contour, shp, filled, (X0, Y0, _, _) = pick
    # the drawn line eats into the room: half its thickness + the boundary pixel, measured to the line's middle
    # (= the wall face a CAD line stands for): X1 comms room sides 5.188 x 3.128 -> 5.240 x 3.181 (gold 5.253 x 3.204)
    # E1 15:55 (X1: German rooms 2-3 % short): the room grows into ITS OWN bounding lines up to their middle — each
    # line pixel goes to the nearer free side (this room vs the other side), at most LINE_GROW_PT deep — so a 1.25 pt
    # wall line is corrected by its own half width, not by the sheet's median stroke (hairlines: 1 px)
    ws = sorted(w for _, w, dark in pg["strokes"] if variant.split("|")[0] == "all" or dark)
    th = max(1, int(round((ws[len(ws) // 2] if ws else 0.5) * ppt)))
    e = max(int(-(-(th / 2 + 0.5) // 1)), int(round(LINE_GROW_PT * ppt)))
    dist_full = _dist(pdf_abs, page, variant)
    H_, W_ = dist_full.shape
    y0p, x0p = max(0, Y0 - e - 1), max(0, X0 - e - 1)
    y1p, x1p = min(H_, Y0 + filled.shape[0] + e + 1), min(W_, X0 + filled.shape[1] + e + 1)
    room = np.zeros((y1p - y0p, x1p - x0p), np.uint8)
    room[Y0 - y0p:Y0 - y0p + filled.shape[0], X0 - x0p:X0 - x0p + filled.shape[1]] = filled
    dcrop = dist_full[y0p:y1p, x0p:x1p]
    line = dcrop == 0
    other = (dcrop > 0) & (room == 0)
    d_room = cv2.distanceTransform((room == 0).astype(np.uint8), cv2.DIST_L2, 3)
    d_other = cv2.distanceTransform((~other).astype(np.uint8), cv2.DIST_L2, 3)
    grown = (room > 0) | (line & (d_room <= d_other + 0.5) & (d_room <= e))
    filled = cv2.copyMakeBorder(grown.astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    X0, Y0, e = x0p, y0p, 1
    cs, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    contour = max(cs, key=cv2.contourArea) + np.array([[X0 - e, Y0 - e]])
    a = float(filled.sum()) * m_per_px ** 2
    shp = _shape(contour)
    poly = _polygon(contour, ppt)
    if not poly:
        return None
    sides = None
    try:
        r = shp.minimum_rotated_rectangle
        xy = list(r.exterior.coords)[:4]
        d = sorted(((xy[i][0] - xy[i + 1][0]) ** 2 + (xy[i][1] - xy[i + 1][1]) ** 2) ** 0.5 for i in range(2))
        sides = [round((v + 1) * m_per_px, 3) for v in reversed(d)]  # long, short (m); +1 px: pixel centres
    except Exception:  # noqa: BLE001
        pass
    return {"polygon": poly, "area_m2": round(a, 2), "support": support, "n_readings": len(cands), "gap_m": gap,
            "variant": variant, "sides_m": sides, "scale_n": scale_n}
