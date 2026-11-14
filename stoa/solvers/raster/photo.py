"""Phone photos of printed sheets (O1, cycle 7): find the paper, undo the perspective, flatten the light.

Stoa (Sat 12:40): the hidden test may upload 'phone photos of prints'. X1 18:25 measured such photos of sheets whose
PDF / PNG we answer: 1/7 sealed, 0/5 dev; they were classified as historical scans and the readers met a tilted,
unevenly lit sheet on a dark table with no usable pixel scale.

    deskew(src) -> Deskewed | None
        None unless the image shows ONE bright sheet of paper fully inside the frame on a clearly darker surround
        (a clean export, a flatbed scan or a crop never qualifies: their 'paper' touches the frame).
        Deskewed.path  = '<cache>/<stem>.photo.png', the sheet warped to a rectangle of the standard paper aspect
                         nearest to the one recovered from the quad (ISO A 1:1.414, ANSI, ARCH), lighting divided out,
                         DPI tag for A3 of that family (a print's usual size; the printed '1:200 @ A1' note rescales it,
                         and the page's px/m does not depend on the assumed size when the sheet names its paper)
        Deskewed.to_photo = 3x3 homography deskewed px -> photo px (evidence goes back onto the uploaded photo)
    map_points(H, pts) / map_box(H, box)

Aspect recovery: Zhang & He, 'Whiteboard scanning and image enhancement' (2007): with the principal point at the image
centre and square pixels the quad gives the focal length and the true width/height ratio; falls back to the mean
opposite side lengths when that is degenerate (a near-fronto-parallel shot).
Kill switch: STOA_O1_PHOTO=0.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# standard sheets: name -> (short, long) mm; the family's A3-equivalent is the size we tag
PAPERS = {"ISO A": (297.0, 420.0), "ANSI A/C/E": (216.0, 279.0), "ANSI B/D": (279.0, 432.0),
          "ARCH A/C/E": (229.0, 305.0), "ARCH B/D": (305.0, 457.0), "Legal": (216.0, 356.0)}
# ARCH E1 (1.40) is left out: it is ISO A's ratio within 1 %, and ISO A is the common case
ASPECT_TOL = 0.035  # snap the recovered long/short ratio to a standard one within 3.5 %
MIN_AREA, MAX_AREA = 0.20, 0.985  # the sheet's share of the frame
MIN_CONTRAST = 35.0  # paper median minus surround median (grey levels)
EDGE_MARGIN = 0.004  # every corner this far inside the frame (share of the long side)
VERSION = "p3"
PHOTO_KEY = "stoa_photo"  # PNG text chunk on the deskewed copy: the paper family
PHOTO_DPI_KEY = "stoa_photo_a3dpi"  # its dpi IF the print were the family's A3-size sheet (a prior, not a tag)
FLATTEN = os.environ.get("STOA_O1_PHOTO_FLAT", "1") != "0"  # divide out the light (paper -> white)
F_MIN, F_MAX = 0.3, 4.0  # plausible focal length, share of the image diagonal (phones ~0.6-1.3)


@dataclass
class Deskewed:
    path: str
    to_photo: list[list[float]]  # 3x3, deskewed px -> photo px
    paper: str  # family name ('ISO A')
    aspect: float  # recovered long/short before snapping
    snapped: bool
    dpi: float
    size: tuple[int, int]  # (w, h) of the deskewed image
    corners: list[list[float]]  # TL, TR, BR, BL in photo px
    notes: list[str] = field(default_factory=list)


def enabled() -> bool:
    return os.environ.get("STOA_O1_PHOTO", "1") != "0"


def _order(pts: np.ndarray) -> np.ndarray:
    """TL, TR, BR, BL."""
    pts = np.asarray(pts, dtype=np.float64).reshape(4, 2)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]])


def _paper_mask(gray: np.ndarray) -> tuple[np.ndarray, float] | None:
    """Largest bright component (holes filled) + Otsu threshold, on a small copy."""
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    t, m = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = max(3, round(min(gray.shape) / 60)) | 1
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    comp = (lab == i).astype(np.uint8) * 255
    cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(comp)
    cv2.drawContours(filled, cnts, -1, 255, -1)
    return filled, float(t)


def _quad(mask: np.ndarray) -> np.ndarray | None:
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for eps in (0.01, 0.02, 0.03, 0.04, 0.05):
        ap = cv2.approxPolyDP(hull, eps * peri, True)
        if len(ap) == 4:
            return _order(ap.reshape(4, 2))
    return None


def _refine(gray: np.ndarray, quad: np.ndarray, thr: float) -> np.ndarray:
    """Fit each side to the paper edge at full resolution (sub-pixel crossings of the paper/table threshold along
    perpendicular profiles, robust line fit), corners = intersections of adjacent sides."""
    h, w = gray.shape
    lines = []
    for k in range(4):
        a, b = quad[k], quad[(k + 1) % 4]
        d = b - a
        L = float(np.hypot(*d))
        if L < 10:
            return quad
        t = d / L
        nrm = np.array([-t[1], t[0]])
        cen = quad.mean(0)
        if np.dot(cen - (a + b) / 2, nrm) < 0:  # normal points INTO the paper
            nrm = -nrm
        reach = max(6.0, 0.01 * L)
        pts = []
        for s in np.linspace(0.08, 0.92, 60):
            p0 = a + d * s
            offs = np.arange(-reach, reach + 0.5, 0.5)
            xs, ys = p0[0] + nrm[0] * offs, p0[1] + nrm[1] * offs
            if xs.min() < 0 or ys.min() < 0 or xs.max() > w - 1 or ys.max() > h - 1:
                continue
            prof = cv2.remap(gray.astype(np.float32), xs.astype(np.float32).reshape(1, -1),
                             ys.astype(np.float32).reshape(1, -1), cv2.INTER_LINEAR).ravel()
            prof = np.convolve(prof, np.ones(3) / 3, mode="same")
            above = prof >= thr
            j = np.nonzero(above[1:] & ~above[:-1])[0]  # outside (dark) -> inside (bright)
            if len(j) == 0:
                continue
            j = j[np.argmin(np.abs(offs[j]))]
            f = (thr - prof[j]) / max(1e-6, prof[j + 1] - prof[j])
            o = offs[j] + 0.5 * f
            pts.append(p0 + nrm * o)
        if len(pts) < 10:
            return quad
        vx, vy, x0, y0 = cv2.fitLine(np.float32(pts), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((np.array([x0, y0]), np.array([vx, vy])))
    out = []
    for k in range(4):
        (p1, d1), (p2, d2) = lines[k - 1], lines[k]
        A = np.array([d1, -d2]).T
        if abs(np.linalg.det(A)) < 1e-9:
            return quad
        s = np.linalg.solve(A, p2 - p1)
        out.append(p1 + d1 * s[0])
    out = np.array(out)
    if np.max(np.abs(out - quad)) > 0.03 * max(h, w):  # a refinement that jumped somewhere else
        return quad
    return out


PHONE_F35 = 26.0  # mm: 35-mm-equivalent focal length of a phone's main camera (W6 19:42)


def _f35(src) -> float | None:
    """EXIF FocalLengthIn35mmFilm of a photo, or None."""
    try:
        from PIL import Image

        with Image.open(src) as im:
            v = im.getexif().get_ifd(0x8769).get(0xA405)
        return float(v) if v and 8 <= float(v) <= 300 else None
    except Exception:  # noqa: BLE001
        return None


def _aspect(quad: np.ndarray, w: int, h: int, f35: float | None = None) -> float:
    """Recovered width/height of the rectangle the quad images (Zhang & He); TL, TR, BR, BL order. When the corners do
    not give the focal length (W6 19:42: a phone tilted about ONE axis leaves one vanishing point at infinity, the
    method's degenerate case; the side ratio was foreshortened, DfE A2 at 20 deg read 1.483 -> ARCH paper), the photo's
    EXIF 35-mm focal length or a phone's 26 mm is used in the same formula (STOA_O1_PHOTO_F=0: side ratio as before)."""
    u0, v0 = w / 2.0, h / 2.0
    tl, tr, br, bl = quad
    # their m1..m4 = (0,0), (W,0), (0,H), (W,H) of the rectangle
    m1, m2, m3, m4 = [np.array([p[0], p[1], 1.0]) for p in (tl, tr, bl, br)]
    k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
    k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    n2 = k2 * m2 - m1
    n3 = k3 * m3 - m1
    mean_w = (np.hypot(*(tr - tl)) + np.hypot(*(br - bl))) / 2
    mean_h = (np.hypot(*(bl - tl)) + np.hypot(*(br - tr))) / 2
    fallback = mean_w / mean_h
    den = n2[2] * n3[2]
    diag = math.hypot(w, h)
    f = None
    if not (abs(den) < 1e-9 or abs(k2 - 1) < 1e-4 or abs(k3 - 1) < 1e-4):
        f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + n2[2] * n3[2] * u0 * u0)
               + (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + n2[2] * n3[2] * v0 * v0)) / den
        if f2 > 0 and F_MIN * diag <= f2 ** 0.5 <= F_MAX * diag:
            f = f2 ** 0.5
    if f is None:
        if os.environ.get("STOA_O1_PHOTO_F", "1") == "0" or (abs(k2 - 1) < 1e-4 and abs(k3 - 1) < 1e-4):
            return fallback  # fronto-parallel: no perspective to undo
        f = (f35 or PHONE_F35) / 43.27 * diag  # the corners cannot say: an assumed (or EXIF) phone focal length
    kinv = np.array([[1 / f, 0, -u0 / f], [0, 1 / f, -v0 / f], [0, 0, 1]])  # K^-1 of K = [[f,0,u0],[0,f,v0],[0,0,1]]
    a, b = kinv @ n2, kinv @ n3
    r = float(np.sqrt(np.dot(a, a) / np.dot(b, b)))
    if not math.isfinite(r) or not (0.8 < r / fallback < 1.25):
        return fallback
    return r


