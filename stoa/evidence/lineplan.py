"""Room areas and lengths on LINE-ONLY CAD sheets (E1, captain 14:39; X1 wild 14:31).

A sheet whose rooms are drawn as wall lines only (no room fills, no CAD twin, no printed areas) with a printed '1:N'
scale: every reader abstained ('no room polygons') and the prior answered. Here the asked room is found

  1. by its printed LABEL (stacked text lines merged: 'CONFERENCE' / 'ROOM'), the question's words matched to the
     label's words; several equal labels ('MEETING ROOM' x4) need a position word to pick one;
  2. or by its POSITION ('the room at the top right', 'the second room from the top on the left side',
     'das dritte Zimmer von oben auf der linken Seite') among the closed rooms traced on the plan,

then MEASURED with stoa.evidence.linework.measure_room around that point at the printed scale (traced from the wall
lines, door openings closed, line thickness corrected) and answered with the traced outline as Evidence.polygon
(area) or the measured side as Evidence.polyline (length / width), exact=False and an approximate confidence.

    from stoa.evidence.lineplan import answer
    answer(pdf_path, question) -> Answer | None    (None = not this kind of sheet / question; never raises)

Called from plan_rooms when it finds no filled room polygons (stoa/solvers/plan_rooms.py, after ifcplan_pdf).
"""

from __future__ import annotations

import functools
import os
import re

from stoa.contract import Answer, Evidence

SOLVER = "lineplan"
# D1 23:52 / H 23:51 sum wordings (E1_PHRASE_SUM=0: the 23:55 behaviour)
PHRASE_SUM = os.environ.get("E1_PHRASE_SUM", "1") != "0"
WALLS_FALLBACK = os.environ.get("E1_WALLS_FALLBACK", "1") != "0"  # walls-only trace when every line reading fails
_AREA_Q = re.compile(r"\b(area|how big|how large|size|floor space|square (?:met\w*|feet|foot)|quadratmet\w*|qm|m2|m²|sq\.? ?m|fläche|"
                     r"grundfläche|wohnfläche|wie gro(?:ß|ss)|groß|gross|oppervlak\w*|hoe groot|surface|superficie|"
                     r"pinta-ala|kuinka suuri|yta|hur stor|areal)\b", re.I)
_PERIM_Q = re.compile(r"\b(perimeter|circumference|umfang|omtrek|périmètre|perimetre|perimetro|piiri|omkrets)\b", re.I)
_LEN_Q = re.compile(r"\b(how long|length|long side|longer side|longest side|wall to wall|wie lang|länge|lengte|"
                    r"hoe lang|longueur|lunghezza|pituus|kuinka pitkä|längd|hur lång)\b", re.I)
_WID_Q = re.compile(r"\b(how wide|width|short side|shorter side|wie breit|breite|breedte|hoe breed|largeur|"
                    r"larghezza|leveys|kuinka leveä|bredd|hur bred)\b", re.I)
_WALL_Q = re.compile(r"\b(walls?|\w*wand|\w*wände|mur|murs|muri|\w*seinä\w*|\w*vägg\w*|\w*muur)\b", re.I)
_ROOM_Q = re.compile(r"\b(rooms?|\w*raum|\w*räume|\w*zimmer|kamers?|\w*kamer|pièces?|stanz[ae]|huone\w*|offices?|"
                     r"kitchens?|küchen?|bath\w*|bed\w*|living|wohn\w*|schlaf\w*|lobb(?:y|ies)|halls?|foyers?|stores?|"
                     r"storage|garages?|studios?|suites?|units?|apartments?|wohnungen?|flats?|wcs?|toilets?|"
                     r"laundr(?:y|ies)|closets?|corridors?|terraces?|balcon(?:y|ies)|cellars?|keller\w*)\b", re.I)
_NOT_ROOM = re.compile(r"\b(thick\w*|dick\w*|stair\w*|treppe\w*|steps?|"
                       r"door\w*|tür\w*|window\w*|fenster\w*|building|gebäude|site|plot|grundstück|height|high|hoch|"
                       r"höhe|ceiling|decke|apartment|wohnung|flat|unit|storey|floor area of the (?:whole|entire)|gross|footprint|"
                       r"brutto\w*|outside (?:of the )?(?:wall|dimension)\w*|outer (?:dimension|face)s?|"
                       r"(?:area|perimeter|size) of the (?:\w+ )?(?:floor|level|storey)|"
                       r"whole (?:floor|level|plan|house)|entire (?:floor|level|plan|house))\b",
                       re.I)
_COUNT_Q = re.compile(r"\b(how many|number of|count|wie viele|anzahl|hoeveel|combien|quanti|kuinka monta|hur många)\b"
                      r"(?!\s+(?:square|sq\.?|m²|m2|qm|quadrat\w*|neliö\w*|vierkante|mètres? carrés|metri quadrati|"
                      r"kvadrat\w*|feet|ft|metres|meters|metre|meter)\b)", re.I)  # 'how many square metres' = an area
_SUM_Q = re.compile(r"\b(total|all|sum|together|combined|gesamt\w*|alle[rn]?|insgesamt|zusammen|samen|totaal|"
                    r"yhteensä|totalt)\b", re.I)
# words that never name a room on their own
_GENERIC = {"room", "rooms", "raum", "räume", "zimmer", "kamer", "pièce", "stanza", "huone", "rum", "space", "area",
            "the", "this", "that", "what", "which", "how", "big", "large", "long", "wide", "size", "floor", "plan",
            "level", "sheet", "drawing", "its", "side", "longer", "shorter", "wall", "inside", "measured", "along",
            "outer", "with", "and", "from", "der", "die", "das", "des", "dem", "den", "ist", "wie", "groß", "gross",
            "lang", "breit", "von", "auf", "seite", "het", "van", "een", "hoe", "groot", "est", "quelle", "surface",
            "square", "metres", "meters", "metre", "meter", "feet", "one", "there", "here", "where", "located",
            # 'all rooms labelled X' (D1 23:11: dfe26 'labelled X' rows 4 -> 8 / 18 with these)
            "labelled", "labeled", "named", "called", "marked", "tagged", "titled"}
if PHRASE_SUM:
    _GENERIC |= {"areas", "spaces"}  # H 23:51: 'Sum of the floor areas of all X' / 'all spaces named X'
    _AREA_Q = re.compile(_AREA_Q.pattern.replace(r"\b(area|", r"\b(areas?|", 1), re.I)
_TITLE_WORDS = re.compile(r"\b(projekt|project|bauherr|client|architekt|architect|ma(?:ß|ss)stab|scale|datum|date|"
                          r"drawn|checked|dateiname|file ?name|sheet|drawing no|rev(?:ision)?|job|title|plan ?nr|"
                          r"blatt|gezeichnet|geprüft|opdrachtgever|schaal|datum)\b", re.I)
_TOP = re.compile(r"\b(top|upper|uppermost|topmost|north(?:ern)?|oben|obere\w*|oberste\w*|boven\w*|haut|en haut|"
                  r"yl[äa]\w*|övre)\b", re.I)
_BOTTOM = re.compile(r"\b(bottom|lower|lowest|south(?:ern)?|unten|untere\w*|unterste\w*|onder\w*|bas|en bas|"
                     r"al[äa]\w*|nedre)\b", re.I)
_LEFT = re.compile(r"\b(left|leftmost|west(?:ern)?|links|linke\w*|linker|gauche|vasen\w*|vänster|sinistra)\b", re.I)
_RIGHT = re.compile(r"\b(right|rightmost|east(?:ern)?|rechts|rechte\w*|rechter|droite|oikea\w*|höger|destra)\b", re.I)
_ORD = [(re.compile(r"\b(first|1st|erste\w*|eerste|premi[eè]re?|ensimmäi\w*)\b", re.I), 1),
        (re.compile(r"\b(second|2nd|zweite\w*|tweede|deuxième|toinen|andra)\b", re.I), 2),
        (re.compile(r"\b(third|3rd|dritte\w*|derde|troisième|kolmas|tredje)\b", re.I), 3),
        (re.compile(r"\b(fourth|4th|vierte\w*|vierde|quatrième|neljäs|fjärde)\b", re.I), 4),
        (re.compile(r"\b(fifth|5th|fünfte\w*|vijfde|cinquième|viides|femte)\b", re.I), 5)]
_FROM_TOP = re.compile(r"\b(from the top|von oben|van boven|depuis le haut|ylhäältä)\b", re.I)
_FROM_BOTTOM = re.compile(r"\b(from the bottom|von unten|van onder|depuis le bas|alhaalta)\b", re.I)
_FROM_LEFT = re.compile(r"\b(from the left|von links|van links|depuis la gauche|vasemmalta)\b", re.I)
_FROM_RIGHT = re.compile(r"\b(from the right|von rechts|van rechts|depuis la droite|oikealta)\b", re.I)


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", s.lower()) if len(w) >= 3]


# X1 01:05: abbreviated room labels ('COMMS ROOM', 'BED 2', 'KIT', 'LDRY') name the spelled-out room of a question
# ('the communications room', 'bedroom 2') and the other way round. One canonical word per room noun, both sides.
_CANON_GROUPS = (
    ("comms", "comm", "communication", "communications", "telecom", "telecoms"),
    ("bedroom", "bed", "bedrm", "beds", "bedrooms", "schlafzimmer", "makuuhuone", "sovrum", "slaapkamer", "chambre"),
    ("kitchen", "kitch", "kitchens", "küche", "kueche", "keittiö", "keittio", "kök", "keuken", "cuisine"),
    ("laundry", "ldry", "lndry", "waschküche", "buanderie", "kodinhoitohuone", "tvättstuga"),
    ("bathroom", "bath", "bth", "bathrm", "badezimmer", "kylpyhuone", "badrum", "badkamer"),
    ("ensuite", "ens"),
    ("living", "liv", "lvg"),
    ("dining", "din", "dng"),
    ("family", "fam"),
    ("meeting", "mtg"),
    ("conference", "conf"),
    ("mechanical", "mech"),
    ("electrical", "elec", "elect"),
    ("corridor", "corr"),
    ("reception", "recep", "rcpt"),
    ("laboratory", "lab", "labs"),
    ("office", "ofc", "offc", "offices"),
    ("storage", "stor", "store", "stores"),
    ("master", "mstr"),
    ("garage", "grg"),
)
_CANON = {w: g[0] for g in _CANON_GROUPS for w in g}


EXACT_FIRST = bool(int(__import__("os").environ.get("E1_EXACT_FIRST", "1")))


def _plural_twin(a: str, b: str) -> bool:
    lo, hi = sorted((a, b), key=len)
    return hi in (lo + "s", lo + "es") or (lo.endswith("y") and hi == lo[:-1] + "ies")


def _same(a: str, b: str, canon: bool = False) -> bool:
    if a == b:
        return True
    if canon and a in _CANON and _CANON.get(a) == _CANON.get(b):
        return True
    n = min(len(a), len(b))
    if n >= 4 and (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 3:
        return True
    return OCR_FOLD and len(a) == len(b) >= 4 and _ocr_twin(a, b)


OCR_FOLD = bool(int(__import__("os").environ.get("E1_OCR_FOLD", "1")))
CANON_WORDS = bool(int(__import__("os").environ.get("E1_CANON_WORDS", "1")))
_OCR_PAIRS = {frozenset("il"), frozenset("ij"), frozenset("ce"), frozenset("uv")}


def _ocr_twin(a: str, b: str) -> bool:
    """X1 00:00 / 00:27: an OCR'd scan label 'Main Hail' is the question's 'main hall' (one letter OCR confuses: i/l,
    i/j, c/e, u/v), so 'Maini' (a prefix hit, no miss) no longer outscores it -- 4.78 m² pocket vs the hall"""
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diff) == 1 and frozenset(diff[0]) in _OCR_PAIRS


def _label_words(text: str) -> list[str]:
    """_words of a label, '!' / '|' inside or after a word read as 'l' (OCR: 'Sport Ha!!' = 'Sport Hall')"""
    if OCR_FOLD:
        text = re.sub(r"(?<=[^\W\d_])[!|]+", lambda m: "l" * len(m.group()), text)
    return _words(text)


def _compound(lw: str, qw: list[str]) -> set[str]:
    """Question words a run-together label word is made of ('masterbath' -> {'master', 'bathroom'}), else empty."""
    if len(lw) < 7:
        return set()
    for w1 in qw:
        if len(w1) >= 4 and lw.startswith(w1):
            rem = lw[len(w1):]
            for w2 in qw:
                if w2 != w1 and len(rem) >= 3 and (w2.startswith(rem) or _same(w2, rem)):
                    return {w1, w2}
    return set()


def _frame(d: tuple) -> tuple[float, float]:
    """A line's writing direction snapped to the nearest axis: (1, 0) / (0, -1) / (-1, 0) / (0, 1)."""
    dx, dy = d
    return ((1.0 if dx > 0 else -1.0), 0.0) if abs(dx) >= abs(dy) else (0.0, (1.0 if dy > 0 else -1.0))


def _to_frame(b, d) -> list[float]:
    """bbox (page) -> bbox in the line's READING frame (u along the text, v towards the next line down)."""
    c, s = d
    pts = [(c * x + s * y, -s * x + c * y) for x in (b[0], b[2]) for y in (b[1], b[3])]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


def _from_frame(b, d) -> list[float]:
    c, s = d
    pts = [(c * u - s * v, s * u + c * v) for u in (b[0], b[2]) for v in (b[1], b[3])]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


# D1 01:4x: DfE Primary L00 prints 'Servery' / 'and' / 'Furniture' / 'Store' on FOUR lines; with 3 the last line was a
# 'Store' of its own (rooms labelled 'Store': 8 vs 7)
LABEL_LINES = int(__import__("os").environ.get("E1_LABEL_LINES", "4"))


def labels(pdf_abs: str, page: int = 0, min_letters: int = 3) -> list[dict]:
    """Room-name labels: text lines with letters, vertically stacked lines of one label merged ('CONFERENCE' over
    'ROOM'). Stacking runs in each line's own reading frame, so labels drawn upwards (a sheet plotted with /Rotate 90:
    'Sports Program' / 'Manager Office' side by side in page x) merge too. [{"text", "words", "bbox" (pdf_pt),
    "pt" (centre)}]."""
    from stoa.evidence.linework import text_lines_dir

    lines = []
    for text, b, d in text_lines_dir(pdf_abs, page):
        if not re.search(r"[^\W\d_]{%d,}" % min_letters, text) or _TITLE_WORDS.search(text) or len(text) > 40:
            continue
        f = _frame(d)
        lines.append({"text": text, "bbox": _to_frame(b, f), "f": f})
    lines.sort(key=lambda d: (d["f"], d["bbox"][1], d["bbox"][0]))
    out: list[dict] = []
    for ln in lines:
        x0, y0, x1, y1 = ln["bbox"]
        h = max(1.0, y1 - y0)
        cx = (x0 + x1) / 2
        hit = None
        for lab in out:
            if lab["f"] != ln["f"]:
                continue
            X0, Y0, X1, Y1 = lab["bbox"]
            if -0.5 * h <= y0 - Y1 <= 0.8 * h and y0 > Y0 and abs(cx - (X0 + X1) / 2) <= 0.25 * max(X1 - X0, x1 - x0) + 2 \
                    and lab["n"] < LABEL_LINES and abs((Y1 - Y0) / lab["n"] - h) <= 0.35 * h:
                hit = lab
                break
        if hit is None:
            out.append({"text": ln["text"], "bbox": [x0, y0, x1, y1], "n": 1, "f": ln["f"]})
        else:
            hit["text"] += " " + ln["text"]
            hit["bbox"] = [min(hit["bbox"][0], x0), hit["bbox"][1], max(hit["bbox"][2], x1), y1]
            hit["n"] += 1
    for lab in out:
        lab["line_h"] = (lab["bbox"][3] - lab["bbox"][1]) / lab["n"]  # one text line's height (stacked labels: n lines)
        lab["bbox"] = _from_frame(lab["bbox"], lab.pop("f"))
        lab["words"] = _label_words(lab["text"])
        X0, Y0, X1, Y1 = lab["bbox"]
        lab["pt"] = ((X0 + X1) / 2, (Y0 + Y1) / 2)
    out.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))
    return out


_VIEW_RX = re.compile(r"\b(floor ?plan|rcp|reflected ceiling(?: plan)?|ceiling plan|furniture plan|finish(?:es)? plan|"
                      r"power plan|lighting plan|electrical plan|plumbing plan|mechanical plan|demolition plan|"
                      r"grundriss|deckenspiegel|pohjapiirustus|plattegrond)\b", re.I)
_FLOOR_VIEW = re.compile(r"\b(floor ?plan|grundriss|pohjapiirustus|plattegrond)\b", re.I)
FLOOR_VIEWS = bool(int(__import__("os").environ.get("E1_FLOOR_VIEWS", "1")))
DOOR_CLUSTER = bool(int(__import__("os").environ.get("E1_DOOR_CLUSTER", "1")))
_OPEN_ZONE = re.compile(r"\b(lobby|foyer|waiting|workstations?|circulation|corridors?|reception|lounge|zone|open|area|"
                        r"flur|gang|eingang\w*|aula|käytävä)\b", re.I)


def floor_plan_labels(pdf_abs: str, page: int, labs: list[dict]) -> list[dict]:
    """A sheet that draws the same rooms in a FLOOR PLAN and in other views (US module sets: '… - FLOOR PLAN' next to
    '… - RCP', every room tag printed in both): keep the labels of the floor-plan views only, so a room is measured
    once (the RCP's ceiling grid slices rooms; sums and counts would double). A label belongs to the view title printed
    nearest BELOW it (in the titles' reading frame) whose left end is at most ~80 pt right of the label. Sheets
    without both kinds of view titles are unchanged."""
    if not FLOOR_VIEWS or not labs:
        return labs
    from stoa.evidence.linework import text_lines_dir

    titles = [(t, b, _frame(d)) for t, b, d in text_lines_dir(pdf_abs, page) if len(t) <= 90 and _VIEW_RX.search(t)]
    if {bool(_FLOOR_VIEW.search(t)) for t, _, _ in titles} != {True, False}:
        return labs
    frames = [f for _, _, f in titles]
    f = max(set(frames), key=frames.count)
    tb = [(_to_frame(b, f), bool(_FLOOR_VIEW.search(t))) for t, b, fr in titles if fr == f]
    keep = []
    for lab in labs:
        u0, v0, u1, v1 = _to_frame(lab["bbox"], f)
        u = (u0 + u1) / 2
        below = [(tv0, tu0, fl) for (tu0, tv0, _, _), fl in tb if tv0 >= v1 - 2 and tu0 <= u + 80]
        if not below:
            keep.append(lab)
            continue
        vmin = min(x[0] for x in below)
        if max((x for x in below if x[0] <= vmin + 40), key=lambda x: x[1])[2]:
            keep.append(lab)
    return keep or labs


