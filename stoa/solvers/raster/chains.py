"""Dimension chains: printed segment strings whose sum is a printed overall dimension (O1, cycle 4).

A dimensioned plan carries chains of segment strings along its sides and the overall dimension on the line just
outside them ('2 500 | 4 000 | 1 300 | 2 700 | 3 000' under '13 500'; "12' | 8' | 12'" under "32'"). When the
segments of one line add up to a total printed on the parallel line next to it, the two printed readings check each
other: that total is the drawing's overall dimension on that side, in any language, unit system or scale, and with no
scale bar. This module finds such verified pairs in a text layer (OCR or VLM text spotting, image pixels) and answers
'How long / wide / deep is the building?', 'What overall depth is dimensioned on the left side?' and 'What area do the
two overall dimensions enclose?' from them.

It also fits the drawing's scale from where the segment strings sit: a segment's text is centred on its segment, so
the distance between two neighbouring strings is k * (v_i + v_i+1) / 2 (k = pixels per metre). The fit's spread is
reported; a chain whose spacing contradicts its values is not trusted (conf 0.5 instead of 0.7).

General by construction: nothing here depends on a file, a language's room names or a template; the only facts used
are the printed numbers, their positions and the sum. Kill switch: STOA_O1_CHAINS=0.
"""

from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass, field

from stoa.contract import Answer, Evidence
from stoa.solvers.raster import scalebar as SB

VERIFIED_CONF = 0.7  # a total equal to the sum of its own chain: two printed readings agree
SPACING_BAD_CONF = 0.5  # the sum holds but the strings' spacing does not fit their values
SPREAD_BAD = 0.12  # max residual / chain span above which a chain's spacing contradicts its values
SCALE_AGREE = 0.015  # verified chains must agree on px per m within this to give the drawing a scale (hand-lettered HABS chains spread 2.4-2.8%: not a measuring scale)
AREA_CONF = 0.6  # 'the area the two overall dimensions enclose' = their product (asked literally)
FOOTPRINT_CONF = 0.4  # 'footprint' = width x depth assumes a rectangular outline

_FTIN = re.compile(r"^(\d{1,3})\s*['’′]\s*-?\s*(?:(\d{1,2})(?:[\s·•\-]+(\d)/(\d{1,2}))?\s*[\"”″]?)?$")
_LOOKALIKE = str.maketrans({"I": "1", "l": "1", "|": "1", "O": "0", "o": "0"})  # 'II'-9"' (HABS OCR) = 11'-9"
_THOU = re.compile(r"^\d{1,3}(?: \d{3})+$")  # '13 500' (mm with a thin-space thousands separator)
_DEC = re.compile(r"^\d{1,2}[.,]\d{2,3}$")  # '3,50' / '12.515' (metres)
_INT = re.compile(r"^\d{2,5}$")  # '2500' (mm) / '350' (cm)
_STRIP = "-–—_|.:;=()[]~*· \t"


@dataclass
class Dim:
    item: object  # textlayer.TextItem
    value: float  # feet for 'ftin', metres for 'dec', the printed integer for 'int' / 'thou'
    kind: str  # 'ftin' | 'dec' | 'int' | 'thou'

    @property
    def family(self) -> str:
        return "ft" if self.kind == "ftin" else "metric"


def parse(text: str) -> tuple[float, str] | None:
    """One printed dimension string -> (value, kind), or None. Level marks (+2.80, ±0.00) are not dimensions."""
    if re.search(r"[+±]", text):
        return None
    t = text.strip().strip(_STRIP).strip()
    if re.search(r"['’′]", t):  # a feet-inch string: letters that look like digits are digits ('II'-0"', '9'-O"')
        t = t.translate(_LOOKALIKE)
    m = _FTIN.match(t)
    if m:
        ft = int(m.group(1)) + (int(m.group(2)) / 12 if m.group(2) else 0.0)
        if m.group(3):
            ft += int(m.group(3)) / int(m.group(4)) / 12
        return (round(ft, 4), "ftin") if 0 < ft <= 999 else None
    if _THOU.match(t):
        return float(t.replace(" ", "")), "thou"
    if _DEC.match(t):
        v = float(t.replace(",", "."))
        return (v, "dec") if v > 0 else None
    if _INT.match(t) and not t.startswith("0"):
        return float(t), "int"
    return None


