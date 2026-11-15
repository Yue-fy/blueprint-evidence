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

import itertools
import os
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


# T1 c3 audit (400 prediction files): area answers 'supported' by a BARE number with 3 decimals read as a decimal
# ('16.850' PE Store = 16.85 m², '37.800' Dining, '18.000', '7.200', '10,000' -> 10.0) were 13 / 13 wrong at 0.75: on a
# metric sheet that token is a dimension in metres to the millimetre (or mm with a thousands mark); areas are printed
# with 0-2 decimals. Its thousands reading ('6,285' SF = 6285, AFCEC) is a different value and stays plain.
_DIM3 = re.compile(r"\d{1,3}[.,]\d{3}")
_BARE_FI = re.compile(r"""\s*\d{1,3}\s*'\s*-?\s*\d{1,2}(?:\s+\d{1,2}/\d{1,2})?\s*"?\s*""")  # T1 c8: '40' - 0"', '9'-0"'
BARE_FI_CHECK = os.environ.get("STOA_T1_BARE_FI", "1") != "0"
_WRAPPED = re.compile(r"^\s*printed value '(?P<t>[^']{1,30})'\s*(?:\(|$)")


def _role(line: str, start: int, end: int, value: float, kind: str | None) -> str:
    r = role_of(line, start, end)
    if r == "plain" and kind in ("area", "volume") and _DIM3.fullmatch(line[start:end]):
        try:
            if abs(float(line[start:end].replace(",", ".")) - float(value)) <= 1e-6 * max(1.0, abs(float(value))):
                return "dimension"
        except ValueError:
            pass
    return r


def _never_for(question: str, kind: str) -> set[str]:
    never = set(NEVER[kind])
    if kind == "length":  # 'How thick is the slab?' may cite '150 THK'; 'How long is the wall?' may not
        never |= {r for r, rx in _ASKS.items() if not rx.search(question or "")}
    return never


def check_citation(question: str, value, texts: list[str], kind: str | None = None, unit: str | None = None) -> Verdict | None:
    """None = nothing to say (the value is not a number / not printed literally in the cited lines / question kind
    not guarded); Verdict(ok=False) = every literal occurrence of the value has a role the asked kind can never be."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    kind = kind or asked_kind(question, unit)
    if kind is None:
        return None
    never = _never_for(question, kind)
    seen: list[tuple[str, str]] = []
    for line in texts:
        line = str(line or "")
        for s, e in occurrences(float(value), line):
            r = _role(line, s, e, float(value), kind)
            if r not in never:
                return Verdict(True, r, line)
            seen.append((r, line))
    if not seen:
        return _sum_verdict(question, float(value), texts, kind)
    r, line = seen[0]
    v = f"{value:g}" if isinstance(value, float) else str(value)
    why = (f"{TAG}: {v} is printed as {_ROLE_WORDS.get(r, r)} ('{line.strip()[:70]}'), not as {_KIND_WORDS[kind]}: "
           "this text does not give the asked quantity")
    return Verdict(False, r, line, why)


_KIND_WORDS = {"area": "an area", "count": "a number of items", "length": "this length", "volume": "a volume"}
_KIND_NOUN = {"area": "area", "count": "count", "length": "length", "volume": "volume"}
SUM_TAG = "is the sum of"  # in the rejection's why (tests / replays look for it)


def _sum_verdict(question: str, value: float, texts: list[str], kind: str) -> Verdict | None:
    """H 23:44 (S-100F PDF 'What is the combined slab-on-grade area, in m²?' -> pdf_text 750 m² @0.55 = 300 + 150 + 300,
    the slab THICKNESSES of '300 SLAB-ON-GRADE W/ D22 @ 300' …): a value printed in none of the cited lines but equal to
    the sum of one number from EACH of them (>= 2 lines) is built from those numbers. If every such sum adds numbers
    printed in a role the asked kind can never be (thickness / spacing / level / bar …), the sum is not the asked
    quantity -> rejected like a literal citation. A sum with at least one usable reading is left alone."""
    lines = [str(t or "") for t in texts if str(t or "").strip()]
    if len(lines) < 2 or len(lines) > 8 or value <= 0:
        return None
    never = _never_for(question, kind)
    per_line: list[list[tuple[float, str, str]]] = []
    for line in lines:
        opts = []
        for m in _NUM.finditer(line):
            r = _role(line, m.start(1), m.end(1), 0.0, kind)
            opts.extend((x, r, m.group(1)) for x in _readings(m.group(1)) if x > 0)
        if not opts:
            return None
        per_line.append(opts[:12])
    n = 1
    for o in per_line:
        n *= len(o)
    if n > 20000:
        return None
    targets = _conversions(value) if kind in ("area", "length", "volume") else [value]
    hits: list[tuple] = []
    for combo in itertools.product(*per_line):
        s = sum(x for x, _, _ in combo)
        if any(abs(s - t) <= max(0.0005 * abs(t), 0.006) for t in targets):
            hits.append(combo)
            if any(r not in never for _, r, _ in combo):
                return None  # one reading of the sum is made of usable numbers
    if not hits:
        return None
    combo = hits[0]
    bad = next(((r, tok, line) for (_, r, tok), line in zip(combo, lines) if r in never), None)
    r, tok, line = bad
    v = f"{value:g}"
    shown = " + ".join(t for _, _, t in combo)
    why = (f"{TAG}: {v} {SUM_TAG} {shown}, numbers printed as {_ROLE_WORDS.get(r, r)} ('{tok}' in "
           f"'{line.strip()[:60]}'), not as {_KIND_WORDS[kind]}: these texts do not give the asked quantity")
    return Verdict(False, r, line, why)


def _counted_by_marks(question: str, ans: Answer) -> bool:
    """A count whose located marks are one per counted item (6 door-schedule rows -> 6 doors): the number comes from the
    marks, not from a printed literal, so a '6' that only appears as a width (6' - 0") is no reason to reject it
    (T1 c2 sweep: sched6 sl-249 'According to the door schedule, how many doors …' = 6, right, would be rejected)."""
    v = ans.value
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer() or v <= 0:
        return False
    if asked_kind(question, ans.unit) != "count":
        return False
    marks = [e for e in ans.evidence or [] if _located(e) and not _AUX_NOTE.search(str(e.note or ""))]
    return len(marks) == int(v)


def citation_guard(question: str, ans: Answer, texts: list[str] | None = None) -> Answer:
    """Readers' hook: cap a reading whose cited number cannot be the asked quantity (cascade falls through)."""
    try:
        if _counted_by_marks(question, ans):
            return ans
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
        if _counted_by_marks(question, ans):
            return ans
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


# guesses: the prior (most common training answer) and the dummy; their boxes are candidate places, never proof
_GUESS_SOLVER = re.compile(r"(?:^|/)(?:prior|dummy)$", re.I)
# sanity.parts_guard's missing line (one printed part for a total of several) -> 'approximate'
PARTS_MISSING = "one printed part, not the total"
DIMCHECK_MISSING = "the printed dimensions used do not add up to the printed overall"  # = chaincheck.DIMCHECK_MISSING
PRINTED_COUNT_MISSING = "the sheet prints a different count"  # = printedcount.PRINTED_COUNT_MISSING (xr-40)
OPPOSITE_MISSING = "the marked lines name a different thing"  # = sanity.OPPOSITE (D1 23:22)
UNCOUNTED_MISSING = "not counted (no symbols located)"  # = sanity.UNCOUNTED (captain 23:04): a count nothing marks
DIMCHECK_ALL = "dimension check (xr-36): all checked"  # = chaincheck.ALL_CHECKED
_AREA_WORD = re.compile(r"m²|m2\b|m\s?2\b|\bsq|\bs\.?f\b|ft²|ft2|\barea|\bgfa|\bgia|\bnia|\bnfa|\bala\b|fläche|yta\b|"
                        r"superficie|surface|\d\s*M\s*$", re.I)


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
                            r"outline area|polygon area|at (?:the )?(?:printed |scale )?1\s*:\s*\d+|"
                            # our segmenter's / detector's candidates ('the one floor drawing on this sheet — candidate
                            # (segmentation finds 1 …)', 'detector … marks 2 here'): 'the printed total' on 48 right /
                            # 33 wrong counts in the T1 c3 replay, never printed text
                            r"candidates?|segment(?:ation|er|ed)|detector|cross-check)\b", re.I)


