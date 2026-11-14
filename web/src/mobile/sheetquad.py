"""The paper in a phone photo of a printed sheet, for measuring in its flat plane (W6; GET /api/m/sheetquad).

    python sheetquad.py IMAGE  ->  one JSON line: {ok, quad, aspect, how, H, w, h}

quad = the sheet's corners in the photo (tl, tr, br, bl; O1's stoa.solvers.raster.rectify.sheet_quad), H = the 3x3
homography photo px -> flat px (row-major), w x h = the flat sheet in px, aspect = w / h. The aspect is the paper's
TRUE one, recovered from the perspective (Zhang & He, 'Whiteboard scanning and image enhancement', 2007: the camera's
focal length from the 4 corners, principal point at the image centre, square pixels); when the photo has almost no
perspective the focal length is undefined and the side lengths give it directly. Why: tapped rooms are measured on the
photo at ONE scale (a scale bar, door swings), and a tilted photo shrinks the far side: Stoa's DfE sheet as a photo, a
room at the top 16 % small on a photo that looks flat, 42 % small with the top edge 30 % narrower (MOBILE.md).
ok false when there is no clear sheet (a scan or a render fills the frame; a white sheet on a white table).
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def true_aspect(q: np.ndarray, W: int, H: int, f35: float | None = None) -> tuple[float, str]:
    """w/h of the real rectangle whose photo is the quad q (tl, tr, br, bl) in an image W x H. f35 = the photo's
    35 mm-equivalent focal length (EXIF), used when the corners cannot give the focal length: a phone tilted about ONE
    axis (the usual booth shot) leaves one vanishing point at infinity, the degenerate case of the method."""
    tl, tr, br, bl = q
    side = float((np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)))
    u0, v0 = W / 2.0, H / 2.0
    m1, m2, m3, m4 = (np.array([p[0] - u0, p[1] - v0, 1.0]) for p in (tl, tr, bl, br))
    k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
    k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    # how far each vanishing point is: z (dimensionless) x the image size vs the in-image part (px)
    L = float(max(W, H))
    r2, r3 = abs(n2[2]) * L / max(np.hypot(n2[0], n2[1]), 1e-9), abs(n3[2]) * L / max(np.hypot(n3[0], n3[1]), 1e-9)
    if r2 < 1e-3 and r3 < 1e-3:
        return side, "sides (no perspective)"
    f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2]) if min(r2, r3) > 0.02 else -1.0
    how = f"perspective (focal length {np.sqrt(max(f2, 0.0)):.0f} px from the corners)"
    if not (0.25 * max(W, H) ** 2 <= f2 <= 9.0 * max(W, H) ** 2):
        # a phone's main camera: 26 mm equivalent unless the photo says otherwise (35 mm diagonal 43.27 mm)
        f = (f35 or 26.0) / 43.27 * float(np.hypot(W, H))
        f2 = f * f
        how = f"perspective (focal length {f:.0f} px {'from the photo' if f35 else 'assumed: a phone main camera, 26 mm'})"
    a2 = (n2[0] ** 2 + n2[1] ** 2 + n2[2] ** 2 * f2) / (n3[0] ** 2 + n3[1] ** 2 + n3[2] ** 2 * f2)
    a = float(np.sqrt(a2))
    if not 0.5 * side <= a <= 2.0 * side:  # nonsense from a bad quad: the sides are safer
        return side, "sides (perspective estimate implausible)"
    return a, how


def main(path: str) -> dict:
    import cv2
    from PIL import Image, ImageOps

    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from stoa.solvers.raster.rectify import sheet_quad

    with Image.open(path) as im:
        f35 = None
        try:
            f35 = float(im.getexif().get_ifd(0x8769).get(0xA405) or 0) or None  # FocalLengthIn35mmFilm
        except Exception:  # noqa: BLE001
            pass
        g = np.asarray(ImageOps.exif_transpose(im).convert("L"))
    Hh, Ww = g.shape
    q = sheet_quad(g)
    if q is None:
        return {"ok": False, "why": "no sheet edge found (a scan or a render fills the frame, or the table is as light as the paper)"}
    # corners on the frame's edge: the sheet runs out of the photo there and the corner found is not the paper's.
    # Along ONE edge (a tilted shot whose near side overflows: DfE at 20°, 49 px out) the flat plane is still far
    # better than the photo (+9 % vs +56 %); on more sides (a close-up) the quad is a crop, not the sheet.
    m = 0.005 * max(Ww, Hh)
    edge = [i for i, (x, y) in enumerate(q) if x < m or y < m or x > Ww - 1 - m or y > Hh - 1 - m]
    if len(edge) >= 3 or (len(edge) == 2 and (edge[1] - edge[0]) % 4 == 2):
        return {"ok": False, "why": f"the sheet runs out of the photo ({len(edge)} corners on the frame's edge)"}
    aspect, how = true_aspect(q, Ww, Hh, f35)
    tl, tr, br, bl = q
    w = float(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    h = w / aspect
    Hm = cv2.getPerspectiveTransform(q.astype(np.float32), np.float32([[0, 0], [w, 0], [w, h], [0, h]]))
    return {"ok": True, "quad": [[round(float(x), 1), round(float(y), 1)] for x, y in q], "aspect": round(aspect, 4), "how": how,
            "w": round(w, 1), "h": round(h, 1), "H": [round(float(v), 10) for v in Hm.ravel()], "corners_on_edge": len(edge)}


if __name__ == "__main__":
    try:
        out = main(sys.argv[1])
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(out))
