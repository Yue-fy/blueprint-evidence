"""Drawing SETS (VP, captain 18:50): a building can have 8 floor plans, a project several buildings. Classify every
page / sheet of the uploaded files so the project view (W5) and whole-building questions (G1) know what is where.

    idx = sheet_index(["set.pdf", "A-2.pdf", "plan.png"])   # [Sheet] one per PDF page / image
    idx[0].kind, idx[0].storey, idx[0].number, idx[0].title  # 'floor_plan', '2', 'A-102', 'SECOND FLOOR PLAN'
    pick_sheets("How many doors does the whole building have?", idx)   # -> every floor-plan sheet, one per storey
    pick_sheets("What is the ceiling height on level 2?", idx)          # -> the level-2 RCP sheet(s)
    storeys(idx)                                             # ordered storey table for the project view

Deterministic: text layer + the per-page views of stoa.sheet.views (~0.1 s per page), $0. Images are classified only
from a text layer the caller passes (no OCR here).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from stoa.sheet.views import (_NOT_TITLE, _WHOLE, DRAWING_KINDS, PLAN_KINDS, Pick, View, _keys, page_reading, pick_views,
                              subject_of, view_kind, views)

_SHEET_NO = re.compile(r"^\s*([A-Z]{1,3}-?\d{1,4}(?:\.\d{1,3})?[A-Z]?)\s*$")
# ISO 19650 / BS 1192 drawing numbers: PROJECT-ORIGINATOR-VOLUME-LEVEL-TYPE-ROLE-NUMBER ('SEN-DFE-XX-00-D-X-0201')
_ISO_NO = re.compile(r"^\s*((?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]{2,8}(?:-[A-Z0-9]{1,8}){4,8})\s*$")
# a value printed under / beside these labels is not the sheet number ('SIZE A2', 'REVISION C01', 'DATE …')
_NOT_NO_LABEL = re.compile(r"\b(?:SIZE|PAPER|FORMAT|REV(?:ISION)?S?|DATE|SCALE|STATUS|SUITABILITY|DRAWN|CHECKED|"
                           r"APPROVED|STAGE|ISSUE|PURPOSE)\b", re.I)
_NO_LABEL = re.compile(r"\b(?:NUMBER|NO\.?|NUM|SHEET|DRAWING|DWG)\b", re.I)
_PARTIAL = re.compile(r"\bENLARGED|\bPARTIAL|\bTYPICAL\b|\bTYP\.?\b|\bUNIT\s+PLAN|\bAREA\s+[A-Z]\b|\bAREA\s+PLAN\b|"
                      r"\bWING\b|\bQUADRANT|\bPART\s+PLAN", re.I)
_LEVEL_WORDS = re.compile(
    r"\b(?:(?:GROUND|GROUD|GRND|BASEMENT|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|1ST|2ND|3RD|\d{1,2}(?:ST|ND|RD|TH))"
    r"\s+(?:FLOOR|LEVEL|STOREY|STORY)|(?:LEVEL|FLOOR|STOREY|KERROS|TASO)\s*[-#:]?\s*\d{1,3}|ROOF|MEZZANINE|"
    r"\d\.\s*KERROS|ERDGESCHOSS|\bEG\b|\d\.\s*OG\b|UPPER\s+FLOOR|LOWER\s+FLOOR)", re.I)
_ORD_EXTRA = {"sixth": 6, "seventh": 7, "eighth": 8}
_COVER = re.compile(r"\bDRAWING\s+(?:INDEX|LIST)|\bSHEET\s+(?:INDEX|LIST)|\bINDEX\s+OF\s+DRAWINGS|\bCOVER\s+SHEET|"
                    r"\bTITLE\s+SHEET", re.I)


@dataclass
class Sheet:
    file: str
    page: int
    number: str | None = None          # sheet number from the title block ('A-101')
    title: str | None = None           # drawing title (title block) or the main view's title
    kind: str = "unknown"              # main kind: floor_plan | rcp | section | elevation | schedule | site_plan | ...
    storey: str | None = None          # storey key: '0', '1', '2', '-1', 'roof', ... (as numbered on the sheet)
    storey_name: str | None = None     # as printed ('SECOND FLOOR', 'Level 00', 'Ground Floor')
    building: str | None = None        # 'B' / 'BUILDING B' when the title names one
    partial: bool = False              # an enlarged / partial / typical-unit plan (not the whole storey)
    views: list = field(default_factory=list)
    ratio: float | None = None         # printed 1:N of the main view(s)
    page_ratio: float | None = None    # 1:N on this page (paper-size note applied)
    confidence: float = 0.5
    title_source: str | None = None    # 'title_block' (a drawing-kind line there) | 'views' | 'title_block_line'
    tb_lines: list = field(default_factory=list, repr=False)   # [(size, text, box)] title-block title candidates
    tb_anchored: str | None = field(default=None, repr=False)   # the line under a 'Drawing Title:' label
    tb_body: float = field(default=0.0, repr=False)              # median print size of the title block
    numbers_seen: set = field(default_factory=set, repr=False)  # every 'A-101'-style line on the page

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.number, self.title) if x) or f"{os.path.basename(self.file)} p{self.page + 1}"

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("file", "page", "number", "title", "kind", "storey", "storey_name",
                                            "building", "partial", "ratio", "page_ratio", "confidence", "title_source")}
        d["views"] = [v.as_dict() for v in self.views]
        return d


def _storey_of(text: str) -> tuple[str | None, str | None]:
    m = _LEVEL_WORDS.search(text or "")
    if not m:
        return None, None
    name = m.group(0).strip()
    low = name.lower()
    if "roof" in low:
        return "roof", name
    if "mezzanine" in low:
        return "mezzanine", name
    if "erdgeschoss" in low or re.fullmatch(r"eg", low):
        return "0", name
    if "lower floor" in low:
        return "0", name
    k = _keys(name).get("floor")
    if k is None:
        w = re.match(r"(sixth|seventh|eighth)", low)
        if w:
            k = str(_ORD_EXTRA[w.group(1)])
        m2 = re.match(r"(\d{1,2})(?:st|nd|rd|th)\s", low) or re.match(r"(\d)\.\s*(?:og|kerros)", low)
        if m2:
            k = str(int(m2.group(1)))
            if "kerros" in low:  # Finnish '1. kerros' = the entrance (ground) floor
                k = str(int(m2.group(1)) - 1)
    return k, name


def _title_block_lines(lines, W, H):
    from stoa.sheet.views import _titleblock_zone, _in

    zone = _titleblock_zone(lines, W, H)
    return [l for l in lines if _in(l.box, zone)] if zone else []


_TB_LABEL = re.compile(
    r":\s*$|^\s*[A-Z .]{2,24}:|\b(?:SHEET\s*(?:ID|NO|NUMBER|TITLE|SIZE)|DRAWING\s*(?:NO|NUMBER|TITLE)|DRAWN|CHECKED|DESIGNED|"
    r"APPROVED|SUBMITTED|REVIEWED|SCALES?|DATE|PROJECT\s*(?:NO|NUMBER)|JOB\s*(?:NO|NUMBER)|REVISIONS?|REV\b|ISSUE[DS]?|"
    r"FILE\s*(?:NAME|NUMBER|NO)|CONTRACT|SOLICITATION|SEAL|STAMP|NORTH|KEY\s*PLAN|CAD\b|PLOT|COPYRIGHT|DESCRIPTION|"
    r"MARK\b|CLIENT|OWNER|CONSULTANT|PRINTED|SUBMITTAL|NOT\s+FOR\s+CONSTRUCTION|PRELIMINARY|FOR\s+(?:REVIEW|CONSTRUCTION)|"
    r"(?:STANDARD|DEFINITIVE|SCHEMATIC|CONCEPT|100\s*%|\d{2}\s*%)\s+DESIGN\b|DESIGN\s+DEVELOPMENT|PROTOTYPE\s*$|"
    r"CONSTRUCTION\s+DOCUMENTS|BID\s+SET|ISSUED\s+FOR|TENDER|PERMIT\s+SET)", re.I)
_TB_FIRM = re.compile(r"\b(?:CORPS|ENGINEERS?|ENGINEERING|ARCHITECTS?|ARCHITECTURE|ASSOCIATES|INC|LLC|LLP|LTD|OY|AB|GMBH|"
                      r"CONSULTING|CONSULTANTS|DISTRICT|DEPARTMENT|AGENCY|COMMAND|AVE|AVENUE|STREET|ROAD|SUITE|BLVD)\b|[®©]|"
                      r"\b[A-Z]{2}\s+\d{5}\b|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b|@|www\.|https?:", re.I)
_TB_DATE = re.compile(r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(?:\d{1,2},?\s+)?\d{4}\b|\bAS\s+(?:INDICATED|NOTED|SHOWN)\b|\bN\.?T\.?S\.?$|"
                      r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b(?:ISO|ARCH|ANSI)\s+[A-E]\d?\b", re.I)


_TB_ONE_WORD = re.compile(r"(?:COVER|INDEX|LEGENDS?|NOTES|ABBREVIATIONS|SCHEDULES?|DETAILS|SECTIONS|ELEVATIONS|DIAGRAMS?|"
                          r"SPECIFICATIONS|PLANS|SYMBOLS)", re.I)
_TB_OPEN_END = re.compile(r"(?:[&/,+-]|\bAND|\bOR|\bOF|\bWITH|\bFOR|\bTO|\bTHE|\bAS|\bIN|\bAT|\bBY|\bON)\s*$", re.I)


def _tb_ok(t: str, number: str | None, whole: bool = False) -> bool:
    toks = t.split()
    if (len(toks) >= 4 and sum(len(x) <= 2 for x in toks) >= 0.5 * len(toks)) or all(len(x) <= 2 for x in toks):
        return False                              # letter-spaced 'A BBR E V I A T I ON S', 'DA TE': no words left
    if whole and sum(any(c.isalnum() for c in w) for w in toks) > 7:
        return False                              # a title-block title is short; longer lines are legend / notes text
    if whole and len(toks) >= 6 and re.search(r"[A-Za-z]{4,}\.\s+[A-Za-z]{3,}", t):
        return False                              # two sentences of a legend ('… ASSEMBLY. VOLTAGE RATING')
    if whole and _NOT_TITLE.search(re.sub(r"\bSHEET\s+\d+(?:\s+OF\s+\d+)?\s*$", "", t, flags=re.I)):
        return False                              # a note line ('WHERE USED, IDENTIFY SWITCHED'); whole titles only:
                                                  # one line of a title may end on '&' ('Police Service &')
    letters = sum(c.isalpha() for c in t)
    if not (4 <= len(t) <= 70) or letters < 4 or letters < 0.6 * len(t.replace(" ", "")):
        return False
    if _SHEET_NO.match(t) or (number and t.replace(" ", "") == number.replace(" ", "")):
        return False
    return not (_TB_LABEL.search(t) or _TB_FIRM.search(t) or _TB_DATE.search(t))


def _vertical(l) -> bool:
    w, h = l.box[2] - l.box[0], l.box[3] - l.box[1]
    return len(l.text.strip()) >= 4 and h > 2 * w


def _next_line(cur, m, vert: bool) -> bool:
    """m is the next line of a title printed on several lines: same print size, right after cur (below it; for
    title-block text running up the sheet edge, to its right), overlapping it along the text."""
    if abs(m.size - cur.size) > 0.06 * cur.size or (len(m.text.strip()) >= 4 and _vertical(m) != vert):
        return False
    a, b = (0, 2) if not vert else (1, 3)        # along the text
    g0, g1 = (1, 3) if not vert else (0, 2)      # across the text (line to line)
    gap = m.box[g0] - cur.box[g1]
    ov = min(m.box[b], cur.box[b]) - max(m.box[a], cur.box[a])
    return 0 <= gap <= 0.9 * cur.size and ov >= 0.3 * min(m.box[b] - m.box[a], cur.box[b] - cur.box[a])


def _tb_candidates(tb, number: str | None) -> list[tuple[float, str]]:
    """Title-block lines that could be the sheet's drawing title: not the sheet number, a field label, the firm /
    address, a date or a paper size; mostly letters, 4..70 chars; a single word only when it names a sheet type
    ('COVER', 'LEGEND'). A title printed on two lines ('Police Service &' / 'Security Forces') is joined when the
    next line has the same print size, sits right below and overlaps it. Largest print first."""
    ok = [l for l in tb if _tb_ok(re.sub(r"\s+", " ", l.text).strip(), number)]
    # continuation lines may be short ('S5' after 'Installation Security/'), never a label, number or firm line
    cont = [l for l in tb if l in ok or (len(l.text.strip()) <= 6 and not l.text.strip().endswith(":")
                                         and not (number and l.text.replace(" ", "") == number.replace(" ", ""))
                                         and not _TB_DATE.search(l.text))]
    sizes = sorted(m.size for m in tb)
    # a 'title block' zone holding a whole table (AW-28's abbreviations, 335 lines) is not one: there only print
    # clearly larger than the zone's body text can be the title (some real title blocks print it SMALL: 6.2 vs 7.8)
    body = 0.0
    if len(tb) > 120:
        top = max((m.size for m in tb if not (number and m.text.replace(" ", "") == number.replace(" ", ""))), default=0)
        body = max(1.3 * sizes[len(sizes) // 2], 0.97 * top)
    used, out = set(), []
    for l in sorted(ok, key=lambda l: (l.box[0], l.box[1]) if _vertical(l) else (l.box[1], l.box[0])):
        if id(l) in used:
            continue
        parts, cur = [l], l
        vert = _vertical(l)
        for _ in range(2):                        # up to 3 lines
            nxt = [m for m in cont if id(m) not in used and m is not cur and m not in parts and _next_line(cur, m, vert)]
            if not nxt:
                break
            cur = min(nxt, key=lambda m: m.box[0] if vert else m.box[1])
            parts.append(cur)
        for m in parts:
            used.add(id(m))
        t = re.sub(r"\s+", " ", " ".join(m.text.strip() for m in parts)).strip()
        if len(parts) > 1 and (len(t) > 90 or not _tb_ok(t, number, whole=True)):
            t = re.sub(r"\s+", " ", l.text).strip()
        if not _tb_ok(t, number, whole=True) or l.size < body:
            continue
        if len(t.split()) == 1 and not _TB_ONE_WORD.fullmatch(t):
            continue
        if _TB_OPEN_END.search(t):
            continue                              # a cut title ('Investigation/') is worse than none
        out.append((round(l.size, 1), t, tuple(l.box)))
    return sorted(out, key=lambda x: -x[0])


_TITLE_LABEL = re.compile(r"\b(?:DRAWING|SHEET)\s+(?:TITLE|NAME)\b|^\s*TITLE\s*:?\s*$", re.I)


def _anchored_title(tb, cands) -> str | None:
    """A title block that labels its title field ('Drawing Title:', 'SHEET TITLE'): the largest candidate printed
    just below / right of the label (SFO 'Police Service & Installation Security/ S5', not the project name)."""
    labels = [l for l in tb if _TITLE_LABEL.search(l.text) and len(l.text) <= 24]
    best = None
    for a in labels:
        z = max(a.size, 4.0)
        for size, t, b in cands:
            below = a.box[1] - 0.5 * z <= b[1] <= a.box[3] + 8 * z and b[0] >= a.box[0] - 3 * z and b[0] <= a.box[2] + 30 * z
            right = abs(b[1] - a.box[1]) <= 1.5 * z and 0 <= b[0] - a.box[2] <= 20 * z
            if (below or right) and (best is None or size > best[0]):
                best = (size, t)
    return best[1] if best else None


def _tb_title(sh: "Sheet", common: set | None = None) -> str | None:
    """The sheet's own line in its title block (USACE 'FLOW DIAGRAM', 'STRUCTURAL GENERAL NOTES'): the largest-print
    candidate that is not repeated across the set (`common` = texts on many sheets of the same file: project name,
    firm). On a single sheet only a line printed clearly larger than the title block's body text."""
    if sh.tb_anchored and not (common and sh.tb_anchored.upper() in common):
        return sh.tb_anchored
    c = [x for x in sh.tb_lines if not common or x[1].upper() not in common]
    if not c:
        return None
    if common is None:
        body = sh.tb_body
        if c[0][0] < 1.15 * body or (len(c) > 1 and c[1][0] >= 0.97 * c[0][0]):
            return None                     # not singled out by print size: could be the project name
    return c[0][1]


