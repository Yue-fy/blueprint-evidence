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
import os
import re

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

FT2_M2 = 0.09290304
_ROOMNO = re.compile(r"^(?:\d{1,2}\.\d{2}|[A-Z]{0,2}\d{2,4}[A-Z]?)$")
_APT_LABEL = re.compile(r"^(?:Whg\.?|Wohnung|App\.?|Appartement|Apt\.?|Unit)\s*[A-Z0-9]{1,3}$", re.I)
_APT_Q = re.compile(r"\b(?:apartment|wohnung|whg\.?|appartement|appartamento|flat|logement)\s+([A-Z0-9]{1,2})\b", re.I)
# F5 20:22: DfE baseline sheets print 'A: 62 m2' / 'NIA: 62 m2' / 'Area = 62 m2' under the room name -> a short letter
# prefix with ':' / '=' is the tag's own label, not a room number
# F5 23:45 (DfE 2012 baseline sheets print '(55.0 m2)' under the room name): an optional bracket around the whole tag
_TAG = re.compile(r"^[(\[]?\s*(?:(?:[A-Za-z]{1,4}|area)\s*[:=]\s*)?(?:(?P<no>[A-Z]{0,3}\d{1,4}(?:\.[A-Z0-9]{1,3})?)\s*[-–:]\s*)?(?P<v>\d{1,3}(?:[,']\d{3})+|\d+(?:[.,]\d+)?)"
                  r"\s*(?P<u>SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|m²|m2|qm|sqm)\s*[)\]]?$", re.I)
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


_SCOPE_WORDS = {"module", "modules", "wing", "wings", "building", "buildings", "storey", "storeys", "story", "level",
                "levels", "second", "third", "fourth", "upper", "lower", "basement", "north", "south", "east", "west",
                "administration", "admin", "area", "zone", "block", "part", "section", "together", "combined", "sum",
                "altogether", "overall", "gesamt", "whole", "entire", "together"}
_FILLER = {"floor", "private", "public", "across", "including", "included", "excluding", "inside", "within", "under",
           "over", "between", "from", "through", "drawn", "plan", "sheet", "here", "listed", "printed", "labelled",
           "labeled", "named", "called", "marked", "tagged", "together", "rooms", "room", "spaces", "space", "net",
           "usable", "useful", "internal", "interior", "type", "types", "each", "both", "every", "what", "much"}
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12}


def _cached_layer(img_abs: str):
    """The image's text layer when some reader already read it (raster / label_count / W5's warm pre-read), else None
    (same test as label_count._image_lines: never a fresh OCR from here)."""
    try:
        from stoa.solvers.raster import textlayer as T

        tag = T.file_tag(img_abs)
        cached = any(k.startswith(f"{img_abs}{tag}|") for k in list(T._mem)) or \
            T._disk_key(img_abs, 1100, "local").exists()
        if not cached:
            from stoa import ocr

            cached = ocr.cached(img_abs)
        return T.text_layer(img_abs) if cached else None
    except Exception:  # noqa: BLE001
        return None


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


_ROW_KEY = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z0-9 .&/'-]{0,30}?)\s*:\s*$")


def _page_lines(path_abs: str, img: bool, layer) -> list:
    """(text, box) lines in the tags' own space: the image's text layer, or the PDF page's text lines (unrotated)."""
    if img:
        return [(re.sub(r"\s+", " ", str(i.text)).strip(), tuple(float(x) for x in i.box))
                for i in getattr(layer, "items", None) or [] if str(i.text).strip()]
    import pymupdf

    out = []
    with pymupdf.open(path_abs) as d:
        for b in d[0].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(sp["text"] for sp in ln["spans"]).strip()
                if t:
                    out.append((t, tuple(ln["bbox"])))
    return out