def _twin_check(pdf: str, page: int, n: float, pick: dict, m: dict, others: list[dict]):
    """The floor-plan room's tag printed again in another view of the sheet (an RCP): trace each twin. A twin that traces
    to a different size (> 10 %) means the outline is not the room's (open-plan zones bounded by room separation lines
    that are not drawn: AF module 'Lobby' 208 vs 569 SF, 'Staff Workstations' 1120 vs 702) -> False (abstain, as two
    disagreeing copies did before the views were told apart). Agreeing twins -> a note; none traced -> None."""
    twins = [lab for lab in others if lab["text"].lower() == pick["text"].lower()][:3]
    vals = []
    for lab in twins:
        t = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]), lab.get("line_h"))
        if t is not None:
            vals.append(t["area_m2"])
    if not vals:
        return None
    a = m["area_m2"]
    if any(not 0.9 * a <= v <= 1.1 * a for v in vals):
        return False
    return (f"the same room tag in another view of the sheet (ceiling plan) traces to "
            f"{', '.join(f'{v:.2f}' for v in vals)} m² ✓")


def _wall_twins(pdf: str, page: int, n: float, pick: dict, others: list[dict]):
    """The floor plan and its ceiling-plan twin disagree (_twin_check False). Furniture, a kitchen counter or millwork
    drawn in the FLOOR plan only is the usual cause (AFCEE A-101 Break Room: floor-plan trace 9.48 m² stops at the
    counter front, RCP 11.53, gold 11.59 to the wall faces). The floor-plan room traced again from its WALLS alone (wall
    fills + bridged collinear openings + door radii, linework 'wall@N'): accepted when it agrees within WALL_TWIN_TOL
    with the ceiling plan's own LINE trace (an independent reading: the RCP draws no furniture) -> (m, note); else None
    (abstain, as before). A walls-only trace of the RCP copy is NOT a witness: it reads the same walls (D1's AF probe:
    5 such 'agreements', all wrong — Group Exercise A 226 vs 2958 SF). Only on sheets with filled walls."""
    if not WALL_TWINS:
        return None
    twins = [lab for lab in others if lab["text"].lower() == pick["text"].lower()][:3]
    if not twins:
        return None
    m = _measure(pdf, page, pick["pt"], n, tuple(pick["bbox"]), pick.get("line_h"), base_variants=[f"wall@{n:g}"])
    if m is None or m.get("support", 0) < 2:
        return None
    vals = []
    for lab in twins:
        t = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]), lab.get("line_h"))
        if t is not None:
            vals.append(t["area_m2"])
    a = m["area_m2"]
    if not vals or any(abs(v - a) > WALL_TWIN_TOL * a for v in vals):
        return None
    return m, ("traced from the WALLS only (wall fills, openings bridged between collinear wall ends; furniture and "
               "built-in lines ignored): the floor plan's line trace disagreed with the ceiling plan's copy; its "
               f"walls-only trace agrees with it ({a:.2f} vs {', '.join(f'{v:.2f}' for v in vals)} m²)")


WALL_TWINS = bool(int(__import__("os").environ.get("E1_WALL_TWINS", "1")))
WALL_TWIN_TOL = 0.04


QUALIFIER_CLASH = bool(int(__import__("os").environ.get("E1_QUAL_CLASH", "1")))
_HEADS = {"hall", "halls", "store", "stores", "storage", "office", "offices", "toilet", "toilets", "studio", "studios",
          "lab", "labs", "laboratory", "classroom", "classrooms", "kitchen", "lobby", "corridor", "bedroom", "bathroom",
          "cupboard", "changing", "workroom", "library", "gym", "court"}


def _qual_clash(question: str, lab_words: list) -> bool:
    """X1 23:20: 'the sports hall' must not match a label 'Main Hall Void Above' (or 'the dining hall' 'Sports Hall'):
    the question and the label put DIFFERENT qualifiers in front of the same head noun, and the question's qualifier is
    nowhere in the label. 'the hall' / 'main hall skirting' vs 'Main Hall Void Above' do not clash."""
    qs = _words(question)
    lw = [w.lower() for w in lab_words]
    for i, h in enumerate(qs):
        if h not in _HEADS or i == 0 or h not in lw:
            continue
        qp = qs[i - 1]
        j = lw.index(h)
        if j == 0 or qp in _GENERIC or qp in _QWORDS:
            continue
        lp = lw[j - 1]
        if lp in _GENERIC or _same(qp, lp) or any(_same(qp, x) for x in lw):
            continue
        return True
    return False


def _match_labels(labs: list[dict], question: str, canon: bool = False) -> list[dict]:
    pos_rx = [_TOP, _BOTTOM, _LEFT, _RIGHT, *(rx for rx, _ in _ORD)]
    qw = [w for w in _words(question) if w not in _GENERIC and not any(rx.fullmatch(w) for rx in pos_rx)]
    if not qw:
        return []
    scored = []
    for lab in labs:
        joined = {w for lw in lab["words"] for w in _compound(lw, qw)}  # 'MASTERBATH' = 'master' + 'bath(room)'
        hit = sum(1 for w in qw if w in joined or any(_same(w, lw, canon) for lw in lab["words"]))
        miss = sum(1 for lw in lab["words"] if lw not in _GENERIC and not any(_same(w, lw, canon) for w in qw)
                   and not _compound(lw, qw))
        # DR 02:50: 'the Servery' hit 'Server Room' ('server' a prefix of 'servery') as well as 'Servery and Furniture
        # Store', and the fewer extra words won; a word matched only through a prefix that is not a plural fold ranks
        # below an exact match with as many hits
        loose = sum(1 for w in qw if w not in joined and not any(w == lw or _plural_twin(w, lw) for lw in lab["words"])
                    and any(_same(w, lw, canon) for lw in lab["words"])) if EXACT_FIRST else 0
        if hit and not (QUALIFIER_CLASH and _qual_clash(question, lab["words"])):
            scored.append((hit, -loose, -miss, lab))
    if not scored:
        # X1 01:05: no label holds the question's words as written: the abbreviations of its room nouns ('BED 2' for
        # 'bedroom 2', 'COMMS ROOM' for 'communications room', 'LDRY'); only as a fallback -- Level 11 prints BOTH
        # 'COMMS ROOM' and a 'COMMUNICATION ROOM' monitor label, and 'the comms room' must stay the former
        return _match_labels(labs, question, True) if CANON_WORDS and not canon else []
    top = max((h, x, m) for h, x, m, _ in scored)
    best = [lab for h, x, m, lab in scored if (h, x, m) == top]
    # 'bedroom 1' / 'Zimmer 2': the number written after the room word picks 'BEDROOM1' / 'BED ROOM 1' among the ties
    nums = re.findall(r"[^\W\d_]{3,}\s*(\d{1,2})\b(?!\s*(?:m|cm|mm|ft|%|°|\.\d))", question)
    if nums and len(best) > 1:
        byn = [lab for lab in best if any(re.search(rf"(?<!\d)0*{n}(?!\d)", lab["text"]) for n in nums)]
        if byn:
            return byn
    if nums and NUM_PICK:
        # T1 02:01 (EFA c2 'Art 1, Art 2 and Art 3'): 'Art 1 - Small' has one word more than 'Art 2' / 'Art 3 - 3D', so
        # it never reached the ties and 'Art 1' matched the other two; when no best label carries the asked number,
        # the labels with as many word hits that DO carry it win (fewest extra words first)
        if not any(re.search(rf"(?<!\d)0*{n}(?!\d)", lab["text"]) for lab in best for n in nums):
            same = [(m, lab) for h, x, m, lab in scored if h == top[0] and
                    any(re.search(rf"(?<!\d)0*{n}(?!\d)", lab["text"]) for n in nums)]
            if same:
                mm = max(m for m, _ in same)
                return [lab for m, lab in same if m == mm]
    # 'Classroom Area A' / 'Group Exercise B' (AF module plans): a capital letter after the room word picks among ties
    lets = re.findall(r"[^\W\d_]{3,}\s+([A-Z])(?=\s*(?:[?.,;:)]|$|\s+(?:and|or|on|in|at|und|oder)\b))", question)
    if lets and len(best) > 1:
        byl = [lab for lab in best if any(re.search(rf"(?<![^\W_]){c}(?![^\W_])", lab["text"]) for c in lets)]
        if 0 < len(byl) < len(best):
            return byl
    return best


def _position(question: str) -> dict | None:
    q = question.lower()
    v = "top" if _TOP.search(q) else "bottom" if _BOTTOM.search(q) else None
    h = "left" if _LEFT.search(q) else "right" if _RIGHT.search(q) else None
    k = next((n for rx, n in _ORD if rx.search(q)), None)
    order = ("top" if _FROM_TOP.search(q) else "bottom" if _FROM_BOTTOM.search(q) else
             "left" if _FROM_LEFT.search(q) else "right" if _FROM_RIGHT.search(q) else None)
    if v is None and h is None:
        return None
    if order in ("top", "bottom") and v == order:
        v = None
    if order in ("left", "right") and h == order:
        h = None
    return {"v": v, "h": h, "k": k, "order": order}


def _pick(items: list[dict], pos: dict) -> tuple[dict | None, str]:
    """items: [{"pt": (x, y), "bbox": [x0, y0, x1, y1]}] in pdf_pt (y down). The one the position words name."""
    if not items:
        return None, "no rooms"
    X0 = min(i["bbox"][0] for i in items)
    Y0 = min(i["bbox"][1] for i in items)
    X1 = max(i["bbox"][2] for i in items)
    Y1 = max(i["bbox"][3] for i in items)
    W, H = max(1e-6, X1 - X0), max(1e-6, Y1 - Y0)

    def uv(i):
        return (i["pt"][0] - X0) / W, (i["pt"][1] - Y0) / H

    v, h, k, order = pos["v"], pos["h"], pos["k"], pos["order"]
    if order:
        pool = items
        if h == "left":
            pool = [i for i in pool if uv(i)[0] < 0.4]
        elif h == "right":
            pool = [i for i in pool if uv(i)[0] > 0.6]
        if v == "top":
            pool = [i for i in pool if uv(i)[1] < 0.4]
        elif v == "bottom":
            pool = [i for i in pool if uv(i)[1] > 0.6]
        key = {"top": lambda i: uv(i)[1], "bottom": lambda i: -uv(i)[1], "left": lambda i: uv(i)[0],
               "right": lambda i: -uv(i)[0]}[order]
        pool = sorted(pool, key=key)
        n = k or 1
        if len(pool) < n:
            return None, f"only {len(pool)} rooms on that side"
        side = " ".join(x for x in (v, h) if x)
        return pool[n - 1], f"room no. {n} from the {order}" + (f" on the {side} side" if side else "") + \
            f" (of {len(pool)} there)"
    cu = 0.0 if h == "left" else 1.0 if h == "right" else None
    cv = 0.0 if v == "top" else 1.0 if v == "bottom" else None

    def dist(i):
        u, w = uv(i)
        return ((u - cu) ** 2 if cu is not None else 0.0) + ((w - cv) ** 2 if cv is not None else 0.0)

    ranked = sorted(items, key=dist)
    if k and k > 1:
        if len(ranked) < k:
            return None, f"only {len(ranked)} rooms"
        return ranked[k - 1], f"room no. {k} nearest the {' '.join(x for x in (v, h) if x)} of the plan"
    return ranked[0], f"the room nearest the {' '.join(x for x in (v, h) if x)} corner of the plan"


_NEAR_Q = re.compile(r"\b(nearest|closest|next|adjacent|nächst\w*|naechst\w*|neben|dichtstbij\w*|lähin\w*|"
                     r"le plus proche|près)\s+(?:to\s+|zu[mr]?\s+|de\s+la\s+|du\s+)?(?:the\s+|der\s+|dem\s+|de\s+)?"
                     r"([^?.,;]{3,60})", re.I)
_QWORDS = {"what", "which", "how", "is", "are", "the", "of", "in", "on", "this", "total", "floor", "area", "size",
           "net", "gross", "combined", "all", "every", "each", "square", "metres", "meters", "m2", "much", "big"}


_LANDMARK_SYN = [(re.compile(r"\b(stair ?cases?|stairs|stairwells?|stairways?)\b", re.I), "stair"),
                 (re.compile(r"\b(elevators?|lifts)\b", re.I), "lift"),
                 (re.compile(r"\b(restrooms?|lavator(?:y|ies)|toilets)\b", re.I), "toilet"),
                 (re.compile(r"\b(entrances?|entry|lobby)\b", re.I), "entrance")]


def _nearest_to(labs: list[dict], hits: list[dict], ref: str) -> tuple[dict | None, str]:
    """'the Secondary Classroom nearest the north stair' (Ziying's dfe-02): the reference label(s) named after
    'nearest'; a compass / position word picks one of several ('north' = up on the sheet); then the asked room whose
    label lies nearest to it."""
    import math

    refs = _match_labels(labs, ref)
    if not refs and PHRASE_SUM:  # H 23:53: 'nearest the north staircase' when the sheet prints 'Stair'
        alt_ref = ref
        for rx, to in _LANDMARK_SYN:
            alt_ref = rx.sub(to, alt_ref)
        if alt_ref != ref:
            refs = _match_labels(labs, alt_ref)
    refs = [r for r in refs if all(r is not h for h in hits)]
    if not refs:
        return None, "no label for the reference"
    note = ""
    if len(refs) > 1:
        pos = _position(ref)
        if pos is None:
            return None, f"{len(refs)} labels read '{refs[0]['text']}'"
        r, _w = _pick(refs, pos)
        if r is None:
            return None, "reference not picked"
        side = " ".join(x for x in (pos["v"], pos["h"]) if x)
        note = (f" (the {side}most of {len(refs)} '{refs[0]['text']}' labels"
                f"{'; north taken as up on the sheet' if re.search(r'north|south|east|west', ref, re.I) else ''})")
        refs = [r]
    rp = refs[0]["pt"]
    ranked = sorted(hits, key=lambda h: math.dist(h["pt"], rp))
    if len(ranked) > 1 and math.dist(ranked[1]["pt"], rp) < 1.15 * math.dist(ranked[0]["pt"], rp):
        return None, "two rooms about as near"
    return ranked[0], f"the one of {len(hits)} nearest the '{refs[0]['text']}' label{note}"


def rooms(pdf_abs: str, page: int, scale_n: float) -> list[dict]:
    """Closed rooms traced on the plan (no seed): free-space components of the linework at a few door-closing gaps,
    merged across readings; regions holding title-block words, running off the page or holding other regions are
    dropped. [{"pt" (most interior point, pdf_pt), "bbox" (pdf_pt), "area_m2" (rough), "readings"}]."""
    import cv2
    import numpy as np

    from stoa.evidence import linework as L

    pg = L._page(pdf_abs, page)
    if pg is None:
        return []
    ppt = pg["ppt"]
    mpp = scale_n * L.PT_TO_M_AT_1 / ppt
    title_px = [((b[0] + b[2]) / 2 * ppt, (b[1] + b[3]) / 2 * ppt) for t, b in L.text_lines(pdf_abs, page)
                if _TITLE_WORDS.search(t)]
    found: list[dict] = []
    for variant in L._variants(pg)[:3]:
        dist = L._dist(pdf_abs, page, variant)
        h, w = dist.shape
        for gap in (0.2, 0.35, 0.5):
            k = max(1, int(round(gap / mpp)))
            free = (dist > k).astype(np.uint8)
            nl, lab, st, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
            for i in range(1, nl):
                x, y, ww, hh, a = st[i]
                if x <= 0 or y <= 0 or x + ww >= w or y + hh >= h:
                    continue
                area = (a + 2 * k * (ww + hh)) * mpp ** 2  # grown back by ~k on every side
                if not 2.0 <= area <= 400:
                    continue
                sub = (lab[y:y + hh, x:x + ww] == i)
                if any(x <= px < x + ww and y <= py < y + hh and sub[int(py) - y, int(px) - x] for px, py in title_px):
                    continue
                dd = cv2.distanceTransform(cv2.copyMakeBorder(sub.astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT,
                                                              value=0), cv2.DIST_L2, 3)[1:-1, 1:-1]
                iy, ix = np.unravel_index(int(np.argmax(dd)), dd.shape)
                seed = ((x + ix) / ppt, (y + iy) / ppt)
                bbox = [(x - k) / ppt, (y - k) / ppt, (x + ww + k) / ppt, (y + hh + k) / ppt]
                same = None
                for f in found:  # one room seen in another reading: its seed falls inside this component
                    fx, fy = f["pt"]
                    if x <= fx * ppt < x + ww and y <= fy * ppt < y + hh and sub[int(fy * ppt) - y, int(fx * ppt) - x]:
                        same = f
                        break
                if same is None:
                    found.append({"pt": seed, "bbox": bbox, "area_m2": round(area, 2), "readings": 1})
                else:
                    same["readings"] += 1
    # a region whose box holds other rooms is the space between a sheet frame and the building (or a table)
    found = [f for f in found if not any(g is not f and f["bbox"][0] <= g["pt"][0] <= f["bbox"][2]
                                         and f["bbox"][1] <= g["pt"][1] <= f["bbox"][3] for g in found)]
    # the plan = the largest group of rooms that touch each other (wall thickness apart); title-block cells and
    # legend boxes form their own small groups
    near = 0.8 / (scale_n * L.PT_TO_M_AT_1)  # 0.8 m on paper, in pt
    groups: list[list[dict]] = []
    for f in found:
        hit = [g for g in groups if any(f["bbox"][0] - near <= h["bbox"][2] and h["bbox"][0] - near <= f["bbox"][2]
                                        and f["bbox"][1] - near <= h["bbox"][3] and h["bbox"][1] - near <= f["bbox"][3]
                                        for h in g)]
        merged = [f] + [h for g in hit for h in g]
        groups = [g for g in groups if g not in hit] + [merged]
    if not groups:
        return []
    return max(groups, key=lambda g: sum(f["area_m2"] for f in g))


def _side_line(poly: list, longest: bool) -> list | None:
    try:
        from shapely.geometry import Polygon

        r = Polygon(poly).minimum_rotated_rectangle
        xy = list(r.exterior.coords)[:4]
        sides = [(xy[i], xy[(i + 1) % 4]) for i in range(2)]
        sides.sort(key=lambda s: (s[0][0] - s[1][0]) ** 2 + (s[0][1] - s[1][1]) ** 2, reverse=longest)
        a, b = sides[0]
        return [[round(a[0], 2), round(a[1], 2)], [round(b[0], 2), round(b[1], 2)]]
    except Exception:  # noqa: BLE001
        return None


_PLURAL_PART = re.compile(r"\b(two|three|four|five|both|all|each|zwei|drei|vier|beide\w*|alle\w*|twee|drie|"
                          r"deux|trois|kaksi|kolme|två|tre|\d+)\b|\w{3,}s\b", re.I)
_SPLIT = re.compile(r"\s*(?:,|\+|\band\b|\bund\b|\ben\b|\bet\b|\bja\b|\boch\b|\bplus\b)\s*", re.I)


