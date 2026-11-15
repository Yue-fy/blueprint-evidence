"""Trust map (F6): one click overlays a sheet with HOW WELL WE READ each region.

green = clean vector lines / crisp ink / clear text / known scale; amber = partly read (faint or blurred lines, walls
that only close once small gaps are bridged, unsure OCR, scale only estimated); red = unreadable (scan noise, broken
walls, unreadable text). Empty paper is not scored. A one-line summary: '82 % of the plan is reliably readable'.

    trust_map(abs_path, page=0, ocr="auto", user_scale=None) -> dict   # tiles + summary + legend + scale, cached
    router: GET /feat/trust_map?path=<dataset path | upload/<id>/<name>>&page=0[&ocr=auto|off|force][&user_scale=1:200]
            GET /feat/trust_map/png?path=...&page=0   # the sheet with the overlay drawn (QA, screenshots, decks)

Signals (all deterministic, CPU, $0; ~1-4 s per sheet, no model call):
  * vector vs raster: PDF vector paths / real text per tile (PyMuPDF); image-only PDFs and pictures are raster
  * ink quality per tile on a ~2400 px grey render: paper grain (std of the paper), ink contrast against the local
    paper, edge sharpness (Sobel peak / contrast), speckle (isolated 1-3 px specks per ink pixel)
  * OCR word confidence per tile: PP-OCR scores from O1's cached reading (stoa.ocr; never run unless ocr=force)
  * wall closure: free space inside the building footprint that is enclosed by ink as drawn (green), only after
    bridging gaps of ~0.4 % of the sheet (amber), or that leaks to the sheet border even then (red)
  * scale certainty per page / view: printed 1:N on a PDF, a graphic bar that agrees (green); a printed ratio on a
    picture whose pixel size is unknown or a bar that disagrees (amber); none (amber cap, 'scale unknown')

A scale the VISITOR set (two clicks / a typed 1:N in the measure tool: the web sends user_scale=<label>; or one F7's
'learn from corrections' remembered for this file / set) counts as known: the tiles that were amber ONLY because the
scale was not certain (tile.clean and level amber, drawn striped in the UI) turn green.

Phone photos of a printed sheet (t4): the map reads the SAME copy the answers read: O1's deskewed sheet (perspective
undone, light evened: stoa.solvers.raster.photo.cached_deskew, the pipeline's normalize.prepare hook) and maps every
tile back onto the uploaded photo (tile.bbox = its box, tile.poly = its four corners in photo px). On the raw photo the
table around the sheet and the uneven light read as 'faint lines' (X1's Pagen photo: 79 % unreadable raw, 3 % on the
flat copy the readers use).

Honest limits: the signals say how CLEAN the reading material is, not whether an answer is right. Validation on X1's
scanned exam: experiments/f6/validate.py (correlation between region colour and answer accuracy).
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

VERSION = "t5"
ANALYSIS_SIDE = 2400        # long side of the grey render the pixel signals are measured on
NEGATIVE_NOTE = ("white lines on a dark background (a blueprint or a dark-mode export): measured as its inverse, dark "
                 "lines on light paper. Our readers are tuned for dark lines on white paper and were tested on few "
                 "prints like this one, so check its answers")
GRID = 24                   # tiles along the long side
GREEN, AMBER = 0.70, 0.42   # score thresholds
COLORS = {"green": "#1a9850", "amber": "#f0a30a", "red": "#d73027"}
LEGEND = [
    {"level": "green", "color": COLORS["green"], "label": "Reliably read",
     "detail": "clean vector lines or crisp ink, clear text, walls close, known scale"},
    {"level": "amber", "color": COLORS["amber"], "label": "Partly read",
     "detail": "faint or blurred lines, small wall gaps bridged, unsure text, scale only estimated"},
    {"level": "red", "color": COLORS["red"], "label": "Unreadable",
     "detail": "scan noise, broken walls, text we cannot read"},
]

_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.Lock()


# ----------------------------------------------------------------------------------------------------------------------
# loading


def _file_key(path: str, page: int, ocr: str) -> str:
    p = Path(path)
    st = p.stat()
    h = hashlib.sha1()
    with p.open("rb") as f:  # whole content, not name: uploads with the same name differ, and two copies of one
        for chunk in iter(lambda: f.read(1 << 22), b""):  # sheet can differ only far into the file (~0.1 s / 40 MB)
            h.update(chunk)
    tag = ""
    if ocr == "auto":  # the OCR lands in the background after an upload: a map made before it must not stay cached
        try:
            import stoa.ocr as O

            # scanned PDF: its page render; phone photo: its deskewed copy (the pipeline reads + OCRs that one)
            src = _page_render(path) if path.lower().endswith(".pdf") else (_photo_png(path) or path)
            tag = "" if src is None else ("ocr" if O.cached(src) else "no-ocr")
        except Exception:  # noqa: BLE001
            tag = "?"
    h.update(f"{st.st_size}|{page}|{ocr}|{tag}|{VERSION}".encode())
    return h.hexdigest()[:20]


def _photo(path: str):
    """O1's deskewed copy of a phone photo of a printed sheet (stoa.solvers.raster.photo.Deskewed, cached in a sidecar
    by content), the same one normalize.prepare hands to the readers; None for any other picture / when switched off."""
    if path.lower().endswith(".pdf"):
        return None
    try:
        from stoa.formats.normalize import _cache_dir  # noqa: PLC2701 (read-only reuse of P1's location rule)
        from stoa.solvers.raster.photo import cached_deskew

        return cached_deskew(path, _cache_dir(Path(path)))
    except Exception:  # noqa: BLE001 - no photo support: read the picture as it is
        return None


def _photo_png(path: str) -> str | None:
    d = _photo(path)
    return d.path if d is not None else None


PHOTO_EDGE = 0.008  # share of the long side blanked along the deskewed sheet's edge (paper edge / table sliver)


def _is_negative(g: np.ndarray) -> bool:  # KEEP THIS NAME: W5's web/server/normalize_upload.image() imports it (8bdf2310)
    """White lines on a dark sheet (a cyanotype blueprint, a dark-mode CAD export): the paper is the dark majority and
    the drawing is a thin BRIGHT tail. Every pixel signal here measures ink as darker than the local paper, so such a
    sheet read as it is gave 'faint lines, 100 % unreadable' on Finna's blueprints. A dim phone photo of a normal print
    has a dark median too, but its lines are darker still (dark tail, no bright tail)."""
    if g.size == 0:
        return False
    k = max(1, max(g.shape) // 600)  # ~600 px on the long side, the same step both ways
    s = g[::k, ::k]
    med = float(np.median(s))
    if med >= 140:
        return False
    hi = s > med + 40
    bright = float(np.mean(hi))
    darker = float(np.mean(s < med - 40))
    if not (bright >= 0.01 and bright > 2.5 * darker):
        return False
    # the bright part must be LINES: a light sheet / sign photographed on a black backdrop is one big bright block
    # (negative_scan.py on 1,500 dataset pictures: the Ayasofya sign photo fired without this)
    import cv2

    solid = cv2.erode(hi.astype(np.uint8), np.ones((5, 5), np.uint8))
    return float(solid.sum()) < 0.35 * float(hi.sum())


def _flatten_alpha(im):
    """A picture with a transparent background (CAD / vector exports) -> on white paper, as the viewer shows it. Its
    transparent pixels are usually black underneath: read as they are, the whole sheet was one block of 'ink' and the
    map came out empty (0 / 0 / 0 %)."""
    from PIL import Image

    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        if rgba.getextrema()[3][0] < 255:
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            bg.alpha_composite(rgba)
            return bg.convert("RGB"), True
    return im, False


class _Sheet:
    """The page as a grey image + how to map analysis px to the evidence space (pdf_pt unrotated / image px)."""

    def __init__(self, path: str, page: int):
        self.path, self.page = path, page
        self.is_pdf = path.lower().endswith(".pdf")
        self.vector_words: list = []  # (x0, y0, x1, y1) in analysis px, real text of a vector PDF
        self.vector_paths: list = []  # path bbox centres (x, y) in analysis px
        self.image_rects: list = []   # embedded raster images (analysis px)
        self.texts: list | None = None  # words / OCR lines of the sheet (None = not read)
        self.ocr_png: str | None = None  # a scanned PDF page: the upload pipeline's render whose OCR is cached
        self.to_poly = None  # phone photo: analysis px box -> its 4 corners in the photo (a tile is a quad there)
        self.n_pages = 1
        self.notes: list[str] = []
        self.negative = False  # white lines on a dark sheet: analysed inverted
        if self.is_pdf:
            self._load_pdf()
        else:
            self._load_image()

    def _load_image(self) -> None:
        from PIL import Image, ImageOps

        Image.MAX_IMAGE_PIXELS = None
        ph = _photo(self.path)
        with Image.open(ph.path if ph is not None else self.path) as im:
            im = ImageOps.exif_transpose(im)
            self.W, self.H = im.size
            s = min(1.0, ANALYSIS_SIDE / max(self.W, self.H))
            im, flat = _flatten_alpha(im)
            g = im.convert("L")
            size = (max(1, round(self.W * s)), max(1, round(self.H * s)))
            if s < 1.0:
                g = g.resize(size, Image.LANCZOS)
            g = np.asarray(g, dtype=np.uint8)
            neg = _is_negative(g)  # decided once on grey: the darkest channel below only changes how it is measured
            if neg and im.mode not in ("L", "1", "I", "I;16", "F"):
                # a cyanotype's pale lines differ from the blue paper mostly in RED (grey is mostly green: blueprint-3
                # lines only ~50 levels above the paper); the darkest channel shows the drawing as a viewer sees it
                from PIL import ImageChops

                r_, g_, b_ = im.convert("RGB").split()
                mn = ImageChops.darker(ImageChops.darker(r_, g_), b_)
                g = np.asarray(mn.resize(size, Image.LANCZOS) if s < 1.0 else mn, dtype=np.uint8)
        if flat:
            self.notes.append("transparent background: read on white paper, as the viewer shows it")
        self.s = s                      # analysis px per source px
        self.g = g
        self.space = "image_px"
        self.kind = "raster"
        self._invert_negative(neg)
        self.to_src = lambda b: [round(v / s, 1) for v in b]
        if ph is not None:
            from stoa.solvers.raster.photo import map_box, map_points

            Hm = ph.to_photo  # deskewed px -> photo px
            self.g = self.g.copy()
            m = max(2, round(PHOTO_EDGE * max(self.g.shape)))  # the warp's edge sliver of table / paper edge
            self.g[:m, :] = 255
            self.g[-m:, :] = 255
            self.g[:, :m] = 255
            self.g[:, -m:] = 255
            self.to_src = lambda b: [round(v, 1) for v in map_box(Hm, [v / s for v in b])]
            self.to_poly = lambda b: [[round(x, 1), round(y, 1)] for x, y in map_points(
                Hm, [[b[0] / s, b[1] / s], [b[2] / s, b[1] / s], [b[2] / s, b[3] / s], [b[0] / s, b[3] / s]])]
            self.ocr_png = ph.path  # the readers OCR the deskewed copy, so its text is cached under that file
            with Image.open(self.path) as im0:  # the browser shows the photo itself (EXIF-upright)
                self.W, self.H = ImageOps.exif_transpose(im0).size if im0.getexif().get(0x0112, 1) != 1 else im0.size
            self.notes.append("phone photo of a printed sheet: read as the sheet with its perspective undone and the "
                              "light evened (the same copy the answers are read from); the colours are mapped back "
                              "onto your photo")

    def _load_pdf(self) -> None:
        import fitz

        doc = fitz.open(self.path)
        try:
            self.n_pages = len(doc)
            pg = doc[min(max(0, self.page), len(doc) - 1)]
            rect = pg.rect  # displayed (rotated) page
            z = ANALYSIS_SIDE / max(rect.width, rect.height)
            pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY, alpha=False)
            self.g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
            self.W, self.H = rect.width, rect.height
            self.s = z
            der = pg.derotation_matrix
            rot = pg.rotation_matrix

            def to_src(b):
                r = fitz.Rect(b[0] / z, b[1] / z, b[2] / z, b[3] / z) * der
                r.normalize()
                return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]

            def to_an(r):  # unrotated page rect -> analysis px on the displayed page
                rr = fitz.Rect(r) * rot
                rr.normalize()
                return (rr.x0 * z, rr.y0 * z, rr.x1 * z, rr.y1 * z)

            self.to_src = to_src
            self.space = "pdf_pt"
            page_area = rect.width * rect.height
            img_area = 0.0
            for info in pg.get_image_info():
                b = to_an(info["bbox"])
                self.image_rects.append(b)
                img_area += max(0.0, (b[2] - b[0]) * (b[3] - b[1])) / (z * z)
            t0 = time.perf_counter()
            try:
                paths = pg.get_cdrawings()
            except Exception:  # noqa: BLE001
                paths = []
            for d in paths:
                r = d.get("rect")
                if r is None:
                    continue
                b = to_an(r)
                self.vector_paths.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
            words = pg.get_text("words")
            dt = time.perf_counter() - t0
            image_only = img_area >= 0.5 * page_area and len(self.vector_paths) < 200
            self.kind = "raster" if image_only else ("mixed" if self.image_rects else "vector")
            if not image_only:
                self.vector_words = [to_an(w[:4]) for w in words if w[4].strip()]
                self.texts = [w[4] for w in words if w[4].strip()]
            elif words:
                self.notes.append(f"scanned page with a text layer of {len(words)} words (OCR'd by the scanner?): "
                                  "read as a picture")
            neg = image_only and _is_negative(self.g)
            if neg:  # a scanned blueprint: its darkest channel, as for a picture
                rgb = pg.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csRGB, alpha=False)
                if (rgb.height, rgb.width) == self.g.shape:
                    a = np.frombuffer(rgb.samples, dtype=np.uint8).reshape(rgb.height, rgb.width, rgb.n)
                    self.g = a[..., :3].min(axis=2).copy()
            if image_only and self.page == 0:
                self.ocr_png = _page_render(self.path)
            if dt > 3:
                self.notes.append(f"vector content took {dt:.1f} s to list")
        finally:
            doc.close()
        self.vector_words = self._invisible_filtered(self.vector_words)
        if self.kind == "raster":  # a scanned blueprint inside a PDF
            self._invert_negative(neg)

    def _invert_negative(self, negative: bool) -> None:
        self.negative = bool(negative)
        if self.negative:
            self.g = (255 - self.g).astype(np.uint8)
            self.notes.append(NEGATIVE_NOTE)

    def _invisible_filtered(self, boxes: list) -> list:
        """Drop text with no ink under it (an invisible OCR layer over a picture is not vector text)."""
        if not boxes:
            return boxes
        g = self.g
        out = []
        for b in boxes:
            x0, y0, x1, y1 = (int(max(0, v)) for v in b)
            x1, y1 = min(g.shape[1], max(x1, x0 + 1)), min(g.shape[0], max(y1, y0 + 1))
            patch = g[y0:y1, x0:x1]
            if patch.size and int(patch.min()) < 160:
                out.append(b)
        return out


# ----------------------------------------------------------------------------------------------------------------------
# pixel signals


def _ink(g: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """(ink mask, darkness below the local paper (0-255), paper level). The local paper is a wide max filter, so
    uneven toner / phone-photo light does not read as ink."""
    import cv2

    k = max(15, (min(g.shape) // 60) | 1)
    bg = cv2.dilate(g, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    bg = cv2.blur(bg, (k, k))
    dark = cv2.subtract(bg, g)
    p995 = float(np.percentile(dark, 99.5)) if dark.size else 0.0
    thr = max(22.0, 0.15 * p995)  # light grey window / door lines are walls too (EFA: windows leak at 0.30)
    ink = dark > thr
    paper = float(np.median(bg))
    return ink, dark, paper


def _border_free(lab: np.ndarray, n: int) -> np.ndarray:
    border = np.zeros(n, bool)
    border[np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))] = True
    return border


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    import cv2

    n, lab = cv2.connectedComponents((~mask).astype(np.uint8), connectivity=4)
    border = _border_free(lab, n)
    border[0] = True  # label 0 = the mask itself
    return mask | ~border[lab]


def _closure(ink: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(inside footprint, enclosed as drawn, enclosed after bridging small gaps) masks of free space."""
    import cv2

    H, W = ink.shape
    u8 = ink.astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(u8, connectivity=8)
    big = max(6, int(0.008 * max(H, W)))  # drop text glyphs and specks: walls are long connected runs
    keep = (np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) >= big)
    # the sheet frame (and a scan's dark margin) spans the whole sheet: it would 'enclose' all the blank paper on a
    # clean sheet and leak it all on a scan whose frame line faded. Not a wall.
    keep &= ~((stats[:, cv2.CC_STAT_WIDTH] >= 0.85 * W) & (stats[:, cv2.CC_STAT_HEIGHT] >= 0.85 * H))
    keep[0] = False
    walls = keep[lab]
    fp_k = max(9, int(0.03 * max(H, W)) | 1)
    foot = cv2.morphologyEx(walls.astype(np.uint8), cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (fp_k, fp_k)))
    # fill holes of the footprint: free components that touch no border (a scan's dark margin can be ink, so a
    # flood fill from one corner would fill the building instead)
    foot = _fill_holes(foot.astype(bool))
    min_room = max(40, int(2e-4 * H * W))

    def enclosed(wm: np.ndarray) -> np.ndarray:
        free = (wm == 0).astype(np.uint8)
        n2, lab2, st2, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
        border = _border_free(lab2, n2)
        ok = (~border) & (st2[:, cv2.CC_STAT_AREA] >= min_room)
        ok[0] = False
        return ok[lab2]

    w0 = cv2.dilate(walls.astype(np.uint8), np.ones((2, 2), np.uint8))
    gap = max(5, int(0.010 * max(H, W)))  # ~ a door at the usual sheet scales (door / sheet side ~ 1/65 .. 1/130)
    w1 = cv2.morphologyEx(w0, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap, gap)))
    free_inside = foot & (w0 == 0)
    return free_inside, enclosed(w0) & free_inside, enclosed(w1) & free_inside, foot