def _own_measurement(e) -> bool:
    return bool(e.polygon or e.polyline) or bool(_MEASURED_NOTE.search(str(e.note or "")))


def _printed_support(question: str, ans: Answer) -> str | None:
    """A cited line prints the value (or its unit conversion) in a role the asked kind can be -> that line. Notes of
    our own measurements (outlines, 'traced at 1:200') are not printed text."""
    hit = _printed_hit(question, ans)
    return hit[0] if hit else None


def _printed_hit(question: str, ans: Answer) -> tuple[str, str] | None:
    """(line, role) of the first cited line that prints the value in a role the asked kind can be."""
    if isinstance(ans.value, bool) or not isinstance(ans.value, (int, float)):
        return None
    kind = asked_kind(question, ans.unit)
    # a scale bar / legend / title-block note is never the printed value ('graphic scale bar 0 10 20 m' gave 10.0 m of
    # skirting 'supported', T1 c3)
    texts = cited_texts(ans) + [str(e.note) for e in ans.evidence if _located(e) and not _POST_HOC.search(str(e.note))
                                and not _own_measurement(e) and not _CITED_NOTE.match(str(e.note or ""))
                                and not _AUX_NOTE.search(str(e.note or ""))]
    never = _never_for(question, kind) if kind else set()
    first = None
    for cand in _conversions(float(ans.value)):
        for line in texts:
            for s, e in occurrences(round(cand, 6), line) or occurrences(round(cand, 2), line):
                r = _role(line, s, e, cand, kind)
                if r in never:
                    continue
                # T1 c5: AFCEC 'NORTH WING DORMITORY = 6,285 SF' was marked, but the bare OCR text '6,285' came first
                # (-> 'approximate: no area unit'): a marked line that names the kind wins over a bare token
                if kind != "area" or r == "area" or _AREA_WORD.search(line):
                    return line, r
                first = first or (line, r)
    return first


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


def _sum_of_marks(question: str, ans: Answer, loc: list, kind: str | None) -> tuple[str, int] | None:
    """'the sum of the 3 marked printed values (6,095 + 6,092 + 6,285)' when every located mark prints one value of the
    asked kind and they add up to the answer (E1 area_table 'total dormitory area across all three wings' = 18,472 on
    AFCEC: 3 marked rows, exact=False -> was 'approximate — the marks show where it was read')."""
    v = ans.value
    if kind not in ("area", "length", "volume") or isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    marks = [e for e in loc if not _AUX_NOTE.search(str(e.note or ""))]
    if len(marks) < 2 or len(marks) > 60:
        return None
    parts: list[tuple[float, str]] = []
    for e in marks:
        if _POST_HOC.search(str(e.note or "")) or _own_measurement(e):
            return None
        m = _CITED_NOTE.match(str(e.note or ""))
        line = m.group("t") if m else str(e.note or "")
        nums = [(n, role_of(line, n.start(1), n.end(1))) for n in _NUM.finditer(line)]
        own = [n for n, r in nums if r == kind] or [n for n, r in nums if r not in NEVER[kind]]
        if len(own) != 1:
            return None
        reads = sorted(_readings(own[0].group(1)), reverse=True)
        if not reads:
            return None
        parts.append((reads[0], own[0].group(1)))
    total = sum(x for x, _ in parts)
    if any(abs(total - c) <= 0.005 * max(abs(c), 1e-9) for c in _conversions(float(v))):
        shown = " + ".join(t for _, t in parts[:8]) + (" + …" if len(parts) > 8 else "")
        return f"the sum of the {len(parts)} marked printed values ({shown})", len(parts)
    return None


# T1 c4: a summed mark whose label does not name what is asked ('combined area of the two ground catchments' = 40,228
# ('BUILDING #241') + 51,000 ('LINED GROUND CATCHMENT GC2') @0.7, gold 179,878; 'total floor area of the English
# classrooms' summed 'ICT Classroom: 62 m2' with 'English 1 … 5'): such a sum is 'approximate', never 'supported'.
_GENERIC = set("""what which whats is are was the a an of in on at for to this that these those its their our total combined
overall whole entire all both each every sum summed together altogether area areas floor floors square sq metre metres
meter meters m2 ft ft2 feet foot sf net gross internal external usable how many much big large size give me tell find
please plan sheet drawing level storey story there here shown drawn labelled labeled named called counting including
incl plus and or with without excluding number space spaces two three four five six seven eight nine ten eleven twelve
room rooms pièce pièces piece pieces raum räume zimmer huone huoneet huoneiden rum rummen stanza stanze locale locali
local locaux espace espaces sum up""".split())
# 'all rooms IN the entry vestibule module': the module is where, not what (T1 c4 replay: 22 right sums flagged before)
_WHERE = re.compile(r"\b(?:in|on|across|within|inside|throughout|at|from|between|per)\b.*$", re.I)
_ENGLISH = re.compile(r"\b(?:the|what|how|which|whats)\b", re.I)
_WORD = re.compile(r"[A-Za-zÀ-ÿ]{3,}")