def _row_keys(tags: list, lines: list) -> list:
    """X1 00:00 / 00:27 (EFA colour sheet scans sc-376 / sc-377 / scl-164: 'According to the GIFA table, how big is the
    second floor?' = 4055 @0.8 on all 3 scans, gold 2253): a 'key: value' table ('Overall GIFA' / 'Ground: 4055m2' /
    'First: 2302m2' / 'Second: 2253m2' / 'Total: 8610m2', the keys in a column LEFT of the values) was read as one tag
    named by the heading above it, the other rows unnamed. A value whose own text row carries a 'Key:' label just left
    of it is named by that key (row = vertical overlap >= half a line), when >= 2 values of one column do so."""
    if not tags or not lines:
        return tags
    keyed = {}
    for i, t in enumerate(tags):
        tb = t.get("tag_bbox") or t.get("bbox")
        if not tb or len(tb) != 4:
            continue
        h = max(1.0, tb[3] - tb[1])
        best = None
        for txt, lb in lines:
            m = _ROW_KEY.match(txt)
            if not m:
                continue
            ov = min(tb[3], lb[3]) - max(tb[1], lb[1])
            gap = tb[0] - lb[2]
            if ov >= 0.5 * min(h, lb[3] - lb[1]) and -2 <= gap <= 4 * h and (best is None or gap < best[0]):
                best = (gap, m.group("k").strip(), lb)
        if best is not None:
            keyed[i] = best
    def x0(i):
        return float((tags[i].get("tag_bbox") or tags[i].get("bbox"))[0])

    def hh(i):
        tb = tags[i].get("tag_bbox") or tags[i].get("bbox")
        return max(1.0, float(tb[3] - tb[1]))

    ok: set[int] = set()
    order = sorted(keyed, key=x0)
    group = order[:1]
    for i in order[1:] + [None]:  # a column = values whose left edges are within a line height of the previous one
        if i is not None and abs(x0(i) - x0(group[-1])) <= max(hh(i), hh(group[-1])):
            group.append(i)
            continue
        if len(group) >= 2:
            ok.update(group)
        group = [i] if i is not None else []
    if not ok:
        return tags
    out = []
    for i, t in enumerate(tags):
        if i in ok:
            _gap, key, lb = keyed[i]
            bb = t.get("bbox") or t.get("tag_bbox")
            if re.fullmatch(r"(?i)(?:lower |upper )?(?:ground|first|second|third|fourth|fifth|basement|mezzanine|roof)", key):
                key = f"{key} floor"  # a GIFA table's row 'Ground:' is the ground floor's total
            t = {**t, "name": key, "number": None, "row_key": True,
                 "bbox": [round(min(bb[0], lb[0]), 1), round(min(bb[1], lb[1]), 1), round(max(bb[2], lb[2]), 1),
                          round(max(bb[3], lb[3]), 1)]}
        out.append(t)
    return out


def _label(t) -> str:
    return " ".join(x for x in (t["name"], t["number"]) if x) or "(unlabelled tag)"


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9.]+", s.lower()) if w not in _STOP and len(w) >= 2}


# E1 22:32 (T1 21:24, X1 sealed): 'What is the floor area of the dining hall?' -> 594 'Sports Hall: 594 m2' tied with
# 'Dining: 290 m2' (= gold) on the head noun 'hall': a qualifier word ('dining') outweighs a generic head noun
_GENERIC = {"hall", "room", "rooms", "area", "areas", "space", "spaces", "store", "office", "unit", "zone", "block",
            "wing", "floor", "level", "suite", "centre", "center", "main", "large", "small", "big", "general"}


def _name_score(exp: set[str], t) -> int:
    tw = _words(t["name"] or "")
    return sum(1 if w in _GENERIC else 2 for w in exp
               if any(x == w or (len(w) >= 4 and x.startswith(w[:4])) for x in tw))


# G1 c12 (X1 23:16 / 23:36, EFA colour sheet 2012 secondary type 2 FF, confident-wrong sums @0.65-0.7): 'the English
# classrooms' summed English 1..9 + 'ICT Classroom' + 'English Staff Work Room' (578; gold 495), 'art rooms' + 'Art
# Store', 'all the MFL rooms' + 'MFL Store' + 'MFL SWR'. A tag belongs to a SUM when it carries every qualifier word of
# the asked subject ('English'; the head noun 'classrooms' may be left out: 'English 4') and no word of ANOTHER room
# type ('Staff Work Room', 'Store', 'ICT'); index words ('1', '3D', 'Small') are fine.
_PURE_OK = {"room", "rooms", "area", "areas", "space", "spaces", "main", "large", "small", "big", "general", "new", "-"}


