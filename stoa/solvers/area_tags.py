"""Room AREA TAGS printed on a CAD/Revit sheet ('U123 - 680 SF', 'Clinic' over '5941 SF', 'Bad' over '4.24 m²'), read
deterministically from the PDF text layer (G1; E1 13:52 Stoa-style probe on real Revit sheets uploaded alone).

pdf_text (LLM retrieval over the text) answered single tags right but: 'total area of all units on this floor' ->
251,975 ft² @0.50 (its own second query said 9,076), 'Which room is the largest?' -> 'Locker Room Men' @0.55 (2 of 29
tags retrieved; 'Clinic 5941 SF' exists), 'area of the gym' -> prior 55.72 @0.01 ('Fitness Center 3935 SF').
This reads EVERY tag: value + unit, the room number before ' - ', and the name lines stacked just above the tag; then
answers one room's / unit's area (by number or name words), the largest / smallest, the total and the count, in the
unit asked (ft² <-> m²). Evidence = the tag's printed text box (exact). Abstains below 3 tags (not this kind of sheet).
"""

from __future__ import annotations

import functools
import re

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

FT2_M2 = 0.09290304
_ROOMNO = re.compile(r"^(?:\d{1,2}\.\d{2}|[A-Z]{0,2}\d{2,4}[A-Z]?)$")
_APT_LABEL = re.compile(r"^(?:Whg\.?|Wohnung|App\.?|Appartement|Apt\.?|Unit)\s*[A-Z0-9]{1,3}$", re.I)
_APT_Q = re.compile(r"\b(?:apartment|wohnung|whg\.?|appartement|appartamento|flat|logement)\s+([A-Z0-9]{1,2})\b", re.I)
_TAG = re.compile(r"^(?:(?P<no>[A-Z]{0,3}\d{1,4}(?:\.[A-Z0-9]{1,3})?)\s*[-–:]\s*)?(?P<v>\d{1,3}(?:[,']\d{3})+|\d+(?:[.,]\d+)?)"
                  r"\s*(?P<u>SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|m²|m2|qm|sqm)$", re.I)
_SYN = {"gym": ["fitness", "gym", "weight", "exercise"], "toilet": ["rest", "toilet", "wc", "restroom", "lavatory"],
        "restroom": ["rest", "toilet", "restroom"], "kitchen": ["kitchen", "kitchenette", "pantry", "break"],
        "lobby": ["lobby", "reception", "entry", "vestibule"], "meeting": ["conf", "conference", "meeting"],
        "conference": ["conf", "conference", "meeting"], "storage": ["storage", "stor", "closet", "utility"],
        "bedroom": ["bd", "bedroom", "bed"], "corridor": ["corr", "corridor", "hall"], "hall": ["hall", "corr"],
        "office": ["office", "off"], "unit": ["unit", "u"], "apartment": ["unit", "apt", "bd", "studio"]}
_STOP = {"what", "is", "the", "of", "area", "areas", "how", "big", "large", "in", "square", "feet", "foot", "metres",
         "meters", "m²", "m2", "sf", "ft²", "ft2", "sq", "ft", "room", "rooms", "this", "on", "floor", "plan", "sheet",
         "drawing", "a", "an", "which", "one", "are", "there", "size", "total", "net", "gross", "all", "and", "to",
         "for", "at", "shown", "level", "first", "1st", "ground", "do", "does", "have", "has", "space", "spaces", "s"}