def _door_check(pdf: str, page: int, n: float) -> tuple[float | None, int]:
    """Median door-swing radius (m) at 1:n and how many: X1 wild 'exh222' 0.45 m at its printed 1:100 (the view is
    1:200), CTR A101 (Revit) 0.43 m at the key plan's 1:360; LEVEL_11 0.83, German sheet 0.90 (right scales)."""
    from stoa.evidence import linework as L

    radii = L.door_radii_m(pdf, page, n)
    med = radii[len(radii) // 2] if len(radii) >= 3 else None
    if DOOR_CLUSTER and med is not None and not 0.6 <= med <= 1.3 and _scale_notes(pdf, page) >= 2 \
            and len(L.sheet_scales(pdf, page)) == 1:
        # E1 c11: AF module plans (gym equipment, light fixtures: 13-22 arcs of 0.3-0.5 m) at their right 1/8" scale
        # have the median off while the real doors form a cluster of leaves at 0.7-1.1 m: that cluster decides — but
        # only when EVERY view prints the same one scale (>= 2 notes): X1 wild exh222 prints '1:100' once for a 1:200
        # view and has 11 arcs at 0.7-1.1 m too (its 0.3-0.5 m cluster is the real doors)
        doors = [r for r in radii if 0.7 <= r <= 1.1]
        if len(doors) >= 5 and len(doors) >= 0.15 * len(radii):
            return doors[len(doors) // 2], len(doors)
    return med, len(radii)


_SCALE_NOTE = re.compile(r"(?<![\d/])(1\s*:\s*\d{2,4}(?![\d.,])|\d{1,2}/\d{1,2}\s*\"?\s*=\s*1\s*'\s*-?\s*0?\"?)")


@functools.lru_cache(maxsize=16)
def _scale_notes(pdf: str, page: int) -> int:
    """How many scale notes ('1:100', '1/8" = 1'-0"') the page prints (each view's title + the scale bar)."""
    import pymupdf

    try:
        with pymupdf.open(pdf) as doc:
            return len(_SCALE_NOTE.findall(doc[page].get_text())) if page < len(doc) else 0
    except Exception:  # noqa: BLE001
        return 0


def _measure(pdf: str, page: int, pt, n: float, tag_box: tuple, line_h: float | None = None,
             base_variants: list[str] | None = None):
    """measure_room at the label; when that fails, at the tip of a LEADER ARROW that starts at the label (W5 18:26,
    Stoa's AFCEC A-101: 'Break Room / 2.02 / 153 SF' is printed in the open space below the room with a straight arrow
    pointing into it -> the label's own region is the whole view (3,576 m², holds 'Retail', 'Lobby' ...))."""
    from stoa.evidence import linework as L

    m = L.measure_room(pdf, page, pt, n, tag_box=tag_box, base_variants=base_variants)
    if not ARROW_SEED:
        return m
    tip = _arrow_tip(pdf, page, tag_box, line_h)
    if tip is None:
        return m
    m2 = L.measure_room(pdf, page, tip, n, tag_box=tag_box, own_radius_pt=0.0, base_variants=base_variants)
    if m2 is None:
        return m
    if m is not None:  # the arrow ends in the label's own room (a note / fall arrow): keep it. Containment is not
        try:           # enough: the label's region has its holes filled, so a room inside it 'contains' the tip
            from shapely.geometry import Polygon

            a, b = Polygon(m["polygon"]).buffer(0), Polygon(m2["polygon"]).buffer(0)
            if a.intersection(b).area >= 0.9 * a.union(b).area:
                return m
        except Exception:  # noqa: BLE001
            return m
    return {**m2, "seed_note": f"label outside the room: followed its leader arrow to ({tip[0]:.0f}, {tip[1]:.0f}) pt"}


ARROW_SEED = bool(int(__import__("os").environ.get("E1_ARROW_SEED", "1")))


def _arrow_tip(pdf: str, page: int, box: tuple, line_h: float | None = None) -> tuple | None:
    """A straight leader from a label: a one-segment stroke whose tail is within ~2 text heights of the label box and
    whose far end carries a small filled head (arrowhead) -> a point just past the head, inside what it points at."""
    import math

    import numpy as np

    from stoa.evidence import linework as L

    pg = L._page(pdf, page)
    if pg is None or not box:
        return None
    ppt = pg["ppt"]
    th = max(2.0, line_h or min(box[2] - box[0], box[3] - box[1]))  # ONE line's height (a stacked label's box is n)
    reach = 2.0 * th + 2.0

    def dbox(p):
        dx = max(box[0] - p[0], 0.0, p[0] - box[2])
        dy = max(box[1] - p[1], 0.0, p[1] - box[3])
        return math.hypot(dx, dy)

    heads = []
    for f in pg["fills"]:
        xs, ys = f[:, 0] / ppt, f[:, 1] / ppt
        bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
        tri = abs(float((xs * np.roll(ys, 1) - ys * np.roll(xs, 1)).sum())) / 2 <= 0.65 * bw * bh  # a triangle, not a
        if tri and bw <= 1.5 * th and bh <= 1.5 * th:  # column / wall-end square (EFA 'Science SWR': a wall line + column)
            heads.append((float(xs.mean()), float(ys.mean()), float(max(xs.max() - xs.min(), ys.max() - ys.min()))))
    if not heads:
        return None
    best = None
    lo_x, lo_y, hi_x, hi_y = ((box[0] - reach) * ppt, (box[1] - reach) * ppt, (box[2] + reach) * ppt,
                              (box[3] + reach) * ppt)
    for a, w, dark in pg["strokes"]:
        if not 2 <= len(a) <= 4:
            continue
        if not any(lo_x <= x <= hi_x and lo_y <= y <= hi_y for x, y in a):
            continue  # no end near the label
        pts = [(float(x) / ppt, float(y) / ppt) for x, y in a]
        p, q = max(((u, v) for u in pts for v in pts), key=lambda t: math.hypot(t[0][0] - t[1][0], t[0][1] - t[1][1]))
        L_ = math.hypot(q[0] - p[0], q[1] - p[1])
        if L_ <= 0 or any(abs((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / L_ > 0.5 for r in pts):
            continue  # one straight line (a path drawn there and back counts), not a polyline
        for s_, e_ in ((p, q), (q, p)):
            ds, de = dbox(s_), dbox(e_)
            ln = math.hypot(e_[0] - s_[0], e_[1] - s_[1])
            if ds > reach or de < ds + 2 * th or ln < 2 * th:
                continue
            h = [hd for hd in heads if math.hypot(hd[0] - e_[0], hd[1] - e_[1]) <= max(1.0 * th, hd[2])]
            if not h:
                continue
            if best is None or ds < best[0]:
                ux, uy = (e_[0] - s_[0]) / ln, (e_[1] - s_[1]) / ln
                k = h[0][2] + 2.0  # past the head
                best = (ds, (e_[0] + ux * k, e_[1] + uy * k))
    return best[1] if best else None


def _measure_labels(pdf: str, page: int, labs: list[dict], n: float) -> list | None:
    from stoa.evidence import linework as L

    out = []
    for lab in labs:
        m = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]), lab.get("line_h"))
        if m is None:
            return None
        out.append((lab, m))
    return out


def _poly_bbox(poly: list) -> list:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _multi(path, page, n, measured, what, value, conf, how, extra) -> Answer:
    unit = "m2" if what == "area" else "m"
    ev = [Evidence(file=path, page=page, bbox=_poly_bbox(m["polygon"]), bbox_space="pdf_pt", exact=False,
                   polygon=m["polygon"], note=f"{lab['text'][:40]}: {m['area_m2'] if what == 'area' else m['sides_m'][0]} "
                                               f"{'m²' if what == 'area' else 'm'} traced at 1:{n:g} (approximate)")
          for lab, m in measured]
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=SOLVER,
                  assumptions=[_scale_words(path, page, n) + f"; traced from the wall lines (approximate)", how,
                               *extra],
                  missing=["no room area is printed on the sheet: a printed area or a CAD/IFC twin would make it exact"],
                  trace=[f"lineplan: line-only sheet at printed 1:{n:g}; {how}"])


def _equal_rooms_answer(path, pdf, page, n, hits, what) -> Answer | None:
    """Several rooms carry the asked label ('QUIET ROOM' x2): if they measure alike (within 10 %), their mean."""
    if what not in ("area", "length") or len(hits) > 6 or len({h["text"].lower() for h in hits}) != 1:
        return None
    d, _ = _door_check(pdf, page, n)
    if d is not None and not 0.6 <= d <= 1.3:
        return None
    measured = _measure_labels(pdf, page, hits, n)
    if not measured:
        return None
    vals = [m["area_m2"] if what == "area" else m["sides_m"][0] for _, m in measured if m.get("sides_m")]
    if len(vals) != len(measured) or max(vals) > 1.10 * min(vals):
        return None
    val = round(sum(vals) / len(vals), 2)
    terms = ", ".join(f"{v:g}" for v in vals)
    return _multi(path, page, n, measured, what, val, 0.45,
                  f"{len(hits)} rooms are labelled '{hits[0]['text']}' and measure alike ({terms}): their mean",
                  [f"each outline traced from the drawn wall lines around its label (door openings closed)"])


_QUOTED = re.compile(r"[\"“”‘’']([^\"“”‘’']{2,60})[\"“”‘’']")
_NAMED = re.compile(r"\b(?:labell?ed|named|called|marked|titled|tagged)\s+[\"“”‘’']?([^\"“”‘’'?,;]{2,60}?)[\"“”‘’']?"
                    r"(?=\s+(?:on|in|at|of|from)\b|[?,;.]|$)", re.I)


def _lab_norm(t: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", str(t).lower()))


def _exact_labels(labs: list, q: str):
    """(labels whose text IS the quoted / 'labelled X' name, that name, plural_folded) or None. Case, spacing and
    punctuation ignored; a plural fold ('Toilets' for 'Toilet') only when nothing matches exactly."""
    m = _QUOTED.search(q) or _NAMED.search(q)
    if not m:
        return None
    name = _lab_norm(m.group(1))
    if not name or len(name) < 3:
        return None
    hit = [lab for lab in labs if _lab_norm(lab["text"]) == name]
    if hit:
        return hit, name, False

    def fold(t):
        return re.sub(r"(?<=[a-z])(?:es|s)\b", "", t)
    hit = [lab for lab in labs if fold(_lab_norm(lab["text"])) == fold(name)]
    return (hit, name, True) if hit else None


_NUMW = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12, "both": 2}


def _counted_labels(labs: list, q: str):
    """'the four Infants classrooms': the question counts N rooms; a room-type noun dropped from the asked words
    ('classrooms', 'offices', 'stores' …) leaves words that EXACTLY N labels hold -> those labels, else None."""
    m = re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|both|\d{1,2})\s+(?=[^\W\d_])", q, re.I)
    if not m or not re.search(r"\b(total|combined|sum|together|altogether)\b", q, re.I):
        return None
    k = int(m.group(1)) if m.group(1).isdigit() else _NUMW[m.group(1).lower()]
    if not 2 <= k <= 12:
        return None
    qw = [w for w in _words(_SUM_Q.sub(" ", q[m.end():])) if w not in _GENERIC and w not in _QWORDS]
    heads = [w for w in qw if w in _HEADS or w in ("classroom", "classrooms", "rooms", "room")]
    rest = [w for w in qw if w not in heads]
    if not rest or not heads:
        return None
    full = [lab for lab in labs if all(any(_same(w, lw) for lw in lab["words"]) for w in rest)]
    with_head = [lab for lab in full if any(any(_same(h, lw) for lw in lab["words"]) for h in heads)]
    if len(with_head) == k:
        return with_head
    if with_head:
        return None
    if len(full) == k:
        return full
    # 'Infants' x 4 + 'Infants WC' x 2: the labels with no other word
    only = [lab for lab in full if all(lw in _GENERIC or any(_same(w, lw) for w in rest)
                                       for lw in _lab_norm(lab["text"]).split())]  # 'WC' is not in lab['words']
    return only if len(only) == k else None


# H 23:51 (d): ', in m²' / 'in square metres?' at the end is a unit, not a second room for _SPLIT
_UNIT_TAIL = re.compile(r",?\s*\b(?:in|as)\s+(?:m²|m2|sq\.?\s?m|square\s+met(?:re|er)s?|met(?:re|er)s|m|ft²|ft2|sf|"
                        r"sq\.?\s?ft|square\s+f(?:ee|oo)t)\s*[?.]?\s*$", re.I)
# 'Add up the floor areas of every room called X', 'How much floor area do all the X have together?'
_ADD_Q = re.compile(r"\b(add(?:ed|ing)?\s+up|altogether|every|both)\b", re.I)
_EXCEPT_Q = re.compile(r"\b(except|excluding|exclude|without|other than|apart from|but not|besides|minus)\b", re.I)
_SCOPE_Q = re.compile(r"\b(wing|block|zone|half|corner|quarter|part|section|side|end)\b", re.I)


def _fold(w: str) -> str:
    """plural fold for label phrase matching: laboratories -> laboratory, classes -> class, rooms -> room"""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and re.search(r"(?:ss|x|ch|sh)es$", w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _phrase_labels(labs: list, q: str):
    """D1 23:52 (the flipped test): the label text whose own words all occur, adjacent and in order (plural folded),
    in the question; the longest such label wins. -> (labels whose words hold that phrase adjacently, phrase words,
    plural_in_question, (start, end) word span in the question) or None. Words of the question outside the phrase
    ('do', 'have', 'add', 'up', 'eight') do not have to be in the label."""
    qw = _words(q)
    qf = [_fold(w) for w in qw]
    best = None
    for lab in labs:
        lw = [w for w in _lab_norm(lab["text"]).split() if len(w) >= 3 and not w.isdigit()]
        if not lw or all(w in _GENERIC for w in lw) or len(lw) > 6:
            continue
        lf = [_fold(w) for w in lw]
        for i in range(len(qw) - len(lw) + 1):
            if all(qf[i + j] == lf[j] or qw[i + j] == lw[j] for j in range(len(lw))):
                if best is None or len(lw) > len(best[0]):
                    best = (lf, i, i + len(lw))
                break
    if best is None:
        return None
    lf, i0, i1 = best
    plural = qw[i1 - 1] != _fold(qw[i1 - 1]) and _fold(qw[i1 - 1]) == lf[-1] and qw[i1 - 1] != lf[-1]

    def holds(lab):
        f = [_fold(w) for w in _lab_norm(lab["text"]).split() if len(w) >= 3 and not w.isdigit()]
        return any(f[k:k + len(lf)] == lf for k in range(len(f) - len(lf) + 1))
    return [lab for lab in labs if holds(lab)], lf, plural, (i0, i1)


def _pre_phrase(q: str, first_word: str) -> str | None:
    """the question text before the first occurrence of the phrase's first word"""
    m = re.search(rf"\b{re.escape(first_word)}", q, re.I)
    return q[:m.start()] if m else None


def _phrase_sum(labs: list, q: str):
    """(labels, note) for a one-part total named by a label phrase, or None. Needs a plural of the label ('the Group
    Rooms') or all / every / both / a number before it; a number must equal the count of labels (else the exact-text
    labels when THEY number it); a position / wing / exclusion word or 'nearest' -> None (not a whole-sheet total)."""
    if _EXCEPT_Q.search(q) or _NEAR_Q.search(q):
        return None
    r = _phrase_labels(labs, q)
    if r is None:
        return None
    full, lf, plural, (i0, i1) = r
    qw = _words(q)
    before = " ".join(qw[max(0, i0 - 3):i0])
    rest = [w for k, w in enumerate(qw) if not i0 <= k < i1]
    if any(rx.fullmatch(w) for w in rest for rx in (_TOP, _BOTTOM, _LEFT, _RIGHT, _SCOPE_Q, *(r_ for r_, _ in _ORD))):
        return None
    m = re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|both)\b", before)
    k = _NUMW[m.group(1)] if m else None
    if k is None:  # 'the 8 Junior Classrooms': a 1-2 digit count right before the phrase (words of < 3 letters skipped)
        md = re.search(r"\b(\d{1,2})\s+(?:[^\W\d_]+\s+){0,2}$", _pre_phrase(q, qw[i0]) or "")
        k = int(md.group(1)) if md and 2 <= int(md.group(1)) <= 16 else None
    if not (plural or k or re.search(r"\b(all|every|each)\b", before)):
        return None
    note = ""
    nxt = qw[i1] if i1 < len(qw) else ""
    if nxt and (nxt in _HEADS or _fold(nxt) in _HEADS or _ROOM_Q.fullmatch(nxt)) and \
            not any(_fold(w) == _fold(nxt) for lab in full for w in _lab_norm(lab["text"]).split()):
        # X1 sl-123 'the maths classrooms (Maths 1-5)': the labels lack the room noun, so the rooms are the numbered
        # ones ('Maths 1' … 'Maths 5'); 'Maths SWR' / 'Math St.' are other rooms
        numbered = [lab for lab in full if all(re.fullmatch(r"\d{1,3}|[a-z]", t) for t in
                                               _lab_norm(lab["text"]).split()[len(lf):])
                    and len(_lab_norm(lab["text"]).split()) > len(lf)]
        if len(numbered) < 2:
            return None
        if len(numbered) < len(full):
            oth = [lab["text"] for lab in full if lab not in numbered]
            note = f"; not counted: {', '.join(repr(t) for t in oth[:3])}{' …' if len(oth) > 3 else ''} (other rooms)"
        full = numbered
    if k is not None and len(full) != k:
        ex = [lab for lab in full if [_fold(w) for w in _lab_norm(lab["text"]).split()
                                      if len(w) >= 3 and not w.isdigit()] == lf]
        if len(ex) != k:
            return None
        note = f"; the {k} labelled exactly '{ex[0]['text']}' (as the question counts them)"
        full = ex
    if not 2 <= len(full) <= 16:
        return None
    return full, note


