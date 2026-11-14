"""Totals of items LISTED in a sheet's notes / schedule text: 'What is the total length of the three trenches listed in
the notes?' -> the note rows naming a trench with a number ('MOTOR CONTROL CENTER TRENCH LENGTH - 4275', 'PUMP CONTROL
PANEL TRENCH LENGTH - 1825', 'PIPE TRENCH LENGTH - 1750 (SEE NOTE 3)') summed: 7850 mm (X1 stoa-like S-100F PNG
sl-052, gold 7.85 m; the VLM said 7050).

OCR splits a long note line into overlapping tile reads ('MOTOR CONTROL CENTER TRENCH LEN' + 'CH LENGTH - 4275' + a
clipped 'ULL LENGTH 4975' 15 px tall): rows are rebuilt from items that share a baseline, clipped re-reads of the same
place as a taller read are dropped, and the row's value is the last number after '-', '=' or ':'.

    listed_sum_answer(img_path, question, tl, solver) -> Answer | None
"""

from __future__ import annotations

import os
import re
import statistics

from stoa.contract import Answer, Evidence

CONF = 0.6  # the rows found = the count the question states
CONF_NO_COUNT = 0.4  # the question states no count
_TOTAL_Q = re.compile(r"\b(total|sum|combined|altogether|together|in all|overall)\b", re.I)
_LISTED_Q = re.compile(r"\b(listed|list|notes?|schedule|table|legend|given|stated|printed)\b", re.I)
_QTY_Q = re.compile(r"\b(length|long|area|volume|weight|width|height|depth|size|metres?|meters?|feet|mm|m2|m²|sf)\b", re.I)
_NUMWORD = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_COUNT = re.compile(r"\b(" + "|".join(_NUMWORD) + r"|\d{1,2})\s+(\w+)", re.I)
_VALUE = re.compile(r"(?:\s[-–]|[=:])\s*(\d{1,3}(?:[ ,]\d{3})+|\d+(?:[.,]\d+)?)\s*(mm|m|cm|m2|m²|sf|ft)?\b(?!.*[-=:–]\s*\d)", re.I)
_STOP = {"what", "is", "the", "of", "a", "an", "in", "on", "are", "listed", "notes", "note", "total", "length", "lengths",
         "sum", "combined", "how", "much", "many", "all", "this", "that", "sheet", "drawing", "plan", "given", "table",
         "schedule", "area", "areas", "volume", "and", "with", "for", "to", "by", "as", "it", "its", "their", "there"}


def _rows(tl) -> list[tuple[str, list]]:
    """Text rows: horizontal items whose centres lie within half a text height of the row's (vertical text skipped),
    clipped reads (much shorter than the row) dropped, merged left to right."""
    items = sorted((i for i in tl.items if (i.box[3] - i.box[1]) <= 2.5 * max(1.0, i.box[2] - i.box[0])),
                   key=lambda i: (i.box[1] + i.box[3]) / 2)
    rows: list[list] = []
    for it in items:
        h, cy = it.box[3] - it.box[1], (it.box[1] + it.box[3]) / 2
        for r in rows[-6:]:
            rh = max(i.box[3] - i.box[1] for i in r)
            rc = statistics.median((i.box[1] + i.box[3]) / 2 for i in r)
            if abs(cy - rc) <= 0.5 * max(h, rh):
                r.append(it)
                break
        else:
            rows.append([it])
    def dup(i, r) -> bool:  # a clipped re-read of the same place as a taller item ('ULL LENGTH 4975' under 'CH LENGTH - 4275')
        hi, wi = i.box[3] - i.box[1], max(1.0, i.box[2] - i.box[0])
        return any(j is not i and (j.box[3] - j.box[1]) > 1.5 * hi
                   and min(i.box[2], j.box[2]) - max(i.box[0], j.box[0]) >= 0.5 * wi for j in r)

    out = []
    for r in rows:
        r = sorted((i for i in r if not dup(i, r)), key=lambda i: i.box[0])
        out.append((" ".join(i.text.strip() for i in r), r))
    return out


def _stems(question: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", question)]
    out = []
    for w in words:
        if w in _STOP or w in _NUMWORD:
            continue
        out.append(re.sub(r"(es|s)$", "", w) if len(w) > 5 else w)
    return out


def listed_sum_answer(img_path: str, question: str, tl, solver: str = "raster") -> Answer | None:
    if os.environ.get("STOA_O1_NOTES", "1") == "0":
        return None
    if not (_TOTAL_Q.search(question) and _LISTED_Q.search(question) and _QTY_Q.search(question)):
        return None
    m = _COUNT.search(question)
    n_q = None
    if m:
        g = m.group(1).lower()
        n_q = int(g) if g.isdigit() else _NUMWORD.get(g)
    noun = m.group(2).lower() if m else None
    stems = [re.sub(r"(es|s)$", "", noun)] if noun and noun not in _STOP else _stems(question)
    if not stems:
        return None
    hits = []
    for text, items in _rows(tl):
        low = text.lower()
        sm = [m2 for st in stems for m2 in re.finditer(rf"\b{re.escape(st)}", low)]
        if not sm or re.match(r"^\s*(?:[a-z]\s+)?(?:notes?:?\s*)?\d+\.\s", low):
            continue  # a numbered note sentence ('1. APPROXIMATE TRENCH LENGTHS ARE …') is not a listed item
        v = _VALUE.search(text)
        if not v or not any(0 <= v.start() - m2.end() <= 40 for m2 in sm):
            continue
        num = v.group(1).replace(" ", "").replace(",", "") if re.match(r"\d{1,3}[ ,]\d{3}", v.group(1)) else \
            v.group(1).replace(",", ".")
        try:
            val = float(num)
        except ValueError:
            continue
        hits.append((val, (v.group(2) or "").lower(), text, items))
    if len(hits) < 2:
        return None
    if n_q is not None and len(hits) != n_q:
        return None  # the rows found do not match the count the question states: another reader
    units = {u for _, u, _, _ in hits if u}
    if len(units) > 1:
        return None
    unit = units.pop() if units else ("mm" if all(v >= 100 and v == int(v) for v, _, _, _ in hits) else None)
    total = sum(v for v, _, _, _ in hits)
    out_v, out_u = (total / 1000, "m") if unit == "mm" else (total, unit)
    parts = " + ".join(f"{v:g}" for v, _, _, _ in hits)
    trace = [f"{len(hits)} listed row(s) naming '{stems[0]}': " + "; ".join(f"'{t[:70]}'" for _, _, t, _ in hits),
             f"total = {parts} = {total:g}{(' ' + unit) if unit else ''}" + (f" = {out_v:g} m" if unit == "mm" else "")]
    if unit == "mm" and not units:
        trace.append("integers >= 100 on a metric structural sheet read as millimetres")
    ev = [Evidence(file=img_path, page=0, bbox=[round(min(i.box[0] for i in it), 1), round(min(i.box[1] for i in it), 1),
                                                round(max(i.box[2] for i in it), 1), round(max(i.box[3] for i in it), 1)],
                   bbox_space="image_px", note=f"listed: '{t[:60]}'") for _, _, t, it in hits]
    return Answer(value=round(out_v, 4), unit=out_u, confidence=CONF if n_q is not None else CONF_NO_COUNT,
                  solver=solver, trace=trace, evidence=ev,
                  assumptions=[f"the total is the sum of the {len(hits)} printed rows that name the item"])
