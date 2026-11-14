"""T1 sanity guards on the final answer (captain 20:47 / 20:49, expert Ziying's workbench rules, tab 3).

1. Implausible zero (Ziying, verbatim): "A facade of habitable rooms (classrooms, offices, bedrooms) has at least one
   window; a count of 0 must be flagged." Generalised (captain 20:49): a count of 0 windows / doors on a facade, a wing,
   a floor, a building or a whole plan, or 0 windows in a habitable room (classroom, office, bedroom, living room),
   is a reading failure far more often than a fact. Storage / plant / WC / corridor rooms may have none, and a
   question about a subtype ('double doors', 'fire doors', 'of type W3'), a difference or a comparison may truly be 0.
   Measured on every earlier prediction file (artifacts/eval, 2,962 files): ~270 distinct 0 answers to window / door
   counts in this scope, 2 right (a Swiss DXF with no window entities, an IFC storey with no doors), both from a
   structured model reader. So:
     - drawing readers (detectors, raster models, VLMs, vector symbol rules): no number. missing[0] says 'none found
       — probably a reading failure', what was searched, and how to count by hand (region tool / 'count all like
       this'); the searched-area boxes stay as evidence; the reader's 0 is kept in the trace.
     - structured model readers (IFC / DXF entities, schedules): the 0 is what the model holds, so it stays, flagged:
       confidence <= 0.3 and the same warning first in missing.
2. Calibration domain (Ziying): "Show the 90% range only when the drawing is in the calibration domain (CubiCasa5K
   flats)." Every count range we print (raster_cc5k detector, vlm_lora LoRA, the pipeline's LoRA-vs-detector range)
   was fitted on held-out CubiCasa5K drawings. On any other drawing kind (router.recognize kind != 'cubicasa_plan')
   the range lines are removed from missing and the 'intervalNN: [lo, hi]' trace line that web/src/card.ts draws is
   rewritten so the card no longer shows it ('range not shown: …').
"""

from __future__ import annotations

import re

from stoa.contract import Answer

TAG = "sanity"
FLAG_CONF = 0.3

# --- what is counted ------------------------------------------------------------------------------------------------
_WINDOW = re.compile(r"\bwindows?\b|\bglazing\b|\bikkun\w*|\bfenster\w*|\bf[öo]nster\w*|\bfen[êe]tres?\b|\bvensters?\b",
                     re.I)
_DOOR = re.compile(r"\bdoors?\b|\bdoorways?\b|\bov(?:i|ea|ia|et|en)\b|\bt[üu]r(?:en)?\b|\bd[öo]rr(?:ar|en|arna)?\b|"
                   r"\bportes?\b|\bdeuren?\b", re.I)
# a subtype, a filter, a difference or a comparison may truly be 0
_SUBSET = re.compile(
    r"\b(?:double|single|twin|pairs? of|sliding|folding|bi-?fold|pocket|revolving|rotating|fire|fire-rated|rated|"
    r"emergency|escape|glazed|glass|louvred|louvered|automatic|motorised|security|steel|timber|wooden|roller|overhead|"
    r"garage|sectional|roof|skylights?|dormer|bay|round|circular|arched|clerestory|internal|interior|inner|"
    r"exterior|external|outside|entrance|entry|front|back|rear|balcony|patio|terrace|french|"
    r"type|types|typ|tagged|marked|labelled|labeled|numbered|schedule|scheduled|legend|"
    r"wider|narrower|taller|higher|lower|larger|smaller|wide|width|height|over|under|above|below|than|at least|"
    r"more|fewer|less|differ\w*|difference|compare\w*|between|without|except|excluding|not|no|missing|removed|"
    r"new|existing|demolished|proposed|to be|connect\w*|besides|apart from|other than|only|"
    r"share[ds]?|shared|per|each|average|ratio|percent\w*|%|"
    r"kaksois\w*|pariovi\w*|liuku\w*|palo\w*|ulko\w*|sisä\w*|doppel\w*|schiebe\w*|brandschutz\w*|au(?:ß|ss)en\w*|"
    r"innen\w*|dubbel\w*|skjut\w*|brand\w*|ytter\w*|inner\w*)\b", re.I)
