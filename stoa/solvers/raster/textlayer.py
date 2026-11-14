"""A text layer for raster drawings (PNG/JPEG floor plans, historical scans): every printed text with its box.

PDFs have a text layer (PyMuPDF) that tools like the agent's `find_text` search; scans and PNG plans have none, so
for them the agent had only `look` (one VLM call on the whole, downscaled image: small print on a 3000 px HABS sheet
is unreadable at that size). This module reads the text tile by tile with the shared VLM (Qwen3-VL text spotting:
JSON list of {bbox_2d, text}), maps every box back to ORIGINAL image pixels, merges duplicates from the tile
overlaps, and caches the result per image. On top of it, deterministic helpers:

    layer = text_layer(path)                -> TextLayer (items with image_px boxes; cost charged once)
    find_text(path, "sauna")                -> hits (text, box, score), fuzzy and accent/case-insensitive
    scales(layer) / dimensions(layer) / areas(layer)  -> parsed "1:100", 1/4" = 1'-0" (1:48), 12'-6", 14.5 m²
    summary(path)                           -> a compact text table for an LLM (the agent's `table` tool)

OCR engine: the VLM by default; STOA_TEXT_ENGINE=ocr|auto reads the layer with PP-OCRv6 on CPU instead (stoa.ocr, own env).
"""

from __future__ import annotations

import concurrent.futures as cf
import contextvars
import difflib
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from stoa import llm
from stoa.contract import Evidence, record_cost

Image.MAX_IMAGE_PIXELS = None

OCR_PROMPT = ("Read ALL text printed on this image of a technical drawing: titles, room names, notes, dimensions, "
              "scale notes, sheet numbers, legends. Output a JSON list with one entry per line of text: "
              '[{"bbox_2d": [x1, y1, x2, y2], "text": "<text exactly as printed>"}]. '
              "Coordinates are 0-1000 relative to this image. Output [] if there is no text.")
VERSION = "tl1"
_ITEM = re.compile(r'\{[^{}]*?"bbox_2d"\s*:\s*\[([^\]]*)\][^{}]*?"text"\s*:\s*"((?:[^"\\]|\\.)*)"[^{}]*\}')
CACHE_DIR = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "artifacts" / \
    "W3" / "textlayer"


def _api_mode() -> bool:
    """STOA_MODE=api (P1, night-P1 stoa.llm.api_mode): no local GPU model. False until that lands on night."""
    return bool(getattr(llm, "api_mode", lambda: False)())


@dataclass
class TextItem:
    text: str
    box: list[float]  # original-image pixels [x0, y0, x1, y1]
    tile: int = 0

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2


@dataclass
class TextLayer:
    path: str
    size: tuple[int, int]
    items: list[TextItem] = field(default_factory=list)
    tiles: int = 1
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(i.text for i in self.items)


# -- tiling ---------------------------------------------------------------------------------------------------------

def tile_boxes(w: int, h: int, tile: int = 1100, overlap: float = 0.12) -> list[tuple[int, int, int, int]]:
    """Grid of overlapping crops so that each crop is at most ~tile px per side (1x1 for small images)."""
    nx, ny = max(1, math.ceil(w / tile)), max(1, math.ceil(h / tile))
    tw = min(w, int(w / nx * (1 + overlap))) if nx > 1 else w
    th = min(h, int(h / ny * (1 + overlap))) if ny > 1 else h
    out = []
    for gy in range(ny):
        for gx in range(nx):
            x0 = 0 if nx == 1 else round(gx * (w - tw) / (nx - 1))
            y0 = 0 if ny == 1 else round(gy * (h - th) / (ny - 1))
            out.append((x0, y0, x0 + tw, y0 + th))
    return out


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s.replace('\\"', '"')


