"""Room outlines for area answers on any drawing: the white region around a point, found by flood fill (CPU, no model).

Called by web/mock/stoaApi.ts (POST /api/shapes), one process per request:

    echo '{"abs": "/tmp/stoa-uploads/ab12/plan.pdf", "page": 0, "space": "pdf_pt",
           "items": [{"i": 0, "bbox": [500, 77, 570, 126]}]}' | $JUNCTION_VENV/bin/python web/server/room_shape.py

Why: the contract gives evidence as boxes (for an area: the room's bounding box, or the printed area's text box on a
scan). A customer checks an area by seeing the room's outline, and with a scale the outline itself measures the area.
How: the page (PDF rendered at ~2,400 px on the long side, or the image) is binarised (adaptive threshold, so phone
photos with shading work), small marks (text, symbols) are dropped so the room label does not block the fill, and the
region around the box centre is flood-filled. Door openings would let the fill leak into the next room, so walls are
thickened step by step (0, then ~0.3 %, ~0.6 %, ~1 % of the page) until the region is closed (not touching the page
edge, not most of the plan); the region is then grown back by the same amount. A region that still leaks is dropped.
An item may carry "scale_n" (the drawing's 1:N, vector PDFs only): then E1's wall-line tracer runs first
(stoa.evidence.linework.measure_room: door swings closed at the doorway line, outline at the lines' middle, other rooms'
printed names kept out; DfE laundry 7.03 m² vs 5.68 here, gold 7.0) and this flood fill is the fallback.
On a picture an item may carry "px_per_m" instead: the same tracer runs on the upload's traced copy (<stem>.vec6.pdf)
when there is one (pic_lines, W6's lineroom.py).
An item may list "others": [[x, y], ...], the label points of OTHER rooms (same space): a region holding one of them
leaked through a door into that room, so the walls are thickened further (none closed → no outline).
Output, per item: {"i", "poly": [[x, y], ...] in the evidence's own space, "area": in that space's units squared,
"closing_px": how much the walls were thickened, "fit": IoU of the outline's box with the evidence box when the
evidence box is room-sized}. Outlines are estimates ("exact": false): the answer's value stays the printed or computed
one.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

LONG_SIDE = 2400
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def wall_lines(abs_path: str, page: int, pt: tuple[float, float], scale_n: float):
    """E1's tracer on a vector PDF at 1:scale_n around pt (pdf_pt): (poly pdf_pt, area pt², how) or None.
    ROOM_E1=0 turns it off (bench)."""
    if not abs_path.lower().endswith(".pdf") or not scale_n or scale_n <= 0 or os.environ.get("ROOM_E1", "1") == "0":
        return None
    try:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        from stoa.evidence.linework import PT_TO_M_AT_1, measure_room

        r = measure_room(abs_path, page, (float(pt[0]), float(pt[1])), float(scale_n))
    except Exception as e:  # noqa: BLE001
        print(f"room_shape: wall-line tracer: {e}", file=sys.stderr)
        return None
    if not r or not r.get("polygon") or not r.get("area_m2"):
        return None
    m_per_pt = float(scale_n) * PT_TO_M_AT_1
    return r["polygon"], float(r["area_m2"]) / (m_per_pt * m_per_pt), "wall lines"


def pic_lines(abs_path: str, pt: tuple[float, float], px_per_m: float):
    """W6 00:48 (UX S-B2): on a PICTURE with a traced copy (<stem>.vec6.pdf, P1's vectorize) E1's wall-line tracer runs
    on that copy (W6's web/src/mobile/lineroom.py, called, not copied): (poly image_px, area px², how) or None.
    DfE Level 00 PNG, 680 taps around 170 room names: right room 488 / wrong 31 vs the flood fill's 346 / 238; hall
    246.97 m² (printed 247.73; flood fill 271). Needs the picture's px per m (door-swing radii). ROOM_E1=0 turns it off."""
    if not abs_path.lower().endswith((".png", ".jpg", ".jpeg")) or not px_per_m or px_per_m <= 0 or os.environ.get("ROOM_E1", "1") == "0":
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("w6_lineroom", os.path.join(REPO, "web", "src", "mobile", "lineroom.py"))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        r = mod.trace(abs_path, float(pt[0]), float(pt[1]), float(px_per_m))
    except Exception as e:  # noqa: BLE001
        print(f"room_shape: picture wall-line tracer: {e}", file=sys.stderr)
        return None
    poly = r.get("poly") if isinstance(r, dict) and r.get("ok") else None
    if not poly or len(poly) < 3:
        return None
    a = 0.0
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        a += x1 * y2 - x2 * y1
    return poly, abs(a) / 2, "wall lines (traced copy)"


def load(abs_path: str, page: int):
    """Grayscale page as uint8, factor from evidence units (pdf_pt or image_px) to these pixels, and the offset (in
    evidence units) of the rendered part: pixel = (unit - offset) * factor.

    A PDF is rendered only where it has ink (W5 14:55: a 1:200 floor on an A3 sheet fills ~1/5 of the page, so the whole
    page at 2,400 px gave rooms ~60 px wide and areas ~12 % small), at 2,400 px on the long side of that part."""
    import cv2

    if abs_path.lower().endswith(".pdf"):
        import pymupdf

        with pymupdf.open(abs_path) as doc:
            pg = doc[min(max(page, 0), len(doc) - 1)]
            # W6 18:14: render the UNROTATED page (pdf_pt, CONTRACT): on a /Rotate 90 sheet (5 of Stoa's 9 PDFs) the
            # render was turned while clicks and outlines are unrotated, so every click read the wrong spot. In memory only.
            if pg.rotation:
                pg.set_rotation(0)
            r = pg.rect
            clip = r
            try:  # the inked part, from a coarse render (text, title block and frame count too: never cuts the plan)
                z0 = 600 / max(r.width, r.height)
                pm = pg.get_pixmap(matrix=pymupdf.Matrix(z0, z0), colorspace=pymupdf.csGRAY, alpha=False)
                g = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width)
                ys, xs = np.nonzero(g < 200)
                if len(xs):
                    pad = 0.01 * max(r.width, r.height)
                    clip = pymupdf.Rect(max(r.x0, r.x0 + xs.min() / z0 - pad), max(r.y0, r.y0 + ys.min() / z0 - pad),
                                        min(r.x1, r.x0 + (xs.max() + 1) / z0 + pad), min(r.y1, r.y0 + (ys.max() + 1) / z0 + pad))
            except Exception:  # noqa: BLE001
                clip = r
            z = LONG_SIDE / max(clip.width, clip.height)
            z = min(max(z, 1.0), 8.0)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
            img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()
            return img, z, (clip.x0 - r.x0, clip.y0 - r.y0)
    img = cv2.imread(abs_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("unreadable image")
    f = min(1.0, LONG_SIDE / max(img.shape))
    if f < 1.0:
        img = cv2.resize(img, (round(img.shape[1] * f), round(img.shape[0] * f)), interpolation=cv2.INTER_AREA)
    return img, f, (0.0, 0.0)


def walls_mask(gray):
    """Dark strokes as 255, small marks (text, hatching dots) removed."""
    import cv2

    h, w = gray.shape
    block = max(15, (max(h, w) // 80) | 1)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 12)
    # also everything clearly dark (thick filled walls whose inside the adaptive threshold leaves white)
    bw |= (gray < 110).astype(np.uint8) * 255
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    small = max(6, int(0.012 * max(h, w)))
    keep = np.zeros(n, np.uint8)
    keep[1:] = ((stats[1:, cv2.CC_STAT_WIDTH] >= small) | (stats[1:, cv2.CC_STAT_HEIGHT] >= small)).astype(np.uint8) * 255
    return keep[lab]


def region(walls, seed, r: int):
    """Flood-filled free region around seed with walls thickened by r px (None if the seed is not free)."""
    import cv2

    h, w = walls.shape
    wl = cv2.dilate(walls, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))) if r else walls
    x, y = seed
    if wl[y, x]:  # the seed sits on a stroke: nearest free pixel within a small window
        win = max(4, r + 6)
        ys, xs = np.nonzero(wl[max(0, y - win):y + win + 1, max(0, x - win):x + win + 1] == 0)
        if not len(xs):
            return None
        k = np.argmin((xs + max(0, x - win) - x) ** 2 + (ys + max(0, y - win) - y) ** 2)
        x, y = int(xs[k] + max(0, x - win)), int(ys[k] + max(0, y - win))
    mask = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(wl.copy(), mask, (x, y), 128, flags=4 | (255 << 8) | cv2.FLOODFILL_MASK_ONLY)
    reg = mask[1:-1, 1:-1]
    if r:
        reg = cv2.dilate(reg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))) & (walls == 0).astype(np.uint8) * 255
    return reg


