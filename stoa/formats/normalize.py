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

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

HEIC_EXT = {".heic", ".heif", ".hif"}
SVG_PDF_VERSION = 2  # in the cached file's name: a fix to the conversion never reuses an older copy
_IFC_SVG = re.compile(rb'class="[^"]*\bIfc[A-Z]')
_LEN = re.compile(r"^\s*([0-9.]+)\s*([a-z%]*)\s*$")
_PX_PER = {"": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0}


@dataclass
class Prepared:
    original: str  # the uploaded file
    path: str  # the file to solve
    kind: str = "as_is"  # as_is | dwg | dxf_roles | svg_pdf | heic | page | scan (an image-only PDF page, as PNG)
    page: int = 0  # page of the ORIGINAL the solved file shows (multi-page PDFs)
    pages: int = 1
    notes: list[str] = field(default_factory=list)  # assumptions to show with the answer
    missing: list[str] = field(default_factory=list)  # what would make it answerable (failed conversion)
    error: str = ""  # user-readable reason the file could not be converted ("" = fine)
    svg_frame: tuple[float, float, float] = (0.0, 0.0, 1.0)  # generic SVG: (x0, y0, user units per PDF pt)
    scan_zoom: float = 0.0  # scan: PNG pixels per PDF point of the original page (0 = not a scan)


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


def svg_to_pdf(path: str | Path, out_dir: str | Path | None = None) -> tuple[str, tuple[float, float, float]]:
    """(PDF path, (x0, y0, SVG user units per PDF pt)). Vector content and text survive (MuPDF's SVG reader)."""
    import pymupdf

    src = Path(path)
    dst = Path(out_dir or _cache_dir(src)) / f"{src.stem}.svg-v{SVG_PDF_VERSION}.pdf"
    with pymupdf.open(stream=svg_text_fix(src), filetype="svg") as doc:
        width_pt = doc[0].rect.width
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            pdf = pymupdf.open("pdf", doc.convert_to_pdf())
            pdf.save(dst, garbage=3, deflate=True)
    x0, y0, w = svg_user_box(src) or (0.0, 0.0, width_pt)
    return str(dst), (x0, y0, w / width_pt if width_pt else 1.0)


def scan_png(pdf: str, max_side: int = 6000) -> tuple[str, float] | None:
    """(PNG path, px per pt) when page 0 is a scan: (almost) no text, no vector paths, one image over >= 50% of the
    page. Rendered at the image's own resolution (capped at max_side px), so no detail is lost or invented."""
    import pymupdf

    from stoa.formats.pages import _stream_bytes

    with pymupdf.open(pdf) as d:
        pg = d[0]
        imgs = pg.get_images(full=True)  # cheap checks first: most uploads are vector sheets
        if not imgs or pg.rotation or _stream_bytes(pg) > 20_000 or len((pg.get_text() or "").strip()) >= 20:
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
    return str(dst), zoom


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
        zoom = max(1, int(72 * zoom + 1e-6)) / 72  # a whole-number dpi, so the PNG's dpi tag is exact
        dst = Path(_cache_dir(Path(pdf))) / f"{Path(pdf).stem}.r{round(72 * zoom)}.png"
        _render(pg, zoom, dst, pdf)
    return str(dst), zoom


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
    pix.save(dst)


def has_text(pdf: str, min_chars: int = 20) -> bool:
    import pymupdf

    with pymupdf.open(pdf) as d:
        return len((d[0].get_text() or "").strip()) >= min_chars


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
            ImageOps.exif_transpose(im).convert("RGB").save(dst, "JPEG", quality=92)
    return str(dst)


def user_message(e: Exception, ext: str) -> str:
    """A sentence the visitor can act on (no paths, no tracebacks); our own converter errors already are one."""
    from stoa.formats.dwg import DwgConvertError, DwgConverterMissing

    if isinstance(e, (DwgConverterMissing, DwgConvertError)) or (isinstance(e, RuntimeError) and "HEIC" in str(e)):
        return str(e)
    kind = (ext or "").lstrip(".").upper() or "file"
    return (f"the {kind} file could not be opened (it may be damaged, empty or not really a {kind}; "
            f"{type(e).__name__}): please export it again, or upload the drawing as PDF or PNG")


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
                prep.notes.append(f"multi-page PDF ({prep.pages} pages): answered from page {k + 1} ({why}); "
                                  "other pages were not read")
            try:
                scan = scan_png(prep.path if prep.kind == "page" else str(src))
            except Exception:  # noqa: BLE001 — a failed scan check leaves the PDF to the PDF readers
                scan = None
            if scan:  # a scanned print: the PDF readers see no text or paths; the image readers can read it
                prep.path, prep.scan_zoom = scan
                prep.kind = "scan" if prep.kind == "as_is" else "page+scan"
                prep.notes.append(f"scanned PDF (the page is one image, no text layer): read as an image at "
                                  f"{72 * prep.scan_zoom:.0f} dpi")
    except Exception as e:  # noqa: BLE001 — never fail the question on a conversion
        prep.path, prep.kind = path, "as_is"
        prep.error = user_message(e, ext)
        prep.missing.append(prep.error)
    return prep


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
        # dwg / heic: evidence stays on the converted .dxf / .jpg, which a browser or CAD viewer can open
    ans.assumptions = [*prep.notes, *(a for a in ans.assumptions if a not in prep.notes)]
    ans.missing = [*ans.missing, *(m for m in prep.missing if m not in ans.missing)]
    return ans
