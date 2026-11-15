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
                                                       str(ans.missing[0]).startswith(SCOPE_LOST) or
                                                       str(ans.missing[0]).startswith(UNCOUNTED))  # withhold mode


def zero_guard(question: str, ans: Answer, path: str | None = None) -> Answer:
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
    if _asked_in_area(path):  # the visitor already selected the area: no circular advice
        tool = "count them by hand inside the selected area"
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


_COUNT_WORD = re.compile(r"\b(?:zero|none|no|nil|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|single|"
                         r"pair|both|dozen|yksi|kaksi|kolme|neljä|viisi|eins|zwei|drei|vier|fünf|en|ett|två|tre|fyra|fem)\b",
                         re.I)


def text_shape_guard(question: str, ans: Answer) -> Answer:
    """A text answer to an area / length / count question that is a message, a room reference, an opening's size or a
    door-schedule cell -> confidence <= SHAPE_CAP and the shape line in missing (support_state -> unlocated)."""
    try:
        v = ans.value
        if float(ans.confidence or 0) <= SHAPE_CAP or v is None or isinstance(v, bool):
            return ans
        kind = _numeric_kind(question)
        if kind is None:
            return ans
        why = None
        if not isinstance(v, str):  # T1 c3: a NUMBER read only from a door / window schedule for a room's size
            if isinstance(v, (int, float)) and kind in ("area", "length") and \
                    not _OPENING_Q.search(question or "") and ans.evidence and \
                    all(_SCHEDULE_EV.search(str(e.note or "")) for e in ans.evidence):
                why = (f"the value ({v:g}) is read from a door / window schedule; the question is not about a door or "
                       "window")
            v = ""
        if not v.strip() and why is None:
            return ans
        noun = {"area": "an area", "length": "a length", "count": "a number", "volume": "a volume"}[kind]
        if why is not None:
            pass
        elif _MESSAGE.search(v):
            why = f"the question asks for {noun}; the answer is a message ('{v.strip()[:60]}')"
        elif kind == "count" and not re.search(r"\d", v) and not _COUNT_WORD.search(v):
            # T1 c8 (sl-329 'How many fire extinguisher cabinets (F.E.C.)?' = 'a / F.E.C.' @0.65 'supported'): a count
            # answered with text that holds no number at all (replay 900 upload files: 5 rows, all wrong)
            why = f"the question asks for a number; the answer is text with no number in it ('{v.strip()[:40]}')"
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


# --- one printed part answers a question about a total of several (T1 c3) ------------------------------------------
# X1 split-c2 (AFCEC PNG, 3 damage variants x photo): 'What is the total dormitory area on this floor across all three
# wings?' = 'DORMITORY = 6,095 SF' (one wing's row; gold 18,472) @0.70 'supported', 'How much circulation area is there
# on the second floor in total?' = 'E CIRCULATION = 1,240 SF' (gold 4,470), '… total common space, including its
# circulation …' = 'COMMON SPACE = 2,911 SF' (gold 4,479); 'total floor area of the six Humanities classrooms' =
# '3960m2' (a floor's GIA). A question that asks to add several things up, answered with ONE printed value whose label
# is not itself a total, is a part: the value stays, confidence <= PARTS_CAP, and the line says what is missing.
PARTS_CAP = 0.4
_AGG_COMMON = (r"across|combined|together|altogether|all together|in total|in all|sum of|summed|added up|"
               r"all (?:the |of the )?(?:two|three|four|five|six|seven|eight|nine|ten|\d+)\b|"
               r"all (?:the |of the )?[a-z]+s\b|both (?:the )?[a-z]+s\b|each of the|"
               r"the (?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+) [a-z]+s\b|"
               r"rooms? (?:labelled|labeled|named|called)|"
               r"yhteensä|yhteenlask\w*|kaikkien|insgesamt|zusammen\w*|aller\b|totalt|sammanlagt|sammanlagd\w*|"
               r"samtliga|alla\b|au total|ensemble|in totale|complessiv\w*|tutt[ie]\b")
_AGG = {"area": re.compile(r"\b(?:" + _AGG_COMMON + r"|including (?:its|the|their|all)|incl\.? (?:its|the)|(?<!not )(?<!without )counting (?:its|the|their|all|in)|plus (?:its|the))\b", re.I),
        "length": re.compile(r"\b(?:" + _AGG_COMMON + r")\b", re.I)}
