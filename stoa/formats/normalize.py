"""One call that turns any uploaded drawing into a file the solvers read, and maps the evidence back (P1).

The solvers read PDF (page 0), PNG/JPG, IFC, DXF and IfcConvert-style SVG. Stoa uploads arbitrary files in the UI
(captain 14 Nov 12:52), so before solving:

  .dwg                 -> <stem>.dwg.dxf (stoa.formats.dwg.to_dxf: LibreDWG/ODA; missing converter = a plain message)
  .svg, not IfcConvert -> <stem>.svg-v2.pdf (MuPDF keeps vectors AND text: a Swiss sheet exported to SVG and back has
                          the same 1,016 text chars, 517/536 paths and coordinates), so the vector-PDF readers
                          (pdf_vector, plan_rooms, schedule, pdf_text) answer it instead of the prior
  .svg with class=Ifc* -> unchanged (svg_ifc reads the IFC classes)
  .dxf, other layer names -> a copy with its layers renamed to the reader's roles by name (AIA A-DOOR, German Tür, …;
                          stoa/formats/dxflayers.py); no recognisable names -> answers capped (see solvers/upload.py)
  .heic/.heif          -> <stem>.jpg, upright (iPhone photos; needs pi-heif or pillow-heif, else a plain message)
  .tif/.webp/.bmp/.gif -> <stem>.png in $STOA_CACHE/formats (first page, transparent background on white), and a
  16-bit grey PNG      -> the same, greys SCALED to 8 bit (Pillow clips them: grey 50 -> 255), and a transparent PNG
                          -> on white; same pixel grid, so the evidence stays on the uploaded file (the web upload
                          converts TIFF / WebP / BMP / GIF and flattens transparency itself)
  .pdf with > 1 page   -> one-page copy of the page the question is about (stoa.formats.pages.pick_page)
  scanned .pdf page    -> PNG at the scan's own resolution (image-only page: no text, no paths), so the image readers
                          (raster, VLM) read it: Swiss sheet scanned 10% as PDF vs 55% as PNG (up_fmt3)

    from stoa.formats.normalize import prepare, restore
    prep = prepare(path, question)          # prep.path = what to solve; prep.notes = assumptions to show
    ans = solver.solve(prep.path, question)
    ans = restore(ans, prep)                # evidence back on the uploaded file (page k / SVG pixels) + notes

Files already readable are returned unchanged (prep.kind == "as_is"): 0 changes on every dataset split (no
dataset question is on a multi-page PDF, DWG, HEIC or generic SVG). Converted files are cached next to the upload,
or under $STOA_CACHE/formats when that folder is read-only.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

HEIC_EXT = {".heic", ".heif", ".hif"}
PIC_EXT = {".tif", ".tiff", ".webp", ".bmp", ".gif"}  # pictures the readers do not open: -> PNG (readable_picture)
DEEP_MODES = {"I;16", "I;16B", "I;16L", "I;16N", "I", "F"}  # > 8 bits per grey pixel
SVG_PDF_VERSION = 2  # in the cached file's name: a fix to the conversion never reuses an older copy
_IFC_SVG = re.compile(rb'class="[^"]*\bIfc[A-Z]')
_LEN = re.compile(r"^\s*([0-9.]+)\s*([a-z%]*)\s*$")
_PX_PER = {"": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0}


@dataclass
class Prepared:
    original: str  # the uploaded file
    path: str  # the file to solve
    kind: str = "as_is"  # as_is | dwg | dxf_roles | svg_pdf | heic | page | scan (an image-only PDF page, as PNG) |
    #                      shrink (an image above STOA_IMAGE_MAX_SIDE px, read from a smaller copy) | picture (a
    #                      TIFF / WebP / BMP / GIF or 16-bit grey PNG read as an 8-bit PNG, same pixels) | picture+shrink
    page: int = 0  # page of the ORIGINAL the solved file shows (multi-page PDFs)
    pages: int = 1
    notes: list[str] = field(default_factory=list)  # assumptions to show with the answer
    missing: list[str] = field(default_factory=list)  # what would make it answerable (failed conversion)
    error: str = ""  # user-readable reason the file could not be converted ("" = fine)
    svg_frame: tuple[float, float, float] = (0.0, 0.0, 1.0)  # generic SVG: (x0, y0, user units per PDF pt)
    scan_zoom: float = 0.0  # scan: PNG pixels per PDF point of the original page (0 = not a scan)
    img_scale: float = 0.0  # shrink: solved-image pixels per ORIGINAL image pixel (0 = not shrunk)
    sheet: str = ""  # page: the picked page's own sheet number ('M-101A'), when its title block prints one
    paper_f: float = 0.0  # paper: the solved PDF page is the original page enlarged this many times (0 = not)
    scan_pdf: str = ""  # scan: the one-page PDF whose page 0 was rendered to path (its page size / image pixels: paper)


def eight_bit(im):
    """(image, note): a grey picture with more than 8 bits per pixel (a scanner's 16-bit grey PNG / TIFF: mode 'I;16',
    'I', 'F') as 8-bit 'L', SCALED. Pillow 13's convert('L' / 'RGB') CLIPS instead (grey 50 of 255 stored as 12850 ->
    255): mid-grey ink, pencil lines and anti-aliased text vanish for every PIL reader, while cv2 scales (P1 c15). 12-bit
    data in a 16-bit file (max <= 4095) is scaled by 4095. Any other mode: (im, "") unchanged."""
    if im.mode not in DEEP_MODES:
        return im, ""
    import numpy as np
    from PIL import Image

    a = np.asarray(im).astype(np.float32)
    hi = float(np.nanmax(a)) if a.size else 0.0
    top = 65535.0 if hi > 4095 else 4095.0 if hi > 255 else 1.0 if (im.mode == "F" and hi <= 1.0) else 255.0
    out = Image.fromarray(np.clip(np.nan_to_num(a) * (255.0 / top) + 0.5, 0, 255).astype(np.uint8), "L")
    if "dpi" in im.info:
        out.info["dpi"] = im.info["dpi"]
    bits = "16-bit" if top > 4095 else "12-bit" if top > 255 else "high bit-depth"
    return out, f"{bits} grey picture: its greys were scaled to 8 bit before reading"


def is_deep(path: str | Path) -> bool:
    """Is this picture a grey image with more than 8 bits per pixel (see eight_bit)? False when unreadable."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as im:
            return im.mode in DEEP_MODES
    except Exception:  # noqa: BLE001
        return False


TRANSPARENT_BG_SHARE = 0.25  # a transparent BACKGROUND: >= this share of pixels with alpha < 128


@functools.lru_cache(maxsize=256)
def _transparent_share(path: str, mtime: float, size: int) -> float:
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        a = np.asarray(im.convert("RGBA"))[::2, ::2, 3]
    return float((a < 128).mean()) if a.size else 0.0


