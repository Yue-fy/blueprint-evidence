"""Printed AREA TABLES on a sheet (E1, captain 16:15: 'read printed area tables first'; X1 stoa-like AFCEC dorm A-2).

A sheet may print its own area summary as a table: 'WEST WING DORMITORY = 6,095 SF', 'TOTAL WEST WING = 7,009 SF',
'COMMON SPACE CIRCULATION = 1,240 SF', 'TOTAL SECOND FLOOR = 26,562 SF'. Room-tag readers (area_tags) see only the
values (labels sit to the LEFT on the same row, not stacked above) and answered 'total area of the second floor' with
a sum of unlabelled tags (1,580 m² @0.6 vs the printed 26,562 SF). Here every value is paired with the label on its
row (same text direction, overlapping row band), and a question is answered from the table:

  1. a TOTAL row whose words are all in the question ('total area of the west wing' -> 'TOTAL WEST WING'), when the
     question names no area type, or the TOTAL row names it too ('total common space' -> 'TOTAL COMMON SPACE');
  2. else the rows naming the asked type ('dormitory', 'circulation'), narrowed by compass / wing words in the
     question ('the north wing') — one row is the answer, several are summed ('across all three wings').

Value in the unit asked (SF <-> m²). Evidence = each row's printed label + value box (exact).

    from stoa.evidence.areatable import answer, rows
    answer(pdf_path, question, page=0) -> Answer | None   (None = no area table / no matching row; never raises)
"""

from __future__ import annotations

import functools
import re

from stoa.contract import Answer, Evidence

SOLVER = "area_table"
FT2_M2 = 0.09290304
_VAL = re.compile(r"^(?P<v>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<u>SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|m²|m2|sqm|qm)$", re.I)
_AREA_Q = re.compile(r"\b(area|square (?:feet|foot|met\w*)|sq\.? ?(?:ft|m)|sf|m²|m2|how (?:big|large)|size|footage)\b",
                     re.I)
_SUM_Q = re.compile(r"\b(total|all|across|combined|together|sum|altogether|overall|in total)\b", re.I)
_STOP = {"what", "is", "the", "of", "area", "areas", "how", "big", "large", "in", "square", "feet", "foot", "metres",
         "meters", "metre", "meter", "sf", "ft", "sq", "m2", "m²", "this", "on", "plan", "sheet", "drawing", "a", "an",
         "which", "are", "there", "size", "total", "all", "and", "to", "for", "at", "shown", "do", "does", "have",
         "has", "much", "many", "its", "across", "combined", "together", "sum", "overall", "net", "gross", "printed",
         "according", "table", "be", "by", "with", "per", "that", "it", "subtotal", "footage", "amount", "there's",
         "whats", "what's", "levels", "level", "three", "two", "four", "both", "each", "every", "s"}
_SCOPE = {"floor", "floors", "storey", "story", "level", "building", "wing", "wings", "first", "second", "third",
          "fourth", "ground", "1st", "2nd", "3rd", "4th", "upper", "lower", "basement", "whole", "entire"}
_QUAL = {"north", "south", "east", "west", "northern", "southern", "eastern", "western", "left", "right", "front",
         "rear", "main"}  # narrow table rows to one wing / side; floor ordinals are the sheet's scope (_SCOPE)
_SYN = {"dorm": "dormitory", "dorms": "dormitory", "dormitories": "dormitory", "bedroom": "dormitory",
        "circ": "circulation", "corridor": "circulation", "corridors": "circulation", "hallway": "circulation",
        "hallways": "circulation", "rooms": "room", "spaces": "space", "areas": "area", "1st": "first", "2nd": "second",
        "3rd": "third", "4th": "fourth", "stairs": "stair", "utilities": "utility", "northern": "north",
        "southern": "south", "eastern": "east", "western": "west", "storey": "floor", "story": "floor", "level": "floor"}
_INCL = re.compile(r"\b(?:including|incl\.?|inclusive of|with its|together with|plus its)\b.*$", re.I)


def _norm(w: str) -> str:
    w = w.lower().strip(".,:;()'\"")
    return _SYN.get(w, w)


def _wordset(s: str) -> set[str]:
    return {_norm(w) for w in re.findall(r"[A-Za-z0-9]+(?:'[a-z]+)?", s)} - {""}