_TOTAL_LABEL = re.compile(r"\b(?:total|totals|subtotal|sum|gross|gfa|gia|nia|nfa|gea|overall|all|combined|grand|incl\w*|"
                          r"yhteensä|summa|gesamt\w*|summe|totalt|tot)\b", re.I)


def one_part_for_total(question: str, ans: Answer) -> str | None:
    """The printed line when an area / length question asks for a total of several things and the answer is ONE printed
    value (one located mark) whose label is not a total; else None."""
    from stoa.trust import support as S

    v = ans.value
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    kind = S.asked_kind(question, ans.unit)
    if kind not in _AGG:
        return None
    m = _AGG[kind].search(question or "")
    if not m:
        return None
    marks = [e for e in ans.evidence or [] if S._located(e) and not S._AUX_NOTE.search(str(e.note or ""))]
    if len(marks) != 1:
        return None
    line = S._printed_support(question, ans)
    if line is None or _TOTAL_LABEL.search(line):
        return None
    return f"'{line.strip()[:70]}' ({m.group(0)})"


# a part answered with the printed TOTAL (T1 c4; my c3 open item): pdf_text 'What is the gross internal floor area of the
# second floor?' = 8610 @0.65, cited '3960m2 / Ground: / First: 2540m2 / 2110m2 / Second: / 8610m2' (gold 2110):
# 8610 = 3960 + 2540 + 2110, the other numbers of the same cited line. The question names one part, not the total.
_PART_TOTAL_Q = re.compile(r"\b(?:total|totals|overall|whole|entire|all|combined|altogether|together|sum|summed|both|"
                           r"each|every|building'?s?|gesamt\w*|insgesamt|yhteensä|kaikki\w*|totalt|sammanlagt)\b", re.I)


def total_for_one_part(question: str, ans: Answer) -> str | None:
    """'8610 = 3960 + 2540 + 2110' when the value equals the sum of >= 2 OTHER numbers printed in the same cited line and
    the question asks for one part (no total / all / combined word); else None."""
    from stoa.trust import support as S

    v = ans.value
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    if S.asked_kind(question, ans.unit) not in ("area", "length", "volume") or _PART_TOTAL_Q.search(question or ""):
        return None
    for line in S.cited_texts(ans):
        nums = [(n, S.role_of(line, n.start(1), n.end(1))) for n in S._NUM.finditer(line)
                if not (n.start(1) > 0 and line[n.start(1) - 1].isalpha())]  # 'm2' / 'ft2' / 'D13' are not values
        vals = [max(S._readings(n.group(1)) or {0.0}) for n, r in nums if r not in ("level", "scale", "identifier",
                                                                                  "date", "spacing", "bar")]
        eps = max(0.001 * float(v), 0.5 if float(v) >= 100 else 0.005)  # a printed total adds up exactly
        hit = [i for i, x in enumerate(vals) if abs(x - float(v)) <= eps]
        if len(hit) != 1:
            continue
        rest = [x for i, x in enumerate(vals) if i != hit[0] and x > 0]
        if len(rest) >= 2 and abs(sum(rest) - float(v)) <= eps:
            return " + ".join(f"{x:g}" for x in rest) + f" = {float(v):g}"
    return None


# skirting / perimeter = the sum of a room's sides; one printed dimension is one side (T1 c3 replay: 'How many metres
# of skirting do we need for English 1?' = '7.200' @0.75, gold 28.71; 8 such rows, 0 right)
_PERIMETER_Q = re.compile(r"skirting|baseboard|perimeter|around the (?:walls?|room)|golvlist\w*|jalkalist\w*|"
                          r"sockelleiste\w*|fu(?:ss|ß)leiste\w*|plinthe\w*|battiscopa|piiri\b|omkrets\w*|umfang\w*", re.I)
_PERIMETER_LINE = re.compile(r"perimeter|skirting|baseboard|total|overall|sum\b|omkrets|piiri|umfang", re.I)


def one_side_for_perimeter(question: str, ans: Answer) -> str | None:
    from stoa.trust import support as S

    v = ans.value
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not _PERIMETER_Q.search(question or ""):
        return None
    if S.asked_kind(question, ans.unit) != "length":
        return None
    hit = S._printed_hit(question, ans)
    if not hit or hit[1] not in ("dimension", "plain", "length") or _PERIMETER_LINE.search(hit[0]):
        return None
    return f"'{hit[0].strip()[:50]}'"


