"""`line_rooms`: a named room's area / length / width on a LINE-ONLY CAD sheet, traced around its printed label (G1).

X1's wild exam (14:31, captain 14:39 'line-only CAD sheets first'): ordinary AutoCAD / Revit PDFs draw rooms with lines
only (no room fills, no printed areas, no CAD twin) but print their scale ('1:100') and the room names. pdf_vector
('no room polygons'), area_tags (no tags) and plan_rooms (no fills) abstain and the prior answered 55.72 m² @0.01.

E1's stoa.evidence.linework.measure_room(pdf, page, label point, 1:N) traces the free region around the label from the
page's own linework (door openings closed at a scale-aware gap, region must not hold another room's label, several
readings must agree) and measures it at the printed scale. This solver only routes to it:
- the room = the ONE text line whose words (≥ 3 letters, not 'room') all occur in the question ('How big is the comms
  room?' -> 'COMMS ROOM'; 'the conference room' -> 'CONFERENCE' over 'ROOM'); several equal labels -> abstain
  (which one?); none -> abstain;
- the scale = the sheet's most frequent printed 1:N (linework.sheet_scales); none printed -> abstain (plan_rooms /
  the user's 'Set the scale' handle unknown scales);
- area -> the traced area; length / width -> the outline's long / short side; perimeter -> the outline's length.
Approximate by construction: confidence 0.5, exact=False, assumption 'traced from the wall lines at 1:N', the outline
as Evidence.polygon so a human sees at once whether the right room was traced.
Without E1's module (not merged yet) it abstains.
"""

from __future__ import annotations

import re

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

_GENERIC = {"room", "rooms", "raum", "zimmer", "the", "and", "with", "area", "floor", "plan", "level", "space", "huone",
            "tila", "rum", "ruimte", "salle", "piece", "stanza", "sala"}
_AREA = re.compile(r"\barea\b|\bhow (?:big|large)\b|\bsize\b|\bsquare\b|m²|\bm2\b|fl[aä]che|pinta-ala|\bwie gro", re.I)
_NOT = re.compile(r"\b(total|all|sum|combined|together|every|each|average|mean|apartment|unit|flat|wohnung|how many|"
                  r"number of|doors?|windows?|stairs?|walls? (?:thickness|height)|thick|height|tall|volume)\b", re.I)


def _words(s: str) -> list[str]:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"[a-z]{3,}", s)


_SKIRT = re.compile(r"(?i)\b(?:skirting(?:[\s-]*boards?)?|base ?boards?|base (?:trim|moulding|molding)|sockelleiste\w*|"
                    r"jalkalist\w*|plinthes?)\b")
_NO_DEDUCT = re.compile(r"(?i)\b(?:ignor\w*|disregard\w*|not deduct\w*|do(?:es)? not (?:deduct|subtract|remove|take off)|"
                        r"don't (?:deduct|subtract)|without deducting|including (?:the )?(?:door )?openings?|"
                        r"(?:door )?openings? (?:included|ignored|not deducted))\b[^,.;?]*")
_SKIRT_ROOM = re.compile(r"(?i)\b(?:how many (?:linear |running |lineal )?(?:metres|meters|m)\s+of|what length of|"
                         r"how much|would run around|go(?:es)? around the walls of|does|need|needs|required|is needed|"
                         r"in total|if door openings?|\(|\))")


# a habitable room traced below this is a mis-trace (S0 d19); bathrooms / WCs / stores / closets can be smaller
MIN_HABITABLE_M2 = 5.0
_HABITABLE = re.compile(r"(?i)\b(living|lounge|family|bed ?room|bedrm|master|dining|office|study|great room|"
                        r"wohn|schlaf|essz|arbeitsz|olohuone|makuuh|salon|s[ée]jour|chambre|soggiorno|"
                        r"camera|woonkamer|slaapkamer|vardagsrum|sovrum)")
_ONE_OF = re.compile(r"\bone of the\b|\beach of the\b|\beach\b|\bany of the\b|\beine[rs]? der\b|\bjede[rs]?\b", re.I)


