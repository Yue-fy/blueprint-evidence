"""Does the cited evidence support the asked quantity? (T1; independent audit 19:51, risk #1 / fix #2)

Live failure (audit, deployed d23): Stoa's S-100F PNG, 'How many square metres of the 150 mm slab with D13 bars at
300 mm centres are shown?' -> 10000 m² @0.75 citing 'TOP OF SLAB EL. = 10000'. The text reader checked only that the
digits are printed in the cited line, never WHAT the printed number is. This module reads the number's role from its
own line:

    role_of(line, start, end)       'level' | 'height' | 'thickness' | 'spacing' | 'bar' | 'scale' | 'identifier' |
                                    'date' | 'area' | 'volume' | 'count' | 'dimension' | 'length' | 'plain'
    check_citation(q, value, texts) None (not applicable: no literal occurrence, not a guarded question kind) or
                                    Verdict(ok, role, line, why); ok=False when EVERY occurrence of the value in the
                                    cited lines has a role that cannot be the asked kind (an elevation is never an
                                    area or a count; a length unit never an area; an area unit never a length)
    citation_guard(q, ans, texts)   readers' hook: a rejected citation -> confidence <= REJECT_CONF + the reason in
                                    `missing`, so the pipeline's cascade falls through to measurement
    final_guard(q, ans)             Pipeline.solve's safety net: the final answer still rests on a rejected citation
                                    -> no number (value None), the rejected lines named, the user told how to measure
    mark_support(q, ans)            the answer's evidence state (contract proposal D49 'support'):
                                    supported | approximate | unlocated, + why, shown / of for counts

A bare number ('14.5' under a room code, '12' doors) is 'plain' and supports anything: the guard only rejects what
the line itself says the number is. Units are read right after the number, roles from the words right before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from stoa.contract import Answer

REJECT_CONF = 0.05
TAG = "support check"  # trace / missing prefix (final_guard looks for it)

# ---------------------------------------------------------------------------------------------------------------------
# the role of one number in its printed line
# ---------------------------------------------------------------------------------------------------------------------
_NUM = re.compile(r"(?<![\d.,])(\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)(?![\d])")

_LEVEL_PRE = re.compile(
    r"(?:\bEL|\bELEV(?:ATION)?|\bLEVEL|\bLVL|\bT\.?O\.?[SCWFB]\b\.?|\bTOS|\bTOC|\bTOW|\bTOF|\bB\.?O\.?[SFC]\b\.?|"
    r"\bINV(?:ERT)?|\bF\.?F\.?[LE]\b\.?|\bS\.?S\.?L\b\.?|\bRL|\bDATUM|\bTOP\s+OF\s+[A-Z][A-Z .\-/]{0,30}|"
    r"\bBOTTOM\s+OF\s+[A-Z][A-Z .\-/]{0,30}|\bH[ÖO]HE|\bKOTE|\bKORKO|\bNIVEAU|\bNIV[ÅA]|\bQUOTA|\bCOTE|\b[OU]\.?K\.?(?:F\.?F\.?|R\.?F\.?)?)"
    r"[\s.:=]*[+\-±]?\s*$")
_SIGNED = re.compile(r"[+±]\s?$")
_HEIGHT_PRE = re.compile(r"(?:\bHEIGHT|\bHT|\bHGT|\bCLG\.?(?:\s*HT\.?)?|\bCEILING(?:\s+HEIGHT)?|\bHEADROOM|"
                         r"\bSILL(?:\s+HEIGHT)?|\bHEAD(?:\s+HEIGHT)?|\bH\s*=|\bKORKEUS|\bH[ÖO]JD|\bHAUTEUR|\bALTEZZA|"
                         r"\bRAUMH[ÖO]HE|\bLICHTE\s+H[ÖO]HE)[\s.:=]*$")
_HEIGHT_POST = re.compile(r"^\s*(?:MM|CM|M)?\.?\s*(?:HIGH\b|HT\b|H\b|TALL\b|A\.?F\.?F\.?\b|ABOVE\b)")
_THICK_PRE = re.compile(r"(?:\bTHK|\bTHICK(?:NESS)?|\bT\s*=|\bPAKSUUS|\bDICKE|\bST[ÄA]RKE|\b[ÉE]PAISSEUR|\bSPESSORE|"
                        r"\bTJOCKLEK)[\s.:=]*$")
_THICK_POST = re.compile(
    r"^\s*(?:MM)?\.?\s*(?:THK\b|THICK\b|SLAB\b|SLAB-ON-GRADE|S\.?O\.?G\.?\b|WALL\b|TOPPING\b|SCREED\b|BLOCKWORK\b|"
    r"BLOCK\b|CONC(?:RETE)?\b|INSULATION\b|PLINTH\b|DECK\b|FOOTING\b|PARTITION\b|MASONRY\b|BRICK\b|CMU\b|"
    r"PLASTERBOARD\b|GYPSUM\b|PAD\b|RAFT\b|BASE\b|SUB-?BASE\b)")
_SPACING_PRE = re.compile(r"(?:@|\bAT|\bC/C|\bCTRS?\.?|\bSPACING|\bS\s*=|\bK/K|\bJAKO|\bE/E)\s*$")
_SPACING_POST = re.compile(r"^\s*(?:MM)?\.?\s*(?:C/C\b|CTRS\b|CRS\b|CENTRES\b|CENTERS\b|O\.?C\.?(?![A-Z])|SPACING\b|"
                           r"K/K\b|EACH\s+WAY\b|E\.?W\.?(?![A-Z]))")
_BAR_PRE = re.compile(r"(?:Ø|⌀|\bDIA\.?|\bDIAMETER|\bNPS|\bDN)\s*$")
_BAR_POST = re.compile(r"^\s*(?:MM|\"|”|″|IN\.?)?\s*(?:Ø|⌀|DIA\b|DIAMETER\b|PIPE\b|BARS?\b|HEADER\b|MAIN\b|LINE\b|"
                       r"SUCTION\b|DISCHARGE\b|SUPPLY\b|RETURN\b)")
# document identifiers (never a quantity) vs item indexes ('TYPE 2', 'ROOM NO. 12': weak for a count, never a size)
_IDENT_PRE = re.compile(r"(?:\bSHEET|\bSHT|\bDWG|\bDRAWING|\bFILE|\bPROJECT|\bPROJ|\bJOB|\bCONTRACT|\bREV(?:ISION)?|"
                        r"\bSOLICITATION|\bISSUE|\bDOC(?:UMENT)?|\bPLOT|\bSERIAL|\bSPEC(?:IFICATION)?|\bCODE|"
                        r"\bPIIRUSTUS|\bBLATT|\bPLAN\s+NO)[\s.:#=\-]*(?:NO\.?|NUMBER|NR\.?|#)?[\s.:#=\-]*$")
_INDEX_PRE = re.compile(r"(?:\bNUMBER|\bNO|\bNR|\bNUM|\bREF|\bDETAIL|\bSECTION|\bGRID|\bTYPE|\bMARK|\bID|\bITEM|#)"
                        r"[\s.:#=\-]*$")
_IDENT_DASH = re.compile(r"(?:^|[^A-Za-z])[A-Za-z]{1,3}[-.]$")  # 'S-100F', 'A-101', 'B.104'
_SCALE_PRE = re.compile(r"(?:\b1\s*:\s*$|\bSCALE\b[^0-9]{0,12}$|\bMASSSTAB\b[^0-9]{0,6}$|\bMITTAKAAVA\b[^0-9]{0,6}$)")
_SCALE_POST = re.compile(r"^\s*:\s*\d")
_DATE = re.compile(r"\b\d{1,2}([./\-])\d{1,2}\1(?:\d{4}|\d{2})\b(?![.,/]\d)|\b\d{4}-\d{2}-\d{2}\b|"
                   r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*\.?\s+\d{1,2},?\s+\d{4}\b|"
                   r"\b\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*\.?\s+\d{2,4}\b")
_DATE_PRE = re.compile(r"\bDATE[D]?\b[^0-9]{0,6}$|\bPVM\b[^0-9]{0,4}$|\bDATUM\b[^0-9]{0,4}$")
_AREA_POST = re.compile(r"^\s*(?:M²|M2\b|M\s2\b|M\^2|SQ\.?\s?M\b|SQM\b|S\.?F\.?(?![A-Z])|SQ\.?\s?F(?:T|EET)\b|FT²|FT2\b|"
                        r"SQUARE\b|NET\b|GROSS\b|NIA\b|GIA\b|GFA\b|HA\b|ACRES?\b)")
_AREA_PRE = re.compile(r"(?:\bAREA|\bGFA|\bGIA|\bNIA|\bNFA|\bPINTA-?ALA|\bFL[ÄA]CHE|\bSURFACE|\bSUPERFICIE|\bYTA|"
                       r"\bAREAL|\bSQ\.?\s?FT\.?|\bSQUARE\s+F(?:EE|OO)T(?:AGE)?)\b[^0-9]{0,12}$")
_VOL_POST = re.compile(r"^\s*(?:M³|M3\b|CU\.?\s?(?:M|FT|YD)\b|FT³|LITRES?\b|LITERS?\b|GAL(?:LONS?)?\b)")
_COUNT_POST = re.compile(r"^\s*(?:NO\b\.?|NOS\b\.?|PCS\b\.?|EA\b\.?|EACH\b|UNITS?\b|TIMES\b|KPL\b|STK\b)")
_COUNT_PRE = re.compile(r"(?:\bQTY|\bQUANTITY|\bCOUNT|\bTOTAL\s+(?:NO\.?|NUMBER)|\bNO\.?\s+OF\s+[A-Z]+|\bNUMBER\s+OF\s+[A-Z]+|"
                        r"\bKPL|\bANZAHL|\bLKM|\bANTAL)[\s.:=]*$")
_DIM_NEAR = re.compile(r"^\s*[x×X]\s*\d|^\s*(?:MM|M)?\s*[x×]\s*\d")
_DIM_PRE = re.compile(r"\d\s*(?:MM|M|')?\s*[x×X]\s*$")
_LEN_POST = re.compile(r"^\s*(?:MM\b|CM\b|DM\b|M\b(?![²2³3^])|KM\b|FT\b\.?|FEET\b|FOOT\b|IN\b\.?|INCH(?:ES)?\b|'|\"|’|”|′|″|"
                       r"METRES?\b|METERS?\b|LF\b|L\.F\.)")


def role_of(line: str, start: int, end: int) -> str:
    """What the number printed at line[start:end] is, read from the words around it (upper-cased)."""
    pre, post = line[:start].upper(), line[end:].upper()
    tok = line[start:end]
    decimal = bool(re.search(r"[.,]\d{1,2}$", tok)) and not re.search(r"[, ]\d{3}$", tok)
    if re.search(r"[A-Z]$", pre) and not decimal:  # 'D13', 'T16', 'A101', 'EL10000'
        if re.search(r"(?:^|[^A-Z])(?:EL|ELEV|TOS|FFL|SSL|RL)$", pre):
            return "level"
        if re.search(r"(?:^|[^A-Z])[DTRYHØ]$", pre) and _f(tok) is not None and _f(tok) <= 60:
            return "bar"
        return "index"  # 'MH2', 'W3', 'A101': an item / type / room mark (weak for a count, never a size)
    if _SCALE_PRE.search(pre) or _SCALE_POST.search(post):
        return "scale"
    for m in _DATE.finditer(line.upper()):
        if m.start() <= start and end <= m.end():
            return "date"
    if _DATE_PRE.search(pre):
        return "date"
    if _LEVEL_PRE.search(pre):
        return "level"
    if _SIGNED.search(pre) and re.search(r"[.,]\d{2,3}$", tok):  # '+3.200', '±0.000'
        return "level"
    if _HEIGHT_PRE.search(pre) or _HEIGHT_POST.search(post):
        return "height"
    if _SPACING_PRE.search(pre) or _SPACING_POST.search(post):
        return "spacing"
    if _BAR_PRE.search(pre) or (_BAR_POST.search(post) and re.match(r'^\s*(?:"|”|″|IN\b|MM\s*(?:Ø|⌀|DIA))', post)):
        return "bar"
    v = _f(tok)
    if (_THICK_PRE.search(pre) or (_THICK_POST.search(post) and v is not None and float(v).is_integer()
                                   and 10 <= v <= 1500)):
        return "thickness"
    if _IDENT_PRE.search(pre):
        return "identifier"
    if _INDEX_PRE.search(pre) or (_IDENT_DASH.search(pre) and not decimal):
        return "index"  # 'FSR-2', 'B-104', '#5', 'TYPE 2': the highest tag index may BE a count of distinct tags
    if _AREA_POST.search(post) or _AREA_PRE.search(pre):
        return "area"
    if _VOL_POST.search(post):
        return "volume"
    if _COUNT_POST.search(post) or _COUNT_PRE.search(pre):
        return "count"
    if _DIM_NEAR.search(post) or _DIM_PRE.search(pre):
        return "dimension"
    if re.fullmatch(r"\s*M\s*", post):  # '31.0M' at the end of an OCR line: the superscript '²' is often lost
        return "plain"
    if _LEN_POST.search(post):
        return "length"
    return "plain"


def _f(tok: str) -> float | None:
    try:
        return float(tok.replace(",", ".")) if re.fullmatch(r"\d+(?:[.,]\d+)?", tok) else None
    except ValueError:
        return None


def _readings(tok: str) -> set[float]:
    """'10,000' -> {10000}; '14,5' -> {14.5}; '1,250' -> {1250, 1.25} (thousands or decimal comma)."""
    out: set[float] = set()
    t = tok.replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:[, ]\d{3})+(?:\.\d+)?", t):
        out.add(float(re.sub(r"[, ]", "", t)))
        if re.fullmatch(r"\d{1,3},\d{3}", t):
            out.add(float(t.replace(",", ".")))
    else:
        try:
            out.add(float(t.replace(",", ".")))
        except ValueError:
            pass
    return out


def occurrences(value: float, line: str) -> list[tuple[int, int]]:
    """Spans of `line` where `value` is printed (decimal comma / point, thousands separators)."""
    spans = []
    for m in _NUM.finditer(line):
        if any(abs(r - value) <= 1e-9 * max(1.0, abs(value)) for r in _readings(m.group(1))):
            spans.append((m.start(1), m.end(1)))
    return spans


# ---------------------------------------------------------------------------------------------------------------------
# which roles can support which asked kind
# ---------------------------------------------------------------------------------------------------------------------
_NOT_A_QUANTITY = {"level", "height", "thickness", "spacing", "bar", "scale", "identifier", "date"}
NEVER = {
    "area": _NOT_A_QUANTITY | {"index", "length", "dimension", "volume", "count"},
    "count": _NOT_A_QUANTITY | {"length", "dimension", "area", "volume"},  # an index ('TYPE 2') is weak, not wrong
    "length": {"scale", "identifier", "index", "date", "area", "volume", "count"},
    "volume": _NOT_A_QUANTITY | {"index", "length", "dimension", "area", "count"},
}
# a length question may ask for exactly what the label is
_ASKS = {
    "level": re.compile(r"\b(elevation|level|height|high|tall|deep|depth|datum|top of|bottom of|invert|korko|höhe|"
                        r"niveau|quota)\b", re.I),
    "height": re.compile(r"\b(height|high|tall|headroom|ceiling|clearance|korkeus|höhe|hauteur|altezza|höjd)\b", re.I),
    "thickness": re.compile(r"\b(thick|thickness|depth|deep|paksu\w*|dicke|stärke|épaisseur|spessore|tjock\w*)\b", re.I),
    "spacing": re.compile(r"\b(spacing|spaced|centres|centers|c/c|o\.?c\.?|pitch|interval|jako|abstand)\b|@", re.I),
    "bar": re.compile(r"\b(diameter|dia|size|sized|bar|rebar|pipe|piping|header|line|main|nps|dn|halkaisija|koko|"
                      r"durchmesser|grösse|größe|dimension)\b|Ø|⌀", re.I),
}
_ROLE_WORDS = {
    "level": "an elevation / level mark", "height": "a height", "thickness": "a thickness",
    "spacing": "a bar / member spacing", "bar": "a bar or pipe size", "scale": "the drawing scale",
    "identifier": "a sheet / file / drawing number", "index": "an item number or type mark", "date": "a date", "area": "an area", "volume": "a volume",
    "count": "a quantity of items", "dimension": "a dimension (length)", "length": "a length",
}


_UNIT_KIND = {**{u: "area" for u in ("m2", "m²", "sqm", "ft2", "ft²", "sf", "sq ft", "sqft", "square feet", "ha")},
              **{u: "length" for u in ("m", "mm", "cm", "km", "ft", "feet", "in", "inch", "inches", '"', "'")},
              **{u: "volume" for u in ("m3", "m³", "ft3", "l")}, "count": "count"}


def asked_kind(question: str, unit: str | None = None) -> str | None:
    """'area' | 'count' | 'length' | 'volume' or None (text / yes-no / other questions are not guarded). The answer's
    own unit decides when it has one ('What size is the suction header?' parses as an area; 16 in is a length)."""
    u = str(unit or "").strip().lower().rstrip(".")
    if u in _UNIT_KIND:
        return _UNIT_KIND[u]
    try:
        from stoa.router.intent import parse

        t = parse(question).task
    except Exception:  # noqa: BLE001
        return None
    return t if t in NEVER else None


@dataclass
class Verdict:
    ok: bool
    role: str
    line: str
    why: str = ""


def check_citation(question: str, value, texts: list[str], kind: str | None = None, unit: str | None = None) -> Verdict | None:
    """None = nothing to say (the value is not a number / not printed literally in the cited lines / question kind
    not guarded); Verdict(ok=False) = every literal occurrence of the value has a role the asked kind can never be."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    kind = kind or asked_kind(question, unit)
    if kind is None:
        return None
    never = set(NEVER[kind])
    if kind == "length":  # 'How thick is the slab?' may cite '150 THK'; 'How long is the wall?' may not
        never |= {r for r, rx in _ASKS.items() if not rx.search(question)}
    seen: list[tuple[str, str]] = []
    for line in texts:
        line = str(line or "")
        for s, e in occurrences(float(value), line):
            r = role_of(line, s, e)
            if r not in never:
                return Verdict(True, r, line)
            seen.append((r, line))
    if not seen:
        return None
    r, line = seen[0]
    v = f"{value:g}" if isinstance(value, float) else str(value)
    why = (f"{TAG}: {v} is printed as {_ROLE_WORDS.get(r, r)} ('{line.strip()[:70]}'), not as {_KIND_WORDS[kind]}: "
           "this text does not give the asked quantity")
    return Verdict(False, r, line, why)