@functools.lru_cache(maxsize=64)
def read_tags(pdf_abspath: str, page: int = 0) -> tuple:
    import pymupdf

    with pymupdf.open(pdf_abspath) as d:
        p = d[page]
        lines = []
        for b in p.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    lines.append((t, tuple(ln["bbox"])))
    out = []
    for t, bb in lines:
        m = _TAG.match(t)
        if not m:
            continue
        v = float(m.group("v").replace(",", "").replace("'", "")) if re.search(r"[,']\d{3}$", m.group("v")) else \
            float(m.group("v").replace(",", "."))
        unit = "m2" if re.match(r"m|qm|sqm", m.group("u"), re.I) else "ft2"
        h = bb[3] - bb[1]
        cx = (bb[0] + bb[2]) / 2
        names, top, box = [], bb[1], list(bb)
        number, apt = m.group("no"), None
        for _ in range(3):  # name lines stacked just above the tag, roughly centred on it
            cand = [(t2, b2) for t2, b2 in lines if -0.4 * h < top - b2[3] <= 0.9 * h + 2 and b2[1] < top - 0.3 * h and
                    abs((b2[0] + b2[2]) / 2 - cx) <= max(0.6 * (bb[2] - bb[0]), 0.6 * (b2[2] - b2[0]), 25)
                    and not _TAG.match(t2)]
            if not cand:
                break
            t2, b2 = max(cand, key=lambda x: (round(x[1][3], 0), -abs((x[1][0] + x[1][2]) / 2 - cx)))  # nearest above
            if _ROOMNO.match(t2.strip()) and not number:
                number = t2.strip()  # Swiss label stack: name / '2.01' / '15.61 m²'
            elif _APT_LABEL.match(t2.strip()):
                apt = t2.strip()  # 'Whg. A' over the first room of an apartment: not part of the room name
            else:
                names.insert(0, t2)
            top = b2[1]
            box = [min(box[0], b2[0]), min(box[1], b2[1]), max(box[2], b2[2]), max(box[3], b2[3])]
        out.append({"number": number, "name": " ".join(names).strip() or None, "value": v, "unit": unit, "apt": apt,
                    "text": t, "bbox": [round(x, 1) for x in box], "tag_bbox": [round(x, 1) for x in bb]})
    return tuple(out)


def _label(t) -> str:
    return " ".join(x for x in (t["name"], t["number"]) if x) or "(unlabelled tag)"


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9.]+", s.lower()) if w not in _STOP and len(w) >= 2}


