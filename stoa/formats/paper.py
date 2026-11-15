"""Which paper sheet an image is, from its PIXEL size + the sheet size it prints (P1, 14 Nov 16:55).

Stoa's example PNGs carry no DPI tag (or PyMuPDF's 96), yet each is a standard sheet rendered at a usual export
resolution: 9934 x 7017 = A1 @ 300 dpi, 10200 x 6600 = ANSI D @ 300 dpi, 14000 x 9902 = A2 @ ~599 dpi. Pixels alone
are ambiguous by a factor 2 (ANSI D @ 300 = Tabloid @ 600; A2 @ 600 ~ A0 @ 300; A1 @ 300 = A3 @ 600), so a candidate
counts only when the sheet's own text names it ('ISO A1', 'A2', 'ANSI D'). Checked on the 4 Stoa sheets against their
PDF twins' page sizes: DfE A2 ✓, M-101A ANSI D ✓, S-100F A1 ✓; the AFCEC sheet (Tabloid @ 600 dpi) names no size ->
None (choosing 'the big sheet at 300 dpi' would have been wrong there).

    paper_from_pixels((w, h), texts) -> (dpi, 'A2') | None
"""

from __future__ import annotations

import re
from pathlib import Path

# resolutions sheets are exported at; never 72 / 96 (screens: a screenshot is not a sheet)
EXPORT_DPI = (100, 150, 200, 240, 250, 300, 400, 600)
PIXEL_TOL = 0.004  # both sides within 0.4 % of the sheet at that resolution (Stoa's A2: 0.22 %)

PAPER_MM = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420), "A4": (210, 297), "A5": (148, 210),
            "Letter": (215.9, 279.4), "Legal": (215.9, 355.6), "Tabloid": (279.4, 431.8), "ANSI C": (431.8, 558.8),
            "ANSI D": (558.8, 863.6), "ANSI E": (863.6, 1117.6), "ARCH A": (228.6, 304.8), "ARCH B": (304.8, 457.2),
            "ARCH C": (457.2, 609.6), "ARCH D": (609.6, 914.4), "ARCH E": (914.4, 1219.2), "ARCH E1": (762, 1066.8)}
_ALIAS = {"Tabloid": ("tabloid", "ledger", "ansi b", r"11\s*[x×]\s*17"), "Letter": ("letter", "ansi a"),
          "Legal": ("legal",)}


def candidates(size: tuple[int, int]) -> list[tuple[str, int]]:
    """Every (sheet, export dpi) whose pixel size matches within PIXEL_TOL on both sides."""
    a, b = sorted(size)
    out = []
    for name, (sw, sl) in PAPER_MM.items():
        for d in EXPORT_DPI:
            ea, eb = sw / 25.4 * d, sl / 25.4 * d
            if abs(a - ea) <= PIXEL_TOL * ea and abs(b - eb) <= PIXEL_TOL * eb:
                out.append((name, d))
    return out


def _named(name: str, texts: list[str]) -> bool:
    """Does any text item name this sheet size? A-sizes: an item that is just 'A1' / 'ISO A1' / 'A1 size' / '@ A1'
    ('A1/S-513' and 'GRID A1' are references, not sheet sizes)."""
    if name.startswith("A") and len(name) == 2:
        rx = re.compile(rf"^\s*(?:iso\s*|@\s*|sheet\s*(?:size)?\s*:?\s*|paper\s*(?:size)?\s*:?\s*)?{name}"
                        rf"(?:\s*(?:size|sheet|paper))?\s*$|@\s*{name}\b|\biso\s*{name}\b", re.I)
    else:
        words = _ALIAS.get(name, (re.escape(name.lower()).replace(r"\ ", r"\s*"),))
        rx = re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)
    return any(rx.search(t or "") for t in texts)


def paper_from_pixels(size: tuple[int, int], texts: list[str]) -> tuple[float, str] | None:
    """(exact dpi, sheet) when exactly one pixel-size candidate is named by the sheet's text, else None. The dpi is
    computed from the sheet's size (14000 px over A2's 594 mm = 598.6 dpi), not the nominal export value."""
    names = {n for n, _ in candidates(size)}
    hit = [n for n in names if _named(n, texts)]
    if len(hit) != 1:
        return None
    sw, sl = PAPER_MM[hit[0]]
    a, b = sorted(size)
    return (a / (sw / 25.4) + b / (sl / 25.4)) / 2, hit[0]