_KIND_WORDS = {"area": "an area", "count": "a number of items", "length": "this length", "volume": "a volume"}
_KIND_NOUN = {"area": "area", "count": "count", "length": "length", "volume": "volume"}


def citation_guard(question: str, ans: Answer, texts: list[str] | None = None) -> Answer:
    """Readers' hook: cap a reading whose cited number cannot be the asked quantity (cascade falls through)."""
    try:
        texts = cited_texts(ans) if texts is None else texts
        v = check_citation(question, ans.value, texts, unit=ans.unit)
    except Exception:  # noqa: BLE001 — a guard never breaks an answer
        return ans
    if v is None or v.ok:
        return ans
    if any(str(m).startswith(TAG) for m in ans.missing):
        return ans
    ans.confidence = round(min(float(ans.confidence or 0), REJECT_CONF), 3)
    ans.missing = [*ans.missing, v.why]
    ans.trace = [*ans.trace, v.why + f" → confidence ≤ {REJECT_CONF}"]
    return ans


# evidence notes that quote the printed line a reader cited (textqa / pdf_textqa: "text '…'", E1: "text: …")
_CITED_NOTE = re.compile(r"^\s*text(?::\s*|\s+')(?P<t>.*?)'?\s*$", re.S)


def cited_texts(ans: Answer) -> list[str]:
    out = []
    for e in ans.evidence or []:
        m = _CITED_NOTE.match(str(e.note or ""))
        if m and (e.bbox or e.polygon):
            out.append(m.group("t"))
    return out


