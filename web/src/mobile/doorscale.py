"""Best-estimate scale of a PHOTO or raster plan from its door swings (W6 phone page, GET /api/m/doorscale).

    $JUNCTION_VENV/bin/python web/src/mobile/doorscale.py /tmp/stoa-uploads/ab12/photo.jpg
    -> {"ok": true, "px_per_m": 79.1, "arcs": 10, "leaf_m": 0.86, "side": 1200, "seconds": 2.1}

O1/W3's stoa.solvers.raster.doorarcs (the median radius of the stronger half of the swing arcs = one door leaf, the
same rule as doorarcs.scale_from_arcs) on a SHRUNKEN copy, scaled back to the image's own pixels: a 4,000 px photo
took > 2 minutes at full size, ~2 s at 1,200 px, ~10 s at 2,400 px.

The detector only sees arcs of >= 12 px radius in the copy. A whole A2 school sheet at 1:200 (Stoa's DfE example)
has its doors at ~8 px in a 1,200 px copy, BELOW that floor: what it finds instead are a few bigger arcs (1.3-2.4 m)
and the 'scale' came out 77 % too large (56 px/m vs the scale bar's 31.5). So:
  1. 1,200 px; keep it when the arcs' median radius is clearly above the floor (>= 1.5x);
  2. else look again at 2,400 px; keep it when the median is still above the floor (>= 1.15x). An image no larger
     than the copy has no sharper look: its one pass takes the 1.15x rule;
  3. else no estimate (the page asks for two taps on a known length).
Measured (phone-like photos, truth from a scale bar or a dimensioned wall):
  DfE school 1:200 (truth 31.5 px/m): 1,200 px 55.8 (median 1.2x floor -> rejected); 2,400 px 29.8 (-5 %, 1.28x)
  Fraser house (truth 88 px/m from the 6.06 m garage wall): 1,200 px 79.1 (-10 %, 1.7x floor) kept
  Pagenstr house: 1,200 px 98.4 (2.1x) kept (full-size run: 94.0)
  AFCEC dorm 1" = 40' (truth ~17 px/m, doors ~15 px in the PHOTO): 1,200 px 2 arcs; 2,400 px 24.0 (+40 %) but at
    1.03x the floor -> rejected, no estimate.
  CubiCasa (native rasters < 1,200 px, one pass): 6 of 8 sampled unchanged (2.1-3.3x the floor); high_quality-5624
    has NO door swings (openings are gaps) and the old rule gave 15.0 px/m off WC bowls / stove rings / scribbles
    (~5x too small) at 1.07x -> now rejected; high_quality-9970 (swings ~15-18 px, plausible 16.7 px/m) at 1.196x kept.
Six data points chose the thresholds: a best estimate, and the page says so ('estimated from N door swings' +
'Scale unknown — set it').
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

FLOOR = 12.0  # smallest arc radius (px in the shrunken copy) the detector looks for, as in doorarcs.scale_from_arcs
PASSES = ((1200, 1.5), (2400, 1.15))  # (long side of the copy, median radius must be >= this x FLOOR)


def arcs_at(im, side: int) -> tuple[float | None, int, float]:
    """(px per m in the ORIGINAL image, #arcs, median radius / FLOOR) on a copy shrunk to `side` px."""
    from PIL import Image

    from stoa.solvers.raster import doorarcs

    k = min(1.0, side / max(im.size))
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "small.png")
        small = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
        small.save(p)
        ps = 46.0  # a pseudo-scale, so detect()'s 1:200 upscale rule does not fire (as scale_from_arcs)
        dets = doorarcs.detect(p, ps, r_m=(FLOOR / ps, max(40.0, max(small.size) / 25) / ps))
    if len(dets) < 3:
        return None, len(dets), 0.0
    top = sorted(dets, key=lambda d: -d["score"])[: max(3, len(dets) // 2)]
    r = statistics.median(d["r_px"] for d in top)
    return r / doorarcs.DOOR_LEAF_M / k, len(dets), r / FLOOR


def main() -> None:
    t0 = time.time()
    out: dict = {"ok": False}
    try:
        from PIL import Image

        from stoa.solvers.raster import doorarcs

        im = Image.open(sys.argv[1]).convert("L")
        tried = []
        for side, need in PASSES:
            last = max(im.size) <= side  # a small image: this pass already sees it at full size, no second look
            need = PASSES[-1][1] if last else need
            s, n, rel = arcs_at(im, side)
            tried.append(f"{side} px: {n} arcs" + (f", {s:.1f} px/m at {rel:.2f}x the size floor" if s else ""))
            if s and rel >= need:
                out = {"ok": True, "px_per_m": round(s, 2), "arcs": n, "leaf_m": doorarcs.DOOR_LEAF_M, "side": side}
                break
            if last:
                break
        if not out["ok"]:
            out["why"] = "no door swings big enough to measure on this photo (" + "; ".join(tried) + ")"
        out["tried"] = tried
    except Exception as e:  # noqa: BLE001 - the page falls back to 'set the scale'
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    out["seconds"] = round(time.time() - t0, 2)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