def parse_items(text: str, w: int, h: int, max_repeat: int = 6) -> list[TextItem]:
    """Qwen bbox_2d JSON (0..1000 relative) -> items in this image's pixels; drops loops (text repeated > N times)."""
    raw = []
    for m in _ITEM.finditer(text):
        try:
            b = [float(v) for v in m.group(1).split(",")]
        except ValueError:
            continue
        t = _unescape(m.group(2)).strip()
        if len(b) == 4 and t:
            raw.append(TextItem(t, [b[0] / 1000 * w, b[1] / 1000 * h, b[2] / 1000 * w, b[3] / 1000 * h]))
    counts: dict[str, int] = {}
    for i in raw:
        counts[i.text] = counts.get(i.text, 0) + 1
    return [i for i in raw if counts[i.text] <= max_repeat]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _overlap_small(a, b) -> float:
    """Intersection over the smaller box (a text cut at a tile border lies inside the full one)."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    s = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / s if s > 0 else 0.0


def merge_items(items: list[TextItem]) -> list[TextItem]:
    """De-duplicate texts read twice in tile overlaps: same/contained text with overlapping boxes -> keep the longer."""
    kept: list[TextItem] = []
    for it in sorted(items, key=lambda i: -len(i.text)):
        n = norm(it.text)
        dup = False
        for k in kept:
            nk = norm(k.text)
            if (n == nk and _iou(it.box, k.box) > 0.2) or (n and n in nk and _overlap_small(it.box, k.box) > 0.5):
                dup = True
                break
        if not dup:
            kept.append(it)
    return sorted(kept, key=lambda i: (round(i.cy / 25), i.cx))


# -- reading --------------------------------------------------------------------------------------------------------

_mem: dict[str, TextLayer] = {}
_locks: dict[str, threading.Lock] = {}
_glock = threading.Lock()


def _abspath(path: str) -> Path:
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return Path(os.environ.get("STOA_DATA", ".")) / path


_tags: dict[tuple, str] = {}


def file_tag(path: str) -> str:
    """'' for a dataset file (its name is unique, so existing cache keys stay valid); else '#<content sha1>'. Uploads
    are all saved as <dir>/<sha>/plan.png (W5), so a key on the file NAME gave every upload the text layer of the
    first one ever read (artifacts/W3/textlayer/plan.*.json, written 09:00 by a test upload): wrong labels, areas
    and scale bar on every later upload."""
    import hashlib

    p = _abspath(path)
    try:
        st = p.stat()
    except OSError:
        return ""
    k = (str(p), st.st_mtime_ns, st.st_size)
    if k not in _tags:
        data = os.environ.get("STOA_DATA")
        try:
            inside = bool(data) and p.resolve().is_relative_to(Path(data).resolve())
        except (OSError, ValueError):
            inside = False
        _tags[k] = "" if inside else "#" + hashlib.sha1(p.read_bytes()).hexdigest()[:16]
    return _tags[k]


def _disk_key(path: str, tile: int, backend: str) -> Path:
    import hashlib

    mode = "|api" if _api_mode() else ""  # STOA_MODE=api (P1): never reuse a layer the local GPU model read
    h = hashlib.sha1(f"{VERSION}|{Path(path).name}{file_tag(path)}|{tile}|{backend}{mode}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{Path(path).stem[:60]}.{h}.json"


_clean: dict[str, bool] = {}


def is_clean(path: str) -> bool:
    """A clean digital render (CAD export, CubiCasa-style PNG) rather than a scan / photo: W3's clean-render gate
    (windows.clean_render: >= 70% pure white and few grey levels). Cached per file; unreadable -> False."""
    k = f"{path}{file_tag(path)}"
    if k not in _clean:
        try:
            from stoa.solvers.raster.windows import clean_render

            _clean[k] = bool(clean_render(str(_abspath(path)))[0])
        except Exception:  # noqa: BLE001
            _clean[k] = False
    return _clean[k]


OCR_MIN_SIDE = 2000  # px; CubiCasa plans are 95% below (median 873), HABS / Finna sheets 100% at or above (3000)


def ocr_suits(path: str) -> bool:
    """auto engine: OCR for scans and photos of whole sheets (fail the clean-render gate AND long side >= 2000 px),
    the VLM for clean renders and small raster plans. Measured (O1 12:48-13:08): OCR = VLM on HABS/Finna scans at
    1/8-1/15 of the cost; OCR worse on clean Swiss renders (area sums 22.8 vs 43.6) and on CubiCasa plans (room-label
    classes equal to the VLM's on 10/24, labels 61 vs 80; 2.5x upscale 7/24)."""
    if is_clean(path):
        return False
    try:
        with Image.open(_abspath(path)) as im:
            return max(im.size) >= OCR_MIN_SIDE
    except OSError:
        return False


# S0 16:48 (gate d17): Stoa's S-100F PDF rendered to '<stem>.r135.png' was OCR'd on CPU for 10+ min although the PDF
# has its own text. P1's upload renders are '<stem>.rNNN.png' (whole single-page PDF) or '<stem>.pN.rNNN.png' (next to
# the one-page '<stem>.pN.pdf'): read the text lines from that PDF, scaled to the image. Outlined text (0 lines) or a
# junk font encoding ('352326(')7ð') -> None (OCR / VLM as before).
_RENDER_NAME = re.compile(r"^(?P<stem>.+?)(?P<page>\.p\d+)?\.r(?P<dpi>\d{2,3})\.png$")
_SANE = re.compile(r"[A-Za-z0-9ÄÖÅÜäöåüßÉéÈèÀàÇçÑñ .,:;'\"()/+\-=×x²³°%#&@!?\[\]_*~|<>±⌀Ø’‘“”´`{}^$€£¼½¾·•–—]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # a font without a usable encoding (X1 farm A0.1)
_pdf_mem: dict[str, "TextLayer | None"] = {}


def _cache_on() -> bool:
    """STOA_TEXT_CACHE=0: read every text layer cold (S0 16:53: the gate measures first-question latency)."""
    return os.environ.get("STOA_TEXT_CACHE", "1") != "0"


def pdf_render_layer(path: str) -> "TextLayer | None":
    p = _abspath(path)
    m = _RENDER_NAME.match(p.name)
    if not m:
        return None
    key = f"{p}{file_tag(path)}"
    if key in _pdf_mem and _cache_on():
        return _pdf_mem[key]
    pdf = p.with_name(f"{m['stem']}{m['page'] or ''}.pdf")
    tl = None
    if pdf.exists():
        try:
            tl = _pdf_lines(p, pdf)
        except Exception as e:  # noqa: BLE001 - the image readers still work
            print(f"[textlayer] PDF text of {pdf.name} not used: {type(e).__name__}", file=sys.stderr)
    _pdf_mem[key] = tl
    return tl


def _pdf_lines(png: Path, pdf: Path) -> "TextLayer | None":
    import pymupdf
    from PIL import Image

    with pymupdf.open(str(pdf)) as doc:
        if doc.page_count != 1:
            return None  # which page the render shows is not known
        pg = doc[0]
        with Image.open(png) as im:
            W, H = im.size
        rect = pg.rect  # the displayed (rotated) page
        sx, sy = W / rect.width, H / rect.height
        if abs(sx / sy - 1) > 0.02:
            return None  # not a render of this page
        items, chars, sane = [], 0, 0
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(sp["text"].strip() for sp in ln["spans"] if sp["text"].strip())
                if not t:
                    continue
                r = pymupdf.Rect(ln["bbox"]) * pg.rotation_matrix
                items.append(TextItem(t, [round(r.x0 * sx, 1), round(r.y0 * sy, 1), round(r.x1 * sx, 1),
                                          round(r.y1 * sy, 1)]))
                if _CTRL.search(t):
                    return None  # undecodable text on this page: the image readers see what is printed
                chars += len(t)
                sane += len(_SANE.findall(t))
    if len(items) < 3 or sane < 0.85 * chars:
        return None
    return TextLayer(path=str(png), size=(W, H), items=items, tiles=0,
                     notes=[f"text layer = the PDF's own text ({pdf.name}, {len(items)} lines; exact, no OCR/VLM)"])


def text_layer(path: str, backend: str = "local", tile: int = 1100, upscale_to: int = 2000,
               max_tokens: int = 2500, workers: int = 6) -> TextLayer:
    """All printed text of a raster drawing with image_px boxes. Cached in memory and on disk (artifacts/W3/textlayer);
    the cost of the reading is charged to the caller every time (cold-run accounting, as elsewhere in raster).

    STOA_TEXT_ENGINE (O1): 'vlm' = the VLM reading below; 'ocr' = PP-OCRv6 line boxes on CPU (stoa.ocr, 0 LLM
    tokens, detector boxes); 'auto' (default) = OCR where `ocr_suits` (scans / photos of whole sheets: fail the
    clean-render gate and long side >= 2000 px) and the OCR engine is installed, else the VLM (clean renders, small
    raster plans: OCR measured worse there). An OCR failure falls back to the VLM reading."""
    engine = os.environ.get("STOA_TEXT_ENGINE", "auto").lower()
    if engine != "vlm":
        tl = pdf_render_layer(path)  # an upload render of a PDF page: the PDF's own words, exact and free
        if tl is not None:
            return tl
    if backend == "local" and engine in ("ocr", "auto"):
        from stoa import ocr

        okey = f"{path}{file_tag(path)}|ocr"
        gkey = f"{okey}|gated"  # the VLM layer that replaced a garbled OCR reading (kept apart: mark_ocr_exact)
        if gkey in _mem and _cache_on():
            return _mem[gkey]
        if okey in _mem and _cache_on():
            return _mem[okey]
        if engine == "ocr" or (ocr_suits(path) and (ocr.cached(path) or ocr.available())):
            if engine == "auto":
                v = _sticky_vlm(path, backend, tile, upscale_to, max_tokens, workers)
                if v is not None:
                    _mem[gkey] = v
                    return v
            try:
                tl = ocr.text_layer(path)
            except Exception as e:  # noqa: BLE001 - keep answering with the VLM layer
                print(f"[textlayer] OCR failed on {path}: {e}; using the VLM layer", file=sys.stderr)
                if engine == "auto" and STICKY and _cache_on() and ("budget" in str(e) or "skipped" in str(e)):
                    # X1 21:02: the first question of a cold upload got the VLM layer (OCR over budget), later ones the
                    # OCR that landed meanwhile -> 41 of 91 values changed between a cold and a warm run of the same
                    # files. The layer that answered first stays this file's layer (memory + a '.layer' note on disk).
                    v = _vlm_layer(path, backend, tile, upscale_to, max_tokens, workers)
                    _set_sticky(path, f"OCR {str(e)[:80]}")
                    _mem[gkey] = v
                    return v
            else:
                if engine == "auto":
                    v = _ocr_gate(path, tl, backend, tile, upscale_to, max_tokens, workers)
                    if v is not None:
                        _mem[gkey] = v
                        if tl.items and set(map(id, tl.items)) <= set(map(id, v.items)):
                            _mem[okey] = tl  # a union layer: the OCR's own boxes for mark_ocr_exact (not the VLM's)
                        return v
                _mem[okey] = tl
                return tl
    return _vlm_layer(path, backend, tile, upscale_to, max_tokens, workers)


STICKY = os.environ.get("STOA_O1_STICKY", "1") != "0"


def _sticky_file(path: str) -> Path | None:
    try:
        from stoa import ocr

        return ocr._key(path).with_suffix(".layer")
    except Exception:  # noqa: BLE001
        return None


def _set_sticky(path: str, why: str) -> None:
    f = _sticky_file(path)
    if f is None:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"layer": "vlm", "why": why, "time": time.strftime("%Y-%m-%d %H:%M:%S")}))
    except OSError:
        pass


