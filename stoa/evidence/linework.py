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
# D1 c7 (23:18): pages larger than ANSI D / A1 (A0, ANSI E: long side > 2600 pt) are rendered at least as finely as
# an A2 sheet at MAX_SIDE_PX (2.67 px/pt). At 4500 px an A0 sheet at 1:200 is 5.3 cm of ground per px and rooms come
# out ~2-3 % small (DfE Secondary GA plans: dfe26 rows 46 -> 82 / 118, 0 losses; A2 / ANSI D / A1 unchanged).
BIG_PAGE_PT = 2600
MIN_PPT_BIG = MAX_SIDE_PX / 1684
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
        # paths and words come in UNROTATED page coordinates: a /Rotate 90 sheet (X1 stoa-like USACE S-100F, rect
        # 2384 x 1684, paths up to y = 2384) needs the unrotated size, else its lower part falls off the raster
        W, H = (pg.cropbox.width, pg.cropbox.height) if pg.rotation % 180 else (pg.rect.width, pg.rect.height)
        drawings = pg.get_drawings()
        traced = (TRACED_FILLS or TRACED_DASHES) and _traced_doc_page(pg, drawings)
    if not drawings or len(drawings) > _MAX_PATHS:
        return None
    ppt = min(4.0, MAX_SIDE_PX / max(W, H))
    if max(W, H) > BIG_PAGE_PT:  # A0 / ANSI E: at least A2's resolution (D1 23:18)
        ppt = max(ppt, MIN_PPT_BIG)
    strokes: list[tuple] = []  # (pts int32 (n,2), width_pt, dark)
    curves: list[tuple] = []  # (pts, width_pt) of Bezier segments
    fills: list = []
    fill_lines: list = []  # thin filled strips / rings = lines (THIN_FILL_PT)
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
        if "f" in typ and fill and sum(fill) / len(fill) < 0.5 and THIN_FILL_PT > 0 and len(polys) >= 2 and not arcs:
            tr = _thin_ring(polys)
            if tr is not None:  # a court / frame LINE drawn as an even-odd ring (outer + inner loop 0.35 pt apart)
                fill_lines.append((np.round(np.asarray(tr[0], dtype=float) * ppt).astype(np.int32),
                                   max(1, int(round(tr[1] * ppt)))))  # (outer loop px, band width px)
                continue
        if "f" in typ and fill and sum(fill) / len(fill) < 0.5:  # wall poché; light room fills are not walls
            ring = [q for p in (polys + arcs) for q in p]  # the whole path is one filled region (a hatch's disjoint
            if len(ring) >= 3:                     # segments enclose ~nothing and are dropped here)
                xs, ys = np.asarray(ring, dtype=float).T
                if abs(float(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))) / 2 >= 2.0:
                    if THIN_FILL_PT > 0 and _thin_strip(ring) is not None:
                        # X1 23:20 (EFA superblock Sports Hall): court markings drawn as 0.3-0.4 pt FILLED strips (= 50 mm
                        # court lines at 1:400) are LINES, not wall poché: drawn in every line set, but not in the walls-
                        # only reading 'wall@N' (as 'walls' they closed it around the label: 63.9 of 594 m²)
                        fill_lines.append((np.round(np.asarray(ring, dtype=float) * ppt).astype(np.int32), 0))
                        continue
                    fills.append(np.round(np.asarray(ring, dtype=float) * ppt).astype(np.int32))
    # hatching: many straight single segments at one oblique angle with one width / colour (Swiss sheets hatch whole
    # apartments at 45°): they would cut every room into slivers, so they are not walls
    from collections import Counter

    cls = Counter((round(a), round(w, 2), tuple(c)) for _, w, _, a, c in strokes if a is not None and _oblique(a))
    hatch = {k for k, n in cls.items() if n >= HATCH_MIN}
    strokes = [(arr, w, dark) for arr, w, dark, a, c in strokes
               if a is None or not _oblique(a) or (round(a), round(w, 2), tuple(c)) not in hatch]
    dashes = []
    if traced and TRACED_DASHES:  # kept apart, not lost: a slab plan's DASHED edges bound slabs (slabs._mask)
        td = _traced_dashes(strokes, ppt)
        dashes = [x for i, x in enumerate(strokes) if i in td]
        strokes = [x for i, x in enumerate(strokes) if i not in td]
    if DROP_DASHED:
        strokes = _drop_dashed(strokes, ppt, fills=fills, shape=(int(H * ppt) + 2, int(W * ppt) + 2))
    if traced and TRACED_FILLS and not fills:
        fills = _thick_ink_fills(strokes, (int(H * ppt) + 2, int(W * ppt) + 2), ppt)
    return {"W": W, "H": H, "ppt": ppt, "strokes": strokes, "fills": fills, "curves": curves, "dashes": dashes,
            "shape": (int(H * ppt) + 2, int(W * ppt) + 2), "hatch_classes": len(hatch), "traced": bool(traced),
            "fill_lines": fill_lines}


# OFF by default (E1 cycle 9): the rebuilt fills win 3 EFA renders (Sports Hall, Studio, Kitchen) but grow Stoa's DfE
# PNG laundry 6.97 -> 7.24 (gold 7.0) and fitness 26.97 -> 27.39 via the bridge reading -> check before turning on
TRACED_FILLS = bool(int(__import__("os").environ.get("E1_TRACED_FILLS", "0")))
TRACED_DASHES = bool(int(__import__("os").environ.get("E1_TRACED_DASHES", "1")))
# X1 23:20 (EFA primary PNG, 1:200): grid-line dashes 4.8-5 pt with 4.5-4.8 pt gaps (4.0 missed them; the hall was cut
# into 59 of its 180 m²)
TRACED_DASH_GAP_PT = float(__import__("os").environ.get("E1_TRACED_DASH_GAP_PT", "6.0"))
TRACED_WALL_PT = float(__import__("os").environ.get("E1_TRACED_WALL_PT", 1.0))  # ink at least this wide = a wall fill


THIN_FILL_PT = float(__import__("os").environ.get("E1_THIN_FILL_PT", "0.5"))  # 0 = every dark fill is a wall


def _thin_strip(ring):
    """A filled polygon that is a LINE: its minimum rotated rectangle is < THIN_FILL_PT wide and >= 10x as long ->
    (its centre line [(x, y), (x, y)] in pt, its width in pt), else None."""
    import cv2
    import numpy as np

    pts = np.asarray(ring, dtype=np.float32)
    (cx, cy), (w, h), ang = cv2.minAreaRect(pts)
    short, long_ = min(w, h), max(w, h)
    if short >= THIN_FILL_PT or long_ < 10 * max(short, 0.05) or long_ < 3.0:
        return None
    import math
    a = math.radians(ang if w >= h else ang + 90)
    dx, dy = math.cos(a) * long_ / 2, math.sin(a) * long_ / 2
    return [(cx - dx, cy - dy), (cx + dx, cy + dy)], float(max(short, 0.1))


def _thin_ring(polys):
    """A filled path of >= 2 closed loops whose band (outer area - inner areas) is < THIN_FILL_PT wide on average
    (band / outer perimeter): a line drawn as a ring -> (outer loop points in pt, band width in pt), else None."""
    import numpy as np

    loops = []
    for p in polys:
        a = np.asarray(p, dtype=float)
        if len(a) < 4 or np.hypot(*(a[0] - a[-1])) > 0.5:
            return None
        xs, ys = a.T
        loops.append((abs(float(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))) / 2, a))
    loops.sort(key=lambda t: -t[0])
    (ao, outer), inner = loops[0], loops[1:]
    x0, y0 = outer.min(0)
    x1, y1 = outer.max(0)
    if any(not (x0 - 0.5 <= a[:, 0].min() and a[:, 0].max() <= x1 + 0.5 and y0 - 0.5 <= a[:, 1].min() and
                a[:, 1].max() <= y1 + 0.5) for _, a in inner):
        return None
    band = ao - sum(ai for ai, _ in inner)
    per = float(np.hypot(*np.diff(outer, axis=0).T).sum())
    if per <= 0 or band <= 0 or band / per >= THIN_FILL_PT or ao < 4.0:
        return None
    return [tuple(q) for q in outer], float(max(band / per, 0.1))