def final_guard(question: str, ans: Answer) -> Answer:
    """Pipeline.solve's last word on a rejected citation: no number, the reason, how to get one (measure)."""
    try:
        v = check_citation(question, ans.value, cited_texts(ans), unit=ans.unit)
    except Exception:  # noqa: BLE001
        return ans
    if v is None or v.ok:
        return ans
    kind = asked_kind(question, ans.unit) or "quantity"
    out = ans.model_copy(deep=True)
    shown = f"{ans.value:g}" if isinstance(ans.value, float) else str(ans.value)
    out.trace = [*out.trace, v.why, f"{TAG}: no number given ({shown} {ans.unit or ''} rejected); the cited text is "
                 "kept in 'missing', not as evidence"]
    # W5 20:29: a no-number card shows missing[0] as its sentence -> the plain sentence first, the technical reason next
    out.missing = [f"no printed {_KIND_NOUN.get(kind, kind)} was found for this; measure it on the drawing (set the "
                   "scale with two clicks on a known length, then click the element) or name the room / sheet more "
                   "exactly", v.why] + [m for m in out.missing if not str(m).startswith(TAG)]
    out.value, out.confidence = None, 0.0
    out.evidence = [e for e in out.evidence if not _CITED_NOTE.match(str(e.note or ""))]
    return out