def dims_of(items) -> list[Dim]:
    out = []
    for i in items:
        p = parse(i.text)
        if p is not None:
            out.append(Dim(i, p[0], p[1]))
    return out


def _metres(d: Dim, int_unit: str) -> float:
    """A metric string in metres; integers in the chain's own unit ('mm' or 'cm')."""
    if d.kind == "dec":
        return d.value
    if d.kind == "thou":
        return d.value / 1000
    return d.value / (1000 if int_unit == "mm" else 100)


def _int_unit(ds: list[Dim]) -> str:
    """mm or cm for the integers of one chain + its total. Room-sized segments are 1-6 m: 1000-6000 in mm, 100-600 in
    cm; a thousands separator or a 5-digit total is mm; integers next to decimal metres ('3,50 | 22 | 3,00') are cm."""
    ints = [d for d in ds if d.kind in ("int", "thou")]
    if any(d.kind == "thou" for d in ds) or any(d.value >= 10000 for d in ints):
        return "mm"
    if any(d.kind == "dec" for d in ds):
        return "cm"
    segs = sorted(d.value for d in ints)
    return "mm" if segs and statistics.median(segs) >= 1000 else "cm"


@dataclass
class Pair:
    axis: str  # 'row' (a horizontal chain: the drawing's width) | 'col' (a vertical chain: its depth)
    total: Dim
    run: list[Dim]
    value: float  # metres (metric) or feet
    unit: str  # 'm' | 'ft'
    int_unit: str | None = None
    px_per_unit: float | None = None  # from the strings' spacing (None: < 3 segments)
    spread: float | None = None  # median relative deviation of the spacing fit
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        segs = " + ".join(d.item.text.strip() for d in self.run)
        u = f" ({self.int_unit})" if self.int_unit else ""
        return f"'{self.total.item.text.strip()}' = {segs}{u} = {self.value:g} {self.unit}"


def _size(d: Dim) -> float:
    x0, y0, x1, y1 = d.item.box
    return max(4.0, min(x1 - x0, y1 - y0))


def _lines(ds: list[Dim], axis: str, tol: float) -> list[list[Dim]]:
    key = (lambda d: d.item.cy) if axis == "row" else (lambda d: d.item.cx)
    along = (lambda d: d.item.cx) if axis == "row" else (lambda d: d.item.cy)
    out: list[list[Dim]] = []
    for d in sorted(ds, key=key):
        if out and abs(key(d) - statistics.fmean(key(e) for e in out[-1])) <= tol:
            out[-1].append(d)
        else:
            out.append([d])
    return [sorted(L, key=along) for L in out if len(L) >= 2]


def _spacing(run: list[Dim], vals: list[float], axis: str) -> tuple[float | None, float | None]:
    """Least squares centre_i = a + k * (v_1 + ... + v_i-1 + v_i / 2): k = pixels per unit of vals; spread = the largest
    residual over the chain's pixel span (a segment's string is centred on it; < 3 strings: not fitted)."""
    if len(run) < 3:
        return None, None
    pos = [(d.item.cx if axis == "row" else d.item.cy) for d in run]
    mid, acc = [], 0.0
    for v in vals:
        mid.append(acc + v / 2)
        acc += v
    mm, mp = statistics.fmean(mid), statistics.fmean(pos)
    sv = sum((m - mm) ** 2 for m in mid)
    if sv <= 0:
        return None, 1.0
    k = sum((m - mm) * (x - mp) for m, x in zip(mid, pos)) / sv
    if k <= 0:
        return None, 1.0
    a = mp - k * mm
    span = k * acc
    return k, max(abs(a + k * m - x) for m, x in zip(mid, pos)) / span