_ROOM_RX = __import__("re").compile(
    r"\b(room|rm|kitchen|bed|bath|wc|toilet|office|hall|corridor|lobby|stor(e|age)|living|dining|class|lab|plant|"
    r"stair|lounge|entry|closet|garage|utility|laundry|shower|reception|meeting|kitchenette|porch|parlou?r|chamber|"
    r"kammer|k(u|ü)che|zimmer|huone|keitti(o|ö)|makuu|eteinen|sauna|wardrobe|ward|cloak|nursery|studio|sitting)\b|"
    r"\d\s*(m²|m2|sq\.?\s*ft|sf)\b", __import__("re").I)


def _page_render(pdf: str) -> str | None:
    """The PNG the upload pipeline made of a scanned PDF's page 0 (stoa.formats.normalize scan_png / render_png, next
    to the PDF or in its cache dir): its OCR reading is cached under that PNG."""
    try:
        from stoa.formats.normalize import _cache_dir  # noqa: PLC2701 (read-only reuse of P1's location rule)

        src = Path(pdf)
        dirs = [src.parent]
        if not os.access(src.parent, os.W_OK):
            dirs.append(_cache_dir(src))
        for d in dirs:
            for c in [d / f"{src.stem}.scan.png", *sorted(d.glob(f"{src.stem}.r[0-9]*.png"))]:
                if c.is_file():
                    return str(c)
    except Exception:  # noqa: BLE001
        return None
    return None