# ---------------------------------------------------------------------------------------------------------------------
# the answer's evidence state (contract proposal D49: Answer.support)
# ---------------------------------------------------------------------------------------------------------------------
_AUX_NOTE = re.compile(r"scale bar|graphic scale|sheet title|searched area|legend|north arrow|title block|"
                       r"scale statement|^scale\b|printed scale|key plan", re.I)
_STRUCT_SOLVER = re.compile(r"(?:^|/)(?:ifc|dxf|svg_ifc|schedule|plan_rooms|ifcplan_pdf|plan_svg|pdf_vector|pdf_text|"
                            r"label_count|stated_count|area_tags|area_table|areatable|room_size|line_rooms|lengths|"
                            r"slab_area|piping\w*|lineplan)$", re.I)


# E1 textground: a box found by searching the sheet for the value AFTER the answer was made (audit architecture notes:
# 'presence of a box is not proof that the answer was derived from it') -> never 'supported' on its own
_POST_HOC = re.compile(r"text search on the sheet", re.I)


def _located(e) -> bool:
    return bool(e.bbox or e.ifc_guid or e.polygon or e.polyline)


def _conversions(v: float) -> list[float]:
    return [v, v / 0.09290304, v * 0.09290304, v / 0.3048, v * 0.3048, v * 1000, v / 1000, v * 100, v / 100]