def _sticky_vlm(path: str, backend: str, tile: int, upscale_to: int, max_tokens: int,
                workers: int) -> "TextLayer | None":
    """The VLM layer when this file's first reading fell back to it (OCR over budget) and it is cached: later questions
    keep it instead of switching to the OCR that landed afterwards (same answers cold and warm). STOA_O1_STICKY=0 or
    STOA_TEXT_CACHE=0 = off."""
    if not STICKY or not _cache_on():
        return None
    f = _sticky_file(path)
    if f is None or not f.exists() or not _vlm_cached(path, backend, tile):
        return None
    try:
        why = json.loads(f.read_text()).get("why", "")
    except (OSError, ValueError):
        why = ""
    from dataclasses import replace

    v = _vlm_layer(path, backend, tile, upscale_to, max_tokens, workers)
    return replace(v, notes=[*v.notes, "text layer = the VLM reading this file's first question used "
                                       f"({why or 'OCR not ready'}); kept so later answers on the file stay the same"])


def _ocr_gate(path: str, tl: TextLayer, backend: str, tile: int, upscale_to: int, max_tokens: int,
              workers: int) -> "TextLayer | None":
    """O1 c8: PP-OCR on a heavily degraded scan returns word-like garbage ('Faress Roons' = Fitness Room; X1: DfE
    200-dpi scan 1/9 questions with OCR vs 7/9 with the VLM layer). When stoa.ocr.quality calls the reading garbled,
    read the VLM layer too and return it if its dictionary-word share beats the OCR's by quality.MARGIN and it read at
    least half as many words; else None (keep the OCR). The OCR layer gets a note either way."""
    from stoa import ocr
    from stoa.ocr import quality

    try:
        vd = ocr.verdict(path)
    except Exception:  # noqa: BLE001 - no verdict: keep the OCR
        vd = None
    garbled = vd is not None and vd.garbled
    if not garbled:
        # P1 20:05: the first question's OCR ran over its budget -> the VLM layer was read and answered; the OCR landed
        # later and REPLACED it (EFA scan: 71 OCR lines vs 141 VLM lines with the room names; right page 12/12 -> right
        # answer 2/12). A VLM layer that is already read (no new cost) stays when it is clearly richer.
        if os.environ.get("STOA_O1_RICHER", "1") == "0" or not _vlm_cached(path, backend, tile):
            return None
        try:
            v = _vlm_layer(path, backend, tile, upscale_to, max_tokens, workers)
        except Exception:  # noqa: BLE001
            return None
        rv, ro = _richness(v), _richness(tl)
        if rv >= RICHER * ro and rv - ro >= 10:
            from dataclasses import replace

            return replace(v, notes=[*v.notes, f"text layer = the VLM reading already read for this file: {rv} words vs "
                                               f"{ro} in the later PP-OCR reading (the richer layer is kept)"])
        if UNION:
            u, n = union_layer(tl, v)
            if n:
                return u
        return None
    try:
        v = _vlm_layer(path, backend, tile, upscale_to, max_tokens, workers)
    except Exception as e:  # noqa: BLE001
        tl.notes.append(f"{vd.describe()} (garbled?); the VLM re-read failed ({type(e).__name__}): OCR kept")
        return None
    vs = quality.dict_share(i.text for i in v.items)
    if vs is not None and vs[0] >= vd.share + quality.MARGIN and vs[1] >= 0.5 * vd.tokens:
        from dataclasses import replace

        return replace(v, notes=[*v.notes, f"text layer = the VLM reading, not PP-OCR: {vd.describe()} (a garbled "
                                           f"scan); the VLM reading's share {vs[0]:.2f} of {vs[1]} tokens"])
    tl.notes.append(f"{vd.describe()}; the VLM reading was not better "
                    f"({vs[0]:.2f} of {vs[1]} tokens): OCR kept" if vs else f"{vd.describe()}: OCR kept")
    return None