def _snap(ratio_long: float) -> tuple[str, float, bool]:
    iso = PAPERS["ISO A"][1] / PAPERS["ISO A"][0]
    if abs(iso - ratio_long) <= ASPECT_TOL * iso:  # the usual sheet first
        return "ISO A", iso, True
    best = min(PAPERS.items(), key=lambda kv: abs(kv[1][1] / kv[1][0] - ratio_long))
    name, (s, l) = best
    if abs(l / s - ratio_long) <= ASPECT_TOL * (l / s):
        return name, l / s, True
    return "", ratio_long, False


def _flatten(img: np.ndarray) -> np.ndarray:
    """Divide out the light: background = a large closing (removes lines) of a small copy, per channel."""
    h, w = img.shape[:2]
    s = 800 / max(h, w)
    small = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    k = max(9, round(max(small.shape[:2]) / 40)) | 1
    bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    out = img.astype(np.float32) / np.maximum(bg, 1.0) * 255.0
    # paper -> pure white, ink keeps its contrast (a faint grey stays grey)
    out = np.clip((out - 0) * (255.0 / 235.0), 0, 255)
    out[out > 247] = 255
    return out.astype(np.uint8)


def detect(img: np.ndarray) -> tuple[np.ndarray, float] | None:
    """(TL, TR, BR, BL corners in img px, Otsu threshold in full-res grey levels) or None (not a photo of a sheet)."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    s = 1000 / max(h, w)
    small = cv2.resize(gray, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    got = _paper_mask(small)
    if got is None:
        return None
    mask, thr = got
    share = float((mask > 0).mean())
    if not (MIN_AREA <= share <= MAX_AREA):
        return None
    q = _quad(mask)
    if q is None:
        return None
    sh, sw = small.shape
    marg = EDGE_MARGIN * max(sh, sw)
    if (q[:, 0].min() < marg or q[:, 1].min() < marg or q[:, 0].max() > sw - 1 - marg
            or q[:, 1].max() > sh - 1 - marg):
        return None  # a corner at the frame: the sheet is cut off (or this is a clean export / scan)
    poly = np.zeros_like(mask)
    cv2.fillConvexPoly(poly, q.astype(np.int32), 255)
    if abs(cv2.contourArea(q.astype(np.float32)) / max(1.0, float((mask > 0).sum())) - 1) > 0.06:
        return None  # the bright blob is not a quadrilateral
    ring = cv2.dilate(poly, np.ones((9, 9), np.uint8)) == 0
    inside = cv2.erode(poly, np.ones((9, 9), np.uint8)) > 0
    if ring.sum() < 0.03 * ring.size:
        return None
    paper_med, surround_med = float(np.median(small[inside])), float(np.median(small[ring]))
    if paper_med - surround_med < MIN_CONTRAST:
        return None
    q_full = q / s
    q_full = _refine(gray, q_full, thr)
    return _order(q_full), thr


def deskew(src: str | Path, out_dir: str | Path | None = None, long_px: int | None = None,
           long_max: int = 0) -> Deskewed | None:
    """The deskewed copy of a phone photo of a sheet (cached next to it / in out_dir), or None."""
    if not enabled():
        return None
    src = Path(src)
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) < 800:
        return None
    got = detect(img)
    if got is None:
        return None
    quad, _thr = got
    tl, tr, br, bl = quad
    r = _aspect(quad, w, h, _f35(src))  # width / height
    landscape = r >= 1
    ratio_long = r if landscape else 1 / r
    name, ratio, snapped = _snap(ratio_long)
    side_w = (np.hypot(*(tr - tl)) + np.hypot(*(br - bl))) / 2
    side_h = (np.hypot(*(bl - tl)) + np.hypot(*(br - tr))) / 2
    L = int(long_px or round(max(side_w, side_h)))
    if long_max:
        L = min(L, int(long_max))
    S = round(L / ratio)
    W, H = (L, S) if landscape else (S, L)
    dst = np.float64([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]])
    M = cv2.getPerspectiveTransform(np.float32(quad), np.float32(dst))
    warped = cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    flat = _flatten(warped) if FLATTEN else warped
    fam = name or "ISO A"
    short_mm, long_mm = PAPERS[fam]
    dpi = L / (long_mm / 25.4)
    out_dir = Path(out_dir) if out_dir else src.parent
    out = out_dir / f"{src.stem}.photo{'' if FLATTEN else '-nf'}.png"
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text(PHOTO_KEY, fam if snapped else "")
    tmp = out.with_name(out.name + f".tmp{os.getpid()}.png")
    # NO DPI tag: a tag would make every reader take the A3 prior as the sheet (upload._as_vector traced a 1971 type
    # sheet photo with no paper size as A3 and plan_rooms miscounted, X1 x1-063); named_paper() gives the true one
    info.add_text(PHOTO_DPI_KEY, f"{dpi:.3f}")
    Image.fromarray(cv2.cvtColor(flat, cv2.COLOR_BGR2RGB)).save(tmp, pnginfo=info)
    os.replace(tmp, out)
    Minv = np.linalg.inv(M)
    notes = [f"phone photo of a printed sheet: its corners were found and the perspective undone "
             f"({'an ' + fam + ' sheet' if snapped else 'side ratio ' + format(ratio_long, '.3f') + ', no standard sheet'}"
             f"), the light evened out, before reading"]
    if snapped:
        notes.append(f"pixel scale from the {fam} paper size the sheet names (A3 assumed if it names none); "
                     "printed dimension strings or a scale bar take precedence")
    return Deskewed(path=str(out), to_photo=Minv.tolist(), paper=fam, aspect=float(ratio_long), snapped=snapped,
                    dpi=float(dpi), size=(W, H), corners=quad.tolist(), notes=notes)


def cached_deskew(src: str | Path, out_dir: str | Path, long_max: int = 6000) -> Deskewed | None:
    """deskew() with a sidecar '<stem>.photo.json' (also for 'not a photo'), so every question after the first on an
    upload costs a stat + a JSON read."""
    import json

    if not enabled():  # the kill switch never writes the 'not a photo' cache (it would outlive the switch)
        return None
    src = Path(src)
    side = Path(out_dir) / f"{src.stem}.photo{'' if FLATTEN else '-nf'}.json"
    key = {"v": VERSION, "flat": FLATTEN, "mtime": src.stat().st_mtime, "size": src.stat().st_size}
    try:
        got = json.loads(side.read_text())
        if got.get("key") == key:
            d = got.get("d")
            if d is None:
                return None
            if Path(d["path"]).exists():
                return Deskewed(**{**d, "size": tuple(d["size"])})
    except Exception:  # noqa: BLE001 - no / stale / broken sidecar: compute
        pass
    img_long = None
    try:
        from PIL import Image

        with Image.open(src) as im:
            img_long = max(im.size)
    except Exception:  # noqa: BLE001
        pass
    d = deskew(src, out_dir=out_dir, long_max=long_max)
    tmp = side.with_name(side.name + f".tmp{os.getpid()}")
    from dataclasses import asdict

    tmp.write_text(json.dumps({"key": key, "img_long": img_long, "d": asdict(d) if d else None}))
    os.replace(tmp, side)
    return d


def photo_family(img_path: str) -> str | None:
    """'ISO A' (the paper family) for a deskewed photo written by deskew(), '' for one with no standard aspect, None
    for any other image."""
    from PIL import Image

    try:
        with Image.open(img_path) as im:
            v = im.info.get(PHOTO_KEY)
    except Exception:  # noqa: BLE001
        return None
    return None if v is None else str(v)


def named_paper(img_path: str, texts: list[str]) -> tuple[float, str] | None:
    """(dpi, sheet) of a deskewed photo when its text names exactly one standard sheet of the image's aspect ('1:200 @
    A1', 'A1', 'ANSI D'), else None. For readers that need a paper size (P1's upload._as_vector / vectorize: use this
    when V.paper() is None and photo_family() is not None)."""
    from PIL import Image

    if photo_family(img_path) is None:
        return None
    try:
        from stoa.formats.paper import PAPER_MM as PM, _named
        from stoa.solvers.raster.scalebar import _AT_WORD

        with Image.open(img_path) as im:
            a, b = sorted(im.size)
    except Exception:  # noqa: BLE001
        return None
    texts = [_AT_WORD.sub(r"@ \1", t or "") for t in texts]
    hits = [n for n, (sw, sl) in PM.items() if abs((b / a) / (sl / sw) - 1) <= 0.02 and _named(n, texts)]
    if len(hits) != 1:
        return None
    return b / (PM[hits[0]][1] / 25.4), hits[0]


def a3_prior(img_path: str) -> tuple[float, str] | None:
    """(dpi, 'A3'-like sheet) IF the photographed print were its family's A3-size sheet, for a deskewed photo."""
    from PIL import Image

    try:
        with Image.open(img_path) as im:
            v = im.info.get(PHOTO_DPI_KEY)
            fam = im.info.get(PHOTO_KEY)
    except Exception:  # noqa: BLE001
        return None
    if not v or not fam:
        return None
    return float(v), {"ISO A": "A3", "ANSI B/D": "Tabloid", "ANSI A/C/E": "Letter", "ARCH B/D": "ARCH B",
                      "ARCH A/C/E": "ARCH A", "Legal": "Legal"}.get(str(fam), "A3")