def wrong_number_of_parts(question: str, ans: Answer) -> str | None:
    """'the question names 4; the sum of the 8 marked printed values (…)' when the answer adds up marked printed values
    and their number is not the number the question names ('the four private offices' = 8 tags, MFC 1,887 SF)."""
    from stoa.trust import support as S

    named = S.named_count(question)
    if named is None:
        return None
    loc = [e for e in ans.evidence or [] if S._located(e)]
    hit = S._sum_of_marks(question, ans, loc, S.asked_kind(question, ans.unit))
    if not hit or hit[1] == named:
        return None
    return f"the question names {named}; {hit[0]}"


def parts_guard(question: str, ans: Answer) -> Answer:
    try:
        if float(ans.confidence or 0) <= PARTS_CAP:
            hit = wrong = side = total = None
        else:
            hit = one_part_for_total(question, ans)
            wrong = None if hit else wrong_number_of_parts(question, ans)
            side = None if hit or wrong else one_side_for_perimeter(question, ans)
            total = None if hit or wrong or side else total_for_one_part(question, ans)
    except Exception:  # noqa: BLE001
        return ans
    if not hit and not wrong and not side and not total:
        return ans
    from stoa.trust.support import PARTS_MISSING

    out = ans.model_copy(deep=True)
    if side:
        why = (f"{PARTS_MISSING}: the marked number {side} is one printed dimension; skirting / a perimeter is the sum "
               "of all the room's sides: measure the outline ('Ask about an area') or add the sides")
    elif hit:
        why = (f"{PARTS_MISSING}: the marked line {hit} is a single item; the question asks for the total of several: "
               "add the other parts, or check that this line is their total")
    elif total:
        why = (f"{PARTS_MISSING}: the marked number is the TOTAL of the other values printed with it ({total}); the "
               "question asks for one part: read that part's own line")
    else:
        why = f"{PARTS_MISSING}: {wrong} — check which ones belong"
    out.missing = [why, *out.missing]
    out.trace = [*out.trace, f"{TAG}: {why} -> confidence capped at {PARTS_CAP}"]
    out.confidence = PARTS_CAP
    return out


# --- a list of printed dimensions is not one length (S0 21:57, d29 wild-sealed) ----------------------------------------
# 'How deep are the private offices, from the corridor wall to the outside wall?' = '11 3/8" 2'-6" 6 5/8"' @0.65
# (pdf_text, three dimension strings; d28's raster number was right). Over 1,500 prediction files: 41 short string
# answers of 2+ dimensions not joined by 'x' / 'by' to a length / area question, 0 right. The value is withheld (None)
# so the upload solver reads the page as an image next (upload._no_reader) and a card never shows a list as the length.
DIMLIST_MISSING = "several printed dimensions, not one"
_DIM_TOK = re.compile(r"\d+\s*['’′]\s*-?\s*\d+(?:\s+\d+/\d+)?\s*(?:\"|”|″)?|\d+\s*['’′](?![\d-])|"
                      r"\d+(?:\s+\d+/\d+)?\s*(?:\"|”|″)|\d+(?:[.,]\d+)?\s*(?:mm|cm|m)\b(?![²2])")
_DIM_JOIN = re.compile(r"\s(?:x|×|by)\s|\d\s*[x×]\s*\d|[\"'’”″′]\s*[x×]\s*\d|\bwide\b|\bhigh\b|\blong\b", re.I)
_ONE_LEN_Q = re.compile(r"\bhow (?:long|wide|deep|high|tall|far|thick|big|large)\b|\b(?:length|width|depth|height|"
                        r"distance|span|thickness|perimeter|area|footprint)\b", re.I)
_MANY_Q = re.compile(r"\b(?:dimensions|sizes|lengths|widths|depths|heights|each|every|list|breakdown|analysis|"
                     r"comprehensive|by type|per\b|all the)\b", re.I)