# a note that DESCRIBES OUR OWN MEASUREMENT ('Laundry: 7.03 m² traced at 1:200 (approximate)', plan_rooms on Stoa's DfE
# sheet) prints the value because we wrote it there: it is not a printed value of the drawing (T1 c2: both DfE flagship
# areas were 'supported — the printed value is marked' while their only evidence was a traced outline, exact=False)
_MEASURED_NOTE = re.compile(r"\b(?:traced|measured|estimated|computed|derived|calculated|shoelace|flood[- ]?fill|"
                            r"from (?:the |its )?(?:outline|walls?|wall lines|geometry|polygon|footprint)|"
                            r"outline area|polygon area|at (?:the )?(?:printed |scale )?1\s*:\s*\d+)\b", re.I)


def _own_measurement(e) -> bool:
    return bool(e.polygon or e.polyline) or bool(_MEASURED_NOTE.search(str(e.note or "")))


def _printed_support(question: str, ans: Answer) -> str | None:
    """A cited line prints the value (or its unit conversion) in a role the asked kind can be -> that line. Notes of
    our own measurements (outlines, 'traced at 1:200') are not printed text."""
    if isinstance(ans.value, bool) or not isinstance(ans.value, (int, float)):
        return None
    kind = asked_kind(question, ans.unit)
    texts = cited_texts(ans) + [str(e.note) for e in ans.evidence if _located(e) and not _POST_HOC.search(str(e.note))
                                and not _own_measurement(e) and not _CITED_NOTE.match(str(e.note or ""))]
    for cand in _conversions(float(ans.value)):
        for line in texts:
            for s, e in occurrences(round(cand, 6), line) or occurrences(round(cand, 2), line):
                r = role_of(line, s, e)
                if kind is None or r not in NEVER[kind]:
                    return line
    return None