@register("area_tags")
class AreaTags(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith(".pdf")

    def solve(self, path: str, question: str) -> Answer:
        from stoa.data import abspath

        try:
            tags = list(read_tags(str(abspath(path))))
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the PDF text ({type(e).__name__})")
        if len(tags) < 3:
            return self.abstain(f"{len(tags)} area tags on the sheet (not a tagged plan)")
        try:
            import os

            from stoa.router.recognize import _pdf

            if _pdf(str(abspath(path)), os.stat(abspath(path)).st_mtime)["bim_fill"] >= 2:
                return self.abstain("IfcConvert-style sheet: plan_rooms measures its room fills")
        except Exception:  # noqa: BLE001
            pass
        q = question.lower()
        if not re.search(r"\barea|\bhow (?:big|large)|\bsize|\bsquare|\bsf\b|\bft²|\bm²|\bm2\b|\blargest|\bbiggest|"
                         r"\bsmallest", q):
            return self.abstain("not an area question")
        if re.search(r"\b(?:door|window|wall|stair|column|perimeter|length|width|volume|height)s?\b", q):
            return self.abstain("asks about something other than room areas")
        want = "m2" if re.search(r"m²|\bm2\b|square met|sq\.? ?m\b|metres|meters|\bsqm\b", q) else \
            ("ft2" if re.search(r"\bsf\b|ft²|ft2|square f|sq\.? ?ft", q) else tags[0]["unit"])

        def conv(v, u):
            if u == want:
                return v
            return v * FT2_M2 if want == "m2" else v / FT2_M2

        named = [t for t in tags if t["name"] or t["number"]]
        loose = [t for t in tags if not (t["name"] or t["number"])]
        unit_txt = "m²" if want == "m2" else "ft²"
        trace = [f"area_tags: {len(tags)} area tags on the sheet ({len(named)} with a room name/number, "
                 f"{len(loose)} unlabelled: {', '.join(t['text'] for t in loose[:3])})"]
        conv_note = [] if all(t["unit"] == want for t in tags) else \
            [f"converted from the sheet's {('ft²' if tags[0]['unit'] == 'ft2' else 'm²')} (1 ft² = 0.09290304 m²)"]

        def ev(ts, note=None):
            return [Evidence(file=path, page=0, bbox=t["bbox"], bbox_space="pdf_pt", exact=True,
                             note=note or f"{_label(t)}: {t['text']}") for t in ts[:80]]

        def ans(v, ts, conf, assumptions, missing=None, extra_trace=None):
            return Answer(value=round(v, 2), unit=want, confidence=conf, evidence=ev(ts), solver=self.name,
                          trace=trace + (extra_trace or []), assumptions=assumptions + conv_note,
                          missing=missing or [])

        sup = re.search(r"\b(largest|biggest|smallest)\b", q)
        total = re.search(r"\b(total|sum|all|combined|altogether|overall|gesamt)\b", q)
        count = re.search(r"\bhow many\b|\bnumber of\b", q)
        units_scope = re.search(r"\b(units?|apartments?|dwellings?|flats?)\b", q)
        scope = named
        if units_scope:
            scope = [t for t in named if (t["number"] or "").upper().startswith("U") or
                     re.search(r"\b(?:\dBD|studio|unit|apt)\b", t["name"] or "", re.I)] or named
        def matches(words: set[str], pool) -> list:
            exp = set(words)
            for w in words:
                exp |= set(_SYN.get(w.rstrip("s"), []))

            def score(t):
                tw = _words(t["name"] or "")
                return sum(1 for w in exp if any(x == w or (len(w) >= 4 and x.startswith(w[:4])) for x in tw))
            best = max((score(t) for t in pool), default=0)
            return [t for t in pool if best and score(t) == best]

        name_words = {w for w in _words(question) if not re.fullmatch(r"\d+(?:\.\d+)?", w)} - {
            "largest", "biggest", "smallest", "units", "unit", "apartment", "apartments", "flat", "flats", "big",
            "combined", "sum", "how", "it", "its"}
        if sup and not count:
            if name_words:  # 'the largest STUDIO apartment' (CTR: 2BD U110 @0.8 before): only tags named like that
                narrowed = matches(name_words, scope)
                if not narrowed:
                    return self.abstain(f"no tagged room is named like {sorted(name_words)[:4]}", trace)
                scope = narrowed
            pick = (max if sup.group(1) != "smallest" else min)(scope, key=lambda t: conv(t["value"], t["unit"]))
            v = conv(pick["value"], pick["unit"])
            rank = sorted(scope, key=lambda t: -conv(t["value"], t["unit"]))
            nxt = rank[1] if sup.group(1) != "smallest" else rank[-2]
            tie = [t for t in scope if t is not pick and abs(conv(t["value"], t["unit"]) - v) < 1e-6]
            text = f"{_label(pick)} ({v:.0f} {unit_txt})" if want == "ft2" else f"{_label(pick)} ({v:.2f} {unit_txt})"
            asks_value = re.search(r"\bwhat is the (?:area|size)\b|\bhow (?:big|large)\b", q) and not re.search(
                r"\bwhich\b|\bwhat room\b|\bname\b", q)
            if tie and not asks_value:
                text += f" — tied with {', '.join(_label(t) for t in tie[:4])}"
            a = Answer(value=round(v, 2) if asks_value else text, unit=want if asks_value else None,
                       confidence=0.6 if tie and not asks_value else 0.8, evidence=ev([pick]) + ev([nxt], f"next: {_label(nxt)}: {nxt['text']}"),
                       solver=self.name, trace=trace + [f"{sup.group(1)} of {len(scope)} tagged rooms: {_label(pick)}"],
                       assumptions=[f"compared all {len(scope)} room area tags on the sheet; next is {_label(nxt)} "
                                    f"({conv(nxt['value'], nxt['unit']):.0f} {unit_txt})"] + conv_note,
                       missing=["rooms drawn without an area tag are not compared"]
                       + ([f"unlabelled area tag(s) {', '.join(t['text'] for t in loose[:3])} (gross/level totals?) "
                           "were left out"] if loose else []))
            return a
        if count:
            return Answer(value=len(scope), unit="count", confidence=0.6, evidence=ev(scope), solver=self.name,
                          trace=trace, assumptions=[f"one area tag per room ({'units' if units_scope else 'rooms'})"],
                          missing=["rooms drawn without an area tag are not counted"])
        # one room / unit named in the question
        nos = {n.upper() for n in re.findall(r"\b([A-Z]{0,3}\d{1,4}(?:\.[A-Z0-9]{1,3})?)\b", question)}
        hit = [t for t in named if t["number"] and t["number"].upper() in nos]
        how = "room number"
        if not hit:
            qw = _words(question)
            qw = {w for w in qw if not re.fullmatch(r"\d+(?:\.\d+)?", w)}
            exp = set(qw)
            for w in qw:
                exp |= set(_SYN.get(w.rstrip("s"), []))
            if exp and not total:
                def score(t):
                    tw = _words(t["name"] or "")
                    return sum(1 for w in exp if any(x == w or (len(w) >= 4 and x.startswith(w[:4])) for x in tw))
                best = max((score(t) for t in named), default=0)
                if best:
                    hit = [t for t in named if score(t) == best]
                    how = "name words" + ("" if qw & {w for t in hit for w in _words(t["name"] or "")} else
                                          " (via a synonym)")
        if not hit and _APT_Q.search(question):
            # 'total area of apartment B' (Swiss sheet: 320 m² = all 25 tags @0.7 before): tags do not say which
            # rooms form an apartment -> pdf_vector / dxf (unit outlines) answer that
            return self.abstain("the question is about one apartment; area tags do not say which rooms belong to it",
                                trace)
        if hit and not total:
            if len(hit) == 1:
                t = hit[0]
                conf = 0.9 if how == "room number" else (0.8 if "synonym" not in how else 0.55)
                return ans(conv(t["value"], t["unit"]), [t], conf,
                           [f"area = the room's printed area tag ('{t['text']}'), matched by {how}"],
                           [] if conf >= 0.8 else [f"the sheet has no tag named like the question; '{_label(t)}' is "
                                                   "the nearest name"])
            hit = sorted(hit, key=lambda t: -conv(t["value"], t["unit"]))
            lst = "; ".join(f"{_label(t)} {conv(t['value'], t['unit']):.0f} {unit_txt}" for t in hit[:8])
            return ans(conv(hit[0]["value"], hit[0]["unit"]), hit, 0.4,
                       [f"{len(hit)} tagged rooms match the question's words ({how}): {lst}; the largest is given"],
                       ["which one is meant (name the room number)"])
        parts = [x for x in re.split(r"\s*(?:,|\band\b|\bplus\b|&|\+)\s*", re.sub(
            r"^.*?\b(?:area|size)s?\b\s*(?:of\s+)?", "", question.rstrip("?. "), flags=re.I)) if _words(x) - {
            "combined", "total", "sum", "all"}]
        if total and len(parts) >= 2 and not units_scope:
            # 'combined area of the laundry and the community room' (CTR: 18,831 ft² = every tag, @0.6 before)
            picked, absent = [], []
            for prt in parts:
                m = matches(_words(prt) - {"combined", "total", "sum", "all"}, named)
                (picked.append(m[0]) if len(m) == 1 else absent.append(prt))
            if absent:
                return self.abstain(f"no single area tag for {absent[:3]} (named in the question)", trace)
            s = sum(conv(t["value"], t["unit"]) for t in picked)
            return ans(s, picked, 0.8, [f"sum of the named rooms' area tags: " + " + ".join(
                f"{_label(t)} {t['text'].split(' - ')[-1]}" for t in picked)])
        if total:
            s = sum(conv(t["value"], t["unit"]) for t in scope)
            printed = [t for t in loose if conv(t["value"], t["unit"]) >= 0.8 * s]
            assumptions = [f"total = sum of {len(scope)} {'unit' if units_scope else 'room'} area tags on the sheet: "
                           + ", ".join(f"{_label(t)} {t['text'].split(' - ')[-1]}" for t in scope[:12])
                           + (" …" if len(scope) > 12 else "")]
            missing = ["rooms without an area tag are not in the sum"]
            conf = 0.7
            if printed:
                g = conv(printed[0]["value"], printed[0]["unit"])
                assumptions.append(f"the sheet also prints an unlabelled total '{printed[0]['text']}' "
                                   f"({g:.0f} {unit_txt}), probably the gross floor area incl. walls/circulation")
                conf = 0.6
            return ans(s, scope, conf, assumptions, missing)
        return self.abstain("no room / unit in the question matches an area tag", trace)