def _sum_answer(path, pdf, page, q, n, labs, what) -> Answer | None:
    """'the combined floor area of the comms room and the two quiet rooms': every named part matched to its label(s)
    (a plural / number takes every room so labelled), each measured; the sum, with every term's outline."""
    if what != "area":
        return None
    if PHRASE_SUM:
        q = _UNIT_TAIL.sub("?", q)
    parts = [p for p in _SPLIT.split(q) if p and _words(p)]
    alt = ""
    ex_q = _exact_labels(labs, q)
    sum_rx = (r"\b(all|every|each|total|combined|sum|together|altogether|add(?:ed|ing)?\s+up|both|two|three|four|"
              r"five|six|seven|eight|nine|ten|alle\w*|jede\w*)\b" if PHRASE_SUM
              else r"\b(all|every|each|total|combined|sum|alle\w*|jede\w*)\b")
    if ex_q is not None and len(ex_q[0]) >= 2 and len(ex_q[0]) <= 16 and re.search(sum_rx, q, re.I):
        # D1 23:11: 'all rooms labelled 'Toilet'' = the rooms whose label IS that text ('Toilets' / 'Accessible Toilet'
        # / 'Entrance and Cloakroom' / 'Sports Equipment Store' are other rooms); the contains-all-words set is stated
        full, how, _pf = ex_q
        cont = [lab for lab in labs if all(any(_same(w, lw) for lw in lab["words"]) for w in _words(how))]
        oth = list(dict.fromkeys(lab["text"] for lab in cont if lab not in full))
        alt = (f"; not counted: {len(cont) - len(full)} other label(s) containing '{how}' ("
               f"{', '.join(repr(t) for t in oth[:3])}{' …' if len(oth) > 3 else ''})" if len(cont) > len(full) else "")
        parts, labs_full = [], full
    elif len(parts) == 1 and _counted_labels(labs, q) is not None:
        # X1 sl-309 / 314: 'the total floor area of the four Infants classrooms' -- the rooms are labelled 'Infants':
        # without the room-type noun the asked words name exactly the 4 rooms the question counts
        full = _counted_labels(labs, q)
        parts, labs_full = [], full
    elif len(parts) == 1 and re.search(r"\b(all|every|each|alle\w*|jede\w*)\b", q, re.I):
        # ONE plural part: 'the total floor area of all Secondary Classrooms' (Stoa DfE sheet, Ziying's dfe-03) = every
        # room whose label holds all the asked words ('Secondary Classroom (Severe …)' included; the exact-label-only
        # total goes into the trace)
        qw = [w for w in _words(_SUM_Q.sub(" ", q)) if w not in _GENERIC and w not in _QWORDS]
        full = [lab for lab in labs if qw and all(any(_same(w, lw) for lw in lab["words"]) for w in qw)]
        exact = _match_labels(labs, q)
        if 2 <= len(full) <= 16:
            if len(exact) != len(full):
                alt = (f"; only the {len(exact)} labelled exactly '{exact[0]['text']}': see the trace" if exact else "")
            parts, labs_full = [], full
        elif not PHRASE_SUM:
            return None
        else:
            labs_full = None
    else:
        labs_full = None
    if labs_full is None and len(parts) == 1 and PHRASE_SUM:
        # D1 23:52 / H 23:51: 'the combined floor area of the Junior Classrooms', 'How much floor area do all the Group
        # Rooms have together?', 'Total area of the secondary classrooms?' -- the label named in the question (its own
        # words all there, in order), every room so labelled
        ph = _phrase_sum(labs, q)
        if ph is not None:
            labs_full, alt = ph
            parts = []
    if len(parts) < 2 and labs_full is None:
        return None
    measured: list = list(labs_full or [])
    seen: set = {id(h) for h in measured}
    for part in parts:
        hits = _match_labels(labs, part)
        if not hits:
            if any(w not in _GENERIC and not _SUM_Q.fullmatch(w) for w in _words(part)) and part is not parts[0]:
                return None  # a named part with no label on the sheet: the sum would miss it
            continue
        if len(hits) > 1 and not _PLURAL_PART.search(re.sub(r"\b(rooms?|räume|zimmer)\b", "", part, flags=re.I)) \
                and not re.search(r"\b(two|three|four|both|all|zwei|drei|beide\w*|alle\w*)\b", part, re.I):
            return None  # 'the meeting room' with four on the sheet
        for h in hits:
            if id(h) not in seen:
                seen.add(id(h))
                measured.append(h)
    if len(measured) < 2 or len(measured) > 12:
        return None
    d, _ = _door_check(pdf, page, n)
    if d is not None and not 0.6 <= d <= 1.3:
        return None
    ms = _measure_labels(pdf, page, measured, n)
    if not ms:
        return None
    if labs_full is not None and len(ms) != len(measured):
        return None  # 'all X': a room that could not be traced would be missing from the total
    cuts = [_recess_area(pdf, page, n, m) if what == "area" else 0.0 for _, m in ms]
    if any(cuts):  # traced picture rooms: their parts in door recesses / wall cavities left out (as for one room)
        ms = [(lab, {**m, "area_m2": round(m["area_m2"] - c, 2)}) for (lab, m), c in zip(ms, cuts)]
    total = round(sum(m["area_m2"] for _, m in ms), 2)
    terms = " + ".join(f"{m['area_m2']:g} ({lab['text'][:24]})" for lab, m in ms)
    a = _multi(path, page, n, ms, what, total, 0.45, f"sum of {len(ms)} rooms found by their labels: {terms} = "
               f"{total} m²{alt}", ["each outline traced from the drawn wall lines around its label (door openings "
                                    "closed)"])
    if any(cuts):
        a.assumptions.append(f"{sum(cuts):.2f} m² of the traced regions inside door recesses / narrow wall cavities left "
                             f"out ({sum(1 for c in cuts if c)} room(s); the picture draws walls as outlines)")
    if alt and ex_q is not None and labs_full is ex_q[0]:
        a.assumptions.append(f"rooms labelled exactly '{ex_q[1]}' (case and spacing ignored{', plural folded' if ex_q[2] else ''}); "
                             "labels that only contain these words are other rooms")
    elif alt:
        ex = {id(x) for x in _match_labels(labs, q)}
        sub = round(sum(m["area_m2"] for lab, m in ms if id(lab) in ex), 2)
        a.trace.append(f"lineplan: alternative = only the rooms labelled exactly '{ms[0][0]['text'] if id(ms[0][0]) in ex else next(lab['text'] for lab, _ in ms if id(lab) in ex)}': {sub} m²")
    return a


_THICK_Q = re.compile(r"\b(thick\w*|dick\w*|stärke|staerke|wandstärke|dikte|épaisseur|epaisseur|spessore|paksu\w*|"
                      r"tjock\w*|grubo\w*)\b", re.I)
_OUTER_Q = re.compile(r"\b(outer|outside|exterior|external|façade|facade|außen\w*|aussen\w*|buiten\w*|extérieur\w*|"
                      r"esterno|esterni|ulko\w*|yttervägg\w*)\b", re.I)


def _outside(pdf: str, page: int, variant: str):
    """Mask of the free space connected to the page border (the outside of the building), line set `variant`."""
    import cv2
    import numpy as np

    from stoa.evidence import linework as L

    d = L._dist(pdf, page, variant)
    free = (d > 0).astype(np.uint8)
    h, w = free.shape
    ff = np.zeros((h + 2, w + 2), np.uint8)
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if free[seed[1], seed[0]] and not ff[seed[1] + 1, seed[0] + 1]:
            cv2.floodFill(free, ff, seed, 2, flags=4 | (1 << 8) | cv2.FLOODFILL_MASK_ONLY)
    return d, ff[1:-1, 1:-1]


def _wall_samples(pdf, page, n, variant, poly, side, max_m=1.0) -> list:
    """Face-to-face wall thickness (m) behind one side of a traced room outline, sampled across the middle 60 % of
    that side: from the outline (= the inner face line's middle) outward to the last drawn line before the outside."""
    from shapely.geometry import LineString, Polygon

    from stoa.evidence import linework as L

    d, out = _outside(pdf, page, variant)
    ppt = L._page(pdf, page)["ppt"]
    th_px = _stroke_px(L._page(pdf, page))
    mpp = n * L.PT_TO_M_AT_1 / ppt
    P = Polygon(poly)
    x0, y0, x1, y1 = P.bounds
    h, w = d.shape
    res = []
    vert = side in ("top", "bottom")
    lo, hi = (x0, x1) if vert else (y0, y1)
    for i in range(1, 30):
        t = lo + (hi - lo) * (0.2 + 0.6 * i / 30)
        ray = LineString([(t, y0 - 1), (t, y1 + 1)] if vert else [(x0 - 1, t), (x1 + 1, t)])
        cut = P.boundary.intersection(ray)
        pts = [g for g in getattr(cut, "geoms", [cut]) if hasattr(g, "x")]
        if not pts:
            continue
        face = (min if side in ("top", "left") else max)(pts, key=lambda g: g.y if vert else g.x)
        step = -1 if side in ("top", "left") else 1
        fx, fy = int(round(face.x * ppt)), int(round(face.y * ppt))
        runs: list[list[int]] = []  # dark runs along the ray, from a few px INSIDE the room outward
        reached = False
        for k in range(-6, int(max_m / mpp) + 2):
            x, y = (fx, fy + step * k) if vert else (fx + step * k, fy)
            if not (0 <= x < w and 0 <= y < h):
                break
            if out[y, x]:
                reached = True
                break
            if d[y, x] == 0:
                if runs and runs[-1][1] == k - 1:
                    runs[-1][1] = k
                else:
                    runs.append([k, k])
        ab = _runs_thickness(runs, th_px) if reached else None
        if ab is None:
            continue
        a_, b_ = ab  # inner / outer face

        def at(kk):
            return ((fx / ppt, (fy + step * kk) / ppt) if vert else ((fx + step * kk) / ppt, fy / ppt))

        res.append(((b_ - a_) * mpp, at(a_), at(b_)))
    return res


def _runs_thickness(runs: list, th_px: float) -> tuple[float, float] | None:
    """(inner, outer) positions (px along the scan) of a wall's two faces from the dark runs crossed: two or more runs
    = face lines (their middles); ONE solid run = a filled wall (poché), its faces = the run's ends less half a stroke."""
    if not runs:
        return None
    if len(runs) == 1:
        a_, b_ = runs[0][0] + th_px / 2 - 0.5, runs[0][1] - th_px / 2 + 0.5
        return (a_, b_) if b_ - a_ >= 1 else None
    return (runs[0][0] + runs[0][1]) / 2, (runs[-1][0] + runs[-1][1]) / 2