def _derived(v: float, texts: list[str], kind: str | None) -> str | None:
    """'the product of the marked 6200 × 9950' / 'the sum of the marked values' when `v` is one, else None."""
    never = NEVER.get(kind or "", set()) - {"length", "dimension"}  # sides of an area are lengths
    nums: list[float] = []
    for line in texts:
        for m in _NUM.finditer(line):
            if role_of(line, m.start(1), m.end(1)) not in never:
                nums.extend(_readings(m.group(1)))
    nums = [x for x in nums if x > 0][:40]

    def near(x: float) -> bool:
        return abs(x - v) <= 0.01 * max(abs(v), 1e-9)

    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            for k in (1.0, 1e-6, 1e-4, 0.09290304, 1 / 144):
                if near(a * b * k):
                    return f"computed: the product of the marked {a:g} × {b:g} (a rectangle)"
            if near(a + b):
                return f"computed: the sum of the marked {a:g} + {b:g}"
    if len(nums) >= 3 and near(sum(nums)):
        return "computed: the sum of the marked values"
    return None


def _near_digits(value, texts: list[str]) -> str | None:
    """S-100F PNG (T1 c1): the image says 10000, our text layer read 'TOP OF SLAB EL. = 1000' -> one digit apart."""
    want = re.sub(r"\.0+$", "", f"{float(value):.6f}".rstrip("0")).replace(".", "")
    if len(want) < 3:
        return None
    for line in texts:
        for m in _NUM.finditer(line):
            got = re.sub(r"[^\d]", "", m.group(1))
            if got != want and len(got) >= 2 and _edit1(got, want):
                return f"the marked text reads '{line.strip()[:50]}' ({m.group(1)}, one digit from {value:g}): check it"
    return None


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