def _stem(w: str) -> str:
    w = w.lower()
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def off_subject(question: str, marks: list) -> str | None:
    """The first summed mark's label that names none of the question's subject words (or, with no 'and' / 'or' in the
    question, lacks a subject word that most of the other marks carry); None when they all fit or labels are absent."""
    q = re.sub(r"\([^)]*\)", " ", question or "")
    if not _ENGLISH.search(q):
        return None
    q = _WHERE.sub(" ", q)
    W = [_stem(w) for w in _WORD.findall(q) if w.lower() not in _GENERIC]
    if not W:
        return None
    labels = []
    for e in marks:
        m = _CITED_NOTE.match(str(e.note or ""))
        t = (m.group("t") if m else str(e.note or "")).split(":")[0]
        labels.append((t.strip(), {_stem(w) for w in _WORD.findall(t)}))
    if not any(ws & set(W) for _, ws in labels):
        return None  # the labels name nothing the question says (codes, bare numbers): not checkable here
    for t, ws in labels:
        if not ws & set(W):
            return t
    if re.search(r"\b(?:and|or|plus|und|oder|ja|tai|et|ou|och|eller)\b|,", q, re.I):
        return None
    for w in set(W):
        has = [t for t, ws in labels if w in ws]
        lack = [t for t, ws in labels if w not in ws]
        if len(has) >= 2 and len(has) > len(lack) and lack:
            return lack[0]
    return None


_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12, "both": 2}
_NAMED_N = re.compile(r"\b(?:the|all|all the|all of the|these|those|its|their)\s+(two|three|four|five|six|seven|eight|nine|"
                      r"ten|eleven|twelve|\d{1,2})\s+(?:[a-z][\w-]*\s+){0,3}?[a-z]+s\b|\b(both)\s+(?:the\s+)?[a-z]+s\b",
                      re.I)


def named_count(question: str) -> int | None:
    """3 for 'across all three wings', 4 for 'the four private offices', 2 for 'both bedrooms'; None otherwise."""
    m = _NAMED_N.search(question or "")
    if not m:
        return None
    w = (m.group(1) or m.group(2)).lower()
    return int(w) if w.isdigit() else _NUMBER_WORDS.get(w)


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
_LABEL = re.compile(r"^\s*(?:text:?\s*)?'?(?P<l>[A-Za-z][A-Za-z.' -]{1,40}?)\s*(?:\([^)]{0,30}\)\s*)?[:=–-]\s*\d")


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


ROOMS_CHECK = os.environ.get("STOA_T1_ROOMS", "1") != "0"  # kill switch for marked_rooms_other (T1 c8)
# T1 c8 (sl-317): a measured outline's own note 'MFL 1 55 m2: 54.83 m²' / 'English 2: 54.85 m²' / 'Sports Hall: 594 m2'
_MARK_LABEL = re.compile(r"^\s*(?:text:?\s*|label\s+|room\s+)?'?(?P<l>[A-Za-z][A-Za-z.&'/ -]{1,40}?)\s*(?:\b\d{1,3}[A-Za-z]?\b)?"
                         r"\s*(?:\(?\d[\d.,]*\s*(?:m2|m²|sq\.? ?ft|sf|ft2)\)?)?\s*[:=]\s*\d")
_NUM_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "all",
              "both", "each", "every", "combined", "together", "entire", "whole", "named", "called", "labelled", "labeled"}
_NOTE_WORDS = {"text", "printed", "value", "label", "answer", "line", "scale", "dimension", "measured", "traced", "outline",
               "sum", "candidate", "detector", "marks", "mark", "region", "box", "searched", "page", "sheet", "tag", "tags",
               "row", "table", "cell", "legend", "note", "notes", "total", "subtotal"}
# a class of rooms whose members are labelled by their own names ('classrooms' = English 1, MFL 2, Art 3 …)
_ROOM_CLASS = re.compile(r"\b(?:class\s?rooms?|teaching|offices?|bed\s?rooms?|bath\s?rooms?|toilets?|wcs?|stores?|storage|"
                         r"circulation|corridors?|plant|ancillary|support|staff|admin\w*|pupil|practical|specialist|"
                         r"general|habitable|living|wet|service|technical|labs?|laborator\w+|studios?|units?|flats?|"
                         r"apartments?|dwellings?|spaces?|areas?)\b", re.I)


def marked_rooms_other(question: str, marks: list) -> str | None:
    """T1 c8 (sl-317 'total floor area of the three art rooms (Art 1, Art 2 and Art 3)' = 328.95 @0.81 'supported' from
    six measured outlines 'MFL 1 …', 'English 2 …', none an art room; gold 272): every marked outline carries a room
    label and NONE of the labels names the asked room -> why ('the marked outlines are MFL 1, English 2 …: none is
    named like the art rooms the question asks for'), else None. Not for a class of rooms ('the classrooms', 'the
    offices': their members carry their own names) and not when any mark is unlabelled (unclear)."""
    m = _ASKED_ROOM.search(question or "")
    if not m or not marks:
        return None
    asked_raw = (m.group("a") or m.group("b") or "").strip()
    if _ROOM_CLASS.search(asked_raw):
        return None
    asked = [w for w in _words(asked_raw) if w not in _NUM_WORDS]
    if not asked:
        return None
    initials = "".join(w[0] for w in asked)
    anywhere = [w for w in _words(question) if w not in _NUM_WORDS]

    def agrees(w: str, pool: list[str]) -> bool:
        return any(w == p or (len(w) >= 4 and len(p) >= 4 and (w.startswith(p[:4]) or p.startswith(w[:4])))
                   for p in pool)

    labels = []
    for e in marks:
        lm = _MARK_LABEL.match(str(getattr(e, "note", "") or ""))
        if not lm:
            return None
        lw = _words(lm.group("l"))
        if not lw or set(lw) & _NOTE_WORDS:  # 'text: 8"', "printed value '1…'": our own words, not a room label
            return None
        if any(agrees(w, asked + anywhere) for w in lw) or \
                any(len(w) >= 2 and (w == initials or initials.startswith(w)) for w in lw):
            return None
        lab = re.split(r"\s*[:=]\s*\d", str(e.note))[0]
        lab = re.sub(r"\s*\(?\d[\d.,]*\s*(?:m2|m²|sq\.? ?ft|sf|ft2)\)?\s*$", "", lab).strip(" '")
        if lab not in labels:
            labels.append(lab)
    shown = ", ".join(labels[:4]) + (" …" if len(labels) > 4 else "")
    return f"the marked outlines are {shown}: none is named like the {asked_raw} the question asks for"


# a part of the building named by direction ('the north wing', 'west block') or by storey ('second floor', 'level 2')
_DIRS = r"north(?:-?(?:east|west))?|south(?:-?(?:east|west))?|east|west"
_ORD = r"ground|basement|first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|upper|lower|top"
_QUAL_Q = re.compile(rf"\b(?P<d>{_DIRS})(?:ern)?\s+(?P<p>wing|block|building|side|part|zone|annex|section)\b|"
                     rf"\b(?P<o>{_ORD})\s+(?P<f>floor|level|storey|story)\b|\b(?P<l>level|floor)\s+(?P<n>\d{{1,2}})\b", re.I)