def _stroke_px(pg: dict) -> float:
    ws = sorted(w for _, w, dark in pg["strokes"] if dark) or [0.5]
    return max(1.0, round(ws[len(ws) // 2] * pg["ppt"]))


def _mode(vals: list, tol: float = 0.015) -> tuple[float | None, int]:
    best = (None, 0)
    for v in vals:
        k = sum(1 for u in vals if abs(u - v) <= tol)
        if k > best[1]:
            best = (v, k)
    if best[0] is None:
        return None, 0
    near = sorted(u for u in vals if abs(u - best[0]) <= tol)
    return near[len(near) // 2], best[1]


def _thickness_answer(path: str, pdf: str, page: int, q: str, n: float, labs: list[dict]) -> Answer | None:
    """'How thick is the outer wall at the top of the plan?' / 'the wall between the kitchen and the bath': the
    face-to-face distance (CAD lines stand for the wall faces) measured behind traced room outlines at the printed
    scale; the modal value over many samples (windows / junctions / hatching are outliers)."""
    from shapely.geometry import Point, Polygon

    from stoa.evidence import linework as L

    d_med, _ = _door_check(pdf, page, n)
    if d_med is not None and not 0.6 <= d_med <= 1.3:
        return None
    pg = L._page(pdf, page)
    variant = f"all|doors@{n:g}" if L._door_spokes(pg, n) else "all"
    parts = [p for p in _SPLIT.split(q) if p and _words(p)]
    per = [_match_labels(labs, p) for p in parts] if len(parts) >= 2 else []
    named = [hs[0] for hs in per if len(hs) == 1] if all(len(hs) <= 1 for hs in per) else []
    if len(named) == 2 and named[0] is not named[1] and not _OUTER_Q.search(q):
        ms = _measure_labels(pdf, page, named, n)
        if not ms:
            return None
        A, B = Polygon(ms[0][1]["polygon"]), Polygon(ms[1][1]["polygon"])
        k = n * L.PT_TO_M_AT_1
        d, _ = _outside(pdf, page, variant)
        ppt = pg["ppt"]
        Bpx = B.buffer(-2 / ppt)  # a few px inside B: the scan has crossed B's face line
        found = []
        for t in [i / 240 for i in range(240)]:
            pa = A.exterior.interpolate(t, normalized=True)
            if B.exterior.distance(pa) * k > 0.8:
                continue
            pb = B.exterior.interpolate(B.exterior.project(pa))
            dx, dy = pb.x - pa.x, pb.y - pa.y
            if abs(dx) > 3 * abs(dy):
                ux, uy = (1 if dx > 0 else -1), 0
            elif abs(dy) > 3 * abs(dx):
                ux, uy = 0, (1 if dy > 0 else -1)
            else:
                continue  # diagonal approach (a corner): skip
            x0_, y0_ = pa.x * ppt - 6 * ux, pa.y * ppt - 6 * uy
            runs: list[list[int]] = []
            hit = False
            for j in range(0, int(0.9 / (k / ppt)) + 8):
                x, y = int(round(x0_ + ux * j)), int(round(y0_ + uy * j))
                if not (0 <= y < d.shape[0] and 0 <= x < d.shape[1]):
                    break
                if j > 6 and Bpx.contains(Point(x / ppt, y / ppt)):
                    hit = True
                    break
                if d[y, x] == 0:
                    if runs and runs[-1][1] == j - 1:
                        runs[-1][1] = j
                    else:
                        runs.append([j, j])
            ab = _runs_thickness(runs, _stroke_px(pg)) if hit else None
            if ab is not None:
                a_, b_ = ab
                found.append(((b_ - a_) * k / ppt, [[round((x0_ + ux * a_) / ppt, 2), round((y0_ + uy * a_) / ppt, 2)],
                                                    [round((x0_ + ux * b_) / ppt, 2), round((y0_ + uy * b_) / ppt, 2)]]))
        vals = [v for v, _ in found if 0.03 <= v <= 0.9]
        val, sup = _mode(vals)
        if val is None or sup < 3:
            return None
        line = min(found, key=lambda f_: abs(f_[0] - val))[1]
        how = f"the wall between '{named[0]['text']}' and '{named[1]['text']}'"
        conf_t = 0.35  # a fixture line (bench, counter) along the wall can stand in for the room's face
    else:
        if not _OUTER_Q.search(q):
            return None
        pos = _position(q) or {"v": None, "h": None}
        sides = [x for x in (pos.get("v"), pos.get("h")) if x] or ["top", "bottom", "left", "right"]
        rs = rooms(pdf, page, n)
        if len(rs) < 2:
            return None
        X0, Y0 = min(r["bbox"][0] for r in rs), min(r["bbox"][1] for r in rs)
        X1, Y1 = max(r["bbox"][2] for r in rs), max(r["bbox"][3] for r in rs)
        near_pt = 1.0 / (n * L.PT_TO_M_AT_1)  # rooms whose side lies within 1 m of the plan's extreme on that side
        samples = []
        for side in sides:
            for r in rs:
                b = r["bbox"]
                if {"top": b[1] - Y0, "bottom": Y1 - b[3], "left": b[0] - X0, "right": X1 - b[2]}[side] > near_pt:
                    continue
                m = L.measure_room(pdf, page, r["pt"], n)
                if m:
                    samples += [(*s_, id(r)) for s_ in _wall_samples(pdf, page, n, variant, m["polygon"], side)]
        samples = [s_ for s_ in samples if 0.05 <= s_[0] <= 0.9]
        vals = [s_[0] for s_ in samples]
        val, sup = _mode(vals)
        if val is None or sup < max(4, len(vals) // 3):
            return None
        n_rooms = len({s_[3] for s_ in samples if abs(s_[0] - val) <= 0.015})
        best = min(samples, key=lambda s_: abs(s_[0] - val))
        line = [[round(best[1][0], 2), round(best[1][1], 2)], [round(best[2][0], 2), round(best[2][1], 2)]]
        how = (f"the outer wall{' at the ' + ' '.join(sides) if len(sides) < 4 else 's'}: {sup} of {len(vals)} samples "
               f"behind {n_rooms} room(s) agree")
        conf_t = 0.5 if n_rooms >= 2 else 0.35
    val = round(val, 3)
    x0, y0 = min(line[0][0], line[1][0]), min(line[0][1], line[1][1])
    x1, y1 = max(line[0][0], line[1][0]), max(line[0][1], line[1][1])
    return Answer(value=val, unit="m", confidence=conf_t, solver=SOLVER,
                  evidence=[Evidence(file=path, page=page, bbox=[x0 - 2, y0 - 2, x1 + 2, y1 + 2], bbox_space="pdf_pt",
                                     exact=False, polyline=line,
                                     note=f"wall thickness {val} m (face line to face line) at 1:{n:g} (approximate)")],
                  assumptions=[_scale_words(path, page, n) + f"; measured on the drawn wall lines (approximate)",
                               f"{how}; thickness = distance between the two drawn face lines (their middles)"],
                  missing=["plaster / finish layers are not drawn separately: a construction detail would make it exact"],
                  trace=[f"lineplan: wall thickness {val} m — {how}"])


_KINDS = [  # (question words, label pattern) for room kinds counted by their printed labels
    (r"bed\s*rooms?|schlafzimmer|schlafräume|chambres?|slaapkamers?|makuuhuone\w*|sovrum|camere da letto|"
     r"dormitorios?|sypialni\w*",
     r"bed\s*rooms?|bedrm|\bbed\s*\d|master\s*bed|\bbr\s*\d|schlaf\w*|chambre|slaapkamer|makuu\w*|sovrum|"
     r"camera da letto|dormitorio|sypialnia"),
    (r"toilets?|\bw\.?\s?c\.?s?\b|lavator\w*|restrooms?|toiletten?|toilettes?|wc:s|vessa\w*",
     r"\bw\.?\s?c\b\.?|toilet\w*|lavator\w*|restroom|\bvc\b|wc\s*\d"),
    (r"bath\s*rooms?|badezimmer|bäder|salles? de bains?|badkamers?|kylpyhuone\w*|badrum|bagni|baños",
     r"bath\s*room|\bbath\b|en\s*-?\s*suite|\bens\b|\bbad\b|badezimmer|salle de bains?|badkamer|kylpyhuone|"
     r"badrum|bagno|baño"),
    (r"kitchens?|küchen?|cuisines?|keukens?|keittiö\w*|kök|cucin\w*|cocinas?",
     r"kitchen|küche|kueche|cuisine|keuken|keittiö|kök|cucina|cocina"),
]
_OBJ_Q = re.compile(r"\b(windows?|fenster\w*|doors?|tür\w*|tuer\w*|deur\w*|portes?|ovi\w*|ikkun\w*|stairs?|treppe\w*|"
                    r"steps?|stufen|columns?|stütze\w*|säule\w*|pillars?|beams?|walls?|wände|\w*wand|sinks?|"
                    r"washbasins?|bath\s?tubs?|showers?|beds?\b|chairs?|desks?|tables?|parking|spaces? for|cars?|"
                    r"lifts?|elevators?|aufzüge?|radiators?|outlets?|sockets?|lights?)\b", re.I)
_HOW_MANY = re.compile(r"\b(?:how many|number of|count the|wie viele|anzahl (?:der|an)|hoeveel|combien de|"
                       r"quant[ie]|kuinka monta|hur många)\s+(.+?)(?:\s+(?:are|is|does|do|on|in|has|have|there|sind|gibt|"
                       r"hat|zijn|y a|ci sono|on|finns)\b|\?|$)", re.I)


COUNT_EXACT = bool(int(__import__("os").environ.get("E1_COUNT_EXACT", "1")))
COUNT_NO_TITLES = bool(int(__import__("os").environ.get("E1_COUNT_NO_TITLES", "1")))
NUM_PICK = bool(int(__import__("os").environ.get("E1_NUM_PICK", "1")))  # _match_labels: the asked room number beats
#                                                                            one extra label word
_VIEW_TITLE = re.compile(r"\s[-–—]\s.*\s[-–—](?:\s|$)|^\s*(?:module|level|block|zone|wing|phase|unit)\s*[\dA-Z]{1,3}\s*[-–—:]\s"
                         r"|\b(?:floor|ceiling|roof|site|key|furniture|enlarged|partial)\s+plan\b|\b(?:rcp|axon\w*|elevation|"
                         r"section)\b", re.I)
ROOM_EXACT = bool(int(__import__("os").environ.get("E1_ROOM_EXACT", "1")))


def _count_answer(path: str, pdf: str, page: int, q: str) -> Answer | None:
    """'How many bedrooms / WCs / meeting rooms …?' on a sheet that names its rooms: the printed labels of that kind
    (stacked lines merged, 'BED ROOM 1..3', 'WC'), each boxed (exact text positions). Abstains when no label matches
    (a room drawn without a label is not 'no room')."""
    m = _HOW_MANY.search(q)
    if not m:
        return None
    target = re.sub(r"\([^)]*\)", " ", m.group(1)).strip(" ?.,")
    if re.search(r"\b(bowls?|pans?|seats?|fixtures?|symbols?|basins?|urinals?)\b", m.group(1), re.I):
        return None  # drawn objects ('toilet bowls'), not rooms labelled 'TOILET'
    pat = None
    for qrx, lrx in _KINDS:
        if re.search(qrx, m.group(1), re.I):
            pat = re.compile(lrx, re.I)
            break
    if pat is None:
        # only ROOM phrases ('meeting rooms', 'offices', 'garages'): objects (windows, doors, stairs …) are drawn, and a
        # legend / note label naming them is not one per object (Swiss 'Fenster' legend: 22 windows -> 1, up_lone)
        if _OBJ_Q.search(target) or not _ROOM_Q.search(target):
            return None
        ws = [w for w in re.findall(r"[^\W\d_]+", target.lower()) if w not in _GENERIC or w in ("room", "rooms")]
        ws = [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in ws]
        if not ws or len(ws) > 3 or ws == ["room"] or not any(len(w) >= 4 and w != "room" for w in ws):
            return None  # 'how many rooms' is a room count, not a label kind
        pat = re.compile(r"\b" + r"\s*".join(re.escape(w) + r"s?" for w in ws) + r"\b", re.I)
    if re.search(r"\b(whole|entire|both|all)\s+(house|building|floors|storeys|levels|gebäude|haus)\b|\bin total\b|"
                 r"\binsgesamt\b", q, re.I):
        try:
            import pymupdf

            with pymupdf.open(pdf) as d:
                if len(d) > 1:
                    return None  # one sheet of a SET: the other floors' labels are on other pages (X1 farm x1-147)
        except Exception:  # noqa: BLE001
            return None
    every = floor_plan_labels(pdf, page, labels(pdf, page, min_letters=2))
    if COUNT_NO_TITLES:  # G1 01:57 (MFC A-103 'How many restrooms?' 3 vs 2): a VIEW TITLE ('MODULE 05 - RESTROOMS - FLOOR
        #                   PLAN') names the drawing, not a room
        every = [lab for lab in every if not _VIEW_TITLE.search(lab["text"])]
    labs = [lab for lab in every if pat.search(lab["text"])]
    exact_note = ""
    if COUNT_EXACT and len(_QUOTED.findall(q)) <= 1 and \
            (_QUOTED.search(q) or re.search(r"\b(?:labell?ed|named|called|titled|marked)\b", q, re.I)):
        # D1 01:3x: 'How many rooms labelled 'Accessible Toilet'' counted every toilet-kind label (11 vs 5: 'Toilets',
        # 'Toilet' too) -> an exact-label question counts the labels whose text IS that name (case / spacing ignored;
        # plural fold only when nothing matches exactly), as _exact_labels does for areas
        ex = _exact_labels(every, q)
        if ex:
            labs, exact_note = ex[0], f" (labelled exactly '{ex[1]}'{', plural folded' if ex[2] else ''})"
    if not labs or len(labs) > 60:
        return None
    # a label that STATES a count ('3 BEDROOMS', '2 x WC', '16 RISERS @ 7.5"') is not one object: a text reader must
    # read the number (X1 15:51) -> abstain; room numbers come AFTER the noun ('BED ROOM 2') and still count
    if any(re.search(r"(?<![\d.,/])\d{1,3}\s*(?:[x×]\s*)?$", lab["text"][:mt.start()])
           for lab in labs for mt in [pat.search(lab["text"])] if mt):
        return None
    names = ", ".join(f"'{lab['text'][:24]}'" for lab in labs[:12]) + (" …" if len(labs) > 12 else "")
    return Answer(value=len(labs), unit="count", confidence=0.6, solver=SOLVER,
                  evidence=[Evidence(file=path, page=page, bbox=[round(v, 2) for v in lab["bbox"]], bbox_space="pdf_pt",
                                     exact=True, note=f"{i}: {lab['text'][:40]} (printed label)")
                            for i, lab in enumerate(labs, 1)],
                  assumptions=[f"counted the rooms LABELLED as {target!r}{exact_note} on the sheet: {len(labs)} ({names})"],
                  missing=["a room of that kind drawn without its own label would not be counted"],
                  trace=[f"lineplan: {len(labs)} printed labels match {pat.pattern[:60]!r}{exact_note}: {names}"])


_BASE_Q = re.compile(r"\b(base ?boards?|skirting(?:[\s-]*boards?)?|skirtings?|base (?:trim|moulding|molding|cove)s?|"
                     r"cove base|wall base|kick ?boards?|sockelleiste\w*|fu(?:ß|ss)leiste\w*|scheuerleiste\w*|"
                     r"plint(?:en|s)?|plinthes?|battiscop\w*|jalkalist\w*|golvlist\w*|sockellist\w*)\b", re.I)
_BASE_WORDS = re.compile(r"\b(linear|lineal|running|metres?|meters?|lm|feet|foot|ft|needs?|needed|required|requires|"
                         r"minus|less|doors?|openings?|perimeter|how|much|many|total|length|of|for|in|does|do|is|are|"
                         r"would|will|we|install|installed|fit|fitted|fitting|along|around|"
                         # ', door opening excluded?' is a condition, not a second room (G1 17:22, sl-091)
                         r"exclud\w*|includ\w*|deduct\w*|subtract\w*|without|not|counted|taken|off|net|gross|"
                         r"ways?|widths?)\b", re.I)
MIN_OPENING_M = 0.5  # a stretch of the room outline with no wall line along it, at least this long, is an opening


def _px(ring, s_pt: float, ppt: float) -> tuple[float, float]:
    q = ring.interpolate(float(s_pt))
    return q.x * ppt, q.y * ppt


def _face_ring(m: dict, m_per_pt: float):
    """The traced outline moved out onto the wall faces: the polygon of pixel centres (simplified) runs ~0.5 pt inside
    the room's pixels, so it is offset by (pixel area - polygon area) / perimeter (X1 sl-055 laundry: 47.9 x 27.7 pt
    -> 48.9 x 28.7 = the wall faces 841.18-890.08 x 938.40-967.14)."""
    from shapely.geometry import LineString, Polygon

    try:
        pg = Polygon([tuple(p) for p in m["polygon"]]).buffer(0)
        if pg.geom_type != "Polygon" or pg.area <= 0:
            return None
        a_px_pt2 = float(m["area_m2"]) / (m_per_pt ** 2)
        d = (a_px_pt2 - pg.area) / pg.length
        if 0 < d <= 2.0:
            g = pg.buffer(d, join_style=2, mitre_limit=2.0)
            if g.geom_type == "Polygon":
                pg = g
        return LineString(list(pg.exterior.coords))
    except Exception:  # noqa: BLE001
        return None


def _skirting(pdf: str, page: int, n: float, m: dict) -> dict | None:
    """Skirting (baseboard) of a traced room = its outline minus the OPENINGS in its walls (captain 16:15, Stoa kind 2):
    an opening is a stretch of the outline with no drawn line along it (door ways closed by the door-swing chord or by
    the gap closing, open passages); the leaf of a door that rests against a wall keeps counting as wall. Opening
    widths are measured jamb to jamb (X1 sl-055: laundry 10.957 m perimeter - 1.02 m opening = 9.93). Returns
    {"perimeter_m", "value_m", "openings": [(a_pt, b_pt, len_m)], "runs": [[pt, ...], ...]} or None."""
    import numpy as np
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    from stoa.evidence import linework as L

    pg = L._page(pdf, page)
    if pg is None or not m.get("polygon"):
        return None
    ppt = pg["ppt"]
    m_per_pt = n * L.PT_TO_M_AT_1
    variant = str(m["variant"])
    d_ink = L.ink_dist(pdf, page)  # the page as printed: walls, jambs, glazing (clip paths respected)
    if d_ink is None:
        return None
    H, W = d_ink.shape

    def at(d, x, y) -> float:
        xi, yi = int(round(x)), int(round(y))
        return float(d[yi, xi]) if 0 <= xi < W and 0 <= yi < H else 255.0

    tol = max(2.5, 0.05 / m_per_pt * ppt)  # px: the outline sits on its lines' middles (simplified polygon: <= 2 px)
    wtol = 0.15 / m_per_pt * ppt  # px: a door leaf resting against a wall lies within this of the wall's line
    leaves, chords = [], []  # door radii: the door way's chord (an opening, drawn or not) / a leaf (counts as wall)
    if "|doors@" in variant:
        spokes, _ = L._door_arcs(pg, n)
        for i in range(0, len(spokes) - 1, 2):
            pair = []
            for sp in spokes[i:i + 2]:
                (cx, cy), (ex, ey) = sp.tolist()
                # drawn = ink along MOST of the radius (median): a door tag ('102') or a leader crossing the door
                # way touches one or two samples (USACE A-101F toilet), a leaf lying on its radius touches all
                mid = float(np.median([at(d_ink, cx + (ex - cx) * t, cy + (ey - cy) * t)
                                       for t in (0.15, 0.25, 0.35, 0.45, 0.5, 0.55, 0.65, 0.75, 0.85)]))
                ext = max(at(d_ink, cx + (ex - cx) * t, cy + (ey - cy) * t) for t in (1.15, 1.3))  # wall line beyond
                pair.append((LineString([(cx, cy), (ex, ey)]), mid, ext))
            if len(pair) != 2:
                continue
            (s0, m0, e0), (s1, m1, e1) = pair
            mtol = min(wtol, 2.5 * ppt)  # a leaf is drawn ON its radius: 0.15 m is 8.5 pt at 1:50 and took in the
            #                              jamb / leaf ends of a 0.9 m door way (A-101F toilet); 1:200 unchanged
            if (m0 > mtol) != (m1 > mtol):  # nothing drawn along one of them: that one spans the door way
                c = 0 if m0 > mtol else 1
            elif (e0 <= tol) != (e1 <= tol):  # both drawn (or both free): the chord runs on into the wall's line
                c = 0 if e0 <= tol else 1
            else:
                leaves.extend(x[0] for x in pair if x[1] <= mtol)
                continue
            chords.append(pair[c][0])
            if pair[1 - c][1] <= mtol:
                leaves.append(pair[1 - c][0])

    def on_chord(x, y) -> bool:
        for c in chords:
            (ax, ay), (bx, by) = c.coords
            vx, vy = bx - ax, by - ay
            ll = vx * vx + vy * vy
            if ll <= 0:
                continue
            t = ((x - ax) * vx + (y - ay) * vy) / ll
            if 0.02 <= t <= 0.98 and abs((x - ax) * vy - (y - ay) * vx) / ll ** 0.5 <= tol:
                return True
        return False

    ring = _face_ring(m, m_per_pt)
    if ring is None:
        return None
    total_pt = ring.length
    step = 1.0 / ppt  # one raster px, in pt
    k = max(8, int(total_pt / step))
    ss = np.linspace(0.0, total_pt, k, endpoint=False)
    d_arr, leaf_arr = np.zeros(k), np.zeros(k, dtype=bool)
    for i, s_ in enumerate(ss):
        x, y = _px(ring, s_, ppt)
        d_arr[i] = at(d_ink, x, y)
        if chords and on_chord(x, y):
            d_arr[i] = 255.0  # a door way: drawn threshold / chord lines do not carry a skirting
        elif d_arr[i] > tol and leaves:
            leaf_arr[i] = any(c.distance(Point(x, y)) <= tol for c in leaves)
    free = (d_arr > tol) & ~leaf_arr
    if free.all() or not free.any():
        return None
    lined = _lined_gaps(d_ink, pg, ring, ss, free, tol, ppt, step, m_per_pt) if variant.startswith("wall@") else []
    # runs of free samples, the ring rotated so it starts on a supported sample
    r0 = int(np.argmin(free))
    order = np.r_[r0:k, 0:r0]
    opens, cur = [], None
    for idx in order:
        if free[idx]:
            cur = [idx, idx] if cur is None else [cur[0], idx]
        elif cur is not None:
            opens.append(cur)
            cur = None
    if cur is not None:
        opens.append(cur)
    max_walk = int(np.ceil(2 * tol)) + 1
    openings = []
    for i0, i1 in opens:
        # each end walked out to the first sample ON ink (the jamb's line), not along a door leaf
        for _ in range(max_walk):
            j = (i0 - 1) % k
            if free[j] or leaf_arr[j] or d_arr[j] <= 0.5 or j == i1:
                break
            i0 = j
        for _ in range(max_walk):
            j = (i1 + 1) % k
            if free[j] or leaf_arr[j] or d_arr[j] <= 0.5 or j == i0:
                break
            i1 = j
        a_s, b_s = float(ss[i0]) - step / 2, float(ss[i1]) + step / 2
        if b_s < a_s:  # wrapped past the ring's start
            b_s += total_pt
        if openings and a_s - openings[-1][1] <= 2 * tol / ppt:  # a double door: two leaves meet mid-opening
            a_s = openings.pop()[0]
        openings.append((a_s, b_s, (b_s - a_s) * m_per_pt))
    if len(openings) > 1 and openings[0][0] + total_pt - openings[-1][1] <= 2 * tol / ppt:
        last = openings.pop()
        openings[0] = (last[0], openings[0][1] + total_pt, openings[0][1] + total_pt - last[0])
        openings[0] = (openings[0][0], openings[0][1], openings[0][2] * m_per_pt)
    openings = [o for o in openings if o[2] >= MIN_OPENING_M]
    if not openings:
        return {"perimeter_m": round(total_pt * m_per_pt, 3), "value_m": round(total_pt * m_per_pt, 3),
                "openings": [], "runs": [[[round(x, 2), round(y, 2)] for x, y in ring.coords]], "lined": lined}

    def piece(a, b):
        a, b = a % total_pt, b % total_pt
        if a <= b:
            g = substring(ring, a, b)
            return list(g.coords)
        return list(substring(ring, a, total_pt).coords) + list(substring(ring, 0, b).coords)[1:]

    runs, outs = [], []
    for j, (a_s, b_s, ln_m) in enumerate(openings):
        ca = piece(a_s, b_s)
        outs.append(([round(c, 2) for c in ca[0]], [round(c, 2) for c in ca[-1]], round(ln_m, 3)))
        nxt = openings[(j + 1) % len(openings)][0]
        if nxt <= b_s:
            nxt += total_pt
        if nxt - b_s > step:
            runs.append([[round(x, 2), round(y, 2)] for x, y in piece(b_s, nxt)])
    per = total_pt * m_per_pt
    rev = _reveals(ring, total_pt, openings, tol / ppt, m_per_pt) if SKIRT_REVEALS else 0.0
    per -= rev
    val = per - sum(o[2] for o in outs)
    rc = _recess_cut(ring, outs, runs, m_per_pt) if RECESS_CUT and not rev else (0.0, 0.0, [], 0.0)
    if rc[0] > 0 and val - rc[1] > 0:
        per, val = per - rc[0], val - rc[1]
    else:
        rc = (0.0, 0.0, [], 0.0)
    return {"perimeter_m": round(per, 3), "value_m": round(val, 3), "openings": outs, "runs": runs,
            "reveals_m": round(rev, 3), "lined": lined, "recess_m": round(rc[1], 3), "recess_n": len(rc[2]),
            "recess_per_m": round(rc[0], 3), "recess_area_m2": round(rc[3], 3)}


def _behind_gaps(pdf: str, page: int, n: float, m: dict, lined: list) -> float:
    """(area m², polygons) of the unlabelled spaces just BEHIND the lined gaps of a walls-only room (the closet behind the
    sliding doors of the AFCEE Break Room: schedule 153 SF = room + closet). Seeded 0.45 m past each gap's middle, away
    from the room; a space larger than the room itself or holding another room's label is not counted."""
    from shapely.geometry import Point, Polygon

    from stoa.evidence import linework as L

    try:
        room = Polygon(m["polygon"]).buffer(0)
        c = room.centroid
    except Exception:  # noqa: BLE001
        return 0.0, []
    u = 1.0 / (n * L.PT_TO_M_AT_1)  # pt per metre
    tot, polys = 0.0, []
    for a, b, _ln in lined:
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        vx, vy = b[0] - a[0], b[1] - a[1]
        ll = (vx * vx + vy * vy) ** 0.5 or 1.0
        nx, ny = -vy / ll, vx / ll
        if (mx + nx - c.x) ** 2 + (my + ny - c.y) ** 2 < (mx - nx - c.x) ** 2 + (my - ny - c.y) ** 2:
            nx, ny = -nx, -ny  # point away from the room
        seed = (mx + nx * 0.45 * u, my + ny * 0.45 * u)
        if room.contains(Point(seed)):
            continue
        t = L.measure_room(pdf, page, seed, n, own_radius_pt=0.0, base_variants=[f"wall@{n:g}"])
        if t is None or t["area_m2"] >= m["area_m2"]:
            continue
        tot += t["area_m2"]
        polys.append(t["polygon"])
    return round(tot, 2), polys


def _lined_gaps(d_ink, pg, ring, ss, free, tol: float, ppt: float, step: float, m_per_pt: float) -> list:
    """On a walls-only outline ('wall@N'): stretches that run along drawn lines but NOT along a wall fill = a gap in the
    wall carrying a window, a glazed screen or a SLIDING / folding door (AFCEE A-101 Break Room closet: two offset
    door panels across a 6'-0" doorway; X1 sl-243 deducts it). Skirting stays across them (windows have wall below,
    Ziying xr-14), but the answer states them. Returns [(a_pt, b_pt, len_m)] of gaps >= MIN_OPENING_M."""
    import cv2
    import numpy as np

    if not pg.get("fills"):
        return []
    fm = np.zeros(pg["shape"], np.uint8)
    cv2.fillPoly(fm, pg["fills"], 255)
    if d_ink is not None and d_ink.shape == fm.shape:
        # the fill AS PRINTED: Revit draws one poché rectangle along the whole wall and CLIPS the door ways out of it
        # (AFCEE closet: fill 2041.8-2130.3 pt, printed only on the two stubs); door panels / glazing lines inside the
        # band print only part of it
        pr = ((fm > 0) & (d_ink == 0)).astype(np.float32)
        f = (fm > 0).astype(np.float32)
        w = max(5, int(round(4.0 * ppt)) | 1)  # ~4 pt: a wall band printed SOLID vs door panels / glazing inside it
        num = cv2.boxFilter(pr, -1, (w, w), normalize=False)
        den = cv2.boxFilter(f, -1, (w, w), normalize=False)
        fm = ((fm > 0) & (num >= 0.85 * np.maximum(den, 1.0))).astype(np.uint8) * 255
    dfill = cv2.distanceTransform((fm == 0).astype(np.uint8), cv2.DIST_L2, 3)
    H, W = dfill.shape
    nof = np.zeros(len(ss), dtype=bool)
    for i, s_ in enumerate(ss):
        x, y = _px(ring, s_, ppt)
        xi, yi = int(round(x)), int(round(y))
        nof[i] = not (0 <= xi < W and 0 <= yi < H) or dfill[yi, xi] > tol + 1
    m = nof & ~free
    if not m.any() or m.all():
        return []
    k = len(m)
    r0 = int(np.argmin(m))
    out, cur = [], None
    for idx in list(range(r0, k)) + list(range(0, r0)):
        if m[idx]:
            cur = [idx, idx] if cur is None else [cur[0], idx]
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    # two sliding panels overlap mid-doorway: that short stretch prints the band solid -> runs <= 0.35 m apart merge
    j = 1
    while j < len(out):
        if ((out[j][0] - out[j - 1][1]) % k) * step * m_per_pt <= 0.35:
            out[j - 1] = [out[j - 1][0], out[j][1]]
            del out[j]
        else:
            j += 1
    gaps = []
    for i0, i1 in out:
        n_ = (i1 - i0) % k + 1
        ln = (n_ * step + 2 * tol / ppt) * m_per_pt  # the fill's distance test shortens each end by ~tol
        if ln >= MIN_OPENING_M:
            a, b = ring.interpolate(float(ss[i0])), ring.interpolate(float(ss[i1]))
            gaps.append(([round(a.x, 2), round(a.y, 2)], [round(b.x, 2), round(b.y, 2)], round(ln, 3)))
    return gaps


SKIRT_REVEALS = bool(int(__import__("os").environ.get("E1_SKIRT_REVEALS", "0")))  # OFF: traced copies cut openings part-way down a jamb (cycle 10)
REVEAL_W = tuple(float(x) for x in __import__("os").environ.get("E1_REVEAL_W", "0.8,1.6").split(","))  # wall-line fit window (m)
REVEAL_MAX_M = 0.7  # a wall's depth: deeper 'recesses' are alcoves / corridors, not door reveals


RECESS_CUT = bool(int(__import__("os").environ.get("E1_RECESS_CUT", "1")))
RECESS_MAX_M = 0.75  # deeper than a wall: an alcove / passage, keeps its skirting
RECESS_LEAVES = bool(int(__import__("os").environ.get("E1_RECESS_LEAVES", "1")))
RECESS_PERIM = bool(int(__import__("os").environ.get("E1_RECESS_PERIM", "1")))  # perimeter answers lose the same parts
RECESS_AREA = bool(int(__import__("os").environ.get("E1_RECESS_AREA", "1")))  # traced pages: area answers lose their area
RECESS_AREA_MAX_M2 = 100.0
SLIT_MAX_M = 3.0  # a slit this deep: a hollow wall's cavity the trace ran into (D1 dfe26 PNG Entrance and Cloakroom)
SLIT_W_M = 0.3  # an outward excursion narrower than this is no part of the room (a trace running out along a line)


def _recess_area(pdf: str, page: int, n: float, m: dict) -> float:
    """m² of a traced picture room's region inside door recesses / narrow wall cavities (0 when not applicable: not a
    traced page, a hall >= RECESS_AREA_MAX_M2, or > 15 % of the room)"""
    from stoa.evidence import linework as L

    val = float(m.get("area_m2") or 0.0)
    if not (RECESS_AREA and 0 < val < RECESS_AREA_MAX_M2 and (L._page(pdf, page) or {}).get("traced")):
        return 0.0
    try:
        sk = _skirting(pdf, page, n, m)
    except Exception:  # noqa: BLE001
        return 0.0
    cut = float((sk or {}).get("recess_area_m2") or 0.0)
    return cut if 0.01 <= cut < 0.15 * val else 0.0


def _recess_cut(ring, outs: list, runs: list, m_per_pt: float) -> tuple[float, float, list, float]:
    """(perimeter cut m, skirting cut m, [(cut m, why)], area cut m²) for the outline's DOOR RECESSES and SLITS (D1 00:46, F2 01:38:
    on a traced picture the walls are outlines, not fills, so a door way closes at the wall's OUTER face and the room runs
    into the recess: Stoa's DfE PNG Multi-Purpose Hall 68.47 m outline vs 63.44 on the PDF, skirting 55.45 vs gold 50.4;
    Kitchen 25.32 vs 22.0). (1) Slits: the outline running out along a stray line and back (a mitre-joined opening of
    the room at SLIT_W_M removes them; 0.2-1.2 m deep, mouth <= SLIT_W_M). (2) Recesses: for each opening <= 2.6 m, the
    mitre-joined opening of the slit-free room at the opening's width (+ 0.4 m) removes the recess; kept when it holds the
    opening's middle, is 0.08-RECESS_MAX_M deep and its mouth is 0.5-1 x the opening (+ 1 m). The outline loses each
    piece's length minus its mouth; the skirting loses the runs inside the pieces (the jambs). A legit narrow part of the
    room (< 2 x the radius) is never touched unless it is that shallow and has the opening in it (a door at the back of a
    <= 0.75 m niche: reveals, no skirting, Ziying's convention)."""
    import numpy as np
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import linemerge, unary_union

    try:
        P = Polygon(ring.coords).buffer(0)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, [], 0.0
    if P.geom_type != "Polygon" or P.area <= 0:
        return 0.0, 0.0, [], 0.0
    eps = 0.03 / m_per_pt

    def opened(G, r_m):
        return G.buffer(-r_m / m_per_pt, join_style=2, mitre_limit=5.0).buffer(r_m / m_per_pt, join_style=2,
                                                                                mitre_limit=5.0)

    def pieces(rg, Q):
        out = rg.difference(Q.buffer(eps, join_style=2))
        if out.is_empty:
            return []
        if out.geom_type == "MultiLineString":
            out = linemerge(out)
        return [g for g in getattr(out, "geoms", [out]) if g.geom_type == "LineString" and g.length > 0]

    def chord(pc) -> float:
        (ax, ay), (bx, by) = pc.coords[0][:2], pc.coords[-1][:2]
        return float(np.hypot(ax - bx, ay - by)) * m_per_pt

    def depth(pc, Q) -> float:
        return max(Q.exterior.distance(Point(c)) for c in pc.coords) * m_per_pt

    P0 = opened(P, SLIT_W_M / 2)
    if P0.is_empty or P0.geom_type != "Polygon" or P0.area < 0.8 * P.area:
        return 0.0, 0.0, [], 0.0
    cut, taken = [], []
    area, used = [0.0], set()

    def comps_near(G, pc, tag):  # the area of G's parts this piece bounds (each part once)
        for i, c in enumerate(getattr(G, "geoms", [G])):
            if (tag, i) not in used and not c.is_empty and c.distance(pc) < eps:
                used.add((tag, i))
                area[0] += c.area * m_per_pt * m_per_pt
    rest0 = P.difference(P0)
    for pc in pieces(ring, P0):
        ch = chord(pc)
        if ch <= 1.5 * SLIT_W_M and 0.2 <= depth(pc, P0) <= SLIT_MAX_M:
            cut.append((pc.length * m_per_pt - ch, "slit"))
            taken.append(pc)
            comps_near(rest0, pc, "s")
    if RECESS_LEAVES:
        # (1b) door LEAVES standing open in the room: the outline runs up one side of the leaf line and back down
        # (DfE PNG hall: 0.72 + 0.62 m at a double door's hinge). An inward notch < SLIT_W_M wide, 0.25-1.2 m deep,
        # starting within 0.15 m of an opening's end; a nib wall elsewhere keeps its skirting
        C = P.buffer(SLIT_W_M / 2 / m_per_pt, join_style=2, mitre_limit=5.0).buffer(
            -SLIT_W_M / 2 / m_per_pt, join_style=2, mitre_limit=5.0)
        ends = [Point(tuple(e)) for o in outs for e in o[:2]]
        if C.geom_type == "Polygon" and ends:
            rest = ring.difference(C.exterior.buffer(eps, join_style=2))
            if rest.geom_type == "MultiLineString":
                rest = linemerge(rest)
            for pc in [g for g in getattr(rest, "geoms", [rest]) if g.geom_type == "LineString" and g.length > 0]:
                ch = chord(pc)
                dp = max(C.exterior.distance(Point(c)) for c in pc.coords) * m_per_pt
                if ch <= SLIT_W_M and 0.25 <= dp <= 1.2 and min(e.distance(pc) for e in ends) * m_per_pt <= 0.15 and \
                        sum(LineString(r).intersection(pc.buffer(eps)).length for r in runs if len(r) >= 2) \
                        >= 0.6 * pc.length:  # counted as wall (a leaf), not a notch the openings already took
                    cut.append((pc.length * m_per_pt - ch, "door leaf"))
                    taken.append(pc)
    # door leaves standing open into the room (thin inward notches) closed first: the bay between a double door's two
    # leaves is the swing, not a recess (MFC r100 laundry: 200.5 -> 188.7 ft² vs 200 before this)
    P0c = P0.buffer(SLIT_W_M / 2 / m_per_pt, join_style=2, mitre_limit=5.0).buffer(
        -SLIT_W_M / 2 / m_per_pt, join_style=2, mitre_limit=5.0) if RECESS_LEAVES else P0
    if P0c.geom_type == "Polygon" and not P0c.is_empty:
        P0 = P0c
    ring0 = LineString(list(P0.exterior.coords))
    for a, b, ln in outs:
        if ln > 2.6:
            continue
        mid = LineString([tuple(a), tuple(b)]).interpolate(0.5, normalized=True)
        Q = opened(P0, ln / 2 + 0.2)
        if Q.is_empty or Q.geom_type != "Polygon" or Q.area < 0.8 * P0.area:
            continue  # the room itself is about that narrow: no way to tell a recess from the room
        for pc in pieces(ring0, Q):
            if pc.distance(mid) > 0.1 / m_per_pt or any(pc.distance(t) < eps and t.length >= pc.length for t in taken):
                continue
            ch = chord(pc)
            dp = depth(pc, Q)
            jambs = sum(LineString(r).intersection(pc.buffer(eps)).length for r in runs if len(r) >= 2) * m_per_pt
            if 0.08 <= dp <= RECESS_MAX_M and 0.5 * ln <= ch <= ln + 1.0 and \
                    jambs >= 0.5 * (pc.length * m_per_pt - ch) and Q.distance(mid) * m_per_pt >= 0.5 * dp:
                # its sides are drawn walls (jambs) and the door way is at its BACK (the wall's outer face); a strip
                # of room behind a door way closed at the room's edge is room (MFC scan laundry, 197.3 -> 192.7 ft²)
                cut.append((pc.length * m_per_pt - ch, f"door recess at the {ln:.2f} m opening"))
                taken.append(pc)
                comps_near(P0.difference(Q), pc, f"r{ln:.3f}")
    if not taken:
        return 0.0, 0.0, [], 0.0
    region = unary_union([pc.buffer(eps) for pc in taken])
    sk = sum(LineString(r).intersection(region).length for r in runs if len(r) >= 2) * m_per_pt
    return sum(c for c, _ in cut), sk, cut, area[0]


def _reveals(ring, total_pt: float, openings: list, tol_pt: float, m_per_pt: float) -> float:
    """Door REVEALS on the outline (H 20:37, Ziying's Multi-Purpose Hall on Stoa's DfE PNG: inner wall faces, 'door
    reveals not added'; our trace on the picture ran into every door recess up to the door line, +4.5 m). At each
    opening the room's WALL LINE is fitted through the outline 0.8-1.6 m either side of it (straight, else skipped); the
    outline's EXCURSION beyond that line, on the side away from the room (a recess, <= REVEAL_MAX_M deep), is longer than
    its straight chord by the two jambs -> that excess is not wall face. Independent of where the opening's ends were
    put (on traced copies they sit part-way down a jamb). A column standing into the room bulges the other way."""
    import numpy as np
    from shapely.geometry import Polygon

    def pt(sv):
        q = ring.interpolate(sv % total_pt)
        return np.array([q.x, q.y])

    try:
        cen = np.array(Polygon(ring.coords).centroid.coords[0])
    except Exception:  # noqa: BLE001
        return 0.0
    w0, w1, lim = REVEAL_W[0] / m_per_pt, REVEAL_W[1] / m_per_pt, REVEAL_MAX_M / m_per_pt
    st = max(tol_pt, 0.5)
    out = 0.0
    for a_s, b_s, _ln in openings:
        if b_s - a_s > 3.0 / m_per_pt:
            continue
        back = [pt(a_s - t) for t in np.arange(w0, w1, st)]
        fwd = [pt(b_s + t) for t in np.arange(w0, w1, st)]
        line = None
        # one straight wall line through both sides; else (a door next to a corner) the one straight side alone
        for P in (np.array(back + fwd), np.array(back), np.array(fwd)):
            if len(P) < 4:
                continue
            c = P.mean(axis=0)
            _, _, vt = np.linalg.svd(P - c)
            if float(np.abs((P - c) @ vt[1]).max()) <= 1.5 * tol_pt:
                line = (c, vt[1])
                break
        if line is None:
            continue  # no straight wall line next to the opening
        c, nrm = line

        def dist(q):
            return float((q - c) @ nrm)

        s1 = a_s
        while s1 > a_s - w0 and abs(dist(pt(s1))) > tol_pt:
            s1 -= st
        s2 = b_s
        while s2 < b_s + w0 and abs(dist(pt(s2))) > tol_pt:
            s2 += st
        ds = [dist(pt(x)) for x in np.arange(s1, s2, st)]
        if not ds:
            continue
        far = max(ds, key=abs)
        depth = abs(far)
        if not 0.05 / m_per_pt <= depth <= lim or np.sign(far) == np.sign(dist(cen)):
            continue  # too shallow / too deep, or it bulges INTO the room
        excess = (s2 - s1) - float(np.hypot(*(pt(s2) - pt(s1))))
        if excess > 0:
            out += min(excess, 2 * depth + 2 * tol_pt) * m_per_pt
    return out


_NO_DEDUCT = re.compile(r"\b(?:do(?:es)?\s*n[o’']t|not|without|no)\s+(?:subtract\w*|deduct\w*|minus\w*|remov\w*|"
                        r"tak\w*\s+(?:off|out)|exclud\w*|leav\w*\s+out)\b|\bignor\w*\s+(?:the\s+|any\s+)?(?:doors?|openings?)|"
                        r"\bincluding\s+(?:the\s+|any\s+)?(?:doors?|door\s*ways?|openings?)|\bacross\s+(?:the\s+)?(?:doors?|"
                        r"openings?)|\bgross\b|\bfull\s+perimeter\b", re.I)
_WHOLE_FLOOR_Q = re.compile(r"\b(?:whole|entire|complete|this|the)\s+(?:floor|level|storey|story|plan|building|school|"
                            r"house|apartment|flat|ground floor|first floor)\b|\ball (?:the )?rooms\b|"
                            r"\b(?:every|each) room\b", re.I)
_NO_SKIRTING = re.compile(r"\b(stair\w*|lift\w*|elevator\w*|shaft\w*|riser\w*|void\w*|duct\w*|plant|walkway|"
                          r"canopy|terrace\w*|balcon\w*|courtyard|external|open to below|disclaimer)\b", re.I)


def _iou_poly(a: list, b: list) -> float:
    try:
        from shapely.geometry import Polygon

        pa, pb = Polygon(a).buffer(0), Polygon(b).buffer(0)
        u = pa.union(pb).area
        return pa.intersection(pb).area / u if u > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


_ACROSS = re.compile(r"\bacross\s+(?:the\s+|an?\s+|its\s+)?(?!.{0,12}\bwalls?\b)", re.I)
_COND = re.compile(r"^\s*(?:no|not|never|leave|leaving|exclud\w*|without|except|ignor\w*|do\s*n[o’']t|don[’']t|but|"
                   r"skip|omit\w*)\b|\bleav\w*\s+(?:\w+\s+){0,4}out\b", re.I)


def _rooms_text(q: str) -> str:
    """The question without its CONDITION sentences / clauses ('No base across the open entrance from the corridor or
    across the closet doorway, and leave the closet itself out.', X1 sl-243): they name rooms that are NOT asked for."""
    sents = [x for x in re.split(r"(?<=[?.!])\s+", q) if x.strip()]
    keep = [x for x in sents if not _COND.search(x)] or sents[:1]
    return " ".join(keep)


_OPEN_Q = re.compile(r"\b(?:how\s+wide|width|wide)\b.{0,30}\b(?:opening|door\s*way|entrance|passage|archway)\b"
                     r".{0,20}\b(?:into|to|of|for|in)\b", re.I)
_OPEN_ROOM = re.compile(r"\b(?:into|to|of|for|in)\s+(?:the\s+)?(.+?)(?:\s+(?:from|off|onto|towards?)\s+(?:the\s+)?"
                        r"(.+?))?\s*[?.!]*\s*$", re.I)


def _opening_answer(path: str, pdf: str, page: int, q: str, n: float, labs: list[dict],
                    all_labs: list[dict]) -> Answer | None:
    """'How wide is the opening into the Break Room 2.02 from the corridor?' (X1 sl-245, AFCEE A-101: an OPEN entrance,
    no door drawn, gold 4.67 ft): the room traced as for skirting, its openings measured jamb to jamb (_skirting). One
    opening -> its width; several -> the one nearest the named neighbour's label ('from the corridor'), else None."""
    mo = _OPEN_ROOM.search(_rooms_text(q))
    if not mo:
        return None
    hits = _match_labels(labs, mo.group(1))
    if len(hits) != 1:
        return None
    lab = hits[0]
    if _door_check(pdf, page, n)[0] is not None and not 0.6 <= _door_check(pdf, page, n)[0] <= 1.3:
        return None
    m = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]), lab.get("line_h"))
    note = None
    others = [x for x in all_labs if x not in labs]
    if m is not None and others:
        note = _twin_check(pdf, page, n, lab, m, others)
        if note is False:
            wt = _wall_twins(pdf, page, n, lab, others)
            if wt is None:
                return None
            m, note = wt
    sk = _skirting(pdf, page, n, m) if m is not None else None
    if not sk or not sk["openings"]:
        return None
    ops = sk["openings"]
    conf, why = 0.5, "the room's only opening"
    if len(ops) > 1:
        nb = _match_labels(labs, mo.group(2)) if mo.group(2) else []
        if not nb:
            return None
        def d2(o):
            cx, cy = (o[0][0] + o[1][0]) / 2, (o[0][1] + o[1][1]) / 2
            return min((cx - b["pt"][0]) ** 2 + (cy - b["pt"][1]) ** 2 for b in nb)
        ops = sorted(ops, key=d2)
        conf, why = 0.35, f"of {len(sk['openings'])} openings, the one nearest the '{nb[0]['text']}' label"
    a, b, w = ops[0]
    name = lab["text"][:40]
    ev = [Evidence(file=path, page=page, bbox=_poly_bbox([a, b]), bbox_space="pdf_pt", exact=False, polyline=[a, b],
                   note=f"{name}: opening {w:.2f} m jamb to jamb at 1:{n:g} (approximate)"),
          Evidence(file=path, page=page, bbox=_poly_bbox(m["polygon"]), bbox_space="pdf_pt", exact=False,
                   polygon=m["polygon"], note=f"{name}: traced outline (approximate)")]
    assumptions = [_scale_words(path, page, n) + f"; traced from the wall lines (approximate)",
                   f"opening = {why}: a stretch of the room's outline with no wall line along it, measured jamb to jamb "
                   "(wall end to wall end; a door frame would make the clear width a little smaller)"]
    if note:
        assumptions.append(note)
    return Answer(value=round(w, 3), unit="m", confidence=conf, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=["no opening width printed: a door schedule / dimension string would make it exact"],
                  trace=[f"lineplan: opening of '{name}' at printed 1:{n:g}: "
                         + ", ".join(f"{o[2]:.2f}" for o in sk["openings"]) + f" m -> {w:.2f} m"])


def _baseboard_answer(path: str, pdf: str, page: int, q: str, n: float, labs: list[dict],
                      all_labs: list[dict] | None = None) -> Answer | None:
    """'How many linear metres of skirting board does the laundry need?' — every named room (a plural takes every room
    so labelled; 'the laundry and the kitchen' sums) traced, its outline minus the openings in its walls (_skirting).
    Evidence per room: the skirting runs as polylines (outline with the openings cut out) + each opening's width."""
    from stoa.evidence import linework as L

    qn = _BASE_WORDS.sub(" ", _BASE_Q.sub(" ", re.sub(r"\(.*?\)", " ", _rooms_text(q))))
    parts = [p for p in _SPLIT.split(qn) if p and _words(p) and not _COND.search(p)] or [qn]
    picked: list = []
    for part in parts:
        hits = _match_labels(labs, part)
        if not hits:
            if any(w not in _GENERIC and not _SUM_Q.fullmatch(w) for w in _words(part)) and len(parts) > 1:
                return None  # a named part with no label on the sheet: the total would miss it
            continue
        if len(hits) > 1:
            plural = _PLURAL_PART.search(re.sub(r"\b(rooms?|räume|zimmer)\b", "", part, flags=re.I)) or \
                re.search(r"\b(all|every|each|both|total|alle\w*|jede\w*)\b", q, re.I)
            if not plural:
                pos = _position(q)
                pick, _why = _pick([{**h} for h in hits], pos) if pos else (None, "")
                if pick is None:
                    return None  # 'the group room' with ten on the sheet
                hits = [pick]
        for h in hits:
            if all(h is not p for p in picked):
                picked.append(h)
    whole = False
    rest = [w for w in _words(_WHOLE_FLOOR_Q.sub(" ", qn)) if w not in _GENERIC and not _SUM_Q.fullmatch(w) and
            w not in {"order", "buy", "needed", "need", "required", "metres", "meters", "skirting", "baseboard"}]
    if not picked and not rest and _WHOLE_FLOOR_Q.search(q):  # 'for the whole floor': every room label, each room once
        picked = [lab for lab in labs if not _NO_SKIRTING.search(lab["text"]) and len(lab["text"]) <= 40]
        whole = True
    if not picked or len(picked) > (150 if whole else 16):
        return None
    d_med, n_doors = _door_check(pdf, page, n)
    if d_med is not None and not 0.6 <= d_med <= 1.3:
        return None
    rows, missed = [], []
    others = [lab for lab in (all_labs or []) if lab not in labs]
    notes = []
    for lab in picked:
        m = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]), lab.get("line_h"))
        if m is not None and others and not whole:  # the tag repeated in the ceiling plan: a witness (as in _answer)
            tn = _twin_check(pdf, page, n, lab, m, others)
            if tn is False:
                wt = _wall_twins(pdf, page, n, lab, others)
                if wt is None:
                    return None
                m, tn = wt
            if tn:
                notes.append(tn)
        sk = _skirting(pdf, page, n, m) if m is not None else None
        if sk is None or (whole and not 1.0 <= m["area_m2"] <= 1500):
            if not whole:
                return None
            missed.append(lab["text"])
            continue
        if whole and any(_iou_poly(m["polygon"], r[1]["polygon"]) > 0.8 for r in rows):
            continue  # two labels in one room ('Changing' over 'and Showers' read apart): counted once
        if not whole:
            try:  # printed dimensions spanning the traced room: its perimeter from the numbers (A-101F toilet:
                #   2 x (1900 + 2054) = 7.908 m minus the 0.91 m door way = 6.998, X1 gold 6.993)
                from stoa.evidence import dimrect as D

                dr = D.rect_spanning(pdf, page, n, tuple(_poly_bbox(m["polygon"])),
                                     tol_pt=0.15 / (n * L.PT_TO_M_AT_1))
            except Exception:  # noqa: BLE001
                dr = None
            if dr is not None:
                per = 2 * (dr["w_mm"] + dr["h_mm"]) / 1000
                sk = {**sk, "perimeter_m": round(per, 3), "value_m": round(per - sum(o[2] for o in sk["openings"]), 3),
                      "dims": (dr["w_mm"], dr["h_mm"])}
        rows.append((lab, m, sk))
    if whole and (len(rows) < 2 or len(missed) > len(rows)):
        return None
    deducted = round(sum(sk["value_m"] for _, _, sk in rows), 2)
    keep_doors = bool(_NO_DEDUCT.search(q))  # 'do not subtract the doors' / 'ignoring door openings' (D1 17:52)
    total = round(sum(sk["perimeter_m"] for _, _, sk in rows), 2) if keep_doors else deducted
    ev, terms = [], []
    for lab, m, sk in rows:
        name = lab["text"][:40]
        ops = sk["openings"]
        op_txt = ", ".join(f"{o[2]:.2f}" for o in ops) or "none"
        dm = sk.get("dims")
        terms.append(f"{name} {sk['perimeter_m']:.2f}" + (f" (printed {dm[0]} x {dm[1]} mm)" if dm else "")
                     + (f" (door reveals {sk['reveals_m']:.2f} m left out)" if sk.get("reveals_m") else "")
                     + (f" (no skirting along door recesses / open leaves / slits of the traced outline: {sk['recess_m']:.2f} m)"
                        if sk.get("recess_m") else "")
                     + f" - ({op_txt}) = {sk['value_m']:.2f} m")
        for i, run in enumerate(sk["runs"]):
            ev.append(Evidence(file=path, page=page, bbox=_poly_bbox(run), bbox_space="pdf_pt", exact=False,
                               polyline=run, polygon=m["polygon"] if i == 0 else None,
                               note=f"{name}: skirting {sk['value_m']:.2f} m = outline {sk['perimeter_m']:.2f} m "
                                    f"minus {len(ops)} opening{'s' * (len(ops) != 1)} at 1:{n:g} (approximate)"
                               if i == 0 else f"{name}: skirting run (approximate)"))
        for a, b, ln in ops:
            ev.append(Evidence(file=path, page=page, bbox=_poly_bbox([a, b]), bbox_space="pdf_pt", exact=False,
                               polyline=[a, b], note=f"{name}: opening {ln:.2f} m (no skirting)"))
    conf = 0.5 if all(m.get("support", 0) >= 3 for _, m, _ in rows) else 0.35
    if len(rows) > 1:
        conf = min(conf, 0.45)
    lined = [(lab["text"][:40], g) for lab, _, sk in rows for g in sk.get("lined") or []]
    named = len(_ACROSS.findall(" ".join(x for x in re.split(r"(?<=[?.!])\s+", q) if _COND.search(x))))
    n_ops = sum(len(sk["openings"]) for _, _, sk in rows)
    if lined and not keep_doors and len(rows) == 1 and named > n_ops and len(lined) <= named - n_ops:
        # the question names the openings that get no skirting ('No base across the open entrance … or across the
        # closet doorway', X1 sl-243): more than the plain openings found -> the lined gaps (a sliding door) are those
        total = round(total - sum(g[2] for _, g in lined), 2)
        terms.append("minus the sliding-door gap(s) " + ", ".join(f"{g[2]:.2f}" for _, g in lined) + f" = {total:g} m")
        notes.append(f"the question names {named} openings without skirting; {n_ops} open door way(s) found, the other"
                     f"{'s' * (len(lined) > 1)} = " + ", ".join(f"{g[2]:.2f} m" for _, g in lined)
                     + " gap(s) in the wall drawn with lines but no wall fill (a sliding door) — deducted too")
        for nm, (a, b, ln) in lined:
            ev.append(Evidence(file=path, page=page, bbox=_poly_bbox([a, b]), bbox_space="pdf_pt", exact=False,
                               polyline=[a, b], note=f"{nm}: opening {ln:.2f} m (sliding door; no skirting)"))
        lined = []
        conf = min(conf, 0.45)
    if lined and not keep_doors:  # gaps in the wall fill that carry lines: window or sliding door? (_lined_gaps)
        conf = min(conf, 0.35)
        alt = round(total - sum(g[2] for _, g in lined), 2)
        notes.append("skirting kept across " + ", ".join(f"a {g[2]:.2f} m gap in the wall ({nm})" for nm, g in lined)
                     + f" drawn with lines but no wall fill (a window, glazed screen or sliding door); if "
                     f"{'it is a doorway' if len(lined) == 1 else 'they are doorways'}: {alt:g} m")
        for nm, (a, b, ln) in lined:
            ev.append(Evidence(file=path, page=page, bbox=_poly_bbox([a, b]), bbox_space="pdf_pt", exact=False,
                               polyline=[a, b], note=f"{nm}: {ln:.2f} m gap in the wall drawn with lines (window or "
                                                     "sliding door?): skirting kept across it"))
    if whole:
        conf = min(conf, 0.3)
    assumptions = [_scale_words(path, page, n) + f"; traced from the wall lines (approximate)",
                   ("skirting = the room's whole traced outline (wall faces), door openings NOT deducted as asked; "
                    f"with the openings deducted it would be {deducted:g} m") if keep_doors else
                   "skirting = the room's traced outline (wall faces) minus every opening in its walls, measured jamb "
                   "to jamb (door ways and open passages); built-in units and fixed furniture not deducted",
                   *([f"sum of {len(rows)} rooms: " + "; ".join(terms)] if len(rows) > 1 else [terms[0]]),
                   *([f"whole floor = every labelled room on the sheet (stairs, lifts, shafts, plant and voids left "
                      f"out); {len(missed)} label(s) not traced"
                      + (f": {', '.join(x for x in missed if not re.search(r'[0-9]', x))[:200]}" if missed else "")]
                     if whole else [])]
    if d_med is not None:
        assumptions.append(f"scale check: {n_doors} door swings measure a median {d_med:.2f} m at 1:{n:g} ✓")
    assumptions += notes
    return Answer(value=total, unit="m", confidence=conf, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=["no skirting schedule or room dimensions printed: a finishes schedule or CAD twin would "
                           "make it exact"],
                  trace=[f"lineplan: skirting on a line-only sheet at printed 1:{n:g}: " + "; ".join(terms)])


