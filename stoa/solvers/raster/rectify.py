"""Phone photos of printed sheets: find the paper's quadrilateral and warp it flat before reading (O1).

X1 wild exam: a Floorplanner print photographed at ~4° with perspective on a grey table -> PP-OCRv6 read 5 lines
('2m' / '4m' of the vertical scale bar missed, 'Salón' split) and no scale bar was found; the flat render of the same
sheet reads 6 lines and the bar. A sheet is the largest bright region whose outline simplifies to 4 corners, covering
25-97% of the photo and not touching every border (a scan / render IS the sheet: nothing to do).

    rectified(path) -> (flat image path, H) | None      H: 3x3 homography photo px -> flat px
    to_photo(points, H) -> the same points in the photo  (evidence goes back onto the uploaded image)

NOT WIRED IN (measured 14:58): raster on the flattened vs the original photo, X1 wild's 23 scored _ph questions: 8 -> 8
right (W: esino W.C. count 3, pagen '21 Stg.'; L: puntar studio 31 -> 34 m², esino level difference 1.7 -> 0.65). The
detector itself fired on all 5 photos and on none of the 6 scans / renders / PNG copies. Next step that would make it
pay: snap scale-bar labels to the drawn bar's block edges (the flattened fplan photo reads '0m', '2m' but not '4m'; the
label fit is 5% off and the drawn run continues past the last read label, so snap() rejects it).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import cv2
import numpy as np


def _order(q: np.ndarray) -> np.ndarray:
    """4 corners -> top-left, top-right, bottom-right, bottom-left."""
    s, d = q.sum(1), np.diff(q, axis=1)[:, 0]
    return np.array([q[np.argmin(s)], q[np.argmin(d)], q[np.argmax(s)], q[np.argmax(d)]], np.float32)


def sheet_quad(g: np.ndarray) -> np.ndarray | None:
    """Corners (tl, tr, br, bl) of the paper in a grey photo, or None."""
    H, W = g.shape
    k = 1000 / max(H, W)
    small = cv2.resize(g, (round(W * k), round(H * k)), interpolation=cv2.INTER_AREA) if k < 1 else g.copy()
    k = min(k, 1.0)
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))  # drawing ink inside the paper
    cs, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    a = cv2.contourArea(c) / (small.shape[0] * small.shape[1])
    if not 0.25 <= a <= 0.97:
        return None
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for eps in (0.01, 0.02, 0.03, 0.05):
        q = cv2.approxPolyDP(hull, eps * peri, True)
        if len(q) == 4:
            break
    else:
        return None
    q = q[:, 0, :].astype(np.float32) / k
    x, y = q[:, 0], q[:, 1]
    m = 0.01 * max(H, W)
    if x.min() < m and y.min() < m and x.max() > W - 1 - m and y.max() > H - 1 - m:
        return None  # the paper fills the frame: a scan
    # the background must be clearly darker than the paper (a white table under a white sheet is no signal)
    inside = np.zeros_like(small)
    cv2.fillPoly(inside, [np.round(q * k).astype(np.int32)], 1)
    if float(np.median(small[inside == 0])) > float(np.median(small[inside == 1])) - 40:
        return None
    return _order(q)


def rectified(path: str, cache_dir: str | None = None) -> tuple[str, np.ndarray] | None:
    """The flat sheet as a PNG (cached by content) + the photo->flat homography, or None (not a photo of a sheet, or
    the sheet is already axis-aligned within 0.5° and fills its box)."""
    from PIL import Image

    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB"))
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    q = sheet_quad(g)
    if q is None:
        return None
    tl, tr, br, bl = q
    w = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    h = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    if w < 300 or h < 300:
        return None
    ang = max(abs(np.degrees(np.arctan2(tr[1] - tl[1], tr[0] - tl[0]))),
              abs(np.degrees(np.arctan2(br[1] - bl[1], br[0] - bl[0]))))
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    Hm = cv2.getPerspectiveTransform(q, dst)
    if ang < 0.5 and abs(np.linalg.norm(bl - tl) - np.linalg.norm(br - tr)) < 2:
        return None
    d = Path(cache_dir or (Path(os.environ.get("JUNCTION_SHARED", "/tmp")) / "artifacts" / "O1" / "rectified"))
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(rgb.tobytes()[:: max(1, rgb.size // 2_000_000)] + str(rgb.shape).encode()).hexdigest()[:16]
    out = d / f"{key}.png"
    if not out.exists():
        flat = cv2.warpPerspective(rgb, Hm, (w, h), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
        Image.fromarray(flat).save(out)
    return str(out), Hm


def to_photo(points, Hm: np.ndarray) -> list[list[float]]:
    pts = np.asarray(points, np.float32).reshape(-1, 1, 2)
    back = cv2.perspectiveTransform(pts, np.linalg.inv(Hm))[:, 0, :]
    return [[round(float(x), 1), round(float(y), 1)] for x, y in back]