def _wf(w: str) -> str:
    return w.rstrip(".").removesuffix("es" if w.endswith("sses") else "s") if len(w) > 3 else w.rstrip(".")


def _same_word(w: str, x: str) -> bool:
    a, b = _wf(w), _wf(x)
    return a == b or (len(a) >= 5 and len(b) >= 5 and (a.startswith(b[:5]) or b.startswith(a[:5])))


def pure_subset(words: list[str], pool, extras: bool = True) -> list | None:
    """Tags named exactly like the asked subject (see above); None when the subject has no qualifier to test.
    extras=False: every tag that carries the qualifier words, whatever else its name says."""
    ws = [w for w in words if len(w) >= 2]
    if not ws:
        return None
    gen = lambda w: w in _GENERIC or _wf(w) in _GENERIC  # noqa: E731
    head, quals = ws[-1], [w for w in ws[:-1] if not gen(w)]
    need = quals or ([head] if not gen(head) else [])
    if not need:
        return None
    alts = {w: [w, *_SYN.get(_wf(w), [])] for w in ws}
    out = []
    for t in pool:
        tw = [x for x in re.findall(r"[a-z0-9.]+", (t["name"] or "").lower()) if x not in _STOP]
        if not all(any(_same_word(a, x) for a in alts[w] for x in tw) for w in need):
            continue
        extra = [x for x in tw if not any(_same_word(a, x) for w in ws for a in alts[w]) and
                 not re.fullmatch(r"\d+[a-z]?|[a-z]|[ivx]{1,4}|\d+\.\d+", x) and x not in _PURE_OK]
        if not extra or not extras:
            out.append(t)
    return out


_Q_FILLER = {"many", "much", "that", "this", "these", "those", "there", "here", "what", "which", "is", "are", "how",
             "square", "feet", "foot", "metres", "meters", "sf", "m2", "ft2", "sq", "ft", "do", "does", "we", "you", "it"}
_SIZE_COUNT = re.compile(r"\b(?:are|is|of|with|measur\w*|sized?)\s+(?:(exactly|over|above|more than|larger than|bigger "
                         r"than|at least|under|below|less than|smaller than|at most)\s+)?(\d+(?:[.,]\d+)?)\s*(m2|m²|sq\.? ?m|"
                         r"sqm|square met\w*|sf|ft2|ft²|sq\.? ?ft|square f\w*)", re.I)


