"""The scale of a picture (PNG / JPG) from what is printed on it, for the ready card when no graphic scale bar was read.

Called by web/mock/stoaApi.ts (GET /api/picscale?path=), one process per request (~1-2 s once the upload's text is read):

    $JUNCTION_VENV/bin/python web/server/pic_scale.py /tmp/stoa-uploads/ab12/sheet.png [ORIGINAL.png]

Why (QA 22:33 / S0 22:56): Stoa's AFCEC A-2 picture prints 'SCALE: 1" = 40'-0"' and a bar, yet the card said 'No scale
found on the drawing': the bar reader finds no numbered bar, and a printed ratio alone does not size a picture's pixels
(the paper size is unknown). F2's stoa.features.takeoff.paper_by_dims settles the paper: when the pixel size fits several
standard sheets, the one whose printed 1:N makes the drawing's own dimension strings ('286'-0"') as long as their drawn
lines wins (AFCEC: Tabloid at 353 dpi, 1:480, 4 strings agree). Order: stoa.features.region.image_scale (the visitor's
reading of the printed ratio + a named paper size, or a scale bar), then paper_by_dims. Reads only a text layer that is
already cached (never starts an OCR / VLM reading: the upload's warm-up does that).
Output: {"ok": true, "px_per_m", "n" (printed 1:N or null), "printed" ('1" = 40'-0"' / '1:200'), "paper", "dpi", "how"}
or {"ok": false, "why"}.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

INCH_FRACTIONS = {12: '1"', 16: '3/4"', 24: '1/2"', 32: '3/8"', 48: '1/4"', 64: '3/16"', 96: '1/8"', 128: '3/32"', 192: '1/16"'}


def printed_words(n: float | None, imperial: bool) -> str:
    """1:N in the words a drawing prints it: imperial 1:480 → '1" = 40'-0"', 1:48 → '1/4" = 1'-0"'; metric '1:200'."""
    if not n:
        return ""
    k = int(round(n))
    if imperial and abs(n - k) < 0.01:
        if k in INCH_FRACTIONS:
            return f"{INCH_FRACTIONS[k]} = 1'-0\""
        if k % 12 == 0 and k >= 120:
            return f"1\" = {k // 12}'-0\""
    return f"1:{n:g}"


def main(abs_path: str) -> dict:
    # the repo (cwd = STOA_KG_REPO when the middleware runs it; else this file's repo): sys.path[0] is web/server
    for root in (os.getcwd(), str(Path(__file__).resolve().parents[2])):
        if (Path(root) / "stoa").is_dir() and root not in sys.path:
            sys.path.insert(0, root)
            break
    from stoa.features import region as R

    if R._image_layer(Path(abs_path)) is None:
        return {"ok": False, "why": "the picture's text is not read yet"}
    from stoa.solvers.raster import textlayer as T

    layer = T.text_layer(abs_path)
    if layer is None:
        return {"ok": False, "why": "the picture's text is not read yet"}
    text = " ".join(getattr(it, "text", "") for it in layer.items)
    imperial = bool(re.search(r"\d\s*'\s*-?\s*\d+\s*\"|\d\s*\"\s*=\s*\d+\s*'", text))
    ppm, why = R.image_scale(abs_path)
    if ppm:
        n = re.search(r"printed 1:([\d.]+)", why)
        dpi = re.search(r"at ([\d.]+) dpi", why)
        paper = re.search(r" on (.+?) at [\d.]+ dpi", why)
        nn = float(n.group(1)) if n else None
        return {"ok": True, "px_per_m": ppm, "n": nn, "printed": printed_words(nn, imperial), "paper": paper.group(1) if paper else "",
                "dpi": float(dpi.group(1)) if dpi else None, "how": why}
    try:
        from stoa.features.takeoff import paper_by_dims
        from stoa.formats.vectorize import _scale_n
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": why}
    n = _scale_n(layer.items)
    got = paper_by_dims(abs_path, layer)
    if not got or not n:
        return {"ok": False, "why": why}
    dpi, sheet, how = got
    return {"ok": True, "px_per_m": dpi / 0.0254 / n, "n": n, "printed": printed_words(n, imperial), "paper": sheet, "dpi": dpi,
            "how": how}


def _width(p: str) -> int:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(p) as im:
        return int(im.size[0])


def with_original(abs_path: str, orig: str | None = None) -> dict:
    """UX pass 2a NEW-3: a large picture's upload is a 6,000 px copy (normalize_upload.MAX_SIDE), so 'printed 1:48 on ANSI
    D at 300 dpi' no longer fits (6,000 px / 34 in = 176 dpi): Stoa's M-101A card printed 1/4" = 1'-0" and the sheet then
    said 'No scale found'. When the copy gives nothing, read the full-size original (its '<stem>.full<ext>' kept beside the
    copy, or the catalogue file the copy was made from) and carry its px per m over by the width ratio."""
    out = main(abs_path)
    if out.get("ok"):
        return out
    p = Path(abs_path)
    for cand in [str(p.with_name(f"{p.stem}.full{p.suffix}")), orig or ""]:
        if not cand or not Path(cand).is_file() or Path(cand).resolve() == p.resolve():
            continue
        try:
            r = main(cand)
            if not r.get("ok"):
                continue
            k = _width(abs_path) / max(1, _width(cand))
            if not (0.05 < k <= 1.0):
                continue
            r["px_per_m"] = float(r["px_per_m"]) * k
            if r.get("dpi"):
                r["dpi"] = round(float(r["dpi"]) * k, 3)
            r["how"] = f"{r.get('how', '')} (read on the full-size original; this copy is {k:.3f}× its width: 1 m = {r['px_per_m']:.2f} px)"
            return r
        except Exception as e:  # noqa: BLE001
            print(f"pic_scale: original {cand}: {e}", file=sys.stderr)
    return out


if __name__ == "__main__":
    try:
        out = with_original(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"could not read the picture ({type(e).__name__})"}
    print(json.dumps(out))