def verified(tl) -> list[Pair]:
    """Every (total, consecutive run on a parallel line) whose values add up, both axes."""
    ds = dims_of(tl.items)
    if len(ds) < 3:
        return []
    size = statistics.median(_size(d) for d in ds)
    tol = max(4.0, 0.6 * size)
    reach = max(8 * size, 0.02 * max(tl.size))
    pairs: list[Pair] = []
    for axis in ("row", "col"):
        key = (lambda d: d.item.cy) if axis == "row" else (lambda d: d.item.cx)
        along = (lambda d: d.item.cx) if axis == "row" else (lambda d: d.item.cy)
        for L in _lines(ds, axis, tol):
            line_key = statistics.fmean(key(d) for d in L)
            for i in range(len(L)):
                for j in range(i + 2, min(len(L), i + 14) + 1):
                    run = L[i:j]
                    fam = {d.family for d in run}
                    if len(fam) != 1:
                        continue
                    fam = fam.pop()
                    lo, hi = along(run[0]) - tol, along(run[-1]) + tol
                    for T in ds:
                        if T in L or T.family != fam or not (tol < abs(key(T) - line_key) <= reach) or \
                                not (lo <= along(T) <= hi):
                            continue
                        if fam == "ft":
                            vals, tv, unit, iu = [d.value for d in run], T.value, "ft", None
                            ok = abs(sum(vals) - tv) <= 1 / 24 + 1e-9  # half an inch
                        else:
                            iu = _int_unit(run + [T])
                            vals, tv, unit = [_metres(d, iu) for d in run], _metres(T, iu), "m"
                            ok = abs(sum(vals) - tv) <= max(0.003 * tv, 0.005)
                        if not ok or tv <= max(vals) or not (1.5 <= (tv * 0.3048 if fam == "ft" else tv) <= 400):
                            continue
                        if not any(d.kind in ("int", "thou") for d in run + [T]):
                            iu = None
                        k, spread = _spacing(run, vals, axis)
                        pairs.append(Pair(axis, T, run, round(tv, 4), unit, iu, k, spread))
    return pairs


def best(pairs: list[Pair], axis: str) -> Pair | None:
    """The overall dimension on one axis = the largest verified total (inner sub-totals are smaller); ties -> the
    longer chain."""
    c = [p for p in pairs if p.axis == axis]
    return max(c, key=lambda p: (p.value * (0.3048 if p.unit == "ft" else 1), len(p.run)), default=None)


@dataclass
class ChainBar(SB.Bar):
    """The drawing's scale from its verified dimension chains: where the segment strings sit (pixels) against what
    they say (metres). Used like a graphic scale bar by the room readers when the sheet has no bar and no usable
    printed ratio. Trusted only when >= 2 verified chains agree within SCALE_AGREE (a photo's perspective or a chain
    drawn 'not to scale' makes them disagree), or one chain of >= 4 segments fits tightly."""

    pairs: list = field(default_factory=list)

    def describe(self) -> str:
        return (f"scale from {len(self.pairs)} printed dimension chain(s) (segment strings vs their values): 1 m = "
                f"{self.px_per_unit:.2f} px (chains agree within {100 * self.residual:.1f}%)")

    def evidence(self, path: str) -> list[Evidence]:
        p = self.pairs[0]
        return [Evidence(file=path, page=0, bbox=[round(c, 1) for c in p.total.item.box], bbox_space="image_px",
                         note=f"dimension chain {p.describe()}: the strings' spacing gives 1 m = "
                              f"{self.px_per_unit:.2f} px")]


def chain_bar(tl) -> ChainBar | None:
    if os.environ.get("STOA_O1_CHAINS", "1") == "0":
        return None
    pairs = [p for p in verified(tl) if p.px_per_unit and p.spread is not None and p.spread <= 0.05]
    seen, uniq = set(), []
    for p in sorted(pairs, key=lambda p: -len(p.run)):  # one chain may verify against two totals: count it once
        k = tuple(id(d) for d in p.run)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    ppm = [(p.px_per_unit / (0.3048 if p.unit == "ft" else 1.0), p) for p in uniq]
    if not ppm:
        return None
    med = statistics.median(v for v, _ in ppm)
    agree = [(v, p) for v, p in ppm if abs(v / med - 1) <= SCALE_AGREE]
    if len(agree) < 2 and not (len(agree) == 1 and len(agree[0][1].run) >= 4 and agree[0][1].spread <= 0.02):
        return None
    if len(agree) < len(ppm) / 2:
        return None  # most chains disagree with each other: no single scale
    v = statistics.fmean(v for v, _ in agree)
    dev = max(abs(x / v - 1) for x, _ in agree)
    box = list(agree[0][1].total.item.box)
    return ChainBar(unit="m", px_per_unit=v, labels=[(0.0, 0.0, box), (1.0, v, box)], box=box, residual=dev,
                    source="chains", unit_text="dimension chains", pairs=[p for _, p in agree])