RICHER = 1.5  # a cached VLM layer replaces a (not garbled) OCR reading only with >= 1.5x its words (and >= 10 more)
# O1 c9: otherwise the cached VLM layer's texts in places where PP-OCR read NOTHING are added to the OCR layer. The answer
# on a scan / photo no longer depends on which layer happened to be read first (X1 sl-133 EFA phone photo 'How long is
# the building overall?': VLM layer 97.63 ✓, OCR layer 20.76 ✗ - the OCR missed '97.630' / '69.600'). STOA_O1_UNION=0 = off.
UNION = os.environ.get("STOA_O1_UNION", "1") != "0"
UNION_SAME = 0.8  # a VLM text this close (match_score) to an OCR text a few line heights away is the same print, shifted


def union_layer(ocr_tl: TextLayer, vlm_tl: TextLayer) -> tuple[TextLayer, int]:
    """The OCR layer plus the VLM items that sit where the OCR read nothing: a VLM item is dropped when its centre lies
    in an OCR box (padded by half a line height), it overlaps one (>= 30 % of the smaller box), or an OCR text within 3
    line heights reads the same (VLM boxes are text-spotting guesses, often shifted). Only items with a digit or >= 3
    letters are added. OCR boxes stay exactly as read (mark_ocr_exact); the added boxes are never marked exact."""
    from dataclasses import replace

    sx = ocr_tl.size[0] / max(1, vlm_tl.size[0])
    sy = ocr_tl.size[1] / max(1, vlm_tl.size[1])
    obs = [(o.box, max(1.0, o.box[3] - o.box[1]), norm(o.text)) for o in ocr_tl.items]
    add = []
    for v in vlm_tl.items:
        nv = norm(v.text)
        if not (re.search(r"\d", nv) or len(re.sub(r"[\W\d_]", "", nv)) >= 3):
            continue
        b = [v.box[0] * sx, v.box[1] * sy, v.box[2] * sx, v.box[3] * sy]
        h = max(1.0, b[3] - b[1])
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        dup = False
        for ob, oh, no in obs:
            pad = 0.5 * max(h, oh)
            if ob[0] - pad <= cx <= ob[2] + pad and ob[1] - pad <= cy <= ob[3] + pad:
                dup = True
                break
            if _overlap_small(b, ob) >= 0.3:
                dup = True
                break
            r = 3 * max(h, oh)
            if ob[0] - r <= cx <= ob[2] + r and ob[1] - r <= cy <= ob[3] + r and (
                    nv == no or match_score(nv, no) >= UNION_SAME or match_score(no, nv) >= UNION_SAME):
                dup = True
                break
        if not dup:
            add.append(TextItem(v.text, [round(c, 1) for c in b], v.tile))
    if not add:
        return ocr_tl, 0
    items = sorted([*ocr_tl.items, *add], key=lambda i: (round(i.cy / 25), i.cx))
    return replace(ocr_tl, items=items, notes=[*ocr_tl.notes, f"text layer = the PP-OCR reading + {len(add)} text(s) of "
                                                               f"the VLM reading already read for this file where the OCR "
                                                               f"read nothing (e.g. {add[0].text[:30]!r})"]), len(add)


