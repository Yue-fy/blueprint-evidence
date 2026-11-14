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


_ONE_OF = re.compile(r"\bone of the\b|\beach of the\b|\beach\b|\bany of the\b|\beine[rs]? der\b|\bjede[rs]?\b", re.I)


def _label(question: str, lines) -> tuple[tuple | None, str]:
    """The text line naming the asked room, or (None, why)."""
    qw = set(_words(question))
    best, ties = None, []
    for text, bb in lines:
        ws = [w for w in _words(text) if w not in _GENERIC]
        if not ws or len(text.split()) > 4 or not all(w in qw for w in ws):
            continue
        score = len(ws)
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
        trace = [f"line_rooms: '{text}' traced from the wall lines at 1:{n:g} (printed): {r['area_m2']} m², sides "
                 f"{' x '.join(str(s) for s in sides)} m ({r.get('support')}/{r.get('n_readings')} readings agree, "
                 f"openings closed at {r.get('gap_m')} m)"]
        if task == "area":
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
        assumptions = [f"scale: 1:{n:g} printed on the sheet", f"{what} traced from the drawn wall lines around the "
                       f"label '{text}' (approximate: to the wall faces; door openings closed)"]
        if len(set(scales)) > 1:
            assumptions.append(f"the sheet prints several scales ({', '.join(f'1:{s:g}' for s in scales[:3])}); "
                               f"used the most frequent")
        # traced AREAS ran low on X1's sheets (-2.5, -11 %; Gemini-located rooms -13..+1.5 %): 0.4; sides 0.5
        conf = (0.4 if task == "area" else 0.5) - (0.05 if len(set(scales)) > 1 else 0.0)
        return Answer(value=value, unit=unit, confidence=round(conf, 3), evidence=ev,
                      solver=self.name, trace=trace, assumptions=assumptions,
                      missing=["a printed area or dimension strings for an exact value"])