_RANGE = re.compile(r"(?<![\w.])(\d{1,2})\s*(?:to|through|thru|until|bis|–|—|-)\s*"
                    r"(?:([A-Za-z][\w/&.]*(?:\s+[A-Za-z][\w/&.]*)?)\s+)?(\d{1,2})(?![\w.])", re.I)
_RANGE_STOP = {"of", "the", "from", "between", "in", "and", "for", "all", "area", "areas", "floor", "floors", "level",
               "levels", "storey", "storeys", "story", "stories", "sheet", "sheets", "page", "pages", "grid", "grids",
               "axis", "axes", "bay", "bays", "total", "to", "is", "are", "what", "how", "big", "metres", "meters", "m",
               "mm", "cm", "ft", "sq", "square", "step", "steps", "figure", "fig", "note", "notes", "detail", "details"}


def expand_ranges(q: str) -> str:
    """'English 1 to English 5' / 'Classroom 3-6' -> 'English 1, English 2, English 3, English 4 and English 5' (X1 scan
    sc-077: the numbered range found no label and abstained; the list sums 5 rooms = 277.1 vs gold 275 on the PDF)."""
    def rep(m):
        a, lab2, b = int(m.group(1)), m.group(2), int(m.group(3))
        if not 1 <= b - a <= 11:
            return m.group(0)
        before = q[:m.start()].rstrip().split()
        if lab2:
            k = len(lab2.split())
            lab = " ".join(before[-k:]) if len(before) >= k else ""
            if lab.lower() != lab2.lower():
                return m.group(0)
        else:
            lab = before[-1] if before else ""
        if not lab or not re.fullmatch(r"[A-Za-z][\w/&.]*(?:\s+[A-Za-z][\w/&.]*)?", lab) or \
                lab.split()[-1].lower() in _RANGE_STOP:
            return m.group(0)
        names = [f"{lab} {i}" for i in range(a, b + 1)]
        mid = names[1:-1]
        return f"{a}, {', '.join(mid)} and {names[-1]}" if mid else f"{a} and {names[-1]}"
    return _RANGE.sub(rep, q)


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        question = expand_ranges(question)
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader of last resort never breaks the cascade
        return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    from stoa.data import abspath
    from stoa.evidence import linework as L

    q = question
    if _BASE_Q.search(q):  # 'how many linear metres of skirting …' before the count words
        pdf = str(abspath(path))
        scales = L.sheet_scales(pdf, page) if pdf.lower().endswith(".pdf") else ()
        if not scales:
            return None
        all_labs = labels(pdf, page)
        return _baseboard_answer(path, pdf, page, q, scales[0], floor_plan_labels(pdf, page, all_labs), all_labs)
    if _OPEN_Q.search(q) and not _COUNT_Q.search(q):
        pdf = str(abspath(path))
        scales = L.sheet_scales(pdf, page) if pdf.lower().endswith(".pdf") else ()
        if not scales:
            return None
        all_labs = labels(pdf, page)
        return _opening_answer(path, pdf, page, q, scales[0], floor_plan_labels(pdf, page, all_labs), all_labs)
    if _COUNT_Q.search(q):
        pdf = str(abspath(path))
        return _count_answer(path, pdf, page, q) if pdf.lower().endswith(".pdf") else None
    if _THICK_Q.search(q) and _WALL_Q.search(q) and not _COUNT_Q.search(q):
        pdf = str(abspath(path))
        scales = L.sheet_scales(pdf, page) if pdf.lower().endswith(".pdf") else ()
        return _thickness_answer(path, pdf, page, q, scales[0], floor_plan_labels(pdf, page, labels(pdf, page))) if scales else None
    near_q = _NEAR_Q.search(q)  # 'the classroom nearest the north stair': the stair is a landmark, not the subject
    q_subj = q[:near_q.start()] if near_q else q
    if _COUNT_Q.search(q) or _NOT_ROOM.search(q_subj) or (_WALL_Q.search(q_subj) and not _ROOM_Q.search(q_subj)):
        return None  # 'wall to wall' / 'along the outer wall' still ask about a room; 'how long is the wall' does not
    what = ("perimeter" if _PERIM_Q.search(q) else "width" if _WID_Q.search(q) else "length" if _LEN_Q.search(q)
            else "area" if _AREA_Q.search(q) else None)
    if what is None:
        return None
    pdf = str(abspath(path))
    if not pdf.lower().endswith(".pdf"):
        return None
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return None  # no printed scale: a traced outline has no size (W5's 'Set the scale' takes over)
    n = scales[0]
    all_labs = labels(pdf, page)
    labs = floor_plan_labels(pdf, page, all_labs)
    hits = _match_labels(labs, q)
    if _SUM_Q.search(q) or (PHRASE_SUM and _ADD_Q.search(q)):
        r = _sum_answer(path, pdf, page, q, n, labs, what)
        if r is not None or len(hits) != 1 or len(_SPLIT.split(_UNIT_TAIL.sub("?", q) if PHRASE_SUM else q)) > 2:
            return r
        # ONE named room with a 'both / all / total' word ('the multi-purpose hall, both halves together (partition
        # open)', X1 sl-059): that room, measured as one
    if ROOM_EXACT and len(hits) > 1 and len(_QUOTED.findall(q)) <= 1:
        # D1 02:0x: 'How big is the room labelled 'Toilet'' (DfE SEN L01) matched 'Toilets' too (plural fold) -> two rooms
        # of different size -> None; the one label that IS the quoted / named text is the room asked about
        ex = _exact_labels(labs, q)
        if ex and not ex[2] and len(ex[0]) == 1:
            hits = ex[0]
    near = _NEAR_Q.search(q)
    if near and hits:
        hits = _match_labels(labs, q[:near.start()])
        if not hits:
            return None  # 'the room next to the kitchen': the subject is unnamed, the kitchen only a landmark
    pos = _position(q[:near.start()] if near else q)
    how = ""
    tag_box = None
    why = ""
    if near and len(hits) > 1:
        pick, why = _nearest_to(labs, hits, near.group(2))
        if pick is None:
            return None
        hits, pos = [pick], None
    if hits:
        if len(hits) > 1:
            if pos is None:  # 'one of the quiet rooms': equal rooms measured together; 'the meeting room' x4 differ
                return _equal_rooms_answer(path, pdf, page, n, hits, what)
            pick, why = _pick([{**h, "bbox": h["bbox"]} for h in hits], pos)
            if pick is None:
                return None
            how = f"{len(hits)} labels read '{hits[0]['text']}'; {why}"
        else:
            pick = hits[0]
            how = f"the room labelled '{pick['text']}'" + (f", {why}" if why else "")
        seed, tag_box, name = pick["pt"], tuple(pick["bbox"]), pick["text"]
        conf = 0.55
    elif pos is not None:
        rs = rooms(pdf, page, n)
        if len(rs) < 2:
            return None
        pick, why = _pick(rs, pos)
        if pick is None:
            return None
        seed, name = pick["pt"], why
        how = f"{why}, among {len(rs)} closed rooms traced on the plan"
        conf = 0.4
    else:
        return None
    door_med, n_doors = _door_check(pdf, page, n)
    if door_med is not None and not 0.6 <= door_med <= 1.3:
        return None  # door leaves are ~0.7-1.0 m: the printed scale is not this view's (or the arcs are not doors)
    behind, behind_polys, asked_out = 0.0, [], None
    m = _measure(pdf, page, seed, n, tag_box, pick.get("line_h")) if tag_box else L.measure_room(pdf, page, seed, n)
    walls_note = None
    m_pocket = None
    if m is not None and POCKET_GUARD and WALLS_FALLBACK and tag_box and \
            _pocket(_poly_bbox(m["polygon"]), tag_box, name, q_subj, m["area_m2"]):
        m_pocket, m = m, None  # a pocket round the label (court markings): the walls-only trace may hold the room
    if m is None and WALLS_FALLBACK and tag_box:
        # X1 23:20 (EFA superblock Sports Hall, court markings drawn inside): every line reading stays in a court cell
        # around the label. On a sheet with solid wall fills the room traced from its WALLS alone (court / floor
        # markings, furniture and built-in lines ignored), when >= 4 of its readings agree
        pgw = L._page(pdf, page)
        if pgw is not None and len(pgw["fills"]) >= 50:
            mw = _measure(pdf, page, seed, n, tag_box, pick.get("line_h"), base_variants=[f"wall@{n:g}"])
            if mw is not None and mw.get("support", 0) >= 4:
                m = mw
                conf = min(conf, 0.45)
                walls_note = ("traced from the WALLS only (wall fills, door radii; lines inside the room such as court "
                              "or floor markings and furniture ignored): every reading of the drawn lines stopped at "
                              "lines around the label")
    if m is None and m_pocket is not None:
        m = m_pocket  # flagged below (confidence <= 0.1)
    printed = None
    if PRINTED_TAG and what == "area" and tag_box:
        try:
            printed = _printed_area(pdf, page, pick, all_labs)
        except Exception:  # noqa: BLE001
            printed = None
        if printed is not None and (m is None or abs(m["area_m2"] - printed[0]) > PRINTED_TAG_TOL * printed[0]):
            return _printed_answer(path, page, n, name, how, printed, m)
    if m is None:
        return None
    twin_note = walls_note
    if tag_box and len(labs) < len(all_labs):  # the same room tag printed in another view (RCP): a second witness
        others = [lab for lab in all_labs if lab not in labs]
        twin_note = _twin_check(pdf, page, n, pick, m, others)
        if twin_note is False:
            wt = _wall_twins(pdf, page, n, pick, others)
            if wt is None:
                return None
            m, twin_note = wt
            sk = _skirting(pdf, page, n, m)
            lined = (sk or {}).get("lined") or []
            if lined:  # a sliding door / glazed gap bounds the walls-only room: is the space behind it part of it?
                behind, behind_polys = _behind_gaps(pdf, page, n, m, lined)
                twin_note += ("; a " + ", ".join(f"{g[2]:.2f} m" for g in lined) + " gap in the wall drawn with lines "
                              "(a sliding door or glazed screen) bounds the room: the space behind it is LEFT OUT "
                              "(a room schedule may count a closet behind sliding doors as part of the room)"
                              + (f"; that space measures {behind:.2f} m², room + it = {m['area_m2'] + behind:.2f} m²"
                                 if behind else ""))
            asked_out = re.search(r"\b(?:without|excluding|exclude|not\s+including|leav\w*\s+(?:\w+\s+){0,4}out)\b", q, re.I)
            conf = min(conf, 0.35 if lined and not asked_out else 0.45)
    poly = m["polygon"]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    bbox = [min(xs), min(ys), max(xs), max(ys)]
    if m.get("support", 0) < 3:
        conf = min(conf, 0.35)
    pocket = _pocket(bbox, tag_box, name, q_subj, m["area_m2"]) if POCKET_GUARD else None
    if pocket:
        conf = min(conf, 0.1)
    open_zone = bool(tag_box) and bool(_OPEN_ZONE.search(name))
    if open_zone:
        conf = min(conf, 0.4)
    scale_line = _scale_words(path, page, n) + f"; traced from the wall lines (approximate)"
    assumptions = [scale_line, f"room = {how}",
                   f"its outline traced from the drawn wall lines around that point (door openings closed at "
                   f"{2 * m['gap_m']:.2f} m, line thickness corrected; {m['support']} of {m['n_readings']} readings "
                   "agree)"]
    missing = ["no room area is printed on the sheet: a printed area or a CAD/IFC twin would make it exact"]
    if "bridge@" in str(m.get("variant")) and not m.get("reveal_cut"):  # E1 c8 fallback: EFA probe 1 W / 2 L of 3 former abstentions -> rough
        conf = min(conf, 0.4)
        assumptions.append("openings wider than a door were closed along their wall line (the room otherwise runs "
                           "into its neighbours): a rough outline")
    if m.get("seed_note"):
        assumptions.append(m["seed_note"])
    if twin_note:
        assumptions.append(twin_note)
    if pocket:
        assumptions.append(pocket)
    if open_zone:
        assumptions.append(f"'{name[:40]}' names an open zone (lobby / waiting / corridor / area): its schedule area may "
                           "end at room separation lines that are not drawn, so the outline closes along the nearest "
                           "drawn lines (AF module plans: 4 of 8 such zones right at this confidence)")
    if door_med is not None:
        assumptions.append(f"scale check: {n_doors} door swings measure a median {door_med:.2f} m at 1:{n:g} "
                           "(a door leaf is ~0.7-1.0 m) ✓")
    sides = m.get("sides_m") or []
    ev_kw = dict(file=path, page=page, bbox=bbox, bbox_space="pdf_pt", exact=False)
    dr = None
    if what == "area":
        try:  # printed dimensions spanning exactly this room (each end within 0.15 m): the numbers win (A-101F toilet)
            from stoa.evidence import dimrect as D

            dr = D.rect_spanning(pdf, page, n, tuple(bbox), tol_pt=0.15 / (n * L.PT_TO_M_AT_1))
        except Exception:  # noqa: BLE001
            dr = None
    if dr is not None:
        (hv, *_h), (vv, *_v) = dr["dims"]
        val, unit = dr["area_m2"], "m2"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: {hv} x {vv} mm = {val} m² (printed dimensions; "
                                                     f"traced {m['area_m2']} m² at 1:{n:g})"),
              *(Evidence(file=path, page=page, bbox=[round(t, 1) for t in d_[5]], bbox_space="pdf_pt", exact=True,
                         note=f"dimension {d_[0]} mm (printed; measures {d_[6]} mm at 1:{n:g})") for d_ in dr["dims"])]
        assumptions.append(f"area = the printed dimensions {hv} x {vv} mm that span this room (the traced outline "
                           f"gives {m['area_m2']} m²); printed dimensions win over the drawing measured at 1:{n:g}")
        missing = []
        conf = max(conf, 0.6)
    elif what == "area" and behind and not asked_out:
        # doors do not bound a room (Revit): the unlabelled closet behind its sliding doors is part of it (AFCEE A-101
        # Break Room: room 11.72 + closet 2.23 = 13.95 m² = 150.2 SF, schedule 153 SF)
        val, unit = round(m["area_m2"] + behind, 2), "m2"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: {m['area_m2']} m² traced at 1:{n:g} (approximate)"),
              *(Evidence(file=path, page=page, bbox=_poly_bbox(bp), bbox_space="pdf_pt", exact=False, polygon=bp,
                         note=f"{name[:50]}: + the unlabelled space behind its sliding door (approximate)")
                for bp in behind_polys)]
        assumptions.append(f"area = the room {m['area_m2']} m² + the unlabelled space behind its sliding door "
                           f"{behind:.2f} m² (doors do not bound a room); without it: {m['area_m2']} m²")
    elif what == "area":
        val, unit = m["area_m2"], "m2"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: {val} m² traced at 1:{n:g} (approximate)")]
        # a traced picture's outlined walls let the room run into its door recesses / a hollow wall's cavity
        # (D1 dfe26 PNG 'Entrance and Cloakroom' 11.84 vs 10.7 m²); in halls (>= 100 m²) the recesses are < 1 %
        # and below a scan trace's own noise (DfE heavy scan hall 242.91 -> 240.62 vs 246.37), so left alone
        cut = _recess_area(pdf, page, n, m)
        if cut:
            if True:
                assumptions.append(f"area = the traced region {val} m² minus {cut:.2f} m² of it inside door recesses / "
                                   f"narrow wall cavities the trace ran into (the picture draws walls as outlines): "
                                   f"{val - cut:.2f} m²")
                val = round(val - cut, 2)
    elif what == "perimeter":
        try:
            from shapely.geometry import Polygon

            val = round(Polygon(poly).length * n * L.PT_TO_M_AT_1, 2)
        except Exception:  # noqa: BLE001
            return None
        unit = "m"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: perimeter {val} m traced at 1:{n:g} (approximate)")]
        if RECESS_PERIM:  # the outline's door recesses / open leaves / slits are no part of the room's edge (D1 dfe26
            #               PNG 'Entrance and Cloakroom' 17.18 vs 13.24: it ran into the door recess and a wall cavity)
            try:
                sk = _skirting(pdf, page, n, m)
            except Exception:  # noqa: BLE001
                sk = None
            cut = float((sk or {}).get("recess_per_m") or 0.0)
            if 0 < cut < 0.4 * val:
                assumptions.append(f"perimeter = the traced outline {val} m minus {cut:.2f} m of it running into door "
                                   f"recesses / round open door leaves / into narrow slits ({sk.get('recess_n')} "
                                   f"part{'s' * (sk.get('recess_n') != 1)}): {val - cut:.2f} m")
                val = round(val - cut, 2)
    else:
        if len(sides) != 2:
            return None
        longest = what == "length"
        val, unit = round(sides[0] if longest else sides[1], 2), "m"
        line = _side_line(poly, longest)
        ev = [Evidence(**ev_kw, polygon=poly, polyline=line,
                       note=f"{name[:50]}: {'long' if longest else 'short'} side {val} m at 1:{n:g} (approximate)")]
        assumptions.append(f"{'length = the longer' if longest else 'width = the shorter'} side of the traced "
                           f"outline's smallest enclosing rectangle ({sides[0]:.2f} x {sides[1]:.2f} m)")
        conf += 0.05
    return Answer(value=val, unit=unit, confidence=round(conf, 3), evidence=ev, solver=SOLVER,
                  assumptions=assumptions, missing=missing,
                  trace=[f"lineplan: line-only sheet at printed 1:{n:g}; {how}; measure_room -> area {m['area_m2']} m², "
                         f"sides {sides}, gap {m['gap_m']} m, variant {m['variant']}, support {m['support']}/"
                         f"{m['n_readings']}",
                         *([f"lineplan: printed dimensions {dr['dims'][0][0]} x {dr['dims'][1][0]} mm span the traced "
                            f"room (each measures its own line: {dr['dims'][0][6]} / {dr['dims'][1][6]} mm) -> "
                            f"{dr['area_m2']} m²"] if dr is not None else [])])