def _traced_doc_page(pg, drawings) -> bool:
    """A picture traced into lines (stoa.formats.vectorize): invisible text only, no image, every path one pen."""
    try:
        tt = pg.get_texttrace()
        if not tt or any(t.get("type") != 3 for t in tt) or pg.get_images():
            return False
    except Exception:  # noqa: BLE001
        return False
    return len({round(d.get("width") or 0, 3) for d in drawings}) == 1


def _thick_ink_fills(strokes, shape, ppt: float) -> list:
    """Wall FILLS for a traced copy (X1 19:20, E1 19:45: a picture of a solid-wall sheet comes back as ink OUTLINES
    only, so no solid-wall reading ran: Studio 47.79 vs 55.6 on the PDF). The ink is recovered from its outlines, the
    parts at least TRACED_WALL_PT wide are kept (an opening by a disk: thin lines, hatch, dimension lines vanish) and
    each blob becomes one fill ring; a blob with holes (a room closed by thick walls all round) is spliced into one
    ring through a zero-width slit per hole, so per-piece code never fills a room."""
    import cv2
    import numpy as np

    ink = np.zeros(shape, np.uint8)
    rings = [a for a, _w, _d in strokes if len(a) >= 3]
    if not rings:
        return []
    cv2.fillPoly(ink, rings, 1)  # outlines of one blob (outer + its holes) fill even-odd = the blob's ink
    r = max(1, int(round(TRACED_WALL_PT * ppt / 2)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    thick = cv2.morphologyEx(ink, cv2.MORPH_OPEN, k)
    if thick.sum() == 0:
        return []
    cs, hier = cv2.findContours(thick, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    hier = hier[0]
    out = []
    min_area = (2.0 * ppt) ** 2
    for i, c in enumerate(cs):
        if hier[i][3] != -1 or cv2.contourArea(c) < min_area:
            continue  # holes are spliced into their outer ring below
        ring = c[:, 0, :].astype(np.int64)
        j = hier[i][2]
        while j != -1:
            h = cs[j][:, 0, :].astype(np.int64)
            if len(h) >= 3:
                d = ((ring[:, None, :] - h[None, :, :]) ** 2).sum(-1)
                a, b = np.unravel_index(int(np.argmin(d)), d.shape)
                ring = np.concatenate([ring[:a + 1], h[b:], h[:b + 1], ring[a:]])
            j = hier[j][0]
        out.append(ring.astype(np.int32))
    return out


HATCH_MIN = 25
DROP_DASHED = bool(int(__import__("os").environ.get("E1_DROP_DASHED", "1")))


def _traced_dashes(strokes: list, ppt: float, max_piece_pt: float = 8.0, max_gap_pt: float = TRACED_DASH_GAP_PT,
                   min_run: int = 6) -> set:
    """Indices of DASH outlines on a traced copy (E1 19:58, X1 sc-215 Studio: the dash-dot grid line across the room
    closed it at 5.86 m instead of 6.76; on the PDF _drop_dashed drops those dashes, but a traced dash is a small
    closed OUTLINE, not a one-segment piece). A small thin outline (<= 8 pt long, <= 1.5 pt wide) on an axis-parallel
    line is one piece; >= 6 such pieces on one line with gaps <= 4 pt are a dashed line, and the dots between them go
    with it."""
    import cv2
    import numpy as np

    groups: dict = {}
    dots: list = []
    for i, (arr, _w, _dark) in enumerate(strokes):
        if len(arr) < 3:
            continue
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(np.asarray(arr, np.float32) / ppt)
        L_, T_ = max(rw, rh), min(rw, rh)
        if L_ > max_piece_pt or T_ > 1.5:
            continue
        if L_ <= 1.6:
            dots.append((cx, cy, i))
            continue
        a = (ang if rw >= rh else ang + 90.0) % 180.0
        if min(a, 180 - a) <= 5:
            horiz = True
        elif abs(a - 90) <= 5:
            horiz = False
        else:
            continue
        t, off = (cx, cy) if horiz else (cy, cx)
        groups.setdefault((horiz, round(off / 1.5)), []).append((t - L_ / 2, t + L_ / 2, i, off))
    drop: set = set()
    lines = []
    for (horiz, _b), pieces in groups.items():
        if len(pieces) < min_run:
            continue
        pieces.sort()
        run = [pieces[0]]
        for p_ in pieces[1:] + [(float("inf"), float("inf"), -1, 0.0)]:
            if p_[2] >= 0 and p_[0] - run[-1][1] <= max_gap_pt:
                run.append(p_)
                continue
            for sub in _regular_runs(run, min_run):
                drop.update(q[2] for q in sub)
                lines.append((horiz, float(np.mean([q[3] for q in sub])), sub[0][0], sub[-1][1]))
            run = [p_]
    for cx, cy, i in dots:
        for horiz, off, t0, t1 in lines:
            t, o = (cx, cy) if horiz else (cy, cx)
            if abs(o - off) <= 1.0 and t0 - 1 <= t <= t1 + 1:
                drop.add(i)
                break
    return drop


def _regular_runs(run: list, min_run: int, base_gap: float = 4.0) -> list:
    """A run that needs gaps above base_gap (TRACED_DASH_GAP_PT) is a dashed line only when its pieces and gaps are
    REGULAR (a printed dash pattern: EFA primary PNG 4.8-5 pt dashes, 4.5-4.8 pt gaps); a solid line a scan broke into
    uneven pieces (EFA scan PNG 'Learning Resource Centre' south wall) is split at the base_gap gaps instead and each
    part judged as before."""
    import numpy as np

    if len(run) < min_run:
        return []
    gaps = [b[0] - a[1] for a, b in zip(run, run[1:])]
    if max(gaps) <= base_gap:
        return [run]
    ln = np.array([q[1] - q[0] for q in run])
    g = np.array(gaps)
    if ln.mean() > 0 and g.mean() > 0 and ln.std() / ln.mean() <= 0.2 and g.std() / g.mean() <= 0.3:
        return [run]
    out, cur = [], [run[0]]
    for q, gp in zip(run[1:], gaps):
        if gp <= base_gap:
            cur.append(q)
        else:
            if len(cur) >= min_run:
                out.append(cur)
            cur = [q]
    if len(cur) >= min_run:
        out.append(cur)
    return out


KEEP_SEPARATIONS = bool(int(__import__("os").environ.get("E1_KEEP_SEPARATIONS", "1")))
SEP_END_PT = 1.5  # a separation line's ends lie this close to wall ink


def _drop_dashed(strokes: list, ppt: float, max_piece_pt: float = 6.0, min_gap_pt: float = 0.3,
                 max_gap_pt: float = 4.0, min_run: int = 6, fills: list | None = None,
                 shape: tuple | None = None) -> list:
    """EXPLODED dash / dash-dot / dotted lines (D1 17:23: EFA school sheets draw grid, centre and hidden lines as dozens
    of 0.5-5 pt collinear pieces with 1-2 pt gaps; the gap closing then turns them into walls that slice halls: Main
    Hall 59.6 of 180 m²): runs of >= min_run short one-segment pieces on one line with small regular gaps are dropped.
    Door arcs exploded into segments are curved (not collinear) and stay.

    Except ROOM SEPARATION lines (QA 20:00: EFA 'PE Store' 50.19 ✓ at d23 -> abstains: the dashed line between it and
    'Com. St' below was dropped, the two rooms merged and two labels sat in one region): a dashed run whose BOTH ends lie
    on wall ink (fills / solid strokes, within SEP_END_PT) and whose middle touches none is kept. A grid line ends at
    its bubble outside the building and crosses walls on the way; a hidden / overhead outline floats in the room."""
    import math

    import numpy as np

    groups: dict = {}
    dots: set = set()
    for i, (arr, w, _dark) in enumerate(strokes):
        if len(arr) != 2:
            continue
        (x0, y0), (x1, y1) = (np.asarray(arr, float) / ppt).tolist()
        ln = math.hypot(x1 - x0, y1 - y0)
        if ln < 0.05:  # a zero-length piece: the DOT of a dash-dot line (EFA Nursery: 2.5 pt dashes + dots), never a wall
            dots.add(i)
            continue
        if ln > max_piece_pt:
            continue
        ang = math.atan2(y1 - y0, x1 - x0) % math.pi
        ux, uy = math.cos(ang), math.sin(ang)
        off = -uy * x0 + ux * y0  # signed distance of the line from the origin
        t0, t1 = sorted((ux * x0 + uy * y0, ux * x1 + uy * y1))
        groups.setdefault((round(math.degrees(ang)) % 180, round(off * 2) / 2, round(w, 2)), []).append((t0, t1, i))
    drop: set = set()
    runs: list = []
    for key, pieces in groups.items():
        if len(pieces) < min_run:
            continue
        pieces.sort()
        run = [pieces[0]]
        for p in pieces[1:] + [(float("inf"), float("inf"), -1)]:
            gap = p[0] - run[-1][1]
            if p[2] >= 0 and min_gap_pt <= gap <= max_gap_pt:
                run.append(p)
                continue
            if len(run) >= min_run:
                drop.update(q[2] for q in run)
                runs.append((key, run))
            run = [p]
    drop |= dots
    if not drop:
        return strokes
    if KEEP_SEPARATIONS and runs and shape is not None:
        keep = _separations(strokes, drop, runs, fills or [], shape, ppt)
        drop -= keep
    return [s for i, s in enumerate(strokes) if i not in drop]


def _separations(strokes: list, drop: set, runs: list, fills: list, shape: tuple, ppt: float) -> set:
    """Indices of the dashed runs that are room separation lines: both ends on wall ink, the middle on none."""
    import math

    import cv2
    import numpy as np

    wall = np.zeros(shape, np.uint8)
    for f in fills:
        cv2.fillPoly(wall, [np.asarray(f, np.int32).reshape(-1, 1, 2)], 1)
    for i, (arr, w, _dark) in enumerate(strokes):
        a = np.asarray(arr, np.int32)
        if i in drop or (len(a) == 2 and math.hypot(*(a[1] - a[0]).tolist()) < 6 * ppt):
            continue  # short pieces (other dashes, hatch ticks) are not walls
        cv2.polylines(wall, [a.reshape(-1, 1, 2)], False, 1, thickness=max(1, int(round(w * ppt))))
    Hh, Ww = wall.shape

    def ink(x, y, r):  # wall ink within r pt of (x, y) pt
        cx, cy, rp = int(round(x * ppt)), int(round(y * ppt)), max(1, int(math.ceil(r * ppt)))
        x0, x1, y0, y1 = max(0, cx - rp), min(Ww, cx + rp + 1), max(0, cy - rp), min(Hh, cy + rp + 1)
        return x0 < x1 and y0 < y1 and bool(wall[y0:y1, x0:x1].any())

    keep: set = set()
    for (deg, off, _w), run in runs:
        ang = math.radians(deg)
        ux, uy = math.cos(ang), math.sin(ang)
        nx, ny = -uy, ux
        t0, t1 = run[0][0], max(q[1] for q in run)
        if t1 - t0 < 4 * SEP_END_PT:
            continue
        if not (ink(t0 * ux + off * nx, t0 * uy + off * ny, SEP_END_PT) and
                ink(t1 * ux + off * nx, t1 * uy + off * ny, SEP_END_PT)):
            continue
        t = t0 + 2 * SEP_END_PT
        crosses = False
        while t <= t1 - 2 * SEP_END_PT:
            if ink(t * ux + off * nx, t * uy + off * ny, 0.3):
                crosses = True
                break
            t += 0.5
        if not crosses:
            keep.update(q[2] for q in run)
    return keep


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
    if TRACED_NO_ALL and pg.get("traced") and not pg["fills"] and any(not dk for _, _, dk in pg["strokes"]):
        # D1 23:45: on a picture traced into lines (no fills) grey furniture (worktops, desks: 0.6 grey) closes the
        # room in 'all' at gap 0 and wins the (gap, -area) key -- Practical Classroom 52.77 vs 'dark' 57.25 (57.58)
        out = ["dark"]
    out += [f"w>={w}" for w in ws[1:4]]
    return out


TRACED_NO_ALL = bool(int(__import__("os").environ.get("E1_TRACED_NO_ALL", "1")))
TRACED_ALL_FALLBACK = bool(int(__import__("os").environ.get("E1_TRACED_ALL_FALLBACK", "1")))  # measure_room


DOOR_R_M = (0.45, 1.45)  # door leaf radius (one leaf), metres
LINE_GROW_PT = 1.0  # a room grows into its bounding lines up to their middle, at most this deep (pt)
RING_FIX = bool(int(__import__("os").environ.get("E1_RING_FIX", "0")))  # see measure_room (c11)
FILL_GROW_M = float(__import__("os").environ.get("E1_FILL_GROW_M", "0.08"))  # into a solid wall fill: at most this (m)
# D1 00:3x (dfe26 PDF rooms read +1.85 cm per side = +2-3 % on 3-13 m² rooms): the traced room counts its boundary
# pixels whole and grows into the wall ink by up to FILL_GROW_M, so its area sits ~0.7 px per side outside the CAD wall
# face (= the vector path: the fill edge / the line's centre, which X1's measure_rect golds and D1's golds both use).
# FACE_SNAP moves every side of an orthogonal outline onto the nearest parallel vector wall / line edge (<= max(0.8 pt,
# 2 px)) and answers that polygon's area when >= FACE_SNAP_MIN of the perimeter snapped and it is within 5 % of the
# pixel area. Vector pages only (not P1's traced copies of pictures).
FACE_SNAP = bool(int(__import__("os").environ.get("E1_FACE_SNAP", "1")))
FACE_SNAP_MIN = float(__import__("os").environ.get("E1_FACE_SNAP_MIN", "0.9"))
# a short diagonal at a corner (the grow-back rounds corners: SEN Resource 3 x 1.5 pt) is squared, not a reason to stop
FACE_SNAP_CUT_PT = float(__import__("os").environ.get("E1_FACE_SNAP_CUT_PT", "3.5"))


@functools.lru_cache(maxsize=8)
def _vector_edges(pdf_abs: str, page: int) -> tuple:
    """Axis-aligned edges of the page's DARK fills and DARK strokes in PDF points: (vertical [(x, y0, y1)], horizontal
    [(y, x0, x1)]), each sorted by position. Grey strokes (furniture, fixtures) are left out."""
    import bisect  # noqa: F401  (used by _snap_edge through the sorted lists)

    import pymupdf

    vert, horiz = [], []
    with pymupdf.open(pdf_abs) as doc:
        if page >= len(doc):
            return [], []
        for d in doc[page].get_drawings():
            typ = d.get("type") or ""
            f, c = d.get("fill"), d.get("color")
            if not (("f" in typ and f is not None and max(f) < 0.15) or ("s" in typ and c is not None and max(c) < 0.35)):
                continue
            for it in d.get("items", []):
                if it[0] == "re":
                    r = it[1]
                    segs = [((r.x0, r.y0), (r.x1, r.y0)), ((r.x1, r.y0), (r.x1, r.y1)), ((r.x1, r.y1), (r.x0, r.y1)),
                            ((r.x0, r.y1), (r.x0, r.y0))]
                elif it[0] == "l":
                    segs = [((it[1].x, it[1].y), (it[2].x, it[2].y))]
                elif it[0] == "qu":
                    q = it[1]
                    segs = [((q.ul.x, q.ul.y), (q.ur.x, q.ur.y)), ((q.ur.x, q.ur.y), (q.lr.x, q.lr.y)),
                            ((q.lr.x, q.lr.y), (q.ll.x, q.ll.y)), ((q.ll.x, q.ll.y), (q.ul.x, q.ul.y))]
                else:
                    continue
                for (ax, ay), (bx, by) in segs:
                    if abs(ax - bx) < 0.05 and abs(ay - by) > 0.5:
                        vert.append((ax, min(ay, by), max(ay, by)))
                    elif abs(ay - by) < 0.05 and abs(ax - bx) > 0.5:
                        horiz.append((ay, min(ax, bx), max(ax, bx)))
    return sorted(vert), sorted(horiz)


def _snap_edge(value: float, lo: float, hi: float, segs: list, tol: float) -> float | None:
    """The vector edge nearest to `value` (within tol pt) whose segments cover >= 15 % of the side [lo, hi]."""
    import bisect

    i = bisect.bisect_left(segs, (value - tol, -1e9, -1e9))
    cov: dict = {}
    while i < len(segs) and segs[i][0] <= value + tol:
        pos, a, b = segs[i]
        ov = min(b, hi) - max(a, lo)
        if ov > 0:
            cov[round(pos, 2)] = cov.get(round(pos, 2), 0.0) + ov
        i += 1
    good = [(abs(p - value), p) for p, c in cov.items() if c >= 0.15 * (hi - lo)]
    return min(good)[1] if good else None


def _face_snap(pdf_abs: str, page: int, contour, ppt: float):
    """The traced room outline (px contour) as an ORTHOGONAL polygon with each side on its vector wall face.
    Returns (polygon pt [closed], area pt², snapped share of the perimeter) or None (diagonal / curved sides)."""
    import cv2
    import numpy as np

    vert, horiz = _vector_edges(pdf_abs, page)
    if not vert or not horiz:
        return None
    ap = cv2.approxPolyDP(contour, 2.0, True)[:, 0, :].astype(float)
    if len(ap) < 4 or len(ap) > 40:
        return None
    edges = []  # (orientation, position px, lo px, hi px)
    for a, b in zip(ap, np.roll(ap, -1, axis=0)):
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs(dx) <= 3 and abs(dy) <= 3:
            if abs(dx) == abs(dy):
                continue
            e = ("v", (a[0] + b[0]) / 2, min(a[1], b[1]), max(a[1], b[1])) if abs(dy) > abs(dx) else \
                ("h", (a[1] + b[1]) / 2, min(a[0], b[0]), max(a[0], b[0]))
        elif abs(dx) <= 3:
            e = ("v", (a[0] + b[0]) / 2, min(a[1], b[1]), max(a[1], b[1]))
        elif abs(dy) <= 3:
            e = ("h", (a[1] + b[1]) / 2, min(a[0], b[0]), max(a[0], b[0]))
        elif max(abs(dx), abs(dy)) <= FACE_SNAP_CUT_PT * ppt:
            continue  # a corner cut: the neighbouring sides meet at the square corner
        else:
            return None  # a diagonal side: not snapped
        if edges and edges[-1][0] == e[0] and abs(edges[-1][1] - e[1]) <= 3:
            p_ = edges.pop()
            e = (e[0], (p_[1] + e[1]) / 2, min(p_[2], e[2]), max(p_[3], e[3]))
        edges.append(e)
    if len(edges) > 1 and edges[0][0] == edges[-1][0] and abs(edges[0][1] - edges[-1][1]) <= 3:
        p_ = edges.pop()
        edges[0] = (p_[0], (p_[1] + edges[0][1]) / 2, min(p_[2], edges[0][2]), max(p_[3], edges[0][3]))
    if len(edges) < 4 or len(edges) % 2 or any(a[0] == b[0] for a, b in zip(edges, edges[1:] + edges[:1])):
        return None
    tol = max(0.8, 2.0 / ppt)
    snapped = total = 0.0
    pos = []
    for o, p_, lo, hi in edges:
        val, lo_pt, hi_pt = (p_ + 0.5) / ppt, (lo + 0.5) / ppt, (hi + 0.5) / ppt
        sv = _snap_edge(val, lo_pt, hi_pt, vert if o == "v" else horiz, tol)
        total += hi_pt - lo_pt
        if sv is not None:
            snapped += hi_pt - lo_pt
        pos.append(sv if sv is not None else val)
    pts = []
    for i, (o, _p, _lo, _hi) in enumerate(edges):
        j = (i + 1) % len(edges)
        pts.append((pos[i], pos[j]) if o == "v" else (pos[j], pos[i]))
    xs = np.array([q[0] for q in pts])
    ys = np.array([q[1] for q in pts])
    area = abs(float(np.dot(xs, np.roll(ys, 1)) - np.dot(ys, np.roll(xs, 1)))) / 2
    if total <= 0 or area <= 0:
        return None
    poly = [[round(float(x), 2), round(float(y), 2)] for x, y in pts]
    return poly + [poly[0]], area, snapped / total


def _door_spokes(pg: dict, scale_n: float) -> list:
    """Door swings drawn as arcs -> the two radii (hinge -> each arc end), in px: one radius is the open leaf, the other
    the closed position = the doorway itself, so the opening is closed exactly at the wall line and the swing sector
    stays part of the room. An arc counts as a door swing when it is circular, sweeps 30-100 degrees and its radius is a
    door leaf (DOOR_R_M) at the sheet's scale; other curves (curved walls) are left out of this variant."""
    import math

    import numpy as np

    return _door_arcs(pg, scale_n)[0]


DOOR_LEAVES = bool(int(__import__("os").environ.get("E1_DOOR_LEAVES", "1")))
LEAF_M = (0.45, 1.2)  # a straight door leaf drawn without its swing arc, metres
LEAF_OFF_DEG = (12.0, 50.0)  # drawn opened this far off the wall line


def _door_leaves(pg: dict, scale_n: float) -> list:
    """Door leaves drawn WITHOUT a swing arc (QA 20:00: EFA Food Room's cupboard 'St.' has a bi-fold pair of straight
    leaves opened ~30° in a 2 m opening; the room's outline leaked into the cupboard and its skirting grew 42.33 ->
    46.94 m). A straight stroke LEAF_M long, LEAF_OFF_DEG off the nearest axis, with wall ink just behind one end (the
    hinge) along that axis and an OPENING ahead of it (the closed position, hinge -> hinge + leaf length along the axis,
    >= 70 % free of ink) -> [leaf, closed position], like an arc's two spokes. A furniture diagonal that starts at a wall
    face has wall ink all along its 'closed position' and is not taken."""
    import math

    import cv2
    import numpy as np

    ck = f"_leaves@{scale_n:g}"
    if ck in pg:
        return pg[ck]
    ppt = pg["ppt"]
    px_per_m = ppt / (scale_n * PT_TO_M_AT_1)
    lmin, lmax = LEAF_M[0] * px_per_m, LEAF_M[1] * px_per_m
    ink = pg.get("_ink")
    if ink is None:
        ink = np.zeros(pg["shape"], np.uint8)
        for a, w, _d in pg["strokes"]:
            cv2.polylines(ink, [np.asarray(a, np.int32).reshape(-1, 1, 2)], False, 1,
                          thickness=max(1, int(round(w * ppt))))
        if pg["fills"]:
            cv2.fillPoly(ink, pg["fills"], 1)
        pg["_ink"] = ink
    Hh, Ww = ink.shape

    def at(x, y):
        xi, yi = int(round(x)), int(round(y))
        return 0 <= xi < Ww and 0 <= yi < Hh and bool(ink[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2].any())

    out: list = []
    for a, _w, _d in pg["strokes"]:
        if len(a) > 6:
            continue
        P = np.asarray(a, dtype=float)
        if len(P) > 2:  # a leaf drawn as a thin sliver / out-and-back path: its two farthest points, if thin
            D = np.hypot(P[:, None, 0] - P[None, :, 0], P[:, None, 1] - P[None, :, 1])
            i, j = np.unravel_index(int(D.argmax()), D.shape)
            P = np.array([P[i], P[j]]) if D[i, j] > 0 and float(np.abs(
                ((P - P[i]) @ np.array([-(P[j] - P[i])[1], (P[j] - P[i])[0]])) / D[i, j]).max()) <= 0.6 * ppt else None
            if P is None:
                continue
        v = P[1] - P[0]
        L = float(np.hypot(*v))
        if not lmin <= L <= lmax:
            continue
        ang = math.degrees(math.atan2(v[1], v[0])) % 180
        if not LEAF_OFF_DEG[0] <= min(ang % 90, 90 - ang % 90) <= LEAF_OFF_DEG[1]:
            continue
        ax = np.array([1.0, 0.0]) if min(ang, 180 - ang) <= 45 else np.array([0.0, 1.0])
        for h, t in ((P[0], P[1]), (P[1], P[0])):
            u = ax if float(np.dot(t - h, ax)) >= 0 else -ax
            if not any(at(*(h - k * ppt * u)) for k in (0.6, 1.0, 1.5)):
                continue  # no wall just behind this end: not the hinge
            ts = np.linspace(1.5 * ppt, L - 1.0 * ppt, max(4, int((L - 2.5 * ppt) / (0.5 * ppt))))
            if not len(ts) or sum(not at(*(h + x * u)) for x in ts) / len(ts) < 0.7:
                continue  # no opening ahead (a wall face, a jamb): not a door leaf
            out += [np.round(np.array([h, t])).astype(np.int32),
                    np.round(np.array([h - 1.0 * ppt * u, h + L * u])).astype(np.int32)]
            break
    pg[ck] = out
    return out


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
    found: list = []
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
    items += _chained_arcs(pg["strokes"], px_per_m)
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
        if not 30 <= sweep <= 100 and not (DOOR_SEMI and sweep > 100 and isinstance(sid, int) and
                                           _leaf_to_centre(pg["strokes"][sid][0], j0, len(pts), (ux, uy), r)):
            continue
        found.append(((ux, uy), r, (int(ax), int(ay)), (int(cx), int(cy)), sid, j0, len(pts)))
    for k, (u, r, a, b, sid, j0, n_pts) in enumerate(found):
        # a CIRCLE drawn as 3-4 quarter arcs round one centre (grid bubbles, section / detail markers: USACE A-101F
        # has 13 of them, 0.4 m at 1:50, and they made its door-scale check abstain) is not a door swing; a door has
        # one arc per hinge (two for a double-swing leaf)
        same = sum(1 for v, s_, *_ in found if abs(v[0] - u[0]) <= 2 + 0.03 * r and abs(v[1] - u[1]) <= 2 + 0.03 * r
                   and abs(s_ - r) <= 0.05 * r)
        if same >= 3:
            continue
        c = (int(round(u[0])), int(round(u[1])))
        out.append(np.array([c, a], dtype=np.int32))
        out.append(np.array([c, b], dtype=np.int32))
        if isinstance(sid, tuple):  # an arc exploded into one-segment paths: each of them is part of the swing
            for s_i in sid:
                arc_ids.setdefault(s_i, []).append((0, 1))
        elif sid is not None:
            arc_ids.setdefault(sid, []).append((j0, j0 + n_pts - 1))  # point index range of the arc's segments
    return out, arc_ids


DOOR_SEMI = bool(int(__import__("os").environ.get("E1_DOOR_SEMI", "1")))


def _leaf_to_centre(arr, j0: int, n_pts: int, centre: tuple, r: float) -> bool:
    """A door swing drawn as a SEMICIRCLE (the leaf opens 180°, H 20:37: Stoa's DfE Multi-Purpose Hall double door; the
    30-100° test dropped it, the outline followed both arcs and the opening was not deducted): accepted when the same
    path also draws the LEAF, a straight segment from one end of the arc to the arc's centre (the hinge). A grid bubble
    drawn as two semicircles has no such radius line."""
    import numpy as np

    P = np.asarray(arr, dtype=float)
    e = j0 + n_pts - 1
    ends = ([P[j0 - 1]] if j0 > 0 else []) + ([P[e + 1]] if e + 1 < len(P) else [])
    return any(float(np.hypot(q[0] - centre[0], q[1] - centre[1])) <= 0.15 * r + 2 for q in ends)


def _chained_arcs(strokes: list, px_per_m: float, max_seg_m: float = 0.35) -> list:
    """Arcs EXPLODED into one-segment paths (USACE A-101F draws its door swings as ~13 separate 2-point lines):
    short single segments chained end to end through points shared by exactly two of them -> (stroke ids, 0, points)
    for _door_arcs' circle fit, which then keeps only round 30-100 degree runs."""
    import numpy as np

    lim = max_seg_m * px_per_m
    segs = {}
    for i, (arr, _w, _d) in enumerate(strokes):
        if len(arr) != 2:
            continue
        a, b = (int(arr[0][0]), int(arr[0][1])), (int(arr[1][0]), int(arr[1][1]))
        if a != b and float(np.hypot(a[0] - b[0], a[1] - b[1])) <= lim:
            segs[i] = (a, b)
    if len(segs) < 5:
        return []
    at: dict = {}
    for i, (a, b) in segs.items():
        at.setdefault(a, []).append(i)
        at.setdefault(b, []).append(i)

    def near(p):  # segments ending at p or within one pixel of it (rounding of the same PDF point)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out += at.get((p[0] + dx, p[1] + dy), [])
        return out

    used: set = set()
    chains = []
    for i0 in segs:
        if i0 in used:
            continue
        used.add(i0)
        chain = [i0]
        a, b = segs[i0]
        pts = [a, b]
        for forward in (True, False):  # walk from both ends while the joint is shared by exactly two segments
            end = pts[-1] if forward else pts[0]
            while True:
                nb = [j for j in set(near(end)) if j != (chain[-1] if forward else chain[0])]
                if len(nb) != 1 or nb[0] in used:
                    break
                j = nb[0]
                used.add(j)
                p, q = segs[j]
                nxt = q if abs(p[0] - end[0]) <= 1 and abs(p[1] - end[1]) <= 1 else p
                if forward:
                    chain.append(j)
                    pts.append(nxt)
                else:
                    chain.insert(0, j)
                    pts.insert(0, nxt)
                end = nxt
        if len(chain) >= 5:
            chains.append((tuple(chain), 0, np.asarray(pts, dtype=float)))
    return chains


@functools.lru_cache(maxsize=2)
def ink_dist(pdf_abs: str, page: int):
    """Distance (px, clipped at 255) to the nearest INK of the page as rendered (same px grid as _page): clip paths,
    white masks and fills behave as printed. CAD exports may draw a wall as one filled strip and cut its door openings
    with a clip path (X1 stoa-like DfE school sheet); the path rasteriser (_dist) sees the strip unbroken."""
    import cv2
    import numpy as np
    import pymupdf

    pg = _page(pdf_abs, page)
    if pg is None:
        return None
    H, W = pg["shape"]
    with pymupdf.open(pdf_abs) as doc:
        p = doc[page]
        if p.rotation:
            p.set_rotation(0)
        pix = p.get_pixmap(matrix=pymupdf.Matrix(pg["ppt"], pg["ppt"]), colorspace=pymupdf.csGRAY, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    ink = np.zeros((H, W), np.uint8)
    h, w = min(H, img.shape[0]), min(W, img.shape[1])
    ink[:h, :w] = img[:h, :w] < 200
    d = cv2.distanceTransform((ink == 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    return np.minimum(d, 255).astype(np.uint8)


@functools.lru_cache(maxsize=8)
def _dist(pdf_abs: str, page: int, variant: str):
    """Distance (px, clipped at 255) from every pixel to the nearest drawn line of the variant; 0 on the lines."""
    import cv2
    import numpy as np

    pg = _page(pdf_abs, page)
    m = np.zeros(pg["shape"], dtype=np.uint8)
    variant, _, dsuf = variant.partition("|")  # '<base>|doors@N': the base line set with door swings -> radii
    doors = dsuf.startswith("doors@")
    anch = variant == "anch"  # 'all' minus the stroke clusters that touch no wall fill
    bridge_n = float(variant[7:]) if variant.startswith("bridge@") else None  # 'all' + collinear wall-end bridges
    # 'wall@N' (E1 c12): the wall fills + their collinear bridges (+ door radii) and NO stroke: furniture, counters and
    # millwork are not walls (AFCEE A-101 Break Room: the kitchen counter cut the floor-plan trace to 9.48 m², gold 11.59)
    wall_n = float(variant[5:]) if variant.startswith("wall@") else None
    bridge_n = bridge_n or wall_n
    if anch or bridge_n:
        variant = "wall" if wall_n else "all"
    wmin = float(variant[3:]) if variant.startswith("w>=") else None
    by_th: dict[int, list] = {}
    arc_ids: dict = {}
    if doors:  # every stroke, door swings replaced by their two radii (_door_arcs)
        spokes, arc_ids = _door_arcs(pg, float(dsuf[6:]))
        if DOOR_LEAVES:
            spokes = spokes + _door_leaves(pg, float(dsuf[6:]))
        by_th.setdefault(max(1, int(round(0.25 * pg["ppt"]))), []).extend(spokes)
    for i, (a, w, dark) in enumerate(pg["strokes"]):
        if i in arc_ids:  # the path's other segments (leaf, doorway chord) are kept
            keep = [a[j:j + 2] for j in range(len(a) - 1) if not any(j0 <= j < j1 for j0, j1 in arc_ids[i])]
            by_th.setdefault(max(1, int(round(w * pg["ppt"]))), []).extend(keep)
            continue
        if variant == "wall":
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
    if pg.get("fill_lines") and variant != "wall":
        for poly_, ring_w in pg["fill_lines"]:  # one by one: an even-odd fill of the whole list would XOR crossings
            if ring_w:
                cv2.polylines(m, [poly_], True, 255, thickness=ring_w, lineType=cv2.LINE_8)
            else:
                cv2.fillPoly(m, [poly_], 255)
    if pg["fills"]:
        if anch:  # only the stroke clusters that touch a solid wall fill (see ANCHORED)
            fm = np.zeros(pg["shape"], dtype=np.uint8)
            cv2.fillPoly(fm, pg["fills"], 255)
            n, lab = cv2.connectedComponents((m > 0).astype(np.uint8), connectivity=8)
            touch = np.unique(lab[(cv2.dilate(fm, np.ones((5, 5), np.uint8)) > 0) & (lab > 0)])
            keep = np.zeros(n, dtype=bool)
            keep[touch] = True
            m = np.where(keep[lab], 255, 0).astype(np.uint8)
            m[fm > 0] = 255
        else:
            cv2.fillPoly(m, pg["fills"], 255)
        if bridge_n:  # BRIDGE: wall-fill pieces on one wall line, <= BRIDGE_M apart, closed along that line
            fm = np.zeros(pg["shape"], dtype=np.uint8)
            cv2.fillPoly(fm, pg["fills"], 255)
            mpp = bridge_n * PT_TO_M_AT_1 / pg["ppt"]
            lmin, lb = max(3, int(round(0.5 / mpp))), max(5, int(round(BRIDGE_M / mpp)))
            for kmin, kb in (((1, lmin), (1, lb)), ((lmin, 1), (lb, 1))):
                run = cv2.morphologyEx(fm, cv2.MORPH_OPEN, np.ones(kmin, np.uint8))  # pieces along this direction
                # close along the piece's own line to the next fill (collinear stub or the wall it faces), only in
                # the piece's own band: two parallel walls across a corridor are never joined
                band = cv2.dilate(run, np.ones(kb, np.uint8))
                closed = cv2.morphologyEx(np.maximum(run, fm), cv2.MORPH_CLOSE, np.ones(kb, np.uint8))
                m[(closed > 0) & (band > 0)] = 255
    d = cv2.distanceTransform((m == 0).astype(np.uint8), cv2.DIST_L2, 3)
    return np.minimum(d, 255).astype(np.uint8)


def _region(dist, seed, k: int, free=None, max_px: float | None = None, memo: list | None = None):
    """Free region around `seed` after growing the lines by k px, grown back by k, holes (furniture drawn as closed
    outlines inside the room) filled; (mask, bbox (x0, y0, x1, y1) px) or None when it leaks to the page edge.
    `free`: (dist > k) as uint8, when the caller reuses it across seeds. `max_px`: the flood fill already holds more
    pixels than that -> False (the grown-back region is at least as big; P1 00:39: outline_for_area spent 12 s of 31 in
    distanceTransforms of leaked regions that could never match the printed area)."""
    import cv2
    import numpy as np

    h, w = dist.shape
    x, y = seed
    if not (0 <= x < w and 0 <= y < h):
        return None
    if free is None:
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
    if memo is not None:  # P1 01:16: a seed inside a region already flooded (same line set, same k) gets its result
        for (mx, my, mm), res in memo:
            if mx <= x < mx + mm.shape[1] and my <= y < my + mm.shape[0] and mm[y - my, x - mx]:
                return res
    ff = np.zeros((h + 2, w + 2), dtype=np.uint8)
    n_px, _, _, rect = cv2.floodFill(free, ff, (x, y), 2, flags=4 | (1 << 8) | cv2.FLOODFILL_MASK_ONLY)
    rx, ry, rw, rh = rect
    key = (rx, ry, ff[1 + ry:1 + ry + rh, 1 + rx:1 + rx + rw].copy()) if memo is not None else None
    if rx <= 0 or ry <= 0 or rx + rw >= w or ry + rh >= h:
        if key is not None:
            memo.append((key, None))
        return None
    if max_px is not None and n_px > max_px:
        if key is not None:
            memo.append((key, False))
        return False
    m = k + 2
    X0, Y0, X1, Y1 = max(0, rx - m), max(0, ry - m), min(w, rx + rw + m), min(h, ry + rh + m)
    reg = ff[1 + Y0:1 + Y1, 1 + X0:1 + X1].copy()
    if k:
        dd = cv2.distanceTransform((reg == 0).astype(np.uint8), cv2.DIST_L2, 3)
        reg = ((dd <= k) & (dist[Y0:Y1, X0:X1] > 0)).astype(np.uint8)
    cs, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        if key is not None:
            memo.append((key, None))
        return None
    c = max(cs, key=cv2.contourArea)
    filled = np.zeros_like(reg)
    cv2.drawContours(filled, [c], -1, 1, thickness=-1)
    out = (filled, c + np.array([[X0, Y0]]), (X0, Y0, X1, Y1))
    if key is not None:
        memo.append((key, out))
    return out


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
    import numpy as np

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
    max_px = area_m2 * (1 + tol) * 1.02 / m_per_px ** 2  # flood-fill pixels past which no reading can match
    cands = []  # every (variant, gap) whose outline passes: (gap, seed, err, area, contour, variant, shapely poly)
    for gap in GAPS_M:
        k = int(round(gap / m_per_px))
        if gap and k < 1:
            continue
        if k > 250:
            break
        for variant in variants:
            dist = _dist(pdf_abs, page, variant)
            free_k = (dist > k).astype(np.uint8)  # once per (line set, gap), not per seed
            memo_k = [] if REGION_SKIP else None
            for si, seed in enumerate(seeds):
                # an outside seed (si > 0) whose flood fill alone is already too big cannot match: skipped before the
                # grow-back (seed 0 is measured in full: its size decides whether the outside seeds are tried at all)
                r = _region(dist, seed, k, free=free_k, max_px=max_px if si and REGION_SKIP else None, memo=memo_k)
                if r is None or r is False:
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
REGION_SKIP = bool(int(__import__("os").environ.get("E1_REGION_SKIP", "1")))  # outline_for_area: see _region max_px


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
        p = doc[page]
        blocks = p.get_text("dict").get("blocks", [])
        r = p.mediabox if p.rotation in (90, 270) else p.rect
        page_mm = sorted((r.width * 25.4 / 72, r.height * 25.4 / 72))
    on_paper = _paper_named(page_mm)
    c: Counter = Counter()
    for b in blocks:
        lines = ["".join(s.get("text", "") for s in ln.get("spans", [])) for ln in b.get("lines", [])]
        for i, text in enumerate(lines):
            for s in parse_scales(text):
                if 10 <= s["ratio"] <= 2000:
                    c[float(s["ratio"]) * _at_paper_factor(text, lines[i + 1] if i + 1 < len(lines) else "",
                                                           on_paper)] += 1
    return tuple(n for n, _ in c.most_common())


def _paper_named(page_mm) -> str | None:
    from stoa.solvers.raster.scalebar import PAPER_MM

    a, b = page_mm
    for name, (sw, sl) in PAPER_MM.items():
        if abs(a - sw) <= 0.02 * sw and abs(b - sl) <= 0.02 * sl:
            return name
    return None


def _at_paper_factor(text: str, next_line: str, on_paper: str | None) -> float:
    """'1:200 @ A1' / '1:200 at A1' printed on a page that IS another standard sheet (D1's EFA school sets: A1 drawings
    published as A3 PDFs, the page size exact): the drawing on the file is at 1:200 x (A1 / A3) = 1:400 (the drawn
    0-10 m bar agrees). Same rule as the raster reader's printed_bar (_AT_PAPER)."""
    from stoa.solvers.raster.scalebar import _AT_PAPER, PAPER_MM

    m = _AT_PAPER.search(text) or (_AT_PAPER.match(next_line.strip()) if next_line else None)
    if not m or on_paper is None:
        return 1.0
    at = (m.group(1) or m.group(2)).upper()
    if at not in PAPER_MM or at == on_paper:
        return 1.0
    if re.fullmatch(r"A\d", at) and re.fullmatch(r"A\d", on_paper):  # ISO A sizes: exactly sqrt(2) per step
        return 2 ** ((int(on_paper[1]) - int(at[1])) / 2)
    return PAPER_MM[at][1] / PAPER_MM[on_paper][1]


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


def text_lines_dir(pdf_abs: str, page: int = 0) -> tuple:
    """((text, bbox pdf_pt, (dx, dy) writing direction), ...): text_lines plus each line's direction ((1, 0) =
    horizontal; (0, -1) = drawn upwards, e.g. every label of a sheet plotted with /Rotate 90)."""
    import pymupdf

    with pymupdf.open(pdf_abs) as doc:
        if page >= len(doc):
            return ()
        blocks = doc[page].get_text("dict").get("blocks", [])
    return tuple((" ".join(s.get("text", "") for s in ln.get("spans", [])).strip(), tuple(ln["bbox"]),
                  tuple(ln.get("dir", (1.0, 0.0))))
                 for b in blocks for ln in b.get("lines", []))


ANCHORED = bool(int(__import__("os").environ.get("E1_ANCHORED", "1")))
BRIDGE = bool(int(__import__("os").environ.get("E1_BRIDGE", "1")))
BRIDGE_M = float(__import__("os").environ.get("E1_BRIDGE_M", "2.5"))  # widest opening closed between collinear stubs
REVEAL_CUT = bool(int(__import__("os").environ.get("E1_REVEAL_CUT", "1")))
ANCH_MIN_GAIN = 1.2  # the anchored room must be this much larger than the all-lines cell to replace it
ANCH_FREE_SHARE = 0.5  # ... and at least this share of that cell's outline must be free-standing ink


@functools.lru_cache(maxsize=8)
def _solid_walls(pdf_abs: str, page: int) -> bool:
    """The sheet draws its walls as SOLID fills (EFA / DfE school sheets, Revit 'poché'): filled ink is at least a third
    of the stroked ink. Hollow-wall sheets with a few filled columns or hatches are not."""
    import cv2
    import numpy as np

    pg = _page(pdf_abs, page)
    if pg is None or not pg["fills"]:
        return False
    fm = np.zeros(pg["shape"], dtype=np.uint8)
    cv2.fillPoly(fm, pg["fills"], 1)
    sm = np.zeros(pg["shape"], dtype=np.uint8)
    for a, w, dark in pg["strokes"]:
        cv2.polylines(sm, [a], False, 1, thickness=max(1, int(round(w * pg["ppt"]))))
    return int(fm.sum()) >= 0.33 * int((sm & (fm == 0)).sum())


def _anchored_pick(pdf_abs, page, pg, pick, seed, others_px, m_per_px, scale_n):
    """ANCHORED reading (X1 18:15, captain 18:21 item 2: big rooms sliced by thin interior linework). On a solid-wall
    sheet the walls are the fills; a stroke cluster that touches no fill (sports-court markings, bleacher rows, furniture,
    'void' diagonals, grid lines inside a hall) is not a room boundary. X1/D1 EFA secondary GF: the 'Sports Hall' label
    sits inside a badminton court -> every all-lines reading traces one court cell (20.59 m²; gold 504). The anchored
    room replaces the all-lines one only when (a) it is closed, holds the label and no other room label (a free-standing
    loop around ANOTHER room's name keeps that room out), (b) it is >= ANCH_MIN_GAIN x larger, and (c) >= ANCH_FREE_SHARE
    of the all-lines outline runs along free-standing ink (the cell really was cut out by non-walls).
    Returns (pick, support) or None."""
    import cv2
    import numpy as np

    if not ANCHORED or not pg["fills"] or not _solid_walls(pdf_abs, page):
        return None
    dsuf = "|" + pick[1].partition("|")[2] if "|" in str(pick[1]) else ""
    var = "anch" + dsuf
    dist_a = _dist(pdf_abs, page, var)
    # (c) the all-lines outline: share of its points far from any anchored ink
    c = pick[3].reshape(-1, 2)
    if len(c) < 4:
        return None
    pts = np.concatenate([np.linspace(c[i], c[(i + 1) % len(c)], max(2, int(np.hypot(*(c[(i + 1) % len(c)] - c[i]))) // 2),
                                      endpoint=False) for i in range(len(c))]).astype(int)
    h, w = dist_a.shape
    pts = pts[(pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)]
    k0 = int(round(pick[0] / m_per_px))
    th = max(2, int(round(0.5 * pg["ppt"])))
    free = float(np.mean(dist_a[pts[:, 1], pts[:, 0]] > k0 + 2 * th)) if len(pts) else 0.0
    if free < ANCH_FREE_SHARE:
        return None
    acands = []
    for gap in GAPS_M:
        k = int(round(gap / m_per_px))
        if (gap and k < 1) or k > 250:
            continue
        r = _region(dist_a, seed, k)
        if r is None:
            continue
        filled, contour, _ = r
        if cv2.pointPolygonTest(contour, (float(seed[0]), float(seed[1])), False) < 0:
            continue
        if any(cv2.pointPolygonTest(contour, q, False) >= 0 for q in others_px):
            continue
        a = float(filled.sum()) * m_per_px ** 2
        if not 0.8 <= a <= 3000:
            continue
        acands.append((gap, var, a, contour, _shape(contour), filled, r[2]))
    if not acands:
        return None
    best = max(acands, key=lambda d: (1 + sum(1 for e in acands if e is not d and _iou(d[4], e[4]) >= 0.9), -d[0]))
    support = 1 + sum(1 for e in acands if e is not best and _iou(best[4], e[4]) >= 0.9)
    if best[2] < ANCH_MIN_GAIN * pick[2]:
        return None
    return best, support


def measure_room(pdf_abs: str, page: int, pt: tuple[float, float], scale_n: float, tag_box: tuple | None = None,
                 own_radius_pt: float = 25.0, base_variants: list[str] | None = None) -> dict | None:
    """A room's outline and area MEASURED around its label when no area is printed (line-only CAD sheets, X1 14:31).
    X1 wild LEVEL_11 (A2, 1:100): comms room 16.41 m² (gold 16.83, -2.5 %), long side 5.24 m (5.253), conference room
    7.72 m (7.73). KNOWN: a side across a door opening bulges by up to the gap (grow-back after the flood fill; synthetic
    +5 %) — next: close the lines morphologically (dilate + erode) BEFORE the flood fill instead. NOT wired.

    of every traced region that holds the label, holds no other word label (the next room's name) and does not run
    off the page, the one most readings (line set x gap) agree on (IoU >= 0.9). Returns {"polygon", "area_m2",
    "support", "n_readings", "gap_m", "variant", "sides_m"} or None. Approximate (exact=False): wall faces, door
    openings closed at `gap_m` x 2. `base_variants` (e.g. ['wall@96']) replaces the line sets tried; then no bridge
    fallback, anchored pick or reveal cut runs (the caller asked for exactly those readings)."""
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
    variants = list(base_variants) if base_variants else _variants(pg)
    if base_variants and any(v.startswith("wall@") for v in base_variants) and not pg["fills"]:
        return None  # no wall fills: a walls-only reading has no walls
    if _door_spokes(pg, scale_n) or (DOOR_LEAVES and _door_leaves(pg, scale_n)):  # every line set with the door
        # swings replaced by their radii (see _door_arcs) and straight leaves closed at their hinge (_door_leaves)
        variants = [f"{v}|doors@{scale_n:g}" for v in variants]
    cands = []

    def _collect(vs):
        for gap in GAPS_M:
            k = int(round(gap / m_per_px))
            if (gap and k < 1) or k > 250:
                continue
            for variant in vs:
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

    _collect(variants)
    if not cands and not base_variants and TRACED_ALL_FALLBACK and not any(v.split("|")[0] == "all" for v in variants):
        # M1 00:45: the traced SCAN copy of DfE L00 closes the Multi-Purpose Hall only in 'all' (245.6 m², gold 244.6;
        # 'dark' never closes it) -- the 'all' line set dropped on traced copies (_variants) comes back as a fallback
        dsuf = "|" + variants[0].partition("|")[2] if variants and "|" in variants[0] else ""
        _collect(["all" + dsuf])
    if not cands and not base_variants and BRIDGE and pg["fills"] and _solid_walls(pdf_abs, page):
        # every reading leaked into another room: openings wider than the door gaps between COLLINEAR wall stubs (EFA
        # Nursery -> Cloak / Toilets / Lobby through ~2 m openings). Fallback only: wall pieces on one line, <= BRIDGE_M
        # apart, are joined; the same guards apply (label-clean, closed, agreeing readings)
        bv = f"bridge@{scale_n:g}" + (f"|doors@{scale_n:g}" if any("doors@" in v for v in variants) else "")
        for gap in GAPS_M:
            k = int(round(gap / m_per_px))
            if (gap and k < 1) or k > 250:
                continue
            r = _region(_dist(pdf_abs, page, bv), seeds[0], k)
            if r is None:
                continue
            filled, contour, _ = r
            if cv2.pointPolygonTest(contour, (float(seeds[0][0]), float(seeds[0][1])), False) < 0:
                continue
            if any(cv2.pointPolygonTest(contour, q, False) >= 0 for q in others_px):
                continue
            a = float(filled.sum()) * m_per_px ** 2
            if 0.8 <= a <= 3000:
                cands.append((gap, bv, a, contour, _shape(contour), filled, r[2]))
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
    reveal_cut = False
    anchored = None if base_variants else _anchored_pick(pdf_abs, page, pg, pick, seeds[0], others_px, m_per_px, scale_n)
    if anchored is not None:
        pick, support = anchored
    if REVEAL_CUT and BRIDGE and not base_variants and "bridge@" not in str(pick[1]) and pg["fills"] and _solid_walls(pdf_abs, page):
        # the outline runs into wide door openings (no door drawn) up to the wall's depth: EFA Main Hall perimeter
        # 57.63 vs 55.95 with the area right. The same room with its wall lines closed across those openings, when
        # it is nearly the same region (IoU >= 0.97, 0-3 % smaller), is the room's own outline
        dsuf = "|" + pick[1].partition("|")[2] if "|" in str(pick[1]) else ""
        bv = f"bridge@{scale_n:g}" + dsuf
        k = int(round(pick[0] / m_per_px))
        r = _region(_dist(pdf_abs, page, bv), seeds[0], k)
        if r is not None and cv2.pointPolygonTest(r[1], (float(seeds[0][0]), float(seeds[0][1])), False) >= 0 and \
                not any(cv2.pointPolygonTest(r[1], q, False) >= 0 for q in others_px):
            a_b = float(r[0].sum()) * m_per_px ** 2
            shp_b = _shape(r[1])
            if 0.97 * pick[2] <= a_b <= pick[2] and _iou(shp_b, pick[4]) >= 0.97:
                pick = (pick[0], bv, a_b, r[1], shp_b, r[0], r[2])
                reveal_cut = True
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
    fm = None
    if pg["fills"]:
        # a SOLID wall strip (poché fill): the room grows into it by at most FILL_GROW_M in real units (D1 17:46: at
        # 1:400 the 1 pt line growth is 0.14 m and EFA solid-wall rooms came out +1-6 %; Stoa's DfE fills at 1:200 sit
        # ~0.03-0.07 m inside the faces X1 measured)
        fm = np.zeros(dcrop.shape, np.uint8)
        cv2.fillPoly(fm, [f - np.array([x0p, y0p]) for f in pg["fills"]], 1)
    other = (dcrop > 0) & (room == 0)
    d_room = cv2.distanceTransform((room == 0).astype(np.uint8), cv2.DIST_L2, 3)
    d_other = cv2.distanceTransform((~other).astype(np.uint8), cv2.DIST_L2, 3)
    lim = np.full(dcrop.shape, float(e), np.float32)
    if fm is not None:
        lim[fm > 0] = min(float(e), FILL_GROW_M / (scale_n * PT_TO_M_AT_1) * ppt)
    if RING_FIX:
        # E1 c11: _dist truncates OpenCV's L2 distance (mask 3: 0.955 next to ink) to uint8, so the free pixel next to
        # every line reads 0 = ink (a one-pixel RING), and cv2 draws thickness t as 2t-1 px. A thick wall line's middle
        # then lies deeper than the 1 pt cap (AF module plans, 1.26 pt lines at 1.74 px/pt: rooms 0.6 px short per
        # side, -2 to -3 % on 120 sq ft rooms). On a stroke's pixels the room may grow to that stroke's centre pixel
        # (ring + t), never less than before and at most one pixel past the cap. The centre pixel counts whole (+half a
        # pixel per side; synthetic 1.26 pt box: -1.1 % -> +1.06 %); the AF schedules sit ~0.5 % above the faces.
        tmap = np.zeros(dcrop.shape, np.uint8)
        by_t: dict = {}
        off = np.array([x0p, y0p])
        for a_, w_, _dk in pg["strokes"]:
            by_t.setdefault(max(1, int(round(w_ * ppt))), []).append((a_ - off).astype(np.int32))
        for t_ in sorted(by_t):
            cv2.polylines(tmap, by_t[t_], False, int(min(t_, 255)), thickness=t_, lineType=cv2.LINE_8)
        on = (tmap > 0) & line
        lim[on] = np.maximum(lim[on], np.minimum(float(e) + 1.0, 1.0 + tmap[on].astype(np.float32)))
    grown = (room > 0) | (line & (d_room <= d_other + 0.5) & (d_room <= lim))
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
    face_snap = None
    if FACE_SNAP and pg["fills"] and not pg.get("traced"):
        try:
            fs = _face_snap(pdf_abs, page, contour, ppt)
            if fs is not None and fs[2] >= FACE_SNAP_MIN:
                from shapely.geometry import Point, Polygon as _P

                a_s = fs[1] * (scale_n * PT_TO_M_AT_1) ** 2
                sp = _P([tuple(q) for q in fs[0]])
                if sp.is_valid and abs(a_s - a) <= 0.05 * a and sp.contains(Point(pt[0], pt[1])):
                    face_snap = {"area_px_m2": round(a, 2), "snapped_share": round(fs[2], 3)}
                    a, poly = a_s, fs[0]
                    shp = _shape(np.round(np.asarray(fs[0][:-1]) * ppt).astype(np.int32).reshape(-1, 1, 2))
        except Exception:  # noqa: BLE001 — the pixel reading stands
            face_snap = None
    sides = None
    try:
        r = shp.minimum_rotated_rectangle
        xy = list(r.exterior.coords)[:4]
        d = sorted(((xy[i][0] - xy[i + 1][0]) ** 2 + (xy[i][1] - xy[i + 1][1]) ** 2) ** 0.5 for i in range(2))
        sides = [round((v + (0 if face_snap else 1)) * m_per_px, 3) for v in reversed(d)]  # long, short (m); +1 px: pixel centres
    except Exception:  # noqa: BLE001
        pass
    return {"polygon": poly, "area_m2": round(a, 2), "support": support, "n_readings": len(cands), "gap_m": gap,
            "variant": variant, "sides_m": sides, "scale_n": scale_n, "reveal_cut": reveal_cut, "face_snap": face_snap}
