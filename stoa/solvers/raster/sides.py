"""Which side of a room a printed dimension string measures (O1 c13).

X1 23:25 (EFA phone photo sl-336): 'How long is the main hall?' -> 10.15 m @0.75 citing the grid dimension '10,150'
printed ABOVE the hall: a horizontal string, so it measures the hall's west-east side, which is its SHORT side (the hall
runs north-south, 17.98 m; the PDF twin answers 18.06). A dimension string measures along its own text direction
(horizontal text -> an x extent, text turned 90° -> a y extent). The room's drawn outline (the ink fill from its label)
says which of its two sides is the long one. When the cited string runs along the other side than the asked one
('long' / 'length' -> the long side, 'wide' / 'width' / 'breadth' -> the short side), the reading answers the wrong side.

`wrong_side(question, value, unit, cited, tl, img_path, px_per_m)` -> a note for `missing`, or None (no claim).
STOA_O1_SIDE_GUARD=0 turns the check off.
"""
from __future__ import annotations

import difflib
import os
import re

from stoa.solvers.raster import measure as M
from stoa.solvers.raster import textlayer as T

GUARD = os.environ.get("STOA_O1_SIDE_GUARD", "1") != "0"
# the asked side estimated from the printed value x the drawn proportions replaces the reading (else: conf <= 0.3 only)
ESTIMATE = os.environ.get("STOA_O1_SIDE_EST", "1") != "0"
EST_CONF, EST_CONF_PHOTO = 0.45, 0.3
ASPECT = 1.3  # long / short side of the room's outline: below this the 'long' side is not clear
ORIENT = 1.5  # text box w / h (or h / w) for a clear text direction
VALUE_OK = 0.15  # a value within 15 % of the ASKED side's drawn length is not contradicted
VALUE_ALONG = 0.25  # ... and it must be within 25 % of the drawn side its string runs along (photos: perspective)
MAX_M2 = 2500.0  # a bigger region is a leak (sheet / site), not a room

_LONG_Q = re.compile(r"\b(how long|length|long side|longer side|pituus|pitkä|länge|wie lang|längd|hur lång|longueur|"
                     r"lunghezza|longitud)\b", re.I)
_WIDE_Q = re.compile(r"\b(how wide|width|wide|breadth|broad|leveys|leveä|breite|wie breit|bredd|hur bred|largeur|"
                     r"larghezza|anchura|ancho)\b", re.I)
# a bare printed dimension: '10,150' / '10.150' / '3.50' / '3 500' / "15'-6\"" / '7.42 m' (no words)
_DIM_TXT = re.compile(r"\s*(?:\d[\d\s.,]*\d|\d)(?:\s?(?:mm|cm|m))?\s*|\s*\d{1,3}\s*'\s*-?\s*\d{0,2}(?:\s*\d/\d)?\s*\"?\s*")
_NOT_ROOM_Q = re.compile(r"\b(walls?|doors?|windows?|openings?|doorways?|entrances?|stairs?|stairways?|ramps?|"
                         r"corridors? (?:into|to)|beams?|slabs?|pipes?|lines?|runs?|counters?|benches?|grid|bays?|"
                         r"total|combined|sum|overall|building|block|wing|site|plot|perimeter|skirting|distance|"
                         r"between|from|thick\w*)\b", re.I)


_STOP = {"how", "long", "wide", "deep", "is", "the", "what", "of", "a", "an", "in", "on", "width", "length", "breadth",
         "this", "that", "plan", "floor", "drawing", "sheet", "room", "space", "metres", "meters", "m", "feet", "ft",
         "its", "does", "do", "are", "measure", "measured", "and", "at", "to", "for", "by", "how's", "whats"}
LABEL_MATCH = 0.8  # difflib ratio of a label (letters only) to a run of the question's words: OCR slips 'Main Hail'


def _letters(text: str) -> str:
    return " ".join(re.sub(r"[^a-zà-öø-ÿ]+", " ", T.norm(text)).split())


def room_label(tl: T.TextLayer, question: str) -> T.TextItem | None:
    """The one label the question names: the item whose letters best match a run of the question's words (OCR slips
    allowed: 'Sport Ha!!' = 'sports hall'), weighted by the run's length so 'Sports Hall' beats a bare 'Hall'; None
    when two different items tie or nothing matches. Unlike measure.find_label it never maps a room word to another
    name ('sports hall' -> 'Entrance' via a concept)."""
    words = _letters(question).split()
    runs = [" ".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, min(len(words), i + 4) + 1)
            if not all(w in _STOP for w in words[i:j])]
    best: list[tuple[float, T.TextItem]] = []
    for it in tl.items:
        k = _letters(it.text)
        if len(k.replace(" ", "")) < 3 or all(w in _STOP for w in k.split()):
            continue
        sc = 0.0
        for r in runs:
            if abs(len(r) - len(k)) > max(3, 0.35 * len(r)):
                continue
            ratio = difflib.SequenceMatcher(None, k, r).ratio()
            if ratio >= LABEL_MATCH:
                sc = max(sc, ratio * len(r))
        if sc:
            best.append((sc, it))
    if not best:
        return None
    best.sort(key=lambda t: -t[0])
    if len(best) > 1 and best[1][0] >= 0.95 * best[0][0]:
        return None  # two labels of that name (or two equally good readings): which room is meant is not known
    return best[0][1]


