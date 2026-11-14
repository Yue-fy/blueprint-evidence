"""A small JPEG of a drawing for the phone page's 'Try a case' cards (W6): python thumb.py SRC.pdf|png|jpg OUT.jpg

First page of a PDF (PyMuPDF) or the image (Pillow), 360 px on its short side's card crop (the card shows the middle,
object-fit: cover), JPEG q78. Prints nothing; exit 1 on failure. ~0.2-1 s; the server caches the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

W = 360


def main(src: str, out: str) -> int:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    if src.lower().endswith(".pdf"):
        import pymupdf

        page = pymupdf.open(src)[0]
        z = W * 2 / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        im = Image.open(src)
        im.draft("RGB", (W * 2, W * 2))  # JPEG: decode at a fraction of the size
        im = im.convert("RGB")
    im.thumbnail((W, W * 2))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    tmp = out + ".part"
    im.save(tmp, "JPEG", quality=78, optimize=True)
    Path(tmp).replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