SRC_SIZE_KEY = "stoa_src_size"  # PNG text chunk written by normalize.shrink_image: the pixel size before the shrink


def paper_of_image(path: str, texts: list[str]) -> tuple[float, str] | None:
    """(dpi of THIS file, sheet) for an untagged sheet export: paper_from_pixels on the image's pixel size, or on its
    size before normalize.shrink_image made this smaller copy (the UI reads 6000-px copies: 14000 px A2 @598.7 dpi ->
    6000 px @256.6 dpi), the dpi scaled to this file. None when the sheet's text does not name exactly one candidate."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as im:
            size = im.size
            raw = str(im.info.get(SRC_SIZE_KEY) or "")
            if not raw:  # JPEG copies carry it as their comment ('stoa_src_size=14000x9902')
                c = im.info.get("comment") or b""
                c = c.decode("latin-1", "replace") if isinstance(c, bytes) else str(c)
                raw = c.split(f"{SRC_SIZE_KEY}=", 1)[1] if f"{SRC_SIZE_KEY}=" in c else ""
    except Exception:  # noqa: BLE001
        return None
    m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", raw)
    src = (int(m.group(1)), int(m.group(2))) if m else _sidecar_src_size(path, size) or size
    hit = paper_from_pixels(src, texts)
    if hit is None:
        pdf = _scan_source(path)  # a scanned PDF's render: the scan's own pixels (paper_of_scan step 1)
        got = paper_of_scan(pdf, size, texts, use_page=False) if pdf else None
        return got[:2] if got else None
    return hit[0] * size[0] / src[0], hit[1]


def _scan_source(path: str) -> str | None:
    """The scanned PDF that normalize.scan_png rendered to `path` (its '<render>.json' sidecar, else '<stem>.pdf' next to
    '<stem>.scan.png'); None for any other image or with STOA_SCAN_PAPER=0."""
    import json
    import os

    if os.environ.get("STOA_SCAN_PAPER", "1") == "0":
        return None
    try:
        pdf = json.loads(Path(f"{path}.json").read_text()).get("scan_pdf")
        if pdf and Path(pdf).is_file():
            return str(pdf)
    except (OSError, ValueError, AttributeError):
        pass
    name = Path(path).name
    if name.endswith(".scan.png"):
        sib = Path(path).with_name(name[: -len(".scan.png")] + ".pdf")
        if sib.is_file():
            return str(sib)
    return None


def _sidecar_src_size(path: str, size: tuple[int, int]) -> tuple[int, int] | None:
    """The original pixel size from normalize.shrunk_copy's '<copy>.json' sidecar: copies cached before the PNG tag
    existed (gate d22: Stoa's DfE copy from 17:35 had none, so the 14000-px A2 export was never traced) and JPEG copies.
    Only when the sidecar's factor maps that size onto this file's (a stale sidecar of another image is ignored)."""
    import json

    try:
        m = json.loads(Path(f"{path}.json").read_text())
        w, h = (int(v) for v in m["src_size"])
        f = float(m["factor"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if 0 < f < 1 and abs(w * f - size[0]) <= 1.5 and abs(h * f - size[1]) <= 1.5:
        return w, h
    return None


SCAN_PAGE_TOL = 0.01  # a scanned PDF's page within 1 % of a standard sheet on both sides
SCAN_DPI_TOL = 0.005  # its image's pixels per page inch within 0.5 % of a usual scan resolution
_ISO = re.compile(r"^A\d$")


def _page_sheet(width_pt: float, height_pt: float, tol: float = SCAN_PAGE_TOL) -> str | None:
    """The standard sheet (ISO A or US ANSI / ARCH / Letter / Tabloid) a PDF page is, both sides within tol."""
    a, b = sorted((width_pt / 72 * 25.4, height_pt / 72 * 25.4))
    for name, (w, h) in PAPER_MM.items():
        if abs(a - w) <= tol * w and abs(b - h) <= tol * h:
            return name
    return None


def _names_size(name: str, texts: list[str]) -> bool:
    """Stricter than _named: the text says it is a SHEET SIZE ('ISO A1', '@ A1', 'A1 size', 'sheet size: A1', 'ANSI D'),
    so a lone 'A2' grid / detail reference (S-100F prints one next to its 'ISO A1') does not count."""
    n = re.escape(name).replace(r"\ ", r"\s*")
    if _ISO.match(name):
        rx = re.compile(rf"\biso\s*{n}\b|@\s*{n}\b|\b{n}\s*(?:size|sheet|paper)\b|"
                        rf"\b(?:sheet|paper)\s*(?:size)?\s*:?\s*{n}\b", re.I)
    else:
        rx = re.compile(rf"\b{n}\b", re.I)
    return any(rx.search(t or "") for t in texts)


def paper_of_scan(pdf: str, size: tuple[int, int], texts: list[str],
                  use_page: bool = True) -> tuple[float, str, str] | None:
    """(dpi of the render `size`, sheet, how) for page 0 of a SCANNED PDF that normalize.scan_png rendered to an image of
    `size` px (P1 22:40, d29 scan_dev: 4 of X1's scanned Stoa sheets were never traced: the render is untagged on
    purpose and shrunk to 6000 px, so its own pixel size is no export size; 13 rows got raster guesses instead).
    (1) The page image's OWN pixel size at a usual scan resolution, confirmed by the sheet size it prints (the
        paper_from_pixels rule: 6800 x 4400 px + 'ANSI D' -> 200 dpi; 6623 x 4678 + 'ISO A1' -> A1 at 200 dpi). This also
        finds an A1 drawing scanned from an A3 print (4961 x 3508 = A3 @300 = A1 @150 + 'A1' printed -> A1).
    (2) use_page: else the PDF page's own size when it is a standard sheet, the image fills it (>= 95 %), its pixels per
        page inch are a usual scan resolution on both sides, and the sheet names no OTHER paper of the same family
        (an 'A1' title block on an A3 page = a reduced copy: its printed 1:N is for A1, so no guess). Weaker than (1):
        a reduced copy that prints no size is taken at the page's size (as for a vector PDF page).
    None when neither holds (no usable image, a custom page size)."""
    import pymupdf

    try:
        with pymupdf.open(pdf) as d:
            pg = d[0]
            pw, ph = pg.rect.width, pg.rect.height
            best, img, rect = 0.0, None, None
            for im in pg.get_images(full=True):
                for r in pg.get_image_rects(im[0]):
                    cover = (r & pg.rect).get_area() / max(pg.rect.get_area(), 1e-9)
                    if cover > best:
                        best, img, rect = cover, (int(im[2]), int(im[3])), r
    except Exception:  # noqa: BLE001
        return None
    if img is None or best < 0.5 or not pw or not ph or rect.width <= 0 or rect.height <= 0:
        return None
    zoom = max(size) / max(pw, ph)  # render px per page pt (scan_png renders the whole page)
    # image px per page pt where it is drawn (an image drawn rotated swaps its sides)
    iw, ih = img if (img[0] >= img[1]) == (rect.width >= rect.height) else (img[1], img[0])
    px_x, px_y = iw / rect.width, ih / rect.height
    hit = paper_from_pixels(img, texts)
    if hit is not None:
        f = zoom / ((px_x + px_y) / 2)  # render px per image px
        return (hit[0] * f, hit[1], f"from the scan's own pixel size ({img[0]} x {img[1]} = {hit[1]} at "
                                    f"{hit[0]:.0f} dpi) and the sheet size it prints")
    if not use_page:
        return None
    sheet = _page_sheet(pw, ph)
    if sheet is None or best < 0.95:
        return None
    dx, dy = px_x * 72, px_y * 72
    if abs(dx - dy) > SCAN_DPI_TOL * dx or not any(abs(dx - s) <= SCAN_DPI_TOL * s for s in EXPORT_DPI):
        return None
    iso = bool(_ISO.match(sheet))
    if any(bool(_ISO.match(n)) == iso and n != sheet and n not in _ALIAS and _names_size(n, texts) for n in PAPER_MM):
        return None
    return (72 * zoom, sheet, f"from the scanned PDF's page size ({sheet}, scanned at {dx:.0f} dpi; taken as the "
                              f"drawing's own paper: on a reduced copy the measured sizes scale with it)")


BAR_SNAP_TOL = 0.03  # the bar's sheet within 3 % of a standard paper on both sides -> that paper's exact dpi


def paper_of_bar(path: str, layer) -> tuple[float, str, str] | None:
    """(dpi of THIS file, sheet, how) from the drawing's own metric scale bar and its printed 1:N (X1 19:20): a scan or
    crop whose pixel size is no standard paper and that has no DPI tag (a scanned PDF's page image, a skewed scan's
    larger canvas) still says how many pixels a paper inch is: px per real metre x N x 0.0254. X1's DfE scan: bar
    39.08 px/m x 1:200 -> 198.5 dpi (true 200). When that dpi makes the image a standard sheet within 3 % and both of
    its sides give the same dpi within 1 %, the paper's exact dpi is used; else the bar's ('custom' sheet). `layer` =
    the image's raster text layer (stoa.solvers.raster.textlayer). None without a metric bar or a printed 1:N."""
    from PIL import Image

    from stoa.solvers.raster import scalebar as SB
    from stoa.solvers.raster.textlayer import parse_scales

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as im:
            w, h = im.size
        bar = next((b for b in SB.bars(path, layer=layer) if b.unit in SB.UNIT_M and b.px_per_m), None)
    except Exception:  # noqa: BLE001
        return None
    ns = [float(n) for it in layer.items for sc in parse_scales(it.text)
          if (n := sc.get("n") or sc.get("scale") or sc.get("ratio"))]
    if bar is None or not ns:
        return None
    from collections import Counter

    n = Counter(ns).most_common(1)[0][0]
    dpi = bar.px_per_m * n * 0.0254
    if not 50 <= dpi <= 1200:
        return None
    how = f"from its scale bar ({bar.px_per_m:.2f} px per m) and its printed 1:{n:g}"
    a_px, b_px = sorted((w, h))
    a, b = a_px / dpi * 25.4, b_px / dpi * 25.4
    for name, (p, q) in PAPER_MM.items():
        if abs(a - p) <= BAR_SNAP_TOL * p and abs(b - q) <= BAR_SNAP_TOL * q:
            da, db = a_px / p * 25.4, b_px / q * 25.4
            if abs(da - db) <= 0.01 * max(da, db):
                return (da + db) / 2, name, f"{how}, = {name} at its pixel size"
    return dpi, "custom", how