def _label(question: str, lines) -> tuple[tuple | None, str]:
    """The text line naming the asked room, or (None, why)."""
    qw = set(_words(question))
    qn = set(re.findall(r"(?<![\d.])\d{1,3}(?![\d.])", question))
    best, ties = None, []
    for text, bb in lines:
        ws = [w for w in _words(text) if w not in _GENERIC]
        if not ws or len(text.split()) > 4 or not all(w in qw for w in ws):
            continue
        tn = set(re.findall(r"(?<![\d.])\d{1,3}(?![\d.])", text))
        if tn and qn and not (tn & qn):
            continue  # 'English 2' is not the asked 'English 9' (D1 skirting probe: ties between numbered rooms)
        score = len(ws) + (0.5 if tn & qn else 0.0)
        if best is None or score > best[0]:
            best, ties = (score, text, bb), []
        elif score == best[0]:
            ties.append((text, bb))
    if best is None:
        return None, "no printed label names the room in the question"
    same = [t for t in ties if t[0].strip().lower() == best[1].strip().lower()]
    if same and _ONE_OF.search(question):
        return ((best[1], best[2]), *same), "same"  # 'one of the quiet rooms': every one of them, checked alike
    if same:
        return None, f"{len(same) + 1} labels '{best[1]}' on the sheet: which one is meant?"
    if ties:
        return None, f"several labels fit the question ('{best[1]}', '{ties[0][0]}')"
    return (best[1], best[2]), ""


_ISO_LONG_MM = {"A0": 1189.0, "A1": 841.0, "A2": 594.0, "A3": 420.0, "A4": 297.0}
_AT_PAPER = re.compile(r"1\s*:\s*(\d{2,4})\s*(?:@|at|auf|på|paperilla)?\s*\(?\s*(A[0-4])\b", re.I)


def paper_rescale(lines, page_long_mm: float, n: float) -> tuple[float, str | None]:
    """D1 16:45 / 17:23 (EFA sheets): '1:200 at A1' printed on an A3 PDF page means 1:400 on the file (areas 4x too small
    at 1:200). -> (N on this page, note) when the sheet names the paper its scale is for and the page is another ISO size."""
    for t, _ in lines:
        m = _AT_PAPER.search(t)
        if not m or float(m.group(1)) != float(n):
            continue
        names = list(_ISO_LONG_MM)
        printed = names.index(m.group(2).upper())
        page = next((i for i, v in enumerate(_ISO_LONG_MM.values()) if abs(page_long_mm - v) / v < 0.03), None)
        if page is None or page == printed:
            return n, None
        n2 = round(n * 2 ** ((page - printed) / 2), 3)  # one ISO size step = sqrt(2) linear
        return n2, (f"the scale 1:{n:g} is printed for {names[printed]} paper; this page is {names[page]}, so the page "
                    f"scale is 1:{n2:g}")
    return n, None


_DIM_NUM = re.compile(r"^\s*(\d{3,5})\s*$")


def printed_sides(lines, poly, m_per_pt: float) -> dict:
    """X1 17:01 (USACE A-101F toilet: traced 2.0 x 1.747 m = 2.72 m², printed '2054' / '1900' at its walls, gold 3.903):
    dimension numbers printed at the traced outline, read along an axis (horizontal text = x extent, vertical text = y
    extent), each within 25 % of the traced side on that axis -> {'x': (m, text, box), 'y': …}. mm first, then cm."""
    xs, ys = [x for x, _ in poly], [y for _, y in poly]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    side = {"x": (x1 - x0) * m_per_pt, "y": (y1 - y0) * m_per_pt}
    pad = max(0.8 / m_per_pt, 0.3 * max(x1 - x0, y1 - y0))  # dimension lines run outside the wall faces
    best: dict = {}
    for t, bb in lines:
        m = _DIM_NUM.match(t)
        if not m:
            continue
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        if not (x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad):
            continue
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        axis = "x" if w > 1.3 * h else "y" if h > 1.3 * w else None
        if axis is None or side[axis] <= 0:
            continue
        for div in (1000.0, 100.0):
            v = int(m.group(1)) / div
            err = abs(v - side[axis]) / side[axis]
            if err <= 0.25:
                if axis not in best or err < best[axis][3]:
                    best[axis] = (v, t, list(bb), err)
                break
    return best


