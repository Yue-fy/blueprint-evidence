"""The printed graphic scale bar of a PHOTO or raster plan (W6 phone page, GET /api/m/barscale).

    $JUNCTION_VENV/bin/python web/src/mobile/barscale.py /tmp/stoa-uploads/ab12/photo.jpg
    -> {"ok": true, "px_per_m": 31.5, "unit": "m", "values": [0, 10, 20], "pts": [[2218, 2657], [2848, 2657]],
        "m": 20.0, "text": "graphic scale bar '0 10 20 m' ...", "seconds": 23.4}

O1's stoa.solvers.raster.scalebar.bars (number labels of the bar fitted against their positions, snapped to the drawn
bar) on the photo itself, so its pixels are the ones the visitor taps. `pts` are the bar's two ends in photo px and
`m` the real length between them: the page treats them like the visitor's own two scale taps, so a tilted photo maps
them into the paper's flat plane exactly (W6 flatUnits).

The scale bar is PRINTED on the sheet: it is a better scale than the door-swing guess (doorscale.py), which the page
keeps only for photos without a readable bar. Stoa's DfE school sheet as a phone photo: bar 31.50 px/m (hand-measured
31.4), door swings 29.8; the kitchen tapped at the bar's scale vs at the door swings' differs by ~11 % in area.

Cost: the photo's own text layer (local PP-OCR, else the local VLM; cached on disk per file; nothing leaves this server):
a fresh 4,000 px DfE photo 23-25 s at load 40-120, Stoa's S-100F / M-101A photos 110 / 268 s (no bar found: S-100F's is
in mm, M-101A prints none; the AFCEC dorm's 0-80' bar is not found either, 77 s). It is NOT the upload warm-up's reading
(that one is of the prepared copy: O1's deskewed photo), so the page asks for it only when the visitor taps a room, and
it waits for the warm-up first. Plausibility check: the sheet in the
photo must measure 1-3,000 m along its long side at the bar's scale, else the bar was misread (a '0 1000 2000' mm bar
read as metres is 1,000x off) and the page falls back to the door swings.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

WARM_WAIT = 90.0  # s: longest wait for the upload's warm-up before reading (then read anyway)
SHEET_M = (1.0, 3000.0)  # plausible real length of the photo's long side at the bar's scale (a detail .. a site plan)


def read(img: str) -> dict:
    t0 = time.time()
    from PIL import Image

    from stoa.solvers.raster import scalebar as SB

    with Image.open(img) as im:
        w, h = im.size
    # the upload's warm-up (stoa.formats.warm) reads the prepared copy first, which the visitor's first question waits
    # for: never compete with it (P1's rule); its text layer is of that copy (O1's deskewed one), not of this photo
    from stoa.formats.warm import wait_for_warm

    waited = wait_for_warm(img, WARM_WAIT)
    bars = [b for b in SB.bars(img) if b.unit in SB.UNIT_M and b.px_per_m and b.px_per_m > 0 and len(b.labels) >= 2]
    out: dict = {"ok": False, "why": "no scale bar with numbers and a unit found", "bars": len(bars), "w": w, "h": h}
    for b in bars:
        span_m = b.span_units * SB.UNIT_M[b.unit]
        sheet_m = max(w, h) / b.px_per_m
        if not (span_m > 0 and SHEET_M[0] <= sheet_m <= SHEET_M[1]):
            out["why"] = f"scale bar misread? the photo would be {sheet_m:.0f} m long"
            continue
        v0, x0, _ = b.labels[0]
        y = (b.box[1] + b.box[3]) / 2
        if b.vertical:  # a bar drawn top-down: label positions are y positions
            xm = (b.box[0] + b.box[2]) / 2
            pts = [[xm, x0], [xm, x0 + b.span_units * b.px_per_unit]]
        else:
            pts = [[x0, y], [x0 + b.span_units * b.px_per_unit, y]]
        out = {"ok": True, "px_per_m": round(b.px_per_m, 3), "unit": b.unit, "values": [v for v, _, _ in b.labels],
               "unit_text": b.unit_text, "pts": [[round(c, 1) for c in p] for p in pts], "m": round(span_m, 4),
               "box": [round(c, 1) for c in b.box], "residual": round(b.residual, 2), "text": b.describe(),
               "notes": b.notes[:3], "bars": len(bars), "w": w, "h": h}
        others = SB.consistency(bars)
        if others:
            out["consistency"] = others
        break
    out["waited_warm"] = waited
    out["seconds"] = round(time.time() - t0, 2)
    return out


if __name__ == "__main__":
    try:
        res = read(sys.argv[1])
    except Exception as e:  # noqa: BLE001 - the page falls back to the door swings
        res = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(res))