# '1:200 @ A1', '1:200 at A1', 'SCALE 1:50@A1' (D1 16:50 / X1 17:41: UK EFA sheets print the scale for A1 on an A3 PDF)
_SCALE_AT = re.compile(r"\b1\s*:\s*(\d{1,5})\s*(?:@|at|bei|auf)\s*(A[0-4])\b", re.I)


def page_paper(width_pt: float, height_pt: float, tol: float = 0.03) -> str | None:
    """The ISO A sheet a PDF page is (both sides within tol), or None."""
    a, b = sorted((width_pt / 72 * 25.4, height_pt / 72 * 25.4))
    for name in ("A0", "A1", "A2", "A3", "A4", "A5"):
        w, h = PAPER_MM[name]
        if abs(a - w) <= tol * w and abs(b - h) <= tol * h:
            return name
    return None


def scale_for_other_paper(text: str, width_pt: float, height_pt: float) -> tuple[float, str, str, int] | None:
    """(factor, printed-for paper, page paper, printed N) when the page prints its scale for ANOTHER paper size than
    the page is: '1:200 @ A1' on an A3 page -> (2.0, 'A1', 'A3', 200): the drawing on this file is at 1:400, and the
    page enlarged 2.0x is the sheet the 1:200 was written for. None when the papers agree, the page is not an ISO A
    sheet, or the page names two different papers."""
    hits = {(int(n), at.upper()) for n, at in _SCALE_AT.findall(text or "")}
    papers = {at for _, at in hits}
    if len(papers) != 1:
        return None
    named = papers.pop()
    page = page_paper(width_pt, height_pt)
    if page is None or page == named:
        return None
    f = PAPER_MM[named][1] / PAPER_MM[page][1]
    return f, named, page, min(n for n, _ in hits)
