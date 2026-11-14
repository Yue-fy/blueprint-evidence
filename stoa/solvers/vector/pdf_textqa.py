"""pdf_textqa: a text-grounded reading of ANY vector PDF page (O1, X1 wild exam).

Real CAD sheets uploaded alone often print the answer in an abbreviation the question doesn't share a word with
('How many steps?' -> '21 Stg.', 'Wie hoch ist die Brüstung?' -> 'BRH 87.50', riser -> '16.2/27.8'): `pdf_text` needs a
shared word and abstains, then the prior answers. Here the page is rendered (~150 dpi, long side <= 3000 px) and the
PDF's OWN text spans become the text layer (exact strings and boxes, no OCR, no VLM text-spotting), then raster's
text-grounded reader (raster/textqa.py: the VLM sees the image + the numbered text lines and cites the lines it used)
answers, with every raster rule on top (cm dimension chains, scale-bar echo, printed-scale bar / room measurement).
Evidence boxes come back in pdf_pt on the original page. Tier: local VLM (one call), so after the deterministic PDF
readers. Not for the dataset's own sources (their twins / readers answer).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from stoa.contract import Answer
from stoa.data import source_of
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

MAX_SIDE = 3000
DPI = 150
SIZE_CAP, OTHER_CAP = 0.3, 0.6


def _cache_dir() -> Path:
    d = Path(os.environ.get("JUNCTION_SHARED", "/tmp")) / "artifacts" / "O1" / "pdfpng"
    d.mkdir(parents=True, exist_ok=True)
    return d


def render_with_text(pdf: str, page: int = 0):
    """-> (png path, TextLayer in image px, px per pdf pt). The PDF's text lines (spans joined per line) as items."""
    import pymupdf

    from stoa.solvers.raster import textlayer as T

    with pymupdf.open(pdf) as doc:
        pg = doc[page]
        w, h = pg.rect.width, pg.rect.height
        # an int: pymupdf's get_pixmap(dpi=<float>) raises TypeError (fz_pixmap_xres_set) — every sheet larger than
        # MAX_SIDE at DPI (A2 and up: X1 lvl11, exh222) failed here
        dpi = max(36, int(min(DPI, MAX_SIDE / max(w, h) * 72)))
        s = dpi / 72
        key = hashlib.sha1(f"{pdf}|{os.path.getmtime(pdf)}|{page}|{dpi:.2f}".encode()).hexdigest()[:16]
        png = _cache_dir() / f"{key}.png"
        if not png.exists():
            pg.get_pixmap(dpi=dpi).save(str(png))
        items = []
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(sp["text"].strip() for sp in ln["spans"] if sp["text"].strip())
                if not t:
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                items.append(T.TextItem(t, [x0 * s, y0 * s, x1 * s, y1 * s]))
        from PIL import Image

        with Image.open(png) as im:
            size = im.size
    tl = T.TextLayer(path=str(png), size=size, items=items, notes=["text layer = the PDF's own text (exact)"])
    return str(png), tl, s


def _to_pdf(ans: Answer, pdf: str, png: str, page: int, s: float) -> Answer:
    for e in ans.evidence:
        if str(e.file) != png or e.bbox_space not in ("image_px", None):
            continue
        e.file, e.page, e.bbox_space = pdf, page, "pdf_pt"
        if e.bbox:
            e.bbox = [round(v / s, 2) for v in e.bbox]
        if e.polygon:
            e.polygon = [[round(x / s, 2), round(y / s, 2)] for x, y in e.polygon]
        if e.polyline:
            e.polyline = [[round(x / s, 2), round(y / s, 2)] for x, y in e.polyline]
        if e.note and e.note.startswith("text '"):
            e.exact = True  # the PDF's own text box
    return ans


@register("pdf_textqa")
class PdfTextQA(BaseSolver):
    def __init__(self, backend: str = "local", **kwargs):
        super().__init__(**kwargs)
        self.backend = backend

    def can_handle(self, path: str, question: str) -> bool:
        return Path(path).suffix.lower() == ".pdf" and \
            source_of(path) not in ("swiss_dwellings", "ifc_models", "opening_design", "cubicasa5k")

    def solve(self, path: str, question: str) -> Answer:
        from stoa.solvers.raster import textqa

        try:
            png, tl, s = render_with_text(path)
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not render the PDF page: {type(e).__name__}")
        if len(tl.items) < 3:
            return self.abstain(f"{len(tl.items)} text lines in the PDF (outlined text: needs OCR on the rendered page)")
        ans = textqa.answer(png, question, image_path=png, backend=self.backend, solver=self.name, layer=tl)
        ans.trace = [f"pdf_textqa: page 1 rendered at {72 * s:.0f} dpi, {len(tl.items)} text lines from the PDF itself",
                     *(ans.trace or [])]
        # not calibrated on vector sheets yet (X1 wild, 10 Qs on one German CAD sheet: printed facts right — '21 Stg.'
        # -> 21, 'BRH 87.50' -> 87.5 cm — but sizes the sheet does not print were answered from unrelated notes at the
        # scans' 0.75: a room width '27.8 m' from the stair note '16.2/27.8'): sizes <= 0.3, the rest <= 0.6
        cap = SIZE_CAP if textqa._SIZE.search(question) or textqa._BIG_Q.search(question) else OTHER_CAP
        if ans.confidence and ans.confidence > cap and not any(a.startswith("measured on the drawing") for a in ans.assumptions):
            ans.confidence = cap
            ans.trace.append(f"pdf_textqa confidence capped at {cap} (no calibration on vector sheets yet)")
        return _to_pdf(ans, path, png, 0, s)