# the counted region is a whole facade / wing / floor / building / plan
_WHOLE = re.compile(
    r"\b(?:facades?|façades?|elevations?|wings?|blocks?|buildings?|house|houses|school|schools|hospital|"
    r"apartments?|flats?|dwellings?|units?|homes?|villa|cottage|bungalow|"
    r"floors?|storeys?|stories|levels?|plans?|floor ?plans?|drawings?|sheets?|pages?|layouts?|"
    r"in total|altogether|all together|whole|entire|overall|"
    r"pohjapiirustu\w*|pohjapiirro\w*|kerro\w*|julkisivu\w*|siipi\w*|rakennu\w*|asunno\w*|talo\w*|"
    r"grundriss\w*|geschoss\w*|stockwerk\w*|fassade\w*|flügel\w*|gebäude\w*|wohnung\w*|haus\w*|"
    r"planritning\w*|våning\w*|fasad\w*|flygel\w*|byggnad\w*|lägenhet\w*)\b", re.I)
_HABITABLE = re.compile(
    r"\b(?:class ?rooms?|teaching (?:rooms?|spaces?)|seminar rooms?|lecture (?:rooms?|halls?)|group rooms?|"
    r"offices?|staff ?rooms?|meeting rooms?|conference rooms?|studys?|studies|studio|library|"
    r"bed ?rooms?|master bedroom|guest room|living rooms?|living areas?|lounges?|sitting rooms?|family rooms?|"
    r"dining rooms?|"
    r"luokka\w*|toimisto\w*|työhuone\w*|makuuhuone\w*|olohuone\w*|"
    r"klassenzimmer\w*|klassenraum\w*|büro\w*|schlafzimmer\w*|wohnzimmer\w*|"
    r"klassrum\w*|kontor\w*|sovrum\w*|vardagsrum\w*)\b", re.I)
# rooms that may have no window: never flag a 0 window count asked about one of these
_NO_WINDOW = re.compile(
    r"\b(?:stor(?:e|es|age|eroom)s?|store ?rooms?|plant ?rooms?|plant|wc|wcs|w\.c\.|toilets?|lavator\w*|restrooms?|"
    r"bath ?rooms?|baths?|showers?|washrooms?|changing|locker|corridors?|hall ?ways?|passages?|circulation|lobby|"
    r"vestibule|foyer|cupboards?|closets?|wardrobes?|walk-in|pantry|larder|risers?|shafts?|ducts?|lifts?|elevators?|"
    r"stair\w*|utility|laundry|cleaners?|janitor|sauna|basement|cellar|garage|server|comms?|data|electrical|"
    r"switch ?room|boiler|tank|pump|meter|bin|refuse|attic|loft|void|"
    r"varasto\w*|vaatehuone\w*|kylpyhuone\w*|pesuhuone\w*|käytävä\w*|eteinen|eteis\w*|tekninen|kellari\w*|"
    r"abstell\w*|lager\w*|flur|keller\w*|bad|dusche|förråd\w*|korridor\w*|hall|tvätt\w*|källare\w*)\b", re.I)
# spaces that may have no door LEAF (open to the next space): never flag a 0 door count asked about one of these.
# Plant rooms, WCs and stores do have doors.
_NO_DOOR = re.compile(
    r"\b(?:corridors?|hall ?ways?|halls?|passages?|circulation|lobby|foyer|landings?|stair\w*|open[- ]plan|"
    r"living|lounges?|dining|kitchens?|kitchenettes?|studio|library|balcon\w*|terrace|patio|loggia|veranda|porch|"
    r"alcoves?|niches?|recess\w*|bay|voids?|shafts?|risers?|ducts?|wardrobes?|closets?|cupboards?|walk-in|"
    r"käytävä\w*|eteinen|eteis\w*|olohuone\w*|keittiö\w*|parveke\w*|flur|wohn\w*|küche\w*|korridor\w*|"
    r"vardagsrum\w*|kök\w*|balkong\w*)\b", re.I)
_DOOR_ROOM = re.compile(r"\b(?:class ?rooms?|teaching rooms?|seminar rooms?|group rooms?|offices?|staff ?rooms?|"
                        r"meeting rooms?|conference rooms?|bed ?rooms?|guest room|luokka\w*|toimisto\w*|"
                        r"makuuhuone\w*|klassenzimmer\w*|büro\w*|schlafzimmer\w*|klassrum\w*|kontor\w*|"
                        r"sovrum\w*)\b", re.I)