def _printed_answer(path: str, page: int, n: float, name: str, how: str, printed: tuple, m: dict | None) -> Answer:
    """The area printed with the matched label wins over a traced outline that disagrees with it by > PRINTED_TAG_TOL
    (or that did not close): exact evidence on the printed text, the traced outline only named in the assumptions."""
    m2, text, b = printed
    traced = (f"the outline traced from the wall lines at 1:{n:g} gives {m['area_m2']} m² "
              f"({(m['area_m2'] / m2 - 1) * 100:+.0f} %: it probably closed round a pocket of lines or ran into a "
              "neighbour), so the printed number wins") if m is not None else \
        f"no outline closed around the label at 1:{n:g}"
    ev = [Evidence(file=path, page=page, bbox=[round(v, 1) for v in b], bbox_space="pdf_pt", exact=True,
                   note=f"{name[:50]}: printed area '{text.strip()[:40]}' = {round(m2, 2)} m²")]
    return Answer(value=round(m2, 2), unit="m2", confidence=0.6, evidence=ev, solver=SOLVER,
                  assumptions=[f"room = {how}", f"area = the area printed with that label ('{text.strip()[:40]}'); "
                               + traced],
                  missing=[],
                  trace=[f"lineplan: {how}; printed area with the label {round(m2, 2)} m² ('{text.strip()[:40]}'); "
                         + (f"traced {m['area_m2']} m², variant {m['variant']}" if m is not None else "no trace")])