_ORD_NUM = {"ground": "0", "first": "1", "1st": "1", "second": "2", "2nd": "2", "third": "3", "3rd": "3",
            "fourth": "4", "4th": "4", "fifth": "5", "5th": "5"}
_DIR_ANY = re.compile(rf"\b(?:{_DIRS})\b|\b[NSEW]\.?\s?(?:WING|BLOCK)\b", re.I)
_ORD_ANY = re.compile(rf"\b(?:{_ORD})\s+(?:floor|level|storey|story|flr)\b|\b(?:level|floor|lvl)\s*\d{{1,2}}\b|"
                      r"\b[0-9]{1,2}(?:st|nd|rd|th)?\s*(?:floor|flr)\b", re.I)


def qualifier_gap(question: str, line: str) -> tuple[str, str] | None:
    """('other' | 'unnamed', the question's qualifier) when the question names a part of the building ('the north
    wing', 'the second floor') and the marked printed line names another part ('WEST WING DORMITORY = 6,095 SF') or
    none at all ('DORMITORY = 6,095 SF': one OCR line of a table with a section per wing). None when it agrees."""
    m = _QUAL_Q.search(question or "")
    if not m:
        return None
    lab = line or ""
    if m.group("d"):
        want = m.group("d").lower().replace("-", "")
        qual = f"{m.group('d')} {m.group('p')}".lower()
        got = [g.group(0).lower().replace("-", "").replace(" ", "").replace(".", "") for g in _DIR_ANY.finditer(lab)]
        if not got:
            return "unnamed", qual
        ini = "".join(w[0] for w in re.findall(r"north|south|east|west", want))
        if any(g == want or g.startswith(want) or g.rstrip("wingblock") == ini for g in got):
            return None
        return "other", qual
    if m.group("o"):
        word = m.group("o").lower()
        qual = f"{word} {m.group('f')}".lower()
        num = _ORD_NUM.get(word)
    else:
        word, num, qual = None, m.group("n"), f"{m.group('l')} {m.group('n')}".lower()
    got = [g.group(0).lower() for g in _ORD_ANY.finditer(lab)]
    if not got:
        return "unnamed", qual
    def nums(g: str) -> set[int]:
        return {int(d) for d in re.findall(r"\d+", g)} | {int(_ORD_NUM[w]) for w in re.findall(r"[a-z0-9]+", g)
                                                          if w in _ORD_NUM}

    if any((word and word in g) or (num and int(num) in nums(g)) for g in got):
        return None
    return "other", qual


_TAG_NOTE = re.compile(r"^\s*(?:tag\s+)?'?(?P<p>[A-Z]{1,6})-\d{1,3}[A-Z]?'?\s*$")


def one_tag_family(question: str, marks: list) -> str | None:
    """Expert rule xr-43: 'How many pumps' = every drawn pump with a tag OR a text label naming one, not only the main
    tag family. The family prefix ('FP') when >= 2 marks are all tags of ONE family that the question does not name
    ('fuel pumps (FP)', 'FP-tagged', 'tagged …'), else None."""
    fams = set()
    for e in marks:
        m = _TAG_NOTE.match(str(e.note or ""))
        if not m:
            return None
        fams.add(m.group("p").upper())
    if len(marks) < 2 or len(fams) != 1:
        return None
    fam = fams.pop()
    q = question or ""
    if re.search(rf"(?<![A-Za-z]){re.escape(fam)}(?![a-z])", q) or re.search(r"\btag(?:s|ged)?\b|\blabell?ed\b", q, re.I):
        return None
    words = re.findall(r"[a-z]+", q.lower())
    k = len(fam)
    if any("".join(w[0] for w in words[i:i + k]) == fam.lower() for i in range(len(words) - k + 1)):
        return None  # the family IS the asked class: 'check valves' = CV, 'fuel pumps' = FP, 'pressure gauges' = PG
    return fam