_COUNT_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b|\bkuinka monta\b|\bmontako\b|\bmonta\b|\bwie ?viele\b|"
                      r"\banzahl\b|\bhur många\b|\bantal\b|\bcombien\b|\bhoeveel\b", re.I)

# structured model readers: a 0 is what the model holds (IFC entities, DXF blocks, a schedule's rows)
_EXACT_SOLVER = re.compile(r"(?:^|/)(?:ifc|kg_qa|dxf|svg_ifc|ifcplan_pdf|schedule|plan_svg)$", re.I)


def _is_zero(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return v == 0
    return isinstance(v, str) and v.strip().lower() in ("0", "0.0", "none", "zero", "no")


def zero_scope(question: str) -> tuple[str, str] | None:
    """(what, where) when a count of 0 for this question is implausible, else None.
    what = 'windows' | 'doors'; where = 'facade' | 'whole' | 'habitable room'."""
    q = question or ""
    if not _COUNT_Q.search(q):
        return None
    win, door = bool(_WINDOW.search(q)), bool(_DOOR.search(q))
    if win == door:  # neither, or both ('windows and doors' sums: leave to the parts guard)
        return None
    what = "windows" if win else "doors"
    if _SUBSET.search(q) or (_NO_WINDOW if win else _NO_DOOR).search(q):
        return None
    if re.search(r"\bfa[cç]ades?\b|\belevations?\b|\bjulkisivu\w*|\bfassade\w*|\bfasad\w*", q, re.I):
        return what, "facade"
    if (_HABITABLE if win else _DOOR_ROOM).search(q):
        return what, "habitable room"
    if _WHOLE.search(q):
        return what, "whole"
    return None


def _scope_words(question: str, where: str) -> str:
    m = re.search(r"\b(?:on|of|in|for|at)\s+(?:the\s+)?((?:north|south|east|west|front|rear|side|main|[\w-]+)\s+"
                  r"(?:fa[cç]ade|elevation|wing)(?:\s+of\s+the\s+[\w -]{2,30}?)?)(?=[?.,]|$)", question or "", re.I)
    if m:
        return "the " + m.group(1).strip()
    m = _HABITABLE.search(question or "") or _DOOR_ROOM.search(question or "")
    if where == "habitable room" and m:
        return "the " + m.group(0).lower()
    return "this drawing"


NONE_FOUND = "none found — probably a reading failure"


def flagged_zero(ans: Answer) -> bool:
    """zero_guard turned a reader's 0 into 'none found', or scope_guard a whole-plan count into 'whole plan only'
    (value None either way): still a reading (it marks where we looked and offers the hand count), so a caller choosing
    between passes should prefer it to the dataset prior (P1's upload._flagged_zero calls this name)."""
    return ans.value is None and bool(ans.missing) and (str(ans.missing[0]).startswith(NONE_FOUND) or
                                                       str(ans.missing[0]).startswith(SCOPE_LOST))


def zero_guard(question: str, ans: Answer) -> Answer:
    """Ziying's rule: an implausible 0 is flagged, never shown as a confident answer (see the module doc)."""
    try:
        if not _is_zero(ans.value) or str(ans.unit or "count").lower() not in ("count", "", "none", "items", "pcs"):
            return ans
        sc = zero_scope(question)
    except Exception:  # noqa: BLE001
        return ans
    if sc is None:
        return ans
    what, where = sc
    place = _scope_words(question, where)
    rule = ("a facade of habitable rooms (classrooms, offices, bedrooms) has at least one window" if what == "windows"
            else "a building, wing or floor plan has at least one door")
    if where == "habitable room":
        rule = f"{place} normally has at least one {what[:-1]}"
    searched = [str(e.note) for e in ans.evidence or [] if re.search(r"searched", str(e.note or ""), re.I)]
    looked = (f"we searched {'the marked area' if searched else place} and recognised no {what[:-1]} symbol"
              f"{' (' + (ans.solver or '').split('/')[-1] + ')' if ans.solver else ''}")
    # the web's region feature is 'Ask about an area' (web/src/features/region); G1's 'count all like this' (click
    # one symbol as a template) is API-only so far: name what a visitor can actually do
    tool = (f"count them by hand on the drawing, or select {place if place != 'this drawing' else 'the part you mean'} "
            f"with 'Ask about an area' and ask again")
    out = ans.model_copy(deep=True)
    solver = out.solver or ""
    if _EXACT_SOLVER.search(solver):
        sentence = (f"0 {what} in the model's data — unusual ({rule}): check that the model has its {what} as "
                    f"{'IfcWindow' if what == 'windows' else 'IfcDoor'} / {what[:-1]} blocks, or {tool}")
        if out.confidence > FLAG_CONF:
            out.trace = [*out.trace, f"{TAG}: 0 {what} on {place} is implausible ({rule}); structured reader "
                                     f"{solver}: value kept, confidence {out.confidence:.2f} -> {FLAG_CONF}"]
            out.confidence = FLAG_CONF
        else:
            out.trace = [*out.trace, f"{TAG}: 0 {what} on {place} is implausible ({rule}); structured reader: kept"]
        out.missing = [sentence] + [m for m in out.missing if m != sentence]
        return out
    sentence = (f"{NONE_FOUND}, not a real 0 ({rule}). {looked[0].upper() + looked[1:]}; "
                f"{tool}")
    out.trace = [*out.trace, f"{TAG}: {solver or 'the reader'} counted 0 {what} on {place}: implausible ({rule}) -> "
                             "no number; the searched area stays marked"]
    out.assumptions = [*out.assumptions, f"the reader ({solver.split('/')[-1] or '?'}) found 0 {what}; not shown as "
                                         "an answer"]
    out.missing = [sentence] + [m for m in out.missing if "90% range" not in str(m)]
    out.value, out.confidence = None, 0.0
    out.trace = [_hide_range(t, "no number") for t in out.trace]
    return out


# --- calibration domain of the count ranges -------------------------------------------------------------------------
# the same pattern web/src/card.ts rangeHtml() draws (unanchored: nested traces carry a '[raster_cc5k] ' prefix)
_RANGE_TRACE = re.compile(r"interval(\d{2}):\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")
_RANGE_MISSING = re.compile(r"\b\d{2}% range\b", re.I)


def _hide_range(line, why: str):
    """'interval90: [0, 3]' -> a line web/src/card.ts no longer draws (its regex needs 'intervalNN: [')."""
    s = str(line)
    m = _RANGE_TRACE.search(s)
    if not m:
        return line
    return f"{s[:m.start()]}range not shown ({why}): {m.group(1)}% range {m.group(2)}–{m.group(3)}{s[m.end():]}"


def in_cc_domain(ans: Answer, path: str | None = None, kind: str | None = None) -> bool | None:
    """True / False when we can tell whether the drawing is a CubiCasa5K-style flat plan, None when we cannot.
    `kind` = the UPLOADED file's recognised kind when the answer was read on a copy (a PDF page rendered to PNG is
    itself recognised as 'cubicasa_plan': Stoa's DfE sheet 1 at 150 dpi).
    A dataset file under cubicasa5k/ IS the calibration family whatever its look: 8.5 % of them are recognised as
    'scan' / 'plan_render' (5,581 eval rows with a range: 5,097 cubicasa_plan, 413 scan, 70 plan_render). Uploads
    have neutral paths, so there the content decides."""
    if kind is None and path and "/cubicasa5k/" in "/" + str(path).lower().replace("\\", "/"):
        return True
    rec = getattr(ans, "recognition", None) or {}
    kind = kind or (rec.get("kind") if isinstance(rec, dict) else None)
    if kind:
        return kind == "cubicasa_plan"
    for t in ans.trace or []:
        m = re.match(r"recognised:\s*\{.*?\"kind\":\s*\"(\w+)\"", str(t))
        if m:
            return m.group(1) == "cubicasa_plan"
    return None


def range_guard(ans: Answer, path: str | None = None, kind: str | None = None, label: str | None = None) -> Answer:
    """Ziying: show the 90% range only on the drawings it was calibrated on (CubiCasa5K flats)."""
    try:
        has = any(_RANGE_TRACE.search(str(t)) for t in ans.trace or []) or \
            any(_RANGE_MISSING.search(str(m)) for m in ans.missing or [])
        if not has or in_cc_domain(ans, path, kind) is not False:
            return ans
    except Exception:  # noqa: BLE001
        return ans
    rec = getattr(ans, "recognition", None) or {}
    if not label and kind:
        from stoa.router.recognize import LABELS

        label = LABELS.get(kind)
    label = label or (rec.get("label") if isinstance(rec, dict) and not kind else None) or "this kind of drawing"
    why = f"calibrated on CubiCasa5K flat plans only; this drawing was recognised as {label}"
    out = ans.model_copy(deep=True)
    out.trace = [_hide_range(t, why) for t in out.trace]
    had = [m for m in out.missing if _RANGE_MISSING.search(str(m))]
    out.missing = [m for m in out.missing if not _RANGE_MISSING.search(str(m))]
    if had:
        out.missing.append(f"exact count not certain; no range given: our ranges are calibrated on CubiCasa5K flat "
                           f"plans only, not on {label}")
    out.trace.append(f"{TAG}: count range not shown ({why})")
    return out


# --- a part of the building asked, the whole plan counted -------------------------------------------------------------
# dfe-08: raster_cc5k was asked the canonical 'How many windows are shown on this floor plan?' for 'windows on the west
# facade of the classroom wing'. Its answer counts a different quantity (every window on the sheet): an upper bound,
# never the facade's count.
_PART = re.compile(r"\b(?:fa[cç]ades?|elevations?|wings?|(?:north|south|east|west)(?:[- ]?(?:north|south|east|west))?"
                   r"(?:ern)?\s+(?:sides?|walls?|ends?|parts?|halves|half|fa[cç]ades?|elevations?|wings?|blocks?))\b",
                   re.I)
_CANON = re.compile(r"asked the canonical form \"([^\"]*)\"?")
SCOPE_LOST = "this counts the whole plan, not only"


def scope_guard(question: str, ans: Answer) -> Answer:
    """A count for a facade / wing / side whose reader was asked the whole-plan form -> no number; the whole-plan
    count is kept in the sentence as an upper bound."""
    try:
        if not isinstance(ans.value, (int, float)) or isinstance(ans.value, bool) or ans.value == 0:
            return ans
        part = _PART.search(question or "")
        if part is None or not _COUNT_Q.search(question or ""):
            return ans
        canon = [m.group(1) for t in ans.trace or [] for m in [_CANON.search(str(t))] if m]
        if not canon or any(_PART.search(c) for c in canon):
            return ans
    except Exception:  # noqa: BLE001
        return ans
    place = _scope_words(question, "facade")
    if place == "this drawing":
        place = "the " + part.group(0).lower()
    n = ans.value
    shown = f"{n:g}" if isinstance(n, float) else str(n)
    noun = next((w for w in ("windows", "doors") if (_WINDOW if w == "windows" else _DOOR).search(question)), "items")
    out = ans.model_copy(deep=True)
    out.trace = [*out.trace, f"{TAG}: the reader was asked the whole-plan form ({canon[-1][:80]!r}); its {shown} is not "
                             f"the count for {place} -> no number (kept as an upper bound)"]
    out.missing = [f"{SCOPE_LOST} {place}: we counted {shown} {noun} on the whole plan (an upper bound) but could not "
                   f"tell which of them are on {place}; count them by hand on the drawing, or select {place} with "
                   "'Ask about an area' and ask again"] + [m for m in out.missing if "90% range" not in str(m)]
    out.assumptions = [*out.assumptions, f"whole-plan count {shown} {noun} (not shown as the answer)"]
    out.value, out.confidence = None, 0.0
    out.trace = [_hide_range(t, "no number") for t in out.trace]
    return out


flagged = flagged_zero  # either flagged reading


# --- a numeric question answered by text that is not that quantity ------------------------------------------------
# Measured on all 2,962 earlier prediction files (603 distinct text answers to area / length questions): the confident
# wrong ones were a room reference for 'Di quanti m² il locale 1.06 è più grande …' (dxf 'room 1.06 (Zimmer)' @0.96-0.99,
# 12 rows), an error message for an area sum (ifc "The room '2. Level' does not exist …" @0.98, 7 rows) and door-schedule
# strings for a room's size (pdf_text "2'-10\" wide x 6'-8\" MIN high" @0.85 for 'How big is the meeting room?').
# Text answers that carry the quantity ('20 ajtó látható …', '4,479 SF', "323'-4''", '16 inches') are left alone.
SHAPE_CAP = 0.3
SHAPE_MISSING = "the answer does not have the shape the question asks for"  # = pipeline._shape_capped's line
_MESSAGE = re.compile(r"\b(?:does not exist|doesn't exist|do not exist|not found|no such|could not|cannot|can't|"
                      r"unable to|not available|no data|unknown)\b", re.I)
_ROOM_REF = re.compile(r"^\s*(?:room|space|locale|raum|huone|rum|pièce|stanza)\s+[\w.-]+\s*\(", re.I)
_OPENING = re.compile(r"\b(?:high|tall|ht\.?|hgt)\b", re.I)
_PAIR = re.compile(r"\bwide\b|\s[x×]\s", re.I)
_SCHEDULE_EV = re.compile(r"(?<![a-z])(?:door|window|opening)[ _-]?schedule(?![a-z])", re.I)  # 't3_door_schedule row: 102A'
_OPENING_Q = re.compile(r"\b(?:doors?|windows?|openings?|doorways?|glazing|frames?|leaf|leaves)\b", re.I)
_AREA_CUE = re.compile(r"m²|\bm2\b|\bsq\.?\s?(?:m|ft|feet)\b|\bsquare\b|\barea\b|\bfläche|\bsuperficie|\bpinta-?ala|"
                       r"\boppervlakte|\bneliö", re.I)


def _numeric_kind(question: str) -> str | None:
    from stoa.trust.support import asked_kind

    k = asked_kind(question)
    if k in ("area", "length", "count", "volume"):
        return k
    return "area" if _AREA_CUE.search(question or "") else None


def text_shape_guard(question: str, ans: Answer) -> Answer:
    """A text answer to an area / length / count question that is a message, a room reference, an opening's size or a
    door-schedule cell -> confidence <= SHAPE_CAP and the shape line in missing (support_state -> unlocated)."""
    try:
        v = ans.value
        if not isinstance(v, str) or not v.strip() or float(ans.confidence or 0) <= SHAPE_CAP:
            return ans
        kind = _numeric_kind(question)
        if kind is None:
            return ans
        why = None
        noun = {"area": "an area", "length": "a length", "count": "a number", "volume": "a volume"}[kind]
        if _MESSAGE.search(v):
            why = f"the question asks for {noun}; the answer is a message ('{v.strip()[:60]}')"
        elif kind != "count" and _ROOM_REF.match(v):
            why = f"the question asks for {noun}; the answer names a room ('{v.strip()[:40]}')"
        elif kind == "area" and _OPENING.search(v) and _PAIR.search(v):
            why = f"the question asks for an area; the answer is an opening's width × height ('{v.strip()[:50]}')"
        elif kind in ("area", "length") and not _OPENING_Q.search(question or "") and ans.evidence and \
                all(_SCHEDULE_EV.search(str(e.note or "")) for e in ans.evidence):
            why = (f"the value ('{v.strip()[:30]}') is read from a door / window schedule; the question is not about a "
                   "door or window")
    except Exception:  # noqa: BLE001
        return ans
    if why is None:
        return ans
    out = ans.model_copy(deep=True)
    out.trace = [*out.trace, f"{TAG}: text answer of the wrong shape: {why} -> confidence capped at {SHAPE_CAP}"]
    out.missing = [*out.missing, f"{SHAPE_MISSING} ({why})"]
    out.confidence = SHAPE_CAP
    return out


def sanity_guard(question: str, ans: Answer, path: str | None = None) -> Answer:
    """Pipeline.solve, after final_guard: Ziying's two rules, the lost-scope check, text answers of the wrong shape."""
    try:
        return text_shape_guard(question, scope_guard(question, zero_guard(question, range_guard(ans, path))))
    except Exception:  # noqa: BLE001
        return ans


__all__ = ["NONE_FOUND", "SCOPE_LOST", "flagged", "flagged_zero", "in_cc_domain", "range_guard", "sanity_guard",
           "scope_guard", "text_shape_guard", "zero_guard", "zero_scope"]
