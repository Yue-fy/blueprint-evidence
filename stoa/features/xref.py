"""Cross-references on a drawing set (F8, captain's feature list, Sat 19:54).

A construction set talks to itself: a section bubble 'A6 over S-500F' on the foundation plan means "section A6 is
drawn on sheet S-500F", a note says 'SEE SHEET S-501 FOR PIPE TRENCH DETAIL', a door tag points at the door
schedule on A-601. This module reads those references from the PDF text layer, resolves each one to the page of
the set that carries the target sheet (and, for a detail / section, to the target view's box via VP's
stoa.sheet.views), and plans the hop a question should take ('what is the hardware of door 101?' asked on the
floor plan -> the schedule sheet).

    idx = sheet_index("AW_078-24-33.pdf")        # [{page, number, title}] one per page, ~0.05 s / page, cached
    refs = page_refs("AW_078-24-33.pdf", 55)     # [Ref] on S-100F: 18 callouts / notes, resolved to pages + view boxes
    plan = follow_plan(path, 68, "What is the hardware set of door 101?")   # the hop to the schedule sheet

Deterministic, text only, $0 (cost tier: cheap). Boxes are in the CONTRACT evidence space (PyMuPDF's unrotated
pdf_pt). Images are not read here (no OCR): their refs list is empty. The API router (prefix /feat/xref) is behind
the STOA_FEATURES flag ('xref'); every endpoint fails closed (an empty list / no plan) on an unreadable file.
"""

from __future__ import annotations

import functools
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

FLAG = "xref"

# sheet numbers: 'S-100F', 'A-101F', 'EP101P', 'CS101', 'G-001', 'A1.01', 'M-101A' (discipline letters + number)
_NUM = r"[A-Z]{1,3}-?\d{3}[A-Z]?|[A-Z]{1,2}\d\.\d{1,3}[A-Z]?|[A-Z]{1,2}-\d{1,2}(?:\.\d{1,2})?[A-Z]?"
SHEET_TOKEN = re.compile(rf"^(?:{_NUM})$")
_STRICT = re.compile(r"^(?:[A-Z]{1,3}-?\d{3}[A-Z]?|[A-Z]{1,2}\d\.\d{1,3}[A-Z]?)$")
SHEET_IN_TEXT = re.compile(rf"(?<![A-Z0-9/-])(?:([A-Z]{{0,2}}\d{{1,3}}[A-Z]?|[A-Z]{{1,2}})\s*/\s*)?({_NUM})(?![A-Z0-9-])")
DETAIL_ID = re.compile(r"^(?:[A-Z]{0,2}\d{1,3}[A-Z]?|[A-Z]{1,2})$")
# UK BS 1192 / ISO 19650 drawing numbers: PROJECT-ORIGINATOR-VOLUME-LEVEL-TYPE-ROLE-NUMBER ('SEN-DFE-XX-00-D-X-0201',
# 'PRJ-ARC-ZZ-01-DR-A-1001'): >= 6 fields, the last one digits. A scan's OCR may keep only the start ('SEN-DFE-XX-00-').
ISO_NUMBER = re.compile(r"^(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,8}(?:-[A-Z0-9]{1,6}){4,7}-\d{3,5}$")
_ISO_PART = re.compile(r"^[A-Z]{2,8}-[A-Z0-9]{2,6}(?:-[A-Z0-9]{1,6}){1,5}-?$")
_TITLE_LABEL = re.compile(r"^(?:DRAWING\s+|SHEET\s+)?TITLE\s*:?$", re.I)
_SEE = re.compile(r"\b(?:SEE|REFER\s+TO|REF\.?|PER|ON|AT)\b", re.I)
_FOR = re.compile(r"\bFOR\s+(.+)$", re.I)
_DISCIPLINE = (
    (re.compile(r"\bSTRUCT(?:URAL|\.)?\b|\bSTRUCT\b", re.I), "S", "structural"),
    (re.compile(r"\bMECH(?:ANICAL|\.)?\b", re.I), "M", "mechanical"),
    (re.compile(r"\bELEC(?:TRICAL|T?\.)?\b", re.I), "E", "electrical"),
    (re.compile(r"\bARCH(?:ITECTURAL|\.)?\b", re.I), "A", "architectural"),
    (re.compile(r"\bCIVIL\b", re.I), "C", "civil"),
    (re.compile(r"\bPLUMB(?:ING|\.)?\b", re.I), "P", "plumbing"),
)
_DISC_SHEETS = re.compile(r"\b(?:SHTS?\.?|SHEETS?|DRAWINGS?|DWGS?\.?|PLANS?)\b", re.I)
# a sheet whose title names a schedule (door / window / room / finish / hardware / equipment)
_SCHEDULE_TITLE = re.compile(r"\bSCHEDULES?\b", re.I)


# ---------------------------------------------------------------------------------------------------- text lines
@dataclass
class _Line:
    text: str
    size: float
    box: tuple          # displayed frame (the page as seen: /Rotate applied), pt
    src: tuple          # unrotated pdf_pt (CONTRACT evidence space)
    dir: tuple          # reading direction in the displayed frame, rounded to a cardinal (1,0) (0,1) (0,-1) (-1,0)


def _cardinal(dx: float, dy: float) -> tuple:
    if abs(dx) >= abs(dy):
        return (1, 0) if dx >= 0 else (-1, 0)
    return (0, 1) if dy >= 0 else (0, -1)


