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
    out = []
    for ln in lines:
        m = _AREA.match(ln.text)
        m2 = None if m else _NO_AREA.match(ln.text)
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
            cand = [l2 for l2 in lines if l2 is not ln and -0.4 * h < top - l2.box[3] <= 0.9 * h + 1
                    and l2.box[1] < top - 0.3 * h and abs(l2.cx - cx) <= max(0.6 * (ln.box[2] - ln.box[0]),
                                                                            0.6 * (l2.box[2] - l2.box[0]), 2.5 * h)
                    and not _AREA.match(l2.text) and not _NO_AREA.match(l2.text)]
            if not cand:
                break
            l2 = max(cand, key=lambda x: (round(x.box[3]), -abs(x.cx - cx)))
            if _ROOMNO.match(l2.text) and not number and not names:
                number = l2.text.strip()
            else:
                names.insert(0, l2.text.strip())
            top = l2.box[1]
            box = [min(box[0], l2.box[0]), min(box[1], l2.box[1]), max(box[2], l2.box[2]), max(box[3], l2.box[3])]
        src = to_src(box)
        holder = None
        for vw in vs:
            if vw.contains((src[0] + src[2]) / 2, (src[1] + src[3]) / 2, pad=2):
                holder = vw
                break
        out.append(RoomTag(name=" ".join(names) or None, number=number, value=v, unit=unit, text=ln.text.strip(),
                           box=src, area_box=to_src(ln.box), view=holder.title if holder else None,
                           view_kind=holder.kind if holder else None))
    return tuple(out)


def room_tags(path: str, page: int = 0, layer=None) -> list[RoomTag]:
    """Every room tag on a PDF page, or on an image given its text layer (boxes then in the layer's image px)."""
    try:
        if str(path).lower().endswith(".pdf"):
            return list(_tags_cached(str(path), int(page)))
        return list(_tags(str(path), int(page), layer)) if layer is not None else []
    except Exception:  # noqa: BLE001
        return []


def dedupe(tags: list[RoomTag]) -> list[RoomTag]:
    """One tag per room: the same number (or name + value) printed in several views -> keep the plan view's."""
    rank = {"floor_plan": 0, "drawing": 1}
    best: dict = {}
    for t in tags:
        key = t.number or (t.name or "").lower() + f"|{t.value:g}"
        cur = best.get(key)
        if cur is None or rank.get(t.view_kind, 5) < rank.get(cur.view_kind, 5):
            best[key] = t
    return [t for t in tags if best.get(t.number or (t.name or "").lower() + f"|{t.value:g}") is t]


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
    if not any(t.name or t.number for t in tags):
        return ()  # only table totals ('1ST FLOOR  21996 SF'), no room tag: let the caller read the page its own way
    return tuple({"number": t.number, "name": t.name, "value": t.value, "unit": t.unit, "apt": None, "text": t.text,
                  "bbox": [round(v, 1) for v in t.box], "tag_bbox": [round(v, 1) for v in t.area_box], "view": t.view}
                 for t in tags)