def _vlm_cached(path: str, backend: str = "local", tile: int = 1100) -> bool:
    return f"{path}{file_tag(path)}|{backend}|{tile}|{_api_mode()}" in _mem or (
        _cache_on() and _disk_key(path, tile, backend).exists())


def _richness(tl: TextLayer) -> int:
    """Dictionary words (>= 4 letters) in a layer, or its line count without a word list."""
    from stoa.ocr import quality

    r = quality.dict_share(i.text for i in tl.items)
    return round(r[0] * r[1]) if r is not None else len(tl.items)


def gate_pending(path: str) -> bool:
    """True when text_layer(path) would still have to READ a VLM layer cold because the cached OCR reading is
    garbled (sheet.cached_layer must not trigger that)."""
    from stoa import ocr

    if os.environ.get("STOA_TEXT_ENGINE", "auto").lower() != "auto":
        return False
    if f"{path}{file_tag(path)}|ocr|gated" in _mem:
        return False
    try:
        vd = ocr.verdict(path) if ocr.cached(path) else None
    except Exception:  # noqa: BLE001
        return False
    if vd is None or not vd.garbled:
        return False
    key = f"{path}{file_tag(path)}|local|1100|{_api_mode()}"
    return key not in _mem and not _disk_key(path, 1100, "local").exists()


def _vlm_layer(path: str, backend: str, tile: int, upscale_to: int, max_tokens: int, workers: int) -> TextLayer:
    key = f"{path}{file_tag(path)}|{backend}|{tile}|{_api_mode()}"  # (a server may overwrite one upload path with a new plan)
    with _glock:
        lk = _locks.setdefault(key, threading.Lock())
    with lk:
        if key in _mem and _cache_on():  # read earlier in this process (another question on the same drawing): paid
            return _mem[key]
        f = _disk_key(path, tile, backend)
        if f.exists() and _cache_on():
            d = json.loads(f.read_text())
            d["items"] = [TextItem(**i) for i in d["items"]]
            d["size"] = tuple(d["size"])
            tl = TextLayer(**d)
            # persisted from an earlier run: bill it as computed, so warm and cold runs cost the same
            record_cost(llm_tokens_in=tl.tokens_in, llm_tokens_out=tl.tokens_out, llm_calls=tl.calls or tl.tiles,
                        gpu_seconds=llm.gpu_seconds_estimate(tl.tokens_in, tl.tokens_out))
        else:
            tl = _read(path, backend, tile, upscale_to, max_tokens, workers)  # llm.chat bills each call itself
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(asdict(tl)))
            except OSError:
                pass
        tl = _joined(tl)
        _mem[key] = tl
        return tl


def _cut_overlap(a: str, b: str, max_k: int) -> int | None:
    """Characters of `a`'s end that `b` repeats at its start (a tile cut reads the overlap twice; the cut letter at
    `a`'s end may be misread: 'ENTRY VESTIE' + 'VESTIBULE - FLOOR PLAN') -> number of chars of `a` to drop. (VP's
    stoa.sheet.views._cut_overlap, night-VP d6cd485.)"""
    A, B = a.upper(), b.upper()
    for drop_last in (0, 1):
        a2 = A[:len(A) - drop_last] if drop_last else A
        for k in range(min(len(a2), len(B), max_k), 2, -1):
            sa, sb = a2[-k:], B[:k]
            bad = sum(x != y for x, y in zip(sa, sb))
            if bad <= (k - 1) // 4 and sa.strip() and sa[0] == sb[0]:
                return k + drop_last
    return None


def join_tile_cuts(items: list[TextItem]) -> tuple[list[TextItem], int]:
    """VP 19:33 (owner O1): a printed line that crosses a VLM tile border comes back as two items that OVERLAP on one
    baseline and repeat a few characters ('MODULE 01- ENTRY VESTIE' + 'VESTIBULE - FLOOR PLAN'); join them, dropping
    the repeated text. VP: 0 false joins on 173 cached image layers. -> (items, number of joins)."""
    def h(i):
        return i.box[3] - i.box[1]

    def cy(i):
        return (i.box[1] + i.box[3]) / 2

    its = sorted(items, key=lambda i: (i.box[0], cy(i)))
    used = [False] * len(its)
    out, joins = [], 0
    for i, a in enumerate(its):
        if used[i]:
            continue
        cur = a
        while True:
            best = None
            for j, b in enumerate(its):
                if used[j] or j == i or b is cur:
                    continue
                if not (abs(cy(b) - cy(cur)) < 0.35 * min(h(cur), h(b)) and abs(h(b) - h(cur)) < 0.3 * max(h(cur), h(b))):
                    continue
                if not (cur.box[0] + 0.2 * (cur.box[2] - cur.box[0]) < b.box[0] < cur.box[2] < b.box[2]):
                    continue
                cw = (cur.box[2] - cur.box[0]) / max(1, len(cur.text))
                k = _cut_overlap(cur.text, b.text, int(2 * (cur.box[2] - b.box[0]) / max(cw, 1e-6)) + 3)
                if k is not None and (best is None or b.box[0] < best[1].box[0]):
                    best = (j, b, k)
            if best is None:
                break
            j, b, k = best
            used[j] = True
            joins += 1
            cur = TextItem(cur.text[:len(cur.text) - k] + b.text,
                           [cur.box[0], min(cur.box[1], b.box[1]), b.box[2], max(cur.box[3], b.box[3])], tile=cur.tile)
        used[i] = True
        out.append(cur)
    return out, joins


