"""F1 — region ask: a question scoped to a box / lasso the visitor drew on the drawing.

The visitor drags a box or a lasso on the 2D drawing; a popover offers questions about that area ('How many doors in
this area?', 'Total area of the rooms in here', free text). The region is sent with the question:

    region = {"page": 0, "space": "pdf_pt" | "image_px", "polygon": [[x, y], ...]}   # >= 3 points, or 2 = box corners

in the CONTRACT evidence space of the uploaded file (pdf_pt = PyMuPDF's unrotated page points; image_px = the image
as stored). Nothing in the answer logic is forked: the normal solver answers, on a copy of the drawing or per room.

Two ways to scope, picked by the question:
  * labels  (areas: 'total area of the rooms in here', 'area of this room') — the room labels printed INSIDE the
             region are read from the text layer; each room is asked on the FULL sheet ('What is the area of the
             Kitchen?'), and a reading counts only when its own evidence (room outline / box) lies in the region (a room
             with the same name elsewhere is rejected and said so). The sum + every term in the trace.
  * mask    (counts, lengths, free text) — a copy of the sheet with everything outside the region removed (PDF:
             text, vectors, image pixels redacted; image: pixels set to white), SAME page size and coordinates, so the
             evidence needs no mapping. The printed scale notes (and the area right around them, where scale bars sit)
             are kept, so the readers find the same scale as on the whole sheet. Image text layers already read on the
             whole sheet are carried over (filtered to what is kept): no second OCR / VLM pass.

    from stoa.features.region import with_region
    ans = with_region(abs_path, question, region, lambda p, q: solver.solve(p, q))   # region None/off -> solve as is

Feature flag: STOA_FEATURES (comma list) must contain 'region' (or 'all'); off -> `with_region` is exactly
`solve(path, question)`. Any failure while scoping -> the whole-sheet answer, capped at confidence 0.2, saying so.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FEATURE = "region"
VERSION = 1  # in the masked copy's name: a fix to the masking never reuses an older copy

# a printed scale note or a paper-size note: kept on the masked copy so readers find the same scale
_SCALE_TXT = re.compile(
    r"(?i)\b1\s*:\s*\d{1,5}\b|\bscale\b|\bma(?:ß|ss)stab\b|\bmittakaava\b|\béchelle\b|\bskala\b|"
    r"\d+(?:\s*/\s*\d+)?\s*\"\s*=\s*\d+\s*'|\bfull\s+\d+\s*x\s*\d+\b|\b(?:ANSI|ARCH)\s*[A-E]\b|@\s*A[0-4]\b|\bA[0-4]\b\s*(?:sheet|size)")

# 'in this area' phrases, removed from the question asked on the masked copy (the copy IS the area)
_HERE = re.compile(
    r"(?i)\s*\b(?:(?:with)?in|inside|on|of|for|from)\s+(?:this|the|that)\s+(?:selected\s+|marked\s+|drawn\s+|highlighted\s+)?"
    r"(?:area|region|selection|box|part|zone|lasso|bit)\b|\s*\b(?:in|inside)\s+here\b|\s*\bhere\b(?=\s*[?.!]?\s*$)|"
    r"\s*\b(?:in|inside)\s+(?:my|the)\s+selection\b")
_AREA_Q = re.compile(r"(?i)\b(?:area|square\s*(?:feet|foot|met(?:re|er)s?)|sq\.?\s*(?:ft|m)|m2|m²|ft2|sqm|sqft|how\s+big|"
                     r"how\s+large|size\s+of\s+(?:the\s+)?rooms?|floor\s+space)\b")
_ROOMS_Q = re.compile(r"(?i)\brooms?\b|\bspaces?\b|\boffices?\b|\bclassrooms?\b|\bthis\b|\bhere\b|\btotal\b|\ball\b")
_COUNT_Q = re.compile(r"(?i)^\s*(?:how\s+many|count|number\s+of)\b")
_LEN_Q = re.compile(r"(?i)\b(?:length|long|perimeter|skirting|baseboard|linear\s+met|how\s+wide|width|distance)\b")


def enabled() -> bool:
    on = {s.strip().lower() for s in os.environ.get("STOA_FEATURES", "").split(",") if s.strip()}
    return FEATURE in on or "all" in on


# --------------------------------------------------------------------------------------------------- geometry

@dataclass
class Region:
    page: int
    space: str  # pdf_pt | image_px
    polygon: list[list[float]]  # closed outline (box -> its 4 corners)
    notes: list[str] = field(default_factory=list)
    snap: bool = True  # outline measures: move the drawn sides onto the nearest wall faces (False = exactly as drawn)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = [p[0] for p in self.polygon], [p[1] for p in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def is_box(self) -> bool:
        x0, y0, x1, y1 = self.bbox
        return len(self.polygon) == 4 and all(
            (abs(x - x0) < 1e-6 or abs(x - x1) < 1e-6) and (abs(y - y0) < 1e-6 or abs(y - y1) < 1e-6)
            for x, y in self.polygon)

    def contains(self, x: float, y: float) -> bool:
        return point_in_polygon(x, y, self.polygon)

    def describe(self) -> str:
        x0, y0, x1, y1 = self.bbox
        u = "pt" if self.space == "pdf_pt" else "px"
        shape = "box" if self.is_box else f"lasso ({len(self.polygon)} points)"
        return f"the {shape} you drew on page {self.page + 1}: x {x0:.0f}-{x1:.0f}, y {y0:.0f}-{y1:.0f} {u}"


def parse_region(r) -> Region | None:
    """dict / JSON string -> Region, or None when it is not a usable region (fail closed: no scoping)."""
    if r is None or r == "":
        return None
    if isinstance(r, str):
        r = json.loads(r)
    if isinstance(r, Region):
        return r
    pts = r.get("polygon") or r.get("points")
    if not pts and r.get("bbox"):
        x0, y0, x1, y1 = (float(v) for v in r["bbox"])
        pts = [[x0, y0], [x1, y1]]
    pts = [[float(p[0]), float(p[1])] for p in (pts or [])]
    if len(pts) == 2:  # box corners
        (ax, ay), (bx, by) = pts
        x0, x1, y0, y1 = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        pts = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    if len(pts) < 3:
        return None
    space = str(r.get("space") or "pdf_pt")
    if space not in ("pdf_pt", "image_px"):
        return None
    reg = Region(page=int(r.get("page") or 0), space=space, polygon=pts, snap=r.get("snap", True) not in (False, 0, "0"))
    x0, y0, x1, y1 = reg.bbox
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return reg


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    inside, n = False, len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / ((by - ay) or 1e-12) + ax:
            inside = not inside
    return inside


def box_share_inside(box, reg: Region, steps: int = 6) -> float:
    """Share of a box's sample points (steps x steps grid) inside the region."""
    x0, y0, x1, y1 = box
    hit = tot = 0
    for i in range(steps):
        for j in range(steps):
            x = x0 + (x1 - x0) * (i + 0.5) / steps
            y = y0 + (y1 - y0) * (j + 0.5) / steps
            tot += 1
            hit += reg.contains(x, y)
    return hit / tot if tot else 0.0


def keep_rects(reg: Region, strips: int = 12) -> list[tuple[float, float, float, float]]:
    """The region as axis-aligned rectangles: a box is itself; a lasso -> horizontal strips (each strip's inside
    x-range), good enough for redaction on a vector page."""
    if reg.is_box:
        return [reg.bbox]
    x0, y0, x1, y1 = reg.bbox
    out = []
    h = (y1 - y0) / strips
    for k in range(strips):
        ya, yb = y0 + k * h, y0 + (k + 1) * h
        xs = []
        for y in (ya + 0.01 * h, (ya + yb) / 2, yb - 0.01 * h):
            n = len(reg.polygon)
            for i in range(n):
                (ax, ay), (bx, by) = reg.polygon[i], reg.polygon[(i + 1) % n]
                if (ay > y) != (by > y):
                    xs.append(ax + (bx - ax) * (y - ay) / (by - ay))
        if len(xs) >= 2:
            out.append((min(xs), ya, max(xs), yb))
    return out or [reg.bbox]


def complement(page: tuple, keeps: list[tuple]) -> list[tuple]:
    """Rectangles covering page minus the union of `keeps` (grid on all edges, cells merged along rows)."""
    px0, py0, px1, py1 = page
    ks = [(max(px0, a), max(py0, b), min(px1, c), min(py1, d)) for a, b, c, d in keeps]
    ks = [k for k in ks if k[2] > k[0] and k[3] > k[1]]
    xs = sorted({px0, px1, *[v for k in ks for v in (k[0], k[2])]})
    ys = sorted({py0, py1, *[v for k in ks for v in (k[1], k[3])]})
    out = []
    for j in range(len(ys) - 1):
        ya, yb = ys[j], ys[j + 1]
        run = None
        for i in range(len(xs) - 1):
            xa, xb = xs[i], xs[i + 1]
            cx, cy = (xa + xb) / 2, (ya + yb) / 2
            kept = any(k[0] <= cx <= k[2] and k[1] <= cy <= k[3] for k in ks)
            if not kept:
                run = (run[0], ya, xb, yb) if run else (xa, ya, xb, yb)
            elif run:
                out.append(run)
                run = None
        if run:
            out.append(run)
    # merge vertically identical runs (fewer, larger rects: more vector paths fully covered by one rect)
    merged: list[list[float]] = []
    for r in sorted(out, key=lambda r: (r[0], r[2], r[1])):
        if merged and abs(merged[-1][0] - r[0]) < 1e-6 and abs(merged[-1][2] - r[2]) < 1e-6 and \
                abs(merged[-1][3] - r[1]) < 1e-6:
            merged[-1][3] = r[3]
        else:
            merged.append(list(r))
    return [tuple(m) for m in merged]