CLOSING = (0.0, 0.003, 0.006, 0.010)
ABSORB = os.environ.get("ROOM_ABSORB", "1") != "0"  # absorb(): fixture-cut parts of the room (env ROOM_ABSORB=0 turns it off for comparisons)


def smooth(reg, seed, k: int):
    """Cut thin spikes (the fill running into window or door gaps) and keep the part holding the seed."""
    import cv2

    if k >= 3:
        reg = cv2.morphologyEx(reg, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab = cv2.connectedComponents(reg, connectivity=4)
    if n <= 2:
        return reg
    x, y = seed
    keep = lab[y, x]
    if not keep:  # the seed pixel was cut off: the biggest part
        keep = 1 + int(np.argmax(np.bincount(lab.ravel())[1:]))
    return (lab == keep).astype(np.uint8) * 255


def holds(reg, pts, rad: int = 2) -> bool:
    """True when the region covers any of pts (another room's printed label: the fill leaked into that room)."""
    h, w = reg.shape
    for x, y in pts:
        x, y = int(round(x)), int(round(y))
        if 0 <= x < w and 0 <= y < h and reg[max(0, y - rad):y + rad + 1, max(0, x - rad):x + rad + 1].any():
            return True
    return False


def absorb(reg, walls):
    """Add the parts of the room that a fixture line cut off (a kitchen counter's front edge, a wardrobe, a shower):
    free regions lying INSIDE the room's box (a wall cavity or the next room runs past it) that touch the room along a
    long edge across one thin line. Swiss kitchens were traced ~50 % small without this (W5 15:15)."""
    import cv2

    ys, xs = np.nonzero(reg)
    if not len(xs):
        return reg
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()
    m = max(3, int(0.004 * max(walls.shape)))  # a fixture line is a few px wide at 2,400 px
    # a counter strip along one wall lies OUTSIDE the box of what the fill reached: look up to ~1/3 of the room's
    # short side beyond it (a wall cavity or the next room still runs past that window and stays out)
    m += int(0.35 * min(x1 - x0, y1 - y0))
    free = ((walls == 0) & (reg == 0)).astype(np.uint8)
    sub = free[max(0, y0 - m):y1 + m + 1, max(0, x0 - m):x1 + m + 1]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=4)
    if n <= 1:
        return reg
    rsub = reg[max(0, y0 - m):y1 + m + 1, max(0, x0 - m):x1 + m + 1]
    t = max(3, int(0.0013 * max(walls.shape)))  # the ring across one fixture line (a hairline, not a wall)
    ring = (cv2.dilate(rsub, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * t + 1, 2 * t + 1))) > 0) & (rsub == 0)
    touch = np.bincount(lab[ring], minlength=n)
    H, W = sub.shape
    room = float(len(xs))
    add = np.zeros_like(rsub)
    for k in range(1, n):
        bx, by, bw, bh, a = stats[k]
        if bx == 0 or by == 0 or bx + bw >= W or by + bh >= H:
            continue  # reaches past the room's box: a wall cavity, a corridor, the outside
        if a < 0.01 * room or a > 0.6 * room:
            continue
        if touch[k] < 0.5 * max(bw, bh):
            continue  # touches only at a corner or a point
        add[lab == k] = 255
    if not add.any():
        return reg
    out = reg.copy()
    out[max(0, y0 - m):y1 + m + 1, max(0, x0 - m):x1 + m + 1] |= add
    # close the fixture line between the room and what was added
    return cv2.morphologyEx(out, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (t, t))) & ((walls == 0) | (out > 0)).astype(np.uint8) * 255