def _clip01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _ocr_items(path: str, sheet: _Sheet, ocr: str) -> list | None:
    """PP-OCR items [(box analysis px, score)] from O1's cache (or a fresh read when ocr='force'); None = not read."""
    if ocr == "off" or (sheet.is_pdf and not sheet.ocr_png):
        return None
    src = sheet.ocr_png or path
    try:
        import stoa.ocr as O

        if ocr != "force" and not O.cached(src):
            return None
        d = O.read(src)
    except Exception:  # noqa: BLE001
        return None
    # OCR boxes are in the read picture's px: the picture itself, or the render of a scanned PDF page
    s = sheet.s if not sheet.ocr_png else sheet.g.shape[1] / float(d.get("size", [sheet.g.shape[1]])[0] or 1)
    out = []
    for it in d.get("items") or []:
        b = it.get("box")
        if not b or not str(it.get("text", "")).strip():
            continue
        out.append(([v * s for v in b], float(it.get("score", 1.0)), str(it.get("text", ""))))
    return out


# ----------------------------------------------------------------------------------------------------------------------
# scale


def _scale(path: str, sheet: _Sheet) -> dict:
    """Scale certainty of the page: {status: known | estimated | conflict | unknown, text, ratio, bar_ratio, why}."""
    lines, bars, ratios = [], [], []
    try:
        import importlib

        V = importlib.import_module("stoa.sheet.views")  # stoa.sheet.views is also the name of a function

        layer = None
        src, pg = path, sheet.page
        if sheet.ocr_png:  # a scanned PDF page: read the render the upload pipeline OCR'd, like a picture
            src, pg = sheet.ocr_png, 0
        if not src.lower().endswith(".pdf"):
            try:
                import stoa.ocr as O

                if O.cached(src):
                    layer = O.text_layer(src)
            except Exception:  # noqa: BLE001
                layer = None
        r = V.page_reading(src, pg, layer)
        if r is not None:
            lines = r[0]
            ratios = V._parse_scales(" \n ".join(l.text for l in lines))  # noqa: SLF001 (read-only reuse)
            bars = V.scale_bars(lines)
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "why": f"scale not read ({type(e).__name__})", "ratio": None, "bar_ratio": None}
    if not lines and (not sheet.is_pdf or sheet.kind == "raster"):
        # a picture, or a scanned PDF page without a text layer: its text is read later (QA 20:21: do not say 'no
        # printed scale' before anything was read)
        return {"status": "unknown", "ratio": None, "bar_ratio": None,
                "why": "checking: the drawing's text is still being read", "pending": True}
    pic = not sheet.is_pdf or sheet.kind == "raster"  # pixels, not paper: a bar fixes px per metre
    ratio = None
    if ratios:
        from collections import Counter

        ratio = Counter(round(r["ratio"]) for r in ratios).most_common(1)[0][0]
    bar = bars[0]["ratio"] if bars else None
    try:  # VP's views: the scale on THIS page ('1:200 @ A1' printed on A3 = 1:400)
        vs = [v for v in V.views(path, sheet.page, layer) if v.kind in ("floor_plan", "site_plan") and v.page_ratio]
        if vs:
            ratio = round(vs[0].page_ratio)
            bar = next((v.bar_ratio for v in vs if v.bar_ratio), bar)
    except Exception:  # noqa: BLE001
        pass
    n_distinct = len({round(r["ratio"]) for r in ratios})
    if ratio and bar:
        if abs(bar - ratio) / ratio <= 0.03:
            return {"status": "known", "ratio": ratio, "bar_ratio": round(bar, 1),
                    "why": f"printed 1:{ratio} and the scale bar agree"}
        if not pic:
            return {"status": "conflict", "ratio": ratio, "bar_ratio": round(bar, 1),
                    "why": f"printed 1:{ratio} but the scale bar measures 1:{bar:.0f} (printed on another paper size?)"}
        return {"status": "known", "ratio": ratio, "bar_ratio": round(bar, 1),
                "why": f"printed 1:{ratio} and a graphic scale bar read on this picture (the bar fixes pixels per metre)"}
    if bar and pic:
        return {"status": "known", "ratio": ratio, "bar_ratio": round(bar, 1),
                "why": "graphic scale bar read on this picture (it fixes pixels per metre)"}
    if bar:
        return {"status": "known" if pic else "estimated", "ratio": ratio, "bar_ratio": round(bar, 1),
                "why": f"graphic scale bar 1:{bar:.0f}" + ("" if pic else " (no printed ratio)")}
    if ratio:
        if sheet.is_pdf and sheet.kind != "raster":
            more = f" ({n_distinct} different ratios printed: views differ)" if n_distinct > 1 else ""
            return {"status": "known", "ratio": ratio, "bar_ratio": None, "why": f"printed 1:{ratio} on a PDF{more}"}
        return {"status": "estimated", "ratio": ratio, "bar_ratio": None,
                "why": f"printed 1:{ratio}, but the picture's paper size / DPI is not known: sizes assume a paper"}
    return {"status": "unknown", "ratio": None, "bar_ratio": None,
            "why": "no printed scale or scale bar found: areas and lengths are estimates (set the scale)"}