# ---------------------------------------------------------------------------------------------------------- questions
_OVERALL = re.compile(r"\b(overall|total|whole|entire|outside|outer|exterior|external|gesamt\w*|au(?:ß|ss)en\w*|"
                      r"insgesamt|kokonais\w*|ulko\w*|hors[- ]tout|totale?s?|complessiv\w*|estern[oa]|exteriores?|"
                      r"hela|yttre|yttermått)\b", re.I)
_BUILDING = re.compile(r"\b(building|house|home|bungalow|cottage|villa|footprint|gebäude\w*|haus|hauses|talo\w*|"
                       r"rakennu\w*|maison|bâtiment|edificio|casa|fabbricato|byggnad\w*|huset)\b", re.I)
_DIMWORD = re.compile(r"\b(dimension(?:ed|s)?|dimension strings?|ma(?:ß|ss)kett\w*|bema(?:ß|ss)ung\w*|mitta\w*|"
                      r"cotes?|cotat\w*|quot[ae]\w*|mått)\b", re.I)
_DEPTH = re.compile(r"\b(deep|depth|front to back|tief\w*|syvä\w*|syvyys|profond\w*|profondità|djup\w*|fondo)\b", re.I)
_WIDTH = re.compile(r"\b(wide|width|breit\w*|levey\w*|leveä\w*|largeur|large|largh\w*|ancho|anchura|bredd|bred)\b", re.I)
_LONG = re.compile(r"\b(long|length|lang|länge|pituus|pitkä|longueur|lunghezza|lungo|largo|längd|lång)\b", re.I)
_AREA = re.compile(r"\b(area|enclose[sd]?|footprint|square (?:feet|foot|met(?:re|er)s?)|m²|m2|ft²|sq\.? ?ft|"
                   r"fläche|grundfläche|pinta-ala|superficie|surface|yta)\b", re.I)
_NOT_OVERALL = re.compile(r"\b(tall|high|height|hoch|höhe|korkeus|hauteur|altezza|thick\w*|dick\w*|stärke|paksu\w*|"
                          r"épaisseur|spessore|rooms?|zimmer|raum|räume|huone\w*|kitchen|küche|bath\w*|bedroom\w*|"
                          r"porch\w*|veranda|terrace|terrasse|garage|carport|stair\w*|treppe\w*|corridor|hallway|flur|"
                          r"lobby|doors?|t[üu]r\w*|windows?|fenster|walls?|wand|wände|seinä\w*|riser\w*|steps?|"
                          r"plot|site|lot|grundstück|tontti|parcel|pip\w*|duct\w*|cabl\w*|beams?|columns?|slabs?|railings?|fences?|"
                          r"units?|apartments?|flats?|wohnung\w*|asun\w*|levels?|stor(?:e)?y|stories|floors?\s+\d)\b", re.I)
# the pipeline normalises 'the house' to 'this floor plan' ("How deep is this floor plan from front to back?")
_PLAN_SUBJECT = re.compile(r"\b(?:how\s+(?:long|wide|deep)|(?:length|width|depth))\s+(?:is|of)\s+(?:this|the|that)\s+"
                           r"(?:floor\s*)?plan\b", re.I)
_COL_SIDE = re.compile(r"\b(left|right|links|rechts|vasem\w*|oike\w*|gauche|droite|sinistr\w*|destr\w*|"
                       r"izquierd\w*|derech\w*|vänster|höger)\b", re.I)
_ROW_SIDE = re.compile(r"\b(top|bottom|upper|lower|oben|unten|ylä\w*|ala\w*|haut|bas|sopra|sotto)\b", re.I)
# compass directions on a north-up plan (X1 Stoa-like AFCEC: 'How long is the building east to west, overall?')
_EW = re.compile(r"\b(east(?:\s*(?:to|-|–)\s*|\s+and\s+)west|west(?:\s*(?:to|-|–)\s*|\s+and\s+)east|left to right|"
                 r"horizontal\w*|ost-west|itä-länsi|est-ouest)\b", re.I)