def _joined(tl: TextLayer) -> TextLayer:
    """The VLM layer with tile cuts joined, ONLY with STOA_O1_TILE_JOIN=1 (default off, see below; VP's STOA_TILE_JOIN=0
    also turns it off). Applied after reading AND after a disk load (in memory only: the disk cache keeps the raw read), so
    cached layers get it without a re-read."""
    if tl.tiles <= 1 or os.environ.get("STOA_O1_TILE_JOIN", "0") != "1" or os.environ.get("STOA_TILE_JOIN", "1") == "0":
        # OFF by default (VP 20:07, measured on night-O1 72b0f0c): the joined layer REGRESSES E1's image area table on
        # Stoa's dorm PNG 10/10 -> 4/10 (partial table rows pair up, so its complete crop-OCR fallback is skipped);
        # no reader was measured to gain from it (raster smoke 60: 10 = 10). STOA_O1_TILE_JOIN=1 turns it on.
        return tl
    from dataclasses import replace

    items, n = join_tile_cuts(tl.items)
    if not n:
        return tl
    return replace(tl, items=items, notes=[*tl.notes, f"{n} line(s) cut at a tile border joined"])


def _read(path: str, backend: str, tile: int, upscale_to: int, max_tokens: int, workers: int) -> TextLayer:
    t0 = time.perf_counter()
    img = Image.open(_abspath(path)).convert("RGB")
    W, H = img.size
    boxes = tile_boxes(W, H, tile)
    tl = TextLayer(path=path, size=(W, H), tiles=len(boxes))

    def read_one(i_b, split: bool = True):
        i, (x0, y0, x1, y1) = i_b
        crop = img.crop((x0, y0, x1, y1))
        s = min(3.0, upscale_to / max(crop.size))
        if s > 1.05:
            crop = crop.resize((round(crop.width * s), round(crop.height * s)), Image.LANCZOS)
        r = llm.chat(OCR_PROMPT, images=[crop], backend=backend, max_tokens=max_tokens,
                     max_pixels=upscale_to * upscale_to, max_side=max(upscale_to, 1024), cache=True)
        notes, tin, tout, ncalls = [], 0, 0, 1
        items = parse_items(r.text, x1 - x0, y1 - y0)
        if r.finish_reason == "length" and len(items) < 3:  # looping output: one retry with a repetition penalty
            r2 = llm.chat(OCR_PROMPT, images=[crop], backend=backend, max_tokens=max_tokens,
                          max_pixels=upscale_to * upscale_to, max_side=max(upscale_to, 1024), cache=True,
                          extra_body={"repetition_penalty": 1.1})
            items2 = parse_items(r2.text, x1 - x0, y1 - y0)
            ncalls = 2  # both tries are billed: `extra` = the one that is not kept in `r`
            if len(items2) > len(items):
                tin, tout = r.prompt_tokens or 0, r.completion_tokens or 0
                items, r = items2, r2
            else:
                tin, tout = r2.prompt_tokens or 0, r2.completion_tokens or 0
            notes.append(f"tile {i}: retry with repetition_penalty")
        elif r.finish_reason == "length":
            notes.append(f"tile {i}: text list truncated at {max_tokens} tokens")
        if r.finish_reason == "length" and split and min(x1 - x0, y1 - y0) >= 300:
            # a dense tile (R1 08:45: 9 of 20 CubiCasa tiles hit the cap, one plan came back with 3 texts): read its
            # 2x2 sub-tiles too (each upscaled more) and merge; boxes go through the same overlap merge
            mx, my = (x0 + x1) // 2, (y0 + y1) // 2
            ox, oy = int(0.06 * (x1 - x0)), int(0.06 * (y1 - y0))
            for it in items:
                it.box = [round(it.box[0] + x0, 1), round(it.box[1] + y0, 1), round(it.box[2] + x0, 1),
                          round(it.box[3] + y0, 1)]
                it.tile = i
            for sb in [(x0, y0, mx + ox, my + oy), (mx - ox, y0, x1, my + oy), (x0, my - oy, mx + ox, y1),
                       (mx - ox, my - oy, x1, y1)]:
                sub, snotes, sti, sto, snc = read_one((i, sb), split=False)  # boxes come back in image px
                items += sub
                tin, tout, ncalls = tin + sti, tout + sto, ncalls + snc
            notes.append(f"tile {i}: truncated -> re-read as 2x2 sub-tiles")
            return items, notes, tin + (r.prompt_tokens or 0), tout + (r.completion_tokens or 0), ncalls
        for it in items:
            it.box = [round(it.box[0] + x0, 1), round(it.box[1] + y0, 1), round(it.box[2] + x0, 1),
                      round(it.box[3] + y0, 1)]
            it.tile = i
        return items, notes, tin + (r.prompt_tokens or 0), tout + (r.completion_tokens or 0), ncalls

    with cf.ThreadPoolExecutor(min(workers, len(boxes))) as ex:
        ctxs = [contextvars.copy_context() for _ in boxes]
        res = list(ex.map(lambda cb: cb[0].run(read_one, cb[1]), zip(ctxs, enumerate(boxes))))
    allitems = []
    for items, notes, ti, to, nc in res:
        allitems += items
        tl.notes += notes
        tl.tokens_in += ti
        tl.tokens_out += to
        tl.calls += nc
    tl.items = merge_items(allitems)
    tl.seconds = round(time.perf_counter() - t0, 2)
    return tl


def mark_ocr_exact(ans):
    """Evidence.exact (D25) for OCR-read text (O1): an evidence box that IS a line box of an OCR layer read in this
    process gets exact=True (PP-OCR detector boxes sit on the printed pixels; VLM text-spotting boxes are guesses and
    stay None -> E1's mark_exact makes them False). Regions, fills and measured boxes never equal a line box."""
    if not getattr(ans, "evidence", None):
        return ans
    out, changed = [], False
    for e in ans.evidence:
        if e.exact is None and e.bbox and e.bbox_space == "image_px":
            tl = _mem.get(f"{e.file}{file_tag(e.file)}|ocr")
            if tl is not None and tuple(round(v, 1) for v in e.bbox) in {tuple(round(v, 1) for v in i.box)
                                                                           for i in tl.items}:
                e = e.model_copy(update={"exact": True})
                changed = True
        out.append(e)
    if changed:
        ans.evidence = out
    return ans