# ----------------------------------------------------------------------------------------------------------------------
# the map


def _user_scale(label: str | None, rel: str | None, printed: dict, visitor: str | None = None) -> dict | None:
    """The scale the visitor set: the label the web sends (this page), else the one F7's corrections store remembers
    for this file (or set-wide from another sheet of the set when this sheet prints none). None = keep our reading."""
    import re

    label = (label or "").strip()[:40]
    if label:
        return {"status": "known", "ratio": None, "bar_ratio": None, "source": "user",
                "why": f"set by you ({label}): sizes use your scale"}
    if not rel:
        return None
    try:
        from stoa.features import corrections as C

        if not C.enabled():
            return None
        fid = C.file_id(rel)
        # F7 22:13: corrections are per visitor (a judge never sees another judge's scale); none -> visitor-less items
        items = [i for i in C.items_for(rel, visitor=visitor) if i.get("kind") == "scale"]
        own = [i for i in items if i.get("file") == fid]
        set_wide = [i for i in items if i.get("file") != fid and i.get("ratio") and i.get("scope") != "file"]
        it = own[-1] if own else (set_wide[-1] if set_wide and printed.get("status") == "unknown" else None)
        if not it:
            return None
        lab = f"1:{it['ratio']}" if it.get("ratio") else (f"{float(it['px_per_m']):.1f} px/m" if it.get("px_per_m") else "")
        if not lab:
            return None
        where = "" if own else f", set on {re.sub(r'^.*/', '', str(it.get('file')))}"
        return {"status": "known", "ratio": it.get("ratio"), "bar_ratio": None, "source": "user",
                "why": f"set by you ({lab}{where}; remembered): sizes use your scale"}
    except Exception:  # noqa: BLE001  (F7 off / not merged: our own reading stands)
        return None