_NS = re.compile(r"\b(north(?:\s*(?:to|-|–)\s*|\s+and\s+)south|south(?:\s*(?:to|-|–)\s*|\s+and\s+)north|"
                 r"top to bottom|vertical\w*|nord-süd|pohjois-etelä|nord-sud)\b", re.I)
_PAREN = re.compile(r"\([^()]*\)")
_NEG = re.compile(r"\b(without|excluding|except|not counting|ohne|ilman|sans|senza|sin|utan)\b[^,.;?]*", re.I)
_SQM = re.compile(r"square met|m²|m2\b|quadratmeter|neliö", re.I)


def question_kind(question: str) -> tuple[str, str | None] | None:
    """(kind, axis) for an overall-dimension question: kind 'width' | 'depth' | 'length' | 'area'; axis 'row' | 'col'
    when the question names a side. None for anything else (rooms, walls, heights, elements)."""
    q = _NEG.sub(" ", _PAREN.sub(" ", question))
    if _NOT_OVERALL.search(q):
        return None
    overall = bool(_OVERALL.search(q) and (_BUILDING.search(q) or _DIMWORD.search(q) or re.search(r"\bplan\b", q, re.I)))
    building = bool(_BUILDING.search(q) or _PLAN_SUBJECT.search(q))
    if not (overall or building):
        return None
    axis = "row" if _EW.search(q) else "col" if _NS.search(q) else \
        "col" if _COL_SIDE.search(q) else "row" if _ROW_SIDE.search(q) else None
    if _AREA.search(q):
        literal = bool(_DIMWORD.search(q) and re.search(r"\benclose|\bspan|\bwithin|\bmultipl|\btimes\b|×", q, re.I))
        foot = bool(re.search(r"footprint|grundfläche|bebaute|emprise|impronta|pohja-ala", q, re.I))
        return ("area", "literal" if literal else "footprint") if (literal or foot) else None
    if _DEPTH.search(q):
        return "depth", axis or "col"
    if _WIDTH.search(q):
        return "width", axis or "row"
    if _LONG.search(q):
        return "length", axis
    return None


def _larger_unverified(tl, p: Pair, any_axis: bool = False) -> Dim | None:
    """A printed dimension of the same unit system 2% to 3x larger than the chosen total (then the total may be a part).
    any_axis: 'how long is the building' = its LONGEST extent, so a larger string in either direction is a rival."""
    lo, hi = p.value * 1.02, p.value * 3
    for d in dims_of(tl.items):
        if d.family != ("ft" if p.unit == "ft" else "metric") or d is p.total:
            continue
        w, h = d.item.box[2] - d.item.box[0], d.item.box[3] - d.item.box[1]
        if not any_axis and not (w > 1.5 * h if p.axis == "row" else h > 1.5 * w):
            continue  # only strings written along the same axis (a building's length is not a rival to its depth)
        v = d.value if d.family == "ft" else _metres(d, p.int_unit or _int_unit([d]))
        if lo < v <= hi:
            return d
    return None


def _ev(img_path: str, p: Pair, page: int = 0) -> list[Evidence]:
    ev = [Evidence(file=img_path, page=page, bbox=[round(x, 1) for x in p.total.item.box], bbox_space="image_px",
                   note=f"overall dimension '{p.total.item.text.strip()}' = {p.value:g} {p.unit} (= the sum of its "
                        f"chain)")]
    ev += [Evidence(file=img_path, page=page, bbox=[round(x, 1) for x in d.item.box], bbox_space="image_px",
                    note=f"chain segment '{d.item.text.strip()}'") for d in p.run[:12]]
    return ev


def _conf(p: Pair, base: float) -> tuple[float, list[str]]:
    if p.spread is not None and p.spread > SPREAD_BAD:
        return min(base, SPACING_BAD_CONF), [f"the printed segments' spacing on the drawing does not fit their values "
                                             f"(off by up to {100 * p.spread:.0f}% of the chain): it may be read out of order"]
    return base, []


