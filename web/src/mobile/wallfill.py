"""The room around a tap on a PICTURE whose floors are hatched (parquet boards, tiles), filled on its SOLID walls only.

    python wallfill.py IMG X Y  ->  {"ok": true, "poly": [[x, y], ...] (IMG px), "area": px², "closing_px", "how"}
                                    {"ok": false, "why": "..."}

Why (UX NEW-R1, 01:05): W5's room_shape.py flood fill treats every stroke as a wall, so on a plan with floor hatching
(UX's Nordic apartment artifacts/UX/upl/ux-test-flat-plan.png, 'EI MITTAKAAVASSA') a tap in the living room outlines
ONE FLOORBOARD: a strip ~16 px tall between two board lines (6 taps: 5 strips, 2A/P 14-20 px; only the unhatched WC
right). Realtor plans, CubiCasa-style images and phone photos of them have exactly this.
How: the walls are the SOLID ink: pixels darker than 215 (black walls, grey window / balcony fills; board lines are
1-3 px of 100-230 grey), opened with a k x k square (k ~ 0.25 % of the long side, >= 3 px) so every stroke thinner
than that goes. Then room_shape.outline() (W5's: walls thickened step by step until the region no longer leaks
through a doorway, grown back, fixtures absorbed) runs on that mask, called, not copied. On a thin-line CAD plan the
solid mask has no walls, every level leaks and the answer is ok: false (the page keeps the normal fill), so the page
asks for this only when the normal outline is a strip (thinner than ~0.45 m, or than 1 % of the picture).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOM_SHAPE = os.path.join(os.path.dirname(os.path.dirname(HERE)), "server", "room_shape.py")
DARK = 215  # solid fills: walls (~0-30), window / balcony fills (~180-195); paper and its shading (240+) are not


def _room_shape():
    spec = importlib.util.spec_from_file_location("w5_room_shape", ROOM_SHAPE)
    if spec is None or spec.loader is None:
        raise ImportError(ROOM_SHAPE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def solid_walls(gray):
    """255 where the ink is solid (thicker than ~0.25 % of the long side), 0 elsewhere: hatching and thin strokes go."""
    import cv2

    k = max(3, int(round(0.0025 * max(gray.shape))))
    dark = (gray < DARK).astype(np.uint8) * 255
    return cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))


def trace(img: str, x: float, y: float) -> dict:
    rs = _room_shape()
    gray, f, _ = rs.load(img, 0)
    walls = solid_walls(gray)
    h, w = walls.shape
    share = walls.mean() / 255
    if share < 0.01:
        return {"ok": False, "why": "no solid walls (a thin-line drawing)"}
    if share > 0.25:  # a photo's table / shadows read as solid (DfE / AFCEC fake photos 0.32 / 0.39; plans 0.08-0.10)
        return {"ok": False, "why": "too much dark area to tell walls from shadows"}
    ys, xs = np.nonzero(walls)
    footprint = float((xs.max() - xs.min()) * (ys.max() - ys.min()))
    seed = (int(min(max(x * f, 0), w - 1)), int(min(max(y * f, 0), h - 1)))
    r = rs.outline(walls, seed, (seed[0] - 1, seed[1] - 1, seed[0] + 1, seed[1] + 1), footprint)
    if r is None:
        return {"ok": False, "why": "no closed room on the solid walls"}
    poly, area_px, rr = r[0], r[1], r[2]
    return {"ok": True, "poly": [[round(float(px) / f, 1), round(float(py) / f, 1)] for px, py in poly],
            "area": round(area_px / (f * f), 1), "closing_px": rr, "how": "solid walls (floor pattern ignored)"}


if __name__ == "__main__":
    try:
        out = trace(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