def needs_picture_png(path: str | Path) -> bool:
    """Should prepare() read this PNG from readable_picture's copy? 16-bit grey (eight_bit), or a TRANSPARENT background
    (F6 01:17: cv2.imread / convert("RGB") drop alpha, a CAD export's black-under-alpha-0 sheet reads as BLACK; the web
    upload flattens it, the CLI / API path did not): >= 25 % of the pixels transparent. Header only for opaque modes; the
    alpha check decodes once per file."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as im:
            mode, transp = im.mode, "transparency" in im.info
        if mode in DEEP_MODES:
            return True
        if mode in ("RGBA", "LA", "PA") or (mode == "P" and transp):
            # a transparent BACKGROUND, not a few clear pixels: 37 dataset CubiCasa PNGs have 0-11 % transparent pixels
            # and are read as they always were (their measured answers and path-keyed readers stay)
            st = Path(path).stat()
            return _transparent_share(str(path), st.st_mtime, st.st_size) >= TRANSPARENT_BG_SHARE
    except Exception:  # noqa: BLE001 — unreadable: the readers say so
        return False
    return False


def _side_cache(src: Path) -> Path:
    """$STOA_CACHE/formats/<hash of the path>: converted copies that must never appear next to an upload (the web's
    upload list shows that folder's files)."""
    root = Path(os.environ.get("STOA_CACHE", Path.home() / ".cache" / "stoa")) / "formats"
    d = root / hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def readable_picture(path: str | Path) -> tuple[str, str]:
    """(png, note): a PNG the image readers open, for TIFF / WebP / BMP / GIF pictures and 16-bit grey PNGs (the CLI /
    API path; the web upload converts the first four itself, web/server/normalize_upload.py): first page / frame,
    16-bit greys scaled (eight_bit), a transparent background on white, CMYK / palette / 1-bit as RGB / grey. The SAME
    pixel grid as the original (no EXIF turn, no resize), so evidence maps back 1:1. Cached in the side cache."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    src = Path(path)
    dst = _side_cache(src) / f"{src.stem}.png"
    with Image.open(src) as im:
        n = getattr(im, "n_frames", 1) or 1
        fmt = (im.format or src.suffix[1:]).upper()
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            im.seek(0)
            im.load()
            out, _ = eight_bit(im)
            if out.mode in ("RGBA", "LA", "PA") or (out.mode == "P" and "transparency" in out.info):
                rgba = out.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                bg.alpha_composite(rgba)
                out = bg.convert("RGB")
            elif out.mode not in ("RGB", "L"):
                out = out.convert("L" if out.mode in ("1", "L;16") else "RGB")
            keep = {}
            try:
                dpi = im.info.get("dpi")
                if dpi and float(dpi[0]) > 0 and float(dpi[1]) > 0:
                    keep = {"dpi": (float(dpi[0]), float(dpi[1]))}
            except (TypeError, ValueError, IndexError):
                keep = {}
            tmp = tmp_path(dst)
            out.save(tmp, "PNG", **keep)
            os.replace(tmp, dst)
        deep = im.mode in DEEP_MODES
    what = ("16-bit grey PNG" if deep else "PNG with a transparent background (filled with white)") if fmt == "PNG" \
        else f"{fmt} picture" + (" (16-bit greys)" if deep else "")
    note = (f"{what} read as an 8-bit PNG copy" + (" (greys scaled, not clipped to white)" if deep else "")
            + (f"; page 1 of {n} read, the other pages were not" if n > 1 and fmt in ("TIFF", "MPO") else ""))
    return str(dst), note


def _cache_dir(src: Path) -> Path:
    if os.access(src.parent, os.W_OK):
        return src.parent
    root = Path(os.environ.get("STOA_CACHE", Path.home() / ".cache" / "stoa")) / "formats"
    d = root / hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_ifc_svg(path: str | Path, head: int = 4_000_000) -> bool:
    """IfcConvert-style SVG (groups with class="IfcSpace|cut IfcDoor|…") -> svg_ifc reads it."""
    try:
        with open(path, "rb") as f:
            return bool(_IFC_SVG.search(f.read(head)))
    except OSError:
        return False


def _svg_px(value: str | None) -> float | None:
    m = _LEN.match(value or "")
    if not m or m.group(2) not in _PX_PER:
        return None
    return float(m.group(1)) * _PX_PER[m.group(2)]


def svg_user_box(path: str | Path) -> tuple[float, float, float] | None:
    """(x0, y0, width) of the SVG's user coordinate system: the viewBox, else (0, 0, width in CSS px)."""
    from xml.etree import ElementTree as ET

    try:
        for _, el in ET.iterparse(str(path), events=("start",)):
            vb = (el.get("viewBox") or "").replace(",", " ").split()
            if len(vb) == 4 and float(vb[2]) > 0:
                return float(vb[0]), float(vb[1]), float(vb[2])
            w = _svg_px(el.get("width"))
            return (0.0, 0.0, w) if w else None
    except Exception:  # noqa: BLE001
        return None
    return None


_GLYPH_XY = re.compile(r'(\s[xy]=")\s*([-+0-9.eE]+)(?:[\s,]+[-+0-9.eE]+)+\s*"')


def svg_text_fix(path: str | Path) -> bytes:
    """The SVG with per-glyph x/y lists cut to their first value (text runs keep their start point).

    MuPDF misplaces a <text>/<tspan> whose x="x1 x2 x3 …" lists one position per glyph when the element also has a
    translating transform: the glyphs land off the page and are dropped. An IFC plan sheet exported to SVG came back
    with 217 of 1,679 characters (every room label lost), so plan_rooms found no labels. Glyph spacing then comes
    from the font, which is enough to read and locate the text."""
    raw = Path(path).read_bytes()
    return _GLYPH_XY.sub(lambda m: f'{m.group(1)}{m.group(2)}"', raw.decode("utf-8", "replace")).encode("utf-8")


def tmp_path(dst: Path) -> Path:
    """A private temp name next to dst for an atomic write (write it, then os.replace(tmp, dst)): readers never see a
    half-written cache file. Per process AND thread (eval workers and the UI's warm + first question are threads of
    one process); keeps dst's suffix so PIL / PyMuPDF pick the same format."""
    dst = Path(dst)
    return dst.with_name(f".{dst.stem}.{os.getpid()}-{threading.get_ident()}.tmp{dst.suffix}")


def svg_to_pdf(path: str | Path, out_dir: str | Path | None = None) -> tuple[str, tuple[float, float, float]]:
    """(PDF path, (x0, y0, SVG user units per PDF pt)). Vector content and text survive (MuPDF's SVG reader)."""
    import pymupdf

    src = Path(path)
    dst = Path(out_dir or _cache_dir(src)) / f"{src.stem}.svg-v{SVG_PDF_VERSION}.pdf"
    with pymupdf.open(stream=svg_text_fix(src), filetype="svg") as doc:
        width_pt = doc[0].rect.width
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            pdf = pymupdf.open("pdf", doc.convert_to_pdf())
            tmp = tmp_path(dst)
            pdf.save(tmp, garbage=3, deflate=True)
            os.replace(tmp, dst)
    x0, y0, w = svg_user_box(src) or (0.0, 0.0, width_pt)
    return str(dst), (x0, y0, w / width_pt if width_pt else 1.0)


def _invisible_share(pg) -> float:
    """Share of the page's text spans drawn invisibly (render mode 3 = an OCR layer over a scan)."""
    try:
        tr = pg.get_texttrace()
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(1 for t in tr if t.get("type") == 3) / len(tr) if tr else 0.0


def ocr_layer_scan(pg) -> bool:
    """X1 c13: a scanned page carrying a scanner's OCR layer: one image over >= 50 % of the page, >= 80 % of the text
    invisible (render mode 3), <= 50 vector drawings. Such a page is read like an image-only scan (its layer ignored).
    STOA_SCAN_OCR_LAYER=0 turns the rule off."""
    if os.environ.get("STOA_SCAN_OCR_LAYER", "1") == "0" or _invisible_share(pg) < 0.8:
        return False
    try:
        area = max(pg.rect.get_area(), 1e-9)
        big = any((r & pg.rect).get_area() >= 0.5 * area for im in pg.get_images(full=True)
                  for r in pg.get_image_rects(im[0]))
        return big and len(pg.get_drawings()) <= 50
    except Exception:  # noqa: BLE001
        return False


def scan_png(pdf: str, max_side: int = 6000) -> tuple[str, float] | None:
    """(PNG path, px per pt) when page 0 is a scan: (almost) no text, no vector paths, one image over >= 50% of the
    page. Rendered at the image's own resolution (capped at max_side px), so no detail is lost or invented."""
    import pymupdf

    from stoa.formats.pages import _stream_bytes

    with pymupdf.open(pdf) as d:
        pg = d[0]
        imgs = pg.get_images(full=True)  # cheap checks first: most uploads are vector sheets
        txt = (pg.get_text() or "").strip()
        # X1 c13: a scanner's OCR layer (invisible text, render mode 3: office 'searchable PDF', Adobe Scan, archive
        # scans) made every such page a 'vector' page rendered at ~130 dpi with the layer as its text; it is a scan
        ocr = bool(imgs) and len(txt) >= 20 and ocr_layer_scan(pg)
        if not imgs or pg.rotation or (not ocr and (_stream_bytes(pg) > 20_000 or len(txt) >= 20)):
            return None
        best, px = 0.0, 0
        for im in imgs:
            for r in pg.get_image_rects(im[0]):
                cover = (r & pg.rect).get_area() / max(pg.rect.get_area(), 1e-9)
                if cover > best:
                    best, px = cover, im[2] / max(r.width, 1e-9)  # image px per pt where it is drawn
        if best < 0.5:
            return None
        zoom = max(1.0, min(px, max_side / max(pg.rect.width, pg.rect.height)))
        dst = Path(_cache_dir(Path(pdf))) / f"{Path(pdf).stem}.scan.png"
        # NOT tagged with its dpi (P1 16:40): a scan's PDF page is often a reduced copy of the drawing (A1 scanned to
        # A4), so 'printed 1:100 + page size' would be wrong; a drawn scale bar shrinks with the copy and stays right.
        # Whole-number dpi resampling also moved scan counts on up_fmt3 (0 W / 0 L, values jitter): kept as it was.
        _render(pg, zoom, dst, pdf, tag=False)
    _scan_sidecar(dst, pdf, zoom)
    return str(dst), zoom


def _scan_sidecar(png: Path, pdf: str, zoom: float) -> None:
    """'<render>.json' {scan_pdf, zoom}: which scanned PDF page this render shows, so a reader that only has the render
    (openings, takeoff, upload) can read the paper from the scan's own pixels (paper.paper_of_image -> paper_of_scan)."""
    import json

    side = Path(f"{png}.json")
    want = {"scan_pdf": str(Path(pdf).resolve()), "zoom": zoom}
    try:
        if json.loads(side.read_text()) == want:
            return
    except (OSError, ValueError):
        pass
    try:
        tmp = side.with_name(f".{side.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(want))
        os.replace(tmp, side)
    except OSError:
        pass


def render_png(pdf: str, dpi: int = 0, max_side: int = 4500) -> tuple[str, float]:
    """(PNG path, px per pt) of page 0 rendered at `dpi` (long side capped at max_side px). For VECTOR sheets that no
    PDF reader can read (text drawn as outlines, no printed scale, …): the image readers / VLM read the picture.
    dpi 0 = 200 when the page has no text layer (OCR is then the only source of text: O1 14:43, X1's outlined Italian
    plan read '1749' at 200 dpi, not at 150), else 150."""
    import pymupdf

    with pymupdf.open(pdf) as d:
        pg = d[0]
        if not dpi:
            dpi = 150 if len((pg.get_text() or "").strip()) >= 20 else 200
        zoom = min(dpi / 72, max_side / max(pg.rect.width, pg.rect.height, 1e-9))
        # NOT snapped to a whole dpi (P1 16:58): a capped A1 render went 136 -> 135 dpi, which changed a whole-image
        # LoRA count (6 -> 4 @0.3) and every cached reading of the old render; the tag is round(72 * zoom) (<= 0.4 %)
        dst = Path(_cache_dir(Path(pdf))) / f"{Path(pdf).stem}.r{round(72 * zoom)}.png"
        _render(pg, zoom, dst, pdf)
    return str(dst), zoom


_OCR_WORD = None


def junk_ocr_layer(pg) -> bool:
    """X1 c13: a scanner's OCR layer (INVISIBLE text, render mode 3, over the scan) that is mostly garbage ('./•',
    '«Si,:,', 'k4:') passes every per-character check (ordinary letters and punctuation) and holds none of the printed
    numbers; used as 'the PDF's own text' it stops our OCR and leaves the VLM nothing to cite (a 1940s Nordic scan PDF:
    0 / 7 lengths). Junk = >= 80 % of the text is invisible and < 30 % of its words look like words or numbers (junk
    layers 0.05-0.11; real sheet text 0.23-0.86; visible text is never judged)."""
    import re

    global _OCR_WORD
    if _OCR_WORD is None:
        _OCR_WORD = re.compile(r"^(?:[A-Za-zÄÖÅÜäöåüßÉéÈèÀà]{3,}|\d+(?:[.,]\d+)?(?:m2|m²|mm|cm|m)?|\d{1,3}(?:,\d{3})+|"
                               r"[A-Z]{1,3}[-.]?\d{1,4}[A-Z]?)[.,:;]?$")
    try:
        tr = pg.get_texttrace()
    except Exception:  # noqa: BLE001 - an old PyMuPDF: judge nothing
        return False
    if not tr or sum(1 for s in tr if s.get("type") == 3) < 0.8 * len(tr):
        return False
    if os.environ.get("STOA_JUNK_OCR", "1") == "all" and _scan_page(pg):
        # X1 c13 (USDA 1966 plan, Internet Archive scan): a GOOD scanner OCR layer reads the prose but none of the plan's
        # labels / dimensions; scanner OCR is made for prose, ours (tiled) for drawings -> any invisible layer on a scan
        return True
    words = [w[4] for w in pg.get_text("words")]
    return len(words) >= 20 and sum(1 for w in words if _OCR_WORD.match(w)) < 0.3 * len(words)


def _scan_page(pg) -> bool:
    """One raster image covers >= 80 % of the page (a scanned sheet)."""
    area = abs(pg.rect) or 1.0
    try:
        return any(abs(r & pg.rect) >= 0.8 * area for img in pg.get_images(full=True)
                   for r in pg.get_image_rects(img[0]))
    except Exception:  # noqa: BLE001
        return False


def seed_text_layer(pdf: str, png: str, zoom: float, min_chars: int = 20) -> int:
    """Give the image readers the PDF's OWN words for a render of its page 0 (S0 16:48: Stoa's S-100F A1 sheet, read as
    an image at 135 dpi, made PP-OCR run 10+ min per question, 3 at once, on a page whose text layer was already
    there). Lines (spans joined, as pdf_textqa) scaled to render pixels go into raster.textlayer's in-process caches
    for this PNG, so text_layer(png) returns them instead of OCR / a VLM reading. Pages with no text layer (outlined
    text) or a junk one (custom font encodings: mostly control characters) are left to OCR. -> items seeded."""
    import pymupdf

    from stoa.solvers.raster import textlayer as T

    with pymupdf.open(pdf) as d:
        pg = d[0]
        raw = pg.get_text() or ""
        if len(raw.strip()) < min_chars:
            return 0
        printable = sum(ch.isprintable() or ch in "\n\t" for ch in raw)
        if printable < 0.97 * len(raw):
            return 0
        if os.environ.get("STOA_JUNK_OCR", "1") != "0" and junk_ocr_layer(pg):
            return 0  # a scanner's junk OCR layer: our OCR reads the scan instead
        items = []
        for b in pg.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                t = " ".join(sp["text"].strip() for sp in ln.get("spans", []) if sp["text"].strip())
                if t:
                    x0, y0, x1, y1 = ln["bbox"]
                    items.append(T.TextItem(t, [x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom]))
    from PIL import Image

    with Image.open(png) as im:
        size = im.size
    tl = T.TextLayer(path=png, size=size, items=items, notes=["text layer = the PDF's own text (exact; no OCR)"])
    tag = T.file_tag(png)
    with T._glock:
        T._mem[f"{png}{tag}|ocr"] = tl  # what text_layer() checks first (OCR engine, backend 'local')
        for backend in ("local", "api", "gemini", "nvidia"):
            T._mem[f"{png}{tag}|{backend}|1100|{T._api_mode()}"] = tl  # default tile; the VLM-reading keys
    return len(items)


def shrink_image(src: str | Path, dst: str | Path, max_side: int = 6000) -> dict:
    """Scale a large image down to `max_side` px (long side) so that it is still the same KIND of image to the readers.

    P1 17:15: the web upload scales images above 6000 px down with LANCZOS (web/server/normalize_upload.py). On Stoa's
    clean CAD sheets that blurs every 1-px line into a ramp of grey levels, and the clean-render gate
    (raster.windows.clean_render: >= 70 % pure white AND the 8 commonest grey levels >= 45 % of the ink) then calls
    2 of the 4 example sheets a SCAN (S-100F top-8 share 0.54 -> 0.22, M-101A 0.57 -> 0.24): their text goes to
    PP-OCR on CPU instead of the VLM and the symbol readers switch off. So when the ORIGINAL passes the gate and the
    plain resize does not, the copy gets near-white (>= 240) snapped to white and its other levels rounded to 8 steps
    per channel (top-8 share 1.0); everything else (photos, scans, sheets that stay clean) is only resized. DPI (if tagged) is scaled with the pixels, so DPI x pixels still gives
    the sheet size. -> {"path", "factor", "size", "clean", "note"}; factor 1.0 = nothing done (dst not written)."""
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # Stoa's sheets are 70-140 MP; A0 at 400 dpi would hit PIL's 179 MP 'bomb' error
    with Image.open(src) as im:
        im.load()
        W, H = im.size
        dpi = im.info.get("dpi")
        if max(W, H) <= max_side:
            return {"path": str(src), "factor": 1.0, "size": (W, H), "clean": None, "note": ""}
        f = max_side / max(W, H)
        im, _ = eight_bit(im)  # P1 c15: a 16-bit grey scan; convert("RGB") below would clip its greys to white
        if im.mode in ("RGBA", "LA", "P"):  # transparent pixels -> white paper
            rgba = im.convert("RGBA")
            im2 = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im2.alpha_composite(rgba)
            im2 = im2.convert("RGB")
        else:
            im2 = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
    from stoa.solvers.raster.windows import CLEAN_TOP8, CLEAN_WHITE

    def gate(img) -> bool:  # raster.windows.clean_render on every 4th pixel (same verdict, 4x cheaper)
        g = np.asarray(img.convert("L"))[::2, ::2]
        nw = g[g < 250]
        top = float(np.sort(np.unique(nw, return_counts=True)[1])[::-1][:8].sum() / nw.size) if nw.size else 1.0
        return float((g >= 250).mean()) >= CLEAN_WHITE and top >= CLEAN_TOP8

    clean = gate(im2)
    out = im2.resize((round(W * f), round(H * f)), Image.LANCZOS)
    flat = clean and not gate(out)  # only where the plain resize changes the kind: flat levels also change routing
    if flat:  # (P1 17:22: flattening the still-clean AFCEC copy made it a 0.97 'CubiCasa plan' -> room graph '0 m2')
        a = np.asarray(out).astype(np.int16)
        a = np.where(a >= 240, 255, (a // 32) * 32 + 16).clip(0, 255).astype(np.uint8)
        out = Image.fromarray(a)
    keep = {}
    try:
        if dpi and float(dpi[0]) > 0 and float(dpi[1]) > 0:
            keep = {"dpi": (float(dpi[0]) * f, float(dpi[1]) * f)}
    except (TypeError, ValueError, IndexError):
        keep = {}
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path(Path(dst))  # S0 17:42: a reader saw a half-written copy
    if Path(dst).suffix.lower() in (".jpg", ".jpeg"):
        from stoa.formats.paper import SRC_SIZE_KEY

        out.convert("RGB").save(tmp, format="JPEG", quality=94, comment=f"{SRC_SIZE_KEY}={W}x{H}", **keep)
    else:
        from PIL import PngImagePlugin

        from stoa.formats.paper import SRC_SIZE_KEY

        info = PngImagePlugin.PngInfo()
        info.add_text(SRC_SIZE_KEY, f"{W}x{H}")  # the export's pixel size = its paper at a usual dpi (paper_of_image)
        out.save(tmp, format="PNG", pnginfo=info, **keep)
    os.replace(tmp, dst)
    note = f"large image ({W}×{H} px) read at {out.width}×{out.height} px"
    if flat:
        note += " (a clean drawing: kept to flat grey levels)"
    return {"path": str(dst), "factor": f, "size": out.size, "clean": clean, "flat": flat, "note": note}


def paper_copy(pdf: str) -> tuple[str, float, str] | None:
    """(path, factor, note) of a one-page PDF enlarged to the paper its printed scale was written for ('1:200 @ A1' on an
    A3 page -> the page drawn 2.0x on A1, cached as <stem>.paperA1.pdf), so every reader's printed 1:N is true on the
    copy; None when the page's paper and its printed scale agree (P1 17:50; D1 16:50: EFA secondary sheets' filled
    rooms match their printed areas 67/76 at 1:400 vs 0-1 at the printed 1:200)."""
    import pymupdf

    from stoa.formats.paper import scale_for_other_paper

    try:
        with pymupdf.open(pdf) as d:
            if d.page_count != 1:
                return None
            pg = d[0]
            hit = scale_for_other_paper(pg.get_text() or "", pg.rect.width, pg.rect.height)
            if hit is None:
                return None
            f, named, page, n = hit
            rot = pg.rotation % 360
            # X1/E1 18:45-18:52: show_pdf_page draws a /Rotate page AS DISPLAYED, so the copy of a rotated sheet had
            # sideways text in its own frame and every vector reader failed (3/3 rows lost). The copy keeps the source's
            # unrotated frame (x f) and the same /Rotate: readers and pdf_pt evidence (x 1/f) see what the original has
            out = _cache_dir(Path(pdf)) / f"{Path(pdf).stem}.paper{named}{f'.rot{rot}' if rot else ''}.pdf"
            if not out.exists() or out.stat().st_mtime < Path(pdf).stat().st_mtime:
                srcd = d
                if rot:
                    srcd = pymupdf.open(pdf)
                    srcd[0].set_rotation(0)
                sp = srcd[0]
                nd = pymupdf.open()
                np_ = nd.new_page(width=sp.rect.width * f, height=sp.rect.height * f)
                np_.show_pdf_page(np_.rect, srcd, 0)
                if rot:
                    np_.set_rotation(rot)
                tmp = tmp_path(out)
                nd.save(tmp, garbage=3, deflate=True)
                nd.close()
                if srcd is not d:
                    srcd.close()
                os.replace(tmp, out)
    except Exception:  # noqa: BLE001 — the page as it is
        return None
    return (str(out), f, f"the sheet prints its scale for {named} ('1:{n} @ {named}') but this PDF page is {page}: the "
                         f"drawing is at 1:{round(n * f)} on this file; read on the page enlarged {f:.2f}x to {named}, "
                         f"where 1:{n} holds")


SHRINK_VERSION = 2  # bump when shrink_image's output changes: older cached copies are rebuilt (H / X1 18:41-18:47:
# copies made before the PNG size tag were reused by every gate). 1 = cb0fc1d (no tag), 2 = 33016c6+ (size tag)
IMAGE_MAX_SIDE = 6000  # = the web upload's cap (web/server/normalize_upload.py), so CLI / API / evals read what the UI reads


def shrunk_copy(src: Path, side: int) -> tuple[str, float, str] | None:
    """(path, scale, note) of `src` shrunk to `side` px (cached next to it as <stem>.s<side>.png|.jpg with a .json
    sidecar), or None when the image is small enough / unreadable."""
    import json

    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(src) as im:
            W, H = im.size
    except Exception:  # noqa: BLE001 — unreadable: the readers say so
        return None
    if max(W, H) <= side:
        return None
    ext = ".jpg" if src.suffix.lower() in (".jpg", ".jpeg") else ".png"
    dst = _cache_dir(src) / f"{src.stem}.s{side}{ext}"
    meta = dst.with_name(dst.name + ".json")

    def cached():
        try:
            if dst.exists() and meta.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                m = json.loads(meta.read_text())
                if m.get("src_size") == [W, H] and m.get("v", 1) == SHRINK_VERSION:
                    return str(dst), float(m["factor"]), m["note"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    if (hit := cached()) is not None:
        return hit
    import fcntl

    # one writer per copy across threads AND processes (S0 17:42: 4 eval workers on one sheet; the API with 2 questions):
    # the others wait here, then find the finished copy (written to a temp name + os.replace, meta last)
    with open(dst.with_name(f".{dst.name}.lock"), "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            if (hit := cached()) is not None:
                return hit
            r = shrink_image(src, dst, side)
            if r["factor"] >= 1.0:
                return None
            try:
                carry_text_layers(src, Path(r["path"]), r["factor"])
            except Exception:  # noqa: BLE001 — the copy is then read like any new image
                pass
            try:
                tmpm = tmp_path(meta)
                tmpm.write_text(json.dumps({"v": SHRINK_VERSION, "src_size": [W, H], "factor": r["factor"],
                                            "note": r["note"], "clean": r["clean"], "flat": r.get("flat"),
                                            "src": str(src)}))  # P1: full_resolution() finds the original
                os.replace(tmpm, meta)
            except OSError:
                pass
            return r["path"], r["factor"], r["note"]
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def _img_size(p: Path) -> tuple[int, int] | None:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(p) as im:  # header only: no pixels decoded
            return im.size
    except Exception:  # noqa: BLE001
        return None


def full_resolution(path: str | Path) -> tuple[str, float]:
    """(original, k) for an image the readers see SHRUNK (captain 23:04, Ziying: door arcs on Stoa's 10,200 px AFCEC A-2
    are a few px wide on the 6,000 px copy): k = original px per px of `path` (>= 1; a box in `path`'s px x k = the same
    box on the original). Sources, in order: the cache copy's sidecar '<copy>.json' {"src"} (shrunk_copy), the copy
    next to its original ('<stem>.s6000.png' <- '<stem>.png' / .jpg), the web upload's '<stem>.full<ext>' sibling (the
    upload is shrunk in place; also one folder up for style-tagged copies), each checked against the PNG's
    'stoa_src_size' tag when it has one and against the aspect ratio. Else (path, 1.0): the image is its own original.
    Use it for COUNTING small symbols (doors, fixtures, tags) on a region (crop_full); keep `path` for overview reads."""
    p = Path(path)
    me = _img_size(p)
    if me is None:
        return str(p), 1.0
    tag = None
    try:
        from PIL import Image

        with Image.open(p) as im:
            raw = str(im.info.get("stoa_src_size") or "")
        if re.fullmatch(r"\d+x\d+", raw):
            tag = tuple(int(v) for v in raw.split("x"))
    except Exception:  # noqa: BLE001
        pass
    cands: list[Path] = []
    try:
        meta = p.with_name(p.name + ".json")
        if meta.exists():
            src = json.loads(meta.read_text()).get("src")
            if src:
                cands.append(Path(src))
    except (OSError, ValueError, AttributeError):
        pass
    m = re.fullmatch(r"(.+)\.s\d{3,5}", p.stem)
    if m:
        cands += [p.with_name(m.group(1) + ext) for ext in (".png", ".jpg", ".jpeg", ".PNG", ".JPG", ".tif", ".tiff")]
    for d in (p.parent, p.parent.parent):
        cands.append(d / f"{p.stem}.full{p.suffix}")
    for c in cands:
        if c == p or not c.exists():
            continue
        sz = _img_size(c)
        if not sz or max(sz) <= max(me):
            continue
        if tag and tuple(sz) != tag:
            continue  # another file that happens to share the name
        k = max(sz) / max(me)
        if abs(sz[0] / sz[1] - me[0] / me[1]) > 0.01 * (me[0] / me[1]):  # not the same picture
            continue
        return str(c), round(k, 6)
    return str(p), 1.0


def crop_full(path: str | Path, box, pad: float = 0.0, max_side: int | None = None) -> dict:
    """Crop `box` = (x0, y0, x1, y1) in `path`'s px (= the evidence space the web draws on) from the FULL-resolution
    original, for counting small symbols on one region (a wing from stoa.sheet.keyplan.zone_for, a room, a quarter).
    pad = extra margin in `path`'s px; max_side caps the crop (then k is smaller). The crop is cached in
    $STOA_CACHE/crops/<hash of the original>/'<stem>.crop-<x0>-<y0>-<x1>-<y1>-<k>.png' with the original's DPI tag.
    -> {"path": crop file, "k": crop px per `path` px, "offset": (x0, y0) in `path`'s px, "size": (w, h) crop px,
        "full": the original, "note": one line for a trace}. A point (u, v) of the crop is (x0 + u / k, y0 + v / k) in
    `path`'s px (to_path_px). With no larger original the crop is cut from `path` itself (k = 1, said in the note)."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    full, k = full_resolution(path)
    me = _img_size(Path(path)) or (0, 0)
    a, b, c_, d = (float(v) for v in box)
    x0, y0 = max(0.0, min(a, c_) - pad), max(0.0, min(b, d) - pad)
    x1, y1 = max(a, c_) + pad, max(b, d) + pad
    if me[0] and me[1]:
        x1, y1 = min(float(me[0]), x1), min(float(me[1]), y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise ValueError(f"empty crop box {box!r} on a {me[0]}x{me[1]} image")
    kk = k
    if max_side and max(x1 - x0, y1 - y0) * k > max_side:
        kk = max_side / max(x1 - x0, y1 - y0)
    src = Path(full)
    # never next to the upload: a file there is listed as another sheet of it (the web hides only known derived names)
    cdir = Path(os.environ.get("STOA_CACHE", Path.home() / ".cache" / "stoa")) / "crops" / hashlib.sha1(
        str(src.resolve()).encode()).hexdigest()[:12]
    cdir.mkdir(parents=True, exist_ok=True)
    out = cdir / f"{src.stem}.crop-{round(x0)}-{round(y0)}-{round(x1)}-{round(y1)}-{kk:.3f}.png"
    with Image.open(src) as im:
        dpi = im.info.get("dpi")
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            c = im.crop((round(x0 * k), round(y0 * k), round(x1 * k), round(y1 * k)))
            c, _ = eight_bit(c)  # P1 c15: a 16-bit grey original (the web's '.full.png'); convert("RGB") would clip it
            if kk < k:
                c = c.resize((max(1, round((x1 - x0) * kk)), max(1, round((y1 - y0) * kk))), Image.LANCZOS)
            if c.mode not in ("RGB", "L", "RGBA", "LA", "1"):
                c = c.convert("RGB")
            tmp = tmp_path(out)
            kw = {"dpi": (dpi[0] * kk / k, dpi[1] * kk / k)} if dpi and kk != k else ({"dpi": dpi} if dpi else {})
            c.save(tmp, format="PNG", **kw)
            os.replace(tmp, out)
    with Image.open(out) as im:
        size = im.size
    note = (f"counted on a crop of the full-resolution original ({Path(full).name}, {kk:.2f} px per displayed px)"
            if kk > 1.0 else "counted on a crop of the image itself (no larger original kept)")
    return {"path": str(out), "k": kk, "offset": (x0, y0), "size": size, "full": full, "note": note}


def to_path_px(crop: dict, pts):
    """Points / boxes of a crop_full() crop back in the shrunk image's px: [(u, v), ...] or [(u0, v0, u1, v1), ...]."""
    (x0, y0), k = crop["offset"], float(crop["k"]) or 1.0
    out = []
    for q in pts:
        q = [float(v) for v in q]
        out.append(tuple(round((x0 if i % 2 == 0 else y0) + v / k, 2) for i, v in enumerate(q)))
    return out


def carry_text_layers(src: Path, dst: Path, factor: float) -> list[str]:
    """Text layers already read on the full-size image (raster VLM layer, OCR layer), boxes x factor, saved as the smaller
    copy's own cached layers, so a sheet read before is not read again at 6000 px (S0 17:42: cold copies made S-100F
    rows 0.5 -> 96 s and label_count skip 'a large image whose text has not been read yet'). -> layers carried."""
    import json

    from stoa.solvers.raster import textlayer as T

    done = []
    for backend in ("local",):
        fs, fd = T._disk_key(str(src), 1100, backend), T._disk_key(str(dst), 1100, backend)
        if fs.exists() and not fd.exists():
            d = json.loads(fs.read_text())
            d["path"] = str(dst)
            d["size"] = [round(v * factor) for v in d.get("size", [0, 0])]
            for it in d.get("items", []):
                it["box"] = [round(v * factor, 1) for v in it["box"]]
            d["notes"] = [*d.get("notes", []), f"carried over from the full-size image's reading (boxes x{factor:.4f})"]
            tmp = tmp_path(fd)
            tmp.write_text(json.dumps(d))
            os.replace(tmp, fd)
            done.append(f"vlm:{backend}")
    try:
        from stoa import ocr

        os_, od = ocr._key(str(src)), ocr._key(str(dst))
        if os_.exists() and not od.exists():
            d = json.loads(os_.read_text())
            d["size"] = [round(v * factor) for v in d.get("size", [0, 0])]
            for it in d.get("items", []):
                it["box"] = [round(v * factor, 1) for v in it["box"]]
            tmp = tmp_path(od)
            tmp.write_text(json.dumps(d))
            os.replace(tmp, od)
            done.append("ocr")
    except Exception:  # noqa: BLE001
        pass
    return done


def _png_dpi(path: Path) -> float | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return float((im.info.get("dpi") or (0,))[0]) or None
    except Exception:  # noqa: BLE001 — unreadable cache file: render again
        return None


def _render(pg, zoom: float, dst: Path, pdf: str, tag: bool = True) -> None:
    """Render page `pg` at `zoom` to dst (cached by mtime), TAGGED with its real resolution (72 * zoom dpi): the image
    then covers the PDF's own paper size, so O1's scalebar.printed_bar turns a printed '1:100' into an exact px/m
    (O1 15:47). PyMuPDF's default tag is 96 dpi whatever the zoom; a cached file with a wrong tag is rendered again."""
    import pymupdf

    dpi = round(72 * zoom)
    if dst.exists() and dst.stat().st_mtime >= Path(pdf).stat().st_mtime and \
            (not tag or abs((_png_dpi(dst) or 0) - dpi) <= 1):
        return
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    if tag:
        pix.set_dpi(dpi, dpi)
    # X1 19:24 (gate d25 sc-110): pix.save wrote dst in place, so a second question on the same upload opened a
    # half-written 22 MB scan.png ('cannot identify image file' in raster AND vlm_direct -> the dataset prior)
    tmp = tmp_path(dst)
    pix.save(tmp)
    os.replace(tmp, dst)


def has_text(pdf: str, min_chars: int = 20) -> bool:
    import pymupdf

    with pymupdf.open(pdf) as d:
        return len((d[0].get_text() or "").strip()) >= min_chars


_TRACED_NAME = re.compile(r"\.vec\d+\.pdf$", re.I)
NATIVE_MIN_STREAM = 20_000  # bytes of page content: a CAD export draws its lines there (stoa-like: 111 kB - 3.3 MB)
_NATIVE: dict = {}
_NATIVE_LOCK = threading.Lock()


def native_vector_pdf(path: str | Path, page: int = 0) -> bool:
    """True when this PDF page is a NATIVE vector drawing (a CAD / Revit export: lines in the page content, a real text
    layer), False for a scan (an image over half the page, with or without an OCR text layer), for one of our traced
    copies of a picture (<stem>.vecN.pdf, producer stoa.formats.vectorize) and on any read error. Cheap (<= 0.1 s on
    Stoa's sheets), memoised by path + mtime. For callers deciding whether a hosted cross-check is worth paying for:
    a located reading from a native vector page is its own evidence (P1 21:25, KNOWLEDGE)."""
    p = str(path)
    if not p.lower().endswith(".pdf") or _TRACED_NAME.search(p):
        return False
    try:
        key = (p, int(page), os.stat(p).st_mtime_ns)
    except OSError:
        return False
    with _NATIVE_LOCK:
        if key in _NATIVE:
            return _NATIVE[key]
    ok = False
    try:
        import pymupdf

        with pymupdf.open(p) as d:
            if "stoa.formats.vectorize" not in str((d.metadata or {}).get("producer") or "") and 0 <= page < len(d):
                pg = d[page]
                area = abs(pg.rect) or 1.0
                cover = max([abs(pymupdf.Rect(i["bbox"]) & pg.rect) / area for i in pg.get_image_info()] or [0.0])
                stream = sum(len(d.xref_stream(x) or b"") for x in pg.get_contents())
                if stream < NATIVE_MIN_STREAM:  # our paper / page copies draw the original page as a Form XObject
                    stream += sum(len(d.xref_stream(x[0]) or b"") for x in pg.get_xobjects())
                ok = cover < 0.5 and stream >= NATIVE_MIN_STREAM and len((pg.get_text() or "").strip()) >= 20
    except Exception:  # noqa: BLE001 — unreadable: not known to be native
        ok = False
    with _NATIVE_LOCK:
        _NATIVE[key] = ok
    return ok


def heic_to_jpg(path: str | Path, out_dir: str | Path | None = None) -> str:
    """Upright JPEG (q 92) of an iPhone HEIC photo. RuntimeError (user-readable) without a HEIF decoder."""
    from PIL import Image, ImageOps

    try:
        from pi_heif import register_heif_opener
    except ImportError:
        try:
            from pillow_heif import register_heif_opener
        except ImportError as e:
            raise RuntimeError("This is an iPhone HEIC photo and the server has no HEIC decoder. Please export it "
                               "as JPEG (iPhone: Settings > Camera > Formats > Most Compatible) or take a "
                               "screenshot.") from e
    register_heif_opener()
    src = Path(path)
    dst = Path(out_dir or _cache_dir(src)) / f"{src.stem}.jpg"
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        with Image.open(src) as im:
            tmp = tmp_path(dst)
            ImageOps.exif_transpose(im).convert("RGB").save(tmp, "JPEG", quality=92)
            os.replace(tmp, dst)
    return str(dst)


def user_message(e: Exception, ext: str) -> str:
    """A sentence the visitor can act on (no paths, no tracebacks); our own converter errors already are one."""
    from stoa.formats.dwg import DwgConvertError, DwgConverterMissing

    if isinstance(e, (DwgConverterMissing, DwgConvertError)) or (isinstance(e, RuntimeError) and "HEIC" in str(e)):
        return str(e)
    kind = (ext or "").lstrip(".").upper() or "file"
    return (f"the {kind} file could not be opened (it may be damaged, empty or not really a {kind}; "
            f"{type(e).__name__}): please export it again, or upload the drawing as PDF or PNG")


def without_sheet(question: str, sheet: str) -> str:
    """The question minus its reference to `sheet` ('(sheet M-101A)', 'On sheet M-101A:', 'on sheet M-101A'): once that
    sheet's page is picked from a set, the readers see a one-page drawing, and 'sheet M-101A' in the question sent
    the intent router to the sheet-table reader (P1 17:46: 6 of 16 Stoa questions lost on the whole USACE sets)."""
    if not sheet:
        return question
    s = re.escape(sheet)
    q = re.sub(rf"\s*\(\s*(?:on\s+|see\s+)?sheet\s+{s}\s*\)", "", question, flags=re.I)
    q = re.sub(rf"^\s*on\s+sheet\s+{s}\s*[:,\-–]\s*", "", q, flags=re.I)
    q = re.sub(rf"\b(?:on|in|of|from)\s+sheet\s+{s}\b", "on the drawing", q, flags=re.I)
    q = q.strip()
    return (q[:1].upper() + q[1:]) if q else question


def prepare(path: str, question: str = "") -> Prepared:
    """What to solve for this upload (never raises: a failed conversion keeps the original + error/missing)."""
    from stoa.data import abspath

    p = Path(path)
    ext = p.suffix.lower()
    prep = Prepared(original=path, path=path)
    try:
        src = abspath(path)
    except Exception:  # noqa: BLE001
        src = p
    if not Path(src).exists():
        return prep
    try:
        if ext == ".dwg":
            from stoa.formats.dwg import dwg_version, to_dxf

            prep.path, prep.kind = to_dxf(src, _cache_dir(Path(src))), "dwg"
            prep.notes.append(f"DWG (AutoCAD {dwg_version(src) or '?'}) converted to DXF before reading; "
                              "geometry and texts as the converter read them")
        elif ext == ".dxf":
            from stoa.formats.dxflayers import (NO_ROLES_MISSING, block_counts, blocks_missing, has_roles, relayer,
                                                relayer_note)

            got = relayer(src, _cache_dir(Path(src)))
            if got:
                prep.path, mapping = got
                prep.kind = "dxf_roles"
                prep.notes.append(relayer_note(mapping))
            blocks = block_counts(prep.path) if prep.kind == "dxf_roles" or has_roles(src) else {}
            if blocks:
                prep.missing.append(blocks_missing(blocks))
            if not got and not has_roles(src):
                prep.missing.append(NO_ROLES_MISSING)
        elif ext == ".svg" and not is_ifc_svg(src):
            prep.path, prep.svg_frame = svg_to_pdf(src)
            prep.kind = "svg_pdf"
            prep.notes.append("SVG read as a vector drawing (converted to PDF: same paths, texts and coordinates)")
            if not has_text(prep.path):
                # P1 13:30: rendering these to PNG for the image readers was no better (2/15, both ways) at 65x the
                # cost and with 2 confident-wrong areas (run 20261114-132839-upload-outlined-b769): say what helps
                prep.missing.append("the SVG's text is drawn as outlines (no readable room names or areas): export "
                                    "it with text kept as text, or upload the PDF")
        elif ext in HEIC_EXT:
            prep.path, prep.kind = heic_to_jpg(src), "heic"
            prep.notes.append("HEIC photo converted to JPEG (turned upright) before reading")
            try:  # F6 00:06: an iPhone photo of a printed sheet gets O1's deskew too (the web converts HEIC itself)
                from stoa.solvers.raster.photo import prepare_hook

                prepare_hook(prep, prep.path, _cache_dir(Path(prep.path)))
            except ImportError:
                pass
        elif ext in (".png", ".jpg", ".jpeg") or (ext in PIC_EXT and os.environ.get("STOA_PICTURE_PNG", "1") != "0"):
            img = str(src)
            # P1 c15: TIFF / WebP / BMP / GIF (CLI / API path), 16-bit grey PNGs (any path: the web keeps a <= 6000 px
            # PNG as uploaded) and transparent-background PNGs (CLI / API) -> an 8-bit PNG, same pixels, on white;
            # kill switch STOA_PICTURE_PNG=0
            if os.environ.get("STOA_PICTURE_PNG", "1") != "0" and (ext in PIC_EXT or (ext == ".png" and needs_picture_png(src))):
                img, note = readable_picture(src)
                prep.path, prep.kind = img, "picture"
                prep.notes.append(note)
            side = int(os.environ.get("STOA_IMAGE_MAX_SIDE", str(IMAGE_MAX_SIDE)) or 0)
            try:  # O1 c7: a phone photo of a printed sheet -> the sheet, deskewed to its paper corners + evened light
                from stoa.solvers.raster.photo import prepare_hook

                photo = prepare_hook(prep, img, _cache_dir(Path(img)))
            except ImportError:
                photo = False
            got = shrunk_copy(Path(img), side) if side > 0 and not photo else None
            if got:
                prep.path, prep.img_scale, note = got
                prep.kind = "shrink" if prep.kind == "as_is" else "picture+shrink"
                prep.notes.append(note)
        elif ext == ".pdf":
            import pymupdf

            with pymupdf.open(src) as d:
                prep.pages = d.page_count
            if prep.pages > 1:
                from stoa.formats.pages import one_page_copy, pick_page

                try:  # G1 16:35: a stair question on a set -> the page whose printed stair note answers it
                    from stoa.pipeline import _stair_page  # (night-G1; absent on older trees)

                    hit = _stair_page(src, question)
                except Exception:  # noqa: BLE001 — ImportError or an unreadable page: the usual pick
                    hit = None
                k, why = hit or pick_page(src, question)
                prep.path, prep.page, prep.kind = one_page_copy(src, k, _cache_dir(Path(src))), k, "page"
                from stoa.formats.pages import sheet_id

                sid = sheet_id(src, k) if prep.pages >= 4 else None  # big sets: say WHICH sheet, and how to pick another
                prep.sheet = sid or ""
                others = ""
                if prep.pages >= 8 and not (sid and re.search(rf"(?<![\w-]){re.escape(sid)}(?!\w)", question or "")):
                    from stoa.formats.pages import related

                    try:  # the sheets whose text carries the question's rare words: one click / re-ask away
                        rel = related(src, question, exclude=k)
                    except Exception:  # noqa: BLE001
                        rel = []
                    if rel:
                        others = "; other sheets with the question's words: " + ", ".join(
                            f"{s_ or 'page'} (p. {i + 1})" for i, s_ in rel)
                prep.notes.append(f"multi-page PDF ({prep.pages} pages): answered from page {k + 1}"
                                  f"{f' (sheet {sid})' if sid else ''} ({why}); other pages were not read"
                                  + others
                                  + ("; to ask about another sheet, name its number in the question (e.g. "
                                     f"'on sheet {(rel[0][1] if others and rel[0][1] else None) or ('S-101' if sid == 'A-101' else 'A-101')}')"
                                     if prep.pages >= 4 else ""))
            scan_src = prep.path if prep.kind == "page" else str(src)
            try:
                scan = scan_png(scan_src)
            except Exception:  # noqa: BLE001 — a failed scan check leaves the PDF to the PDF readers
                scan = None
            if scan:  # a scanned print: the PDF readers see no text or paths; the image readers can read it
                prep.path, prep.scan_zoom = scan
                prep.scan_pdf = scan_src
                prep.kind = "scan" if prep.kind == "as_is" else "page+scan"
                prep.notes.append(f"scanned PDF (the page is one image, no text layer): read as an image at "
                                  f"{72 * prep.scan_zoom:.0f} dpi")
            else:
                got = paper_copy(prep.path if prep.kind == "page" else str(src))
                if got:
                    prep.path, prep.paper_f, note = got
                    prep.kind = "paper" if prep.kind == "as_is" else "page+paper"
                    prep.notes.append(note)
    except Exception as e:  # noqa: BLE001 — never fail the question on a conversion
        prep.path, prep.kind = path, "as_is"
        prep.error = user_message(e, ext)
        prep.missing.append(prep.error)
    return prep


def to_solved(prep: Prepared, pts, space: str):
    """Points on the UPLOADED file, in the space the browser shows ('image_px' on a picture, 'pdf_pt' on a PDF page, 'svg'
    on an SVG) -> (points, space) on prep.path, the file the readers solve: the inverse of restore() for inputs (a
    right-click point / region from W5's feature registry, F7 / F1 15:5x). None when no map is known (a deskewed phone
    photo) or the space does not fit the conversion: then the caller must not pass the points on."""
    try:
        pts = [[float(p[0]), float(p[1])] for p in pts]
    except (TypeError, ValueError, IndexError):
        return None
    if prep.kind == "as_is" or not pts:
        return pts, space
    if prep.scan_zoom:  # scanned / rendered PDF page read as a PNG: page points -> PNG pixels
        if space != "pdf_pt":
            return None
        z = prep.scan_zoom
        return [[x * z, y * z] for x, y in pts], "image_px"
    if prep.paper_f:  # the page enlarged to the paper its scale was written for
        if space != "pdf_pt":
            return None
        return [[x * prep.paper_f, y * prep.paper_f] for x, y in pts], "pdf_pt"
    if getattr(prep, "photo_h", None):
        return None  # deskewed photo: no forward map here (stoa.solvers.raster.photo maps evidence back only)
    if prep.img_scale:  # the uploaded picture's smaller copy
        if space != "image_px":
            return None
        return [[x * prep.img_scale, y * prep.img_scale] for x, y in pts], "image_px"
    if prep.kind.startswith("svg_pdf") and space == "svg":
        ox, oy, k = prep.svg_frame
        return [[(x - ox) / k, (y - oy) / k] for x, y in pts], "pdf_pt"
    return pts, space  # page / dxf_roles / dwg / heic: same coordinates


@contextlib.contextmanager
def ask_context_on(prep: Prepared):
    """While the readers solve prep.path: $STOA_ASK_CONTEXT's point / region (W5's ask_context, on the uploaded file) in
    the solved file's coordinates and page. A point that cannot be mapped is dropped (with 'dropped' saying why), never
    passed on in the wrong space. Restored afterwards."""
    raw = os.environ.get("STOA_ASK_CONTEXT")
    click, cv = None, None
    try:  # G1 20:47: the API's /ask serves threads and hands the context as a context var (stoa.agent.click.use)
        from stoa.agent import click

        cv = click._CTX.get()
        cv = cv if isinstance(cv, click.Ask) else None
    except Exception:  # noqa: BLE001 — G1's module not on this branch
        click = None
    if prep.kind == "as_is" or (not raw and cv is None):
        yield
        return
    ctx = None
    if cv is not None:  # the context var wins over the env (stoa.agent.click.current does the same)
        ctx = {"point": list(cv.point) if cv.point else None, "region": [list(q) for q in cv.region],
               "view": cv.view, "space": cv.space, "page": cv.page}
    else:
        try:
            ctx = json.loads(raw)
        except ValueError:
            ctx = None
    if not isinstance(ctx, dict):
        yield
        return
    reg = ctx.get("region")
    if isinstance(reg, (list, tuple)) and len(reg) == 4 and all(isinstance(v, (int, float)) for v in reg):
        x0, y0, x1, y1 = (float(v) for v in reg)  # a box [x0, y0, x1, y1] -> its corners
        ctx = {**ctx, "region": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}
    new = dict(ctx)
    space = str(ctx.get("space") or ("pdf_pt" if prep.original.lower().endswith(".pdf") else "image_px"))
    page = ctx.get("page")
    off_page = page is not None and prep.pages > 1 and int(page) != int(prep.page)
    out_space = space
    for key in ("point", "region"):
        v = ctx.get(key)
        if not v:
            continue
        m = None if off_page else to_solved(prep, [v] if key == "point" else v, space)
        if m is None:
            new.pop(key, None)
            new["dropped"] = [*new.get("dropped", []), f"{key}: " + (
                f"on page {int(page) + 1}, the question was read on page {prep.page + 1}" if off_page
                else f"no map from the uploaded file's {space} to its {prep.kind} copy")]
            continue
        new[key] = m[0][0] if key == "point" else m[0]
        out_space = m[1]
    new["space"] = out_space
    if prep.pages > 1 or prep.kind.startswith("page"):
        new["page"] = 0  # the solved file is the picked page alone
    new["solved_file"] = prep.path
    if raw:
        os.environ["STOA_ASK_CONTEXT"] = json.dumps(new)
    try:
        with (click.use(new) if click is not None else contextlib.nullcontext()):
            yield
    finally:
        if raw:
            os.environ["STOA_ASK_CONTEXT"] = raw


def restore(ans, prep: Prepared):
    """Evidence back on the uploaded file + the conversion notes. Mutates and returns `ans`."""
    if prep.kind == "as_is" and not prep.error:
        return ans
    from stoa.data import dataset_rel

    solved = {prep.path, dataset_rel(prep.path), str(Path(prep.path).resolve())}
    orig = dataset_rel(prep.original)
    for ev in ans.evidence:
        if ev.file not in solved and str(Path(ev.file).resolve()) not in solved:
            continue
        if prep.scan_zoom and ev.bbox_space == "image_px":  # PNG pixels -> points on the scanned/rendered page
            z = 1 / prep.scan_zoom
            ev.file, ev.page, ev.bbox_space = orig, prep.page, "pdf_pt"
            ev.bbox = [v * z for v in ev.bbox] if ev.bbox else ev.bbox
            for attr in ("polygon", "polyline"):
                pts = getattr(ev, attr, None)
                if pts:
                    setattr(ev, attr, [[x * z, y * z] for x, y in pts])
            if not prep.kind.startswith("svg_pdf"):
                continue
        if prep.paper_f and ev.bbox_space == "pdf_pt":  # the enlarged page's points -> the uploaded page's points
            z = 1 / prep.paper_f
            ev.file, ev.page = orig, prep.page
            ev.bbox = [v * z for v in ev.bbox] if ev.bbox else ev.bbox
            for attr in ("polygon", "polyline"):
                pts = getattr(ev, attr, None)
                if pts:
                    setattr(ev, attr, [[x * z, y * z] for x, y in pts])
            continue
        if getattr(prep, "photo_h", None) and ev.bbox_space == "image_px":  # deskewed sheet -> the uploaded photo
            from stoa.solvers.raster.photo import restore_ev

            restore_ev(ev, prep, orig)
            continue
        if prep.img_scale and ev.bbox_space == "image_px":  # smaller copy's pixels -> the uploaded image's pixels
            z = 1 / prep.img_scale
            ev.file = orig
            ev.bbox = [v * z for v in ev.bbox] if ev.bbox else ev.bbox
            for attr in ("polygon", "polyline"):
                pts = getattr(ev, attr, None)
                if pts:
                    setattr(ev, attr, [[x * z, y * z] for x, y in pts])
            continue
        if prep.kind == "page":  # same page size and coordinates, only the page index differs
            ev.file, ev.page = orig, prep.page
        elif prep.kind.startswith("svg_pdf") and ev.bbox_space == "pdf_pt":  # -> the SVG's user coordinates (D29)
            ox, oy, k = prep.svg_frame
            ev.file, ev.page, ev.bbox_space = orig, None, "svg"
            if ev.bbox:
                ev.bbox = [ox + ev.bbox[0] * k, oy + ev.bbox[1] * k, ox + ev.bbox[2] * k, oy + ev.bbox[3] * k]
            for attr in ("polygon", "polyline"):
                pts = getattr(ev, attr, None)
                if pts:
                    setattr(ev, attr, [[ox + x * k, oy + y * k] for x, y in pts])
        elif prep.kind == "dxf_roles":  # same drawing units, only layer names changed
            ev.file = orig
        elif prep.kind == "picture" and ev.bbox_space == "image_px":  # same pixel grid as the uploaded picture
            ev.file = orig
        # dwg / heic: evidence stays on the converted .dxf / .jpg, which a browser or CAD viewer can open
    ans.assumptions = [*prep.notes, *(a for a in ans.assumptions if a not in prep.notes)]
    ans.missing = [*ans.missing, *(m for m in prep.missing if m not in ans.missing)]
    return ans
