"""The printed words of a raster plan, as points in ITS pixels (W6 phone page, GET /api/m/words).

    $JUNCTION_VENV/bin/python web/src/mobile/words.py /tmp/stoa-uploads/ab12/plan.png
    -> {"ok": true, "source": "traced copy", "w": 6000, "h": 4244, "words": [[x, y, "Toilets", hw, hh], ...], "seconds": 0.05}

(x, y = the word's centre, hw / hh = half its width / height, all in the picture's px.)

Tap-the-room steps out of furniture only when no printed word sits inside the small outline (a store or a WC has its
name inside; a table has none). On a vector PDF the page reads that from pdf.js; on a picture it needs this:
1. the upload's traced copy '<stem>.vec6.pdf' (P1's stoa.formats.vectorize: the picture's lines as strokes + its OCR
   text, one page at the picture's aspect; Stoa's DfE Level 00 PNG: 261 words), scaled by img_w / page_w;
2. else the picture's OCR reading if it is ALREADY on disk (stoa.ocr cache; never started from a tap: a cold read is
   20-270 s);
3. else {"ok": false}: the page keeps the small outline and says 'if that is furniture, tap an empty spot'.
A phone photo's traced copy is of its DESKEWED copy ('<stem>.photo.vec6.pdf', other pixels): not used (the aspect check
would mostly catch it too).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

ASPECT_TOL = 0.01  # the traced page must have the picture's aspect (same pixels, only scaled)


def _word(t: str) -> bool:
    """a label word: 2+ letters or a digit (the same rule as the page's pdf.js text runs)"""
    t = t.strip()
    return sum(c.isalpha() for c in t) >= 2 or any(c.isdigit() for c in t)


def read(img: str) -> dict:
    t0 = time.time()
    from PIL import Image

    with Image.open(img) as im:
        w, h = im.size
    p = Path(img)
    vec = p.with_name(p.stem + ".vec6.pdf")
    if vec.exists():
        import pymupdf

        with pymupdf.open(vec) as doc:
            pg = doc[0]
            pw, ph = pg.rect.width, pg.rect.height
            if pw > 0 and ph > 0 and abs((w / h) / (pw / ph) - 1) <= ASPECT_TOL and pg.rotation == 0:
                k = w / pw
                ws = [[round((x0 + x1) / 2 * k, 1), round((y0 + y1) / 2 * k, 1), t,
                       round((x1 - x0) / 2 * k, 1), round((y1 - y0) / 2 * k, 1)]
                      for x0, y0, x1, y1, t, *_ in pg.get_text("words") if _word(t)]
                return {"ok": True, "source": "traced copy", "w": w, "h": h, "words": ws,
                        "seconds": round(time.time() - t0, 3)}
    from stoa import ocr

    if ocr.cached(img):
        d = ocr.read(img)
        sw, sh = (d.get("size") or [w, h])[:2]
        kx, ky = w / sw if sw else 1.0, h / sh if sh else 1.0
        ws = []
        for i in d.get("items", []):
            b = i.get("box") or []
            if len(b) == 4 and _word(str(i.get("text", ""))):
                ws.append([round((b[0] + b[2]) / 2 * kx, 1), round((b[1] + b[3]) / 2 * ky, 1), str(i["text"]),
                           round(abs(b[2] - b[0]) / 2 * kx, 1), round(abs(b[3] - b[1]) / 2 * ky, 1)])
        return {"ok": True, "source": "text layer", "w": w, "h": h, "words": ws, "seconds": round(time.time() - t0, 3)}
    return {"ok": False, "why": "no text layer read yet", "w": w, "h": h, "seconds": round(time.time() - t0, 3)}


if __name__ == "__main__":
    try:
        out = read(sys.argv[1])
    except Exception as e:  # noqa: BLE001 - the page falls back to the hint
        out = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(out))
