"""Is this phone photo one of the sheets we have on file? SIFT features + a RANSAC homography, photo -> sheet.

    python photomatch.py PHOTO SHEET [SHEET ...] [--alike SHEET ...]
        ->  {"ok": true, "i": <index of the sheet>, "H": [9 numbers, row-major: photo px -> the sheet's evidence units
             (image px, or unrotated pdf_pt of page 0)], "inliers", "second", "cover", "side", "close": true (a close-up)}
            {"ok": false, "why": "..."}

Why (booth, W6 01:45): at our table a visitor photographs a PRINTED sheet of one of the 'Try a case' drawings. The
sheet on file has a finished takeoff (rooms traced along their walls, names, m²: GET /api/glance), the photo has none
(flood fill at a door-swing scale: the DfE kitchen 38.5 m² vs 36.36). With the photo matched to the sheet, a tap on the
photo is mapped onto the sheet, takes the takeoff's room there, and its outline comes back onto the photo through the
same homography (a tilted photo too): the desktop's number, named, in about a second. Said so on the card ('matched to
the drawing on file'); a photo of any other drawing finds no match and is measured on the photo as before.
How: grey, long side 1,600 px (photo) / 2,000 px (sheet; a PDF page rendered unrotated so units are pdf_pt as in the
contract), SIFT 6,000 features, Lowe ratio 0.75, RANSAC 4 px (at the photo's 1,600 px). A match needs >= 60 inliers,
>= 40 % of the ratio-test matches, the sheet's ink found again on the warped photo (>= 65 % of it, 3 px tolerance: true
matches 0.92-0.995 on fake / 20 deg tilted photos of DfE L00 / L01 / AFCEC; ANOTHER sheet of the same building, USACE
A-101F floor plan or AW_078-24-33 p76 vs the S-100F slab plan on file, shares its grid bubbles and title block: 172-175
inliers, 0.24-0.37 of the ink: refused), a homography that keeps orientation (det > 0) and maps the photo onto >= 15 % of the
sheet, and a clear lead: the runner-up sheet below 60 % of the best's inliers (Maleva floors 2-4 are the same plan:
no pick between them). Sheet features are cached on disk per file (mtime) in $TMPDIR/w6-photomatch.
CLOSE-UPS (W6 02:30: a visitor photographs one room of the print): a photo showing 4-15 % of a sheet is matched only when
'--alike' sheets are given (other sheets of the same sets) and it beats them by the same lead: a title block alone is the
same on every sheet of a set (DfE primary-school title block close-up: 428 inliers on the special school's Level 00, ink
0.998, but 431 on its own sheet -> refused; S-100F title block 139 vs A-101F 98 -> refused). When the 1,600 px look finds no
sheet, a second look at 1,000 px (a close-up's fine print has no counterpart in the sheet's 2,000 px render). Fake close-ups
of DfE Level 00 (hall + kitchen 5 %, south wing 6 %, west wing 9 %, a third 14 %) and S-100F (9 %): all matched, 61-310
inliers, ink 0.96-0.998; whole-sheet photos unchanged. The phone uses a close-up match for taps / long-presses only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

import numpy as np

CACHE = os.path.join(tempfile.gettempdir(), "w6-photomatch")
PHOTO_SIDE, PHOTO_SIDE_2, SHEET_SIDE, NFEAT = 1600, 1000, 2000, 6000
MIN_INLIERS, MIN_SHARE, MIN_COVER, LEAD, MIN_INK = 60, 0.4, 0.15, 0.6, 0.65
MIN_COVER_CLOSE = 0.04  # a close-up of one room (>= 4 % of the sheet) when look-alike sheets of the same sets are given


def _gray(path: str, side: int):
    """grey image at <= side px on its long side, and the factor from these px to the file's evidence units."""
    import cv2

    if path.lower().endswith(".pdf"):
        import pymupdf

        with pymupdf.open(path) as doc:
            pg = doc[0]
            if pg.rotation:
                pg.set_rotation(0)  # pdf_pt as the contract has them (unrotated)
            r = pg.rect
            z = side / max(r.width, r.height)
            pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
            img = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width).copy()
            return img, 1.0 / z, (r.width, r.height)
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"unreadable: {path}")
    h, w = img.shape
    f = min(1.0, side / max(h, w))
    if f < 1.0:
        img = cv2.resize(img, (round(w * f), round(h * f)), interpolation=cv2.INTER_AREA)
    return img, 1.0 / f, (float(w), float(h))


