"""The PRINTED scale of a vector PDF page (W6 phone page, GET /api/m/pdfscale), for tap-the-room on a PDF.

    $JUNCTION_VENV/bin/python web/src/mobile/pdfscale.py /tmp/stoa-uploads/ab12/plan.pdf 0
    -> {"ok": true, "n": 50.0, "all": [50.0], "text": "1:50", "source": "printed on the sheet", "seconds": 0.08}

Why: the phone page used to take a PDF's scale from its door swings (W5's /api/doorscale) when no answer had read one
yet. Rounded corners of title-block boxes are quarter arcs too, so Stoa's USACE A-101F (printed 1:50) came out
'1:250 from 20 door swings' and a tapped toilet read 50.8 m² instead of ~2 m². The printed scale comes first, as in
the solvers (stoa/agent/vision.py _pdf_scale): E1's linework.sheet_scales (every scale printed in the text layer,
most frequent first; US 1/4" = 1'-0" -> 48), else the recognition step's scale (1:N or feet-inches). No printed scale
-> {"ok": false}, and the page falls back to the door swings, then to two taps.
Measured on Stoa's sheets (0.02-0.65 s each): DfE school 1:200, USACE A-101F / S-100F 1:50, M-101A 1/4" = 1'-0" -> 48,
AFCEC medium fitness centre A-101 1/8" = 1'-0" -> 96, AFCEC dorm A-2 1" = 40' -> 480, the Swiss sample 1:200,
X1's LEVEL 11 1:100; the AW-78 scanned sheets and the enlisted dorm have none (ok: false).
"""

from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def printed_scale(pdf_abs: str, page: int = 0) -> dict:
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    t0 = time.perf_counter()
    found: tuple[float, ...] = ()
    text = ""
    source = "printed on the sheet"
    try:
        import pymupdf

        from stoa.evidence.linework import sheet_scales
        from stoa.solvers.raster.textlayer import parse_scales

        found = tuple(sheet_scales(pdf_abs, page))
        if found:  # how it is printed ('1/4" = 1'-0"' tells the page the sheet is in feet: sq ft next to m²)
            with pymupdf.open(pdf_abs) as doc:
                for ln in doc[page].get_text().splitlines():
                    hit = next((x for x in parse_scales(ln) if float(x["ratio"]) == found[0]), None)
                    if hit:
                        text = str(hit["text"])
                        break
    except Exception as e:  # noqa: BLE001
        print(f"pdfscale: sheet_scales: {e}", file=sys.stderr)
    if not found:
        try:
            from stoa.router.recognize import recognize
            from stoa.solvers.raster.textlayer import parse_scales

            rec = recognize(pdf_abs) or {}
            if rec.get("scale_source") == "printed" and rec.get("scale"):
                found = tuple(float(s["ratio"]) for s in parse_scales(f"SCALE {rec['scale']}") if 10 <= s["ratio"] <= 2000)
                text = str(rec["scale"])
                source = "printed on the sheet (read from the rendered page)"
        except Exception as e:  # noqa: BLE001
            print(f"pdfscale: recognize: {e}", file=sys.stderr)
    out: dict = {"ok": bool(found), "seconds": round(time.perf_counter() - t0, 2)}
    if found:
        out.update(n=found[0], all=list(found[:6]), text=text or f"1:{found[0]:g}", source=source)
    else:
        out["why"] = "no scale printed on the sheet"
    return out


if __name__ == "__main__":
    print(json.dumps(printed_scale(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)))