_SHAPE_MISSING = "the answer does not have the shape the question asks for"
# words that name a KIND of space, not which one ('dining hall' vs 'sports hall': the qualifier decides)
_GENERIC = {"room", "rooms", "hall", "space", "area", "areas", "floor", "the", "of", "a", "an", "and", "total", "net",
            "gross", "internal", "usable", "floor", "plan", "level", "storey", "ground", "first", "second", "third",
            "m2", "m²", "sf", "sq", "ft", "ft2", "square", "feet", "metres", "meters", "size", "big", "large", "whole",
            "this", "that", "on", "in", "for", "at", "is", "what", "how"}
_ASKED_ROOM = re.compile(r"\b(?:area|size|footprint|space)\s+of\s+(?:the\s+)?(?P<a>[a-z][a-z' -]{1,40}?)\s*(?:\?|$|,|\bon\b|"
                         r"\bin\b|\(|\bincluding\b|\bexcluding\b)|\bhow big is\s+(?:the\s+)?(?P<b>[a-z][a-z' -]{1,40}?)"
                         r"\s*(?:\?|$|,|\bon\b|\bin\b|\()", re.I)
_LABEL = re.compile(r"^\s*(?:text:?\s*)?'?(?P<l>[A-Za-z][A-Za-z.' -]{1,40}?)\s*[:=–-]\s*\d")


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-zà-ÿ]+", s.lower()) if w not in _GENERIC and len(w) > 1]


def other_room(question: str, line: str) -> str | None:
    """'the Sports Hall' when the cited line's own label names a different room than the question ('floor area of
    the dining hall' cited 'Sports Hall: 594 m2', X1 stoa_like_sealed); None when they agree or either is unclear.
    Abbreviations count as agreeing: initials ('MPH' = multi-purpose hall) and shared 4-letter prefixes ('Classrm')."""
    m = _ASKED_ROOM.search(question or "")
    lab = _LABEL.match(line or "")
    if not m or not lab:
        return None
    asked, label = _words(m.group("a") or m.group("b") or ""), _words(lab.group("l"))
    if not asked or not label:
        return None
    initials = "".join(w[0] for w in re.findall(r"[a-z]+", (m.group("a") or m.group("b") or "").lower()))

    def agrees(w: str, pool: list[str]) -> bool:
        return any(w == p or (len(w) >= 4 and len(p) >= 4 and (w.startswith(p[:4]) or p.startswith(w[:4])))
                   for p in pool)

    anywhere = _words(question)  # 'living room (vardagsrum)', 'total dormitory area of the west wing'
    if any(agrees(w, asked + anywhere) for w in label) or \
            any(w == initials or initials.startswith(w) for w in label if len(w) >= 2):
        return None
    if all(not agrees(w, label) for w in asked):
        return "the " + lab.group("l").strip()
    return None


