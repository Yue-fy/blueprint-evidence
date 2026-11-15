"""Door swing arcs on a raster floor plan (matched filter), for door counts on plans without a vector twin.

A door is drawn as a leaf plus a quarter arc of radius = leaf width (0.35-1.35 m) centred on the hinge. With the
drawing's scale (px per metre, scalebar.py) the arc radius range in pixels is known, so a quarter-ring template per
radius and quadrant, scored as (ink on the ring) - (ink on rings 3.5 px inside and outside), finds arcs and ignores
straight lines and filled/hatched areas. Rejected: full circles (north arrow, round fixtures: >= 3 quadrants inked)
and anything below the legend / title-block rule (a horizontal line across most of the sheet in its lower half).
Measured on TRAIN Swiss floors against DXF arcs mapped to PNG px (experiments/w3/doorwin/arcs.py).

detect(png, px_per_m) -> [{"c": [x, y], "r_px", "q", "score", "box"}] in image px.
"""
from __future__ import annotations

import functools
import math
import os

import numpy as np
from PIL import Image

CONF = 0.15  # exact on few TRAIN floors (count_rules.py); an estimate with per-door evidence
DUP_RED = 5  # red (apartment boundary) pixels around a door -> the source data counts it once per side
R_M = (0.55, 1.35)  # 0.35: +fixtures (toilets, basins) as FPs, precision 0.89 -> 0.58; DXF radii < 0.55 m are 3%
THR = 0.55
INK = 210


@functools.lru_cache(maxsize=512)
def ring_kernel(r: float, quad: int, width: float = 1.0, margin_deg: float = 10.0):
    """(on, off) masks: on = pixels within `width` of radius r in quadrant `quad` (image angles quad*90..+90, y
    down, minus a margin at both ends); off = rings at r -+ 3.5 px over the same angles."""
    R = int(math.ceil(r + 5))
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1].astype(float)
    rr = np.hypot(xx, yy)
    ang = (np.degrees(np.arctan2(yy, xx)) + 360) % 360
    sector = (ang >= quad * 90 + margin_deg) & (ang <= quad * 90 + 90 - margin_deg)
    on = (np.abs(rr - r) <= width) & sector
    off = ((np.abs(rr - (r - 3.5)) <= 0.8) | (np.abs(rr - (r + 3.5)) <= 0.8)) & sector
    return on.astype(np.float32), off.astype(np.float32)


def _score_at(ink: np.ndarray, x: int, y: int, r: float, q: int) -> float:
    on, off = ring_kernel(round(r * 2) / 2, q)
    R = on.shape[0] // 2
    H, W = ink.shape
    if x - R < 0 or y - R < 0 or x + R + 1 > W or y + R + 1 > H:
        return 0.0
    patch = ink[y - R:y + R + 1, x - R:x + R + 1]
    return float((patch * on).sum() / on.sum() - (patch * off).sum() / off.sum())


