"""Printed room SIZES ('LIVING ROOM' over '13'-5" x 13'-6"') -> the room's area (X1 19:33, real HABS scans).

Residential plans (HABS / survey drawings, sales plans, many scanned sheets) print each room's nominal size right under
or next to its name: "LIVING ROOM 13'-5" x 13'-6"", "KITCHEN 4,20 x 3,60". The area the question asks for is then
A x B, read straight from the sheet, with no scale and no outline: night d24 (Gemini off) answered these from a picture
reading (living room 14.44 m² @0.05 vs gold 181.13 sq ft) or read one side as the area (kitchen 13.5).

Reads the text layer the other readers use (PDF words; for images the cached / cheap layer of
label_count._image_lines, never a fresh slow OCR). The asked room = the label whose words best match the question's
room words; the size = a 'A x B' string on the same line after the name or the nearest one just below it. ft-in sizes
give ft² (m² when the question asks), decimal metre sizes ('4,20 x 3,60', '4.2 x 3.6 m') give m². Bare integer
pairs ('900 x 2100': doors, mm) are not room sizes and are ignored. Abstains when nothing like that is printed.
"""

from __future__ import annotations

import re

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

FT2_M2 = 0.09290304
_FT = r"(\d{1,3})\s*['’′]\s*-?\s*(?:(\d{1,2})(?:\s+(\d)/(\d))?\s*(?:[\"”″]|'')?)?"
_FTIN_SIZE = re.compile(_FT + r"\s*[xX×]\s*" + _FT)
_M_SIZE = re.compile(r"(?<![\d.,])(\d{1,2}[.,]\d{1,2})\s*(?:m\b)?\s*[xX×]\s*(\d{1,2}[.,]\d{1,2})(?![\d.,])\s*(?:m\b)?")
_STOP = {"what", "is", "the", "of", "a", "an", "floor", "area", "how", "big", "large", "in", "square", "metres", "meters",
         "feet", "foot", "ft", "sq", "m2", "m²", "sf", "size", "total", "net", "this", "that", "plan", "drawing", "sheet",
         "on", "at", "does", "do", "have", "has", "which", "much", "space", "there", "are", "be", "its", "it", "s", "me",
         "tell", "please", "give", "and", "approximately", "roughly", "about", "measure", "measures", "house", "building",
         "shown", "drawn", "ground", "first", "second", "upper", "lower", "level", "storey", "story", "for", "to"}
_GENERIC = {"room", "rooms"}


def _ft(m, i: int) -> float:
    f = float(m.group(i))
    inch = float(m.group(i + 1) or 0)
    if m.group(i + 2) and m.group(i + 3) and float(m.group(i + 3)):
        inch += float(m.group(i + 2)) / float(m.group(i + 3))
    return f + inch / 12.0


def sizes_in(text: str) -> list[tuple[float, float, str, str]]:
    """(a, b, unit 'ft'|'m', matched text) for each 'A x B' room size in a line."""
    out = []
    for m in _FTIN_SIZE.finditer(text):
        a, b = _ft(m, 1), _ft(m, 5)
        if 2 <= a <= 200 and 2 <= b <= 200:
            out.append((a, b, "ft", m.group(0).strip()))
    if not out:
        for m in _M_SIZE.finditer(text):
            a, b = (float(g.replace(",", ".")) for g in m.groups())
            if 0.6 <= a <= 60 and 0.6 <= b <= 60:
                out.append((a, b, "m", m.group(0).strip()))
    return out


_QTOK = re.compile(r"\d{1,3}\s*['’′]\s*-?\s*\d{0,2}\s*[\"”″]?|(?<![\w.])\d{2,4}(?![\w.])|\b[A-Z]\d{1,2}\b")


def _norm_tok(t: str) -> str:
    return re.sub(r"\s+", "", t.replace("’", "'").replace("′", "'").replace("”", '"').replace("″", '"')).rstrip('"')


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zäöåéèüß]+", s.lower()) if len(w) >= 2}


def _lines(path: str):
    """((text, box), ...), bbox space."""
    import os

    from stoa.data import abspath

    p = str(abspath(path))
    if p.lower().endswith(".pdf"):
        from stoa.solvers.label_count import text_lines

        return tuple(text_lines(p, os.stat(p).st_mtime)), "pdf_pt"
    from stoa.solvers.label_count import _image_lines

    return tuple(_image_lines(p) or ()), "image_px"