def asked_side(question: str) -> str | None:
    """'long' / 'wide' / None (both or neither: 'how long and wide', 'how deep', 'dimensions')."""
    lo, wi = bool(_LONG_Q.search(question)), bool(_WIDE_Q.search(question))
    return "long" if lo and not wi else "wide" if wi and not lo else None


def text_axis(item: T.TextItem) -> str | None:
    """'x' for a horizontal string, 'y' for one turned 90°, None when the box is too square to tell."""
    w, h = item.box[2] - item.box[0], item.box[3] - item.box[1]
    if w >= ORIENT * h:
        return "x"
    if h >= ORIENT * w:
        return "y"
    return None


def wrong_side(question: str, value, unit, cited: list, tl: T.TextLayer, img_path: str, px_per_m: float | None,
               len_m: dict | None = None) -> str | None:
    got = check(question, value, unit, cited, tl, img_path, px_per_m, len_m)
    return got["note"] if got else None


def check(question: str, value, unit, cited: list, tl: T.TextLayer, img_path: str, px_per_m: float | None,
          len_m: dict | None = None) -> dict | None:
    """-> {"note", "estimate" (the asked side = value x drawn asked / drawn along, in the answer's unit), "line"
    ([[x, y], [x, y]] image px across the room along the asked side), "box"} or None (no claim)."""
    if not GUARD or not cited or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    side = asked_side(question)
    if side is None or _NOT_ROOM_Q.search(question):
        return None
    dims = [i for i in cited if _DIM_TXT.fullmatch(i.text)]
    if len(dims) != 1:  # one printed dimension (several cited lines = a sum / a chain: not this check)
        return None
    d = dims[0]
    axis = text_axis(d)
    if axis is None:
        return None
    lab = room_label(tl, question)
    if lab is None:
        return None
    if not px_per_m:
        return None  # without a scale the fill's door / wall sizes are guesses and the value cannot be checked
    um = (len_m or {}).get(str(unit or "m").strip().lower().rstrip("."), None)
    if not um:
        return None
    r = outline_box(img_path, lab, tl, px_per_m, skip=d)
    if r is None:
        return None
    x0, y0, x1, y1 = r["box"]
    rw, rh = x1 - x0, y1 - y0
    if min(rw, rh) < 4 or max(rw, rh) < ASPECT * min(rw, rh):
        return None
    # the string must lie along the room (its centre within the room's extent on its own axis), else it is not
    # this room's dimension at all and this check says nothing
    c, lo, hi, ext = (d.cx, x0, x1, rw) if axis == "x" else (d.cy, y0, y1, rh)
    if not lo - 0.1 * ext <= c <= hi + 0.1 * ext:
        return None
    long_axis = "x" if rw > rh else "y"
    if (side == "long") == (axis == long_axis):
        return None
    # positive check: the value IS the drawn side the string runs along (within VALUE_ALONG), and is not the asked one
    v = float(value) * um
    along_m, asked_m = (rw if axis == "x" else rh) / px_per_m, (rh if axis == "x" else rw) / px_per_m
    if abs(v / along_m - 1) > VALUE_ALONG or abs(v / asked_m - 1) <= VALUE_OK:
        return None
    dirn = "west-east (horizontal)" if axis == "x" else "north-south (vertical)"
    which = "short" if side == "long" else "long"
    note = (f"the printed '{d.text}' runs {dirn}, along the {lab.text}'s {which} side: its drawn outline measures "
            f"about {along_m:.1f} m that way and {asked_m:.1f} m the other way; the question asks its "
            + ("length (the long side)" if side == "long" else "width (the short side)"))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    line = [[cx, float(y0)], [cx, float(y1)]] if axis == "x" else [[float(x0), cy], [float(x1), cy]]
    est = float(value) * (rh / rw if axis == "x" else rw / rh)
    return {"note": note, "estimate": est, "line": line, "box": [float(v) for v in r["box"]], "label": lab.text,
            "text": d.text}

FILLS = ({"door_m": 1.05, "wall_m": 0.1}, {"door_m": 2.0, "wall_m": 0.1}, {})


def outline_box(img_path: str, lab: T.TextItem, tl: T.TextLayer, px_per_m: float, skip=None) -> dict | None:
    """The room around a label for its SHAPE: dash-dot grid lines and furniture cut the plain ink fill into bands (EFA
    scans: the main hall read 10.2 x 6.2 m of its 10.4 x 18.5), and they only ever make a room smaller; a walls-only
    fill (thin lines opened away at 0.1 m) with door gaps closed at 1.05 / 2 m finds the walls. The largest region
    of the walls-only variants is taken (the plain fill only when neither closes)."""
    others = [i.box for i in tl.items if i is not lab and i is not skip]
    best = None
    for kw in FILLS:
        if best is not None and not kw.get("wall_m"):
            break  # the plain ink fill only when no walls-only variant closed (it leaks through door gaps)
        fill = {**M.FILL, **kw}
        wall = fill.pop("wall_m", 0.0)
        try:
            r = M.room_area(img_path, lab.box, px_per_m, **fill, wall_m=wall, others=others)
        except Exception:  # noqa: BLE001 - a check, never a reason to fail the question
            r = None
        if r is not None and r["m2"] <= MAX_M2 and (best is None or r["m2"] > best["m2"]):
            best = r
    return best