def _grow(box, fx: float, fy: float, page=None):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    b = (x0 - w * fx, y0 - h * fy, x1 + w * fx, y1 + h * fy)
    if page:
        b = (max(page[0], b[0]), max(page[1], b[1]), min(page[2], b[2]), min(page[3], b[3]))
    return b


def _scale_keep(box, page) -> tuple:
    """A printed scale note + the area around it, where the graphic scale bar usually sits."""
    x0, y0, x1, y1 = box
    pad = 6 * min(y1 - y0, x1 - x0)  # 6 line heights (vertical text on a rotated page: its SHORT side)
    return (max(page[0], x0 - pad), max(page[1], y0 - pad), min(page[2], x1 + pad), min(page[3], y1 + pad))


# --------------------------------------------------------------------------------------------------- masked copy

def _cache_dir(src: Path) -> Path:
    """Masked copies live in one cache folder, never next to the upload (the upload solver reads files in the upload's
    folder as its siblings: a sheet's IFC model) nor in a dataset folder. STOA_REGION_CACHE overrides."""
    d = Path(os.environ.get("STOA_REGION_CACHE") or Path(os.environ.get("STOA_CACHE", tempfile.gettempdir())) / "stoa-region")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(src: Path, reg: Region) -> str:
    st = src.stat()
    raw = json.dumps([VERSION, str(src.resolve()), st.st_size, st.st_mtime_ns, reg.page, reg.space,
                      [[round(x, 1), round(y, 1)] for x, y in reg.polygon]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


@dataclass
class Masked:
    path: str  # the copy to solve
    page: int  # page of the ORIGINAL it shows
    kept_scale: list[str] = field(default_factory=list)  # printed scale notes kept
    n_text_in: int = 0  # text items inside the region (0 on an empty area: say so)
    notes: list[str] = field(default_factory=list)
    offset: tuple[float, float] = (0.0, 0.0)  # image crop: original px = copy px + offset


def _pdf_lines(pg) -> list[tuple[tuple, str]]:
    out = []
    for b in pg.get_text("dict").get("blocks", []):
        for ln in b.get("lines", []):
            t = " ".join(s.get("text", "") for s in ln.get("spans", [])).strip()
            if t:
                out.append((tuple(ln["bbox"]), t))
    return out


def mask_pdf(src: Path, reg: Region) -> Masked:
    """One-page PDF: the region's page with text, vector paths and image pixels outside the region removed (the
    printed scale notes and the area around them kept). Same page size, rotation and coordinates as the original page,
    so evidence on page 0 of the copy = evidence on page reg.page of the original."""
    import pymupdf

    out = _cache_dir(src) / f"{src.stem[:40]}.region-{_key(src, reg)}.pdf"
    doc = pymupdf.open(src)
    try:
        if not 0 <= reg.page < doc.page_count:
            raise ValueError(f"page {reg.page + 1} is not in this PDF ({doc.page_count} pages)")
        if doc.page_count > 1:
            doc.select([reg.page])
        pg = doc[0]
        rot = pg.rotation
        if rot:
            pg.set_rotation(0)  # unrotated = the CONTRACT's pdf_pt space; restored below
        R = pg.rect
        page = (R.x0, R.y0, R.x1, R.y1)
        lines = _pdf_lines(pg)
        keeps = keep_rects(reg)
        n_in = sum(1 for b, _ in lines if reg.contains((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
        scale = [(b, t) for b, t in lines if _SCALE_TXT.search(t) and not reg.contains((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)]
        keeps += [_scale_keep(b, page) for b, _ in scale[:8]]
        if not out.exists():
            # pass 1, graphics + image pixels: four OVERLAPPING bands around the region's box (a path fully outside
            # the box lies in one band; with a grid of disjoint cells a door arc across two cells survived: A-101
            # module 02 doors 9 -> 11) + the strips of the box outside a lasso
            x0, y0, x1, y1 = reg.bbox
            bands = [(page[0], page[1], page[2], y0), (page[0], y1, page[2], page[3]),
                     (page[0], page[1], x0, page[3]), (x1, page[1], page[2], page[3])]
            if not reg.is_box:
                bands += complement(reg.bbox, keep_rects(reg))
            for r in bands:
                if r[2] - r[0] > 0.5 and r[3] - r[1] > 0.5:
                    pg.add_redact_annot(pymupdf.Rect(*r))
            pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                text=pymupdf.PDF_REDACT_TEXT_NONE)
            # pass 2, text: everything outside the region except the printed scale notes
            for r in complement(page, keeps):
                pg.add_redact_annot(pymupdf.Rect(*r))
            pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            if rot:
                pg.set_rotation(rot)
            tmp = out.with_name(f".{out.name}.tmp")
            doc.save(tmp, garbage=3, deflate=True)
            os.replace(tmp, out)
    finally:
        doc.close()
    return Masked(path=str(out), page=reg.page, kept_scale=[t for _, t in scale[:8]], n_text_in=n_in)


def _image_layer(src: Path):
    """The image's text layer if one was already read (VLM or OCR cache), else None (never starts a new reading)."""
    try:
        from stoa.solvers.raster import textlayer as T

        fs = T._disk_key(str(src), 1100, "local")
        if fs.exists():
            return json.loads(fs.read_text()), fs, "vlm"
    except Exception:  # noqa: BLE001
        pass
    try:
        from stoa import ocr

        os_ = ocr._key(str(src))
        if os_.exists():
            return json.loads(os_.read_text()), os_, "ocr"
    except Exception:  # noqa: BLE001
        pass
    return None


def _carry_layer(src: Path, dst: Path, keep: Callable[[list], bool], offset=(0, 0), size=None) -> list[str]:
    """Whole-sheet text layers (VLM, OCR) saved as the masked copy's own layers, only the items that stay visible."""
    done = []
    try:
        from stoa.solvers.raster import textlayer as T

        fs, fd = T._disk_key(str(src), 1100, "local"), T._disk_key(str(dst), 1100, "local")
        if fs.exists() and not fd.exists():
            d = json.loads(fs.read_text())
            d["path"] = str(dst)
            d["items"] = [_shift(it, offset) for it in d.get("items", []) if keep(it["box"])]
            if size:
                d["size"] = list(size)
            d["notes"] = [*d.get("notes", []), "carried over from the whole sheet's reading (items in the region)"]
            tmp = fd.with_name(f".{fd.name}.tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, fd)
            done.append("vlm")
    except Exception:  # noqa: BLE001
        pass
    try:
        from stoa import ocr

        os_, od = ocr._key(str(src)), ocr._key(str(dst))
        if os_.exists() and not od.exists():
            d = json.loads(os_.read_text())
            d["items"] = [_shift(it, offset) for it in d.get("items", []) if keep(it["box"])]
            if size:
                d["size"] = list(size)
            tmp = od.with_name(f".{od.name}.tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, od)
            done.append("ocr")
    except Exception:  # noqa: BLE001
        pass
    return done


def _shift(it: dict, offset) -> dict:
    if not offset or offset == (0, 0):
        return it
    b = it["box"]
    return {**it, "box": [b[0] - offset[0], b[1] - offset[1], b[2] - offset[0], b[3] - offset[1]]}


def mask_image(src: Path, reg: Region, crop: bool = False) -> Masked:
    """PNG copy of the same size: pixels outside the region white, the printed scale notes (and around them) kept,
    DPI / PNG text tags kept (the paper-size scale path reads them).
    crop=True (counts on images): only the region's box (+ a small margin), outside the polygon white; a mostly-white
    full-size copy went to the whole-image VLM count (A-101 PNG module 01: 10 for 4 doors). Evidence = copy px + offset."""
    from PIL import Image, ImageDraw
    from PIL.PngImagePlugin import PngInfo

    out = _cache_dir(src) / f"{src.stem[:40]}.region-{_key(src, reg)}{'-crop' if crop else ''}.png"
    lay = _image_layer(src)
    items = (lay[0].get("items") or []) if lay else []
    with Image.open(src) as im:
        W, H = im.size
        page = (0.0, 0.0, float(W), float(H))
        scale = [(tuple(it["box"]), it.get("text", "")) for it in items
                 if _SCALE_TXT.search(it.get("text", "")) and not reg.contains(*_centre(it["box"]))]
        keeps = [] if crop else [_scale_keep(b, page) for b, _ in scale[:8]]
        n_in = sum(1 for it in items if reg.contains(*_centre(it["box"])))
        x0, y0, x1, y1 = reg.bbox
        m_px = max(16.0, 0.03 * max(x1 - x0, y1 - y0))
        cb = (int(max(0, x0 - m_px)), int(max(0, y0 - m_px)), int(min(W, x1 + m_px + 1)), int(min(H, y1 + m_px + 1))) \
            if crop else (0, 0, W, H)
        if not out.exists():
            rgb = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
            mask = Image.new("L", (W, H), 0)
            dr = ImageDraw.Draw(mask)
            dr.polygon([tuple(p) for p in reg.polygon], fill=255)
            if crop and reg.is_box:  # a box keeps its margin (a door on the drawn edge stays whole)
                dr.rectangle(cb, fill=255)
            for k in keeps:
                dr.rectangle(k, fill=255)
            white = Image.new(rgb.mode, (W, H), 255 if rgb.mode == "L" else (255, 255, 255))
            res = Image.composite(rgb, white, mask)
            if crop:
                res = res.crop(cb)
            info = PngInfo()
            for k, v in im.info.items():
                if isinstance(v, str) and k not in ("dpi",):
                    info.add_text(k, v)
            kw = {"pnginfo": info}
            if im.info.get("dpi"):
                kw["dpi"] = tuple(float(v) for v in im.info["dpi"])
            tmp = out.with_name(f".{out.name}.tmp.png")
            res.save(tmp, **kw)
            os.replace(tmp, out)

    def visible(box) -> bool:
        cx, cy = _centre(box)
        return reg.contains(cx, cy) or any(k[0] <= cx <= k[2] and k[1] <= cy <= k[3] for k in keeps)

    carried = _carry_layer(src, out, visible, offset=(cb[0], cb[1]), size=(cb[2] - cb[0], cb[3] - cb[1]))
    m = Masked(path=str(out), page=0, kept_scale=[t for _, t in scale[:8]] if not crop else [], n_text_in=n_in,
               offset=(float(cb[0]), float(cb[1])))
    if carried:
        m.notes.append(f"text layer carried from the whole sheet ({', '.join(carried)})")
    if crop:
        m.notes.append(f"counted on a crop of the image: x {cb[0]}-{cb[2]}, y {cb[1]}-{cb[3]} px")
    return m


def _centre(b) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def masked_copy(abs_path: str, reg: Region, crop: bool = False) -> Masked:
    src = Path(abs_path)
    ext = src.suffix.lower()
    if ext == ".pdf":
        if reg.space != "pdf_pt":
            raise ValueError("a region on a PDF must be in pdf_pt")
        return mask_pdf(src, reg)
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"):
        if reg.space != "image_px":
            raise ValueError("a region on an image must be in image_px")
        return mask_image(src, reg, crop=crop)
    raise ValueError(f"region questions work on PDF and image sheets, not {ext or 'this file'}")


# --------------------------------------------------------------------------------------------------- room labels

_NOT_ROOM = re.compile(r"(?i)^(?:[\d.,\s'\"\-/x×=+]+(?:mm|cm|m|ft|in|sf|sq\.?\s*ft|m2|m²)?|[A-Z]?\d+[A-Z]?|"
                       r"(?:dn|up|wc\d*|n|s|e|w|ref|typ\.?|sim\.?|cl|el\.?|ffl|sfl)|.*\b1\s*:\s*\d+.*|"
                       r"(?:existing|new|proposed|demolish(?:ed)?|note|notes|see|refer|provide|install|verify|align|"
                       r"coordinate|connect|mount|typical|match|extend|remove|relocate)\b.*)$")
_AREA_TXT = re.compile(r"(?i)\b\d[\d,.]*\s*(?:m2|m²|sq\.?\s*m|sf|sq\.?\s*ft|ft2)\b")


def _pdf_phrases(pg) -> list[tuple[tuple, str]]:
    """Words joined into phrases in READING orientation (a /Rotate 90 page prints vertically in pdf_pt space; one
    PyMuPDF line can run through several rooms' labels on one baseline): words on one baseline with a gap under ~1.2
    letter heights. Boxes back in unrotated pdf_pt."""
    import pymupdf

    M = pg.rotation_matrix
    inv = ~M
    ws = []
    for w in pg.get_text("words"):
        r = pymupdf.Rect(w[:4]) * M
        ws.append([r.x0, r.y0, r.x1, r.y1, w[4]])
    ws.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[list] = []
    for w in ws:
        h = w[3] - w[1]
        cy = (w[1] + w[3]) / 2
        for ph in reversed(rows[-40:]):
            ph_h = ph[3] - ph[1]
            if abs((ph[1] + ph[3]) / 2 - cy) <= 0.35 * max(h, ph_h) and 0 <= w[0] - ph[2] <= 1.2 * max(h, ph_h):
                ph[2], ph[1], ph[3] = max(ph[2], w[2]), min(ph[1], w[1]), max(ph[3], w[3])
                ph[4] = f"{ph[4]} {w[4]}"
                break
        else:
            rows.append(list(w))
    out = []
    for x0, y0, x1, y1, t in rows:
        r = pymupdf.Rect(x0, y0, x1, y1) * inv
        out.append(((r.x0, r.y0, r.x1, r.y1), t, (x0, y0, x1, y1)))
    return out


def room_labels(abs_path: str, reg: Region, limit: int = 10) -> list[dict]:
    """Room-name candidates printed inside the region: [{text, box}], stacked lines joined ('Secondary Classroom' /
    '(Severe Needs)'), numbers / dimensions / door tags / area figures left out. Box = unrotated pdf_pt / image px."""
    items: list[tuple[tuple, str, tuple]] = []  # (box in the region's space, text, box in reading orientation)
    p = Path(abs_path)
    if p.suffix.lower() == ".pdf":
        import pymupdf

        with pymupdf.open(p) as d:
            items = _pdf_phrases(d[reg.page])
    else:
        lay = _image_layer(p)
        if lay:
            items = [(tuple(it["box"]), it.get("text", ""), tuple(it["box"])) for it in lay[0].get("items") or []]
    inside = [(b, re.sub(r"\s+", " ", t).strip(), d) for b, t, d in items if reg.contains(*_centre(b))]
    cands = [(b, t, d) for b, t, d in inside
             if len(re.findall(r"[A-Za-zÀ-ÿ]", t)) >= 3 and not _NOT_ROOM.match(t) and not _AREA_TXT.fullmatch(t)
             and len(t) <= 48]
    cands.sort(key=lambda c: (c[2][1], c[2][0]))
    used, out = set(), []
    for i, (b, t, d) in enumerate(cands):
        if i in used:
            continue
        box, disp, text = list(b), list(d), t
        for j in range(i + 1, len(cands)):  # the next line of the same label, right below (reading orientation)
            if j in used:
                continue
            b2, t2, d2 = cands[j]
            h = disp[3] - disp[1]
            if len(text.split()) + len(t2.split()) > 6:
                break
            if 0 <= d2[1] - disp[3] <= 0.9 * h and \
                    abs((d2[0] + d2[2]) / 2 - (disp[0] + disp[2]) / 2) <= 0.6 * max(d2[2] - d2[0], disp[2] - disp[0]):
                text = f"{text} {t2}"
                box = [min(box[0], b2[0]), min(box[1], b2[1]), max(box[2], b2[2]), max(box[3], b2[3])]
                disp = [min(disp[0], d2[0]), min(disp[1], d2[1]), max(disp[2], d2[2]), max(disp[3], d2[3])]
                used.add(j)
        used.add(i)
        out.append({"text": text, "box": [round(v, 1) for v in box]})
    seen, uniq = set(), []
    for c in out:
        k = c["text"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[:limit]


# --------------------------------------------------------------------------------------------------- answering

def scoped_question(question: str) -> str:
    """'How many doors are in this area?' -> 'How many doors are on this plan?' (the masked copy is the area)."""
    q = _HERE.sub(" on this plan", question).strip()
    q = re.sub(r"(?:\s+on this plan){2,}", " on this plan", q)
    q = re.sub(r"\s+([?.!])", r"\1", q)
    return re.sub(r"\s{2,}", " ", q)


def _ev_box(e) -> list | None:
    if e.polygon:
        xs, ys = [p[0] for p in e.polygon], [p[1] for p in e.polygon]
        return [min(xs), min(ys), max(xs), max(ys)]
    return list(e.bbox) if e.bbox else None


def _same_file(f: str, paths: set[str]) -> bool:
    if not f:
        return True
    try:
        return f in paths or str(Path(f).resolve()) in paths or Path(f).name in {Path(p).name for p in paths}
    except OSError:
        return f in paths


def _region_evidence(abs_path: str, reg: Region, note: str):
    from stoa.contract import Evidence

    x0, y0, x1, y1 = reg.bbox
    return Evidence(file=abs_path, page=reg.page if reg.space == "pdf_pt" else None, bbox=[x0, y0, x1, y1],
                    bbox_space=reg.space, polygon=None if reg.is_box else [list(p) for p in reg.polygon],
                    note=note, exact=True)


def _wants_rooms_area(question: str) -> bool:
    return bool(_AREA_Q.search(question)) and not _LEN_Q.search(question) and bool(_ROOMS_Q.search(question)) \
        and not _COUNT_Q.search(question)


def ask_by_labels(abs_path: str, question: str, reg: Region, solve: Callable, labels: list[dict]):
    """Each room label in the region asked on the full sheet; readings whose evidence lies in the region summed."""
    from stoa.contract import Answer

    paths = {abs_path, str(Path(abs_path).resolve())}
    terms, rejected, evidence, unit, confs, trace, cost = [], [], [], None, [], [], []
    for lab in labels:
        q = f"What is the area of the {lab['text']}?"
        try:
            a = solve(abs_path, q)
            if not isinstance(a, Answer):
                a = Answer.model_validate(a)
        except Exception as e:  # noqa: BLE001
            rejected.append(f"'{lab['text']}': {type(e).__name__}")
            continue
        cost.append(a.cost)
        v = a.value
        try:
            v = float(v)
        except (TypeError, ValueError):
            rejected.append(f"'{lab['text']}': no area ({a.value!r})")
            continue
        boxes = [(_ev_box(e), e) for e in a.evidence
                 if _ev_box(e) and _same_file(e.file, paths) and (e.page in (None, reg.page) or reg.space == "image_px")]
        own = [(b, e) for b, e in boxes if box_share_inside(b, reg) >= 0.5]
        if boxes and not own:
            rejected.append(f"'{lab['text']}' = {v:g} {a.unit or ''} but its outline is outside the region (another room "
                            "with that name?)")
            continue
        lx, ly = _centre(lab["box"])
        pad = 0.5 * min(lab["box"][2] - lab["box"][0], lab["box"][3] - lab["box"][1])
        mine = [(b, e) for b, e in own if b[0] - pad <= lx <= b[2] + pad and b[1] - pad <= ly <= b[3] + pad]
        if own and not mine:
            rejected.append(f"'{lab['text']}' = {v:g} {a.unit or ''} read at another room's place ("
                            f"{(own[0][1].note or 'its mark')[:50]})")
            continue
        own = mine or own
        if unit and a.unit and str(a.unit).lower() != str(unit).lower():
            rejected.append(f"'{lab['text']}' = {v:g} {a.unit} (unit differs from {unit})")
            continue
        if any(t["box"] and own and _iou(t["box"], own[0][0]) > 0.6 for t in terms):
            rejected.append(f"'{lab['text']}' = the same room as an earlier label")
            continue
        unit = unit or a.unit
        confs.append(float(a.confidence or 0.0) * (1.0 if own else 0.6))
        terms.append({"label": lab["text"], "value": v, "unit": a.unit, "conf": a.confidence,
                      "box": own[0][0] if own else None, "solver": a.solver,
                      "boxed": bool(own)})
        evidence += [e for _, e in own] or [e for _, e in boxes][:1]
        trace.append(f"region: '{lab['text']}' -> {v:g} {a.unit or ''} @{float(a.confidence or 0):.2f} ({a.solver})"
                     + ("" if own else " (no outline to check it is the one in the region)"))
    if not terms:
        return None, rejected
    total = round(sum(t["value"] for t in terms), 2)
    conf = round(min(confs) if confs else 0.0, 3)
    ans = Answer(value=total, unit=unit, confidence=conf, evidence=evidence,
                 solver="region/labels", trace=trace)
    ans.assumptions = [f"scoped to {reg.describe()}: the rooms whose names are printed inside it",
                       "sum of " + " + ".join(f"{t['label']} {t['value']:g}" for t in terms) + f" = {total:g} {unit or ''}"]
    if rejected:
        ans.missing = [f"not counted: {r}" for r in rejected]
    try:
        from stoa.contract import Cost

        tot = {}
        for c in cost:
            for k, val in c.model_dump().items():
                tot[k] = round((tot.get(k) or 0) + (val or 0), 4)
        ans.cost = Cost.model_validate(tot)
    except Exception:  # noqa: BLE001
        pass
    return ans, rejected


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _count_inside(a, reg: Region) -> str:
    """A count whose evidence is one mark per counted item: marks with half or more of their box outside the region are
    dropped and the count follows ('' = nothing changed / not a per-item count). EFA English 1: the next room's door
    sat on the drawn edge (centre on it) -> 2 for 1."""
    try:
        v = float(a.value)
    except (TypeError, ValueError):
        return ""
    if v != int(v) or v <= 0 or str(a.unit or "count").lower() not in ("count", "", "doors", "windows", "items"):
        return ""
    marks = [e for e in a.evidence if _ev_box(e) and e.bbox_space == reg.space]
    if len(marks) != int(v):
        return ""
    keep = [e for e in marks if box_share_inside(_ev_box(e), reg) > 0.5]  # more than half of the mark inside
    if len(keep) == len(marks):
        return ""
    gone = [e.note or "mark" for e in marks if e not in keep]
    a.value = len(keep)
    a.evidence = [e for e in a.evidence if e not in marks or e in keep]
    return (f"region: {len(marks)} marks counted on the copy, {len(gone)} of them mostly outside the region "
            f"({'; '.join(gone[:3])}) -> {len(keep)}")


def ask_by_tags(abs_path: str, reg: Region):
    """Printed room-area tags ('Lobby / 1.01 / 569 SF', 'Kitchen 12.4 m²') inside the region, summed: the drawing's
    own numbers, no measuring (VP's stoa.sheet.labels.as_area_tags, the reader area_tags uses). None = no tag inside."""
    from stoa.contract import Answer, Evidence
    from stoa.sheet.labels import as_area_tags

    layer, pdf = None, abs_path.lower().endswith(".pdf")
    if not pdf:
        if _image_layer(Path(abs_path)) is None:
            return None  # never start a whole-sheet reading for this
        from stoa.solvers.raster import textlayer as T

        layer = T.text_layer(abs_path)
    tags = as_area_tags(abs_path, None, reg.page if pdf else 0, layer=layer) or ()
    inside = [t for t in tags if t.get("bbox") and reg.contains(*_centre(t["bbox"]))]
    if not inside:
        return None
    units = {t["unit"] for t in inside}
    if len(units) > 1:
        return None
    seen, uniq = set(), []
    for t in inside:  # one tag per room (a room number printed twice)
        k = (t.get("number") or t.get("name") or "", t["value"])
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    total = round(sum(float(t["value"]) for t in uniq), 2)
    unit = units.pop()
    name = lambda t: " ".join(x for x in (t.get("name") or "", t.get("number") or "") if x) or "room"  # noqa: E731
    ev = [Evidence(file=abs_path, page=reg.page if pdf else None, bbox=list(t["bbox"]),
                   bbox_space="pdf_pt" if pdf else "image_px", note=f"{name(t)}: {t['value']:g} {unit} (printed)",
                   exact=True) for t in uniq]
    ans = Answer(value=total, unit=unit, confidence=0.8 if len(uniq) > 1 else 0.75, evidence=ev,
                 solver="region/area_tags",
                 trace=[f"region: {len(uniq)} printed room-area tags inside the region: "
                        + ", ".join(f"{name(t)} {t['value']:g}" for t in uniq)])
    ans.assumptions = [f"scoped to {reg.describe()}: the rooms whose printed area tags lie inside it",
                       "sum of " + " + ".join(f"{name(t)} {t['value']:g}" for t in uniq) + f" = {total:g} {unit}",
                       "areas as printed on the drawing (not measured)"]
    return ans


def ask_masked(abs_path: str, question: str, reg: Region, solve: Callable):
    from stoa.contract import Answer

    # crop (instead of mask) for counts on images: measured NO gain (A-101 PNG modules 10 / 10 either way: both copies
    # go to the CubiCasa LoRA count) and a crop loses the printed scale -> off unless STOA_REGION_CROP=1
    crop = os.environ.get("STOA_REGION_CROP") == "1" and bool(_COUNT_Q.search(question)) and \
        Path(abs_path).suffix.lower() != ".pdf"
    m = masked_copy(abs_path, reg, crop=crop)
    q2 = scoped_question(question)
    a = solve(m.path, q2)
    if not isinstance(a, Answer):
        a = Answer.model_validate(a)
    copy = {m.path, str(Path(m.path).resolve())}
    kept_out = 0
    for e in a.evidence:  # the copy's evidence = the original's (same page geometry); page index of the original
        if e.file and (e.file in copy or str(Path(e.file).resolve()) in copy or Path(e.file).name == Path(m.path).name):
            e.file = abs_path
            if e.bbox_space == "pdf_pt":
                e.page = m.page
            elif e.bbox_space == "image_px" and m.offset != (0.0, 0.0):
                ox, oy = m.offset
                if e.bbox:
                    e.bbox = [e.bbox[0] + ox, e.bbox[1] + oy, e.bbox[2] + ox, e.bbox[3] + oy]
                for attr in ("polygon", "polyline"):
                    pts = getattr(e, attr, None)
                    if pts:
                        setattr(e, attr, [[x + ox, y + oy] for x, y in pts])
        b = _ev_box(e)
        if b and box_share_inside(b, reg) < 0.5:
            kept_out += 1
    a.trace = [str(t).replace(m.path, abs_path) for t in (a.trace or [])]
    dropped = _count_inside(a, reg)
    a.trace = [f"region: asked '{q2}' on a copy of the sheet showing only {reg.describe()}"
               + (f"; printed scale kept: {', '.join(repr(s) for s in m.kept_scale[:3])}" if m.kept_scale else ""),
               *m.notes, *a.trace]
    a.assumptions = [f"scoped to {reg.describe()}: everything outside it was hidden from the readers",
                     *(x.replace(m.path, abs_path) for x in a.assumptions)]
    if dropped:
        a.trace = [*a.trace, dropped]
        kept_out = 0
    if kept_out:
        a.missing = [*a.missing, f"{kept_out} evidence mark(s) lie mostly outside the region (scale notes or a reading "
                                 "that crossed its edge): check them"]
    if m.n_text_in == 0 and Path(abs_path).suffix.lower() == ".pdf":
        a.missing = [*a.missing, "no printed text inside the region"]
    return a


# --------------------------------------------------------------------------------------------------- outline measure

_OUTLINE_Q = re.compile(
    r"(?i)\b(?:area|size|perimeter|length)\s+of\s+(?:this|the|my)\s+(?:drawn\s+|marked\s+)?"
    r"(?:outline|shape|polygon|selection|lasso|box|line)\b|\bmeasure\s+(?:this|the|my)\s+(?:outline|shape|polygon|"
    r"selection|line)\b|\b(?:outline|polygon)\s+(?:area|perimeter)\b|"
    r"\b(?:skirting|baseboards?)\b[^?]*?\b(?:of|along|around|for)\s+(?:this|the|my)\s+(?:drawn\s+|marked\s+)?"
    r"(?:outline|shape|polygon|selection|lasso)\b")
_INCH_M = 0.0254


def _shoelace(poly: list[list[float]]) -> float:
    n = len(poly)
    return abs(sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))) / 2


def _perimeter(poly: list[list[float]], closed: bool = True) -> float:
    n = len(poly)
    m = n if closed else n - 1
    return sum(((poly[(i + 1) % n][0] - poly[i][0]) ** 2 + (poly[(i + 1) % n][1] - poly[i][1]) ** 2) ** 0.5
               for i in range(m))


def pdf_scale(abs_path: str, reg: Region) -> tuple[float | None, str]:
    """Metres per PDF point at the region, from the printed scale: the view the region sits in (VP's views, paper note
    applied), else the page's one printed ratio, x the paper factor ('1:200 @ A1' on an A3 page = 1:400)."""
    cx, cy = _centre(reg.bbox)
    try:
        from stoa.sheet import views

        for v in views(abs_path, reg.page):
            if v.contains(cx, cy) and v.page_ratio:
                return v.page_ratio * _INCH_M / 72, f"1:{v.page_ratio:g} of the view '{v.title}' (printed)"
    except Exception:  # noqa: BLE001
        pass
    import pymupdf

    from stoa.solvers.raster.textlayer import parse_scales

    with pymupdf.open(abs_path) as d:
        text = d[reg.page].get_text()
    ratios = sorted({round(s["ratio"], 2) for line in text.splitlines() for s in parse_scales(line)
                     if 20 <= s["ratio"] <= 2000})
    if len(ratios) != 1:
        return None, ("several printed scales (" + ", ".join(f"1:{r:g}" for r in ratios) + ")") if ratios else \
            "no printed scale"
    n, why = ratios[0], f"1:{ratios[0]:g} printed on the sheet"
    try:
        from stoa.formats.normalize import paper_copy

        got = paper_copy(abs_path) if reg.page == 0 else None
        if got and got[1] and abs(got[1] - 1) > 0.01:
            n, why = n * got[1], f"{why}, for a {got[1]:.2f}x larger paper: 1:{n * got[1]:g} on this page"
    except Exception:  # noqa: BLE001
        pass
    return n * _INCH_M / 72, why


def image_scale(abs_path: str, user_px_per_m: float | None = None) -> tuple[float | None, str]:
    """Pixels per metre: the visitor's own scale, else the printed ratio + paper size (DPI), else a graphic scale bar."""
    if user_px_per_m:
        return float(user_px_per_m), f"your scale: 1 m = {float(user_px_per_m):.2f} px"
    from stoa.solvers.raster import scalebar as SB

    lay = _image_layer(Path(abs_path))
    tl = None
    if lay is not None:
        from stoa.solvers.raster import textlayer as T

        try:
            tl = T.text_layer(abs_path)
        except Exception:  # noqa: BLE001
            tl = None
    if tl is not None:
        try:
            pb = SB.printed_bar(abs_path, tl)
            if pb is not None and pb.px_per_m:
                return pb.px_per_m, f"printed 1:{pb.ratio:g} on {pb.paper} at {pb.dpi:g} dpi: 1 m = {pb.px_per_m:.2f} px"
        except Exception:  # noqa: BLE001
            pass
        try:
            b = SB.best_bar(abs_path, layer=tl, reread=False)
            if b is not None and b.px_per_m:
                return b.px_per_m, f"{b.describe()} -> 1 m = {b.px_per_m:.2f} px"
        except Exception:  # noqa: BLE001
            pass
    return None, "no scale found on the image (no text layer read yet)" if tl is None else "no scale found on the image"


def outline_measure(abs_path: str, question: str, reg: Region, user_px_per_m: float | None = None):
    """'Area of this outline': the area (and perimeter) of the polygon the visitor drew, at the sheet's scale. The
    visitor's own outline is the evidence (exact = it is what they drew); no reader, no model ($0, < 1 s). The scan
    fallback when room tracing is unsure (captain 20:08, audit fix #4: scanned DfE kitchen traced across rooms)."""
    from stoa.contract import Answer, Evidence

    pdf = abs_path.lower().endswith(".pdf")
    if pdf:
        m_per_u, why = pdf_scale(abs_path, reg)
        if m_per_u is None:  # an image-only (scanned) page: its scan render's pixel scale
            try:
                from stoa.formats.normalize import scan_png

                got = scan_png(abs_path) if reg.page == 0 else None
            except Exception:  # noqa: BLE001
                got = None
            if got:
                png, zoom = got
                ppm, why2 = image_scale(png, user_px_per_m)
                if ppm:
                    m_per_u, why = zoom / ppm, f"scanned page read at {72 * zoom:.0f} dpi; {why2}"
    else:
        ppm, why = image_scale(abs_path, user_px_per_m)
        m_per_u = 1 / ppm if ppm else None
    u = "pt" if pdf else "px"
    base = bool(_BASEBOARD_Q.search(question))
    is_len = base or bool(re.search(r"(?i)\b(?:perimeter|length)\b", question)) or len(reg.polygon) == 2
    ev = Evidence(file=abs_path, page=reg.page if pdf else None, bbox=list(reg.bbox), bbox_space=reg.space,
                  polygon=[list(p) for p in reg.polygon], exact=True, note="your outline")
    if m_per_u is None:
        area_u, per_u = _shoelace(reg.polygon), _perimeter(reg.polygon)
        a = Answer(value=None, unit="m" if is_len else "m2", confidence=0.0, evidence=[ev], solver="region/outline",
                   trace=[f"region: outline {area_u:.0f} {u}² / perimeter {per_u:.0f} {u}; {why}"])
        a.missing = [f"the drawing's scale ({why}): set it (Measure -> set the scale with a known length) and ask again"]
        return a
    poly, snap_notes, ink, frame, ops = reg.polygon, [], None, None, None
    # snap unless the region says so or the question asks for the outline 'as drawn' (the web re-asks that way after
    # the visitor dragged a corner: their placement wins)
    want_snap = reg.snap and len(reg.polygon) >= 3 and not re.search(r"(?i)\bas\s+(?:drawn|placed)\b", question)
    if want_snap or base:
        try:
            ink, org, z = _ink_frame(abs_path, reg, m_per_u)  # darkness 0..1
            frame = (org, z, z / m_per_u)
        except Exception as e:  # noqa: BLE001 — no picture to test: measure the outline as drawn
            snap_notes = [f"not snapped: the drawing could not be read here ({type(e).__name__})"]
    if frame is not None:
        (ox, oy), z, ppm = frame
        px = [[(x - ox) * z - 0.5, (y - oy) * z - 0.5] for x, y in reg.polygon]  # px centres sit at +0.5
        sp = None
        px0 = px
        if want_snap and len(px) > 8:  # a freehand lasso: its corners first (Douglas-Peucker at 0.25 m), then snap those
            import cv2
            import numpy as np

            simp = cv2.approxPolyDP(np.asarray(px, np.float32).reshape(-1, 1, 2), 0.25 * ppm, True).reshape(-1, 2)
            if 3 <= len(simp) <= 16:
                px = [[float(x), float(y)] for x, y in simp]
        if want_snap:
            try:
                bias = _stroke_half_pt(abs_path, reg, m_per_u) * z if pdf else 0.0
            except Exception:  # noqa: BLE001
                bias = 0.0
            sp, snap_notes = snap_outline(ink, px, ppm, bias_px=bias)
            if sp is not None and bias:
                snap_notes = [f"{snap_notes[0]}, to the centre of the {2 * bias / z:.2f}-pt wall strokes"]
        if sp is not None:
            poly = [[(x + 0.5) / z + ox, (y + 0.5) / z + oy] for x, y in sp]
            px = sp
        else:
            px = px0  # not snapped: the openings are looked for along the outline as drawn (the perimeter's own)
        if base and len(px) >= 3:
            ops = outline_openings(ink, px, ppm, snapped=sp is not None)
            if ops is not None:
                ops["segments_u"] = [([(o["a"][0] + .5) / z + ox, (o["a"][1] + .5) / z + oy],
                                      [(o["b"][0] + .5) / z + ox, (o["b"][1] + .5) / z + oy],
                                      o["m"]) for o in ops["openings"]]
    area_u, per_u = _shoelace(poly), _perimeter(poly)
    area, per = area_u * m_per_u ** 2, per_u * m_per_u
    snapped = poly is not reg.polygon
    if snapped:
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        ev.polygon, ev.bbox = [list(p) for p in poly], [min(xs), min(ys), max(xs), max(ys)]
    ev.note = f"{'your outline, on the wall faces' if snapped else 'your outline'}: {area:.2f} m² " \
              f"({area / 0.09290304:.0f} ft²), perimeter {per:.2f} m"
    conf = 0.85 if user_px_per_m else 0.75
    evs, trace_ops, assumptions, missing = [ev], [], [], []
    value, unit = (round(per, 2), "m") if is_len else (round(area, 2), "m2")
    if base:
        if ops is None:
            missing.append("door openings: could not test the walls along the outline; this is the full perimeter")
            conf = min(conf, 0.4)
        else:
            open_m = ops["openings_m"]
            if open_m > 0.4 * per:  # most of the outline is 'open': it does not follow walls; do not deduct blindly
                missing.append(f"door openings: {open_m:.2f} m of the {per:.2f} m outline has no wall within reach, so "
                               "it does not follow the walls; nothing deducted (drag the corners onto the wall faces)")
                conf = min(conf, 0.3)
            else:
                value = round(per - open_m, 2)
                ops_m = []
                for (x0, y0), (x1, y1), m in ops["segments_u"]:
                    ops_m.append(m)
                    evs.append(Evidence(file=abs_path, page=reg.page if pdf else None,
                                        bbox=[min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], bbox_space=reg.space,
                                        polyline=[[x0, y0], [x1, y1]], exact=False,
                                        note=f"opening {m:.2f} m (no skirting)"))
                trace_ops = [f"perimeter {per:.2f} m - {len(ops_m)} opening(s) "
                             f"{' + '.join(f'{m:.2f}' for m in ops_m) or '0'} m = {value:.2f} m"]
                assumptions.append("baseboard = the outline's perimeter minus the door openings found along it (a "
                                   "stretch with no wall line just outside it, >= 0.4 m); windows keep their wall line")
                if not ops_m:
                    missing.append("no door opening was found along the outline: if the room has a door, its width "
                                   "is still included")
                    conf = min(conf, 0.5)
                conf = min(conf, 0.7)
    a = Answer(value=value, unit=unit, confidence=conf, evidence=evs, solver="region/outline",
               trace=[f"region: the outline you drew, {len(reg.polygon)} corners"
                      + (f" ({snap_notes[0]})" if snap_notes else "")
                      + f": {area_u:.0f} {u}² and {per_u:.0f} {u} at 1 {u} = {m_per_u:.5f} m ({why}"
                      # 'N px per m': the web's scaleFromAnswer (measure.ts) takes the scale from it (no 'Scale unknown' pill)
                      + ("" if pdf else f"; {1 / m_per_u:.2f} px per m") + ")", *trace_ops])
    what = "baseboard along" if base else "perimeter of" if is_len else "area of"
    a.assumptions = [f"the {what} the outline you drew (its corners as placed"
                     + ("; its sides moved onto the nearest wall faces" if snapped else "")
                     + "; drag them to refine)", f"scale: {why}", *assumptions,
                     f"= {area / 0.09290304:,.0f} ft²" if not is_len else f"= {value / 0.3048:,.1f} ft"]
    a.missing = missing
    return a


# --------------------------------------------------------------------------------------------------- wall snap + baseboard

_BASEBOARD_Q = re.compile(r"(?i)\b(?:skirting|baseboards?|base\s*boards?|jalkalist|golvlist|sockelleiste)")
SNAP_REACH_M = 0.45  # a drawn side moves at most this far onto a wall face
INK_PX_PER_M = 200.0  # PDF regions are rendered at about this many px per metre for the wall / opening tests
LINE_DARK = 0.3  # mean darkness (0 white .. 1 black) along a side's middle 80 % that makes it a wall line


def _ink_frame(abs_path: str, reg: Region, m_per_u: float):
    """(darkness array 0..1 float32, (ox, oy) origin in file units, px per unit) around the region (+1.5 m): a PDF page
    is rendered with its /Rotate taken off (region points are unrotated pdf_pt), an image is cropped as stored. Red ink
    (unit outlines, annotations) counts as paper, like stoa.solvers.raster.measure._ink."""
    import numpy as np

    margin = 1.5 / m_per_u
    x0, y0, x1, y1 = reg.bbox
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    if abs_path.lower().endswith(".pdf"):
        import pymupdf

        with pymupdf.open(abs_path) as d:
            pg = d[reg.page]
            if pg.rotation:
                pg.set_rotation(0)  # in memory only (never saved): unrotated points = page points
            clip = pymupdf.Rect(x0, y0, x1, y1) & pg.rect
            z = min(16.0, max(1.0, INK_PX_PER_M * m_per_u), 5000 / max(clip.width, clip.height, 1))
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, alpha=False)
            rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[..., :3]
            org = (clip.x0, clip.y0)
    else:
        from PIL import Image

        with Image.open(abs_path) as im:
            W, H = im.size
            box = (max(0, int(x0)), max(0, int(y0)), min(W, int(x1) + 1), min(H, int(y1) + 1))
            rgb = np.asarray(im.convert("RGB").crop(box))
        org, z = (box[0], box[1]), 1.0
    g = rgb.astype(np.float32).mean(axis=2)
    red = (rgb[..., 0] > 140) & (rgb[..., 1] < 120) & (rgb[..., 2] < 120)
    dark = np.clip((235.0 - g) / 200.0, 0, 1)  # paper (>= 235) = 0, ink (<= 35) = 1
    dark[red] = 0
    return dark.astype(np.float32), org, z


def _stroke_half_pt(abs_path: str, reg: Region, m_per_u: float) -> float:
    """Half the length-weighted median width (pt) of the vector STROKES near the outline: CAD wall geometry is the stroke's
    centre line, the ink edge a snap finds lies half a stroke room-wards (Stoa's DfE sheet: 2.1-pt wall fills outlined
    with 0.6-pt strokes -> ink 0.3 pt = 0.02 m inside the face, -3 % area on a 7 m² room). 0 for images / scans."""
    if not abs_path.lower().endswith(".pdf"):
        return 0.0
    import pymupdf

    g = 0.6 / m_per_u
    x0, y0, x1, y1 = reg.bbox
    near = pymupdf.Rect(x0 - g, y0 - g, x1 + g, y1 + g)
    ws = []
    with pymupdf.open(abs_path) as d:
        pg = d[reg.page]
        if pg.rotation:
            pg.set_rotation(0)
        for dr in pg.get_drawings():
            w = dr.get("width") or 0
            if "s" not in (dr.get("type") or "") or not 0.05 <= w <= 3 or not dr["rect"].intersects(near):
                continue
            r = dr["rect"]
            ws.append((w, max(r.width, r.height)))
    if not ws:
        return 0.0
    ws.sort()
    tot, acc = sum(L for _, L in ws), 0.0
    for w, L in ws:
        acc += L
        if acc >= tot / 2:
            return w / 2
    return 0.0


def _sample(dark, xs, ys):
    """Bilinear darkness at float pixel positions (outside the frame = 0)."""
    import cv2
    import numpy as np

    mx, my = np.asarray(xs, np.float32).ravel(), np.asarray(ys, np.float32).ravel()
    n = len(mx)
    if not n:
        return np.zeros(0, np.float32)
    w = 1024  # remap maps must stay below 32767 px per side
    pad = (-n) % w
    mx, my = np.pad(mx, (0, pad), constant_values=-10), np.pad(my, (0, pad), constant_values=-10)
    out = cv2.remap(dark, mx.reshape(-1, w), my.reshape(-1, w), cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0)
    return out.ravel()[:n]


def _profile(dark, a, t, nrm, L: float, R: int, sub: int = 4, span=(0.1, 0.9)):
    """Mean darkness of lines parallel to a side (its middle 80 %, or `span`), at offsets -R..R px in 1/sub px steps."""
    import numpy as np

    ss = np.arange(span[0] * L, span[1] * L, 1.0)
    ds = np.arange(-R * sub, R * sub + 1) / sub
    px = a[0] + t[0] * ss[None, :] + nrm[0] * ds[:, None]
    py = a[1] + t[1] * ss[None, :] + nrm[1] * ds[:, None]
    v = _sample(dark, px.ravel(), py.ravel()).reshape(len(ds), len(ss))
    return ds, v.mean(axis=1)


def _face(ds, pr, px_per_m: float, line_dark: float = LINE_DARK) -> float | None:
    """The room-side face of the nearest wall line in a darkness profile (offsets ds, + = outwards), sub-pixel; None
    when no offset reaches line_dark."""
    import numpy as np

    hit = pr >= line_dark
    if not hit.any():
        return None
    idx = np.flatnonzero(hit)
    i0 = int(min(idx, key=lambda i: (abs(ds[i]), -ds[i])))  # nearest line; a tie goes outwards
    i = i0
    while i - 1 >= 0 and hit[i - 1]:
        i -= 1  # room-wards across a filled wall
    cav = 0.3 * px_per_m  # drawn in a hollow wall's cavity (a line on both sides, white between): the room-side line
    inner = [j for j in range(i - 2, -1, -1) if hit[j] and ds[i] - ds[j] <= cav]
    if inner and ds[i0] > 0:
        i = inner[0]
        while i - 1 >= 0 and hit[i - 1]:
            i -= 1
    j = i
    while j + 1 < len(pr) and hit[j + 1] and pr[j + 1] >= pr[j]:
        j += 1  # up to the line's darkest offset
    if i == 0 or pr[i] == pr[i - 1]:
        return float(ds[i])
    half = 0.5 * (pr[j] + pr[i - 1])
    k = i - 1
    while k < j and pr[k + 1] < half:
        k += 1
    f = (half - pr[k]) / max(pr[k + 1] - pr[k], 1e-6) if k < j else 0.0
    return float(ds[k] + f * (ds[1] - ds[0]))


def snap_outline(dark, poly_px: list[list[float]], px_per_m: float, reach_m: float = SNAP_REACH_M,
                 line_dark: float = LINE_DARK, bias_px: float = 0.0) -> tuple[list[list[float]] | None, list[str]]:
    """Each drawn side -> the room-side face of the nearest wall line parallel to it (within reach_m). The mean
    darkness along the side's middle 80 % is >= line_dark on a wall line (a door gap still leaves ~45 % on a short
    wall); the nearest such offset wins, then it walks room-wards while the line holds (a filled wall's thickness; a
    hollow wall's room-side line when the side was drawn in its cavity). The face = where the darkness crosses half the
    wall's, sub-pixel (bilinear samples, 1/4 px steps). Corners = where the moved sides meet. -> (snapped polygon in the
    same px, notes) or (None, why) when it cannot snap (a lasso with many / short sides, < 3/4 of the sides found a wall)."""
    import numpy as np

    P = np.asarray(poly_px, np.float64)
    n = len(P)
    if n < 3 or n > 16:
        return None, [f"not snapped: {n} corners (snapping is for outlines of 3-16 corners)"]
    R = max(2, int(round(reach_m * px_per_m)))
    lines, moved, found = [], [], 0
    for k in range(n):
        a, b = P[k], P[(k + 1) % n]
        L = float(np.hypot(*(b - a)))
        if L < 0.6 * px_per_m:
            return None, [f"not snapped: side {k + 1} is shorter than 0.6 m"]
        t = (b - a) / L
        nrm = np.array([t[1], -t[0]])  # outward: the probe just outside the side's middle must not be in the outline
        mid = (a + b) / 2 + nrm * min(3.0, L / 10)
        if point_in_polygon(float(mid[0]), float(mid[1]), P.tolist()):
            nrm = -nrm
        whole = _face(*_profile(dark, a, t, nrm, L, R), px_per_m, line_dark)
        if whole is None:
            lines.append((a, t))
            moved.append(None)
            continue
        # each half on its own: a side drawn at a slight angle takes the wall's angle (a parallel shift keeps the tilt)
        h1 = _face(*_profile(dark, a, t, nrm, L, R, span=(0.1, 0.45)), px_per_m, line_dark)
        h2 = _face(*_profile(dark, a, t, nrm, L, R, span=(0.55, 0.9)), px_per_m, line_dark)
        found += 1
        both = h1 is not None and h2 is not None and max(abs(h1 - whole), abs(h2 - whole)) <= 0.15 * px_per_m
        whole += bias_px  # vector sheets: the stroke's centre line, not its ink edge
        if both:
            q1, q2 = a + t * 0.275 * L + nrm * (h1 + bias_px), a + t * 0.725 * L + nrm * (h2 + bias_px)
            t2 = (q2 - q1) / max(float(np.hypot(*(q2 - q1))), 1e-9)
            lines.append((q1, t2))
            moved.append((h1 + h2) / 2 / px_per_m)
        else:
            lines.append((a + nrm * whole, t))
            moved.append(whole / px_per_m)
    if found < max(3, int(np.ceil(0.75 * n))):
        return None, [f"not snapped: only {found} of {n} sides found a wall line within {reach_m:g} m"]
    out = []
    for k in range(n):
        (p1, t1), (p2, t2) = lines[k - 1], lines[k]
        cr = t1[0] * t2[1] - t1[1] * t2[0]
        if abs(cr) < 0.26:  # nearly parallel neighbours: keep the drawn corner, moved like its side
            out.append(list(map(float, P[k] + (p2 - P[k]) - t2 * np.dot(p2 - P[k], t2))))
            continue
        w = p2 - p1
        u = (w[0] * t2[1] - w[1] * t2[0]) / cr
        out.append(list(map(float, p1 + t1 * u)))
    mv = [m for m in moved if m is not None]
    notes = [f"snapped {found} of {n} sides to the wall faces (moved {min(mv, key=abs):+.2f} to {max(mv, key=abs):+.2f} m"
             f"; + = outwards)"]
    return out, notes


def outline_openings(dark, poly_px: list[list[float]], px_per_m: float, snapped: bool, step: float = 1.0,
                     min_m: float = 0.4) -> dict | None:
    """Door openings along an outline: a stretch is an opening when the ray cast OUTWARD from it (up to SNAP_REACH_M)
    meets no WALL: a wall = a dark run >= 0.06 m thick (a filled wall) or two separate lines, faint ones included (a
    hollow wall, a window's grey glazing lines). A single thin line - a sill,
    a dashed corridor or overhead line, a door arc near the strike - does not close an opening (EFA English 1: a dashed
    line runs under its door). Runs >= min_m. (Same idea as raster.measure.room_perimeter, which takes any ink.)
    -> {"perimeter_px", "openings": [{"a": [x, y], "b": [x, y], "m": float}], "openings_m"} or None."""
    import numpy as np

    P = np.asarray(poly_px, np.float64)
    n = len(P)
    if n < 3:
        return None
    # 0.45 m both ways: a thick outer wall's window glazing sits mid-band (DfE hall: at 0.3 m only one grey line is
    # reached and every window became an 'opening': 44.0 vs gold 50.4; at 0.45 m 49.5)
    reach = max(3, int(round(SNAP_REACH_M * px_per_m)))
    thick = max(2, int(round(0.06 * px_per_m)))
    perim, runs, cur = 0.0, [], []
    for k in range(n):
        a, b = P[k], P[(k + 1) % n]
        seg = float(np.hypot(*(b - a)))
        if seg < 1:
            continue
        perim += seg
        t = (b - a) / seg
        nrm = np.array([t[1], -t[0]])
        mid = (a + b) / 2 + nrm * min(3.0, seg / 10)
        if point_in_polygon(float(mid[0]), float(mid[1]), P.tolist()):
            nrm = -nrm
        ss = np.arange(0, seg, step)
        dd = np.arange(0, reach + 0.5, 0.5)  # half-px steps: a 1-px grey line sampled between rows reads half as dark
        xs = a[0] + t[0] * ss[:, None] + nrm[0] * dd[None, :]
        ys = a[1] + t[1] * ss[:, None] + nrm[1] * dd[None, :]
        dv = _sample(dark, xs.ravel(), ys.ravel()).reshape(len(ss), len(dd))
        for si in range(len(ss)):
            r = np.flatnonzero(np.diff(np.concatenate([[0], (dv[si] >= 0.5).astype(np.int8), [0]])))
            starts, ends = r[::2], r[1::2]
            # faint lines count in pairs: a window's glazing is 2-4 thin GREY lines across the wall band (Stoa's DfE
            # hall on the heavy scan: g ~ 180, darkness ~ 0.3); a doorway's band is blank (leaves / arcs swing outside)
            faint = np.flatnonzero(np.diff(np.concatenate([[0], (dv[si] >= 0.2).astype(np.int8), [0]])))[::2]
            wall = len(starts) >= 2 or (len(starts) == 1 and (ends[0] - starts[0]) * 0.5 >= thick) or len(faint) >= 2
            p = a + t * ss[si]
            if not wall:
                cur.append([float(p[0]), float(p[1])])
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
            cur = []
    ops = []
    for c in runs:
        m = (len(c) * step) / px_per_m
        if m >= min_m:
            ops.append({"a": c[0], "b": c[-1], "m": round(m, 3)})
    return {"perimeter_px": perim, "openings": ops, "openings_m": round(sum(o["m"] for o in ops), 3)}


def _scan_proxy(abs_path: str, reg: Region):
    """(scan render png, px per pt) of a ONE-page image-only PDF (stoa.formats.normalize.scan_png, cached), else None.
    The masked PDF of a scan keeps no printed scale (its notes are pixels, not text) and the tags / labels readers find
    no PDF text: on the render they read the image's own text layer (OCR / VLM, usually cached by the upload)."""
    if not abs_path.lower().endswith(".pdf") or reg.space != "pdf_pt" or reg.page != 0:
        return None
    try:
        import pymupdf

        with pymupdf.open(abs_path) as d:
            if d.page_count != 1:
                return None
        from stoa.formats.normalize import scan_png

        return scan_png(abs_path)
    except Exception:  # noqa: BLE001
        return None


def _image_to_pdf(ans, abs_path: str, page: int, zoom: float) -> None:
    """Evidence on the scan render (image px) -> the uploaded PDF page (pdf_pt = px / zoom)."""
    z = 1 / zoom
    for e in ans.evidence:
        if e.bbox_space != "image_px":
            continue
        e.file, e.page, e.bbox_space = abs_path, page, "pdf_pt"
        if e.bbox:
            e.bbox = [v * z for v in e.bbox]
        for attr in ("polygon", "polyline"):
            pts = getattr(e, attr, None)
            if pts:
                setattr(e, attr, [[x * z, y * z] for x, y in pts])


def _scoped(path: str, question: str, reg: Region, solve: Callable):
    """The strategies (outline / printed tags / room labels / masked copy) on one readable file."""
    if _OUTLINE_Q.search(question):
        return outline_measure(path, question, reg, _user_ppm(solve))
    if _wants_rooms_area(question):
        try:
            ans = ask_by_tags(path, reg)
        except Exception:  # noqa: BLE001 — no tags reader on this tree / unreadable: the labels path
            ans = None
        if ans is not None:
            return ans
        labels = room_labels(path, reg)
        if labels:
            ans, _rejected = ask_by_labels(path, question, reg, solve, labels)
            if ans is not None:
                return ans
    return ask_masked(path, question, reg, solve)


def with_region(abs_path: str, question: str, region, solve: Callable, force: bool = False):
    """THE entry point. region None / feature off -> solve(abs_path, question) unchanged."""
    from stoa.contract import Answer

    reg = None
    try:
        reg = parse_region(region)
    except Exception:  # noqa: BLE001
        reg = None
    if reg is None or not (force or enabled()):
        return solve(abs_path, question)
    try:
        proxy = _scan_proxy(abs_path, reg)
        if proxy is not None:  # an image-only (scanned) page: scope on its scan render, evidence back in pdf_pt
            png, zoom = proxy
            preg = Region(page=0, space="image_px", polygon=[[x * zoom, y * zoom] for x, y in reg.polygon])
            ans = _scoped(png, question, preg, solve)
            _image_to_pdf(ans, abs_path, reg.page, zoom)
            ans.trace = [f"region: the page is a scan: scoped on its {72 * zoom:.0f}-dpi image", *ans.trace]
            ans.trace = [str(t).replace(png, abs_path) for t in ans.trace]
        else:
            ans = _scoped(abs_path, question, reg, solve)
        if ans.solver != "region/outline":  # the outline answer's evidence IS the drawn polygon
            ans.evidence = [_region_evidence(abs_path, reg, "the region you drew"), *ans.evidence]
        return ans
    except Exception as e:  # noqa: BLE001 — never lose the question: the whole-sheet answer, said so, low confidence
        a = solve(abs_path, question)
        if not isinstance(a, Answer):
            a = Answer.model_validate(a)
        a.confidence = min(float(a.confidence or 0.0), 0.2)
        a.missing = [*a.missing, f"could not limit the answer to the region you drew ({type(e).__name__}: {e}); "
                                 "this is the whole sheet's reading"]
        return a


def _user_ppm(solve) -> float | None:
    """The visitor's pixel scale (W5 'set scale' -> solver_args px_per_m -> Pipeline.px_per_m) behind a bound solve."""
    try:
        owner = getattr(solve, "_ppm_owner", None) or getattr(solve, "__self__", None)
        v = getattr(owner, "px_per_m", None)
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def upload_solve(solve: Callable, ctx: dict | None, page=None) -> Callable:
    """For web/server/ask_upload.py: `solve = upload_solve(solver.solve, req.get("context"), req.get("page"))`, then
    `solve(abs_path, question)`. A context with a region (registry.ts askWith {region, space, page}) is honoured even when
    the server's STOA_FEATURES lacks 'region': it only arrives when the visitor drew it with the web feature switched on,
    and answering the whole sheet instead would be wrong. No region -> solve unchanged."""
    if not isinstance(ctx, dict) or not ctx.get("region"):
        return solve
    pts = ctx["region"]
    reg = {"polygon": pts, "space": ctx.get("space") or "pdf_pt",
           "page": int(ctx["page"]) if isinstance(ctx.get("page"), int) else int(page or 0)}

    def inner(p, q):
        """The solver without the region in $STOA_ASK_CONTEXT: this feature scopes, the pipeline's own region branch
        (G1 night-G1 c6918dc, used when STOA_FEATURES lacks 'region') must not scope the per-room questions again."""
        keep = os.environ.get("STOA_ASK_CONTEXT")
        try:
            if keep:
                try:
                    c = json.loads(keep)
                    if isinstance(c, dict) and "region" in c:
                        c.pop("region", None)
                        os.environ["STOA_ASK_CONTEXT"] = json.dumps(c)
                except ValueError:
                    pass
            return solve(p, q)
        finally:
            if keep is None:
                os.environ.pop("STOA_ASK_CONTEXT", None)
            else:
                os.environ["STOA_ASK_CONTEXT"] = keep

    inner._ppm_owner = getattr(solve, "__self__", None)  # type: ignore[attr-defined]  (the visitor's px/m)
    return lambda p, q: with_region(p, q, reg, inner, force=True)


# --------------------------------------------------------------------------------------------------- suggestions

def suggestions(abs_path: str, region) -> list[dict]:
    """Questions for the popover, from what is printed in the region (cheap: text layer only, no model)."""
    reg = parse_region(region)
    if reg is None:
        return []
    out = [{"label": "How many doors in this area?", "question": "How many doors are in this area?", "kind": "count"},
           {"label": "How many windows in this area?", "question": "How many windows are in this area?", "kind": "count"}]
    try:
        labels = room_labels(abs_path, reg, limit=6)
    except Exception:  # noqa: BLE001
        labels = []
    if len(labels) >= 2:
        out.insert(0, {"label": f"Total area of the {len(labels)} rooms in here",
                       "question": "What is the total area of the rooms in this area?", "kind": "area",
                       "rooms": [lab["text"] for lab in labels]})
    for lab in labels[:3]:
        out.append({"label": f"Area of {lab['text']}", "question": f"What is the area of the {lab['text']}?",
                    "kind": "area", "box": lab["box"], "scoped": False})
    x0, y0, x1, y1 = reg.bbox
    if max(x1 - x0, y1 - y0) > 5 * min(x1 - x0, y1 - y0):
        out.append({"label": "Length of the wall / line in here", "question": "What is the length of the wall in this area?",
                    "kind": "length"})
    return out


# --------------------------------------------------------------------------------------------------- HTTP

try:  # module level: with `from __future__ import annotations` FastAPI cannot resolve a model defined in _router()
    from pydantic import BaseModel

    class RegionAsk(BaseModel):
        path: str
        question: str = ""
        region: dict
        solver: str = "pipeline"
        solver_args: dict = {}
except Exception:  # noqa: BLE001
    RegionAsk = None  # type: ignore[assignment,misc]


def _router():
    from fastapi import APIRouter, HTTPException

    r = APIRouter(prefix="/feat/region", tags=["region"])

    def _abs(path: str) -> str:
        from stoa.data import abspath, data_root

        if not path or ".." in Path(path).parts or path.startswith("/"):
            raise HTTPException(400, "path must be dataset-relative (data/...) without '..'")
        p = abspath(path).resolve()
        if data_root().resolve() not in p.parents or not p.is_file():
            raise HTTPException(404, f"no such file: {path}")
        return str(p)

    @r.get("/health")
    def health() -> dict:
        return {"ok": True, "feature": FEATURE, "enabled": enabled()}

    @r.post("/suggest")
    def suggest(req: RegionAsk) -> dict:
        if not enabled():
            return {"enabled": False, "suggestions": []}
        return {"enabled": True, "suggestions": suggestions(_abs(req.path), req.region)}

    @r.post("/ask")
    def ask(req: RegionAsk) -> dict:
        if not enabled():
            raise HTTPException(404, "feature 'region' is off (STOA_FEATURES)")
        from stoa.contract import CostMeter
        from stoa.data import dataset_rel
        from stoa.solvers import get_solver

        abs_path = _abs(req.path)
        solver = get_solver(req.solver, **req.solver_args)
        with CostMeter():
            ans = with_region(abs_path, req.question, req.region, lambda p, q: solver.solve(p, q))
        out = ans.model_dump()
        for e in out.get("evidence") or []:
            if e.get("file") in (abs_path, None, ""):
                e["file"] = req.path
            else:
                e["file"] = dataset_rel(e["file"])
        return out

    return r


try:
    router = _router()
except Exception:  # noqa: BLE001 — fastapi missing in a tool env: the module still works as a library
    router = None