# -- search ---------------------------------------------------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s/:.'\"=-]", " ", s)).strip()


def match_score(query: str, text: str) -> float:
    """1.0 = query contained in text (normalised); else the best fuzzy ratio of the query vs a same-length window."""
    q, t = norm(query), norm(text)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    qw, tw = q.split(), t.split()
    best = 0.0
    for i in range(max(1, len(tw) - len(qw) + 1)):
        win = " ".join(tw[i:i + len(qw)])
        best = max(best, difflib.SequenceMatcher(None, q, win).ratio())
    return best


def find_text(path: str, query: str, threshold: float = 0.8, layer: TextLayer | None = None, **kw) -> list[dict]:
    """Hits of `query` in the drawing's text layer, best first: [{text, box, score}] (boxes in image px)."""
    tl = layer or text_layer(path, **kw)
    hits = [{"text": i.text, "box": i.box, "score": round(match_score(query, i.text), 2)} for i in tl.items]
    return sorted([h for h in hits if h["score"] >= threshold], key=lambda h: -h["score"])


def evidence(path: str, hits: list[dict], note: str, limit: int = 6) -> list[Evidence]:
    return [Evidence(file=path, page=0, bbox=[round(v, 1) for v in h["box"]], bbox_space="image_px",
                     note=f"{note}: '{h['text'][:60]}'") for h in hits[:limit]]


# -- parsers: scale, dimensions, areas -------------------------------------------------------------------------------

_FRAC = r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
_RATIO = re.compile(r"(?<![\d/.])1\s*[:]\s*(\d{1,3}(?:[ ,.]\d{3})+|\d{1,6})(?![\d/])")  # '1:51/2' = 1'-5 1/2" misread
_IMPERIAL = re.compile(_FRAC + r"\s*(?:\"|”|''|in\.?|inch(?:es)?)\s*=\s*(\d+)\s*(?:'|’|ft\.?|feet|foot)\s*-?\s*(\d+)?\s*(?:\"|”)?",
                       re.I)
_SCALE_WORD = re.compile(r"scale|mittakaava|mittak\.|maßstab|massstab|échelle|echelle|skala|scala|escala|schaal|"
                         r"\bm\s*1\s*:|\bm\.?\s?k\.?(?=\s*1\s*[:/])|\bmst\.?(?=\s*1\s*[:/])", re.I)
# '1/500' is a scale only next to a scale word (ASEMAPIIRROS M.K. 1/500, Skala 1/100): bare '1/16' is a fraction
_STD_SCALES = {20, 25, 50, 100, 200, 250, 400, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000}
_SLASH = re.compile(r"(?<![\d/.])1\s*/\s*(\d{1,3}(?:[ ,.]\d{3})+|\d{2,6})(?![\d/\"”'’])")


def _frac(s: str) -> float:
    s = s.strip()
    if " " in s:
        a, b = s.split(None, 1)
        return float(a) + _frac(b)
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


# OCR variants of imperial notes (HABS, Q bug 29): '1"=12000' / '1"=400"' (feet only in a separate 'FEET' label of the
# graphic scale), '1"= 25 Miles', 'SCALE: 1/8" - 1'-0"' (a dash read for '=': only on a line with a scale word)
_IMPERIAL_OCR = re.compile(_FRAC + r"\s*(?:\"|”|''|in\.?|inch(?:es)?)\s*([=\-–])\s*(\d{1,3}(?:,\d{3})+|\d+)\s*"
                           r"(?:(?P<ft>'|’|ft\.?|feet|foot)\s*-?\s*(?P<inch>\d+)?|(?P<mi>mi\.?|miles?)\b|(?P<q>\"|”))?", re.I)
_MILE_IN = 63360


def parse_scales(text: str) -> list[dict]:
    """Scale notes in one text line -> [{ratio, kind, text}] ('1:100' -> 100; 1/4" = 1'-0" -> 48)."""
    out = []
    for m in _IMPERIAL.finditer(text):
        try:
            inch = _frac(m.group(1))
            real_in = int(m.group(2)) * 12 + (int(m.group(3)) if m.group(3) else 0)
        except (ValueError, ZeroDivisionError):
            continue
        if inch > 0 and real_in > 0:
            out.append({"ratio": round(real_in / inch, 2), "kind": "imperial", "text": m.group(0).strip()})
    if not out:
        worded = bool(_SCALE_WORD.search(text))
        for m in _IMPERIAL_OCR.finditer(text):
            if m.group(2) != "=" and not worded:
                continue
            try:
                inch, n = _frac(m.group(1)), int(m.group(3).replace(",", ""))
            except (ValueError, ZeroDivisionError):
                continue
            if inch <= 0 or n <= 0:
                continue
            if m.group("ft"):
                out.append({"ratio": round((n * 12 + int(m.group("inch") or 0)) / inch, 2), "kind": "imperial",
                            "text": m.group(0).strip()})
            elif m.group("mi"):
                out.append({"ratio": round(n * _MILE_IN / inch, 2), "kind": "imperial", "text": m.group(0).strip()})
            elif n >= 100:  # '1"=12000': the unit is printed elsewhere (scales() needs a 'FEET' label on the sheet)
                out.append({"ratio": round(n * 12 / inch, 2), "kind": "imperial", "text": m.group(0).strip(),
                            "unit_from_label": "feet"})
    if not out:
        for m in _RATIO.finditer(text):
            n = int(re.sub(r"[ ,.]", "", m.group(1)))
            if 1 <= n <= 1_000_000:
                out.append({"ratio": float(n), "kind": "ratio", "text": m.group(0)})
    if not out:
        worded = bool(_SCALE_WORD.search(text))
        for m in _SLASH.finditer(text):
            n = int(re.sub(r"[ ,.]", "", m.group(1)))
            if (worded and 10 <= n <= 1_000_000) or n in _STD_SCALES:  # 'POHJA 1/100' (Finnish plan title)
                out.append({"ratio": float(n), "kind": "ratio", "text": m.group(0)})
    return out