@functools.lru_cache(maxsize=32)
def rows(pdf_abs: str, page: int = 0) -> tuple:
    """((label, value, unit 'ft2'|'m2', bbox), ...) for every printed 'LABEL = value SF' row; () when fewer than 3."""
    import pymupdf

    lines = []
    with pymupdf.open(pdf_abs) as d:
        if page >= len(d):
            return ()
        for b in d[page].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    lines.append((t, tuple(ln["bbox"]), tuple(round(x, 2) for x in ln["dir"])))
    out = []
    for t, bb, dr in lines:
        m = _VAL.match(t)
        if not m:
            continue
        horiz = abs(dr[0]) >= abs(dr[1])
        a0, a1 = (bb[1], bb[3]) if horiz else (bb[0], bb[2])  # the row band across the reading direction
        best = None
        for t2, b2, d2 in lines:
            if d2 != dr or b2 == bb or _VAL.match(t2) or not re.search(r"[A-Za-z]{3,}", t2):
                continue
            c0, c1 = (b2[1], b2[3]) if horiz else (b2[0], b2[2])
            ov = min(a1, c1) - max(a0, c0)
            if ov < 0.6 * min(a1 - a0, c1 - c0):
                continue
            gap = (b2[0] - bb[2] if b2[0] >= bb[2] else bb[0] - b2[2]) if horiz else \
                (b2[1] - bb[3] if b2[1] >= bb[3] else bb[1] - b2[3])
            if gap < -2 or gap > 300:
                continue
            if best is None or gap < best[0]:
                best = (gap, t2, b2)
        if best is None:
            continue
        label = best[1].rstrip(" =:").strip()
        v = float(m.group("v").replace(",", ""))
        unit = "m2" if re.match(r"m|sqm|qm", m.group("u"), re.I) else "ft2"
        box = [round(min(bb[0], best[2][0]), 1), round(min(bb[1], best[2][1]), 1),
               round(max(bb[2], best[2][2]), 1), round(max(bb[3], best[2][3]), 1)]
        out.append((label, v, unit, tuple(box), best[1].rstrip().endswith(("=", ":"))))
    # a TABLE: several 'LABEL =' rows (room tags stacked over their values never pair this way)
    if len(out) < 3 or sum(1 for r in out if r[4] or re.search(r"\btotal\b", r[0], re.I)) < 3:
        return ()
    return tuple(r[:4] for r in out)