def support_state(question: str, ans: Answer) -> dict | None:
    """{'state': 'supported' | 'approximate' | 'unlocated', 'why': str, 'shown': int | None, 'of': int | None}"""
    if ans.value is None:
        return None
    if any(str(m).startswith(TAG) for m in ans.missing):
        why = next(str(m) for m in ans.missing if str(m).startswith(TAG))
        return {"state": "unlocated", "why": why[len(TAG) + 2:], "shown": None, "of": None}
    shape = next((str(m) for m in ans.missing if str(m).startswith(_SHAPE_MISSING)), None)
    if shape:  # the pipeline's shape check: an area for 'How long …' (wild_sealed: 196 ft2 cited '(196 SF)')
        return {"state": "unlocated", "why": "the marked value is not what the question asks for" +
                shape[len(_SHAPE_MISSING):].rstrip(), "shown": None, "of": None}
    loc = [e for e in ans.evidence or [] if _located(e)]
    if not loc:
        return {"state": "unlocated", "why": "nothing on the drawing is marked for this answer", "shown": None,
                "of": None}
    kind = asked_kind(question, ans.unit)
    if kind == "count" and isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool) and \
            float(ans.value).is_integer():
        n = int(ans.value)
        marks = [e for e in loc if not _AUX_NOTE.search(str(e.note or ""))]
        printed = _printed_support(question, ans)
        if n == 0:
            return {"state": "approximate", "why": "none found: the marks show where we looked, not proof of absence",
                    "shown": 0, "of": 0}
        if len(marks) >= n:
            return {"state": "supported", "why": f"{n} marks, one per counted item", "shown": len(marks), "of": n}
        if printed is not None and len(marks) <= 3:
            return {"state": "supported", "why": f"the printed total '{printed.strip()[:60]}' is marked",
                    "shown": len(marks), "of": n}
        return {"state": "approximate", "why": f"showing {len(marks)} of {n} counted items: the rest are not marked",
                "shown": len(marks), "of": n}
    printed = _printed_support(question, ans)
    if printed is not None:
        other = other_room(question, printed)
        if other:
            return {"state": "unlocated", "why": f"the marked label '{printed.strip()[:50]}' names {other}, not the "
                    "room the question asks for", "shown": None, "of": None}
        return {"state": "supported", "why": f"the printed value is marked: '{printed.strip()[:60]}'", "shown": None,
                "of": None}
    if all(_POST_HOC.search(str(e.note or "")) for e in loc):
        return {"state": "approximate", "why": "the number is printed at the marked spot (found by a search after "
                "the answer was read): check it is the line the answer means", "shown": None, "of": None}
    texts = cited_texts(ans)
    if texts and len(texts) == len(loc) and isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool):
        # every mark is a cited text line and none prints the number: computed from them, or not from them at all
        how = _derived(float(ans.value), texts, kind) or _near_digits(ans.value, texts)
        if how is None:
            return {"state": "unlocated", "why": "the marked text does not print this number (nor its parts)",
                    "shown": None, "of": None}
        return {"state": "approximate", "why": how, "shown": None, "of": None}
    geom = [e for e in loc if e.polygon or e.polyline or e.ifc_guid]
    if geom and any(e.exact is not False for e in geom) and _STRUCT_SOLVER.search(str(ans.solver or "")):
        return {"state": "supported", "why": "measured from the drawing's own geometry (outline shown)", "shown": None,
                "of": None}
    if geom:
        return {"state": "approximate", "why": "the outline is traced or estimated, not read from the drawing's own "
                "data: check its shape",
                "shown": None, "of": None}
    if all(e.exact for e in loc) and _STRUCT_SOLVER.search(str(ans.solver or "")):
        return {"state": "supported", "why": "read from the drawing's own data (marked)", "shown": None, "of": None}
    return {"state": "approximate", "why": "the marks show where it was read, not the measured shape or printed value",
            "shown": None, "of": None}


def clean_boxes(ans: Answer) -> Answer:
    """Audit architecture notes: Evidence accepts bbox [1], reversed [5, 5, 1, 1], NaN. A box a viewer cannot draw is
    dropped to file level (note kept, as the contract does for an unknown space); reversed corners are ordered."""
    import math

    out, changed = [], False
    for e in ans.evidence or []:
        b = e.bbox
        if b is not None:
            ok = len(b) == 4 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in b)
            nb = [min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])] if ok else None
            if nb != list(b):
                e = e.model_copy(update={"bbox": nb})
                changed = True
        out.append(e)
    if changed:
        ans.evidence = out
    return ans


def mark_support(question: str, ans: Answer) -> Answer:
    """Pipeline.solve's last step: the evidence state as a trace line, and Answer.support once the contract has it."""
    try:
        clean_boxes(ans)
    except Exception:  # noqa: BLE001
        pass
    try:
        st = support_state(question, ans)
    except Exception:  # noqa: BLE001
        return ans
    if st is None:
        return ans
    ans.trace = [t for t in ans.trace if not str(t).startswith("support: ")] + \
        [f"support: {st['state']} — {st['why']}"]
    if "support" in type(ans).model_fields:
        ans.support = st
    return ans


__all__ = ["Verdict", "asked_kind", "other_room", "check_citation", "citation_guard", "cited_texts", "final_guard",
           "mark_support", "occurrences", "role_of", "support_state"]