def answer(path: str, question: str) -> Answer | None:
    from stoa.router.intent import parse

    it = parse(question)
    if it.task != "area" or re.search(r"\b(?:gross|whole|entire|total|overall|all|combined|together|sum)\b",
                                      question, re.I):
        return None  # one room's printed size; totals / gross areas are other readers' job
    qw = _words(question) - _STOP
    if not qw - _GENERIC:
        return None
    try:
        lines, space = _lines(path)
    except Exception:  # noqa: BLE001
        return None
    if not lines:
        return None
    sized = [(i, s) for i, (t, _) in enumerate(lines) for s in sizes_in(t)]
    if not sized:
        return None
    cands = []
    for i, (t, bb) in enumerate(lines):
        name = _FTIN_SIZE.sub(" ", _M_SIZE.sub(" ", t))
        lw = _words(name)
        if not lw or len(lw) > 5:
            continue
        hit = qw & lw
        # 'bedroom' ~ 'bed room', 'living' alone names 'LIVING ROOM'
        if not (hit - _GENERIC) and not any(w.startswith(x) or x.startswith(w) for w in qw - _GENERIC for x in lw
                                             if min(len(w), len(x)) >= 5):
            continue
        score = len(hit) + (0.5 if not (lw - qw - _GENERIC) else 0.0)
        cands.append((score, i, t, bb))
    if not cands:
        return None
    # D1 aw29 probe: 'the 1'-6" slab-on-grade' matched the '1'-9" SLAB-ON-GRADE' label on words alone: a size / number
    # the question names ('1'-6"', '150', 'D13') must be printed in the label when some label prints it
    toks = {_norm_tok(m) for m in _QTOK.findall(question)}
    if toks:
        named = [c for c in cands if toks & {_norm_tok(m) for m in _QTOK.findall(c[2])}]
        if named:
            cands = named
    best = max(c[0] for c in cands)
    picks = []
    for score, i, t, bb in cands:
        if score != best:
            continue
        own = sizes_in(t)
        if own:
            picks.append((i, t, bb, own[0], i))
            continue
        h = max(bb[3] - bb[1], 1.0)
        below = None
        for j, s in sized:
            if j == i:
                continue
            sb = lines[j][1]
            gap = sb[1] - bb[3]
            ox = min(bb[2], sb[2]) - max(bb[0], sb[0])
            if -0.5 * h <= gap <= 3.2 * h and ox > -0.25 * max(bb[2] - bb[0], sb[2] - sb[0]):
                if below is None or gap < below[0]:
                    below = (gap, j, s)
        if below is not None:
            picks.append((i, t, bb, below[2], below[1]))
    if not picks:
        return None
    i, t, bb, (a, b, u, txt), j = picks[0]
    want_m2 = it.unit == "m2" and re.search(r"m²|\bm2\b|square met|sq\.? ?m\b|\bsqm\b", question, re.I)
    if u == "ft":
        v, unit = a * b, "ft2"
        if want_m2:
            v, unit = v * FT2_M2, "m2"
    else:
        v, unit = a * b, "m2"
        if re.search(r"\bsq\.? ?ft|square f|\bsf\b|ft²|ft2", question, re.I):
            v, unit = v / FT2_M2, "ft2"
    img = space == "image_px"
    conf = 0.8 if len(picks) == 1 else 0.45
    if img:
        conf -= 0.1
    sb = lines[j][1]
    ev = [Evidence(file=path, page=0, bbox=[round(float(c), 1) for c in bb], bbox_space=space, exact=not img,
                   note=f"room label: {t.strip()[:60]}")]
    if j != i:
        ev.append(Evidence(file=path, page=0, bbox=[round(float(c), 1) for c in sb], bbox_space=space, exact=not img,
                           note=f"printed size: {txt}"))
    unit_txt = {"ft2": "sq ft", "m2": "m²"}[unit]
    assumptions = [f"area = the room's printed size {txt} ({a:.3g} x {b:.3g} {u}) = {v:.2f} {unit_txt}; a printed nominal "
                   "size is a rectangle (bays / closets not included)"]
    if u == "ft" and unit == "m2":
        assumptions.append("converted from square feet (1 sq ft = 0.09290304 m²)")
    missing = [] if len(picks) == 1 else [f"{len(picks)} labels match the question ("
                                          + ", ".join(p[1].strip()[:30] for p in picks[:4]) + "): name the room exactly"]
    return Answer(value=round(v, 2), unit=unit, confidence=round(conf, 2), evidence=ev, solver="room_size",
                  trace=[f"room_size: '{t.strip()[:40]}' with the printed size '{txt}' -> {a:.3g} x {b:.3g} {u} = "
                         f"{v:.2f} {unit_txt}" + (f" ({len(picks)} matching labels; the first)" if len(picks) > 1 else "")],
                  assumptions=assumptions, missing=missing)


@register("room_size")
class RoomSize(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"))

    def solve(self, path: str, question: str) -> Answer:
        a = answer(path, question)
        return a if a is not None else self.abstain("no room size 'A x B' printed by the asked room")