def dim_list_guard(question: str, ans: Answer) -> Answer:
    try:
        v = ans.value
        if not isinstance(v, str) or len(v) > 60 or "\n" in v:
            return ans
        kind = _numeric_kind(question)
        if kind not in ("length", "area") or not _ONE_LEN_Q.search(question or "") or _MANY_Q.search(question or ""):
            return ans
        if kind == "area" and _AREA_CUE.search(v):
            return ans
        toks = _DIM_TOK.findall(v)
        if len(toks) < 2 or _DIM_JOIN.search(v):
            return ans
    except Exception:  # noqa: BLE001
        return ans
    noun = "one length" if kind == "length" else "an area"
    out = ans.model_copy(deep=True)
    why = (f"{DIMLIST_MISSING}: the reader returned {len(toks)} printed dimensions ('{v.strip()[:50]}'), not {noun}; "
           "measure it on the drawing (set the scale with two clicks, then click the two ends) or name the wall more "
           "exactly")
    out.trace = [*out.trace, f"{TAG}: {why} -> no number (the reading {v!r} @{ans.confidence} is kept here only)"]
    out.missing = [why, *out.missing]
    out.value, out.unit, out.confidence = None, None, 0.0
    return out


# --- a cross-reference to another sheet is not a size (T1 c4) --------------------------------------------------------
# Stoa's S-100F, live on night 3451469f: "What's the footprint of the generator set foundation?" -> pdf_text
# 'SEE A1/S-513' @0.65, card 'supported — read from the drawing's own data' (gold 26.486 m²; 3 rows in the newest 1,500
# prediction files, all wrong). The note says WHERE the size is drawn, not the size: no number, the plain sentence first,
# and the upload solver reads the page as an image next (as for dim_list_guard).
XREF_MISSING = "this sheet refers to another drawing for it"
_XREF = re.compile(r"^\s*\(?\s*(?:SEE|REFER(?:\s+TO)?|REF\.|AS\s+PER|PER\s+(?:DETAIL|DWG|DRAWING|SHEET|SHT))\b", re.I)
_XREF_WHERE = re.compile(r"\b[A-Z]{0,2}\d{1,2}\s*/\s*[A-Z]{1,3}-?\d{1,4}[A-Z]?\b|"
                         r"\b(?:SHT|SHEET|DWG|DRAWING|DETAIL|DET|SECTION|SEC)\.?\s*[A-Z]{0,3}-?\d[\w.-]*", re.I)
_SIZE_Q = re.compile(r"\bfootprint\b|\bhow (?:big|large|long|wide|deep|many|much)\b|\b(?:size|area|length|width|"
                     r"depth|dimensions?|count|number of|volume|quantity)\b", re.I)


def ref_guard(question: str, ans: Answer) -> Answer:
    try:
        v = ans.value
        if not isinstance(v, str) or len(v) > 80 or not _XREF.search(v):
            return ans
        if not (_numeric_kind(question) or _SIZE_Q.search(question or "")):
            return ans
        where = _XREF_WHERE.search(v)
    except Exception:  # noqa: BLE001
        return ans
    ref = v.strip()[:60]
    why = (f"{XREF_MISSING} ('{ref}'): the size is not printed here, only where it is drawn"
           + (f" ({where.group(0).strip()})" if where else "") + "; upload that drawing, or measure it on this one "
           "(set the scale with two clicks, then trace the outline)")
    out = ans.model_copy(deep=True)
    out.trace = [*out.trace, f"{TAG}: {why} -> no number (the reading {v!r} @{ans.confidence} is kept here only)"]
    out.missing = [why, *out.missing]
    out.value, out.unit, out.confidence = None, None, 0.0
    return out


# --- the whole drawing's overall size for a named element (T1 c4) ---------------------------------------------------
# After ref_guard, S-100F "What's the footprint of the generator set foundation?" fell to the image pass: raster's
# overall-dimension convention 27 m x 15.522 m = 419.094 m² @0.4 (the whole sheet; gold 26.486). 'footprint' names the
# building only when the question's subject is the building: a named element (foundation, slab, tank, room …) is not.
_OVERALL_TR = re.compile(r"overall dimensions: width '(?P<w>[^']+)'.*?depth '(?P<d>[^']+)'.*?product (?P<p>[\d.]+) (?P<u>\S+)")
_SUBJECT = re.compile(r"\b(?:footprint|area|size|extent|dimensions?)\s+(?:of|for)\s+(?:the\s+|this\s+|that\s+|a\s+)?"
                      r"(?P<x>[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,5}?)(?=\s*(?:\?|$|,|\(|\b(?:in|on|without|excluding|"
                      r"including|incl|excl|shown|drawn|measured)\b))|"
                      r"\bhow (?:big|large) is (?:the\s+|this\s+|that\s+)?(?P<y>[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,5}?)"
                      r"(?=\s*(?:\?|$|,|\(|\b(?:in|on|overall)\b))", re.I)