@register("line_rooms")
class LineRooms(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith(".pdf")

    def solve(self, path: str, question: str) -> Answer:
        try:
            from stoa.evidence import linework as L
        except ImportError:
            return self.abstain("the linework tracer is not available")
        from stoa.data import abspath
        from stoa.router.lengths import kind as length_kind

        q = question
        skirt_note = None
        if _SKIRT.search(q):
            # D1 17:52 (EFA skirting probe: 13 of 29 at the prior): skirting with the door openings NOT deducted is the
            # room's perimeter; with them deducted it is lineplan's job (it finds the openings)
            if not _NO_DEDUCT.search(q):
                return self.abstain("skirting minus the door openings: needs the openings (lineplan)")
            skirt_note = "skirting = the room's whole perimeter (the question says the door openings are not deducted)"
            q = "What is the perimeter of " + _SKIRT_ROOM.sub(" ", _SKIRT.sub(" ", _NO_DEDUCT.sub(" ", q)))
        k = length_kind(re.sub(r"\bwall[- ]to[- ]wall\b", " ", q, flags=re.I))
        task = "area" if (_AREA.search(q) and k != "perimeter") else k
        if task not in ("area", "room_len", "room_width", "perimeter"):
            return self.abstain("not a single room's area / length / width / perimeter")
        if _NOT.search(q):
            return self.abstain("totals, counts or other objects: not one traced room")
        if re.search(r"\b(?:feet|foot|ft|sq\.? ?ft|sf)\b", q, re.I):
            return self.abstain("asks in feet")
        p = str(abspath(path))
        try:
            scales = L.sheet_scales(p, 0)
            lines = L.text_lines(p, 0)
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the PDF ({type(e).__name__})")
        if not scales:
            return self.abstain("no printed 1:N scale on the sheet")
        if len(lines) > 1500:
            return self.abstain(f"{len(lines)} text lines: a dense sheet (E1: tracing unreliable there)")
        lab, why = _label(q, lines)
        if lab is None:
            return self.abstain(why)
        n = float(scales[0])
        paper_note = None
        try:
            import pymupdf

            with pymupdf.open(p) as doc:
                r0 = doc[0].rect
            n, paper_note = paper_rescale(lines, max(r0.width, r0.height) * 25.4 / 72, n)
        except Exception:  # noqa: BLE001
            pass
        labs = lab if why == "same" else (lab,)
        reads = []
        for text, bb in labs[:4]:
            c = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
            try:
                r = L.measure_room(p, 0, c, n, tag_box=tuple(bb))
            except Exception as e:  # noqa: BLE001
                return self.abstain(f"tracing failed ({type(e).__name__}: {e})")
            if not r:
                return self.abstain(f"no closed outline around '{text}' at 1:{n:g} (runs off the page or into another "
                                    "room)")
            reads.append((text, bb, r))
        if len(reads) > 1:
            ar = [x[2]["area_m2"] for x in reads]
            if max(ar) > 1.05 * min(ar):
                return self.abstain(f"the {len(reads)} rooms labelled '{reads[0][0]}' differ "
                                    f"({', '.join(f'{a:g}' for a in ar)} m²): which one is meant?")
        text, bb, r = reads[0]
        poly = r["polygon"]
        sides = r.get("sides_m") or []
        if re.search(_HABITABLE, text) and float(r["area_m2"]) < MIN_HABITABLE_M2:
            # S0 d19 (2 wild-sealed losses): on a US sheet the trace closed a < 3 m² box inside a living room @0.40, which
            # stopped the cascade before the page was read as an image (raster was right). A habitable room that small
            # is a mis-trace, not a room
            return self.abstain(f"'{text}' traced to only {r['area_m2']} m² (< {MIN_HABITABLE_M2:g} m² for a habitable "
                                "room): the outline closed inside the room")
        trace = [f"line_rooms: '{text}' traced from the wall lines at 1:{n:g} (printed): {r['area_m2']} m², sides "
                 f"{' x '.join(str(s) for s in sides)} m ({r.get('support')}/{r.get('n_readings')} readings agree, "
                 f"openings closed at {r.get('gap_m')} m)"]
        m_per_pt = n * L.PT_TO_M_AT_1
        dims = printed_sides(lines, poly, m_per_pt) if task in ("area", "room_len", "room_width") else {}
        if len(dims) == 2:
            (vx, tx, bx, _), (vy, ty, by, _) = dims["x"], dims["y"]
            sides = sorted([vx, vy], reverse=True)
            trace.append(f"line_rooms: printed dimensions at the outline '{tx}' x '{ty}' = {vx:g} x {vy:g} m (each within "
                         "25 % of the traced side): used instead of the trace")
        if task == "area" and len(dims) == 2:
            value, unit, what = round(sides[0] * sides[1], 3), "m2", "floor area"
        elif task == "area":
            value, unit, what = round(float(r["area_m2"]), 2), "m2", "floor area"
        elif task == "perimeter":
            from shapely.geometry import Polygon

            m_per_pt = n * L.PT_TO_M_AT_1
            value, unit, what = round(Polygon(poly).length * m_per_pt, 2), "m", "perimeter"
        else:
            if len(sides) < 2:
                return self.abstain("the traced outline has no clear long / short side")
            value = round(float(sides[0] if task == "room_len" else sides[1]), 2)
            unit, what = "m", ("length (longer side)" if task == "room_len" else "width (shorter side)")
        xs, ys = [x for x, _ in poly], [y for _, y in poly]
        ev = [Evidence(file=path, page=0, bbox=[min(xs), min(ys), max(xs), max(ys)], bbox_space="pdf_pt",
                       polygon=[[round(x, 2), round(y, 2)] for x, y in poly], exact=False,
                       note=f"'{text}' outline traced from the wall lines: {r['area_m2']} m² at 1:{n:g}"),
              Evidence(file=path, page=0, bbox=list(bb), bbox_space="pdf_pt", exact=True, note=f"label '{text}'")]
        assumptions = [*([skirt_note] if skirt_note else []),
                       paper_note or f"scale: 1:{n:g} printed on the sheet", f"{what} traced from the drawn wall lines around the "
                       f"label '{text}' (approximate: to the wall faces; door openings closed)"]
        if len(set(scales)) > 1:
            assumptions.append(f"the sheet prints several scales ({', '.join(f'1:{s:g}' for s in scales[:3])}); "
                               f"used the most frequent")
        # traced AREAS ran low on X1's sheets (-2.5, -11 %; Gemini-located rooms -13..+1.5 %): 0.4; sides 0.5
        conf = (0.4 if task == "area" else 0.5) - (0.05 if len(set(scales)) > 1 else 0.0)
        if len(dims) == 2:  # printed dimensions that agree with the trace: the value is read, not measured
            conf = 0.6
            assumptions.append(f"Convention: the room's printed dimensions ({dims['x'][1]} x {dims['y'][1]}) are its "
                               "clear internal sizes; the traced outline only confirms which numbers belong to the room")
            ev += [Evidence(file=path, page=0, bbox=d[2], bbox_space="pdf_pt", exact=True, note=f"printed dimension "
                            f"'{d[1]}' = {d[0]:g} m") for d in (dims["x"], dims["y"])]
        return Answer(value=value, unit=unit, confidence=round(conf, 3), evidence=ev,
                      solver=self.name, trace=trace, assumptions=assumptions,
                      missing=["a printed area or dimension strings for an exact value"])