def _scale_note(p: Pair) -> str | None:
    if p.px_per_unit is None:
        return None
    per = "ft" if p.unit == "ft" else "m"
    return (f"the segments' spacing fits {p.px_per_unit:.1f} px per {per} on the image "
            f"(largest deviation {100 * (p.spread or 0):.1f}% of the chain)")


def overall_answer(img_path: str, question: str, tl, solver: str = "raster") -> Answer | None:
    if os.environ.get("STOA_O1_CHAINS", "1") == "0":
        return None
    qk = question_kind(question)
    if qk is None:
        return None
    kind, axis = qk
    pairs = verified(tl)
    if not pairs:
        return None
    row, col = best(pairs, "row"), best(pairs, "col")
    if kind == "area":
        if row is None or col is None or row.unit != col.unit:
            return None
        if _larger_unverified(tl, row) is not None and _larger_unverified(tl, col) is not None:
            return None  # both sides may be parts of a larger outline
        val = row.value * col.value
        unit = "ft2" if row.unit == "ft" else "m2"
        trace = [f"overall dimensions: width {row.describe()}; depth {col.describe()}; product {val:.3f} {unit}",
                 "Convention: area of the overall dimensions = width × depth as printed (a rectangle; recesses not "
                 "subtracted)" + (f"; = {val * 0.09290304:.2f} m²" if unit == "ft2" else "")]
        if unit == "ft2" and _SQM.search(question):
            val, unit = val * 0.09290304, "m2"
            trace.append(f"converted to {val:.3f} m² (1 ft² = 0.0929 m²)")
        base = AREA_CONF if axis == "literal" else FOOTPRINT_CONF
        c1, m1 = _conf(row, base)
        c2, m2 = _conf(col, base)
        asm = [f"area = overall width × overall depth ({row.value:g} × {col.value:g} {row.unit}), each the printed "
               "total that equals the sum of its own dimension chain"]
        if axis != "literal":
            asm.append("the outline is taken as a rectangle (recesses, porches and projections are not subtracted)")
        miss = m1 + m2 + ([] if axis == "literal" else ["the drawn outline (to subtract recesses or add projections)"])
        return Answer(value=round(val, 3), unit=unit, confidence=min(c1, c2), solver=solver, trace=trace,
                      evidence=_ev(img_path, row) + _ev(img_path, col), assumptions=asm, missing=miss)
    if kind == "length" and axis is None:
        cands = [p for p in (row, col) if p is not None]
        p = max(cands, key=lambda p: p.value * (0.3048 if p.unit == "ft" else 1)) if cands else None
        side = "the longer of the two overall dimensions"
    else:
        p = row if axis == "row" else col
        side = ("the horizontal overall dimension (top/bottom chain)" if axis == "row" else
                "the vertical overall dimension (left/right chain)")
    if p is None:
        return None
    big = _larger_unverified(tl, p, any_axis=kind == "length" and axis is None)
    if big is not None:
        # X1 Stoa-like AFCEC plan: the largest VERIFIED total (286') was a wing, the overall 323'-4" did not verify:
        # 286 ft @0.70. A larger printed dimension of the same unit system means this total may not be the overall
        return None
    conf, miss = _conf(p, VERIFIED_CONF)
    asm = [f"{kind} = {side}: the printed total {p.describe()}", "the overall dimension is the largest printed total "
           "that equals the sum of the chain beside it"]
    if p.int_unit:
        asm.append(f"integer dimension strings read as {p.int_unit} (segment sizes {'1000-6000' if p.int_unit == 'mm' else '100-600'} "
                   "for rooms)")
    sn = _scale_note(p)
    metric = f" = {p.value * 0.3048:.3f} m" if p.unit == "ft" else ""
    trace = [f"overall dimension ({kind}, {p.axis}): {p.describe()}" + (f"; {sn}" if sn else ""),
             f"verified chain totals found: {len(pairs)}",
             f"Convention: overall dimension = the outermost printed total along that side (as dimensioned on the "
             f"drawing); units as printed: {p.value:g} {p.unit}{metric}"]
    if metric:
        asm.append(f"{p.value:g} ft{metric}")
    return Answer(value=round(p.value, 3), unit=p.unit, confidence=conf, solver=solver, trace=trace,
                  evidence=_ev(img_path, p), assumptions=asm, missing=miss)