_WHOLE_SUBJ = re.compile(r"\b(?:building|house|home|bungalow|cottage|villa|cabin|cabins|structure|facility|plan|drawing|"
                         r"sheet|whole|entire|overall|outline|gebäude\w*|haus|hauses|talo\w*|rakennu\w*|maison|bâtiment|"
                         r"edificio|casa|fabbricato|byggnad\w*|huset)\b", re.I)
OVERALL_SCOPE = "this is the whole drawing's overall size"


def overall_scope_guard(question: str, ans: Answer) -> Answer:
    try:
        if not isinstance(ans.value, (int, float)) or isinstance(ans.value, bool):
            return ans
        tr = next((m for m in (_OVERALL_TR.search(str(t)) for t in ans.trace) if m), None)
        if tr is None:
            return ans
        m = _SUBJECT.search(question or "")
        x = (m.group("x") or m.group("y") or "").strip() if m else ""
        if not x or _WHOLE_SUBJ.search(x):
            return ans
    except Exception:  # noqa: BLE001
        return ans
    why = (f"{OVERALL_SCOPE} ({tr['w']} x {tr['d']} = {tr['p']} {tr['u']}), not the {x}'s: the {x} is not dimensioned "
           f"on its own here; measure it on the drawing (set the scale with two clicks, then trace its outline)")
    out = ans.model_copy(deep=True)
    out.trace = [*out.trace, f"{TAG}: {why} -> no number (the reading {ans.value!r} @{ans.confidence} is kept here only)"]
    out.missing = [why, *out.missing]
    out.value, out.unit, out.confidence = None, None, 0.0
    return out


# --- the marks are labels, but not the asked label (T1 c3) --------------------------------------------------------
# label_count 'How many rooms are labelled Plant Room on this plan?' = 7 @0.70, every mark "label 'Room'" (gold 3):
# a mark whose own label lacks a word of the asked label does not show the counted item.
_ASKED_LABEL = re.compile(r"\b(?:labell?ed|named|called|titled)\s+(?:as\s+)?['\"‘“«]?(?P<x>[A-Za-z][\w .&/-]{1,40}?)"
                          r"['\"’”»]?(?=\s+(?:are|is|on|in|there|does|do|have|shown|drawn)\b|\s*[?,]|$)", re.I)
_MARK_LABEL = re.compile(r"^label '(?P<y>[^']+)'")


def label_guard(question: str, ans: Answer) -> Answer:
    try:
        v = ans.value
        if isinstance(v, bool) or not isinstance(v, (int, float)) or float(ans.confidence or 0) <= SHAPE_CAP:
            return ans
        m = _ASKED_LABEL.search(question or "")
        if not m:
            return ans
        labels = [x.group("y") for x in (_MARK_LABEL.match(str(e.note or "")) for e in ans.evidence or []) if x]
        if not labels:
            return ans
        want = set(re.findall(r"[a-z0-9]+", m.group("x").lower()))
        bad = [y for y in labels if not want <= set(re.findall(r"[a-z0-9]+", y.lower()))]
        if not bad:
            return ans
    except Exception:  # noqa: BLE001
        return ans
    why = f"the marked labels read '{bad[0]}', not '{m.group('x').strip()}'"
    out = ans.model_copy(deep=True)
    out.trace = [*out.trace, f"{TAG}: {why} -> confidence capped at {SHAPE_CAP}"]
    out.missing = [f"{SHAPE_MISSING} ({why})", *out.missing]
    out.confidence = SHAPE_CAP
    return out


# --- a count with nothing located (captain 23:04, Ziying afcec-04) -----------------------------------------------------
# 'How many doors are in the north wing?' -> 12 @0.105 from the VLM with NO boxes (hers: 34): the expert would have to
# recount from scratch. A count no marked symbol backs is the reader's estimate, not a count. Replay (T1 c5, newest 300
# upload files): counts > 0 with no located non-auxiliary mark 75 right / 192 wrong (vlm_lora 60 / 138, agent 13 / 24,
# raster 2 / 11, prior 0 / 19); at conf >= 0.5 still 19 / 13. The value stays (a refusal scores nothing, and the guess
# is right 28 % of the time), shown as a guess: confidence <= UNCOUNTED_CAP (< web LOW_CONF 0.4 -> 'Our guess'; > 0.3 so
# upload._weak does not start a ~60 s vector trace of a PNG for it) + this sentence first in missing + support unlocated.
UNCOUNTED = "not counted (no symbols located)"
UNCOUNTED_CAP = 0.35
UNCOUNTED_KEEP = 0.9  # a reader confidence this high is kept (V1 01:30: 97.7 % right on 33,374 dataset counts)
_ITEMS = re.compile(r"\bhow\s+many\s+(?:of\s+the\s+)?(?P<x>[\w\s'/-]{2,40}?)\s+(?:are|is|were|does|do|did|can|have|has|"
                    r"there|in|on|at|of|shown|drawn|labell?ed|appear)\b", re.I)