def _page_lines(page) -> list[_Line]:
    import pymupdf as fitz

    M = page.rotation_matrix
    out = []
    for b in page.get_text("dict").get("blocks", []):
        for l in b.get("lines", []):
            spans = [s for s in l.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = re.sub(r"\s+", " ", "".join(s["text"] for s in l["spans"])).strip()
            src = fitz.Rect(l["bbox"])
            disp = src * M
            o = fitz.Point(0, 0) * M
            v = fitz.Point(*l.get("dir", (1, 0))) * M
            out.append(_Line(text, float(max(s["size"] for s in spans)), tuple(disp), tuple(src),
                             _cardinal(v.x - o.x, v.y - o.y)))
    return out


def _frame(box: tuple, d: tuple) -> tuple:
    """A displayed box in the reading frame of text running along d (the text then reads left to right)."""
    x0, y0, x1, y1 = box
    if d == (1, 0):
        return (x0, y0, x1, y1)
    if d == (-1, 0):
        return (-x1, -y1, -x0, -y0)
    if d == (0, 1):        # reads downward: frame x = y, frame y = -x
        return (y0, -x1, y1, -x0)
    return (-y1, x0, -y0, x1)  # (0, -1) reads upward: frame x = -y, frame y = x


def _key(num: str | None) -> str:
    return re.sub(r"[\s.\-]", "", (num or "").upper())


# ---------------------------------------------------------------------------------------------------- sheet index
@dataclass
class SheetEntry:
    page: int
    number: str | None
    title: str | None
    number_box: tuple | None = None   # unrotated pdf_pt of the title-block sheet number
    file: str | None = None           # the PDF this page belongs to (set indexes: several files dropped together)
    level: str | None = None          # storey key ('0', '1', '-1', 'roof'): ISO number field 4, else the title's words
    level_name: str | None = None     # as printed ('Level 00', 'SECOND FLOOR')
    title_box: tuple | None = None    # evidence-space box of the title line(s)

    def label(self) -> str:
        return " ".join(x for x in (self.number, self.title) if x) or f"page {self.page + 1}"


_LABEL_LINE = re.compile(r":\s*$|^(?:SHEET\s+ID|SHEET|DATE|SIZE|SCALE|DRAWN|CHECKED|DESIGNED|SUBMITTED|APPROVED|"
                         r"FILE|ISSUE|CONTRACT|SOLICITATION|PROJECT|REV|MARK|DESCRIPTION|US ARMY|U\.?S\.? ARMY|"
                         r"OF ENGINEERS|DISTRICT|DOD STANDARD|STANDARD DESIGN)\b", re.I)


def _sheet_of(page, lines: list[_Line] | None = None) -> SheetEntry:
    lines = _page_lines(page) if lines is None else lines
    return _sheet_from(lines, page.rect.width, page.rect.height, page.number)


def _sheet_from(lines: list[_Line], W: float, H: float, page_no: int = 0) -> SheetEntry:
    """The title-block sheet number (largest sheet-number line near the right / bottom edge) and the sheet title (the
    larger lines of the title-block column above it; else the page's biggest drawing title)."""
    ent = SheetEntry(page=page_no, number=None, title=None)
    try:
        _sheet_core(ent, lines, W, H)
    finally:
        _level_of(ent)
    return ent


def _level_of(ent: SheetEntry) -> None:
    """Storey from the ISO number's level field ('SEN-DFE-XX-01-…' = Level 01; 'XX' / 'ZZ' = none), else VP's storey
    reader on the title ('Special School Plan Level 00', 'SECOND FLOOR PLAN')."""
    f = (ent.number or "").rstrip("…").split("-")
    if len(f) >= 4 and (ISO_NUMBER.match(ent.number or "") or (ent.number or "").endswith("…")):
        lv = f[3].upper()
        if re.fullmatch(r"\d{2}", lv):
            ent.level, ent.level_name = str(int(lv)), f"Level {lv}"
        elif re.fullmatch(r"B\d", lv):
            ent.level, ent.level_name = str(-int(lv[1])), f"Level {lv}"
        elif lv in ("GF", "RF", "RL"):
            ent.level, ent.level_name = ("0", "Ground floor") if lv == "GF" else ("roof", "Roof")
    if ent.level is None and ent.title:
        try:
            from stoa.sheet.sets import _storey_of

            k, name = _storey_of(ent.title)
        except Exception:  # noqa: BLE001 — no stoa.sheet: no storey
            k, name = None, None
        if k is not None:
            ent.level, ent.level_name = k, name


_PROJECT_WORDS = re.compile(r"\b(?:DISTRICT|FUELING\s+SYSTEM|SYSTEM\s+TYPE|AIR\s+FORCE\s+BASE|AFB|PROJECT|CONTRACT|"
                            r"SOLICITATION|CORPS\s+OF\s+ENGINEERS|\d{3,}\s+[A-Z]+\s+(?:AVE|ST|RD|BLVD|DR)\b\.?)", re.I)
_DRAWING_WORDS = re.compile(r"\b(?:PLANS?|SECTIONS?|ELEVATIONS?|DETAILS?|SCHEDULES?|LEGENDS?|NOTES|DIAGRAMS?|RISERS?|"
                            r"ISOMETRICS?|LAYOUTS?|INDEX|ABBREVIATIONS|SYMBOLS|TABLES?|SITE|STAIRS|PITS?|VAULTS?)\b", re.I)


def _strip_shared_prefix(out: list[SheetEntry]) -> None:
    """F8 c8: 37 of AW-28's 156 titles read '1616 CAPITAL AVE. OMAHA, NE 68102 ABBREVIATIONS' (the site address printed
    over the title in the title block). A leading run of words shared by >= 15 % of the file's titles (>= 3), that looks
    like an address / project line (a 3+ digit number or a project word, no drawing word), is cut where something is left."""
    titles = [e.title for e in out if e.title]
    if len(titles) < 4:
        return
    import collections
    cnt = collections.Counter()
    for t in titles:
        w = t.split()
        for k in range(3, min(len(w) - 1, 12) + 1):
            cnt[" ".join(w[:k])] += 1
    need = max(3, int(0.15 * len(titles) + 0.999))
    good = [(len(pfx.split()), pfx) for pfx, n in cnt.items() if n >= need and not _DRAWING_WORDS.search(pfx)
            and (re.search(r"\d{3,}", pfx) or _PROJECT_WORDS.search(pfx))]
    if not good:
        return
    pfx = max(good)[1]
    for e in out:
        if e.title and e.title.startswith(pfx + " "):
            rest = e.title[len(pfx):].strip(" ,-·")
            if len(re.findall(r"[A-Za-z]", rest)) >= 4:
                e.title = rest


def _sheet_core(ent: SheetEntry, lines: list[_Line], W: float, H: float) -> None:
    edge = [l for l in lines if (SHEET_TOKEN.match(l.text) or ISO_NUMBER.match(l.text)) and
            (l.box[0] > 0.72 * W or l.box[1] > 0.8 * H)]
    if not edge:  # a scan's OCR kept only the start of an ISO number ('SEN-DFE-XX-00-'): the number, marked '…'
        part = [l for l in lines if _ISO_PART.match(l.text) and l.box[0] > 0.6 * W and l.box[1] > 0.75 * H]
        if part:
            num = max(part, key=lambda l: len(l.text))
            ent.number, ent.number_box = num.text.rstrip("-") + "…", num.src
            _label_title(ent, lines, W, H)
        return
    num = max(edge, key=lambda l: (l.size, l.box[0] + l.box[1]))
    ent.number, ent.number_box = num.text, num.src
    if ISO_NUMBER.match(num.text) and _label_title(ent, lines, W, H):
        return
    # title: lines in the title-block column (x overlapping the number's column), above it, print >= 35 % of the
    # number's, not a field label, not another number; the run of adjacent lines nearest the number's top wins
    col0, col1 = num.box[0] - 0.06 * W, max(num.box[2], W)
    cands = [l for l in lines if l is not num and l.box[0] >= col0 and l.box[2] <= col1 + 1 and l.box[3] <= num.box[1] + 2
             and l.box[1] > 0.45 * H and 0.35 * num.size <= l.size < num.size and len(re.findall(r"[A-Za-z]", l.text)) >= 4
             and not _LABEL_LINE.search(l.text) and not SHEET_TOKEN.match(l.text)
             and not re.search(r"(?:\b\w ){4,}", l.text)]  # letter-spaced title-block fonts ('S O L I C I T')
    if cands:
        cands0 = cands
        big = max(c.size for c in cands)
        cands = [c for c in cands if c.size >= 0.85 * big]

        def dist(l):
            return max(num.box[1] - l.box[3], 0) + max(l.box[0] - num.box[2], 0, num.box[0] - l.box[2])

        seed = min(cands, key=dist)                  # USACE title blocks print the title sideways (dir 0,-1)
        same = sorted([c for c in cands if c.dir == seed.dir], key=lambda l: _frame(l.box, l.dir)[1])
        i = same.index(seed)
        lo = hi = i
        while lo > 0 and _frame(same[lo].box, seed.dir)[1] - _frame(same[lo - 1].box, seed.dir)[3] <= 1.2 * seed.size:
            lo -= 1
        while hi + 1 < len(same) and _frame(same[hi + 1].box, seed.dir)[1] - _frame(same[hi].box, seed.dir)[3] <= 1.2 * seed.size:
            hi += 1
        ent.title = " ".join(l.text for l in same[lo:hi + 1])[:120]
        ent.title_box = tuple(_union(_union(same[lo].src, same[hi].src), same[i].src))
        if _PROJECT_WORDS.search(ent.title) and not _DRAWING_WORDS.search(ent.title):
            # F8 c8 (UX P2b-5): the OCR of Stoa's M-101A picture gave 'RESSURIZED HYDRANT OMAHA DISTRICT FUELING SYSTEM
            # TYPE III' (the project's name, printed bigger) for a sheet titled 'PIPING PLAN': a drawing title wins
            alt = [c for c in cands0 if _DRAWING_WORDS.search(c.text) and not _PROJECT_WORDS.search(c.text)]
            if alt:
                b = min(alt, key=dist)
                ent.title, ent.title_box = b.text[:120], tuple(b.src)
    else:
        body = [l for l in lines if l.box[2] < 0.85 * W and l.size >= 1.8 * num.size * 0.5 and
                len(re.findall(r"[A-Za-z]", l.text)) >= 6 and not SHEET_TOKEN.match(l.text)]
        if body:
            b = max(body, key=lambda l: l.size)
            ent.title, ent.title_box = b.text[:120], tuple(b.src)


def _label_title(ent: SheetEntry, lines: list[_Line], W: float, H: float) -> bool:
    """UK / ISO title blocks label their fields: the drawing title is the line under 'TITLE' (DfE: 'TITLE' 6.9 pt, then
    'Special School Plan Level 00' 12 pt), plus a second line of the same print right below it."""
    labels = [l for l in lines if _TITLE_LABEL.match(l.text) and (l.box[0] > 0.55 * W or l.box[1] > 0.7 * H)]
    for lab in labels:
        h = max(lab.box[3] - lab.box[1], 1.0)
        below = [l for l in lines if l is not lab and l.dir == lab.dir and l.box[1] >= lab.box[3] - 0.3 * h and
                 l.box[1] - lab.box[3] <= 6 * h and lab.box[0] - 2 * h <= l.box[0] <= lab.box[0] + 6 * h and
                 len(re.findall(r"[A-Za-z]", l.text)) >= 4 and not _LABEL_LINE.search(l.text) and
                 not re.search(r"\d{4}-\d{2}-\d{2}", l.text)]
        if not below:
            continue
        first = min(below, key=lambda l: l.box[1])
        run = [first]
        for l in sorted(below, key=lambda l: l.box[1]):
            if l is not first and abs(l.size - first.size) <= 0.15 * first.size and \
                    0 <= l.box[1] - run[-1].box[3] <= 0.8 * first.size and abs(l.box[0] - first.box[0]) <= 2 * h:
                run.append(l)
        ent.title = " ".join(l.text for l in run)[:120]
        ent.title_box = tuple(_union(run[0].src, run[-1].src))
        return True
    return False


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_BUILD: dict = {}   # key -> lock while one thread builds a sheet index


def _file_key(path: str) -> tuple:
    st = os.stat(path)
    return (os.path.realpath(path), st.st_size, int(st.st_mtime))


def _is_pdf(path: str) -> bool:
    return str(path).lower().endswith(".pdf")


IMAGE_EXT = (".png", ".jpg", ".jpeg")


def _is_image(path: str) -> bool:
    return str(path).lower().endswith(IMAGE_EXT)


def _image_lines(path: str) -> tuple[list[_Line], float, float] | None:
    """An image's printed text from its text layer IF it was read before (the upload warm-up reads it; memory or disk
    cache) — never a cold OCR / VLM read here. Boxes in image px (= the evidence space 'image_px'). Tiles overlap, so
    an item mostly inside an earlier one is dropped ('S-500E' under 'S-500F')."""
    try:
        from stoa.solvers.raster.sheet import cached_layer

        layer = cached_layer(str(path))
    except Exception:  # noqa: BLE001
        return None
    if layer is None:
        return None
    import collections

    texts = [re.sub(r"\s+", " ", it.text or "").strip() for it in layer.items]
    freq = collections.Counter(texts)
    kept: list[_Line] = []
    for it, t in zip(layer.items, texts):
        if not t:
            continue
        b = tuple(float(x) for x in it.box)
        area = max((b[2] - b[0]) * (b[3] - b[1]), 1.0)
        w, h = b[2] - b[0], b[3] - b[1]
        vertical = h > 2.5 * w and len(t) >= 3
        line = _Line(t, (w if vertical else h) * 0.8, b, b, (0, -1) if vertical else (1, 0))
        j = next((j for j, k in enumerate(kept) if max(0.0, min(b[2], k.box[2]) - max(b[0], k.box[0])) *
                  max(0.0, min(b[3], k.box[3]) - max(b[1], k.box[1])) > 0.6 * area), None)
        if j is None:
            kept.append(line)
        elif freq[t] > freq[kept[j].text]:  # two tiles read one label: keep the reading the sheet prints more often
            kept[j] = line
    kept = _join_tile_cuts(kept)
    W, H =(layer.size if getattr(layer, "size", None) else (max(l.box[2] for l in kept), max(l.box[3] for l in kept)))
    return kept, float(W), float(H)


def _join_tile_cuts(lines: list[_Line]) -> list[_Line]:
    """A label cut by a tile edge comes back as two overlapping pieces on one row whose texts overlap
    ('SEN-DFE-XX-00-' + '-00-D-X-0201'): join them ('SEN-DFE-XX-00-D-X-0201'). Only pieces whose boxes overlap AND whose
    texts share >= 2 characters at the seam are joined, so two neighbouring labels never merge."""
    out = list(lines)
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(out):
            h = max(a.box[3] - a.box[1], 1.0)
            for j, b in enumerate(out):
                if i == j or a.dir != (1, 0) or b.dir != (1, 0) or not (a.box[0] < b.box[0] <= a.box[2]) or \
                        b.box[2] <= a.box[2] or abs(a.box[1] - b.box[1]) > 0.5 * h:
                    continue
                ov = next((k for k in range(min(len(a.text), len(b.text)) - 1, 1, -1) if a.text.endswith(b.text[:k])), 0)
                if ov < 2:
                    continue
                box = (a.box[0], min(a.box[1], b.box[1]), b.box[2], max(a.box[3], b.box[3]))
                out[i] = _Line(a.text + b.text[ov:], max(a.size, b.size), box, box, a.dir)
                del out[j]
                changed = True
                break
            if changed:
                break
    return out


def _lines_of(path: str, page: int) -> tuple[list[_Line], float, float] | None:
    """(lines, W, H) of one page of a PDF, or of an image with a cached text layer; None = nothing readable."""
    if _is_image(path):
        return _image_lines(path) if int(page) == 0 else None
    import pymupdf as fitz

    with fitz.open(path) as doc:
        if not 0 <= int(page) < doc.page_count:
            return None
        pg = doc[int(page)]
        lines, W, H = _page_lines(pg), pg.rect.width, pg.rect.height
        if len(lines) < 5 and doc.page_count == 1 and pg.rotation == 0:
            # a scan: None until its text is read (nothing gets cached meanwhile); a short vector page keeps its lines
            return _scan_pdf_lines(path, W, H) or ((lines, W, H) if lines else None)
        return lines, W, H


def _scan_pdf_lines(path: str, W: float, H: float) -> tuple[list[_Line], float, float] | None:
    """A one-page SCANNED PDF (no text of its own): the text layer the upload warm-up read from the page's image
    (stoa.formats.warm: normalize.scan_png '<stem>.scan.png', or render_png '<stem>.r<dpi>.png' for outlined text),
    scaled back to pdf_pt by the image's px per pt. Cached layers only (never a cold read); None until it was read."""
    got = None
    try:   # a scanned print: the warm-up (normalize.prepare) reads the page's own image, '<stem>.scan.png'
        from stoa.formats.normalize import scan_png

        got = scan_png(path)
    except Exception:  # noqa: BLE001
        got = None
    if not got:
        try:   # text drawn as outlines: the warm-up reads the page render, '<stem>.r<dpi>.png' (cached on disk)
            from stoa.formats.normalize import render_png

            got = render_png(path)
        except Exception:  # noqa: BLE001
            return None
    png, zoom = got
    r = _image_lines(png)
    if not r or not r[0] or not zoom:
        return None
    k = 1.0 / zoom
    return [_Line(l.text, l.size * k, tuple(v * k for v in l.box), tuple(v * k for v in l.src), l.dir)
            for l in r[0]], W, H


def sheet_index(path: str) -> list[SheetEntry]:
    """One SheetEntry per page of a PDF (images / other files: []). Cached per file (path, size, mtime)."""
    if _is_image(path):
        r = _image_lines(path)
        return [_sheet_from(r[0], r[1], r[2], 0)] if r and r[0] else []
    if not _is_pdf(path):
        return []
    key = ("idx", *_file_key(path))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
        build = _BUILD.setdefault(key, threading.Lock())
    with build:  # /sheets and /refs arrive together on a fresh upload: build the index once, the other call waits
        with _CACHE_LOCK:
            if key in _CACHE:
                return _CACHE[key]
        import pymupdf as fitz

        out = []
        with fitz.open(path) as doc:
            if doc.page_count == 1 and len(_page_lines(doc[0])) < 5:   # a scanned one-page PDF: its render's cached
                r = _lines_of(path, 0)                                   # text layer, not cached here until read
                return [_sheet_from(r[0], r[1], r[2], 0)] if r and r[0] else [SheetEntry(page=0, number=None, title=None)]
            for pg in doc:
                try:
                    out.append(_sheet_of(pg))
                except Exception:  # noqa: BLE001 — one odd page never sinks the set
                    out.append(SheetEntry(page=pg.number, number=None, title=None))
        try:
            _strip_shared_prefix(out)
        except Exception:  # noqa: BLE001
            pass
        with _CACHE_LOCK:
            _CACHE[key] = out
    return out


def lookup(number: str, idx: list[SheetEntry], own: str | None = None) -> tuple[SheetEntry | None, str]:
    """The sheet of the set a printed reference names. Exact number first; else the same number with this sheet's
    own suffix letter (USACE sets: 'S-501' printed on S-100F = S-501F, F = filter building) or the only sheet that
    extends it. Returns (entry | None, note)."""
    k = _key(number)
    if not k:
        return None, ""
    by = {}
    for e in idx:
        if e.number:
            by.setdefault(_key(e.number), e)
    if k in by:
        return by[k], ""
    m = re.search(r"([A-Z])$", _key(own) or "")
    if m and k[-1:].isdigit() and k + m.group(1) in by:
        e = by[k + m.group(1)]
        return e, f"{number} is not a sheet of this set; read as {e.number} (same suffix as this sheet, {own})"
    ext = [e for kk, e in by.items() if kk.startswith(k) and len(kk) == len(k) + 1 and kk[-1].isalpha() and k[-1:].isdigit()]
    if len(ext) == 1:
        return ext[0], f"{number} is not a sheet of this set; read as {ext[0].number} (the only sheet that extends it)"
    return None, (f"{number} is not in this upload" + (f" (candidates: {', '.join(e.number for e in ext)})" if ext else ""))


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def set_index(paths: list[str]) -> list[SheetEntry]:
    """One index over several PDFs dropped together (a project's sheets as separate files): every entry knows its
    file. Order = the order of `paths`; unreadable files are skipped."""
    from dataclasses import replace

    out = []
    for p in paths:
        try:
            out += [replace(e, file=str(p)) for e in sheet_index(p)]
        except Exception:  # noqa: BLE001
            continue
    return out


def _index_for(path: str, set_paths: list[str] | None) -> list[SheetEntry]:
    if set_paths and len(set_paths) > 1 and any(_same(path, p) for p in set_paths):
        return set_index(set_paths)
    return set_index([path])


def _set_key(set_paths: list[str] | None) -> tuple:
    return tuple(_file_key(p) for p in set_paths) if set_paths and len(set_paths) > 1 else ()


# ---------------------------------------------------------------------------------------------------- references
@dataclass
class Ref:
    page: int                      # page the reference is printed on (0-based)
    kind: str                      # callout | note | discipline | tag | copy | level
    text: str                      # as printed ('A6 / S-500F', 'SEE SHEET S-501 FOR PIPE TRENCH DETAIL')
    bbox: list                     # unrotated pdf_pt on `page`
    sheet: str | None = None       # target sheet number as printed
    detail: str | None = None      # target view id ('A6', '3') for a callout
    subject: str | None = None     # what the note says is there ('PIPE TRENCH DETAIL')
    target_page: int | None = None
    target_file: str | None = None     # set indexes: the other PDF the target page is in (None = this file)
    target_sheet: str | None = None
    target_title: str | None = None
    target_view: str | None = None     # the target view's title ('FOUNDATION SECTION 5')
    target_bbox: list | None = None    # the target view's extent on target_page (unrotated pdf_pt)
    targets: list = field(default_factory=list)   # discipline refs: [{page, number, title}]
    note: str = ""
    transform: dict | None = None      # copy / level refs: register() fit of this page onto the target page

    @property
    def resolved(self) -> bool:
        return self.target_page is not None or bool(self.targets)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["resolved"] = self.resolved
        return d


def _callouts(lines: list[_Line], own_box: tuple | None) -> list[tuple[_Line, _Line | None]]:
    """(sheet line, detail-id line) pairs: a bubble prints the view id over the sheet number."""
    out = []
    ids = [l for l in lines if DETAIL_ID.match(l.text) and len(l.text) <= 4]
    for t in lines:
        if not SHEET_TOKEN.match(t.text) or (own_box and tuple(t.src) == tuple(own_box)):
            continue
        tf = _frame(t.box, t.dir)
        h = max(tf[3] - tf[1], 1.0)
        w = tf[2] - tf[0]
        best, bd = None, 1e9
        for d in ids:
            if d.dir != t.dir or not (0.55 * t.size <= d.size <= 1.8 * t.size):
                continue
            df = _frame(d.box, d.dir)
            cx_t, cx_d = (tf[0] + tf[2]) / 2, (df[0] + df[2]) / 2
            gap = tf[1] - df[3]
            if abs(cx_t - cx_d) <= 0.6 * max(w, df[2] - df[0]) and -0.35 * h <= gap <= 1.6 * h:
                dist = abs(gap) + abs(cx_t - cx_d)
                if dist < bd:
                    best, bd = d, dist
        out.append((t, best))
    return out


def _union(a: tuple, b: tuple) -> list:
    return [round(min(a[0], b[0]), 1), round(min(a[1], b[1]), 1), round(max(a[2], b[2]), 1), round(max(a[3], b[3]), 1)]


def _box(a: tuple) -> list:
    return [round(float(x), 1) for x in a]


def _find_view(path: str, page: int, detail: str | None) -> tuple[str | None, list | None]:
    """The view numbered `detail` on `page` (VP's stoa.sheet.views), as (title, bbox pdf_pt); else (None, None)."""
    if not detail:
        return None, None
    try:
        vs = _views_cached(path, page)
    except Exception:  # noqa: BLE001 — no stoa.sheet (older trees) or an odd page: the title-bubble search below
        vs = []
    for v in vs:
        if v.number and _key(v.number) == _key(detail):
            b = getattr(v, "bbox", None) or getattr(v, "title_bbox", None)  # a view without an extent: its title
            if b:
                return v.title, _box(b)
    # no numbered view: a big-print title bubble with this id on the page (title line next to it)
    try:
        r = _lines_of(path, page)
        lines = r[0] if r else []
    except Exception:  # noqa: BLE001
        return None, None
    if not lines:
        return None, None
    med = sorted(l.size for l in lines)[len(lines) // 2]
    for l in lines:
        if l.text == detail and l.size >= 1.6 * med:
            lf = _frame(l.box, l.dir)
            near = [m for m in lines if m is not l and m.size >= 1.4 * med and m.dir == l.dir and
                    len(re.findall(r"[A-Za-z]", m.text)) >= 4 and abs(_frame(m.box, m.dir)[1] - lf[1]) <= 2 * l.size and
                    0 <= _frame(m.box, m.dir)[0] - lf[2] <= 12 * l.size]
            if near:
                m = min(near, key=lambda m: _frame(m.box, m.dir)[0])
                return m.text, _union(l.src, m.src)
    return None, None


@functools.lru_cache(maxsize=256)
def _views_cached_key(path: str, page: int, _size: int, _mtime: int):
    from stoa.sheet.views import views

    if _is_image(path):  # VP's views read an image only from the caller's text layer: the cached one, never a cold read
        from stoa.solvers.raster.sheet import cached_layer

        layer = cached_layer(path)
        return views(path, page, layer=layer) if layer is not None else []
    return views(path, page)


def _views_cached(path: str, page: int):
    _, size, mtime = _file_key(path)
    return _views_cached_key(os.path.realpath(path), int(page), size, mtime)


def _resolve(ref: Ref, path: str, idx: list[SheetEntry], own: str | None, with_views: bool) -> Ref:
    if ref.kind == "discipline":
        return ref
    e, note = lookup(ref.sheet or "", idx, own)
    ref.note = note
    if e is not None:
        ref.target_page, ref.target_sheet, ref.target_title = e.page, e.number, e.title
        ref.target_file = e.file if e.file and not _same(e.file, path) else None
        if with_views and ref.detail:
            ref.target_view, ref.target_bbox = _find_view(e.file or path, e.page, ref.detail)
            if ref.target_bbox is None:
                ref.note = (ref.note + "; " if ref.note else "") + f"view {ref.detail} not found on {e.number}: whole sheet"
    return ref


# '101A', 'D-12', and module-dotted '7.04B' (AFCEE MEDIUMFC A-602: module 7, room 04, leaf B)
# row keys: '101A', 'D-12', '7.04B', '9A.1A' / '9D.1C' (sub-modules, SFO A-303), '10.01A'; a key column also takes a stray
# lowercase letter suffix ('1z', SFO A-303: a real row with its own sizes) once >= 3 proper keys made the column
_ROW_KEY = re.compile(r"^(?:\d{2,4}[A-Z]?|[A-Z]{1,2}-?\d{1,3}[A-Z]?|\d{1,2}\.(?:\d{2}[A-Z]?|\d[A-Z])|\d{1,2}[A-Z]\.\d{1,2}[A-Z]?)$")
_ROW_KEY_LOOSE = re.compile(r"^\d{1,3}[a-z]$")
# DR 21:57: a key column only under a header that names the row's own id, never 'Room Number' / 'Door Type' / 'Hardware
# Group' (A-801: their codes 'A1' / '01' look like keys); no header read = accepted as before
_ID_HEAD = re.compile(r"\b(?:DOOR|WINDOW|OPENING|DR|WDW|WIN)\.?\s*(?:NO\b|NO\.|NUMBER|NUM\b|#|MARK|ID\b|TAG)|^\s*(?:NO\.?|NUMBER|MARK|TAG|ID|#)\s*$|\bMARK\b", re.I)
_NOT_ID_HEAD = re.compile(r"\b(?:ROOM|TYPE|HARDWARE|HDW|GROUP|FRAME|RATING|ELEV(?:ATION)?|FINISH|SIZE|WIDTH|HEIGHT|REMARKS?|NOTES?)\b", re.I)
# a scheduled row that is not a door leaf (estimators: 'cased openings are not doors')
_OPENING_ONLY = re.compile(r"\bCASED\s+OPENING|\bOPENING\s+ONLY\b|\bNO\s+DOOR\b|\bFRAMED\s+OPENING\b", re.I)
# a room number printed on a plan ('7.04', '101'): doors are often numbered after their room plus a leaf letter
# ('7.04A', '7.04B'), and plans like A-103 print only the room number, never the door's
_ROOM_NO = re.compile(r"^(?:\d{1,2}\.\d{1,2}|\d{3,4}|[A-Z]\d{3})$")
_TABLE_KIND = (("door", re.compile(r"\bDOOR", re.I)), ("window", re.compile(r"\bWINDOW", re.I)),
               ("room", re.compile(r"\bROOM|\bFINISH", re.I)))


def schedule_rows(path: str, page: int) -> list[dict]:
    """The rows of the schedule tables on one sheet: [{key '101A', kind door | window | room | other, table 'CUT AND
    COVER DOOR SCHEDULE', bbox (pdf_pt / image px, the row across its table)}]. A key column = >= 3 row keys left-aligned
    one under the other; its table = the nearest '... SCHEDULE' heading above it. Cached."""
    key = ("rows", *_file_key(path), int(page))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    got = _lines_of(path, page)
    out: list[dict] = []
    if got and got[0]:
        lines = got[0]
        keys = [l for l in lines if (_ROW_KEY.match(l.text) or _ROW_KEY_LOOSE.match(l.text)) and l.dir == (1, 0)]
        heads = [l for l in lines if _SCHEDULE_TITLE.search(l.text) and l.dir == (1, 0)]
        # key columns: keys whose centres line up (left- or centre-aligned cells: '101A' and '102' share a centre)
        cols: dict[int, list[_Line]] = {}
        run, last = -1, None
        for l in sorted(keys, key=lambda l: (l.box[0] + l.box[2]) / 2):
            c = (l.box[0] + l.box[2]) / 2
            if last is None or c - last > max(4.0, 0.5 * (l.box[3] - l.box[1])):
                run += 1
            cols.setdefault(run, []).append(l)
            last = c
        # a short key in a LEFT-aligned column ('1z' under '1.01A') has its centre a few points off: it joins the column
        # whose left edge it shares
        big = [c for c in cols.values() if len(c) >= 3]
        for k in [k for k, c in cols.items() if len(c) < 3]:
            for l in list(cols[k]):
                hh = l.box[3] - l.box[1]
                home = next((c for c in big if abs(sorted(m.box[0] for m in c)[len(c) // 2] - l.box[0]) <= max(1.5, 0.25 * hh)), None)
                if home is not None:
                    home.append(l)
                    cols[k].remove(l)
            if not cols[k]:
                del cols[k]
        # table heading LINE -> its LEFTMOST key column (the row keys). Keyed by the heading's position, not its text:
        # A-602 prints two side-by-side tables both titled 'DOOR SCHEDULE' (the second continues the first)
        first: dict[tuple, tuple[list[_Line], float, str]] = {}
        runs: list[list[_Line]] = []
        for col in cols.values():
            # one key column can hold two tables stacked (AW-28 A-601: PUMPHOUSE + PUMPSHELTER DOOR SCHEDULE): split
            # it where rows stop following each other
            col.sort(key=lambda l: l.box[1])
            h = sorted(l.box[3] - l.box[1] for l in col)[len(col) // 2]
            cur = [col[0]]
            for a, b in zip(col, col[1:]):
                if b.box[1] - a.box[1] > 3.5 * h + 6:
                    runs.append(cur)
                    cur = []
                cur.append(b)
            runs.append(cur)
        for col in runs:
            if sum(1 for l in col if _ROW_KEY.match(l.text)) < 3:
                continue
            h = sorted(l.box[3] - l.box[1] for l in col)[len(col) // 2]
            # the column's own header (the text just above its first key, over the column): 'Room Number' / 'Type' -> not
            # the row ids (DR 21:57); door / window tables only (a room schedule's key IS the room number)
            cx0, cx1 = min(l.box[0] for l in col), max(l.box[2] for l in col)
            hdr = " ".join(m.text for m in lines if m.dir == (1, 0) and m.box[3] <= col[0].box[1] + 1
                           and col[0].box[1] - m.box[3] < 5 * h and m.box[0] < cx1 + h and m.box[2] > cx0 - h
                           and not _ROW_KEY.match(m.text) and not _SCHEDULE_TITLE.search(m.text))
            if hdr and _NOT_ID_HEAD.search(hdr) and not _ID_HEAD.search(hdr):
                above0 = [hd for hd in heads if hd.box[3] <= col[0].box[1] + 1 and col[0].box[1] - hd.box[3] < 30 * h]
                tk = next((k for k, rx in _TABLE_KIND if above0 and rx.search(max(above0, key=lambda hd: hd.box[3]).text)), "")
                if tk in ("door", "window"):
                    continue
            above = [hd for hd in heads if hd.box[3] <= col[0].box[1] + 1 and col[0].box[1] - hd.box[3] < 30 * h
                     and hd.box[0] - 60 * h <= col[0].box[0] <= hd.box[2] + 10 * h]
            if not above:
                continue                               # a column of numbers under no schedule heading
            hd = max(above, key=lambda hd: hd.box[3])
            if hd.box not in first or col[0].box[0] < first[hd.box][0][0].box[0]:
                first[hd.box] = (col, h, hd.text)
        tabs = list(first.values())
        for col, h, table in tabs:
            kind = next((k for k, rx in _TABLE_KIND if rx.search(table)), "other")
            # a row ends where the next table to its right starts (A-602: two door tables side by side)
            right = min((c2[0].box[0] for c2, _, _ in tabs if c2[0].box[0] > col[0].box[2] + 5
                         and c2[0].box[1] <= col[-1].box[3] and col[0].box[1] <= c2[-1].box[3]), default=1e9)
            for l in col:
                band = [m for m in lines if abs((m.box[1] + m.box[3]) / 2 - (l.box[1] + l.box[3]) / 2) <= 0.6 * h
                        and l.box[0] - 30 <= m.box[0] <= l.box[0] + 60 * h and m.box[2] < right - 2]
                x1 = max(m.box[2] for m in band) if band else l.box[2]
                text = " ".join(m.text for m in sorted(band, key=lambda m: m.box[0]))
                row = {"key": l.text, "kind": kind, "table": table, "bbox": _box(
                    _src_rect(l, (l.box[0] - 4, l.box[1] - 1.5, x1 + 4, l.box[3] + 1.5))), "text": text[:200]}
                if kind == "door" and _OPENING_ONLY.search(text):
                    row["opening_only"] = True             # a cased opening: scheduled, but no door leaf
                out.append(row)
    with _CACHE_LOCK:
        _CACHE[key] = out
    return out


def _src_rect(l: _Line, disp: tuple) -> tuple:
    """A displayed-frame box on the page of line l -> the page's source (evidence) space, using l's own mapping."""
    (dx0, dy0, dx1, dy1), (sx0, sy0, sx1, sy1) = l.box, l.src
    if (dx1 - dx0) and (dy1 - dy0) and abs((dx1 - dx0) - (sx1 - sx0)) < 0.5 and abs(dx0 - sx0) < 0.5 and abs(dy0 - sy0) < 0.5:
        return disp                                    # unrotated page / image: same space
    # rotated page: map the two corners through the line's displayed->source affine (axis-aligned 90-degree turns)
    def m(x, y):
        fx = (x - dx0) / max(dx1 - dx0, 1e-6)
        fy = (y - dy0) / max(dy1 - dy0, 1e-6)
        # which source axis follows displayed x? the one whose span equals the displayed width
        if abs((dx1 - dx0) - (sx1 - sx0)) <= abs((dx1 - dx0) - (sy1 - sy0)):
            return sx0 + fx * (sx1 - sx0), sy0 + fy * (sy1 - sy0)
        return sx0 + fy * (sx1 - sx0), sy1 - fx * (sy1 - sy0)
    (ax, ay), (bx, by) = m(disp[0], disp[1]), m(disp[2], disp[3])
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def page_refs(path: str, page: int = 0, resolve: bool = True, with_views: bool = True,
              set_paths: list[str] | None = None, links: bool = True) -> list[Ref]:
    """Every cross-reference printed on one page of a PDF, resolved against the set when asked: the PDF's own pages,
    or every PDF in `set_paths` (files dropped together; a target in another file sets Ref.target_file)."""
    if not (_is_pdf(path) or _is_image(path)):
        return []
    key = ("refs", *_file_key(path), int(page), bool(resolve), bool(with_views), _set_key(set_paths), bool(links))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    got = _lines_of(path, page)
    if not got or not got[0]:
        return []                      # an image whose text layer is not read yet: nothing (and not cached)
    lines, W, H = got
    own = _sheet_from(lines, W, H, int(page))
    refs: list[Ref] = []
    seen = set()
    for t, d in _callouts(lines, own.number_box):
        bbox = _union(t.src, d.src) if d else _box(t.src)
        refs.append(Ref(page=int(page), kind="callout", text=(f"{d.text} / {t.text}" if d else t.text), bbox=bbox,
                        sheet=t.text, detail=d.text if d else None))
        seen.add(id(t))
    for l in lines:
        if id(l) in seen or SHEET_TOKEN.match(l.text):
            continue
        if own.number_box and tuple(l.src) == tuple(own.number_box):
            continue
        hits = list(SHEET_IN_TEXT.finditer(l.text))
        hits = [m for m in hits if _SEE.search(l.text[:m.start()]) or m.group(1)]
        if hits:
            fm = _FOR.search(l.text)
            for m in hits:
                refs.append(Ref(page=int(page), kind="note", text=l.text, bbox=_box(l.src), sheet=m.group(2),
                                detail=m.group(1), subject=fm.group(1).strip(" .") if fm else None))
            continue
        if re.search(r"\bSEE\b|\bREFER\b", l.text, re.I) and _DISC_SHEETS.search(l.text) or \
                re.match(r"^\s*SEE\s*$", l.text, re.I) is None and re.search(r"\bSEE\b.*\b(?:STRUCT|MECH|ELEC|ARCH)", l.text, re.I):
            discs = [(letter, name) for rx, letter, name in _DISCIPLINE if rx.search(l.text)]
            if discs:
                refs.append(Ref(page=int(page), kind="discipline", text=l.text, bbox=_box(l.src),
                                subject=", ".join(n for _, n in discs), detail=None,
                                sheet="/".join(letter for letter, _ in discs)))
    # equipment / door tags share the short form ('CV-1', 'FP-2', 'D-12'): such a token is a sheet reference only when
    # the set has that sheet; the usual 3-digit numbers ('S-500F', 'EP101P', 'A1.01') count even when not uploaded
    idx = _index_for(path, set_paths)
    have = {_key(e.number) for e in idx if e.number}
    refs = [r for r in refs if r.kind == "discipline" or _STRICT.match(r.sheet or "") or _key(r.sheet) in have]
    if resolve:
        for r in refs:
            if r.kind == "discipline":
                letters = (r.sheet or "").split("/")
                suf = (re.search(r"([A-Z])$", own.number or "") or [None, None])[1]
                ts = [e for e in idx if e.number and any(re.match(rf"^{x}[A-Z]?-?\d", e.number) for x in letters)]
                # same series first (USACE: 'F' = the filter building's sheets when this is A-101F)
                ts.sort(key=lambda e: (0 if suf and e.number.endswith(suf) else 1, e.page))
                r.targets = [{"page": e.page, "number": e.number, "title": e.title,
                              "file": e.file if e.file and not _same(e.file, path) else None} for e in ts][:40]
                r.note = f"{len(r.targets)} {r.subject} sheet(s) in this set" if r.targets else \
                    f"no {r.subject} sheets in this upload"
            else:
                try:
                    _resolve(r, path, idx, own.number, with_views)
                except Exception as e:  # noqa: BLE001 — one odd target never sinks the page's other refs
                    r.note = f"could not resolve ({type(e).__name__})"
        try:
            refs += _tag_refs(path, int(page), lines, own, idx)
        except Exception:  # noqa: BLE001 — tags are a bonus
            pass
        if links and len(idx) > 1:
            try:
                refs += _link_refs(path, int(page), lines, own, idx)
            except Exception:  # noqa: BLE001 — copies / floors are a bonus too
                pass
    with _CACHE_LOCK:
        _CACHE[key] = refs
    return refs


def _table_name(table: str | None, e: SheetEntry) -> str:
    """'CUT AND COVER DOOR SCHEDULE · ' — empty when the table is named like its sheet (A-602 'DOOR SCHEDULE')."""
    t = (table or "").strip()
    return "" if not t or t.upper() == (e.title or "").strip().upper() else f"{t} · "


def _tag_refs(path: str, page: int, lines: list[_Line], own: SheetEntry, idx: list[SheetEntry]) -> list[Ref]:
    """Door / window / room tags on a plan -> their row in the set's schedule ('101A' -> CUT AND COVER DOOR SCHEDULE row
    101A on A-601). Only keys a schedule of the set lists; door / window rows win over room rows for the same key."""
    if own.title and _SCHEDULE_TITLE.search(own.title):
        return []
    rows: dict[str, tuple[dict, SheetEntry]] = {}
    for e in idx:
        if not (e.title and _SCHEDULE_TITLE.search(e.title)) or (_same(e.file or path, path) and e.page == page):
            continue
        for r in schedule_rows(e.file or path, e.page):
            if r["kind"] == "other":
                continue
            k = r["key"].upper()
            if k not in rows or (rows[k][0]["kind"] == "room" and r["kind"] != "room"):
                rows[k] = (r, e)
    doors: dict[str, list[tuple[dict, SheetEntry]]] = {}  # room number -> its doors' rows ('7.04' -> 7.04A, 7.04B)
    for k, (r, e) in rows.items():
        rm = _room_of(k)
        if r["kind"] == "door" and rm:
            doors.setdefault(rm, []).append((r, e))
    out = []
    for l in lines:
        k = l.text.strip().upper()
        if own.number_box and tuple(l.src) == tuple(own.number_box):
            continue
        if k not in rows and k in doors:
            ds = [(r, e) for r, e in doors[k] if (e.file, e.page) == (doors[k][0][1].file, doors[k][0][1].page)]
            e = ds[0][1]
            keys = [r["key"] for r, _ in ds]
            bb = [r["bbox"] for r, _ in ds]
            out.append(Ref(page=page, kind="tag", text=l.text, bbox=_box(l.src), sheet=e.number, detail=k,
                           subject="door", target_page=e.page,
                           target_file=e.file if e.file and not _same(e.file, path) else None, target_sheet=e.number,
                           target_title=e.title,
                           target_view=f"{_table_name(ds[0][0]['table'], e)}door{'s' if len(ds) > 1 else ''} "
                                       f"{', '.join(keys[:6])}{' …' if len(keys) > 6 else ''} (room {l.text.strip()})",
                           target_bbox=[min(b[0] for b in bb), min(b[1] for b in bb), max(b[2] for b in bb),
                                        max(b[3] for b in bb)]))
            continue
        if k not in rows:
            continue
        r, e = rows[k]
        out.append(Ref(page=page, kind="tag", text=l.text, bbox=_box(l.src), sheet=e.number, detail=k,
                       subject=r["kind"], target_page=e.page,
                       target_file=e.file if e.file and not _same(e.file, path) else None, target_sheet=e.number,
                       target_title=e.title, target_view=f"{_table_name(r['table'], e)}{r['kind']} {k}",
                       target_bbox=r["bbox"]))
        if len(out) >= 300:
            break
    return out


# ---------------------------------------------------------------------------------------------------- schedule check
def _room_of(key: str) -> str | None:
    """'7.04B' -> '7.04', '101A' -> '101': the room a door key is numbered after (None when it is not that pattern)."""
    m = re.match(r"^(.+?\d)([A-Z])$", key.strip().upper())
    return m.group(1) if m and _ROOM_NO.match(m.group(1)) else None


def reconcile(path: str, set_paths: list[str] | None = None, max_pages: int = 400) -> dict:
    """The estimator's check (captain 21:13): every schedule table of the set (door / window / room finish / pipe support
    …) against the tags printed on the set's other sheets, both ways. -> {tables: [{sheet, title, page, file, table, kind,
    rows, tagged, untagged: [{key, bbox}], links: {key: [{page, sheet, file, bbox}]}}], sheets_read}. A row whose key is
    printed on no other sheet of the upload is listed (a door the plans do not show, or a plan not uploaded) — never a
    verdict. Text only, cached per set."""
    key = ("recon", _set_key(set_paths) or _file_key(path))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    idx = _index_for(path, set_paths)[:max_pages]
    rows: dict[str, list[tuple[dict, SheetEntry]]] = {}
    tables: dict[tuple, dict] = {}
    for e in idx:
        if not (e.title and _SCHEDULE_TITLE.search(e.title)):
            continue
        for r in schedule_rows(e.file or path, e.page):
            tk = (e.file or path, e.page, r["table"] or e.title)
            t = tables.setdefault(tk, {"sheet": e.number, "title": e.title, "page": e.page, "file": e.file,
                                       "table": r["table"] or e.title, "kind": r["kind"], "keys": [], "boxes": {},
                                       "openings": []})
            if r["key"] not in t["boxes"]:
                t["keys"].append(r["key"])
                t["boxes"][r["key"]] = r["bbox"]
                if r.get("opening_only"):
                    t["openings"].append(r["key"])        # a cased opening: no door leaf (no tag needed on a plan)
            rows.setdefault(r["key"].upper(), []).append((r, e))
    links: dict[str, list[dict]] = {}
    rooms: dict[str, list[dict]] = {}       # room number printed on a plan -> where (its doors' rows count as shown)
    by_room = {rm for k, v in rows.items() if any(r["kind"] == "door" for r, _ in v) for rm in [_room_of(k)] if rm}
    read = 0
    # a short key ('C1', 'A1', '10') is also a grid / detail / note id: count it only on sheets of the schedule's own
    # discipline (conduit C1 on E-sheets); general sheets (G-001 code / abbreviation sheets) never count
    disc = {k: {(e.number or "?")[:1] for _, e in v} for k, v in rows.items()}
    short = {k for k in rows if len(k) <= 3 and "-" not in k and any(r["kind"] == "other" for r, _ in rows[k])}
    for e in idx:
        if e.title and _SCHEDULE_TITLE.search(e.title) or not rows or re.match(r"^G-?\d", e.number or ""):
            continue
        f = e.file or path
        try:
            got = _lines_of(f, e.page)
        except Exception:  # noqa: BLE001
            got = None
        if not got or not got[0]:
            continue
        read += 1
        for l in got[0]:
            k = l.text.strip().upper()
            if k in short and (e.number or "?")[:1] not in disc[k]:
                continue
            if e.number_box and tuple(l.src) == tuple(e.number_box):
                continue
            if k in rows:
                links.setdefault(k, []).append({"page": e.page, "sheet": e.number, "file": e.file, "bbox": _box(l.src)})
            if k in by_room:                  # also a room-schedule key ('1.00'): its doors count as shown too
                rooms.setdefault(k, []).append({"page": e.page, "sheet": e.number, "file": e.file, "bbox": _box(l.src),
                                                "via": f"room {l.text.strip()}"})
    out = []
    for t in tables.values():
        lk = {k: links.get(k.upper(), [])[:20] or rooms.get(_room_of(k) or "", [])[:20] for k in t["keys"]}
        untagged = [{"key": k, "bbox": t["boxes"][k], **({"opening_only": True} if k in t["openings"] else {})}
                    for k in t["keys"] if not lk[k]]
        out.append({k2: v for k2, v in t.items() if k2 not in ("keys", "boxes")} |
                   {"rows": len(t["keys"]), "tagged": len(t["keys"]) - len(untagged), "untagged": untagged,
                    "links": {k: v for k, v in lk.items() if v}, "row_boxes": t["boxes"]})
    res = {"tables": out, "sheets_read": read}
    with _CACHE_LOCK:
        _CACHE[key] = res
    return res


# ---------------------------------------------------------------------------------------------------- copies and floors
# Two kinds of link a building's sheets carry without printing a reference:
#   copy  — the same drawing twice in one upload (the vector PDF and a scan / photo of it; two revisions): the same
#           ISO number (a scan's OCR may keep only its start), or the same title and level. A registration fit (printed
#           labels matched by fuzzy text, RANSAC similarity) maps any point of one copy onto the other.
#   level — a stair / lift / riser / shaft / void on this floor plan -> the same element on the plan of the floor above /
#           below (registered the same way: CAD sheets of one building share the paper frame). A void -> the room under it.
_VERTICAL = re.compile(r"\b(stair(?:s|case|well)?|lifts?|elevators?|escalators?|risers?|shafts?|voids?|atrium|open\s+to\s+below|"
                       r"dumb\s*waiter|hoist)\b", re.I)


def _vkind(text: str) -> str | None:
    m = _VERTICAL.search(text or "")
    if not m:
        return None
    w = m.group(1).lower()
    return ("stair" if w.startswith("stair") else "lift" if w.startswith(("lift", "elevator")) else
            "void" if w.startswith(("void", "atrium", "open")) else re.sub(r"s$", "", w))


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def _ctr(b) -> complex:
    return complex((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _labels(lines: list[_Line]) -> list[tuple[str, tuple]]:
    """Drawing labels with their stacked second lines joined ('Multi-Purpose' over 'Hall', 'Stair' over '(Upper)'),
    as (text, evidence box). Horizontal lines only."""
    hs = sorted([l for l in lines if l.dir == (1, 0) and l.text.strip()], key=lambda l: (l.src[1], l.src[0]))
    used, out = set(), []
    for i, a in enumerate(hs):
        if i in used:
            continue
        text, box, last = a.text, tuple(a.src), a
        for j in range(i + 1, min(i + 12, len(hs))):
            b = hs[j]
            h = max(last.src[3] - last.src[1], 1.0)
            if j in used or abs(b.size - a.size) > 0.25 * a.size:
                continue
            if -0.3 * h <= b.src[1] - last.src[3] <= 0.6 * h and abs(_ctr(b.src).real - _ctr(last.src).real) <= 2.5 * h:
                text, box, last = f"{text} {b.text}", tuple(_union(box, b.src)), b
                used.add(j)
        out.append((text, box))
    return out


def _sim(a: str, b: str) -> float:
    import difflib

    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def register(la: list[_Line], wa: float, ha: float, lb: list[_Line], wb: float, hb: float) -> dict | None:
    """A similarity transform (scale, rotation, shift) mapping page A's evidence space onto page B's, from printed
    labels both pages carry (fuzzy text: a scan's OCR 'Classro:m' = 'Classroom'). RANSAC over label pairs, then a least-
    squares refit on the inliers. -> {a, b, tx, ty, inliers, of, rms} with x' = a x - b y + tx, y' = b x + a y + ty;
    None when fewer than 6 labels agree (different drawings, or nothing readable)."""
    import random

    A = [(_norm(l.text), _ctr(l.src)) for l in la if len(_norm(l.text)) >= 2]
    B = [(_norm(l.text), _ctr(l.src)) for l in lb if len(_norm(l.text)) >= 2]
    if len(A) < 6 or len(B) < 6:
        return None
    cand: list[tuple[int, complex, complex]] = []
    for i, (ta, ca) in enumerate(A):
        row = []
        for tb, cb in B:
            s = _sim(ta, tb) if (ta == tb or (len(ta) >= 4 and abs(len(ta) - len(tb)) <= max(3, len(ta) // 3))) else 0.0
            if s >= 0.72:
                row.append((s, cb))
        row.sort(key=lambda x: -x[0])
        cand += [(i, ca, cb) for _, cb in row[:8]]
    if len(cand) < 6:
        return None
    scale0 = ((wb * wb + hb * hb) / max(wa * wa + ha * ha, 1e-6)) ** 0.5   # full-sheet renders: about the size ratio
    tol = 0.012 * (wb * wb + hb * hb) ** 0.5
    rnd = random.Random(7)
    best: tuple[int, complex, complex] | None = None

    def inliers(s: complex, t: complex) -> dict:
        got: dict[int, tuple[complex, complex, float]] = {}
        for i, ca, cb in cand:
            d = abs(s * ca + t - cb)
            if d <= tol and (i not in got or d < got[i][2]):
                got[i] = (ca, cb, d)
        return got

    for _ in range(600):
        (i1, a1, b1), (i2, a2, b2) = rnd.sample(cand, 2)
        if i1 == i2 or abs(a1 - a2) < 0.05 * (wa * wa + ha * ha) ** 0.5 or abs(b1 - b2) < 1e-6:
            continue
        s = (b1 - b2) / (a1 - a2)
        if not (0.7 * scale0 <= abs(s) <= 1.4 * scale0) or abs(s.imag) > 0.1 * abs(s):  # a scan is skewed a few degrees
            continue
        t = b1 - s * a1
        n = len(inliers(s, t))
        if best is None or n > best[0]:
            best = (n, s, t)
    if best is None or best[0] < 6:
        return None
    n, s, t = best
    for _ in range(2):  # least-squares refit on the inliers (complex linear regression)
        got = list(inliers(s, t).values())
        if len(got) < 6:
            break
        za = [g[0] for g in got]
        zb = [g[1] for g in got]
        ma, mb = sum(za) / len(za), sum(zb) / len(zb)
        den = sum(abs(z - ma) ** 2 for z in za)
        if den <= 0:
            break
        s = sum((zb_ - mb) * (za_ - ma).conjugate() for za_, zb_ in zip(za, zb)) / den
        t = mb - s * ma
    got = list(inliers(s, t).values())
    if len(got) < 6:
        return None
    rms = (sum(abs(s * g[0] + t - g[1]) ** 2 for g in got) / len(got)) ** 0.5
    # matches inside the drawing body (not the title block / border band): a copy of the same drawing matches its room
    # labels there; two sheets of one set match only their shared title block (HAER C3 / C6: 36 matches, 3 inside)
    inner = sum(1 for g in got if 0.05 * wa < g[0].real < 0.80 * wa and 0.05 * ha < g[0].imag < 0.85 * ha)
    return {"a": round(s.real, 6), "b": round(s.imag, 6), "tx": round(t.real, 2), "ty": round(t.imag, 2),
            "inliers": len(got), "of": len({i for i, _, _ in cand}), "rms": round(rms, 2), "inner": inner}


def map_box(tr: dict, box) -> list:
    """An evidence box of page A -> page B through register()'s transform (the box of the four mapped corners)."""
    s, t = complex(tr["a"], tr["b"]), complex(tr["tx"], tr["ty"])
    zs = [s * complex(x, y) + t for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
    return [round(min(z.real for z in zs), 1), round(min(z.imag for z in zs), 1),
            round(max(z.real for z in zs), 1), round(max(z.imag for z in zs), 1)]


def _register_cached(pa: str, ga: int, pb: str, gb: int) -> dict | None:
    key = ("reg", *_file_key(pa), int(ga), *_file_key(pb), int(gb))
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    ra, rb = _lines_of(pa, ga), _lines_of(pb, gb)
    tr = register(ra[0], ra[1], ra[2], rb[0], rb[1], rb[2]) if ra and rb and ra[0] and rb[0] else None
    if tr is None and ra and rb and ra[0] and rb[0] and abs(ra[1] - rb[1]) <= 0.01 * ra[1] and abs(ra[2] - rb[2]) <= 0.01 * ra[2]:
        tr = {"a": 1.0, "b": 0.0, "tx": 0.0, "ty": 0.0, "inliers": 0, "of": 0, "rms": None, "frame": True}
    if ra and rb and ra[0] and rb[0]:          # a side whose text is not read yet: ask again later
        with _CACHE_LOCK:
            _CACHE[key] = tr
    return tr


@functools.lru_cache(maxsize=512)
def _scan_pdf_key(path: str, _size: int, _mtime: int) -> bool:
    import pymupdf as fitz

    with fitz.open(path) as doc:
        return doc.page_count == 1 and len(_page_lines(doc[0])) < 5


def is_scan(path: str | None) -> bool:
    """A picture, or a one-page PDF with no text of its own (a scanned print)."""
    if not path:
        return False
    if _is_image(path):
        return True
    try:
        return _is_pdf(path) and _scan_pdf_key(*_file_key(path))
    except Exception:  # noqa: BLE001
        return False


def _fmt(path: str | None) -> str:
    return "picture" if _is_image(path or "") else "scanned PDF" if is_scan(path) else "PDF"


def _copy_of(a: SheetEntry, b: SheetEntry) -> str | None:
    """Why b is a copy of the same drawing as a (None = it is not)."""
    ka, kb = _key(a.number).rstrip("…"), _key(b.number).rstrip("…")
    if a.level is not None and b.level is not None and a.level != b.level:
        return None
    pa, pb = (a.number or "").endswith("…"), (b.number or "").endswith("…")
    if ka and kb:
        if not pa and not pb:
            return "same drawing number" if ka == kb else None
        short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
        if len(short) >= 8 and long_.startswith(short):
            return "same drawing number (as far as the scan's print could be read)"
        return None
    ta, tb = _norm(a.title), _norm(b.title)
    if len(ta) >= 8 and len(tb) >= 8 and _sim(ta, tb) >= 0.85 and a.level is not None and a.level == b.level:
        return "same title and level"
    return None


def _registers_as_copy(tr: dict | None) -> bool:
    """Two copies of one drawing share most of their printed labels at one similarity transform. Two DIFFERENT sheets of
    one set share only the title block (HAER scans C3 / C6 both print 'HAER No. HI-76', read as their number): a few
    matches, a small share. The same-paper-frame fallback alone is never enough (every sheet of a set has one frame)."""
    if not tr or tr.get("frame"):
        return False
    return int(tr.get("inliers") or 0) >= 8 and int(tr.get("inner") or 0) >= 6


def _level_order(k: str | None) -> float | None:
    if k is None:
        return None
    if k == "roof":
        return 99.0
    try:
        return float(k)
    except ValueError:
        return None


def _same_project(a: SheetEntry, b: SheetEntry) -> bool:
    fa, fb = (a.number or "").split("-"), (b.number or "").split("-")
    if len(fa) >= 4 and len(fb) >= 4:
        return fa[:2] == fb[:2]
    return True   # no ISO numbers: the registration (shared frame / labels) decides


def _link_refs(path: str, page: int, lines: list[_Line], own: SheetEntry, idx: list[SheetEntry]) -> list[Ref]:
    out: list[Ref] = []
    me = next((e for e in idx if _same(e.file or path, path) and e.page == page), None)
    me = me or own
    if me.level is None and own.level is not None:
        me = own
    others = [e for e in idx if not (_same(e.file or path, path) and e.page == page)]
    anchor = list(me.number_box or me.title_box or own.number_box or own.title_box or ()) or None
    # copies of this drawing
    for e in others:
        why = _copy_of(me, e)
        if not why:
            continue
        tf = e.file or path
        tr = _register_cached(path, page, tf, e.page)
        if not _registers_as_copy(tr):
            continue                          # same number, different drawing (a project id read as the number)
        fit = f"; registered on {tr['inliers']} matching labels (rms {tr['rms']})"
        r = Ref(page=page, kind="copy", text=me.number or me.title or "this drawing", bbox=_box(anchor or (0, 0, 40, 20)),
                sheet=e.number, subject=_fmt(tf), target_page=e.page,
                target_file=e.file if e.file and not _same(e.file, path) else None, target_sheet=e.number,
                target_title=e.title, target_view=f"{_fmt(tf)} copy" + (f" · {e.level_name}" if e.level_name else ""),
                target_bbox=None, note=why + fit)
        r.transform = tr
        out.append(r)
    # other floors of this building: vertical elements line up
    lo = _level_order(me.level)
    if lo is None:
        return out
    floors: dict[float, SheetEntry] = {}
    for e in others:
        o = _level_order(e.level)
        if o is None or o == lo or not _same_project(me, e):
            continue
        prev = floors.get(o)
        if prev is None or (_is_image(prev.file or path) and not _is_image(e.file or path)):  # the vector copy first
            floors[o] = e
    if not floors:
        return out
    above = min((o for o in floors if o > lo), default=None)
    below = max((o for o in floors if o < lo), default=None)
    mine = [(t, b, _vkind(t)) for t, b in _labels(lines)]
    mine = [(t, b, k) for t, b, k in mine if k and len(t) <= 40]
    for o in (below, above):
        if o is None or not mine:
            continue
        e = floors[o]
        tf = e.file or path
        tr = _register_cached(path, page, tf, e.page)
        got = _lines_of(tf, e.page)
        if not tr or not got:
            continue
        theirs = [(t, b, _vkind(t)) for t, b in _labels(got[0])]
        diag = (got[1] ** 2 + got[2] ** 2) ** 0.5
        R = 0.05 * diag
        picks: list = []
        for t, b, k in mine:
            if k == "void" and o > lo:
                continue                                   # a void looks down, never up
            if o < lo and re.search(r"\broof\b", t, re.I):
                continue                                   # 'Stair (Roof)' climbs from this floor to the roof
            c = _ctr(map_box(tr, b))
            if k == "void":                                # the room under the void: nearest room label below
                pool = [(tt, bb) for tt, bb, kk in theirs if kk is None and len(re.findall(r"[A-Za-z]", tt)) >= 4
                        and not re.match(r"\s*open\s+to\b|\s*(?:below|above)\b", tt, re.I)]
                score = [(abs(_ctr(bb) - c), tt, bb) for tt, bb in pool]
            else:                                          # the same kind of element; the same label wins a tie
                score = [(abs(_ctr(bb) - c) + (0 if _norm(tt.split("(")[0]) == _norm(t.split("(")[0]) else 0.3 * R), tt, bb)
                         for tt, bb, kk in theirs if kk == k]
            score = sorted([x for x in score if x[0] <= R], key=lambda x: x[0])
            if not score:
                continue
            picks.append((t, b, k, c, score))
        # a void between two rooms' labels is ambiguous; voids printed on both sides of one double-height room share it:
        # an ambiguous void (2nd label within 35 % of the 1st) takes the room a neighbouring void picks clearly
        clear = [(c, sc[0][1]) for t, b, k, c, sc in picks if k == "void" and (len(sc) == 1 or sc[1][0] > 1.35 * sc[0][0])]
        for t, b, k, c, score in picks:
            d, tt, bb = score[0]
            amb = ""
            if k == "void" and len(score) > 1 and score[1][0] <= 1.35 * score[0][0]:
                near = {name for cc, name in clear if 0 < abs(cc - c) <= 2 * R}
                alt = next((x for x in score[:3] if x[1] in near), None)
                if alt:
                    d, tt, bb = alt
                amb = f"; nearest labels: {', '.join(x[1] for x in score[:2])}" + (f" — {tt} as the void next to it" if alt else "")
            m = 0.04 * diag                                # frame the element, not just its label
            tb = [round(bb[0] - m, 1), round(bb[1] - m, 1), round(bb[2] + m, 1), round(bb[3] + m, 1)]
            where = "above" if o > lo else "below"
            r = Ref(page=page, kind="level", text=t, bbox=_box(b), sheet=e.number, detail=k,
                    subject=f"{'room under the void' if k == 'void' else k} · {where}", target_page=e.page,
                    target_file=e.file if e.file and not _same(e.file, path) else None, target_sheet=e.number,
                    target_title=e.title, target_view=f"{tt} · {e.level_name or e.title or ''}".strip(" ·"),
                    target_bbox=tb,
                    note=(f"{'under this void' if k == 'void' else 'the same ' + k} on {e.level_name or 'the floor ' + where}"
                          f" ({tt}; {round(d / max(diag, 1) * 100, 1)} % of the sheet from where it lines up{amb})"))
            r.transform = tr
            out.append(r)
    return out


def related_sheets(path: str, page: int = 0, set_paths: list[str] | None = None) -> list[dict]:
    """The other copies of this drawing and the plans of the floors above / below in the set, each with the
    registration that maps this page's evidence space onto it: [{kind copy | floor, file, page, number, title, level,
    level_name, why, transform}]. Used by the web for 'the same place on the other copy / floor'."""
    got = _lines_of(path, page)
    if not got or not got[0]:
        return []
    lines, W, H = got
    idx = _index_for(path, set_paths)
    own = _sheet_from(lines, W, H, int(page))
    me = next((e for e in idx if _same(e.file or path, path) and e.page == page), None) or own
    out = []
    lo = _level_order(me.level)
    for e in idx:
        if _same(e.file or path, path) and e.page == page:
            continue
        why = _copy_of(me, e)
        tf = e.file or path
        if why and not _registers_as_copy(_register_cached(path, page, tf, e.page)):
            why = None                        # same number, different drawing (HAER 'HI-76' on every sheet)
        o = _level_order(e.level)
        floor = not why and lo is not None and o is not None and o != lo and _same_project(me, e)
        if not why and not floor:
            continue
        out.append({"kind": "copy" if why else "floor", "file": e.file if e.file and not _same(e.file, path) else None,
                    "page": e.page, "number": e.number, "title": e.title, "level": e.level, "level_name": e.level_name,
                    "format": _fmt(tf), "why": why or f"{e.level_name or 'another floor'} of the same building",
                    "transform": _register_cached(path, page, tf, e.page)})
    return out


def set_refs(path: str, with_views: bool = False) -> dict:
    """The set's reference graph: per page its outgoing refs (resolved, no view boxes unless asked) and the pages
    that point at it (incoming)."""
    idx = sheet_index(path)
    out, incoming = {}, {}
    for e in idx:
        rs = page_refs(path, e.page, resolve=True, with_views=with_views, links=False)
        out[e.page] = rs
        for r in rs:
            if r.target_page is not None and r.target_page != e.page:
                incoming.setdefault(r.target_page, set()).add(e.page)
    return {"sheets": idx, "refs": out, "incoming": {k: sorted(v) for k, v in incoming.items()}}


# ---------------------------------------------------------------------------------------------------- follow a hop
_STOP = set("the a an of on in at to for and or is are what which how many much does do this that with from by be "
            "sheet sheets drawing plan detail section see per".split())
_SCHEDULE_Q = re.compile(r"\b(?:schedule|hardware|fire[- ]?rat(?:ing|ed)|frame|finish(?:es)?|door\s+type|window\s+type|"
                         r"glazing|lintel|threshold|leaf|leaves|width|height|size|material|keying|closer|head|jamb|sill)\b", re.I)
_PLAN_Q = re.compile(r"\b(?:areas?|how\s+many|number\s+of|count|square|sq\.?\s?(?:m|ft)|m²|perimeter|total\s+length|"
                     r"linear|skirting|baseboards?)\b", re.I)
_FINISH_Q = re.compile(r"\bceiling\s+height|\b(?:floor|wall|base|ceiling)\s+finish|\bfinish\s+schedule", re.I)
_OPENING_Q = re.compile(r"\b(?:doors?|windows?|openings?|rooms?)\b", re.I)
# F8 c7: 'How tall is door 7.04A?' / 'How wide is window W2?': plain size words are a schedule attribute only when the
# question names the door / window BY ITS NUMBER (not 'how wide is the door into the kitchen', which is measured on the plan)
_KEYED_OPENING_Q = re.compile(r"\b(?:doors?|windows?)\s+(?:no\.?\s*|number\s*|#\s*)?"
                              r"(?:\d{1,3}[A-Z]?\.\d{1,3}[A-Z]?|[A-Z]{0,2}-?\d{1,4}[A-Z]?)\b", re.I)
_KEYED_SIZE_Q = re.compile(r"\b(?:wide|tall|high|thick(?:ness)?|dimensions?|rough\s+opening|how\s+big)\b", re.I)
# F8 c7: an AREA question about rooms ('What is the size of the largest room?', 'What size are the rooms on this floor?',
# 'total floor area of the rooms with a VCT floor finish') is measured on the PLAN: it never hops to a room / finish
# schedule (AW-28 sent all three to A-601) unless it says 'schedule'. Door / window sizes are schedule attributes.
_AREA_Q = re.compile(r"\b(?:areas?|square|sq\.?\s?(?:m|ft)|m²|m2|how\s+big|largest|smallest|biggest)\b", re.I)
_ROOM_SIZE_Q = re.compile(r"\bsizes?\b", re.I)
_DOOR_WORD_Q = re.compile(r"\b(?:doors?|windows?|openings?)\b", re.I)
# F8 c10: the size / height word belongs to a ROOM that a numbered door only locates ('What size is the room next to door
# 104S?', 'ceiling height of the room behind door 104S'): AW-28 read door 104S's printed 3'-0" x 7'-0" / 7'-0" and put it
# over the reader's answer at 0.8. Such questions stay on the plan. 'the mech room door 104S' / 'room's door' stay doors.
_ROOM_SUBJECT_Q = re.compile(r"\b(?:sizes?|areas?|dimensions?|big|large|tall|high|wide|height|width|long|length)\b[^?]*?"
                             r"\b(?:room|space)s?\b(?!(?:'s)?\s+(?:doors?|windows?)\b)[^?]*?\b(?:doors?|windows?)\s+"
                             r"(?:no\.?\s*|number\s*|#\s*)?(?:\d{1,3}[A-Z]?\.\d{1,3}[A-Z]?|[A-Z]{0,2}-?\d{1,4}[A-Z]?)\b", re.I)
# F8 c11: whose size is it? A numbered door often only LOCATES the thing measured ('How wide is the corridor outside
# door 104S?', 'How thick is the wall at door 104S?', 'height of the window next to door 7.04A'): AW-28 / SFO hopped 19
# of 23 such phrasings and put the door's printed 3'-0" / 7'-0" / 1 3/4" over the reader's answer at 0.8. A PART of the
# door ('the louver in door 104S', 'glass size', 'the frame of door 104S') is still read on its schedule row, but not
# from the door's own WIDTH / HEIGHT column.
_SUBJ_SIZE = re.compile(r"\b(?:how\s+big|rough\s+opening|sizes?|widths?|heights?|thickness|dimensions?|wide|tall|high|"
                        r"thick|big|lengths?|long)\b", re.I)
_SUBJ_FUNC = frozenset("a an the of is are was were be its it this that these those what whats s for does do did has "
                       "have how which me us give tell show find please and or in on per any".split())
_SUBJ_OK = frozenset("door doors opening openings leaf leaves clear overall nominal actual total rough schedule "
                     "scheduled listed printed shown specified given according exact exactly approximate approximately "
                     "roughly mm cm m ft feet foot inch inches meters metres millimeters millimetres size sizes width "
                     "widths height heights thickness dimension dimensions wide tall high thick big".split())
_SUBJ_LOC = frozenset("next behind beside besides outside inside near nearest nearby by at adjacent opposite across "
                      "beyond past from between along through above below under over around off to into after before "
                      "leading serving with within facing".split())
_SUBJ_SPACE = frozenset("room rooms space spaces corridor corridors hall halls hallway hallways lobby stair stairs "
                        "stairway stairwell office offices toilet toilets restroom restrooms closet closets kitchen "
                        "bathroom bedroom vestibule wall walls ceiling ceilings floor floors landing area building storey "
                        "story level exit passage ramp shaft garage".split())


def _size_subject(q: str) -> str:
    """'door' | 'located' (the door only locates what is measured) | 'part' (a part of the numbered door)."""
    md, ms = _KEYED_OPENING_Q.search(q), _SUBJ_SIZE.search(q)
    if not md or not ms:
        return "door"
    words = lambda s: re.findall(r"[a-z]+", s.lower())  # noqa: E731
    if ms.start() >= md.end():                       # 'door 104S: what are its dimensions?'
        seg = words(q[md.end():ms.start()])
        m = re.match(r"\s*(?:is|are)\s+(?:the|a|an|this|that)\s+([a-z]+)", q[ms.end():], re.I)
        tail = [m.group(1).lower()] if m else []
    else:                                            # 'How wide is the corridor outside door 104S?'
        seg = words(re.sub(r"'s\b", "", q[ms.end():md.start()]))   # 'the storage room's door 104B'
        while seg and seg[-1] not in _SUBJ_FUNC and seg[-1] not in _SUBJ_LOC:
            seg.pop()                                # 'the mech room door 104S': a modifier of the door itself
        before = words(q[:ms.start()])[-1:]          # 'glass size in door 104S', 'the frame width of door 104S'
        tail = before
    m = re.match(r"\s*'s\s+([a-z]+)", q[md.end():], re.I)   # "door 104S's frame"
    tail = tail + ([m.group(1).lower()] if m else [])
    content = [w for w in seg + tail if w not in _SUBJ_FUNC and w not in _SUBJ_OK]
    if any(w in _SUBJ_LOC for w in seg) or any(w in _SUBJ_SPACE for w in content):
        return "located"
    return "part" if content else "door"


_SHEET_Q = re.compile(rf"\b(?:(?:on|in|from|see|sheet|drawing|dwg\.?)\s+)({_NUM})\b|\b({_NUM})\b")
_DETAIL_Q = re.compile(rf"\b(?:detail|section|view|callout)\s+([A-Z]{{0,2}}\d{{1,3}}[A-Z]?|[A-Z]{{1,2}})(?:\s*(?:/|on|of)\s*({_NUM}))?\b",
                       re.I)


def _sing(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]{3,}", (s or "").lower()) if w not in _STOP}


@dataclass
class Hop:
    from_page: int
    from_sheet: str | None
    to_page: int
    to_sheet: str | None
    to_title: str | None
    why: str                          # one line for the trace
    ref: dict | None = None           # the printed reference that was followed (bbox on from_page)
    to_bbox: list | None = None       # the target view's extent (unrotated pdf_pt on to_page)
    question: str = ""                # the question to ask on to_page (a '(view: TITLE)' hint when a view is known)
    to_file: str | None = None        # set indexes: the other PDF to_page is in (None = this file)
    mode: str = "auto"                # auto = the question itself names the target (sheet / detail / schedule attribute);
    #                                   suggest = matched by subject words only: offer it as a choice, do not re-route
    to_row: dict | None = None        # the schedule row the question names: {key, table, cells: [{text, bbox}]} (F8 c7:
    #                                   the web marks the answer's value in THIS row, not the first match on the sheet)

    def as_dict(self) -> dict:
        return asdict(self)


_COLUMN_WORDS = {"width": r"WIDTH|WD", "height": r"HEIGHT|HGT|HT", "thickness": r"THICKNESS|THICK|THK"}


def _row_cells(path: str, page: int, box, column: str | None = None) -> tuple[list[dict], dict | None]:
    """The printed cells (text lines) inside one schedule row's box, in the row box's space (unrotated pdf_pt; image px):
    [{text, bbox}] (at most 40), and the cell under the asked COLUMN's header ('height' -> the 'HEIGHT' heading above the
    row in the displayed page; a heading line 'WIDTH HEIGHT THICKNESS' is split by character position) as
    {column, text, bbox, header} or None. Empty / None when the page cannot be read or no heading is found."""
    try:
        got = _lines_of(path, page)
    except Exception:  # noqa: BLE001
        got = None
    if not got or not box:
        return [], None
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    inside = []
    for ln in got[0]:
        a, b, c, d = ln.src
        cx, cy = (a + c) / 2, (b + d) / 2
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            inside.append(ln)
    cells = [{"text": ln.text, "bbox": [round(v, 2) for v in ln.src]} for ln in inside][:40]
    rowd = [ln for ln in inside if ln.dir == (1, 0)]

    def under(col: str) -> dict | None:
        word = _COLUMN_WORDS.get(col)
        if not word or not rowd:
            return None
        rb = (min(ln.box[0] for ln in rowd), min(ln.box[1] for ln in rowd), max(ln.box[2] for ln in rowd),
              max(ln.box[3] for ln in rowd))
        best = None
        for ln in got[0]:
            # a heading above the row: its centre above the row's top (a tight first row overlaps the heading's
            # box by a fraction of a point: F8 c8)
            if ln.dir != (1, 0) or (ln.box[1] + ln.box[3]) / 2 > rb[1] or ln.box[2] < rb[0] or ln.box[0] > rb[2]:
                continue
            for m in re.finditer(rf"\b(?:{word})\.?(?![A-Z])", ln.text.upper()):
                n = max(1, len(ln.text))
                wx = ln.box[0] + (ln.box[2] - ln.box[0]) * (m.start() + m.end()) / 2 / n   # the word's centre
                if not rb[0] - 2 <= wx <= rb[2] + 2:
                    continue
                if best is None or ln.box[3] > best[0].box[3]:     # the nearest heading above the row
                    best = (ln, wx)
        if best is None:
            return None
        ln, wx = best
        hit = [c for c in rowd if c.box[0] - 3 <= wx <= c.box[2] + 3]
        if len(hit) == 1 and re.search(r"\d", hit[0].text):
            return {"column": col, "text": hit[0].text, "bbox": [round(v, 2) for v in hit[0].src], "header": ln.text}
        return None

    if column == "size":
        # F8 c8: 'What size is door 7.04A?' / 'dimensions of door 104S' = WIDTH x HEIGHT of the named row (a door size is
        # printed as two cells; the reader answered one of them, or another row's). Both cells or nothing.
        w, h = under("width"), under("height")
        if not (w and h) or w["bbox"] == h["bbox"]:
            return cells, None
        hdr = w["header"] if w["header"] == h["header"] else f"{w['header'].strip()} / {h['header'].strip()}"
        return cells, {"column": "size", "text": f"{w['text']} x {h['text']}", "bbox": w["bbox"], "header": hdr,
                       "parts": [w, h]}
    return cells, under(column or "")


_ROOM_GENERIC = {"room", "rooms", "door", "doors", "window", "windows", "opening", "openings", "into", "from", "this",
                 "that", "what", "which", "width", "height", "size", "wide", "tall", "high", "thick", "thickness", "type",
                 "material", "frame", "fire", "rating", "rated", "hardware", "schedule", "detail", "head", "jamb", "sill",
                 "glazing", "finish", "leaf", "leaves", "how", "the", "and", "for", "with", "does", "use", "uses"}


def _door_of_room(rows: list[dict], q: str, room: dict | None = None) -> tuple[dict | None, str | None]:
    """'How tall is the door to the mech room?' on a sheet with a ROOM FINISH schedule and a DOOR schedule (AW-28
    A-601): the room row whose name shares a specific word with the question (104 'MECH. ROOM') -> the door rows keyed by
    that room number ('104', '104S', '104A'; '7.04' -> '7.04A'). Only when exactly ONE room and ONE door number fit
    (a pair of leaves listed twice is one door); else (None, None) and the reader picks."""
    if not re.search(r"\b(?:doors?|windows?)\b", q, re.I):
        return None, None
    kind = "window" if re.search(r"\bwindows?\b", q, re.I) and not re.search(r"\bdoors?\b", q, re.I) else "door"
    qw = {_sing(w) for w in _words(q)} - _ROOM_GENERIC
    scored = []
    for r in rows:
        if r.get("kind") != "room":
            continue
        name = re.sub(rf"^\s*{re.escape(r['key'])}\s*", "", r.get("text") or "")
        sc = 9 if room is not None and r is room else len(qw & {_sing(w) for w in _words(name)})
        if sc:
            scored.append((sc, r, name))
    if not scored:
        return None, None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None, None
    rk = scored[0][1]["key"]
    doors = [r for r in rows if r.get("kind") == kind and re.fullmatch(rf"{re.escape(rk)}[A-Z]{{0,2}}", r["key"], re.I)]
    keys = list(dict.fromkeys(r["key"].upper() for r in doors))
    if len(keys) != 1:
        return None, None
    toks = scored[0][2].split()
    end = next((i + 1 for i, w in enumerate(toks[:3]) if re.fullmatch(r"(?:ROOM|RM\.?)", w, re.I)), 1)
    nm = " ".join(toks[:end]) if toks and re.search(r"[A-Za-z]{2}", toks[0]) else ""
    return doors[0], f"the {kind} of room {rk}{(' ' + nm) if nm else ''} is {kind} {keys[0]} ({kind} numbers carry the room number)"


def follow_plan(path: str, page: int, question: str, set_paths: list[str] | None = None,
                _probe: bool = False) -> Hop | None:
    """Where a question asked on `page` should be answered, when the answer lives on another sheet of the set (this
    PDF's pages, or every PDF in `set_paths` when several files were dropped together).

    Order: (1) the question names a sheet number ('on A-601', 'detail A6/S-500F') -> that sheet (and view);
    (2) a door / window / room attribute question ('hardware of door 101', 'fire rating', 'what size is window W2')
    -> the set's schedule sheet (title says SCHEDULE; the one the page refers to first); (3) a callout whose target
    view's title, else a note whose 'FOR <subject>', matches the question's words ('how deep is the pipe trench?' ->
    PIPE TRENCH SECTION): only offered (mode 'suggest'). None = answer on this page (no hop)."""
    if not (_is_pdf(path) or _is_image(path)):
        return None
    idx = _index_for(path, set_paths)
    here = next((e for e in idx if _same(e.file, path) and e.page == int(page)), None)
    if len(idx) < 2 or here is None:
        return None
    q = question or ""
    sp = set_paths if _set_key(set_paths) else None
    where = here.number or f"page {here.page + 1}"

    def other(fl, pg) -> bool:
        return pg is not None and not (pg == here.page and (fl is None or _same(fl, path)))

    def hop(e_file, e_page, e_number, e_title, why, ref=None, box=None, qq=q, mode="auto") -> Hop:
        return Hop(from_page=here.page, from_sheet=here.number, to_page=e_page, to_sheet=e_number, to_title=e_title,
                   why=why, ref=ref, to_bbox=box, question=qq, mode=mode,
                   to_file=e_file if e_file and not _same(e_file, path) else None)

    def at(e: SheetEntry) -> str:
        f = f"{os.path.basename(e.file)} " if e.file and not _same(e.file, path) else ""
        return f"{f}page {e.page + 1}"

    # (1) an explicit sheet / detail in the question
    md = _DETAIL_Q.search(q)
    for m in _SHEET_Q.finditer(q.upper()):
        num = m.group(1) or m.group(2)
        e, note = lookup(num, idx, here.number)
        if e is None or not other(e.file, e.page):
            continue
        detail = md.group(1).upper() if md and (not md.group(2) or _key(md.group(2)) == _key(num)) else None
        title, box = _find_view(e.file or path, e.page, detail) if detail else (None, None)
        why = f"the question names sheet {num}" + (f" ({note})" if note else "") + f": answered on {at(e)} " \
              f"({e.label()})" + (f", view {detail} '{title}'" if title else "")
        return hop(e.file, e.page, e.number, e.title, why, None, box, f"{q} (view: {title})" if title else q)
    if md and not md.group(2):
        # 'section A6' asked on the plan: the callout on this page names the sheet
        for r in page_refs(path, here.page, set_paths=sp):
            if r.kind == "callout" and r.detail and _key(r.detail) == _key(md.group(1)) and other(r.target_file, r.target_page):
                why = f"callout {r.text} on {where} points to sheet {r.target_sheet}" + \
                      (f": view '{r.target_view}'" if r.target_view else "")
                return hop(r.target_file, r.target_page, r.target_sheet, r.target_title, why, r.as_dict(), r.target_bbox,
                           f"{q} (view: {r.target_view})" if r.target_view else q)
    refs = page_refs(path, here.page, with_views=False, set_paths=sp)
    # (2) schedule questions: door / window / room attributes live on the schedule sheet
    subj = _size_subject(q)
    room_subject = bool(_ROOM_SUBJECT_Q.search(q)) or subj == "located"
    keyed = bool(_KEYED_OPENING_Q.search(q) and _KEYED_SIZE_Q.search(q) and not _PLAN_Q.search(q)) and not room_subject
    # F8 c8: 'What type is window W1?' / 'type of door 104S': a type named for ONE numbered opening is a schedule cell
    keyed_type = bool(_KEYED_OPENING_Q.search(q) and re.search(r"\btypes?\b", q, re.I) and not _PLAN_Q.search(q))
    room_area = bool((_AREA_Q.search(q) or _ROOM_SIZE_Q.search(q) and not _DOOR_WORD_Q.search(q) or room_subject)
                     and not re.search(r"\bschedule", q, re.I) and not keyed)
    named_size = keyed or bool(_KEYED_OPENING_Q.search(q) and re.search(r"\b(?:size|width|height|thickness)\b", q, re.I)
                               and not _PLAN_Q.search(q) and not room_subject)
    if named_size and not room_area and not _probe and here.title and _SCHEDULE_TITLE.search(here.title):
        # F8 c9: the visitor is ON the schedule and names one opening ('What size is door 104S?' on AW A-601: the reader
        # said 7'-0" x 1 3/4" = HEIGHT x THICKNESS @0.85): plan it as if asked on another sheet of this file; when that
        # lands on THIS page with the named row, return it (to_page = this page: the browser asks here, no jump, and puts
        # the row's printed cell(s) over the reader's value)
        alt = next((e for e in idx if _same(e.file, path) and e.page != here.page
                    and not (e.title and _SCHEDULE_TITLE.search(e.title))), None)
        h = follow_plan(path, alt.page, question, set_paths, _probe=True) if alt is not None else None
        if h is not None and h.mode == "auto" and h.to_file is None and h.to_page == here.page and h.to_row:
            h.from_page, h.from_sheet = here.page, here.number
            h.why = h.why.replace(" — this sheet refers to it", "") + " (the sheet on screen)"
            return h
    if (_OPENING_Q.search(q) and _SCHEDULE_Q.search(q) or _FINISH_Q.search(q) or keyed or keyed_type) and not room_area and \
            not (here.title and _SCHEDULE_TITLE.search(here.title)):
        scheds = [e for e in idx if e.title and _SCHEDULE_TITLE.search(e.title) and other(e.file, e.page)]
        if scheds:
            want = {_sing(w) for w in _words(" ".join(_OPENING_Q.findall(q)))} or \
                ({"room", "finish"} if _FINISH_Q.search(q) else set())
            pointed = {(os.path.realpath(r.target_file or path), r.target_page) for r in refs if r.target_page is not None}
            scored = [(len(want & {_sing(w) for w in _words(e.title)}),
                       (os.path.realpath(e.file or path), e.page) in pointed, e) for e in scheds]
            scored = [x for x in scored if x[0] >= 1]   # a door question never lands on a LIGHT FIXTURE SCHEDULE
            if not scored:
                return None
            scored.sort(key=lambda x: (-x[0], not x[1], x[2].page))
            pick, ptd = scored[0][2], scored[0][1]
            why = f"a {'/'.join(sorted(want))} attribute is read from the schedule: sheet {pick.label()} " \
                  f"({at(pick)})" + (" — this sheet refers to it" if ptd else "")
            # the row itself when the question names a key the schedule lists ('door 104B' -> row 104B)
            box, to_row = None, None
            try:
                rows = schedule_rows(pick.file or path, pick.page)
                # dotted US module numbers first ('7.04B', '9A.1A', '10.01A'): else findall stops at the '7' (F8 c7)
                asked = {t.upper() for t in re.findall(r"\b(?:\d{1,3}[A-Z]?\.\d{1,3}[A-Z]?|[A-Z]{0,2}-?\d{1,4}[A-Z]?)\b",
                                                        q.upper())}
                row = next((r for r in rows if r["key"].upper() in asked and r["kind"] in want), None) or \
                    next((r for r in rows if r["key"].upper() in asked), None)
                via = None
                if row is None and not asked and not _PLAN_Q.search(q):     # never a count / area: one row
                    row, via = _door_of_room(rows, q)
                elif row is not None and row["kind"] == "room" and re.search(r"\b(?:doors?|windows?)\b", q, re.I) \
                        and not _PLAN_Q.search(q):
                    # 'the door into room 102': 102 is a ROOM number; its door is 102S
                    d, v = _door_of_room(rows, q, room=row)
                    if d is not None:
                        row, via = d, v
                if row:
                    box = row["bbox"]
                    why += (f", {via}" if via else "") + f", row {row['key']} of {row['table']}"
                    ql = q.lower()
                    col = None if row["kind"] not in ("door", "window") or subj == "part" else \
                        "width" if re.search(r"\b(?:width|wide)\b", ql) else \
                        "height" if re.search(r"\b(?:height|tall|high)\b", ql) else \
                        "thickness" if re.search(r"\bthick(?:ness)?\b", ql) else None
                    named = {c for c in ("width", "height", "thickness")
                             if re.search({"width": r"\b(?:width|wide)\b", "height": r"\b(?:height|tall|high)\b",
                                           "thickness": r"\bthick(?:ness)?\b"}[c], ql)}
                    if col and len(named) > 1:
                        # 'width and height' = the door's size; with the thickness too: no single answer
                        col = "size" if named == {"width", "height"} else None
                    if row["kind"] in ("door", "window") and not named and subj != "part" and \
                            re.search(r"\b(?:size|dimensions?|how\s+big)\b", ql) and not _PLAN_Q.search(q):
                        col = "size"                                # F8 c8: width x height of the named row
                    cells, asked = _row_cells(pick.file or path, pick.page, box, col)
                    to_row = {"key": row["key"], "table": row["table"], "cells": cells, "asked": asked}
            except Exception:  # noqa: BLE001
                pass
            qq = q
            if keyed and subj != "part" and not re.search(r"\b(?:width|height|thickness|size)\b", q, re.I):
                # the schedule's own column word: 'How tall is door 7.04A?' read the WIDTH on d31 (3'-0" @0.65, gold 7'-0")
                w = _KEYED_SIZE_Q.search(q).group(0).lower()
                col = {"wide": "width", "tall": "height", "high": "height"}.get(w, "thickness" if w.startswith("thick") else "size")
                qq = f"What is the {col} of {_KEYED_OPENING_Q.search(q).group(0)}?"
                why += f"; asked there as '{qq}' (the schedule's column)"
            h = hop(pick.file, pick.page, pick.number, pick.title, why, None, box, qq)
            h.to_row = to_row
            return h
    # (3) a callout whose target view's title matches the question ('pipe trench' -> E1 / S-501F 'PIPE TRENCH
    # SECTION'), else a 'SEE SHEET X FOR <subject>' note that does. Generic words never match alone.
    qw = _words(q) - {"foundation", "floor", "building", "wall", "room"}
    if _PLAN_Q.search(q):  # areas / counts / lengths along the plan stay on the plan
        return None
    if qw and any(r.kind == "callout" for r in refs):
        best, bs = None, 0
        for r in page_refs(path, here.page, with_views=True, set_paths=sp):
            if r.kind != "callout" or not other(r.target_file, r.target_page) or not r.target_view:
                continue
            sc = len(qw & _words(r.target_view))
            if sc > bs:
                best, bs = r, sc
        if best is not None and bs >= 1:
            why = f"callout {best.text} on {where} points to '{best.target_view}' on sheet {best.target_sheet} " \
                  f"({'other file, ' if best.target_file else ''}page {best.target_page + 1})"
            return hop(best.target_file, best.target_page, best.target_sheet, best.target_title, why, best.as_dict(),
                       best.target_bbox, f"{q} (view: {best.target_view})", mode="suggest")
    best, bs = None, 0
    for r in refs:
        if r.kind != "note" or not other(r.target_file, r.target_page):
            continue
        sc = len(qw & _words(r.subject or r.text))
        if sc > bs:
            best, bs = r, sc
    if best is not None and bs >= 1:
        title, box = _find_view(best.target_file or path, best.target_page, best.detail) if best.detail else (None, None)
        why = f"note '{best.text}' on {where} sends this to sheet {best.target_sheet}" + \
              (f" ({best.note})" if best.note else "")
        return hop(best.target_file, best.target_page, best.target_sheet, best.target_title, why, best.as_dict(), box,
                   f"{q} (view: {title})" if title else q, mode="suggest")
    return None


# ---------------------------------------------------------------------------------------------------- HTTP router
def enabled() -> bool:
    """On when STOA_FEATURES names 'xref' or 'all' (the registry's rule; QA 21:19: 'all' mounted the router but every call
    said 'off')."""
    want = {x.strip().lower() for x in os.environ.get("STOA_FEATURES", "").split(",")}
    return FLAG in want or "all" in want


def _uploads_root() -> Path:
    return Path(os.environ.get("STOA_UPLOADS") or os.path.join(tempfile.gettempdir(), "stoa-uploads"))


_UPLOAD_PATH = re.compile(r"^upload/([A-Za-z0-9_-]{1,64})/(?:([a-z0-9_]+)/)?([^/\\]+)$")


def resolve_path(p: str) -> str:
    """A browser path -> a local file: 'upload/<id>/<name>' (W5's uploads, $STOA_UPLOADS) or a dataset-relative path
    (data/...). Raises ValueError for anything else ('..', absolute paths, missing files)."""
    if not p or ".." in Path(p).parts or p.startswith("/") or "\\" in p:
        raise ValueError("path must be 'upload/<id>/<name>' or dataset-relative, without '..'")
    if p.startswith("upload/"):
        # 'upload/<id>/<name>', or 'upload/<id>/<style>/<name>' = a picture uploaded with a drawing style (the web stores
        # it under '<id>/data/<style>/<name>', web/mock/stoaApi.ts uploadAbs; images get one by default when the style
        # guess is sure, e.g. 'cubicasa5k')
        m = _UPLOAD_PATH.match(p)
        if not m or m.group(3).startswith("."):
            raise ValueError("upload path is upload/<id>/<name> or upload/<id>/<style>/<name>")
        root = _uploads_root().resolve()
        f = (root / m.group(1) / "data" / m.group(2) / m.group(3) if m.group(2) else root / m.group(1) / m.group(3)).resolve()
        if root not in f.parents:
            raise ValueError("path outside the uploads folder")
    else:
        from stoa.data import abspath, data_root

        root = data_root().resolve()
        f = abspath(p).resolve()
        if root not in f.parents:
            raise ValueError("path outside the dataset")
    if not f.is_file():
        raise FileNotFoundError(p)
    return str(f)


def _make_router():
    from fastapi import APIRouter, HTTPException, Query

    router = APIRouter(prefix="/feat/xref", tags=["xref (F8)"])

    def _path(p: str) -> str:
        if not enabled():
            raise HTTPException(404, "feature 'xref' is off (STOA_FEATURES)")
        try:
            return resolve_path(p)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    def _set(virtual: str, f: str) -> tuple[list[str] | None, dict]:
        """Files dropped together share one upload folder (W5's /upload/set): its PDFs are the set. -> (abs paths or
        None, abs -> browser path)."""
        if not virtual.startswith("upload/"):
            return None, {f: virtual}
        folder, vdir = os.path.dirname(f), virtual.rsplit("/", 1)[0]
        # the sheets dropped together: PDFs and images, not the server's derived copies ('A-101F.r136.png', '.s6000.png')
        pdfs = sorted(os.path.join(folder, n) for n in os.listdir(folder)
                      if n.lower().endswith((".pdf",) + IMAGE_EXT) and n.count(".") == 1)[:40]
        virt = {p: f"{vdir}/{os.path.basename(p)}" for p in pdfs}
        virt[f] = virtual
        return (pdfs if len(pdfs) > 1 else None), virt

    def _v(virt: dict, fl: str | None) -> str | None:
        return virt.get(fl) or (f".../{os.path.basename(fl)}" if fl else None)

    @router.get("/sheets")
    def sheets(path: str = Query(...)) -> dict:
        f = _path(path)
        try:
            sp, virt = _set(path, f)
            idx = _index_for(f, sp)
        except Exception:  # noqa: BLE001 — fail closed
            idx, virt = [], {}
        out = []
        for e in idx:
            d = asdict(e)
            d["file"] = _v(virt, e.file)
            out.append(d)
        return {"path": path, "sheets": out, "files": len({e.file for e in idx}), "cost_tier": "cheap"}

    @router.get("/refs")
    def refs(path: str = Query(...), page: int = 0, incoming: int = 1) -> dict:
        """incoming=0: skip the whole-set scan for 'referenced from' (the boxes can be drawn at once; ~5 s on a 121-page
        set the first time, then cached) and return incoming: null."""
        f = _path(path)
        try:
            sp, virt = _set(path, f)
            rs = page_refs(f, page, set_paths=sp)
            idx = _index_for(f, sp)
            here = next((e for e in idx if _same(e.file, f) and e.page == page), None)
            inc = [] if incoming else None
            if incoming and len(idx) <= 400:
                for e in idx:
                    if _same(e.file, f) and e.page == page:
                        continue
                    if any(r.kind not in ("copy", "level") and r.target_page == page and _same(r.target_file or e.file, f)
                           for r in page_refs(e.file or f, e.page, with_views=False, set_paths=sp, links=False)):
                        inc.append({"page": e.page, "number": e.number, "title": e.title,
                                    "file": None if _same(e.file, f) else _v(virt, e.file)})
            out = []
            for r in rs:
                d = r.as_dict()
                d["target_file"] = _v(virt, r.target_file)
                for t in d.get("targets") or []:
                    t["file"] = _v(virt, t.get("file"))
                out.append(d)
            sheet = asdict(here) if here else None
            if sheet:
                sheet["file"] = path
            rel = []
            if sp:
                for x in related_sheets(f, page, set_paths=sp):
                    x["file"] = _v(virt, x["file"])
                    rel.append(x)
        except Exception:  # noqa: BLE001
            out, sheet, inc, rel = [], None, [], []
        return {"path": path, "page": page, "sheet": sheet, "refs": out, "incoming": inc, "related": rel,
                "scan": is_scan(f), "cost_tier": "cheap"}

    @router.get("/reconcile")
    def reconcile_ep(path: str = Query(...)) -> dict:
        """Schedules vs the tags on the set's other sheets (both ways), for the panel's 'Schedule check'."""
        f = _path(path)
        try:
            sp, virt = _set(path, f)
            res = reconcile(f, set_paths=sp)
            tabs = []
            for t in res["tables"]:
                t = dict(t)
                t["file"] = _v(virt, t["file"]) if t["file"] and not _same(t["file"], f) else None
                t["links"] = {k: [dict(x, file=_v(virt, x["file"]) if x["file"] and not _same(x["file"], f) else None)
                                  for x in v] for k, v in t["links"].items()}
                tabs.append(t)
            out = {"tables": tabs, "sheets_read": res["sheets_read"]}
        except Exception:  # noqa: BLE001
            out = {"tables": [], "sheets_read": 0}
        return {"path": path, **out, "cost_tier": "cheap"}

    @router.post("/follow")
    def follow(req: _FollowReq) -> dict:
        f = _path(req.path)
        try:
            sp, virt = _set(req.path, f)
            hop = follow_plan(f, req.page, req.question, set_paths=sp)
            d = hop.as_dict() if hop else None
            if d:
                d["to_file"] = _v(virt, hop.to_file)
                if d.get("ref"):
                    d["ref"]["target_file"] = _v(virt, d["ref"].get("target_file"))
        except Exception:  # noqa: BLE001
            hop, d = None, None
        return {"path": req.path, "page": req.page, "hop": d,
                "trace": [f"xref: {hop.why}"] if hop else [], "cost_tier": "cheap"}

    return router


try:
    from pydantic import BaseModel

    class _FollowReq(BaseModel):  # module level: FastAPI resolves the (postponed) annotation from module globals
        path: str
        page: int = 0
        question: str

    router = _make_router()
except Exception:  # noqa: BLE001 — no FastAPI in this env: the module still works as a library
    router = None