_TAG_NUM = re.compile(r"^\s*(?:tag\s+)?'?(?P<p>[A-Z]{1,6})-(?P<n>\d{1,3})[A-Z]?'?\s*$")
DUP_IOU = 0.5
DOUBT_CONF = 0.6  # < web 'Fairly sure' 0.7
ODD_LO, ODD_HI = 0.6, 1.7


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def distinct_marks(question: str, marks: list) -> str | None:
    """T1 c7 (captain 00:31, AUDIT RECHECK-0026 sl-041): 'supported' for a count needs N DISTINCT real objects, not N
    marks. None when the marks pass; else a why ('dup: …' / 'odd: …'):
    - dup: two marks on the same spot (box IoU >= 0.5, same file / page / frame) that E1's 'x2 at this spot' does not
      explain: one object may have been counted twice;
    - odd: in a family of >= 4 tag marks 'XX-n', one whose text box is < 0.6x / > 1.7x the family's median height, or
      numbered 0 while the rest start at >= 1 ('PS-0' read from a 16 px fragment among 35 px PS- tags = a misread)."""
    boxes = [(e, tuple(float(c) for c in e.bbox)) for e in marks
             if getattr(e, "bbox", None) and len(e.bbox) == 4 and "at this spot" not in str(e.note or "")]
    for i in range(len(boxes)):
        ei, bi = boxes[i]
        for j in range(i + 1, len(boxes)):
            ej, bj = boxes[j]
            if (ei.file, ei.page, getattr(ei, "bbox_space", None)) != (ej.file, ej.page, getattr(ej, "bbox_space", None)):
                continue
            if _iou(bi, bj) >= DUP_IOU:
                return (f"dup: two of the marks sit on the same spot ('{str(ei.note or '')[:30]}' and "
                        f"'{str(ej.note or '')[:30]}'): one object may be counted twice")
    tags = []
    for e, b in boxes:
        m = _TAG_NUM.match(str(e.note or ""))
        if m:
            tags.append((m.group("p").upper(), int(m.group("n")), b[3] - b[1], str(e.note).strip()))
    fams: dict[str, list] = {}
    for t in tags:
        fams.setdefault(t[0], []).append(t)
    for fam, ts in fams.items():
        if len(ts) < 4:
            continue
        hs = sorted(t[2] for t in ts)
        med = hs[len(hs) // 2]
        nums = sorted(t[1] for t in ts)
        for p, n, h, note in ts:
            if med > 0 and not (ODD_LO * med <= h <= ODD_HI * med):
                return (f"odd: '{note}' does not look like the other {len(ts) - 1} {fam}- tags (its text is "
                        f"{h / med:.1f}x their height): it may be a misread")
            if n == 0 and nums[1] >= 1:
                return f"odd: '{note}' is numbered 0 while the other {fam}- tags start at {nums[1]}: it may be a misread"
    return None


WEAK_MARKS_CHECK = os.environ.get("STOA_T1_WEAK_MARKS", "1") != "0"  # kill switch (T1 c8)
_WEAK_MARKS = re.compile(r"(?:^|/)(?:pdf_text|agent|door_detect|gemini_vision|raster_cc5k)$")
PLURAL_CHECK = os.environ.get("STOA_T1_PLURAL", "1") != "0"  # kill switch for plural_labels (T1 c8)
_LABEL_MARK = re.compile(r"^\s*label\s+'(?P<l>[^']{1,60})'\s*$", re.I)


def plural_labels(question: str, marks: list) -> str | None:
    """T1 c8 (sl-353 'How many restrooms are there?' = 1 from the label 'RESTROOMS,', gold 2): a count whose marks are
    printed LABELS that read like a note fragment (end in ',' / ';' / ':') or, for a single mark, name the counted
    noun in the plural ('RESTROOMS' for 'how many restrooms'): one such label is not one object -> why, else None."""
    labs = []
    for e in marks:
        m = _LABEL_MARK.match(str(getattr(e, "note", "") or ""))
        if not m:
            return None
        labs.append(m.group("l").strip())
    if not labs:
        return None
    frag = [x for x in labs if re.search(r"[,;:]\s*$", x)]
    if frag and len(frag) == len(labs):
        return (f"the marked label '{frag[0]}' reads like a heading or part of a note, not the name of one item: "
                "what it names is not marked one by one — check the drawing")
    noun = _counted_noun(question).lower()
    last = re.findall(r"[a-z]+", labs[0].lower())
    if len(labs) == 1 and last and noun.endswith("s") and last[-1] == noun.split()[-1] and len(last[-1]) >= 4:
        return (f"the marked label '{labs[0]}' names {noun} in the plural: it may stand for several, not one — "
                "check the drawing")
    return None


TITLE_CHECK = os.environ.get("STOA_T1_TITLE", "1") != "0"  # kill switch for title_mark (T1 c8)
_OWN_LABEL = re.compile(r"^\s*(?:\d{1,3}:\s*(?P<a>.{2,80}?)\s*\(printed label\)|label\s+'(?P<b>[^']{2,80})'|text:\s*(?P<c>.{2,80}))\s*$",
                        re.I)
_VIEW_TITLE = re.compile(r"\b(?:floor\s+plan|plan\s+view|reflected\s+ceiling|rcp|elevations?|axon\w*|isometric|layout|site\s+plan|"
                         r"roof\s+plan|key\s*plan|enlarged\s+plan|part\s+plan|general\s+arrangement)\b|\bmodule\s+\d+\s*-",
                         re.I)


def title_mark(marks: list) -> str | None:
    """T1 c8 (sl-347 'How many restrooms are there?' = 3 marks 'Female Restrooms', 'MODULE 05 - RESTROOMS - FLOOR PLAN',
    'Male Restrooms', gold 2): a mark whose OWN label is a drawing's view title (… FLOOR PLAN / RCP / AXON. / ELEVATION,
    'MODULE 05 - …') is not one of the counted items -> that label, else None. Marks that only NAME their view as a place
    ('door 1 · MODULE 02-ADMINISTRATION - FLOOR PLAN · opening 0.92 m': 17 right / 4 wrong) are not judged."""
    for e in marks:
        m = _OWN_LABEL.match(str(getattr(e, "note", "") or ""))
        if not m:
            continue
        lab = (m.group("a") or m.group("b") or m.group("c") or "").strip()
        lab = re.sub(r"\s*\(view\s[^)]*\)?\s*$", "", lab)  # 'Storage / 7.04 / 1786 SF (view MODULE 07 - SUPPORT - RCP)'
        if _VIEW_TITLE.search(lab):
            return lab
    return None


GAP_CHECK = os.environ.get("STOA_T1_GAP", "1") != "0"  # kill switch for series_gap (T1 c8)
_SERIES = re.compile(r"^\s*(?:tag\s+|label\s+|text:?\s*|room\s+)?'?(?P<p>[A-Za-z][A-Za-z.&/ ]{0,30}?)(?:\s+|-)(?P<n>\d{1,3})"
                     r"[A-Z]?(?=['\s:=,(]|$)")
_SERIES_STOP = {"level", "floor", "storey", "story", "page", "sheet", "grid", "line", "detail", "section", "elevation",
                "zone", "phase", "block", "building", "wing", "type", "note", "item", "step", "row", "column", "revision",
                "rev", "plot", "unit", "flat", "apartment", "house"}


def series_gap(question: str, marks: list) -> str | None:
    """T1 c8 (sl-340 'total floor area of the English classrooms' = 'English 1' … 'English 7' + 'English 9' summed,
    440 @0.77, gold 495: English 8's tag was not read): the marks are a numbered series of one name ('English n',
    'PS-n') with >= 4 members, starting at 1 or 2, and a hole inside their own range (at most a quarter of it) ->
    why ('English 8 is not among the marks, although English 1-9 are …'), else None. Not when the question itself
    names numbers of that series ('Art 1, Art 2 and Art 3': the asked set may skip one)."""
    fams: dict[str, set[int]] = {}
    names: dict[str, str] = {}
    for e in marks:
        m = _SERIES.match(str(getattr(e, "note", "") or ""))
        if not m:
            continue
        p = re.sub(r"\s+", " ", m.group("p")).strip(" .-")
        key = p.lower()
        if not key or key in _SERIES_STOP or key.split()[-1] in _SERIES_STOP or key.startswith("ifc"):
            continue
        fams.setdefault(key, set()).add(int(m.group("n")))
        names.setdefault(key, p)
    for key, nums in fams.items():
        if len(nums) < 4:
            continue
        lo, hi = min(nums), max(nums)
        holes = sorted(set(range(lo, hi + 1)) - nums)
        if lo > 2 or not holes or len(holes) > max(1, (hi - lo + 1) // 4):
            continue
        if re.search(rf"\b{re.escape(names[key])}[\s-]?\d", question or "", re.I):
            continue
        sep = "-" if re.search(rf"{re.escape(names[key])}-\d", " ".join(str(getattr(e, 'note', '')) for e in marks)) else " "
        miss = ", ".join(f"{names[key]}{sep}{h}" for h in holes[:3]) + (" …" if len(holes) > 3 else "")
        return (f"{miss} {'is' if len(holes) == 1 else 'are'} not among the marks, although {names[key]}{sep}{lo} to "
                f"{names[key]}{sep}{hi} are: {'it' if len(holes) == 1 else 'they'} may be unread — check the drawing")
    return None


_FAM_Q = re.compile(r"(?<![A-Za-z])([A-Z]{2,5})(?![a-z])")
_FAM_STOP = {"PDF", "PNG", "IFC", "DXF", "CAD", "BIM", "MEP", "HVAC", "SF", "GSF", "NSF", "SQ", "FT", "MM", "CM", "USA", "UK",
             "NO", "ID", "AND", "OR", "THE"}


def missing_families(question: str, marks: list) -> str | None:
    """T1 c7 (sl-331 'How many filter separators (FSI and FSR)?' = 5 FSI- marks, gold 7): the question NAMES >= 2 tag
    families and the marks are tags of only some of them -> why ('the question names FSI and FSR; only FSI- tags are
    marked …'), else None. Only when every mark is an 'XX-n' tag (a text label of another kind may be the other family)."""
    named = [f for f in dict.fromkeys(_FAM_Q.findall(question or "")) if f not in _FAM_STOP]
    if len(named) < 2 or not marks:
        return None
    got = set()
    for e in marks:
        m = _TAG_NUM.match(str(e.note or ""))
        if not m:
            return None
        got.add(m.group("p").upper())
    if not got <= set(named):
        return None
    miss = [f for f in named if f not in got]
    if not miss:
        return None
    return (f"the question names {' and '.join(named)}; only {', '.join(sorted(got))}- tags are marked (no "
            f"{', '.join(miss)}- tag found): check the drawing for them")


def _counted_noun(question: str) -> str:
    m = re.search(r"\bhow\s+many\s+(?P<x>[a-z][a-z -]{1,30}?)\s+(?:are|is|does|do|can|in|on|there|shown|drawn)\b",
                  question or "", re.I)
    return m.group("x").strip() if m else "other items"


_TABLE_CHECK = re.compile(r"cross-check: the sheet's printed area table says (?P<v>\d[\d,]*(?:\.\d+)?) ?(?P<u>[a-z0-9²]+)? ✓",
                          re.I)


def _table_agrees(ans: Answer) -> str | None:
    """'6285 ft2' when a trace line says the sheet's printed area TABLE gives this very value (the cross-check the
    pipeline runs after an area answer), else None. Not 'N of M printed areas match the traced outline' (area_tags)."""
    if isinstance(ans.value, bool) or not isinstance(ans.value, (int, float)):
        return None
    for t in ans.trace:
        m = _TABLE_CHECK.search(str(t))
        if m:
            v = float(m.group("v").replace(",", ""))
            if abs(v - float(ans.value)) <= 0.005 * max(abs(v), 1e-9):
                return f"{m.group('v')} {m.group('u') or ''}".strip()
    return None


# F8 00:13 / 00:31 (web/src/features/report/check.ts ppmCheck, ported): a PICTURE's area answer whose marked outline(s),
# measured at the scale bar the reader states ('1 m = 29.6 px', '29.60 px per m'), differ from the value by > 5 % (and
# the value is neither a printed figure nor the outline widened by the drawn line thickness): F8's replay on 60 upload
# runs: every such 'check' verdict was a wrong answer; right answers agree within 2 % or are 2-5 % larger. One-way: an
# agreement proves nothing (6 right / 4 wrong), so it never makes an answer 'supported'.
_PPM = re.compile(r"(\d+(?:\.\d+)?)\s*px\s*per\s*m(?:etre|eter)?\b|\b1\s*m\s*=\s*(\d+(?:\.\d+)?)\s*px\b", re.I)
_RESIZED = re.compile(r"\((\d+)\s*[×x]\s*\d+\s*px\)\s*read at (\d+)\s*[×x]\s*\d+\s*px", re.I)
_IMG = re.compile(r"\.(?:png|jpe?g|tiff?|webp|bmp|gif)$", re.I)


def _shoelace(p: list) -> float:
    n = len(p)
    return abs(sum(p[i][0] * p[(i + 1) % n][1] - p[(i + 1) % n][0] * p[i][1] for i in range(n))) / 2


def scale_bar_check(ans: Answer, kind: str | None) -> str | None:
    v = ans.value
    if kind != "area" or isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    ev = [e for e in ans.evidence or [] if e.polygon and len(e.polygon) >= 3]
    if not ev or len(ev) > 12:
        return None
    if not all((e.bbox_space or ("image_px" if _IMG.search(str(e.file or "")) else "pdf_pt")) == "image_px" for e in ev):
        return None
    notes = [str(e.note or "") for e in ans.evidence or []]
    lines = [*(str(x) for x in ans.assumptions or []), *(str(x) for x in ans.trace or []), *notes]
    allt = " ".join(lines)
    rz = _RESIZED.search(allt)
    fz = float(rz.group(1)) / float(rz.group(2)) if rz and float(rz.group(2)) > 0 else 1.0
    if not 1 <= fz < 20:
        return None
    src = next((t for t in [*(str(e.note or "") for e in ev), *lines] if _PPM.search(t)), None)
    if src is None:
        return None
    m = _PPM.search(src)
    ppm = float(m.group(1) or m.group(2)) * fz
    if ppm <= 1:
        return None
    ft = bool(re.search(r"ft|sf", str(ans.unit or "").lower()))
    try:
        px = sum(_shoelace(e.polygon) for e in ev)
    except (TypeError, IndexError):
        return None
    real = px / (ppm * ppm) * (10.7639104 if ft else 1.0)
    d = abs(real - float(v)) / float(v)
    if d <= 0.05 or real < 0.5 * float(v):  # < half: only some of the summed rooms are outlined (partial, not a contradiction)
        return None
    printed = any(e.exact is True and re.search(r"\bprint(?:s|ed)\b", str(e.note or ""), re.I) and not e.polygon
                  and not e.polyline for e in ans.evidence or []) or \
        any(re.search(r"\b(?:as printed|printed (?:in|on) the (?:room|tag|table|schedule))\b", str(x), re.I)
            for x in ans.assumptions or [])
    if printed:
        return None
    thick = any(re.search(r"line thickness|line width", str(x), re.I) for x in [*(ans.assumptions or []), *(ans.trace or [])])
    if thick and real < v and d <= 0.15:
        return None
    u = "ft²" if ft else "m²"
    return (f"the marked outline measures {real:,.1f} {u} at the picture's scale bar ({ppm:.2f} px per m), "
            f"{d * 100:.0f} % from the answer: check the outline and the scale bar")


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
    if _GUESS_SOLVER.search(str(ans.solver or "")):  # T1 c3: 'supported — 1 marks, one per counted item' on a prior
        return {"state": "unlocated", "why": "a typical value for this kind of question, not read from this drawing "
                "(the marks are only candidate places)", "shown": None, "of": None}
    opp = next((str(m) for m in ans.missing if str(m).startswith(OPPOSITE_MISSING)), None)
    if opp:  # sanity.opposite_guard: 'roof' asked, every marked line says GROUND
        return {"state": "unlocated", "why": opp.split(" — ")[0], "shown": None, "of": None}
    unc = next((str(m) for m in ans.missing if str(m).startswith(UNCOUNTED_MISSING)), None)
    if unc:  # sanity.uncounted_guard: a count > 0 with no located object = a guess, not a count
        # the card prefixes 'Not located on the drawing: ' (web customer.ts supportHtml): no second 'no symbols located'
        return {"state": "unlocated", "why": "not counted — " + unc.split(". ")[0][len(UNCOUNTED_MISSING):].lstrip(" :"),
                "shown": 0, "of": None}
    part = next((str(m) for m in ans.missing if str(m).startswith(PARTS_MISSING)), None)
    if part:  # sanity.parts_guard: one printed item answers a question about the total of several
        return {"state": "approximate", "why": part[len(PARTS_MISSING):].lstrip(" :—-"), "shown": None, "of": None}
    pc = next((str(m) for m in ans.missing if str(m).startswith(PRINTED_COUNT_MISSING)), None)
    if pc:  # printedcount (xr-40): '48 DORMS' printed on the sheet, 2 counted
        return {"state": "approximate", "why": pc.split(" — ")[0], "shown": None, "of": None}
    dimc = next((str(m) for m in ans.missing if str(m).startswith(DIMCHECK_MISSING)), None)
    if dimc:  # chaincheck (expert rule xr-36): the printed strings used do not add up to the printed overall
        return {"state": "approximate", "why": "the printed dimensions used do not add up to the printed overall"
                + dimc[len(DIMCHECK_MISSING):].split(" — ")[0], "shown": None, "of": None}
    loc = [e for e in ans.evidence or [] if _located(e)]
    if not loc:
        return {"state": "unlocated", "why": "nothing on the drawing is marked for this answer", "shown": None,
                "of": None}
    kind = asked_kind(question, ans.unit)
    bar = scale_bar_check(ans, kind) if kind == "area" else None
    if bar:  # F8 00:13 / 00:31 (report ppmCheck, ported): the marked outline at the picture's own scale bar disagrees
        return {"state": "approximate", "why": bar, "shown": None, "of": None}
    rooms = marked_rooms_other(question, [e for e in loc if not _AUX_NOTE.search(str(e.note or ""))]) \
        if kind in ("area", "length", "volume") and ROOMS_CHECK else None
    if rooms:  # T1 c8 (sl-317): 'the three art rooms' measured on MFL 1-3 + English 1-3 outlines (replay 0 R / 1 W)
        return {"state": "unlocated", "why": rooms, "shown": None, "of": None}
    gap = series_gap(question, loc) if kind == "area" and GAP_CHECK else None
    if gap:  # T1 c8 (sl-340): 'English 1'-'English 7' + 'English 9' summed; areas only (replay 2 R / 7 W; for counts
        # 14 R / 1 W: M-101A's PS- tags really skip PS-2, so a tag series may have holes)
        return {"state": "approximate", "why": gap, "shown": None, "of": None, "doubt": "gap"}
    if kind == "count" and isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool) and \
            float(ans.value).is_integer():
        n = int(ans.value)
        marks = [e for e in loc if not _AUX_NOTE.search(str(e.note or ""))]
        printed = _printed_support(question, ans)
        if n == 0:
            return {"state": "approximate", "why": "none found: the marks show where we looked, not proof of absence",
                    "shown": 0, "of": 0}
        agree = next((str(t) for t in ans.trace if str(t).startswith("printed count check (xr-40)") and "✓" in str(t)),
                     None)
        if agree and n > len(marks):  # printedcount: the sheet's own count table says this number ('48 DORMS')
            return {"state": "supported", "why": agree.split(": ", 1)[1].replace(" = this count ✓", "") +
                    " (the drawing's own count; " + f"{len(marks)} of {n} marked)", "shown": len(marks), "of": n}
        if len(marks) >= n:
            fam = one_tag_family(question, marks)
            if fam:  # xr-43 (Ziying, m101a-02): 'How many pumps' = FP-1..5 + jockey + drain = 7, ours 5 FP tags
                noun = _counted_noun(question)
                eg = " (e.g. a jockey or drain pump)" if "pump" in noun.lower() else ""
                return {"state": "approximate", "why": f"{n} marks, but all are {fam}-tagged: {noun} labelled by text "
                        f"or by another tag{eg} are not counted — check the drawing", "shown": len(marks), "of": n}
            fams = missing_families(question, marks)
            if fams:  # T1 c7 (sl-331 '(FSI and FSR)' = 5 FSI- marks): an asked tag family has no mark
                return {"state": "approximate", "why": f"{n} marks, but {fams}", "shown": len(marks), "of": n,
                        "doubt": "family"}
            if WEAK_MARKS_CHECK and _WEAK_MARKS.search(str(ans.solver or "")):
                # T1 c8: count marks from a text search / a schedule's text rows / a general model are not objects
                # checked one by one: 'supported — N marks, one per counted item' on 900 non-sealed upload files was
                # right 7 / 41 times for these readers (pdf_text 3 / 17: door schedule rows 76 of 95, 'Therapy Room'
                # for plant rooms), vs label_count 79 / 116, dxf 93 / 95; d31 + d32 sealed aggregates agree (5 / 18).
                # pdf_text schedule TEXT ROWS ('t1 row: 1.00A') 0 R / 11 W; its room-label text marks ('text: Specialist /
                # Science / Laboratory') 3 R / 3 W non-sealed, and the d31 + d32 sealed aggregates 0 R / 6 W for them: kept
                # in (pooled 3 / 12) — NB the sealed aggregates informed this choice, so sealed numbers are not independent
                return {"state": "approximate", "why": f"{n} marks, but they are found by a text search or a general "
                        "model (each match is one mark), not checked as objects one by one — check the marked spots",
                        "shown": len(marks), "of": n}
            title = title_mark(marks) if TITLE_CHECK else None
            if title:  # T1 c8 (sl-347): a drawing's title counted as a room (replay: 3 W / 0 R, one question + sl-327)
                return {"state": "approximate", "why": f"{n} marks, but the marked '{title[:50]}' is a drawing's title, "
                        "not one of the counted items — check the marked spots", "shown": len(marks), "of": n,
                        "doubt": "title"}
            plural = plural_labels(question, marks) if PLURAL_CHECK else None
            if plural:  # T1 c8 (sl-353 'RESTROOMS,'): a heading / note fragment / plural label is not one object
                # (replay 840 upload files: 0 right / 10 wrong flagged)
                return {"state": "approximate", "why": f"{n} marks, but {plural}", "shown": len(marks), "of": n,
                        "doubt": "plural"}
            doubt = distinct_marks(question, marks)
            if doubt:  # T1 c7 (sl-041 'PS-0'): N marks are not N distinct real objects
                return {"state": "approximate", "why": f"{n} marks, but {doubt.split(': ', 1)[1]} — check the marked "
                        "spots", "shown": len(marks), "of": n, "doubt": doubt.split(":")[0]}
            return {"state": "supported", "why": f"{n} marks, one per counted item", "shown": len(marks), "of": n}
        if printed is not None and len(marks) <= 3:
            return {"state": "supported", "why": f"the printed total '{printed.strip()[:60]}' is marked",
                    "shown": len(marks), "of": n}
        return {"state": "approximate", "why": f"showing {len(marks)} of {n} counted items (at least {len(marks)} "
                "located): the rest are not marked",
                "shown": len(marks), "of": n}
    hit = _printed_hit(question, ans)
    printed = hit[0] if hit else None
    if printed is not None:
        other = other_room(question, printed)
        if other:
            return {"state": "unlocated", "why": f"the marked label '{printed.strip()[:50]}' names {other}, not the "
                    "room the question asks for", "shown": None, "of": None}
        gap = qualifier_gap(question, printed) if kind in ("area", "length", "volume") else None
        if gap and gap[0] == "other":  # 'north wing' answered from 'WEST WING DORMITORY = 6,095 SF'
            return {"state": "unlocated", "why": f"the marked line '{printed.strip()[:50]}' is for another part of "
                    f"the building, not the {gap[1]}", "shown": None, "of": None}
        # NOT for a line that names no part ('DORMITORY = 6,095 SF' for the north wing, AFCEC damaged-image variants):
        # replay T1 c5 over the newest 300 upload files flagged 56 right / 7 wrong (a floor plan's room labels do not
        # repeat the floor's name; IFC 'on First Floor' rows 28 / 0), so that case keeps its state
        if kind == "area" and hit[1] == "plain" and not _AREA_WORD.search(printed):
            # T1 c3 audit: a bare number with no area unit / word ('1.06', '7.01', '14.00', '20.3') supported 19 wrong
            # areas and 1 right one: it may be a dimension or a room number
            return {"state": "approximate", "why": f"the marked number '{printed.strip()[:40]}' has no area unit "
                    "(m², SF) next to it: it may be a dimension or a room number, check it", "shown": None, "of": None}
        tok = _WRAPPED.match(printed)  # T1 c8: raster's own wrapper "printed value '33,000' (the reading's number)"
        tok = tok.group("t") if tok and BARE_FI_CHECK else printed  # (sl-292 'How wide is the sports hall?' 33 vs 18)
        if kind == "length" and (_DIM3.fullmatch(tok.strip()) or (BARE_FI_CHECK and _BARE_FI.fullmatch(tok))):
            # + T1 c8: a bare feet-inch dimension ('40' - 0"' for 'How deep is the house?', gold 24'; '9'-0"' for an
            # opening 4.67 wide): replay of 900 upload files 2 right / 5 wrong
            # T1 c6 replay (800 prediction files): a length 'supported' by ONE bare dimension token printed to the
            # millimetre ('18.880' for 'How long is the main hall?', EFA scans) was right 6 / 30 times (lengths from a
            # bare integer token: 48 / 61; from a labelled line: 209 / 224): the token names nothing it measures
            return {"state": "approximate", "why": f"the marked dimension '{tok.strip()}' has no label: check "
                    "that it measures what the question asks for (a dimension chain prints many such numbers)",
                    "shown": None, "of": None}
        return {"state": "supported", "why": f"the printed value is marked: '{printed.strip()[:60]}'", "shown": None,
                "of": None}
    summed = _sum_of_marks(question, ans, loc, kind)
    named = named_count(question) if summed else None
    odd = off_subject(question, [e for e in loc if not _AUX_NOTE.search(str(e.note or ""))]) if summed else None
    if odd:
        return {"state": "approximate", "why": f"{summed[0]}, but the marked '{odd[:50]}' does not name what the "
                "question asks for: check which ones belong", "shown": summed[1], "of": None}
    if summed and named is not None and named != summed[1]:  # 'the four private offices' summed over 8 tags
        return {"state": "approximate", "why": f"the question names {named}; {summed[0]} — check which ones belong",
                "shown": summed[1], "of": named}
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
    tab = _table_agrees(ans)
    if tab:  # T1 c5: 'cross-check: the sheet's printed area table says 2467.69 m2 ✓' (7 / 7 right, shown approximate)
        return {"state": "supported", "why": f"the sheet's printed area table gives the same {tab} (cross-check ✓)",
                "shown": None, "of": None}
    checked = next((str(t) for t in ans.trace if str(t).startswith(DIMCHECK_ALL)), None)
    if checked:  # chaincheck (xr-36): the value is computed from printed dimensions that add up to printed overalls
        return {"state": "supported", "why": checked[len(DIMCHECK_ALL):].lstrip(" :") +
                ", each adding up to the printed overall dimension", "shown": None, "of": None}
    if geom:
        return {"state": "approximate", "why": "the outline is traced or estimated, not read from the drawing's own "
                "data: check its shape",
                "shown": None, "of": None}
    if all(e.exact for e in loc) and _STRUCT_SOLVER.search(str(ans.solver or "")):
        parts = [e for e in loc if not _AUX_NOTE.search(str(e.note or ""))]
        odd = off_subject(question, parts) if kind in ("area", "length", "volume") and len(parts) >= 2 else None
        if odd:  # 'English classrooms' summed over 'ICT Classroom' too
            return {"state": "approximate", "why": f"read from the drawing's own data, but the marked '{odd[:50]}' "
                    "does not name what the question asks for: check which ones belong", "shown": None, "of": None}
        return {"state": "supported", "why": "read from the drawing's own data (marked)", "shown": None, "of": None}
    if summed:
        if named == summed[1]:  # 'all three wings' = 3 marked printed rows that add up to the answer
            return {"state": "supported", "why": summed[0], "shown": summed[1], "of": summed[1]}
        return {"state": "approximate", "why": f"{summed[0]}: check these are the parts the question means",
                "shown": summed[1], "of": None}
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
    if st.get("doubt"):  # T1 c7: a count whose marks may hold a misread / a double: never 'Fairly sure' on them
        if float(ans.confidence or 0) > DOUBT_CONF:
            what = {"gap": "a numbered item of the series is not marked",
                    "plural": "the marked label is not one object",
                    "title": "a marked label is a drawing's title"}.get(st["doubt"],
                                                                        "the marks are not all distinct real objects")
            ans.trace = [*ans.trace[:-1], f"support: confidence {float(ans.confidence):.2f} -> {DOUBT_CONF} ({what})",
                         ans.trace[-1]]
            ans.confidence = DOUBT_CONF
        st = {k: v for k, v in st.items() if k != "doubt"}
    if "support" in type(ans).model_fields:
        ans.support = st
    return ans


__all__ = ["scale_bar_check", "PARTS_MISSING", "off_subject", "Verdict", "asked_kind", "other_room", "check_citation", "citation_guard", "cited_texts", "final_guard",
           "mark_support", "distinct_marks", "missing_families", "marked_rooms_other", "series_gap", "plural_labels", "title_mark", "occurrences", "role_of", "support_state"]