_COVER_TITLE = re.compile(r"(?:COVER|COVER\s+(?:PAGE|SHEET)|INDEX|TITLE\s+SHEET|DRAWING\s+INDEX|SHEET\s+INDEX|"
                          r"INDEX\s+OF\s+DRAWINGS|(?:LIST\s+OF\s+)?DRAWINGS?\s+LIST|LIST\s+OF\s+DRAWINGS)", re.I)


def _mark_index_sheets(sh: list) -> None:
    """In a set, a page that lists the sheet numbers of most other sheets is the cover / drawing index, whatever its
    index rows look like ('A-1  FIRST FLOOR PLAN T-SHAPE' read as a plan title): kind 'cover', no storey."""
    nums = {x.number for x in sh if x.number}
    if len(nums) < 4:
        return
    for x in sh:
        others = (x.numbers_seen & nums) - {x.number}
        if len(others) >= 3 and len(others) >= 0.5 * (len(nums) - 1):
            x.kind, x.storey, x.storey_name, x.partial = "cover", None, None, False
            c = [z[1] for z in x.tb_lines if _COVER_TITLE.fullmatch(z[1].strip())]
            if c and (not x.title or not _COVER_TITLE.fullmatch(x.title.strip())):
                x.title, x.title_source = c[0], "title_block_line"
            elif not x.title or view_kind(x.title) in PLAN_KINDS + ("rcp",):
                x.title, x.title_source = "DRAWING INDEX", "index"