_AUX_ASKED = re.compile(r"\b(legend|key plan|title block|schedule)\b", re.I)


def _counted_items(question: str) -> str:
    m = _ITEMS.search(question or "")
    x = m.group("x").strip() if m else ""
    return x if x and len(x.split()) <= 5 else "items"


def _estimate_source(solver: str) -> str:
    s = (solver or "").split("/")[-1].lower()
    if re.fullmatch(r"prior|dummy", s):
        return "a typical value for this kind of question"
    if re.search(r"vlm|agent|look|gemini|nemotron|vision", s):
        return "the vision model's estimate from the picture"
    if s == "compose":
        return "computed from other readings that mark nothing"
    return f"the {s or 'reader'}'s estimate"


def _asked_in_area(path: str | None) -> bool:
    """The visitor already chose a point / region (W5's ask_context via stoa.agent.click, or features.region's masked
    '….region-<key>….png/pdf' copy): advice must not send them to 'Ask about an area' again."""
    import os

    if ".region-" in os.path.basename(str(path or "")):
        return True
    try:  # an AREA (>= 3 corners), not a right-click point or a view name: those still get the region-tool advice
        from stoa.agent import click

        v = click._CTX.get()
        if v is None and os.environ.get("STOA_ASK_CONTEXT"):
            v = click.Ask.parse(os.environ["STOA_ASK_CONTEXT"])
        return isinstance(v, click.Ask) and len(v.region) >= 3
    except Exception:  # noqa: BLE001
        return False


def uncounted_guard(question: str, ans: Answer, path: str | None = None) -> Answer:
    """A count > 0 with no located object (every box is a searched area / legend / scale bar, or none at all): not shown
    as an answer. Structured model readers (IFC / DXF entities, schedules) are exempt: the number is the model's data."""
    import os

    from stoa.trust import support as S

    mode = os.environ.get("STOA_T1_UNCOUNTED", "guess").strip().lower()  # guess (default) | withhold | off
    v = ans.value
    if mode == "off" or not isinstance(v, (int, float)) or isinstance(v, bool) or not float(v).is_integer() or v <= 0:
        return ans
    if S.asked_kind(question, ans.unit) != "count" or _EXACT_SOLVER.search(str(ans.solver or "")):
        return ans
    if any(str(m).startswith(UNCOUNTED) for m in ans.missing):
        return ans
    loc = [e for e in ans.evidence or [] if S._located(e)]
    if any(not S._AUX_NOTE.search(str(e.note or "")) for e in loc):
        return ans
    asked_aux = {w.lower() for w in _AUX_ASKED.findall(question or "")}
    if asked_aux and any(w in str(e.note or "").lower() for e in loc for w in asked_aux):
        return ans  # 'How many items does the legend list?' with the legend boxed: the box IS what was counted
    n, items = int(v), _counted_items(question)
    searched = any(re.search(r"searched", str(e.note or ""), re.I) for e in loc)
    place = _scope_words(question, "")
    where = "the marked area" if searched else "the whole sheet"  # no reader crops to 'the north wing' first (yet)
    reader = (ans.solver or "").split("/")[-1] or "the reader"
    if _asked_in_area(path):
        where = "the selected area"
        tool = "count them by hand inside the selected area"
    else:
        tool = (f"count them by hand on the drawing, or select {place if place != 'this drawing' else 'the part you mean'}"
                " with 'Ask about an area' and ask again")
    sentence = (f"{UNCOUNTED}: {n} is {_estimate_source(ans.solver or '')}, not a count of marked {items}. We searched "
                f"{where} ({reader}) and located none of them; {tool}")
    out = ans.model_copy(deep=True)
    out.missing = [sentence] + [m for m in out.missing if m != sentence]
    if mode == "withhold":  # the captain's strict reading of 23:04: no number at all (the estimate stays in the trace)
        out.trace = [*out.trace, f"{TAG}: {reader} counted {n} {items} but located none of them -> no number "
                                 "(STOA_T1_UNCOUNTED=withhold)"]
        out.assumptions = [*out.assumptions, f"the reader's estimate ({n}) is not shown: nothing was located"]
        out.value, out.confidence = None, 0.0
        return out
    if out.confidence >= UNCOUNTED_KEEP:
        # V1 01:30 (full dataset, d31): 59,604 vlm_lora counts with no box were capped and were 86.6 % right; the 33,374
        # whose own calibrated confidence was >= 0.9 were 97.7 % right (uploads: 6 right / 1 wrong) -> keep it here; on an
        # upload the answer stays 'unlocated', so stoa.trust.calibrate still shows it as a guess (<= 0.39)
        out.trace = [*out.trace, f"{TAG}: {reader} counted {n} {items} but located none of them -> a guess, not a count "
                                 f"(its own calibrated confidence {out.confidence:.2f} is kept: >= {UNCOUNTED_KEEP})"]
    elif out.confidence > UNCOUNTED_CAP:
        out.trace = [*out.trace, f"{TAG}: {reader} counted {n} {items} but located none of them -> a guess, not a "
                                 f"count: confidence {out.confidence:.2f} -> {UNCOUNTED_CAP}"]
        out.confidence = UNCOUNTED_CAP
    else:
        out.trace = [*out.trace, f"{TAG}: {reader} counted {n} {items} but located none of them -> a guess, not a count"]
    return out


