"""Read an upload's costly common parts BEFORE the first question (P1 17:20).

The first question on a new sheet pays for reading it: a 6000-px copy of one of Stoa's sheets = ~30 VLM text tiles =
37-50 s (P1 17:18, cold, 4 questions on one file), the 14000-px original 81 s; every later question on that sheet
takes 0.4-3 s from the caches. A visitor needs 10-30 s to type the first question, so starting the reading when the
file arrives hides most of it:

    $JUNCTION_VENV/bin/python -m stoa.formats.warm /tmp/stoa-uploads/ab12/plan.png      # prints one JSON line

`warm(path)` = normalize.prepare (DWG -> DXF, SVG -> PDF, HEIC -> JPG, a big image's smaller copy) and, for an image,
the raster text layer (VLM or OCR as raster.textlayer picks; the disk cache is keyed by the file's content, so the
question's own process finds it), the clean-render gate, the recognise step, and for a clean sheet whose paper is
known (DPI tag, or pixel size + the sheet size it prints) the traced vector copy upload._as_vector reads (Stoa's DfE
PNG at 6000 px: the first question 158 s cold with tracing, later ones 3-9 s). A one-page PDF needs nothing: its
own text layer is read in milliseconds. Multi-page PDFs need the question to pick a page: not warmed. Never raises.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg"}


def warm(path: str) -> dict:
    t0 = time.perf_counter()
    out: dict = {"path": str(path), "done": [], "error": ""}
    try:
        from stoa.formats.normalize import prepare

        prep = prepare(str(path), "")
        out["solved_file"], out["kind"] = prep.path, prep.kind
        if prep.kind != "as_is":
            out["done"].append(f"prepared ({prep.kind})")
        if Path(prep.path).suffix.lower() in IMAGE_EXT:
            from stoa.solvers.raster import textlayer as T

            out["clean"] = T.is_clean(prep.path)
            tl = T.text_layer(prep.path)
            out["texts"] = len(tl.items)
            out["done"].append(f"text layer ({len(tl.items)} texts)")
            if out["clean"]:  # a clean sheet export with a known paper: the vector copy that upload._as_vector reads
                from stoa.formats import vectorize as V
                from stoa.formats.normalize import _cache_dir
                from stoa.formats.paper import paper_of_image

                hit = V.paper(prep.path) or paper_of_image(prep.path, [i.text for i in tl.items])
                if hit:
                    src = Path(prep.path)
                    vec = _cache_dir(src) / f"{src.stem}.vec{V.VERSION}.pdf"
                    if not vec.exists() or vec.stat().st_mtime < src.stat().st_mtime:
                        V.to_vector_pdf(str(src), str(vec), dpi=hit[0])
                    out["done"].append(f"vector copy ({hit[1]} at {hit[0]:.0f} dpi)")
        try:
            from stoa.router.recognize import recognize

            rec = recognize(prep.path)
            out["recognised"] = getattr(rec, "kind", None) or (rec.get("kind") if isinstance(rec, dict) else None)
            out["done"].append("recognised")
        except Exception as e:  # noqa: BLE001 — older trees / unreadable: the question does it itself
            out.setdefault("notes", []).append(f"recognise skipped ({type(e).__name__})")
    except Exception as e:  # noqa: BLE001 — warming is only a head start; the question still answers
        out["error"] = f"{type(e).__name__}: {e}"
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def main(argv: list[str]) -> None:
    for p in argv or []:
        print(json.dumps(warm(p)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
