"""Best-estimate scale of a PHOTO or raster plan from its door swings (W6 phone page, GET /api/m/doorscale).

    $JUNCTION_VENV/bin/python web/src/mobile/doorscale.py /tmp/stoa-uploads/ab12/photo.jpg
    -> {"ok": true, "px_per_m": 79.1, "arcs": 10, "leaf_m": 0.85, "seconds": 2.1}

O1/W3's stoa.solvers.raster.doorarcs.scale_from_arcs (median radius of the strongest door arcs = one door leaf) on a
copy shrunk to 1,200 px on the long side (4,000 px photos took > 2 minutes; 1,200 px ~2 s), scaled back to the image's
own pixels. Measured on two phone photos (X1 fraser / pagenstr): Fraser 79 px/m vs 88 from its 6.06 m garage wall
(-10 %). A best estimate only: the page labels it 'estimated from N door swings' next to 'Scale unknown — set it'.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

SIDE = 1200


def main() -> None:
    t0 = time.time()
    out: dict = {"ok": False}
    try:
        from PIL import Image

        from stoa.solvers.raster import doorarcs

        im = Image.open(sys.argv[1]).convert("L")
        k = min(1.0, SIDE / max(im.size))
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "small.png")
            im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS).save(p)
            s, n = doorarcs.scale_from_arcs(p)
        leaf = getattr(doorarcs, "DOOR_LEAF_M", 0.85)
        out = {"ok": s is not None and s > 0, "arcs": n, "leaf_m": leaf}
        if s:
            out["px_per_m"] = round(s / k, 2)
        else:
            out["why"] = f"only {n} door swings found (3 needed)"
    except Exception as e:  # noqa: BLE001 - the page falls back to 'set the scale'
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    out["seconds"] = round(time.time() - t0, 2)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