# --- every mark names the OPPOSITE of what was asked (D1 23:22) -------------------------------------------------------
# 'total ROOF catchment area of the two rainshed buildings' = 128,878 + 51,000 SF from 'LINED GROUND … GC1' + 'INSTALL
# LINED GROUND CATCHMENT - GC2' @0.70 (gold 86,157). Replay T1 c5 over 700 prediction files: every-mark contradictions
# 1 wrong / 0 right; some-mark ones (a view title 'EXISTING … PLAN' for a 'new' item) 3 wrong, left alone (a view title
# is where, not what).
OPPOSITE = "the marked lines name a different thing"
_PAIRS = (("roof", "ground"), ("internal", "external"), ("interior", "exterior"), ("existing", "new"),
          ("existing", "proposed"), ("upper", "lower"), ("inside", "outside"))


def opposite_guard(question: str, ans: Answer) -> Answer:
    from stoa.trust import support as S

    if ans.value is None or isinstance(ans.value, bool) or not isinstance(ans.value, (int, float)):
        return ans
    if S.asked_kind(question, ans.unit) not in ("area", "length", "volume"):
        return ans
    marks = [e for e in ans.evidence or [] if S._located(e) and not S._AUX_NOTE.search(str(e.note or ""))]
    if not marks or any(str(m).startswith(OPPOSITE) for m in ans.missing):
        return ans
    q = (question or "").lower()

    def has(w: str, t: str) -> bool:
        return bool(re.search(rf"\b{w}\b", t))

    for x, y in _PAIRS:
        for want, opp in ((x, y), (y, x)):
            if not has(want, q) or has(opp, q):
                continue
            notes = [str(e.note or "").lower() for e in marks]
            if all(has(opp, n) and not has(want, n) for n in notes):
                out = ans.model_copy(deep=True)
                out.missing = [f"{OPPOSITE}: the question asks for the {want} one, every marked line says {opp.upper()} "
                               f"('{str(marks[0].note)[:50]}') — check which printed values belong", *out.missing]
                if out.confidence > PARTS_CAP:
                    out.trace = [*out.trace, f"{TAG}: every mark says '{opp}', the question asks '{want}' -> confidence "
                                             f"{out.confidence:.2f} -> {PARTS_CAP}"]
                    out.confidence = PARTS_CAP
                return out
    return ans