def prepare_hook(prep, src: str | Path, cache_dir: str | Path) -> bool:
    """For stoa.formats.normalize.prepare (P1): a phone photo of a sheet is solved as its deskewed copy. Sets
    prep.path / prep.kind = 'photo' / prep.photo_h (3x3 deskewed px -> photo px) and adds the notes."""
    try:
        d = cached_deskew(src, cache_dir)
    except Exception:  # noqa: BLE001 - never fail the upload on this
        return False
    if d is None:
        return False
    prep.path, prep.kind = d.path, "photo"
    prep.photo_h = d.to_photo
    prep.notes.extend(d.notes)
    return True


def restore_ev(ev, prep, orig: str) -> bool:
    """For stoa.formats.normalize.restore: evidence on the deskewed copy (image_px) -> the uploaded photo's px."""
    H = getattr(prep, "photo_h", None)
    if not H or getattr(ev, "bbox_space", None) != "image_px":
        return False
    ev.file = orig
    if ev.bbox:
        ev.bbox = map_box(H, ev.bbox)
    for attr in ("polygon", "polyline"):
        pts = getattr(ev, attr, None)
        if pts:
            setattr(ev, attr, [[round(x, 2), round(y, 2)] for x, y in map_points(H, pts)])
    return True


def map_points(H, pts):
    """Apply a 3x3 homography to [[x, y], ...]."""
    H = np.asarray(H, dtype=np.float64)
    P = np.c_[np.asarray(pts, dtype=np.float64).reshape(-1, 2), np.ones(len(pts))]
    Q = P @ H.T
    return (Q[:, :2] / Q[:, 2:3]).tolist()


def map_box(H, box):
    """Axis-aligned bounding box of a mapped [x0, y0, x1, y1]."""
    x0, y0, x1, y1 = box
    q = np.array(map_points(H, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]))
    return [float(q[:, 0].min()), float(q[:, 1].min()), float(q[:, 0].max()), float(q[:, 1].max())]


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        d = deskew(p, out_dir=os.environ.get("OUT"))
        print(p, "->", None if d is None else (d.path, d.paper, round(d.aspect, 4), d.snapped, round(d.dpi, 1),
                                                d.size))