def outline(walls, seed, evbox_px, footprint: float, start: int = 0, others=()):
    import cv2

    h, w = walls.shape
    big = evbox_px[2] - evbox_px[0] > 0.02 * w and evbox_px[3] - evbox_px[1] > 0.02 * h  # a room-sized evidence box
    for level in range(start, len(CLOSING)):
        r = int(round(CLOSING[level] * max(h, w)))
        reg = region(walls, seed, r)
        if reg is None:
            continue
        reg = smooth(reg, seed, int(0.004 * max(h, w)) | 1)
        ys, xs = np.nonzero(reg)
        if len(xs) < 30:
            continue
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max(), ys.max()
        if x0 <= 1 or y0 <= 1 or x1 >= w - 2 or y1 >= h - 2:
            continue  # leaked outside the building
        if len(xs) > 0.45 * footprint:
            continue  # most of the plan: leaked through a door
        if others and holds(reg, others):
            continue  # another room's label inside: leaked through a door opening into that room
        if ABSORB:
            reg = absorb(reg, walls)
            ys, xs = np.nonzero(reg)
        fit = None
        if big:
            fit = iou((x0, y0, x1, y1), evbox_px)
            if fit < 0.35:
                continue
        cs, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cs, key=cv2.contourArea)
        c = cv2.approxPolyDP(c, max(1.5, 0.004 * cv2.arcLength(c, True)), True)
        if len(c) < 3:
            continue
        return c.reshape(-1, 2), float(len(xs)), r, fit, level, (int(x0), int(y0), int(x1), int(y1))
    return None


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    gray, f, (ox, oy) = load(req["abs"], int(req.get("page") or 0))
    walls = walls_mask(gray)
    # the plan's footprint: the box around all strokes (a sheet's title block counts too; it only loosens the check)
    ys, xs = np.nonzero(walls)
    footprint = float((xs.max() - xs.min()) * (ys.max() - ys.min())) if len(xs) else float(walls.size)
    items = [it for it in req.get("items") or [] if it.get("bbox") and len(it["bbox"]) >= 4]
    res: dict[int, tuple | None] = {}
    start = {k: 0 for k in range(len(items))}
    hh, ww = walls.shape
    thin_px = max(10.0, 0.008 * max(hh, ww))  # ~0.7 m at 1:200 on an A3 render: thinner than any door
    open_walls: list = []

    def thin(r) -> bool:
        """UX NEW-R1: on a parquet-hatched floor the fill stopped at the floorboard lines: a 'room' 120 x 6 px. A room is
        never thinner than a door."""
        import cv2

        x0, y0, x1, y1 = r[5]
        a, b = sorted((x1 - x0 + 1, y1 - y0 + 1))
        # W6 01:51: a 16-17 px strip passed 'a < thin_px' (thin_px 16 on a 2000 px picture). The mean width 2A/P of
        # the outline catches it whatever its bounding box (a 16 x 300 strip: 15 px; a 2 m room at 1:100: ~100 px).
        per = float(cv2.arcLength(np.asarray(r[0], np.float32).reshape(-1, 1, 2), True))
        mean_w = 2.0 * r[1] / per if per > 0 else 0.0
        return a <= thin_px or mean_w < thin_px or (b > 25 * a and a < 2 * thin_px)  # a long corridor is not thin

    def opened(kk: int):
        """The wall mask with pattern lines thinner than kk px (floorboards, tiles, hatching) opened away."""
        import cv2

        while len(open_walls) <= kk:
            open_walls.append(None)
        if open_walls[kk] is None:
            open_walls[kk] = cv2.morphologyEx(walls, cv2.MORPH_OPEN, np.ones((kk, kk), np.uint8))
        return open_walls[kk]

    def hatch(r, kk: int) -> float:
        """Share of the region's pixels that are pattern lines (in the wall mask, gone once opened with kk)."""
        import cv2

        m = np.zeros_like(walls)
        cv2.fillPoly(m, [np.asarray(r[0], np.int32)], 255)
        inside = m > 0
        return float(((walls > 0) & (opened(kk) == 0) & inside).sum()) / max(1, int(inside.sum()))

    def run(k: int):
        bb = items[k]["bbox"][:4]
        b = [(bb[0] - ox) * f, (bb[1] - oy) * f, (bb[2] - ox) * f, (bb[3] - oy) * f]
        seed = (int(min(max((b[0] + b[2]) / 2, 0), walls.shape[1] - 1)), int(min(max((b[1] + b[3]) / 2, 0), walls.shape[0] - 1)))
        # optional: other rooms' label points (same space) that must stay outside this room's outline (W5: a room
        # found by its printed name on an upload; the other names on the sheet are the other rooms)
        others = [((float(p[0]) - ox) * f, (float(p[1]) - oy) * f) for p in (items[k].get("others") or [])[:200] if len(p) >= 2]
        try:
            r = outline(walls, seed, b, footprint, start[k], others)
            # UX NEW-R1 (parquet / tiled floors): the fill stops at the pattern lines (a floorboard: 120 x 6 px). Open
            # the pattern away step by step while the room found is full of pattern lines (>= 3 % of it) and keeps
            # growing; a leak (None) stops it. Plans without patterns keep the first outline (DfE: opened = none).
            if r is not None and thin(r) and os.environ.get("ROOM_UNHATCH", "1") != "0":
                best = None
                for kk in (3, 5, 7):
                    r2 = outline(opened(kk), seed, b, footprint, start[k], others)
                    if r2 is None or hatch(r2, kk) < 0.03 or (best is not None and r2[1] <= 1.2 * best[1]):
                        break
                    best = r2
                r = best if best is not None and not thin(best) else None
            return r
        except Exception as e:  # noqa: BLE001
            print(f"room_shape: {e}", file=sys.stderr)
            return None

    # vector PDF at a known scale: the wall-line tracer first (items it outlines skip the flood fill)
    lined: dict[int, tuple] = {}
    for k, it in enumerate(items):
        bb = it["bbox"][:4]
        mid = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
        hit = None
        if it.get("scale_n"):
            hit = wall_lines(req["abs"], int(req.get("page") or 0), mid, float(it["scale_n"]))
        elif it.get("px_per_m"):  # a picture at a known scale with a traced copy (W6 00:48)
            hit = pic_lines(req["abs"], mid, float(it["px_per_m"]))
        if hit:
            lined[k] = hit
    todo = [k for k in range(len(items)) if k not in lined]
    for _ in range(len(CLOSING)):
        for k in todo:
            res[k] = run(k)
        # two items of the answer in one region: the fill leaked through a door between them -> thicker walls for both
        by_box: dict[tuple, list[int]] = {}
        for k, r in res.items():
            if r is not None:
                by_box.setdefault(r[5], []).append(k)
        todo = [k for ks in by_box.values() if len(ks) > 1 for k in ks]
        if not todo:
            break
        for k in todo:
            start[k] = res[k][4] + 1
            if start[k] >= len(CLOSING):
                res[k] = None
        todo = [k for k in todo if start[k] < len(CLOSING)]
    out = []
    for k, it in enumerate(items):
        if k in lined:
            poly, area, how = lined[k]
            out.append({"i": it.get("i"), "poly": [[round(float(x), 2), round(float(y), 2)] for x, y in poly], "area": round(area, 2),
                        "closing_px": 0, "fit": None, "method": how})
            continue
        r = res.get(k)
        if r is None:
            out.append({"i": it.get("i"), "poly": None})
            continue
        poly, area_px, rr, fit = r[:4]
        out.append({"i": it.get("i"), "poly": [[round(float(x) / f + ox, 2), round(float(y) / f + oy, 2)] for x, y in poly],
                    "area": round(area_px / (f * f), 2), "closing_px": rr, "fit": None if fit is None else round(fit, 2)})
    # derotated (W6 18:45): PDF pages are rendered unrotated (load()), so flood-fill outlines on /Rotate sheets are in pdf_pt
    # too; the phone page trusts flood fill on rotated pages only when this flag is set.
    print(json.dumps({"shapes": out, "space": req.get("space") or "pdf_pt", "method": "flood fill (CPU)", "derotated": True}))


if __name__ == "__main__":
    main()