def _apply_tb_title(sh: "Sheet", common: set | None = None, prefer: bool = False) -> None:
    """Fill an empty title from the title block's own line. In a set (`prefer`, `common` known) that line also
    replaces a title read off the views ('PLAN' -> 'DORMITORY MODULE'; a cover whose drawing index was read as plan
    titles -> 'COVER SHEET', kind cover); then a storey / partial plan it names counts too
    ('FIRST FLOOR COMMON AREA PLAN')."""
    t = _tb_title(sh, common)
    if not t or (sh.title and t.upper() == sh.title.upper()):
        return
    if sh.title:
        cur = sh.title.upper()
        longer = cur in t.upper() and len(t) > len(cur)      # 'FIRST FLOOR PLAN' -> 'FIRST FLOOR PLAN T-SHAPE'
        generic = not subject_of(sh.title).strip()            # 'PLAN', 'FLOOR PLAN', 'SECTION'
        if not (longer or (prefer and (sh.title_source == "views" or generic))):
            return
    sh.title, sh.title_source = t, "title_block_line"
    if _COVER_TITLE.fullmatch(t.strip()):
        sh.kind, sh.storey, sh.storey_name, sh.partial = "cover", None, None, False
    elif sh.kind == "unknown":
        k = view_kind(t)
        if k and k != "floor_plan":
            sh.kind = k
    if sh.kind in ("floor_plan", "drawing", "rcp") and sh.storey is None:
        k, name = _storey_of(t)
        if k is not None:
            sh.storey, sh.storey_name = k, name
    if sh.kind != "cover":
        sh.partial = sh.partial or bool(_PARTIAL.search(t))
    sh.confidence = round(min(sh.confidence + 0.1, 0.95), 2)