@register("area_tags")
class AreaTags(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"))

    def solve(self, path: str, question: str) -> Answer:
        from stoa.data import abspath

        img = not str(path).lower().endswith(".pdf")
        layer = None
        if img:
            # VP 19:27: stoa.sheet reads the same views / room tags on Stoa's PNGs as on their PDF twins (AFCEE A-101
            # PNG sl-100 702 / sl-101 362 / sl-102 1388 with a sum rule; d24 sl-101 raster 484). Images: only VP's
            # scoped, NAMED tags on an already-read text layer (no fresh OCR here)
            layer = _cached_layer(str(abspath(path)))
            if layer is None:
                return self.abstain("image without a text layer read yet (area tags need the printed tags)")
        scoped = ()
        try:  # VP 18:43: the room tags of the view(s) the question means, rotation-aware, one per room (night-VP
            # stoa/sheet/labels.py; measured 2 W / 0 L on X1 stoa-like dev + D1's probes); () -> every tag as before
            from stoa.sheet.labels import as_area_tags

            scoped = (as_area_tags(str(abspath(path)), question, layer=layer) if img else
                      as_area_tags(str(abspath(path)), question)) or ()
            if scoped and not any(t.get("name") or t.get("number") for t in scoped):
                scoped = ()  # S0 d25: a scope of only unnamed totals (CTR A101: 5 x '21996 SF') names no room
        except Exception:  # noqa: BLE001 - module not merged yet, or it failed on this sheet
            scoped = ()
        if img and not scoped:
            return self.abstain("no named room area tags in the image's plan views")
        try:
            tags = list(scoped or read_tags(str(abspath(path))))
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the PDF text ({type(e).__name__})")
        try:  # a 'Key: value' table's rows are named by their keys (X1 00:00, GIFA table on the EFA scans)
            if os.environ.get("STOA_G1_ROW_KEYS", "1") != "0":  # kill switch (freeze)
                tags = _row_keys(tags, _page_lines(str(abspath(path)), img, layer))
        except Exception:  # noqa: BLE001
            pass
        if len(tags) < 3:
            return self.abstain(f"{len(tags)} area tags on the sheet (not a tagged plan)")
        try:
            import os

            from stoa.router.recognize import _pdf

            if not img and _pdf(str(abspath(path)), os.stat(abspath(path)).st_mtime)["bim_fill"] >= 2:
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
            return [Evidence(file=path, page=0, bbox=t["bbox"], bbox_space="image_px" if img else "pdf_pt", exact=not img,
                             note=note or f"{_label(t)}: {t['text']}") for t in ts[:80]]

        def ans(v, ts, conf, assumptions, missing=None, extra_trace=None):
            return Answer(value=round(v, 2), unit=want, confidence=conf, evidence=ev(ts), solver=self.name,
                          trace=trace + (extra_trace or []), assumptions=assumptions + conv_note,
                          missing=missing or [])

        sup = re.search(r"\b(largest|biggest|smallest)\b", q)
        # X1 21:26 rp-096: 'Add up the private offices in the admin module - how many square feet is that?' -> one
        # office (120; gold 362): 'add up' / 'together' / 'sum up' ask for a sum too
        total = re.search(r"\b(total|sum|all|combined|altogether|overall|gesamt|add(?:ed)? up|adding up|summed|together|"
                          r"in all|put together|sammanlagd\w*|yhteens[aä]|zusammen)\b", q)
        # H 19:06: 'How many square metres is the west wing in total?' answered '4 count' (the number of tags)
        count = re.search(r"\bhow many\b(?!\s+(?:square|sq\b|sq\.|m²|m2|ft²|ft2|sf|cubic|linear|metres|meters|feet))|"
                          r"\bnumber of\b", q)
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
                return _name_score(exp, t)
            # T1 22:51 (replay of 1,500 prediction files, all wrong @0.65-0.7): 'the four private offices in the
            # administration module' summed 'Corridor 2.03', 'all the science labs' summed 'Head's Office' /
            # 'Circulation': when the asked subject has a non-generic word, every picked tag carries one of them
            core = {w for w in words if w not in _GENERIC and len(w) >= 3}  # the question's own words, not synonyms
            if core:
                for w in list(core):
                    core |= set(_SYN.get(w.rstrip("s"), []))
                pool = [t for t in pool if _name_score(core, t) > 0]
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
        if count and _SIZE_COUNT.search(question):
            # X1 sl-322 'How many rooms on this floor are 55 m2?' -> 41 = every tag (gold 17 tags reading '55 m2')
            mz = _SIZE_COUNT.search(question)
            op, n0 = (mz.group(1) or "exactly").lower(), float(mz.group(2).replace(",", "."))
            uz = "ft2" if re.match(r"(?i)sf|ft|sq\.? ?ft|square f", mz.group(3)) else "m2"
            n0 = n0 if uz == want else (n0 * FT2_M2 if want == "m2" else n0 / FT2_M2)
            tol = max(0.5, 0.005 * n0)
            test = {"exactly": lambda v: abs(v - n0) <= tol}.get(op) or (
                (lambda v: v >= n0 - 1e-9) if op == "at least" else (lambda v: v <= n0 + 1e-9) if op == "at most" else
                (lambda v: v > n0 + tol) if op in ("over", "above", "more than", "larger than", "bigger than") else
                (lambda v: v < n0 - tol))
            sel = [t for t in scope if test(conv(t["value"], t["unit"]))]
            return Answer(value=len(sel), unit="count", confidence=0.65, evidence=ev(sel), solver=self.name,
                          trace=trace + [f"area_tags: {len(sel)} of {len(scope)} tagged rooms are {op} {n0:g} {unit_txt}: "
                                         + ", ".join(_label(t) for t in sel[:12]) + (" …" if len(sel) > 12 else "")],
                          assumptions=[f"one area tag per room; the printed tag value compared ({op} {n0:g} {unit_txt}, "
                                       f"±{tol:g})"], missing=["rooms drawn without an area tag are not counted"])
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
                    return _name_score(exp, t)
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
        body = re.sub(r"^.*?\b(?:area|size)s?\b\s*(?:of\s+)?", "", question.rstrip("?. "), flags=re.I)
        mp = re.search(r"\(([^()]*(?:,|\band\b|&)[^()]*)\)", body)
        if mp and os.environ.get("STOA_G1_NAME_LIST", "1") != "0":
            # O1 01:30 / X1 sl-317 'the three art rooms (Art 1, Art 2 and Art 3)': the brackets list the rooms
            body = mp.group(1)
        parts = [x for x in re.split(r"\s*(?:,|\band\b|\bplus\b|&|\+)\s*", body) if _words(x) - {
            "combined", "total", "sum", "all"}]
        if total and len(parts) >= 2 and not units_scope:
            # 'combined area of the laundry and the community room' (CTR: 18,831 ft² = every tag, @0.6 before)
            picked, absent = [], []
            for prt in parts:
                m = matches(_words(prt) - {"combined", "total", "sum", "all"}, named)
                nm = re.search(r"\b(\d{1,4}[A-Za-z]?)\s*\W*$", prt)
                if len(m) > 1 and nm and os.environ.get("STOA_G1_NAME_LIST", "1") != "0":
                    # 'Art 1' vs tags 'Art 1 - Small' / 'Art 2' / 'Art 3 - 3D': _words drops 1-character tokens, so
                    # the room's own number picks the one tag that prints it (as a whole token)
                    rx = re.compile(r"(?<![\w.])" + re.escape(nm.group(1)) + r"(?![\w.])", re.I)
                    m = [t for t in m if rx.search(str(t.get("name") or ""))] or m
                (picked.append(m[0]) if len(m) == 1 else absent.append(prt))
            if absent:
                return self.abstain(f"no single area tag for {absent[:3]} (named in the question)", trace)
            s = sum(conv(t["value"], t["unit"]) for t in picked)
            return ans(s, picked, 0.8, [f"sum of the named rooms' area tags: " + " + ".join(
                f"{_label(t)} {t['text'].split(' - ')[-1]}" for t in picked)])
        if total and len(loose) > len(named) and not scoped:
            # H 19:06 (AFCEC A-2 PDF, 4 named + 12 unlabelled figures = the SQUARE FOOTAGE table): 'the west wing's
            # total area in m²' summed the 4 named tags (1580.19 m² @0.6; gold 7,009 SF printed in the table)
            return self.abstain(f"{len(loose)} of {len(tags)} area figures carry no room name (a printed area table, "
                                "not room tags): a total is read from the table, not summed from tags", trace)
        if total and not units_scope:
            # VP 19:06 / E1 19:07 (AFCEE A-101, X1 sl-096): 'combined area of the four private offices in the
            # administration module' summed every scoped tag (1887 SF, gold 362); 'total floor area of all Manager
            # Offices' -> 3275 (E1: 281). Room words that pick a PROPER subset of >= 2 tags -> the sum of that subset.
            # Scope words (module / wing / storey names) are VP's scope_for's job, not a room name.
            room_words = name_words - _SCOPE_WORDS - _FILLER - set(_NUMBER_WORDS)
            # 'in the entry vestibule module' names the PLACE (VP's scope), not a room (X1 sl-280: 'Sum up all the room
            # areas in the entry vestibule module.' = the module's 8 tags, 1388 SF)
            for mp in re.finditer(r"\b(?:in|of|within|across|for)\s+(?:the\s+)?((?:(?!(?:in|of|on|at|within|across|for|the|"
                                  r"and|all)\b)[a-z0-9'-]+\s+){0,3})(?:module|wing|"
                                  r"block|zone|department|suite|section|area)s?\b", q):
                room_words -= set(mp.group(1).split())
            room_words = {w.rstrip(".") for w in room_words} - _SCOPE_WORDS - {"up", "add", "added", "adding", "sum",
                                                                                "summed", "total", "combined"}
            every = re.search(r"\ball (?:of )?(?:the )?(?:rooms?|spaces?)(?:'s)?(?: (?:floor )?areas?)?\b|"
                              r"\bevery (?:room|space)\b", q)  # scope words name the view
            sub = matches(room_words, scope) if room_words and not every else []
            dropped = []
            if sub and not every:
                order = [w for w in re.findall(r"[a-z0-9.]+", q) if w in room_words and w not in _Q_FILLER]
                pure = pure_subset(list(dict.fromkeys(order)), scope)
                if pure == [] and len(sub) >= 2 and pure_subset(list(dict.fromkeys(order)), sub, extras=False) == []:
                    # D1 23:22 (HAER HI-76 C2): 'total roof catchment area of the two rainshed buildings' summed the two
                    # LINED GROUND catchment tags (179,878 @0.70; gold 86,157 = the RAINSHED tags): no tag carries every
                    # qualifier word of the subject -> not a sum of these tags
                    return self.abstain(f"no area tag carries all of {' '.join(dict.fromkeys(order))} (the tags that "
                                        f"share a word: {', '.join(sorted({_label(t) for t in sub})[:4])})", trace)
                if pure and {id(t) for t in pure} != {id(t) for t in sub}:
                    dropped = [t for t in sub if t not in pure]
                    trace.append(f"area_tags: subject check: {len(pure)} tag(s) named exactly like "
                                 f"{' '.join(dict.fromkeys(order))}" + (f"; left out (another room type): "
                                 f"{', '.join(sorted({_label(t) for t in dropped})[:6])}" if dropped else ""))
                    sub = pure
            if room_words and not every and len(sub) == 1 and len(scope) >= 6:
                # (>= 6 named tags: on a sheet with a few tags the sum of all of them may still be the subject: D1's
                # HAER 'lined ground catchments' GC1 (OCR 'LINED GROUND IMENT') + GC2 = 179,878 = gold)
                # X1 sl-318 'total area of the ICT classrooms' -> 2071 = EVERY tag: a one-tag subset fell through to the
                # whole-sheet sum below; one tag named like that is that tag (the question expects several: say so)
                t = sub[0]
                return ans(conv(t["value"], t["unit"]), [t], 0.5,
                           [f"one area tag is named like {sorted(room_words)[:4]}: {_label(t)} {t['text']}"],
                           ["the question asks for a total of several rooms; only one tag named like that was read "
                            "(a second identical room may be merged with it or untagged)"])
            if room_words and not every and not sub:
                # X1 sl-127 / sc-083 (EFA scans): 'total floor area of English 1 to English 5' summed every tag (2388 m²)
                # when no tag is named 'English': rooms the question names that no tag carries -> not this reader
                return self.abstain(f"no area tag is named like {sorted(room_words)[:4]} (the rooms the question "
                                    "sums)", trace)
            if 2 <= len(sub) < len(scope):
                s = sum(conv(t["value"], t["unit"]) for t in sub)
                said = next((n for w, n in _NUMBER_WORDS.items() if re.search(rf"\b{w}\b", q)), None)
                conf = 0.8 if said == len(sub) else (0.45 if said else 0.65)
                return ans(s, sub, conf, [f"sum of the {len(sub)} area tags named like {sorted(room_words)[:4]}: " + " + ".join(
                    f"{_label(t)} {t['text'].split(' - ')[-1]}" for t in sub[:12]) + (" …" if len(sub) > 12 else "")]
                    + ([f"not in the sum (another room type): {', '.join(sorted({_label(t) for t in dropped})[:6])}"]
                       if dropped else []),
                    [f"the question says {said} rooms, {len(sub)} tags match"] if said and said != len(sub) else
                    ["rooms named like that without an area tag are not in the sum"],
                    [f"total of a named subset: {len(sub)} of {len(scope)} tags"])
        if total and not units_scope and not re.search(r"\ball (?:of )?(?:the )?(?:rooms?|spaces?)\b|\bevery (?:room|space)\b", q):
            # room_words: set by the subset branch above (same condition): scope phrases / verbs already dropped
            order = list(dict.fromkeys(w.rstrip(".") for w in re.findall(r"[a-z0-9.]+", q)
                                       if w.rstrip(".") in room_words and w not in _Q_FILLER))
            if order and pure_subset(order, scope, extras=False) == []:
                # D1 23:22: 'total roof catchment area of the two rainshed buildings' = the sum of the sheet's 2 named
                # tags (LINED GROUND catchments, 179,878 @0.70; gold 86,157): no tag carries 'roof' -> not their sum
                return self.abstain(f"no area tag carries all of {' '.join(order)}: the sum of every tag would not be "
                                    "the asked subject", trace)
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