def _analyse(path: str, page: int, ocr: str, scale_cap: bool = True, user_scale: str | None = None,
             rel: str | None = None, visitor: str | None = None, app_scale: str | None = None) -> dict:
    import cv2

    t0 = time.perf_counter()
    sh = _Sheet(path, page)
    g = sh.g
    H, W = g.shape
    ink, dark, paper = _ink(g)
    # speckle: isolated tiny ink blobs (scan dust, JPEG / toner grain)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    tiny = (stats[:, cv2.CC_STAT_AREA] <= 3)
    tiny[0] = False
    # only ISOLATED specks: the dot of an 'i' or a hatch dot sits next to other ink
    solid = cv2.dilate(((~tiny[lab]) & ink).astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
    if n > 1:
        cx = np.clip(cent[:, 0].round().astype(int), 0, W - 1)
        cy = np.clip(cent[:, 1].round().astype(int), 0, H - 1)
        iso = tiny & ~solid[cy, cx]
        speck_xy = cent[iso]
    else:
        speck_xy = np.zeros((0, 2))
    # sharpness: gradient peak at ink edges relative to the ink contrast
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(gx, gy)
    edge = cv2.dilate(ink.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~cv2.erode(
        ink.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    # paper grain: std of the paper away from any ink
    near_ink = cv2.dilate(ink.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
    free_in, enc0, enc1, foot = _closure(ink)
    # the same at a LOOSE ink threshold: walls of a faded scan close once faint ink counts (partly read), broken or
    # missing walls do not close at all
    loose = dark > 22
    if loose.sum() > 1.05 * ink.sum():
        free_l, encl0, encl1, _ = _closure(loose)
    else:
        free_l, encl0, encl1 = free_in, enc0, enc1
    ocr_items = _ocr_items(path, sh, ocr)
    if ocr_items is not None:
        sh.texts = list(sh.texts or []) + [t for _, _, t in ocr_items]
    # wall closure only means something on a plan with rooms: a site map, an elevation or a detail has none. Gate it on
    # room-like labels when the sheet's text is known (unknown text: keep it; it is a soft signal anyway)
    n_room_words = None if sh.texts is None else sum(1 for t in sh.texts if _ROOM_RX.search(t))
    rooms_plan = n_room_words is None or n_room_words >= 2
    if not rooms_plan:
        sh.notes.append("no room labels found on this sheet (site plan, elevation, detail?): wall closure not scored")
    scale = _scale(path, sh)
    mine = _user_scale(user_scale, rel, scale, visitor)
    if mine:
        mine["read"] = scale["why"].replace(" (set the scale)", "")  # what the sheet itself gave us, for the panel
        scale = mine
    elif app_scale and scale["status"] != "known" and (not sh.is_pdf or sh.kind == "raster") and sh.to_poly is None:
        # (not on a phone photo: its pixel size rests on the paper size we assumed, so the app's reading is an estimate)
        # the scale the app read off this PICTURE (the answer card's 'Scale 1:200 … the drawing's scale bar'): in image
        # space the app never guesses (no door-swing / 1:100 default), so the map says the same as the card (UX B5)
        r = scale.get("ratio")
        # an engineer's words when the sheet prints its ratio (UX 23:30 / captain 21:39: no '1 m = 51 px' on screen)
        scale = {"status": "known", "ratio": r, "bar_ratio": None, "source": "app", "read": scale["why"],
                 "why": (f"1:{r:g} printed, the same reading as the answer card" if r else
                         "read from the drawing (the same reading as the answer card)"), "app_label": app_scale[:40]}

    T = max(16, int(math.ceil(max(H, W) / GRID)))
    cols, rows = int(math.ceil(W / T)), int(math.ceil(H / T))
    vp = np.array(sh.vector_paths) if sh.vector_paths else np.zeros((0, 2))
    vw = np.array([((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in sh.vector_words]) if sh.vector_words else \
        np.zeros((0, 2))
    oc = np.array([((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, s) for b, s, _ in ocr_items]) if ocr_items else \
        np.zeros((0, 3))

    def in_tile(arr, x0, y0, x1, y1):
        if not len(arr):
            return arr
        m = (arr[:, 0] >= x0) & (arr[:, 0] < x1) & (arr[:, 1] >= y0) & (arr[:, 1] < y1)
        return arr[m]

    def in_image(x0, y0, x1, y1) -> bool:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in sh.image_rects)

    tiles = []
    for j in range(rows):
        for i in range(cols):
            x0, y0 = i * T, j * T
            x1, y1 = min(W, x0 + T), min(H, y0 + T)
            area = (x1 - x0) * (y1 - y0)
            sl = (slice(y0, y1), slice(x0, x1))
            ink_t = ink[sl]
            n_ink = int(ink_t.sum())
            ink_frac = n_ink / max(1, area)
            n_vec = len(in_tile(vp, x0, y0, x1, y1))
            n_vw = len(in_tile(vw, x0, y0, x1, y1))
            t = {"col": i, "row": j, "bbox": sh.to_src([x0, y0, x1, y1]), "ink": round(ink_frac, 4),
                 "plan": bool(foot[sl].mean() >= 0.3)}
            if sh.to_poly is not None:
                t["poly"] = sh.to_poly([x0, y0, x1, y1])
            fi = int(free_in[sl].sum())
            if ink_frac < 0.003 and n_vec == 0 and n_vw == 0 and fi < 0.5 * area:
                t.update(level="empty", score=None, reasons=[])
                tiles.append(t)
                continue
            sig, reasons = {}, []
            vector = sh.kind != "raster" and (n_vec + n_vw) > 0 and not in_image(x0, y0, x1, y1)
            # ink signals (also on vector tiles: a render of vector lines is crisp, so they pass)
            paper_px = g[sl][~near_ink[sl]].astype(np.float32)
            # robust spread (MAD): a few light-grey grid / hatch lines do not make clean paper 'noisy'
            grain = float(np.median(np.abs(paper_px - np.median(paper_px)))) * 1.4826 if paper_px.size > 50 else 0.0
            contrast = float(np.median(dark[sl][ink_t])) if n_ink else 0.0
            e = edge[sl]
            sharp = float(np.percentile(grad[sl][e], 90)) / (4.0 * max(contrast, 1.0)) if e.any() else 1.0
            n_sp = len(in_tile(speck_xy, x0, y0, x1, y1))
            speck = n_sp / max(1.0, n_ink / 50.0)  # specks per 50 ink px
            sig["grain"] = round(grain, 2)
            sig["contrast"] = round(contrast, 1)
            sig["sharp"] = round(sharp, 3)
            sig["speck"] = round(speck, 3)
            q_grain = _clip01((7.0 - grain) / 5.0)
            q_con = _clip01((contrast - 35.0) / 70.0) if n_ink else 1.0
            q_sharp = _clip01((sharp - 0.22) / 0.30)  # measured, not scored: at 2400 px a scan's blur is hidden
            q_speck = _clip01((0.6 - speck) / 0.5)
            q_ink = min(q_grain, q_con)
            if not vector:
                if q_grain < 0.5:
                    reasons.append("scan noise on the paper")
                if q_con < 0.5:
                    reasons.append("faint lines")
                if q_sharp < 0.5:
                    reasons.append("blurred lines")
                if q_speck < 0.5:
                    reasons.append("speckles")
            # walls
            q_wall = None
            if rooms_plan and fi > 0.15 * area:
                c0 = float(enc0[sl].sum()) / fi
                c1 = float(enc1[sl].sum()) / fi
                fl = max(1, int(free_l[sl].sum()))
                cl = max(float(encl0[sl].sum()), float(encl1[sl].sum())) / fl
                sig["closed"] = round(c0, 3)
                sig["closed_bridged"] = round(c1, 3)
                sig["closed_faint"] = round(cl, 3)
                # door-sized gaps are normal (bridged); walls that close only when faint ink counts are partly read; a
                # room that leaks even then is open plan or has broken / missing walls
                q_wall = max(c0, c1, 0.7 * cl, 0.15)
                if q_wall < 0.4:
                    # on vector lines nothing is broken: the outline simply has openings (QA 20:21: do not alarm)
                    reasons.append("room outlines open here (doors / openings)" if vector
                                   else "rooms not closed (broken or missing walls)")
                elif max(c0, c1) < 0.5:
                    reasons.append("walls close only where faint ink is counted")
            # text
            q_txt = None
            if vector and n_vw:
                sig["text"] = "vector"
            elif len(oc):
                ot = in_tile(oc, x0, y0, x1, y1)
                if len(ot):
                    ms = float(np.mean(ot[:, 2]))
                    low = float(np.mean(ot[:, 2] < 0.8))
                    sig["ocr_mean"], sig["ocr_low"], sig["ocr_n"] = round(ms, 3), round(low, 3), int(len(ot))
                    q_txt = _clip01((ms - 0.60) / 0.30) * (1.0 - 0.5 * low)
                    if q_txt < 0.5:
                        reasons.append(f"text hard to read (OCR {ms:.2f})")
            if vector:
                # exact lines and text: open room outlines on a vector sheet are doors / openings in the design, not a
                # reading problem (F6 20:55, 3 d25 runs: vector tiles amber only for open outlines 10/10 right vs vector
                # green 77/96), so they stay green and keep the note for 'Why is this green?'
                score = 0.85 if q_wall is None or q_wall >= 0.5 else 0.75
                reasons.insert(0, "vector lines" + (" and text" if n_vw else ""))
            else:
                # HARD signals (paper noise, faded ink, unreadable text) can make a region red; SOFT ones (isolated
                # specks, rooms that do not close) only amber: validation on S0's d25 stoa_like_dev showed 31 of 34
                # answers marked in tiles made red by closure / specks on clean pictures were right (F6 20:50)
                hard = [q_ink] + ([q_txt] if q_txt is not None else [])
                soft = [q_speck] + ([q_wall] if q_wall is not None else [])
                score = 0.6 * min(hard) + 0.4 * (sum(hard + soft) / len(hard + soft))
                # soft signals cap a tile at amber only where the ink / text is not clean too: on clean ink they
                # carried no accuracy information (d28: amber on clean sheets 55/62 right = green; d25: 31/34 answers
                # in tiles downgraded by closure / specks on clean pictures were right) and alarmed on clean renders
                # (UX 23:30: 'speckles', '32 % partly read' on Stoa's own DfE PNG)
                hard_clean = min(hard) >= 0.8
                if min(soft) < 0.5 and not hard_clean:
                    score = min(score, GREEN - 0.01)
                if hard_clean:
                    reasons[:] = ["room outlines open here (doors / openings)" if r.startswith("rooms not closed")
                                  else r for r in reasons if r != "speckles"]
            level = "green" if score >= GREEN else ("amber" if score >= AMBER else "red")
            clean_read = level == "green"  # reading quality alone, before the scale caps it
            if not scale_cap:
                pass
            elif level == "green" and scale["status"] in ("unknown", "conflict"):
                level = "amber"
                reasons.append("scale unknown: areas and lengths are estimates" if scale["status"] == "unknown"
                               else "scale ambiguous: printed ratio and bar disagree")
            elif level == "green" and scale["status"] == "estimated" and not vector:
                level = "amber"
                reasons.append("scale only estimated")
            if not reasons and level == "green":
                reasons.append("crisp lines")
            t.update(level=level, score=round(score, 3), reasons=reasons[:3], signals=sig, vector=vector,
                     clean=clean_read)
            tiles.append(t)

    scored = [t for t in tiles if t["level"] != "empty" and t["plan"]] or \
        [t for t in tiles if t["level"] != "empty"]  # 'the plan' = the building footprint, not the title block
    wsum = sum(max(t["ink"], 0.003) for t in scored) or 1.0

    def pct(level):  # share of the DRAWN content (tiles weighted by ink), not of blank paper
        return round(100.0 * sum(max(t["ink"], 0.003) for t in scored if t["level"] == level) / wsum)

    g_, a_, r_ = pct("green"), pct("amber"), pct("red")
    kind_txt = {"vector": "vector PDF", "mixed": "vector PDF with pictures", "raster": "picture / scan"}[sh.kind]
    clean_ = round(100.0 * sum(max(t["ink"], 0.003) for t in scored if t.get("clean")) / wsum)
    if clean_ > g_ + 5 and scale["status"] != "known":
        # the drawing itself reads well; only the scale keeps it amber: say that, not '0% reliably readable'
        line = (f"{clean_}% of the plan reads cleanly, but the scale is "
                + ("still being checked" if scale.get("pending") else
                   f"{'not known' if scale['status'] == 'unknown' else 'ambiguous' if scale['status'] == 'conflict' else 'only estimated'}"
                   ": sizes are estimates"))
        if r_:
            line += f" · {r_}% unreadable"
    elif not scored:  # nothing drawn (a blank page / the back of a sheet): not '0% of the plan is reliably readable'
        line = "No drawing found on this page: nothing to read"
    else:
        line = f"{g_}% of the plan is reliably readable"
        if a_ or r_:
            line += f" · {a_}% partly · {r_}% unreadable"
    from collections import Counter

    worst: Counter = Counter()
    for t in scored:
        if t["level"] != "green":
            for r in t["reasons"]:
                # the scale has its own line; 'speckles' is a soft signal that also fires on clean renders (UX 23:30)
                # 'room outlines open here' is information (doors / openings on clean ink), not a reading problem
                if not r.startswith(("vector", "scale", "room outlines open")) and r != "speckles":
                    worst[r] += max(t["ink"], 0.003) / wsum
    # only issues that touch >= 10 % of the drawn plan (QA 20:21: a few open rooms on an 88 %-green sheet are not news)
    return {
        "ok": True, "version": VERSION, "path": None, "page": page, "n_pages": sh.n_pages,
        "space": sh.space, "size": [round(sh.W, 2), round(sh.H, 2)], "kind": sh.kind,
        "photo": sh.to_poly is not None,
        "negative": sh.negative,
        "grid": {"cols": cols, "rows": rows, "tile": round(T / sh.s, 2)},
        "tiles": tiles,
        "summary": {"green": g_, "amber": a_, "red": r_, "clean": clean_, "line": line, "kind": kind_txt,
                    "scale": scale["why"], "main_issues": [k for k, v in worst.most_common(3) if v >= 0.10],
                    "ocr": "read" if ocr_items is not None else ("vector text" if sh.kind != "raster" else "not read")},
        "scale": scale, "legend": LEGEND, "notes": sh.notes,
        "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0, "api_calls": 0,
                 "wall_seconds": round(time.perf_counter() - t0, 3), "tier": "cheap"},
        "paper": round(paper, 1),
    }


def trust_map(path: str, page: int = 0, ocr: str = "auto", scale_cap: bool = True, user_scale: str | None = None,
              rel: str | None = None, visitor: str | None = None, app_scale: str | None = None) -> dict:
    """The trust map of one page of a drawing (absolute path). Cached per (content, page, ocr mode, the visitor's
    scale). rel = the browser's name for the file (data/... or upload/...): looks up a scale F7 remembered for it."""
    # the key names the visitor's scale whenever one could apply (set-wide ones only when the sheet prints none:
    # decided inside _analyse, deterministic per content + OCR state, so the key stays exact)
    mine = _user_scale(user_scale, rel, {"status": "unknown"}, visitor) if (user_scale or rel) else None
    app_scale = (app_scale or "").strip()[:40] or None
    key = (_file_key(path, page, ocr) + ("" if scale_cap else "|ink") + ("|u:" + mine["why"] if mine else "")
           + ("|a:" + app_scale if app_scale and not mine else ""))
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _warn(_CACHE[key])
    out = _analyse(path, page, ocr, scale_cap, user_scale, rel, visitor, app_scale)
    if ocr == "auto" and out["summary"]["ocr"] == "not read" and not path.lower().endswith(".pdf"):
        out["notes"].append("Text and scale are not included yet: this picture's text is read in the background "
                            "after upload (1-2 minutes for a scan). The map updates by itself once it is read.")
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return _warn(out)


# Sheet-level warning. Measured (F6 21:5x, experiments/f6/sheets.py, artifacts/F6/sheets_d28_pool.txt, sheets_d28sl_wild_d25scan.txt): with >= 10 % of
# the sheet's non-empty TILES red, answers on the sheet were right far less often than on the other sheets, and on such
# sheets the colour under an answer's own mark did not separate right from wrong (reasons.py, X1's scans: d28 green 7/10,
# red 25/40, amber 47/87, striped 14/40; d25 red 48 %,
# amber 33 %, striped 22 %). So the honest message is about the whole sheet. The ink-weighted summary share separates
# less (faint scan ink weighs little), hence the tile share.
# d28 (S0's gate runs: stoa-like dev a506 + wild dev 3031 + scan dev b685): warned sheets 102/212 = 48 % right, the rest
# 272/369 = 74 % (on d25 it was 32 % vs 74 %: the readers got better on scans; re-measure after big reader changes).
POOR_TILE_RED = 10.0
POOR_RIGHT, REST_RIGHT = "about half the time", "about 3 in 4"   # rounded from the measured rates above
# d32 (cycle 9, T1's calibrated confidence; experiments/f6/card_note.py + sheets.py on S0's d32b stoa-like dev 22f0, wild
# dev 17cc, wild sealed 5adf and X1's scan check 31f3): warned sheets 96 / 183 = 52 % right (scans 83 / 142 = 58 %) vs about
# 3 in 4 elsewhere — but the answers the card calls 'Very sure' (>= 0.9) on them were right 20 / 20 (with a red mark too:
# 29 / 29), the rest 76 / 163 = 47 %. So the warning names the exception, and the web adds no card line to a Very sure answer.
POOR_SURE = "answers we call Very sure held up even here"
# A PHONE PHOTO of a printed sheet gets its own measured note instead (t4): on the deskewed copy the readers use, the ink
# warning did not separate right from wrong (S0's d29 + d30 gate runs, X1's 9 photos: warned 11/16 right, not warned
# 12/29), while photos as a class were right 23 / 45 = 51 % against 165 / 248 = 67 % on the other pictures (d30 wild +
# stoa-like dev / sealed; d29 with scan_dev: 236 / 395 = 60 %). experiments/f6/photo_check.py, artifacts/F6/photo_check_*.txt
PHOTO_RIGHT, PHOTO_REST = "about half the time", "about 2 in 3"
PHOTO_WARNING = ("This is a phone photo of a printed sheet: we undid the perspective and evened the light before reading "
                 f"it. In our tests, answers on phone photos were right {PHOTO_RIGHT} (other pictures: {PHOTO_REST}). "
                 "Check each number's marks; for areas and lengths set the scale with two clicks. The PDF or a flat "
                 "scan reads better.")
# Not measured: none of our test sets has questions on a blueprint, so no 'right N in 10' here (F6 01:2x; negative_scan.py:
# the rule fires on 15 of 1,500 dataset pictures, 14 of them blueprints, and on 0 of 998 team test drawings)
NEGATIVE_WARNING = ("This is a blueprint (white lines on a dark background). We measured it inverted, as dark lines on "
                    "paper; few of our test drawings were like it, so we cannot say how often its answers are right. "
                    "Check each number's marks.")


def _warn(out: dict) -> dict:
    """Adds summary.red_tiles (share of the plan's non-empty tiles that are red) and summary.warning (None or a plain sentence
    for a sheet that reads poorly). Computed from the tiles, so cached maps get it too."""
    if not out.get("ok") or "warning" in out.get("summary", {}):
        return out
    # the PLAN's tiles (the building footprint), like the summary: a light-grey sheet frame read as 'faint lines' made a
    # 98 %-green traced vector PDF 37 % red over all tiles (catalogue dfe-…level00.vec6.pdf). Measured on d28: plan tiles
    # >= 10 % red 98/203 = 48 % right vs 276/378 = 73 % (all tiles: 48 vs 74 %), artifacts/F6/sheets_d28_pool_plan.txt
    used = [t for t in out.get("tiles", []) if t.get("level") != "empty"]
    used = [t for t in used if t.get("plan")] or used
    # INK-red only: a tile red together with unsure OCR text does not count, so the warning does not flip when a
    # picture's text lands 1-2 min after upload (Stoa's clean M-101A PNG: 8 % before the text, 13 % after = a false
    # alarm, its answers 27/37 = 73 % right on d28). d28: plan ink-red >= 10 % 96/198 = 48 % vs 278/383 = 73 %.
    red = 100.0 * sum(t["level"] == "red" and not any(r.startswith("text hard") for r in t.get("reasons", []))
                      for t in used) / max(len(used), 1)
    out["summary"]["red_tiles"] = round(red)
    out["summary"]["photo"] = bool(out.get("photo"))
    out["summary"]["negative"] = bool(out.get("negative"))
    out["summary"]["warning"] = PHOTO_WARNING if out.get("photo") else NEGATIVE_WARNING if out.get("negative") else (
        # no percentage here: the summary line above it already gives the (ink-weighted) unreadable share, and a second,
        # different number for 'hard to read' next to it confused (walk 21:5x: '33% unreadable' + '27% hard to read')
        f"This sheet reads poorly. In our tests, answers on sheets like this "
        f"were right {POOR_RIGHT} (other sheets: {REST_RIGHT}); {POOR_SURE}. Check the other numbers' marks; if "
        "you have the original PDF or a sharper scan, upload that instead."
        if used and red >= POOR_TILE_RED else None)
    return out


def _holds(t: dict, x: float, y: float) -> bool:
    """Is the point inside the tile? Its quad on a phone photo (tile.poly), else its box."""
    q = t.get("poly")
    if not q:
        b = t["bbox"]
        return b[0] <= x <= b[2] and b[1] <= y <= b[3]
    sign = 0.0
    for k in range(len(q)):
        (ax, ay), (bx, by) = q[k], q[(k + 1) % len(q)]
        c = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if c and sign and (c > 0) != (sign > 0):
            return False
        sign = sign or c
    return True


def level_at(m: dict, bbox: list[float]) -> str | None:
    """The trust level of the region under an evidence box (same space as the map): the worst scored tile it touches,
    weighted to the tile holding its centre. None when it touches only empty paper."""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rank = {"red": 0, "amber": 1, "green": 2}
    best = None
    tiles = [t for t in m["tiles"] if t["level"] != "empty"]
    for t in tiles:
        if _holds(t, cx, cy):
            return t["level"]
    for t in tiles:
        b = t["bbox"]
        if b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
            continue
        if best is None or rank[t["level"]] < rank[best]:
            best = t["level"]
    return best


# ----------------------------------------------------------------------------------------------------------------------
# overlay picture (QA, screenshots)


def overlay_png(path: str, page: int = 0, ocr: str = "auto", side: int = 1600, alpha: float = 0.38,
                user_scale: str | None = None) -> bytes:
    from PIL import Image, ImageDraw

    m = trust_map(path, page, ocr, user_scale=user_scale)
    sh = _Sheet(path, page)
    base = Image.fromarray(sh.g).convert("RGB")
    s = side / max(base.size)
    base = base.resize((max(1, round(base.width * s)), max(1, round(base.height * s))), Image.LANCZOS)
    over = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    a = int(255 * alpha)
    T = m["grid"]["tile"] * sh.s * s
    for t in m["tiles"]:
        if t["level"] == "empty":
            continue
        c = COLORS[t["level"]]
        rgb = tuple(int(c[k:k + 2], 16) for k in (1, 3, 5))
        x0, y0 = t["col"] * T, t["row"] * T
        if t["level"] == "amber" and t.get("clean"):  # read cleanly, amber only for the scale: striped (as in the UI)
            d.rectangle([x0, y0, x0 + T - 1, y0 + T - 1], fill=rgb + (a // 4,))
            step = 7
            for k in range(-int(T), int(T), step):  # 45° stripes clipped to the tile
                p0 = (x0 + max(0, k), y0 + max(0, -k))
                p1 = (x0 + min(T - 1, T - 1 + k), y0 + min(T - 1, T - 1 - k))
                if p0[0] <= p1[0]:
                    d.line([p0, p1], fill=rgb + (min(255, a + 15),), width=2)
            continue
        d.rectangle([x0, y0, x0 + T - 1, y0 + T - 1], fill=rgb + (a,))
    img = Image.alpha_composite(base.convert("RGBA"), over)
    bar = Image.new("RGBA", (img.width, 34), (255, 255, 255, 255))
    db = ImageDraw.Draw(bar)
    db.text((8, 10), m["summary"]["line"] + "  |  scale: " + m["summary"]["scale"], fill=(0, 0, 0, 255))
    out = Image.new("RGBA", (img.width, img.height + 34), (255, 255, 255, 255))
    out.paste(bar, (0, 0))
    out.paste(img, (0, 34))
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------------------------------
# HTTP


def resolve(path: str) -> str:
    """Absolute file for a dataset path ('data/...') or an upload's virtual path ('upload/<id>/[style/]<name>', the
    web middleware's STOA_UPLOADS layout). Raises ValueError / FileNotFoundError."""
    if not path or ".." in Path(path).parts or path.startswith("/"):
        raise ValueError("path must be relative (data/... or upload/<id>/<name>) without '..'")
    if path.startswith("upload/"):
        import re

        m = re.match(r"^upload/([0-9a-f]{12})/(?:([a-z0-9_]+)/)?([^/\\]+)$", path)
        if not m or m.group(3).startswith("."):
            raise ValueError("bad upload path")
        # the web middleware's STOA_UPLOADS (default <os.tmpdir()>/stoa-uploads); the API may run with another TMPDIR
        roots = [os.environ.get("STOA_UPLOADS"), Path(tempfile.gettempdir()) / "stoa-uploads", "/tmp/stoa-uploads"]
        rel = Path(m.group(1)) / ("data/" + m.group(2) if m.group(2) else "") / m.group(3)
        cands = [Path(r) / rel for r in roots if r]
        p = next((c for c in cands if c.is_file()), cands[0])
    else:
        from stoa.data import abspath, data_root

        p = abspath(path).resolve()
        if data_root().resolve() not in p.parents:
            raise ValueError("path outside the dataset")
    if not p.is_file():
        raise FileNotFoundError(path)
    return str(p)


def _router():
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import Response

    r = APIRouter(prefix="/feat/trust_map", tags=["feature: trust map"])

    def _abs(path: str) -> str:
        try:
            return resolve(path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {path}") from e

    @r.get("")
    def get_map(path: str = Query(...), page: int = 0, ocr: str = "auto", user_scale: str | None = None,
                visitor: str | None = None, app_scale: str | None = None) -> dict:
        ab = _abs(path)
        if ocr not in ("auto", "off", "force"):
            raise HTTPException(400, "ocr must be auto | off | force")
        try:
            v = visitor if visitor and len(visitor) <= 64 else None
            m = dict(trust_map(ab, page, ocr, user_scale=user_scale, rel=path, visitor=v, app_scale=app_scale))
        except Exception as e:  # noqa: BLE001  fail closed: the UI shows nothing
            return {"ok": False, "why": f"trust map failed: {type(e).__name__}: {e}"[:300], "path": path}
        m["path"] = path
        return m

    @r.get("/png")
    def get_png(path: str = Query(...), page: int = 0, ocr: str = "auto", side: int = 1600, user_scale: str | None = None):
        ab = _abs(path)
        try:
            return Response(overlay_png(ab, page, ocr, side=max(400, min(side, 4000)), user_scale=user_scale),
                            media_type="image/png")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"trust map failed: {type(e).__name__}") from e

    return r


try:
    router = _router()
except Exception:  # noqa: BLE001  (no fastapi in a bare env: the library part still works)
    router = None


if __name__ == "__main__":  # python -m stoa.features.trust_map FILE [page] [out.png]
    import json
    import sys

    f = sys.argv[1]
    pg = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    res = trust_map(f, pg)
    print(json.dumps({k: v for k, v in res.items() if k != "tiles"}, indent=1))
    if len(sys.argv) > 3:
        Path(sys.argv[3]).write_bytes(overlay_png(f, pg))