def _labelled_not_number(l, zone) -> bool:
    """True when the nearest small label above or below `l` (overlapping it across) names a size / revision / date /
    scale box ('SIZE' over 'A2', a revision row 'C01' over 'REVISION DATE') and no number label is next to it."""
    x0, y0, x1, y1 = l.box
    h = max(y1 - y0, 1.0)
    near = []
    for m in zone:
        if m is l or not (m.box[0] < x1 + h and m.box[2] > x0 - h):
            continue
        gap = (y0 - m.box[3]) if m.box[3] <= y0 + 0.3 * h else (m.box[1] - y1)
        label = _NO_LABEL.search(m.text) or (len(m.text.split()) <= 3 and not re.search(r"\d", m.text))
        if -0.3 * h <= gap <= 2.5 * h and m.size <= l.size * 1.05 and label:
            near.append((gap, m))      # labels only: short, no digits ('SIZE', 'REVISION  DATE'), or a NUMBER label
    for _g, m in sorted(near, key=lambda x: x[0])[:2]:
        if _NO_LABEL.search(m.text):
            return False
        if _NOT_NO_LABEL.search(m.text):
            return True
    return False


def classify(path: str, page: int = 0, layer=None, _set_mode: bool = False) -> Sheet:
    """One page / sheet -> Sheet (kind, storey, number, title, views)."""
    sh = Sheet(file=str(path), page=int(page))
    try:
        r = page_reading(path, page, layer)
        vs = views(path, page, layer=layer)
    except Exception:  # noqa: BLE001
        return sh
    if r is None:
        return sh
    lines, _ink, _s, W, H, _to_src, _to_disp = r
    sh.views = vs
    tb = _title_block_lines(lines, W, H)
    # sheet number: the largest 'A-101' style line in the title block (else anywhere near the bottom-right corner)
    zone = tb or [l for l in lines if l.box[0] > 0.8 * W and l.box[1] > 0.8 * H]
    iso = [l for l in zone if _ISO_NO.match(l.text) and not _TB_DATE.search(l.text)]
    nums = [l for l in zone if _SHEET_NO.match(l.text) and not _labelled_not_number(l, zone)]
    if iso:      # 'SEN-DFE-XX-00-D-X-0201' under 'PROJECT - ORIGINATOR - … - NUMBER' (not 'A2' under SIZE)
        sh.number = _ISO_NO.match(max(iso, key=lambda l: (l.size, len(l.text))).text).group(1)
    elif nums:
        sh.number = _SHEET_NO.match(max(nums, key=lambda l: l.size).text).group(1)
    # (a KEY PLAN inset often sits in the title block: it never names the sheet)
    titled = [l for l in tb if view_kind(l.text) in tuple(k for k in DRAWING_KINDS if k != "key_plan") + ("schedule",)
              and len(l.text.split()) <= 10]
    if titled:
        sh.title, sh.title_source = max(titled, key=lambda l: l.size).text, "title_block"
    sh.tb_lines = _tb_candidates(tb, sh.number)
    sh.tb_anchored = _anchored_title(tb, sh.tb_lines)
    tbs = sorted(l.size for l in tb)
    sh.tb_body = tbs[len(tbs) // 2] if tbs else 0.0
    sh.numbers_seen = {_SHEET_NO.match(l.text).group(1) for l in lines if _SHEET_NO.match(l.text)}
    drawings = [v for v in vs if v.kind in DRAWING_KINDS]
    plans = [v for v in drawings if v.kind == "floor_plan"]
    if not sh.title and plans:
        sh.title = " | ".join(v.title for v in plans[:4])       # a multi-view sheet: its plan views name it
        sh.title_source = "views"
    elif not sh.title and drawings:
        main = max(drawings, key=lambda v: _area(v.display_bbox))
        sh.title = main.title if main.kind != "drawing" else None
        sh.title_source = "views" if sh.title else None
    text_all = " ".join(l.text for l in lines[:4000])
    if _COVER.search(text_all) and not [v for v in drawings if v.kind != "drawing"]:
        sh.kind = "cover"
    elif plans:                                                   # 'FLOOR PLAN & RCP', module plan + RCP + axon
        sh.kind = "floor_plan"
    elif sh.title and view_kind(sh.title):
        sh.kind = view_kind(sh.title)
    elif drawings:
        main = max(drawings, key=lambda v: _area(v.display_bbox))
        sh.kind = main.kind
    elif any(v.kind == "schedule" for v in vs):
        sh.kind = "schedule"
    elif any(v.kind == "notes" for v in vs):
        sh.kind = "notes"
    if sh.kind == "drawing":
        sh.kind = "floor_plan" if re.search(r"\bPLAN\b", sh.title or "", re.I) else "drawing"
    # storey: the sheet title first, then the plan views' titles
    for t in [sh.title or ""] + [v.title for v in drawings if v.kind in PLAN_KINDS + ("rcp",)]:
        k, name = _storey_of(t)
        if k is not None:
            sh.storey, sh.storey_name = k, name
            break
    b = _keys(" ".join([sh.title or ""] + [v.title for v in drawings])).get("building")
    if b:
        sh.building = b
    sh.partial = bool(_PARTIAL.search(sh.title or ""))
    rs = [v.ratio for v in drawings if v.ratio]
    prs = [v.page_ratio for v in drawings if v.page_ratio]
    sh.ratio = max(set(rs), key=rs.count) if rs else None
    sh.page_ratio = max(set(prs), key=prs.count) if prs else None
    sh.confidence = round(0.4 + 0.2 * bool(sh.number) + 0.2 * bool(sh.title) + 0.15 * (sh.kind != "unknown"), 2)
    if not _set_mode:            # a set fills these in sheet_index, once it knows which lines repeat on every sheet
        _apply_tb_title(sh)
    return sh


def _area(b) -> float:
    return (b[2] - b[0]) * (b[3] - b[1]) if b else 0.0


def sheet_index(paths, layers: dict | None = None, max_pages: int = 300) -> list[Sheet]:
    """Every PDF page / image of the given files -> [Sheet]. `layers` = {image path: TextLayer} for images.

    Give it ALL the files of one project in one call: the sheets are read as a set (title-block lines printed on
    many of them = project name / firm, not a title; a page listing most other sheet numbers = the drawing index),
    across files too (three single-page PDFs of one set share their title block). Per page, classify() alone."""
    import pymupdf

    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    out: list[Sheet] = []
    for p in paths:
        p = str(p)
        if p.lower().endswith(".pdf"):
            try:
                with pymupdf.open(p) as d:
                    n = min(len(d), max_pages)
            except Exception:  # noqa: BLE001
                continue
            out.extend(classify(p, i, _set_mode=True) for i in range(n))
        else:
            out.append(classify(p, 0, layer=(layers or {}).get(p), _set_mode=True))
    common = None
    if len(out) >= 3:            # title-block lines on many sheets = project name, firm, phase ...
        cnt: dict = {}
        for x in out:
            for t in {z[1].upper() for z in x.tb_lines}:
                cnt[t] = cnt.get(t, 0) + 1
        common = {t for t, c in cnt.items() if c >= max(2, 0.3 * len(out))}
    for x in out:
        _apply_tb_title(x, common, prefer=common is not None)
    _mark_index_sheets(out)
    return out


def storeys(sheets: list[Sheet]) -> list[dict]:
    """The project's storey table from its floor-plan sheets: [{storey, name, sheets: [label], building}], lowest first."""
    def order(k):
        if k == "roof":
            return 1000
        if k == "mezzanine":
            return 0.5
        try:
            return float(k)
        except (TypeError, ValueError):
            return 999
    rows: dict = {}
    for s in sheets:
        if s.kind not in ("floor_plan", "drawing") or s.storey is None:
            continue
        key = (s.building, s.storey)
        r = rows.setdefault(key, {"storey": s.storey, "name": s.storey_name, "building": s.building, "sheets": []})
        r["sheets"].append(s.label)
    return sorted(rows.values(), key=lambda r: (r["building"] or "", order(r["storey"])))


def _as_view(s: Sheet) -> View:
    title = s.title or " ".join(v.title for v in s.views if v.kind in DRAWING_KINDS)[:200] or s.label
    if s.title and s.title_source == "title_block_line":   # the sheet's own title + its views' (module names, storeys)
        extra = [v.title for v in s.views if v.kind in PLAN_KINDS + ("rcp",) and v.title.upper() not in title.upper()]
        if extra:
            title = (title + " | " + " | ".join(extra[:4]))[:240]
    if s.storey_name and s.storey_name.lower() not in title.lower():
        title = f"{title} {s.storey_name}"
    kind = s.kind if s.kind in DRAWING_KINDS else "drawing" if s.kind == "unknown" else s.kind
    if s.kind in ("unknown", "drawing", "notes", "floor_plan"):
        role = _role(s)          # a legend / notes / diagram / details sheet is never a plan to count or sum
        if role in ("notes / legend", "diagram", "rendering", "cover / index"):
            kind = "notes"
        elif role == "details":
            kind = "detail"
    return View(kind=kind, title=title, bbox=(0, 0, 1, 1), title_bbox=(0, 0, 1, 1), subject=subject_of(title))


_CHOSEN_SHEET = re.compile(r"\(\s*sheet\s*:\s*([^()]{1,160})\)", re.I)


def pick_sheets(question: str, sheets: list[Sheet]) -> tuple[list[Sheet], Pick]:
    """The sheets a question means: its kind (ceiling -> RCP sheets, …, else floor plans), its storey / building /
    module; nothing named = every floor-plan sheet, one per storey (whole building), whole-storey plans preferred
    over enlarged / partial ones. A re-ask from sheet_choices ('… (sheet: A-2 SECOND FLOOR PLAN)') picks that one."""
    m = _CHOSEN_SHEET.search(question)
    if m:
        want = re.sub(r"[^A-Z0-9]+", " ", m.group(1).upper()).strip()
        hit = [s for s in sheets if re.sub(r"[^A-Z0-9]+", " ", s.label.upper()).strip() == want]
        if hit:
            return hit[:1], Pick([_as_view(hit[0])], f"the sheet the user chose: {hit[0].label}", 0.95)
        question = question[:m.start()] + question[m.end():]
    pv = [_as_view(s) for s in sheets]
    p = pick_views(question, pv)
    chosen = [sheets[i] for i, v in enumerate(pv) if any(v is c for c in p.views)]
    # a storey the set does not have ('fifth floor' on a 3-storey set): storey-less sheets do not 'match' it
    f = _keys(question).get("floor")
    known = {s.storey for s in sheets if s.storey and s.kind in ("floor_plan", "drawing")}
    if f and f not in ("0", "99") and known and f not in known and not any(s.storey == f for s in chosen):
        order = sorted(known, key=lambda k: (not k.lstrip("-").isdigit(), int(k) if k.lstrip("-").isdigit() else 0, k))
        return [], Pick([], f"the set has no plan of storey {f} (its storeys: {', '.join(order)})", 0.2, p.asked_kind)
    # whole storeys: drop partial plans of a storey that also has a whole-storey plan
    whole = {(s.building, s.storey) for s in chosen if not s.partial}
    chosen = [s for s in chosen if not (s.partial and (s.building, s.storey) in whole)]
    # whole building, storey plans known: a plan that names no storey at a much larger scale is an enlargement
    # (DAF dorm 'DORMITORY MODULE' 1:96 next to the storey plans at 1:480): summing it would double count
    storeyed = [s for s in chosen if s.storey is not None and not s.partial]
    rs = sorted(s.page_ratio or s.ratio for s in storeyed if (s.page_ratio or s.ratio))
    if rs and not f:
        med = rs[len(rs) // 2]
        enl = [s for s in chosen if s.storey is None and (s.page_ratio or s.ratio) and (s.page_ratio or s.ratio) < 0.75 * med]
        if enl:
            chosen = [s for s in chosen if s not in enl]
            p.reason += "; left out (no storey, drawn at a larger scale than the storey plans: enlargements): " + \
                ", ".join(s.label for s in enl)
    per: dict = {}
    for s in chosen:
        if s.storey is not None and not s.partial:
            per.setdefault((s.building, s.storey), []).append(s.label)
    dup = {k: v for k, v in per.items() if len(v) > 1}
    if dup:
        p.reason += "; several plans of the same storey (variants or overview + plan, count each storey once): " + \
            "; ".join(f"storey {k[1]}: {', '.join(v)}" for k, v in dup.items())
        p.confidence = min(p.confidence, 0.5)
    return chosen, p


def sheet_choices(question: str, sheets: list[Sheet], max_options: int = 6) -> dict | None:
    """Interactive clarification for a drawing SET (captain 18:50, W5's Answer.clarify shape): several plans of the
    asked storey (variants / overview + plan), a storey read under an assumption ('ground floor' -> FIRST FLOOR, US
    numbering), or a storey / part no sheet has -> {ask, why, answered_for, options: [{label, question '(sheet: …)',
    value: None, unit: None, evidence: {file, page, bbox, bbox_space, note}}], click: None}; else None."""
    if _CHOSEN_SHEET.search(question):
        return None
    chosen, p = pick_sheets(question, sheets)
    plans = [s for s in sheets if s.kind in ("floor_plan", "drawing") and not s.partial]
    one_storey = bool(_keys(question).get("floor")) and not _WHOLE.search(question)
    if "several plans of the same storey" in p.reason and one_storey:
        why = "the set has several plans of that storey"
        if "read as" in p.reason:
            why += "; " + p.reason.split("(", 1)[-1].split(")", 1)[0]
        opts = chosen
    elif "read as" in p.reason and chosen:
        why = p.reason.split("(", 1)[-1].rstrip(")") if "(" in p.reason else p.reason
        opts = chosen + [s for s in plans if s not in chosen]
    elif not chosen and plans:
        why = p.reason if "has no plan of storey" in p.reason else \
            "no sheet is titled like the storey / part the question names"
        per: dict = {}
        for s in plans:  # one option per storey (its first whole plan), then the storey-less plans
            per.setdefault(s.storey or f"?{s.label}", s)
        opts = list(per.values())
    else:
        return None
    if len(opts) < 2 and not (opts and "read as" in p.reason):
        return None
    base = question.strip().rstrip("?").strip()
    out = []
    for s in opts[:max_options]:
        main = [v for v in s.views if v.kind in DRAWING_KINDS and v.bbox]
        box = None
        if main:
            box = [round(min(v.bbox[0] for v in main), 2), round(min(v.bbox[1] for v in main), 2),
                   round(max(v.bbox[2] for v in main), 2), round(max(v.bbox[3] for v in main), 2)]
        label = s.label + (f" ({s.storey_name})" if s.storey_name and s.storey_name.lower() not in s.label.lower() else "")
        out.append({"label": label, "value": None, "unit": None, "kind": s.kind, "question": f"{base} (sheet: {s.label})?",
                    "evidence": {"file": s.file, "page": s.page, "bbox": box,
                                 "bbox_space": "pdf_pt" if str(s.file).lower().endswith(".pdf") else "image_px",
                                 "note": f"sheet: {label}"}})
    return {"ask": "Which sheet do you mean?", "why": why, "answered_for": [s.label for s in chosen], "options": out,
            "click": None}


# ---------------------------------------------------------------- the uploaded PROJECT in one call (T3 / W5)

ROLES = {"floor_plan": "floor plan", "rcp": "reflected ceiling plan", "section": "section", "elevation": "elevation",
         "schedule": "schedule", "site_plan": "site plan", "roof_plan": "roof plan", "foundation_plan": "foundation plan",
         "services_plan": "services plan", "detail": "details", "axon": "axonometric", "key_plan": "key plan",
         "notes": "notes / legend", "cover": "cover / index", "drawing": "drawing", "unknown": "unknown"}
_ROLE_WORDS = [(re.compile(r"\bLEGEND|\bABBREVIATIONS|\bGENERAL\s+NOTES|\bNOTES\b|\bSYMBOLS\b|PROJECT\s+INFORMATION", re.I),
                "notes / legend"),
               (re.compile(r"\bDIAGRAMS?\b|\bONE[- ]LINE\b|\bWIRING\b|\bRISER\b|\bSCHEMATIC|\bI/O\b|\bSEQUENCE\s+OF", re.I),
                "diagram"),
               (re.compile(r"\bDETAILS?\b", re.I), "details"),
               (re.compile(r"\bRENDERING|\bPERSPECTIVE|\b3D\s+VIEW", re.I), "rendering")]


def _cached_layer(img: str):
    """An image's text layer when some reader already read it (G1's area_tags test: never a fresh OCR), else None."""
    try:
        from stoa.solvers.raster import textlayer as T

        tag = T.file_tag(img)
        cached = any(k.startswith(f"{img}{tag}|") for k in list(T._mem)) or T._disk_key(img, 1100, "local").exists()
        if not cached:
            from stoa import ocr

            cached = ocr.cached(img)
        return T.text_layer(img) if cached else None
    except Exception:  # noqa: BLE001
        return None


def _scan_page_sheet(path: str, page: int):
    """A scanned PDF page classified from the upload warm-up's cached reading (P1's stoa.formats.pages.classify_scan_page:
    never a cold OCR); None when that helper is absent, the page has text of its own, or nothing was read yet."""
    try:
        import pymupdf

        with pymupdf.open(path) as d:
            if len(d[int(page)].get_text().strip()) >= 20:
                return None
        from stoa.formats import pages as PG

        fn = getattr(PG, "classify_scan_page", None)
        sh = fn(path, int(page)) if fn else None
        return sh if sh is not None and (sh.kind != "unknown" or sh.storey is not None) else None
    except Exception:  # noqa: BLE001
        return None


def _role(s: Sheet) -> str:
    """What the sheet is for a person: its kind, unless its title (with no PLAN word) says legend / notes / diagram /
    details / rendering ('STEEL TANK DETAILS SHEET 4' read as a floor plan from its views)."""
    t = s.title or ""
    if s.kind in ("unknown", "drawing", "floor_plan", "notes") and t and not re.search(r"\bPLANS?\b", t, re.I):
        for rx, role in _ROLE_WORDS:
            if rx.search(t):
                return role
    return ROLES.get(s.kind, s.kind)


def project(paths, read_text: bool = False, max_pages: int = 120, layers: dict | None = None) -> dict:
    """Every sheet of the uploaded files as ONE project, JSON-ready (T3's project loop, W5's sheet browser):

        {"sheets": [{"id": "<file>#<page>", "file", "page", "number", "title", "label", "role", "kind", "storey",
                     "storey_name", "level", "building", "partial", "scale", "for_totals", "confidence", "text",
                     "views": [{"kind", "title", "bbox", "bbox_space"}]}],
         "storeys": [{"storey", "name", "building", "sheets": [id]}],      # lowest first, roof last
         "buildings": [...], "totals": {"sheets": [id], "why": str},      # what a whole-building count / area sums
         "notes": [str]}                                                   # duplicates, unread images, ...

    PDFs: text layer, ~0.1 s per page ($0). Images: the cached text layer (or `layers[path]`); with read_text=True a
    missing one is read (the VLM / OCR text layer: tens of seconds per image). 'for_totals' marks the sheets a
    whole-building question sums: one whole-storey floor plan per (building, storey) — never RCPs, sections,
    elevations, details, partial / enlarged plans or a second plan of the same storey."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    paths = [str(p) for p in paths]
    layers = dict(layers or {})
    notes: list[str] = []
    for p in paths:
        if p.lower().endswith(".pdf") or p in layers:
            continue
        lay = _cached_layer(os.path.abspath(p))
        if lay is None and read_text:
            try:
                from stoa.solvers.raster import textlayer as T

                lay = T.text_layer(os.path.abspath(p))
            except Exception as e:  # noqa: BLE001
                notes.append(f"{os.path.basename(p)}: text layer failed ({type(e).__name__})")
        if lay is not None:
            layers[p] = lay
        else:
            notes.append(f"{os.path.basename(p)}: image not read yet (no text layer): role / level unknown")
    idx = sheet_index(paths, layers=layers, max_pages=max_pages)
    for p in paths:
        if p.lower().endswith(".pdf"):
            try:
                import pymupdf

                with pymupdf.open(p) as d:
                    if len(d) > max_pages:
                        notes.append(f"{os.path.basename(p)}: only the first {max_pages} of {len(d)} pages indexed")
            except Exception:  # noqa: BLE001
                notes.append(f"{os.path.basename(p)}: could not be opened as a PDF")
    unread = []
    for i, s in enumerate(idx):          # a SCANNED PDF page (no text of its own): what the upload warm-up read
        if s.file.lower().endswith(".pdf") and s.kind == "unknown" and not s.title:
            got = _scan_page_sheet(s.file, s.page)
            if got is not None:
                got.file, got.page = s.file, s.page
                idx[i] = got
            else:
                unread.append(s)
    sid = {id(s): f"{s.file}#{s.page}" for s in idx}
    tot, pick = pick_sheets("What is the total floor area of the whole building?", idx)
    tot = [s for s in tot if not any(s is u for u in unread)]
    for u in unread:
        notes.append(f"{os.path.basename(u.file)} page {u.page + 1}: no text read yet (a scan?): role / level unknown, "
                     f"left out of the totals until it is read")
    tot_ids = {id(s) for s in tot}
    # a second whole plan of the same storey: keep the first in the totals, list the rest
    seen: dict = {}
    for s in tot:
        if s.storey is not None and not s.partial:
            k = (s.building, s.storey)
            if k in seen:
                tot_ids.discard(id(s))
                notes.append(f"storey {s.storey_name or s.storey}: {s.label} and {seen[k].label} both plan it: "
                             f"totals use {seen[k].label} only (check which one is current)")
            else:
                seen[k] = s
    if not tot:
        notes.append("no floor-plan sheet found: no whole-building total (ask per sheet)")
    elif any(s.storey is None for s in tot if id(s) in tot_ids) and len(tot_ids) > 1:
        notes.append("some floor plans name no storey: a whole-building total may double count (show per-sheet values)")
    rows = []
    for s in idx:
        img = not s.file.lower().endswith(".pdf")
        rows.append({
            "id": sid[id(s)], "file": s.file, "page": s.page, "number": s.number, "title": s.title, "label": s.label,
            "role": _role(s), "kind": s.kind, "storey": s.storey, "storey_name": s.storey_name,
            "level": s.storey_name or (f"storey {s.storey}" if s.storey is not None else None),
            "building": s.building, "partial": s.partial,
            "scale": f"1:{s.page_ratio or s.ratio:g}" if (s.page_ratio or s.ratio) else None,
            "for_totals": id(s) in tot_ids, "confidence": s.confidence, "title_source": s.title_source,
            "text": ("image_layer" if s.file in layers else "none") if img else "pdf",
            "views": [{"kind": v.kind, "title": v.title, "bbox": [round(x, 1) for x in v.bbox] if v.bbox else None,
                       "bbox_space": "image_px" if img else "pdf_pt"} for v in s.views],
        })
    st = storeys(idx)
    lab2id = {s.label: sid[id(s)] for s in idx}
    for r in st:
        r["sheets"] = [lab2id.get(l, l) for l in r["sheets"]]
    return {"sheets": rows, "storeys": st, "buildings": sorted({s.building for s in idx if s.building}),
            "totals": {"sheets": [sid[id(s)] for s in idx if id(s) in tot_ids], "why": pick.reason},
            "notes": notes}


if __name__ == "__main__":   # python -m stoa.sheet.sets a.pdf b.png [--read-text] -> project JSON on stdout
    import contextlib
    import json
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with contextlib.redirect_stdout(sys.stderr):
        out = project(args, read_text="--read-text" in sys.argv)
    print(json.dumps(out, indent=1, default=str))