# --- a part question answered from the sheet's WHOLE-building line (T1 c6) -------------------------------------------
# EFA C2 scan 'According to the GIFA table, how big is the second floor?' -> area_tags 4055 m² @0.80 from 'Overall GIFA:
# 4055m²' (gold 2253 = the table's 'Second: 2253m²' row; 3 upload rows). Replay T1 c6 over 800 prediction files: 1 wrong /
# 0 right. Left alone: a line naming a part ('Overall GIFA Second: 2253m 2', right) and questions asking a total
# themselves ('How large is the total common space on the third floor?' = 'TOTAL COMMON SPACE = 4,499 SF', right).
_WHOLE_LINE = re.compile(r"\b(?:overall|total|grand\s+total|building\s+total)\b", re.I)
_PART_WORD = re.compile(r"\b(?:ground|first|second|third|fourth|fifth|sixth|basement|cellar|attic|mezzanine|level|floor|"
                        r"storey|story|flr|lvl|wing|block|north|south|east|west|[nsew]\.?\s?wing|\d+(?:st|nd|rd|th)|"
                        r"[0-9]{1,2}\s*(?:krs|og|eg))\b", re.I)
_TOTAL_Q = re.compile(r"\b(?:total|overall|whole|entire|all|combined|altogether|together|sum|building'?s)\b", re.I)


def whole_guard(question: str, ans: Answer) -> Answer:
    from stoa.trust import support as S

    if ans.value is None or isinstance(ans.value, bool) or not isinstance(ans.value, (int, float)):
        return ans
    if S.asked_kind(question, ans.unit) not in ("area", "length", "volume", "count"):
        return ans
    m = S._QUAL_Q.search(question or "")
    if not m or _TOTAL_Q.search(question or "") or any(str(x).startswith(OPPOSITE) for x in ans.missing):
        return ans
    hit = S._printed_hit(question, ans)
    if not hit:
        return ans
    line = hit[0].strip()
    q = re.search(r"\btext '(.+?)'", line)  # the agent's '<sub-question> — text '6,285 SF'' notes: the quoted text only
    line = q.group(1).strip() if q else line.split(" — ")[-1].strip()
    if not _WHOLE_LINE.search(line) or _PART_WORD.search(line):
        return ans
    part = m.group(0).lower()
    out = ans.model_copy(deep=True)
    out.missing = [f"{OPPOSITE}: the question asks for the {part}; the marked line is the whole building's total "
                   f"('{line[:50]}') — check the printed row for the {part}", *out.missing]
    if out.confidence > PARTS_CAP:
        out.trace = [*out.trace, f"{TAG}: the {part} answered from a whole-building line ('{line[:40]}') -> confidence "
                                 f"{out.confidence:.2f} -> {PARTS_CAP}"]
        out.confidence = PARTS_CAP
    return out


def sanity_guard(question: str, ans: Answer, path: str | None = None) -> Answer:
    """Pipeline.solve, after final_guard: Ziying's two rules, the lost-scope check, text answers of the wrong shape, one
    printed part for a total."""
    try:
        ans = label_guard(question, parts_guard(question, text_shape_guard(question, ref_guard(question, dim_list_guard(
            question, overall_scope_guard(question, scope_guard(question, zero_guard(question, range_guard(ans, path), path))))))))
    except Exception:  # noqa: BLE001
        return ans
    try:  # xr-40 FIRST (T1 c6): a count the sheet itself prints marks its printed line, so it is no 'uncounted' guess
        from stoa.trust import printedcount

        ans = printedcount.check(question, ans, path)
    except Exception:  # noqa: BLE001
        pass
    try:  # captain 23:04: a count with no located object is a guess, not a count
        ans = uncounted_guard(question, ans, path)
    except Exception:  # noqa: BLE001
        pass
    try:  # D1 23:22: 'roof catchment' summed from GROUND catchment lines
        ans = opposite_guard(question, ans)
    except Exception:  # noqa: BLE001
        pass
    try:  # T1 c6: 'the second floor' answered from the sheet's 'Overall GIFA' line
        ans = whole_guard(question, ans)
    except Exception:  # noqa: BLE001
        pass
    try:  # expert rule xr-36: the printed dimension strings used must add up to the printed overall
        from stoa.trust import chaincheck

        return chaincheck.check(ans, path)
    except Exception:  # noqa: BLE001
        return ans


__all__ = ["whole_guard", "DIMLIST_MISSING", "OVERALL_SCOPE", "overall_scope_guard", "XREF_MISSING", "ref_guard", "dim_list_guard", "label_guard", "PARTS_CAP", "one_part_for_total", "total_for_one_part", "parts_guard", "wrong_number_of_parts", "NONE_FOUND", "SCOPE_LOST", "flagged", "flagged_zero", "in_cc_domain", "range_guard", "sanity_guard",
           "scope_guard", "text_shape_guard", "zero_guard", "zero_scope"]
