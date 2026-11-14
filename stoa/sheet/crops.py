"""Renders of the view(s) a question means, for image readers (VLM count/area readers, V1's door detector, the agent's
`look`): on a multi-view sheet they otherwise see the RCP and axonometric copies of the same rooms and doors too
(AFCEE A-101 'doors in module 02': whole-sheet VLM 10, gold 9).

    crops = crop_for(pdf, question)          # [] = no scoping (single view sheet): use the whole sheet as before
    crops[0].png                             # PNG of one picked view (cached by file content + view + dpi)
    crops[0].to_pdf_pt(x_px, y_px)           # a box / point found on the crop -> pdf_pt (CONTRACT evidence space)
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from stoa.sheet.views import View, scope_for

CROP_DIR = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "artifacts" / \
    "VP" / "crops"


@dataclass
class Crop:
    png: str
    view: View
    dpi: float
    clip: tuple          # displayed-page pt of the crop's top-left .. bottom-right
    rotation: int        # page /Rotate
    page_w: float        # unrotated page size (pt), for the inverse rotation
    page_h: float

    def to_pdf_pt(self, x: float, y: float) -> tuple[float, float]:
        """Crop pixel -> PyMuPDF unrotated page point."""
        import pymupdf

        k = 72.0 / self.dpi
        p = pymupdf.Point(self.clip[0] + x * k, self.clip[1] + y * k)
        # displayed -> unrotated = page.derotation_matrix (checked against PyMuPDF for 0/90/180/270)
        rot = {0: pymupdf.Matrix(1, 0, 0, 1, 0, 0), 90: pymupdf.Matrix(0, -1, 1, 0, 0, self.page_h),
               180: pymupdf.Matrix(-1, 0, 0, -1, self.page_w, self.page_h),
               270: pymupdf.Matrix(0, 1, -1, 0, self.page_w, 0)}[self.rotation % 360]
        q = p * rot
        return (round(q.x, 2), round(q.y, 2))

    def box_to_pdf_pt(self, box) -> tuple:
        a, b = self.to_pdf_pt(box[0], box[1]), self.to_pdf_pt(box[2], box[3])
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _key(pdf: str, page: int, v: View, dpi: float) -> str:
    st = os.stat(pdf)
    h = hashlib.sha1(f"{os.path.abspath(pdf)}|{st.st_size}|{st.st_mtime_ns}|{page}|{v.display_bbox}|{dpi}".encode())
    return h.hexdigest()[:16]


def crop_for(path: str, question: str, page: int = 0, dpi: float = 150.0, max_px: int = 3000,
             margin_pt: float = 6.0) -> list[Crop]:
    """One PNG per picked view of a PDF page ([] when the sheet needs no scoping or is not a PDF)."""
    if not str(path).lower().endswith(".pdf"):
        return []
    s = scope_for(path, question, page)
    if not s:
        return []
    import pymupdf

    out = []
    try:
        with pymupdf.open(path) as doc:
            pg = doc[page]
            W, H = pg.rect.width, pg.rect.height
            for v in s.views:
                if not v.display_bbox:
                    continue
                x0, y0, x1, y1 = v.display_bbox
                clip = (max(0.0, x0 - margin_pt), max(0.0, y0 - margin_pt), min(W, x1 + margin_pt), min(H, y1 + margin_pt))
                d = int(min(dpi, max_px * 72.0 / max(clip[2] - clip[0], clip[3] - clip[1])))
                CROP_DIR.mkdir(parents=True, exist_ok=True)
                png = CROP_DIR / f"{Path(path).stem[:40]}.p{page}.{_key(path, page, v, d)}.png"
                if not png.exists():
                    pg.get_pixmap(clip=pymupdf.Rect(clip), dpi=d).save(str(png))
                out.append(Crop(str(png), v, d, clip, pg.rotation, pg.mediabox.width, pg.mediabox.height))
    except Exception as e:  # noqa: BLE001 - never break a reader
        import sys

        print(f"[sheet.crops] {path}: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    return out