def scales(tl: TextLayer) -> list[dict]:
    """Scale notes of the drawing; lines that say 'scale'/'mittakaava'/... rank first. Each hit carries its box."""
    out = []
    feet_label = any(re.fullmatch(r"\s*(?:scale\s*(?:in\s*)?)?(?:feet|ft\.?)\s*", i.text, re.I) for i in tl.items)
    for it in tl.items:
        for s in parse_scales(it.text):
            if s.get("unit_from_label") and not feet_label:
                continue  # '1"=12000' with no 'FEET' on the sheet: the unit is a guess, not a fact
            s.update(box=it.box, line=it.text, labelled=bool(_SCALE_WORD.search(it.text)))
            out.append(s)
    return sorted(out, key=lambda s: (not s["labelled"], s["kind"] != "imperial"))


_FTIN = re.compile(r"(?<![\d.])(\d{1,3})\s*(?:'|’|ft)\s*-?\s*(\d{1,3}/\d{1,2}(?![\d/])|\d{1,2}(?:\s+\d{1,2}/\d{1,2}|\.\d+)?)?"
                   r"\s*(?:\"|”)?")
_STACK_DEN = (2, 4, 8, 16)


def inch_value(s: str) -> float:
    """The inch part of a feet-inch string -> inches. A stacked fraction loses its space in OCR and VLM layers
    (D1 20:12: HABS ak0040 "13'-11/2"" = 13'-1 1/2", its chain sums to the printed 73'-9"): 'NM/D' with D in
    2/4/8/16 and NM > D is N + M/D (11/2 -> 1 1/2, 51/2 -> 5 1/2, 39/16 -> 3 9/16, 113/16 -> 1 13/16). A reduced
    fraction's numerator is odd and < D, and the whole inches stay < 12; anything else keeps the plain reading."""
    s = s.strip()
    m = re.fullmatch(r"(\d{2,3})/(\d{1,2})", s)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if den in _STACK_DEN and num > den:
            for k in ((2, 1) if den == 16 and num >= 100 else (1,)):
                whole, part = divmod(num, 10 ** k)
                if part % 2 == 1 and part < den and whole < 12 and (k == 1 or part >= 10):
                    return whole + part / den
    return _frac(s)
_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s*(m²|m2|m\^2|sq\.?\s*ft|ft²|sf)\b", re.I)


def dimensions(tl: TextLayer) -> list[dict]:
    """Imperial dimension strings (12'-6" -> 12.5 ft) with boxes. Metric dimension numbers are too ambiguous to
    parse without context (a bare '3600' may be mm, a room number or a year), so they are left to the reader."""
    out = []
    for it in tl.items:
        if parse_scales(it.text):
            continue
        for m in _FTIN.finditer(it.text):
            ft = int(m.group(1)) + (inch_value(m.group(2)) / 12 if m.group(2) else 0)
            out.append({"feet": round(ft, 3), "text": m.group(0).strip(), "box": it.box})
    return out


def areas(tl: TextLayer) -> list[dict]:
    out = []
    for it in tl.items:
        for m in _AREA.finditer(it.text):
            v = float(m.group(1).replace(",", "."))
            unit = "m2" if m.group(2).lower().startswith("m") else "ft2"
            out.append({"value": v, "unit": unit, "text": m.group(0), "line": it.text, "box": it.box})
    return out


def summary(path: str, max_lines: int = 80, **kw) -> str:
    """Compact table of the text layer for an LLM: texts with their positions (x%, y%), then parsed facts."""
    tl = text_layer(path, **kw)
    W, H = tl.size
    lines = [f"text layer of {Path(path).name} ({W}x{H} px, {len(tl.items)} texts read by a VLM in {tl.tiles} "
             f"tile(s); positions in % of width/height):"]
    for it in tl.items[:max_lines]:
        lines.append(f"- ({100 * it.cx / W:.0f}%, {100 * it.cy / H:.0f}%) {it.text[:90]}")
    if len(tl.items) > max_lines:
        lines.append(f"... {len(tl.items) - max_lines} more (use find_text)")
    sc = scales(tl)
    if sc:
        lines.append("scale notes: " + "; ".join(f"'{s['line'][:50]}' -> 1:{s['ratio']:g}" for s in sc[:3]))
    else:
        lines.append("scale notes: none found (lengths/areas need a scale bar, a dimension string or an assumption)")
    try:  # graphic scale bars (CPU only here: labels from this layer, snapped to the drawn bar)
        from stoa.solvers.raster import scalebar

        for b in scalebar.bars(path, reread=False, layer=tl)[:2]:
            lines.append(b.describe())
    except Exception:  # noqa: BLE001 - optional fact
        pass
    ds = dimensions(tl)
    if ds:
        lines.append(f"dimension strings ({len(ds)}): " + ", ".join(f"{d['text']}={d['feet']:g} ft" for d in ds[:12]))
    ar = areas(tl)
    if ar:
        lines.append(f"printed areas ({len(ar)}): " + ", ".join(f"{a['line'][:30]}" for a in ar[:12]))
    return "\n".join(lines)