def _scale_words(path: str, page: int, n: float) -> str:
    """'scale: 1:N printed on the sheet', or -- when the sheet prints 1:R for a bigger paper and this page is a
    reduced copy (L.sheet_scales applies the '@ A1' factor) -- 'scale: 1:N on this page (the sheet prints 1:R at its
    full paper size)' (F8 00:27: the card showed '1:400 printed' next to the recognised '1:200 (printed)')."""
    try:
        from stoa.data import abspath
        from stoa.evidence import linework as L
        from stoa.solvers.raster.textlayer import parse_scales

        raw = {float(sc["ratio"]) for t, _b in L.text_lines(str(abspath(path)), page) for sc in parse_scales(t)}
        raw = {r for r in raw if 10 <= r <= 2000}
        if raw and not any(abs(r - n) <= 0.01 * n for r in raw):
            r = min(raw, key=lambda r: abs(n / r - 1))
            return f"scale: 1:{n:g} on this page (the sheet prints 1:{r:g} at its full paper size)"
    except Exception:  # noqa: BLE001
        pass
    return f"scale: 1:{n:g} printed on the sheet"


POCKET_GUARD = bool(int(__import__("os").environ.get("E1_POCKET_GUARD", "1")))
_BIG_HALL = re.compile(r"\b(sports?|main|assembly|school|dining|multi[- ]?purpose|function|community|exhibition|drill|"
                       r"games|concert|lecture|banquet|great)\s+ha[l!|1i]{2}s?\b", re.I)


def _pocket(bbox: list, tag_box, name: str, q_subj: str, area_m2: float) -> str | None:
    """X1 00:00 / 00:27 (scans of EFA plans): the traced 'room' is a pocket round its own label -- court markings or
    glyph traces close a cell barely bigger than the words (superblock light scan 'Sport Ha!!' 6.2 m², the hall 594).
    Flagged (the caller drops the confidence to <= 0.1, so another reading wins or the card says 'not measured') when the
    outline's box is hardly wider than the label and not much taller, or a sports / main / assembly hall measures under
    30 m² -> the assumption line, else None."""
    if tag_box:
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        lw, lh = tag_box[2] - tag_box[0], tag_box[3] - tag_box[1]
        if bw > 0 and bh > 0 and lw >= 0.85 * bw and lh >= 0.35 * bh:
            return (f"the traced outline ({bw:.0f} x {bh:.0f} pt) is hardly bigger than the label '{name[:30]}' itself "
                    f"({lw:.0f} x {lh:.0f} pt): it is probably a pocket of lines round the label (floor markings, "
                    "furniture, scan noise), not the room")
    hall = _BIG_HALL.search(q_subj) or _BIG_HALL.search(name)
    if hall and area_m2 < 30:
        return (f"a {hall.group(0).lower()} of {area_m2:.1f} m² is implausibly small: the outline probably closes round "
                "lines inside the hall (court or floor markings, seating, furniture) near its label")
    return None


PRINTED_TAG = bool(int(os.environ.get("E1_PRINTED_TAG", "1")))
PRINTED_TAG_TOL = float(os.environ.get("E1_PRINTED_TAG_TOL", "0.2"))


def _printed_area(pdf: str, page: int, pick: dict, labs: list | None = None) -> tuple | None:
    """X1 02:24 (real-world sealed: a German area question on a US sheet with printed SF tags; the cross-language label
    match reached lineplan, which traced a 4.1 m² pocket @0.77 while the room's tag prints its area): the area printed
    WITH the matched label -- on the label's own line(s) ('KITCHEN 120 SF') or on a line just below / above it, centred
    on it (a room-number line may sit between), in the text's own reading frame (sheets plotted with /Rotate 90 draw
    every label upwards) -> (m², printed text, bbox pdf_pt), else None. Two candidates at the same distance on both
    sides (a neighbour's tag above, its own below) -> the one below (name, then area); a tag that fits another
    label better (below it) is that room's. Areas under 1 m² and '1/2 SF'
    fractions are not room tags."""
    from stoa.evidence import linework as L

    def tag_m2(text):
        for m in L._AREA_TAG.finditer(text):
            if m.start() > 0 and text[m.start() - 1] == "/":
                continue
            try:
                v = L._num(m.group(1))
            except ValueError:
                continue
            m2 = v * 0.09290304 if m.group(2).lower().startswith(("sf", "sq", "ft")) else v
            if m2 >= 1.0:
                return m2
        return None

    own = tag_m2(pick["text"])
    if own:
        return round(own, 3), pick["text"], tuple(pick["bbox"])

    def key(lab, b, f):
        """(side: 0 below / same line, 1 above; gap in line heights) of tag box b for label lab in frame f, or None:
        a tag stack is name / room number / area (0920ea2a4e: 'Investigations' / '7.05' / '120 SF' sits 0.9 line
        above 'Corridor', whose own '61 SF' is 1.2 lines below it), so below wins before distance"""
        h = float(lab.get("line_h") or 1.0)
        X0, Y0, X1, Y1 = _to_frame(lab["bbox"], f)
        bx0, by0, bx1, by1 = _to_frame(b, f)
        if (by1 - by0) > 2.5 * h or (bx1 - bx0) > 6 * max(X1 - X0, h):
            return None  # a table cell / note block, not a room tag line
        centred = abs((bx0 + bx1) / 2 - (X0 + X1) / 2) <= 0.5 * max(X1 - X0, bx1 - bx0) + h
        below, above = by0 - Y1, Y0 - by1
        if centred and -0.3 * h <= below <= 1.6 * h:
            return 0, round(max(below, 0.0) / h, 1)
        if centred and -0.3 * h <= above <= 1.6 * h:
            return 1, round(max(above, 0.0) / h, 1)
        if abs((by0 + by1) / 2 - (Y0 + Y1) / 2) <= 0.5 * h and -0.3 * h <= bx0 - X1 <= 2.0 * h:
            return 0, round(max(bx0 - X1, 0.0) / h, 1)  # the same line, a separately placed span to the right
        return None

    cands = []
    for text, b, d in L.text_lines_dir(pdf, page):
        m2 = tag_m2(text) if len(text) <= 40 else None
        if not m2:
            continue
        f = _frame(d)
        k = key(pick, b, f)
        if k is None:
            continue
        # the tag belongs to the label it fits best: one printed below another room's name is that room's
        if any(lab is not pick and (k2 := key(lab, b, f)) is not None and k2 < k for lab in labs or ()):
            continue
        cands.append((k, m2, text, b))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    _k, m2, text, b = cands[0]
    return round(m2, 3), text, tuple(b)


def add_traced_outlines(ans: Answer, max_items: int = 6) -> Answer:
    """Captain 14:39: a PRINTED room area (area_tags' answer on a lone vector PDF: 'Zimmer 14.20 m²', 'U116 - 642 SF')
    stays the value; the room's outline traced from the wall lines around its tag is added as evidence when its area
    at the printed scale agrees with the printed one (linework.outline_for_area, +-6 %): an extra approximate item
    (exact=False, the tag's own box stays exact) and a 'cross-check: … ✓' trace line. Never changes value/confidence."""
    try:
        return _add_traced_outlines(ans, max_items)
    except Exception:  # noqa: BLE001
        return ans


def _add_traced_outlines(ans: Answer, max_items: int) -> Answer:
    from stoa.data import abspath
    from stoa.evidence import linework as L

    if ans.value is None or (ans.unit or "").lower() not in ("m2", "m²", "ft2") or \
            (ans.solver or "").split("/")[-1] != "area_tags":
        return ans
    evs = [e for e in ans.evidence if e.bbox and e.bbox_space == "pdf_pt" and e.polygon is None
           and str(e.file).lower().endswith(".pdf")]
    if not evs or len(evs) > max_items or len({(str(e.file), e.page or 0) for e in evs}) != 1:
        return ans
    pdf, page = str(abspath(evs[0].file)), evs[0].page or 0
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return ans
    n = scales[0]
    d, _ = _door_check(pdf, page, n)
    if d is not None and not 0.6 <= d <= 1.3:
        return ans
    added, checks = [], []
    for e in evs:
        m = L._AREA_TAG.search(e.note.split(":", 1)[-1])
        if not m:
            continue
        v = L._num(m.group(1))
        a_m2 = v * 0.09290304 if re.match(r"s|f", m.group(2), re.I) else v
        x0, y0, x1, y1 = e.bbox
        r = L.outline_for_area(pdf, page, ((x0 + x1) / 2, (y0 + y1) / 2), a_m2, n, tag_box=tuple(e.bbox))
        if not r:
            continue
        name = e.note.split(":", 1)[0][:40]
        added.append(Evidence(file=e.file, page=page, bbox=_poly_bbox(r["polygon"]), bbox_space="pdf_pt", exact=False,
                              polygon=r["polygon"], note=f"{name}: outline traced from the wall lines {r['area_m2']} m² "
                                                         f"≈ printed {round(a_m2, 2)} m² ✓ (approximate outline)"))
        checks.append(f"{name} {r['area_m2']} ≈ {round(a_m2, 2)}")
    if added:
        ans.evidence = [*ans.evidence, *added]
        ans.trace = [*ans.trace, f"cross-check: {len(added)} of {len(evs)} printed areas match the room outline traced "
                                 f"from the wall lines at 1:{n:g} (±{L.TOL:.0%}): {'; '.join(checks)} ✓"]
    return ans


__all__ = ["add_traced_outlines", "answer", "labels", "rooms"]
