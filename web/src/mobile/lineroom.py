"""The room around a tap on a PICTURE, traced along its wall lines (W6 phone page, GET /api/m/lineroom).

    $JUNCTION_VENV/bin/python web/src/mobile/lineroom.py /tmp/stoa-uploads/ab12/plan.png 3000 2860 50.59
    -> {"ok": true, "poly": [[x, y], ...], "area_m2": 246.97, "method": "wall lines", "variant": "dark|doors@199.692",
        "support": 7, "seconds": 0.8}

Tap-the-room on a picture used W5's flood fill only (web/server/room_shape.py: wall lines are for vector PDFs). On a
picture the upload's traced copy '<stem>.vec6.pdf' (P1's stoa.formats.vectorize: the picture's lines as strokes + its OCR
text, one page at the picture's aspect) IS a line drawing, so E1's stoa.evidence.linework.measure_room runs on it as on a
CAD sheet: door swings closed at the doorway, words of other rooms kept out. Stoa's DfE Level 00 PNG at its printed 1:200
(50.59 px/m), tapped in the room:
    Multi-Purpose Hall 246.97 m² (printed 247.73; flood fill 271), Laundry 6.95 (~7.0; flood fill 5.42),
    Store 3.02 (flood fill 2.43); 0.8 s the first time on the sheet, ~0.1 s after.
Needs the scale (px per m of the picture), which sets scale_n for the door-swing radii; the page measures the returned
outline itself (its own scale, and the flat plane of a tilted photo). No traced copy (yet), another aspect (a phone
photo's traced copy is of its deskewed copy) or no closed room -> {"ok": false}: the page keeps the flood fill.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

ASPECT_TOL = 0.01  # the traced page must have the picture's aspect (same pixels, only scaled)
PT_PER_M_AT_1 = 1000 / 25.4 * 72  # 2834.6 pt of paper per metre at 1:1


def trace(img: str, x: float, y: float, px_per_m: float) -> dict:
    t0 = time.time()
    from PIL import Image

    with Image.open(img) as im:
        w, h = im.size
    p = Path(img)
    vec = p.with_name(p.stem + ".vec6.pdf")
    if not vec.exists():
        return {"ok": False, "why": "no traced copy yet", "retry": True}
    import pymupdf

    with pymupdf.open(vec) as doc:
        pg = doc[0]
        pw, ph, rot = pg.rect.width, pg.rect.height, pg.rotation
    if not (pw > 0 and ph > 0) or rot != 0 or abs((w / h) / (pw / ph) - 1) > ASPECT_TOL:
        return {"ok": False, "why": "the traced copy is of other pixels"}
    if not (px_per_m > 0):
        return {"ok": False, "why": "no scale"}
    k = w / pw  # picture px per traced-copy pt
    scale_n = PT_PER_M_AT_1 / (px_per_m / k)
    from stoa.evidence import linework

    r = linework.measure_room(str(vec), 0, (x / k, y / k), scale_n)
    if not r or not r.get("polygon") or len(r["polygon"]) < 3:
        return {"ok": False, "why": "no closed room along the wall lines", "seconds": round(time.time() - t0, 3)}
    return {"ok": True, "poly": [[round(a * k, 1), round(b * k, 1)] for a, b in r["polygon"]], "area_m2": r.get("area_m2"),
            "method": "wall lines", "variant": r.get("variant"), "support": r.get("support"),
            "seconds": round(time.time() - t0, 3)}


if __name__ == "__main__":
    try:
        out = trace(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]))
    except Exception as e:  # noqa: BLE001 - the page keeps the flood fill
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(out))