def _features(img):
    import cv2

    sift = cv2.SIFT_create(nfeatures=NFEAT)
    kp, des = sift.detectAndCompute(img, None)
    pts = np.float32([k.pt for k in kp]) if kp else np.zeros((0, 2), np.float32)
    return pts, des


def sheet_features(path: str):
    """(points in the sheet's evidence units, descriptors, (width, height) in those units), cached per file + mtime."""
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{SHEET_SIDE}|{NFEAT}".encode()).hexdigest()[:20]
    f = os.path.join(CACHE, key + ".npz")
    if os.path.exists(f):
        z = np.load(f)
        return z["pts"], z["des"], tuple(z["size"])
    img, k, size = _gray(path, SHEET_SIDE)
    pts, des = _features(img)
    pts = pts * k
    os.makedirs(CACHE, exist_ok=True)
    tmp = f + f".{os.getpid()}.npz"
    np.savez(tmp, pts=pts, des=des if des is not None else np.zeros((0, 128), np.float32), size=np.float32(size))
    os.replace(tmp, f)
    return pts, des, size


def ink_overlap(photo_img, H_img_to_units, sheet: str) -> float:
    """Share of the sheet's dark ink (inside the part the photo shows) that has photo ink within 3 px once the photo is
    warped onto the sheet: the same drawing lines up line for line; another sheet of the same building only at its grid."""
    import cv2

    simg, ks, _ = _gray(sheet, SHEET_SIDE)
    to_sheet_px = np.diag([1.0 / ks, 1.0 / ks, 1.0]) @ H_img_to_units  # photo 1,600 px -> sheet render px
    h, w = simg.shape
    def ink(g):
        return (cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15) > 0)
    warped = cv2.warpPerspective(photo_img, to_sheet_px, (w, h), flags=cv2.INTER_AREA, borderValue=255)
    seen = cv2.warpPerspective(np.full(photo_img.shape, 255, np.uint8), to_sheet_px, (w, h), flags=cv2.INTER_NEAREST) > 0
    seen = cv2.erode(seen.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    s_ink = ink(simg) & seen
    p_ink = cv2.dilate(ink(warped).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    n = int(s_ink.sum())
    return float((s_ink & p_ink).sum() / n) if n > 500 else 0.0


def _rank(ppts, pdes, sheets: list[str]) -> list[dict]:
    """every sheet's RANSAC inliers against the photo's features, best first"""
    import cv2

    bf = cv2.BFMatcher(cv2.NORM_L2)
    found = []
    for i, s in enumerate(sheets):
        try:
            spts, sdes, size = sheet_features(s)
        except Exception as e:  # noqa: BLE001
            found.append({"i": i, "inliers": 0, "why": str(e)[:80]})
            continue
        if sdes is None or len(spts) < MIN_INLIERS:
            found.append({"i": i, "inliers": 0})
            continue
        good = [m for m, n in (p for p in bf.knnMatch(pdes, sdes, k=2) if len(p) == 2) if m.distance < 0.75 * n.distance]
        if len(good) < MIN_INLIERS:
            found.append({"i": i, "inliers": len(good) // 4})
            continue
        src = ppts[[m.queryIdx for m in good]]  # photo px at the working size
        dst = spts[[m.trainIdx for m in good]]  # sheet units
        # RANSAC in the photo's working frame: sheet units -> photo px is the inverse we estimate (4 px tolerance there)
        Hi, mask = cv2.findHomography(dst, src, cv2.RANSAC, 4.0)
        n = int(mask.sum()) if mask is not None else 0
        found.append({"i": i, "inliers": n, "share": n / len(good), "Hi": Hi, "size": size})
    found.sort(key=lambda d: -d["inliers"])
    return found


def _match_at(photo: str, side: int, sheets: list[str], alike: list[str]) -> dict:
    import cv2

    img, k, _ = _gray(photo, side)
    ppts, pdes = _features(img)
    if pdes is None or len(ppts) < MIN_INLIERS:
        return {"ok": False, "why": "too few features in the photo"}
    found = _rank(ppts, pdes, sheets)
    best = found[0] if found else None
    second = found[1]["inliers"] if len(found) > 1 else 0
    if not best or best["inliers"] < MIN_INLIERS:
        return {"ok": False, "why": "no sheet on file matches", "best": best["inliers"] if best else 0}
    if best["share"] < MIN_SHARE:
        return {"ok": False, "why": "matches too scattered", "best": best["inliers"]}
    if second >= LEAD * best["inliers"]:
        return {"ok": False, "why": "two sheets match alike (repeated floors)", "best": best["inliers"], "second": second}
    Hi = best["Hi"]
    try:
        H = np.linalg.inv(Hi)  # photo px (working frame) -> sheet units
    except np.linalg.LinAlgError:
        return {"ok": False, "why": "degenerate homography"}
    H = H @ np.diag([1.0 / k, 1.0 / k, 1.0])  # photo file px -> working frame -> sheet units
    h, w = img.shape
    corners = np.float32([[0, 0], [w * k, 0], [w * k, h * k], [0, h * k]]).reshape(-1, 1, 2)
    on = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
    sw, sh = best["size"]
    x0, y0 = np.clip(on.min(0), 0, [sw, sh])
    x1, y1 = np.clip(on.max(0), 0, [sw, sh])
    cover = float((x1 - x0) * (y1 - y0) / max(1e-9, sw * sh))
    close = cover < MIN_COVER and bool(alike)  # a close-up: allowed only when it can be told from its look-alikes
    if np.linalg.det(H[:2, :2]) <= 0 or cover < (MIN_COVER_CLOSE if close else MIN_COVER):
        return {"ok": False, "why": "the matched part is too small or mirrored", "best": best["inliers"], "cover": round(cover, 3)}
    ink = ink_overlap(img, H @ np.diag([k, k, 1.0]), sheets[best["i"]])
    if ink < MIN_INK:
        return {"ok": False, "why": "the photo's lines do not lie on the sheet's", "best": best["inliers"], "ink": round(ink, 3)}
    if close:
        rival = _rank(ppts, pdes, alike)
        if rival and rival[0]["inliers"] >= LEAD * best["inliers"]:
            return {"ok": False, "why": "this part looks the same on another sheet of the set (title block?)", "best": best["inliers"],
                    "alike": rival[0]["inliers"], "cover": round(cover, 3)}
    return {"ok": True, "ink": round(ink, 3), "i": best["i"], "H": [float(v) for v in (H / H[2, 2]).ravel()], "inliers": best["inliers"], "second": second,
            "share": round(best["share"], 3), "cover": round(cover, 3), "side": side, **({"close": True} if close else {})}


def match(photo: str, sheets: list[str], alike: list[str] | None = None) -> dict:
    """the 1,600 px look first; when it finds no sheet (or only scattered matches), a second look at 1,000 px: a close-up's
    fine print has no counterpart in the sheet's 2,000 px render, its coarser shapes do (DfE left wing close-up: 'scattered'
    at 1,600 px, 189 inliers / ink 0.99 at 1,000)."""
    r = _match_at(photo, PHOTO_SIDE, sheets, alike or [])
    if r["ok"] or r["why"] not in ("no sheet on file matches", "matches too scattered"):
        return r
    r2 = _match_at(photo, PHOTO_SIDE_2, sheets, alike or [])
    return r2 if r2["ok"] else r


if __name__ == "__main__":
    try:
        args = sys.argv[2:]
        cut = args.index("--alike") if "--alike" in args else len(args)
        out = match(sys.argv[1], args[:cut], [a for a in args[cut + 1:] if os.path.exists(a)])
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