def _content(label: str) -> set[str]:
    return _wordset(label) - _STOP - {"total", "subtotal"}


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader never breaks the cascade
        return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    from stoa.data import abspath

    q = question
    if not _AREA_Q.search(q) or re.search(r"\b(how many|number of)\b(?!\s+(?:square|sq))", q, re.I):
        return None
    pdf = str(abspath(path))
    if not pdf.lower().endswith(".pdf"):
        return None
    rs = rows(pdf, page)
    if not rs:
        return None
    qw = _wordset(_INCL.sub(" ", q)) - _STOP
    subject = qw - _SCOPE - _QUAL - {"room", "space"} if not ({"common"} & qw) else qw - _SCOPE - _QUAL
    quals = qw & _QUAL
    totals = [r for r in rs if re.search(r"\btotal\b", r[0], re.I) and not re.search(r"\bsubtotal\b", r[0], re.I)]
    pick: list = []
    how = ""
    cands = [r for r in totals if _content(r[0]) and _content(r[0]) <= qw and subject <= _content(r[0])]
    if cands:
        best = max(len(_content(r[0])) for r in cands)
        top = [r for r in cands if len(_content(r[0])) == best]
        if len(top) == 1:
            pick, how = top, f"the printed table row '{top[0][0]}'"
    if not pick and not subject and re.search(r"\b(this|the|whole|entire)\s+(floor|level|storey|story|sheet|plan)\b",
                                              q, re.I):
        # 'the total area of this floor': the one floor-wide TOTAL row ('TOTAL FIRST FLOOR' on the A-1 sibling)
        fl = [r for r in totals if _content(r[0]) & {"floor"} and
              _content(r[0]) <= {"floor", "first", "second", "third", "fourth", "ground", "upper", "lower", "basement"}]
        if len(fl) == 1:
            pick, how = fl, f"the printed table row '{fl[0][0]}' (this sheet's floor)"
    if not pick and subject:
        hits = [r for r in rs if subject <= _content(r[0]) and not re.search(r"\b(sub)?total\b", r[0], re.I)]
        if quals:
            hits = [r for r in hits if quals & _content(r[0]) or not (_QUAL & _content(r[0]))]
            if any(quals & _content(r[0]) for r in hits):
                hits = [r for r in hits if quals & _content(r[0])]
        if len(hits) == 1:
            pick, how = hits, f"the printed table row '{hits[0][0]}'"
        elif len(hits) > 1 and (_SUM_Q.search(q) or len(hits) <= 6):
            if not _SUM_Q.search(q) and not re.search(r"\b(wings|floors|levels|spaces|areas|rooms)\b", q, re.I):
                return None  # 'the circulation area' with four circulation rows and no 'total': ambiguous
            pick = hits
            how = f"sum of {len(hits)} printed table rows naming {', '.join(sorted(subject))}"
    if not pick:
        return None
    units = {r[2] for r in pick}
    if len(units) != 1:
        return None
    src_unit = units.pop()
    total = sum(r[1] for r in pick)
    want_m2 = bool(re.search(r"\b(square met\w*|m²|m2|sq\.? ?m|sqm|quadrat\w*)\b", q, re.I))
    want_ft2 = bool(re.search(r"\b(square f\w*|sq\.? ?ft|sf|ft²|ft2)\b", q, re.I))
    if src_unit == "ft2" and want_m2:
        val, unit = round(total * FT2_M2, 2), "m2"
    elif src_unit == "m2" and want_ft2:
        val, unit = round(total / FT2_M2, 1), "ft2"
    else:
        val, unit = (round(total, 2), src_unit)
    terms = " + ".join(f"{r[1]:,.0f}" if r[1] == int(r[1]) else f"{r[1]:g}" for r in pick)
    su = "SF" if src_unit == "ft2" else "m²"
    ev = [Evidence(file=path, page=page, bbox=list(r[3]), bbox_space="pdf_pt", exact=True,
                   note=f"{r[0][:48]} = {r[1]:,.0f} {su} (printed area table)") for r in pick]
    conf = 0.85 if len(pick) == 1 else 0.75
    assumptions = [f"{how}: {terms} {su}" + (f" = {total:,.0f} {su}" if len(pick) > 1 else ""),
                   "areas as printed in the sheet's own area table (not measured)"]
    if unit != src_unit:
        assumptions.append(f"converted: {total:,.0f} {su} x {FT2_M2 if unit == 'm2' else 1 / FT2_M2:.8g} = {val} "
                           f"{'m²' if unit == 'm2' else 'SF'}")
    return Answer(value=val, unit=unit, confidence=conf, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=[], trace=[f"area_table: {len(rs)} printed 'LABEL = value' rows on the sheet; {how}"])


__all__ = ["answer", "rows"]


def prefer_printed_table(path: str, question: str, ans: Answer) -> Answer:
    """Pipeline hook (E1 block, captain 16:15 'printed area tables first'): on a PDF whose own area table answers the
    question, the table's value is the answer (exact, the rows boxed); an answer that already agrees (±0.5 %) is kept
    with a cross-check line. Fires only on sheets with >= 3 'LABEL = value SF/m²' rows (rows())."""
    try:
        b = answer(path, question)
    except Exception:  # noqa: BLE001
        return ans
    if b is None:
        return ans
    try:
        v = float(ans.value) if ans.value is not None else None
    except (TypeError, ValueError):
        v = None
    if v is not None and str(ans.unit or "").lower() in ("m2", "m²", "ft2") and \
            abs(v * (FT2_M2 if str(ans.unit).lower() == "ft2" else 1) - b.value * (FT2_M2 if b.unit == "ft2" else 1)) \
            <= 0.005 * b.value * (FT2_M2 if b.unit == "ft2" else 1):
        ans.trace = [*ans.trace, f"cross-check: the sheet's printed area table says {b.value:g} {b.unit} ✓"]
        return ans
    b.solver = f"pipeline/{SOLVER}" if str(ans.solver or "").startswith("pipeline/") else SOLVER
    b.cost = b.cost.add(ans.cost) if ans.cost is not None and b.cost is not None else (ans.cost or b.cost)
    b.trace = [*(t for t in ans.trace[:2] if str(t).startswith("recognised:")),
               f"E1 area_table: the sheet prints this area in its own table; replaces {ans.solver}: "
               f"{str(ans.value)[:40]!r} @{ans.confidence}", *b.trace]
    b.recognition = getattr(ans, "recognition", None)
    return b