def legend_cut(ink: np.ndarray) -> int:
    """y of the topmost horizontal rule across >= 70% of the width in the lower half (Swiss legend / title block);
    the image height when there is none."""
    H, W = ink.shape
    rows = ink[H // 2:].mean(axis=1)
    ys = np.nonzero(rows >= 0.7)[0]
    return int(H // 2 + ys[0]) if len(ys) else H


ARC_THREADS = max(1, int(os.environ.get("STOA_O1_ARC_THREADS", "6") or 1))  # 1 = the sequential loop; Stoa DfE 6000-px
# copy (128 arcs, identical): 36.8 s sequential, 14.9 s with 4 threads, 9.9 s with 8 (box load ~250)


def _arc_chunk(work: np.ndarray, pairs: list, base: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Best ring score per pixel over (r, q) pairs, in order (strict '>': earlier pairs win ties), and the index
    (base + position) of the pair that gave it. O1 c16 (X1 03:12: a 37 Mpx traced scan took the process 0.3 -> 8.6 GB):
    one score buffer and one mask are reused for every pair and the winner is one uint16 index instead of a float32
    radius + an int8 quadrant -> ~2.7 instead of ~4.5 work-sized float32 arrays per running chunk, identical results."""
    import cv2

    best = np.full(work.shape, -1.0, np.float32)
    idx = np.zeros(work.shape, np.uint16)
    s = np.empty(work.shape, np.float32)
    m = np.empty(work.shape, bool)
    for j, (r, q) in enumerate(pairs):
        on, off = ring_kernel(float(r), q)
        kern = on / on.sum() - off / off.sum()  # filter2D correlates (no flip needed)
        cv2.filter2D(work, cv2.CV_32F, kern, dst=s, borderType=cv2.BORDER_CONSTANT)
        np.greater(s, best, out=m)
        np.copyto(best, s, where=m)
        idx[m] = base + j
    return best, idx


def _fold(best: np.ndarray, idx: np.ndarray, b: np.ndarray, bi: np.ndarray) -> None:
    m = b > best  # later chunks win only when strictly better (the sequential loop's tie rule)
    np.copyto(best, b, where=m)
    np.copyto(idx, bi, where=m)


def detect(png: str, px_per_m: float, r_m: tuple[float, float] = R_M, thr: float = THR, ink_lvl: int = INK,
           max_side: int = 3600) -> list[dict]:
    import cv2

    im = Image.open(png).convert("L")
    k = min(1.0, max_side / max(im.size))
    if px_per_m * k < 45:  # 1:200 sheets (29.5 px/m): the filters are tuned at ~59 px/m; upscale (331 dets -> ?)
        k = min(59.0 / px_per_m, 2 * max_side / max(im.size))
    if k != 1.0:  # never below ~2 px line spacing: arcs vanish when downscaled (half-res recall 7% vs 91% full)
        im = im.resize((round(im.size[0] * k), round(im.size[1] * k)), Image.BICUBIC if k > 1 else Image.LANCZOS)
    g = np.asarray(im)
    del im
    ink = (g < ink_lvl).astype(np.float32)
    del g
    ppm = px_per_m * k
    cut = legend_cut(ink)
    work = ink[:cut]
    # search from 0.1 m below the smallest door: a straight wall / window line scores higher as the ring shrinks, so
    # its best radius is the search minimum; a real arc peaks at its own radius (1:200 FPs all sat at r_min)
    radii, r = [], max(6.0, (r_m[0] - 0.1) * ppm)
    while r <= r_m[1] * ppm + 0.01:  # step <= 2 px: the +-1 px ring still covers an arc between two radii
        radii.append(r)
        r += max(1.0, min(2.0, 0.04 * r))
    pairs = [(r, q) for r in radii for q in range(4)]
    if ARC_THREADS > 1 and len(pairs) > 8:
        # P1 18:01: 124 sequential filter2D on Stoa's DfE sheet (7009 x 4957 after the upscale) = 27 of 35 s on the
        # upload's critical path. cv2 releases the GIL: chunks run in threads, each folds its own best in (r, q)
        # order, and the chunks are folded here in order with the same strict '>' -> identical results (ties keep the
        # earlier (r, q)). Waves of ARC_THREADS chunks bound memory; a chunk's result is dropped as soon as it is
        # folded (O1 c16: the wave's futures used to hold every result until the next wave).
        from concurrent.futures import ThreadPoolExecutor

        size = max(4, -(-len(pairs) // (3 * ARC_THREADS)))
        starts = list(range(0, len(pairs), size))
        best = idx = None
        with ThreadPoolExecutor(ARC_THREADS) as ex:
            for i in range(0, len(starts), ARC_THREADS):
                fs = [ex.submit(_arc_chunk, work, pairs[a:a + size], a) for a in starts[i:i + ARC_THREADS]]
                for j in range(len(fs)):
                    b, bi = fs[j].result()
                    fs[j] = None
                    if best is None:
                        best, idx = b, bi
                    else:
                        _fold(best, idx, b, bi)
                    del b, bi
    else:
        best, idx = _arc_chunk(work, pairs)
    pr = np.asarray([p[0] for p in pairs], np.float64)
    pq = np.asarray([p[1] for p in pairs], np.int64)
    sz = int(max(5, 0.35 * ppm)) | 1
    peaks = (best >= cv2.dilate(best, np.ones((sz, sz), np.uint8))) & (best >= thr)
    ys, xs = np.nonzero(peaks)
    dets = []
    for y, x in zip(ys, xs):
        r, q = float(np.float32(pr[idx[y, x]])), int(pq[idx[y, x]])
        if r < r_m[0] * ppm - 0.01:
            continue
        quads = [_score_at(ink, int(x), int(y), r, j) for j in range(4)]
        if sum(s >= 0.5 * thr for s in quads) >= 3:
            continue  # a full circle: north arrow, round table, fixture
        dets.append({"c": [float(x / k), float(y / k)], "r_px": r / k, "q": q, "score": float(best[y, x])})
    dets.sort(key=lambda d: -d["score"])
    keep: list[dict] = []
    for d in dets:
        if all(math.dist(d["c"], e["c"]) > 0.3 * px_per_m for e in keep):
            keep.append(d)
    for d in keep:  # the swept quarter disc, in image px
        (x, y), r, q = d["c"], d["r_px"], d["q"]
        sx, sy = (1 if q in (0, 3) else -1), (1 if q in (0, 1) else -1)
        xs_ = sorted([x, x + sx * r])
        ys_ = sorted([y, y + sy * r])
        d["box"] = [round(xs_[0], 1), round(ys_[0], 1), round(xs_[1], 1), round(ys_[1], 1)]
        d["r_m"] = round(r / px_per_m, 2)
    return keep


def red_near(rgb: np.ndarray, d: dict, px_per_m: float) -> int:
    """Pixels of the red dash-dot apartment boundary (176, 58, 46) within the door's swept quarter disc + 0.1 m."""
    bx = d["box"]
    x, y = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
    rad = d["r_px"] / 2 + 0.1 * px_per_m
    H, W = rgb.shape[:2]
    x0, x1, y0, y1 = max(0, int(x - rad)), min(W, int(x + rad) + 1), max(0, int(y - rad)), min(H, int(y + rad) + 1)
    p = rgb[y0:y1, x0:x1].astype(int)
    m = (abs(p[..., 0] - 176) < 40) & (abs(p[..., 1] - 58) < 40) & (abs(p[..., 2] - 46) < 40)
    return int(m.sum())


DOOR_LEAF_M = 0.86  # median DXF door radius on 60 TRAIN Swiss floors (per-floor medians 0.66-1.07)


def scale_from_arcs(png: str) -> tuple[float | None, int]:
    """(px per m, #arcs) WITHOUT a scale bar: the median radius of the stronger half of the swing arcs found over
    12 px .. long side / 25, read as a standard door leaf (DOOR_LEAF_M). Swiss dev (100 floors, native scale):
    within 10% of the bar on 71/97, 15% on 87/97 (experiments/w3/doorwin/windows_doorscale.py). (None, n) on < 3 arcs.
    detect() gets a pseudo-scale of 46 px per 'm' so its 1:200 upscale rule does not fire."""
    import statistics

    w = max(Image.open(png).size)
    lo, hi, ps = 12.0, max(40.0, w / 25), 46.0
    dets = detect(png, ps, r_m=(lo / ps, hi / ps))
    if len(dets) < 3:
        return None, len(dets)
    top = sorted(dets, key=lambda d: -d["score"])[: max(3, len(dets) // 2)]
    med = statistics.median(d["r_px"] for d in top)
    if LEAF_GATE and med < LEAF_BELOW * lo and leaf_share(png, top) < LEAF_SHARE:
        return None, len(dets)  # near the radius floor and mostly no door leaves: not door swings (W6 17:42)
    return med / DOOR_LEAF_M, len(dets)


# W6 17:42 (night-W6 616f21f web/src/mobile/doorscale.py, measured there; ported unchanged): near the detector's 12 px
# radius floor the radius steps put the median of ANY small quarter strokes at ~15 px, so photos of big sheets read the
# same ~30 px/m whatever the sheet (USACE S-100F / M-101A photos: truth ~95). A door swing has a LEAF (ink along one
# bounding radius): DfE 93 %, Fraser 100 %, Pagenstr 80 % vs S-100F 12 %, M-101A 45 %. Applied only below 1.5x the
# floor (CubiCasa bare-arc swings well above it keep their estimate). STOA_O1_LEAF_GATE=0 = off.
LEAF_SHARE, LEAF_BELOW, LEAF_INK = 0.65, 1.5, 0.75
LEAF_GATE = os.environ.get("STOA_O1_LEAF_GATE", "1") != "0"


def leaf_share(png: str, dets: list[dict]) -> float:
    """Share of door-arc detections (image px) with a straight radial line of ink from the hinge along one of the
    quadrant's bounding radii (0.15-0.9 r, 1 px slack) - a door leaf. Rounded box corners, pipe bends and symbol strokes
    have none."""
    from PIL import ImageFilter

    ink = np.asarray(Image.open(png).convert("L").filter(ImageFilter.MinFilter(3))) < INK
    h, w = ink.shape

    def frac(cx: float, cy: float, th: float, r: float) -> float:
        n = hit = 0
        for t in np.linspace(0.15, 0.9, 16):
            x, y = int(round(cx + math.cos(th) * t * r)), int(round(cy + math.sin(th) * t * r))
            if 0 <= x < w and 0 <= y < h:
                n, hit = n + 1, hit + int(ink[y, x])
        return hit / max(1, n)

    leaves = sum(max(frac(*d["c"], math.radians(d["q"] * 90), d["r_px"]),
                     frac(*d["c"], math.radians(d["q"] * 90 + 90), d["r_px"])) >= LEAF_INK for d in dets)
    return leaves / max(1, len(dets))
