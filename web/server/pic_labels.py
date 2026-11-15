"""The printed text of a picture (PNG / JPG) with its place, for the right-click selection on picture sheets.

Called by web/mock/stoaApi.ts (GET /api/piclabels?path=), one process per picture (cached by the middleware):

    $JUNCTION_VENV/bin/python web/server/pic_labels.py /tmp/stoa-uploads/ab12/sheet.png

Why (UX 23:51): on a PDF the viewer reads the words from pdf.js's text layer, so a right-click knows the room label or the
tag under the pointer; on a picture (Stoa's DfE / AFCEC / M-101A PNGs, every scan and phone photo) it had no text at all
and recognised nothing. This prints the picture's text layer (stoa.solvers.raster.sheet.cached_layer: PP-OCR or the VLM
reading the upload's warm-up already made, or the PDF's own words for a rendered page) as label points in image_px, the
same space as the picture's evidence boxes. It reads only a CACHED layer: never starts an OCR / VLM reading (the upload's
warm-up does that), so it costs no model call.
Output: {"ok": true, "items": [{"at": [x, y], "text": "...", "box": [x0, y0, x1, y1]}, ...], "source": "..."} or
{"ok": false, "why": "..."}.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def stacks(items: list[dict]) -> list[dict]:
    """A room name printed on two lines ('Secondary' over 'Classroom', DfE) is read as two lines: each such stack is also
    one label 'Secondary Classroom' (box = both lines), as web/src/viewer2d.ts labels() does for a PDF's words. Stack =
    letters only, same text height (+-30 %), centres within half the wider line, next line 0.6-1.8 heights lower."""
    words = [i for i in items if len(i["text"]) <= 24 and all(c.isalpha() or c in " '&/-" for c in i["text"])]
    out = []
    for a in words:
        ha = a["box"][3] - a["box"][1]
        for b in words:
            if b is a:
                continue
            hb = b["box"][3] - b["box"][1]
            if ha <= 0 or hb <= 0 or abs(ha - hb) > 0.3 * max(ha, hb):
                continue
            dy = b["at"][1] - a["at"][1]
            wa, wb = a["box"][2] - a["box"][0], b["box"][2] - b["box"][0]
            if 0.6 * ha <= dy <= 1.8 * ha and abs(b["at"][0] - a["at"][0]) <= max(wa, wb) / 2:
                box = [min(a["box"][0], b["box"][0]), a["box"][1], max(a["box"][2], b["box"][2]), b["box"][3]]
                out.append({"at": [round((box[0] + box[2]) / 2, 1), round((box[1] + box[3]) / 2, 1)],
                            "text": f"{a['text']} {b['text']}", "box": box, "stack": True})
                break
    return out


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path:
        print(json.dumps({"ok": False, "why": "no path"}))
        return
    # the repo (cwd = STOA_KG_REPO when the middleware runs it; else this file's repo): sys.path[0] is web/server
    for root in (os.getcwd(), str(Path(__file__).resolve().parents[2])):
        if (Path(root) / "stoa").is_dir() and root not in sys.path:
            sys.path.insert(0, root)
            break
    try:
        from stoa.solvers.raster.sheet import cached_layer

        tl = cached_layer(path)
    except Exception as e:  # noqa: BLE001 - no text layer is 'nothing recognised', never an error page
        print(json.dumps({"ok": False, "why": f"text layer unavailable ({type(e).__name__})"}))
        return
    if tl is None:
        print(json.dumps({"ok": False, "why": "the picture's text is not read yet"}))
        return
    items = []
    for it in tl.items:
        t = " ".join(str(it.text or "").split())
        b = [float(v) for v in (it.box or [])][:4]
        if not t or len(b) != 4 or len(t) > 160:
            continue
        items.append({"at": [round((b[0] + b[2]) / 2, 1), round((b[1] + b[3]) / 2, 1)], "text": t,
                      "box": [round(v, 1) for v in b]})
    items += stacks(items)
    print(json.dumps({"ok": True, "items": items, "size": list(tl.size or ()), "source": "; ".join(tl.notes[:1])}))


if __name__ == "__main__":
    main()
