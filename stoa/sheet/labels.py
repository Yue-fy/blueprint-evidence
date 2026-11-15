"""Room tags read on the DISPLAYED page, per view (VP).

A US / Revit room tag is a centred stack 'Lobby' / '1.01' / '569 SF'. On a /Rotate 90 PDF (5 of Stoa's 9 example
PDFs) 'above' in PyMuPDF's unrotated space is a different axis, so a reader that stacks names above the area line in
pdf_pt finds no names there (area_tags on AFCEE A-101: 0 of 34 tags named -> 'total area of module 01' = 108 SF, gold
1388). Here the stack is read in display space, each tag is assigned to the view that holds it, and the same room
printed in several views (floor plan + RCP) is kept once (the plan's).

    room_tags(path, page=0)                 -> [RoomTag] every tag on the page, with its view
    scoped_tags(path, question, page=0)     -> (tags in the views the question means, Scope | None), deduped by number
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass

from stoa.sheet.views import DRAWING_KINDS, Scope, page_reading, scope_for, views

_AREA = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|NSF|GSF|m²|m2|M2|"
                   r"sq\.?\s*m|sqm)\s*$", re.I)
_ROOMNO = re.compile(r"^\s*[A-Z]{0,2}\d{1,4}(?:[.\-]\d{1,3})?[A-Z]?\s*$")
# number and area on one line ('U106 - 468 SF', '103.E - 545 SF'), optionally after the name ('CORRIDOR - NORTH
# 103.N - 393 SF'): Revit tags of apartment sets (CTR A101)
_NO_AREA = re.compile(r"^\s*(?:(?P<name>.*?\S)\s+)?(?P<no>[A-Z]{0,2}\d{1,4}(?:\.[A-Z0-9]{1,3})?[A-Z]?)\s*[-–]\s*"
                      r"(?P<v>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<u>SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|NSF|GSF|m²|m2|"
                      r"M2|sq\.?\s*m|sqm)\s*$", re.I)


_BRACKETED = re.compile(r"^\s*[(\[]\s*(.*?)\s*[)\]]\s*$")


def _unbracket(text: str) -> str:
    """'(55.0 m2)' -> '55.0 m2': DfE 2012 baseline sheets print the area in brackets under the name (G1 01:3x, F5 23:45).
    STOA_VP_BRACKET=0 turns it off."""
    if os.environ.get("STOA_VP_BRACKET", "1") == "0":
        return text
    m = _BRACKETED.match(text or "")
    return m.group(1) if m else text


def _overlap(h: float, h2: float) -> float:
    """How far an OCR'd name line may reach down into the area line under it: 0.4 of the tag's height on a PDF; picture
    layers box each line loosely, so up to half the taller line (EFA phone photo: 'English 8' overlaps its '55 m2' by
    6 px = 0.40 x 15 px and was left out: 'English classrooms' 440 vs 495, X1 sl-340). STOA_VP_TAG_OVERLAP=0: 0.4 x h."""
    if os.environ.get("STOA_VP_TAG_OVERLAP", "1") == "0":
        return 0.4 * h
    return max(0.4 * h, 0.5 * max(h, h2))


@dataclass
class RoomTag:
    name: str | None
    number: str | None
    value: float
    unit: str            # 'ft2' | 'm2'
    text: str            # the area line as printed
    box: tuple           # whole tag stack, page space (pdf_pt unrotated)
    area_box: tuple      # the area line, page space
    view: str | None = None       # title of the view holding the tag
    view_kind: str | None = None
    dwelling: bool = False        # the flat's headline ('2 H+KK+S 45.5 m²'): the whole dwelling, never a room

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.name, self.number) if x) or "(unlabelled tag)"


@functools.lru_cache(maxsize=16)
def _tags_cached(path: str, page: int) -> tuple:
    return _tags(path, page, None)


def _tags(path: str, page: int, layer) -> tuple:
    r = page_reading(path, page, layer)
    if r is None:
        return ()
    lines, _ink, _s, _W, _H, to_src, _to_disp = r
    vs = views(path, page, layer=layer)
    out, used, nameless = [], set(), []
    for ln in lines:
        txt = _unbracket(ln.text)
        m = _AREA.match(txt)
        m2 = None if m else _NO_AREA.match(txt)
        if not m and not m2:
            continue
        if m:
            v = float(m.group(1).replace(",", ""))
            unit = "m2" if re.match(r"m|sq\.?\s*m", m.group(2), re.I) else "ft2"
            names, number = [], None
        else:
            v = float(m2.group("v").replace(",", ""))
            unit = "m2" if re.match(r"m|sq\.?\s*m", m2.group("u"), re.I) else "ft2"
            names, number = ([m2.group("name").rstrip(" -–")] if m2.group("name") else []), m2.group("no")
        h, cx, top = ln.h, ln.cx, ln.box[1]
        box = list(ln.box)
        for _ in range(4):  # name / number lines stacked just above, centred on the tag
            cand = [l2 for l2 in lines if l2 is not ln and -_overlap(h, l2.h) < top - l2.box[3] <= 0.9 * h + 1
                    and l2.box[1] < top - 0.3 * h and abs(l2.cx - cx) <= max(0.6 * (ln.box[2] - ln.box[0]),
                                                                            0.6 * (l2.box[2] - l2.box[0]), 2.5 * h)
                    and not _AREA.match(_unbracket(l2.text)) and not _NO_AREA.match(_unbracket(l2.text))]
            if not cand:
                break
            l2 = max(cand, key=lambda x: (round(x.box[3]), -abs(x.cx - cx)))
            used.add(id(l2))
            if _ROOMNO.match(l2.text) and not number and not names:
                number = l2.text.strip()
            else:
                names.insert(0, l2.text.strip())
            top = l2.box[1]
            box = [min(box[0], l2.box[0]), min(box[1], l2.box[1]), max(box[2], l2.box[2]), max(box[3], l2.box[3])]
        if not names and not number:
            nameless.append((len(out), ln))
        out.append(_make_tag(names, number, v, unit, ln, box, to_src, vs))
    if os.environ.get("STOA_VP_SIDE_NAMES", "1") != "0":
        for k, ln, l2 in _side_names(lines, nameless, used):
            box = [min(ln.box[0], l2.box[0]), min(ln.box[1], l2.box[1]), max(ln.box[2], l2.box[2]),
                   max(ln.box[3], l2.box[3])]
            t = out[k]
            nm, no = ([], l2.text.strip()) if _ROOMNO.match(l2.text) else ([l2.text.strip()], None)
            out[k] = _make_tag(nm, no, t.value, t.unit, ln, box, to_src, vs)
    _unglue(out)
    return tuple(out)


def _make_tag(names, number, v, unit, ln, box, to_src, vs) -> RoomTag:
    src = to_src(box)
    holder = None
    for vw in vs:
        if vw.contains((src[0] + src[2]) / 2, (src[1] + src[3]) / 2, pad=2):
            holder = vw
            break
    name = " ".join(names) or None
    return RoomTag(name=name, number=number, value=v, unit=unit, text=ln.text.strip(), box=src,
                   area_box=to_src(ln.box), view=holder.title if holder else None,
                   view_kind=holder.kind if holder else None,
                   dwelling=bool(name and _DWELLING.match(name) and os.environ.get("STOA_VP_DWELLING", "1") != "0"))


# O1 02:24 (Finnish flat plan, CubiCasa style): the flat's type code + area is its HEADLINE ('2 H+KK+S 45.5 m²' = 2 rooms
# + kitchenette + sauna, 45.5 m² in all), never a room; Swedish '2 rok' / '3 r+k' / '2 rum och kök'
_DWELLING = re.compile(r"^\s*(?:as\.?\s*\d{1,3}[a-z]?|\d\s*(?:h|mh|r)\s*(?:[+,]\s*(?:kk|k|kt|kh|s|sauna|vh|p|parv|las(?:itettu)?\.?(?:\s*parv\w*)?|"
                       r"wc|khh|tk|et|ak|at|var|rt|tupak|avok|ok|ph|lh|th)\b\.?\s*)+|\d\s*rok\b(?:\s*\+\s*\w+)*|"
                       r"\d\s*rum\s+(?:och|o)\s+kök\b.*)\s*$", re.I)
# a room code / short name that may sit BESIDE or BELOW its area line (Finnish flat plans: 'KK' left of '6.0 m²', 'OH'
# under '13.0 m²'); 2+ letters, not a dimension ('18 x 12', '3600')
_CODE_LINE = re.compile(r"^(?=(?:.*[A-Za-zÄÖÅäöåÜü]){2})[\w .,/&'()+\-ÄÖÅäöåÜü]{2,32}$")
# never a room name beside a figure: printed totals / table headings / title-block words (sweep 02:4x: 'Overall GIFA'
# beside 1,248, 'SQ FT' beside 40,228, 'AIR FORCE CENTER' / 'FINAL' beside title-block areas, 'Huoneistoala 120m2')
_NOT_A_ROOM = re.compile(r"\b(?:gifa|gia|gea|nia|gfa|total|overall|gross|net|areas?|sq|ft|sf|final|cent(?:er|re)|scale|"
                         r"drawing|project|sheet|title|date|rev(?:ision)?|client|issue|status|huoneistoala|kerrosala|"
                         r"(?:asuin)?pinta-?ala|yta|fl(?:ä|ae)che)\b|\d\s*(?:m2|m²|sf\b|sq)", re.I)


def _side_names(lines, nameless, used) -> list:
    """For tags with no name above them: an unclaimed short name / code line right beside the area line (same row,
    gap <= 1.5 line heights), further above (0.9-2.2 heights) or below it (<= 2.2 heights), each line used once, the
    nearest pairs first. -> [(tag index, area line, name line)]."""
    pairs = []
    for k, ln in nameless:
        m = _AREA.match(_unbracket(ln.text))
        if m is None:
            continue
        v = float(m.group(1).replace(",", ""))
        if v > (400 if re.match(r"m|sq\.?\s*m", m.group(2), re.I) else 4300):
            continue   # a room-sized figure only: bigger ones are floor / building totals
        h, w = ln.h, ln.box[2] - ln.box[0]
        cy = (ln.box[1] + ln.box[3]) / 2
        for j, l2 in enumerate(lines):
            t2 = l2.text.strip()
            one = bool(re.fullmatch(r"[A-ZÄÖÅ]", t2))   # 'S' (sauna) / 'K' (keittiö): only right beside the area
            if id(l2) in used or l2 is ln or not (one or _CODE_LINE.match(t2)) or _AREA.match(_unbracket(t2)) or \
                    _NO_AREA.match(_unbracket(t2)) or _DWELLING.match(t2) or _NOT_A_ROOM.search(t2) or \
                    t2.count("(") != t2.count(")"):
                continue
            hh = max(h, l2.h)
            dy = (l2.box[1] + l2.box[3]) / 2 - cy
            dx = abs(l2.cx - ln.cx)
            d = None
            if abs(dy) <= 1.0 * hh:
                gap = ln.box[0] - l2.box[2] if l2.cx < ln.cx else l2.box[0] - ln.box[2]
                if -0.2 * hh <= gap <= 1.5 * hh:
                    d = max(gap, 0) + abs(dy)
            elif not one and dx <= max(0.6 * w, 0.6 * (l2.box[2] - l2.box[0]), 1.5 * hh):
                below, above = l2.box[1] - ln.box[3], ln.box[1] - l2.box[3]
                if -0.3 * hh <= below <= 2.2 * hh:
                    d = max(below, 0) + 0.5 * dx + 0.5 * hh
                elif 0.9 * h < above <= 2.2 * hh:
                    d = above + 0.5 * dx + 0.25 * hh
            if d is not None:
                pairs.append((d, k, j, ln, l2))
    out, tk, tj = [], set(), set()
    for d, k, j, ln, l2 in sorted(pairs, key=lambda p: p[0]):
        if k in tk or j in tj:
            continue
        tk.add(k)
        tj.add(j)
        out.append((k, ln, l2))
    return out


_GLUED = re.compile(r"^(?P<head>.*?\b)(?P<w>[A-Za-z]{2,})(?P<n>\d{1,3})$")
_SPACED = re.compile(r"\b([A-Za-z]{2,}) \d{1,3}$")


def _unglue(tags: list) -> None:
    """OCR drops the space in 'MFL 1' -> 'MFL1' (EFA colour sheet scans: MFL1 / MFL3 / MFL4 / MFL6 next to 'MFL 2',
    'MFL 5'), so 'the MFL rooms' sums 2 of 6. A glued word + number is split only when the same sheet prints that word
    with a spaced number ('MFL 2'): an id that is always glued ('GC2' catchments) stays as printed."""
    if os.environ.get("STOA_VP_UNGLUE", "1") == "0":
        return
    spaced = {m.group(1).lower() for t in tags if t.name for m in [_SPACED.search(t.name)] if m}
    if not spaced:
        return
    for t in tags:
        m = _GLUED.match(t.name or "")
        if m and m.group("w").lower() in spaced:
            t.name = f"{m.group('head')}{m.group('w')} {m.group('n')}"


def room_tags(path: str, page: int = 0, layer=None) -> list[RoomTag]:
    """Every room tag on a PDF page, or on an image given its text layer (boxes then in the layer's image px)."""
    try:
        if str(path).lower().endswith(".pdf"):
            return list(_tags_cached(str(path), int(page)))
        return list(_tags(str(path), int(page), layer)) if layer is not None else []
    except Exception:  # noqa: BLE001
        return []


def dedupe(tags: list[RoomTag]) -> list[RoomTag]:
    """One tag per room: the same number (or name + value) printed in several views -> keep the plan view's. A name +
    value repeated within ONE view is several rooms (G1 00:01: EFA 'ICT Classroom 62 m2' twice = two rooms, total 124,
    not 62; repeated modules / dorm rooms): every tag of the chosen view is kept, except a copy printed on top of
    another (centres within one text height: the same text drawn twice)."""
    rank = {"floor_plan": 0, "drawing": 1}

    def key(t):
        return t.number or (t.name or "").lower() + f"|{t.value:g}"

    best: dict = {}
    for t in tags:
        cur = best.get(key(t))
        if cur is None or rank.get(t.view_kind, 5) < rank.get(cur.view_kind, 5):
            best[key(t)] = t
    out: list[RoomTag] = []
    kept: dict = {}
    for t in tags:
        b = best.get(key(t))
        if t.number:
            if b is t:
                out.append(t)
            continue
        if b is None or t.view != b.view:
            continue
        cx, cy = (t.box[0] + t.box[2]) / 2, (t.box[1] + t.box[3]) / 2
        h = max(1.0, min(t.box[2] - t.box[0], t.box[3] - t.box[1]))
        if any(abs(cx - x) <= h and abs(cy - y) <= h for x, y in kept.get(key(t), [])):
            continue
        kept.setdefault(key(t), []).append((cx, cy))
        out.append(t)
    return out


def scoped_tags(path: str, question: str, page: int = 0, layer=None) -> tuple[list[RoomTag], Scope | None]:
    """Room tags of the views the question means (all drawing views when nothing narrows), one per room."""
    tags = room_tags(path, page, layer=layer)
    s = scope_for(path, question, page, layer=layer)
    if s:
        titles = {v.title for v in s.views}
        tags = [t for t in tags if t.view in titles]
    else:
        tags = [t for t in tags if t.view_kind is None or t.view_kind in DRAWING_KINDS]
    return dedupe(tags), s


def as_area_tags(path: str, question: str | None = None, page: int = 0, layer=None) -> tuple:
    """Drop-in for stoa.solvers.area_tags.read_tags (same dicts: number, name, value, unit, apt, text, bbox, tag_bbox),
    read on the displayed page and, with a question, limited to the views it means (one tag per room). () when the
    page has no room tags, so a caller can fall back: `tags = as_area_tags(pdf, q) or read_tags(pdf)`."""
    if question:
        tags, _s = scoped_tags(path, question, page, layer=layer)
    else:
        tags = dedupe([t for t in room_tags(path, page, layer=layer)
                       if t.view_kind is None or t.view_kind in DRAWING_KINDS])
    tags = [t for t in tags if not t.dwelling]   # a flat's headline is never one of its rooms (O1 02:24)
    if not any(t.name or t.number for t in tags):
        return ()  # only table totals ('1ST FLOOR  21996 SF'), no room tag: let the caller read the page its own way
    return tuple({"number": t.number, "name": t.name, "value": t.value, "unit": t.unit, "apt": None, "text": t.text,
                  "bbox": [round(v, 1) for v in t.box], "tag_bbox": [round(v, 1) for v in t.area_box], "view": t.view}
                 for t in tags)
