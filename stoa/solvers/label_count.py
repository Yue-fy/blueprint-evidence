"""`label_count`: 'How many bedrooms / WCs / meeting rooms …?' on a vector PDF = the printed room LABELS of that type (G1).

X1's wild exam (14:22, real house plans outside the dataset, uploaded alone): the sheets print 'BED ROOM 1', 'BED ROOM 2',
'bedroom3', 'WC', 'WC' in their text layer but have no printed scale and no room fills, so pdf_vector ('no printed
scale'), plan_rooms ('no filled polygons') and pdf_text ('no tables') abstained and the prior answered '1' (6 of 6
wrong). A count needs no scale: each label of the asked room type is one room, and its box is the evidence.

Rules (all deterministic, $0):
- the asked type = the noun phrase after 'how many' (plus the words in brackets: 'toilets (WC)'), singular, with a small
  synonym table (bed room / bedroom / bedrm, WC / W.C. / toilet / lavatory, bath / bathroom, …);
- a label counts when its text, without spaces and punctuation, IS the type plus at most a short index ('BED ROOM 2',
  'bedroom3', 'WC', 'Meeting Room 4.02'); longer lines (notes, legends, titles) never count;
- abstains: no label of the type (absence of a label is not absence of the room), dense sheets (> 400 text lines:
  schedules and legends repeat names), IfcConvert-style sheets (plan_rooms counts their outlines), questions scoped to
  an apartment / unit / storey other than the sheet itself (labels carry no membership).
Confidence 0.7 (a room may be unlabelled or labelled by a number only); related labels that were NOT counted
('ensuite' for bathrooms, 'master' for bedrooms) are named in the assumptions.
"""

from __future__ import annotations

import functools
import re

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

# canonical type -> label stems (folded: lowercase, no spaces / punctuation)
_SYN = {
    "bedroom": ("bedroom", "bedrm", "bed", "masterbedroom", "masterbed", "schlafzimmer", "kinderzimmer", "makuuhuone",
                "sovrum", "slaapkamer", "chambre"),
    "bathroom": ("bathroom", "bath", "badezimmer", "bad", "kylpyhuone", "badrum", "badkamer", "salledebain"),
    "toilet": ("toilet", "toilets", "wc", "lavatory", "powderroom", "powder", "restroom", "wctoilet", "toilette",
               "vessa"),
    "kitchen": ("kitchen", "kuche", "keittio", "kok", "keuken", "cuisine", "cucina", "kitchenette"),
    "living room": ("livingroom", "living", "lounge", "wohnzimmer", "wohnen", "olohuone", "vardagsrum", "woonkamer",
                    "sejour", "salon"),
    "dining room": ("diningroom", "dining", "essen", "esszimmer", "ruokailu"),
    "garage": ("garage", "carport", "carpark"),
    "balcony": ("balcony", "balkon", "parveke", "balkong", "balcon"),
    "terrace": ("terrace", "terrasse", "terassi", "patio"),
    "office": ("office", "buro", "toimisto", "kontor", "ufficio", "bureau"),
    "meeting room": ("meetingroom", "meeting", "besprechung", "besprechungsraum", "neuvotteluhuone"),
    "conference room": ("conferenceroom", "conference", "boardroom"),
    "storage": ("storage", "store", "storeroom", "abstellraum", "lager", "varasto", "forrad"),
    "laundry": ("laundry", "waschkuche", "pesuhuone", "tvattstuga"),
    "study": ("study", "den"),
    "stair": ("stair", "stairs", "staircase", "treppe", "treppenhaus", "porras", "porrashuone"),
}
_RELATED = {"bathroom": ("ensuite", "ensuit", "shower", "dusche"), "bedroom": ("dressingroom", "guest", "nursery"),
            "toilet": ("bathroom", "bath", "ensuite"), "meeting room": ("conference", "boardroom"),
            "conference room": ("meetingroom", "meeting")}
_PLURAL = re.compile(r"(?:'s|s|es)$")
_INDEX = re.compile(r"^(?:\d{1,3}[a-z]?|[a-z]|\d+\.\d+|[ivx]{1,4})?$")
_HOWMANY = re.compile(r"\bhow many\s+(.+?)(?=\s+(?:are|is|does|do|on|in|at|there|exist|can|have|has|does)\b|[?.,]|$)",
                      re.I)
_SCOPED = re.compile(r"\b(apartment|flat|unit|wohnung|asunto|lägenhet|appartement)s?\s+(?:[A-Z]\b|\d|no\.?)|"
                     r"\b(in|of) (?:the )?(?:whole|entire) (?:building|model|project)\b", re.I)
_FOLD = re.compile(r"[^a-z0-9]")
MAX_LINES = 400


def _fold(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.lower().replace("ß", "ss"))
    return _FOLD.sub("", "".join(c for c in s if not unicodedata.combining(c)))


def asked_types(question: str) -> list[str]:
    """Label stems for the room type a 'how many X' question asks about ([] when it names none we read)."""
    m = _HOWMANY.search(question)
    if not m:
        return []
    phrase = m.group(1)
    are_lab = re.match(r"\s+(?:are|is)\s+((?:labell?ed|named|called|marked|tagged)\s+(?:as\s+)?['\"‘’“”]?[^?.,'\"‘’“”]+?"
                       r"['\"‘’“”]?)(?=\s+(?:on|in|at|of|within|inside)\b|\s*[?.,]|\s*$)", question[m.end():], re.I)
    if are_lab and re.fullmatch(r"(?i)(?:rooms?|spaces?|areas?)", phrase.strip()):
        # X1 21:41: 'How many rooms are labelled Plant Room on this plan?' counted every '… Room' label (7; gold 3)
        phrase = f"{phrase} {re.sub(r'(?i)^(\w+)\s+as\s+', r'\1 ', are_lab.group(1))}"
    lab = re.search(r"\b(?:rooms?|spaces?|areas?)\s+(?:labell?ed|named|called|marked|tagged)\s+['\"‘’“”]?(.+?)['\"‘’“”]?\s*$",
                    phrase, re.I)
    if lab:  # D1 17:22: 'How many rooms labelled Infants' -> the label is the type ('Infants'), not 'roomsinfants'
        phrase = lab.group(1)
        f = _fold(phrase)
        if f and re.search(r"(?:labell?ed|named|called|marked|tagged)\s+['\"‘’“”]", m.group(1), re.I):
            return [f]  # a QUOTED label is counted as written ('Bad' is not every bathroom)
    parts = [phrase] + re.findall(r"\(([^)]{1,30})\)", phrase)
    stems: list[str] = []
    for p in parts:
        p = re.sub(r"\([^)]*\)", " ", p)
        p = re.sub(r"\b(the|separate|individual|different|of|all|labelled|labeled|named|drawn)\b", " ", p, flags=re.I)
        f = _fold(p)
        if not f:
            continue
        cands = {f, _PLURAL.sub("", f), f[:-1] if f.endswith("s") else f}
        if f.endswith("ies"):
            cands.add(f[:-3] + "y")
        hit = [k for k, v in _SYN.items() if cands & set(v) or _fold(k) in cands]
        for k in hit:
            stems += [s for s in _SYN[k] if s not in stems]
        if not hit:
            stems += [c for c in sorted(cands, key=len, reverse=True) if len(c) >= 3 and c not in stems]
    return stems


def _matches(label: str, stems: list[str]) -> str | None:
    f = _fold(label)
    if not f or len(label.split()) > 4:
        return None
    for s in sorted(stems, key=len, reverse=True):
        if f.startswith(s) and _INDEX.match(f[len(s):]):
            # 'bed' alone is a bedroom only with an index ('BED 2'), never the bare word (furniture)
            if s == "bed" and f == "bed":
                continue
            return s
    return None


# O1 22:28 (D1's real HAER scan dc1026): OCR read the box labels with slips — 'STRAIMER', 'DKHSTRAINER' (5 of 6),
# 'NOTOR' (5 of 5) — and the exact stem match kept 2 / 4: on OCR layers only, letters OCR confuses are folded together
# and a short glued prefix (<= 3 letters) is allowed before a long stem
_OCR_FOLD = str.maketrans({"m": "n", "l": "i", "1": "i", "|": "i", "!": "i", "0": "o", "5": "s", "8": "b", "$": "s"})


def _ocr_matches(label: str, stems: list[str]) -> str | None:
    raw = (label or "").lower()
    if not raw or len(raw.split()) > 4:
        return None
    f = re.sub(r"[^a-z0-9|!$]", "", raw).translate(_OCR_FOLD)
    for s in sorted(stems, key=len, reverse=True):
        if len(s) < 5:
            continue
        sf = s.translate(_OCR_FOLD)
        if f.startswith(sf) and _INDEX.match(f[len(sf):]):
            return s
        if len(s) >= 6:
            i = f.find(sf)
            if 1 <= i <= 3 and f[:i].isalpha() and _INDEX.match(f[i + len(sf):]):
                return s
    return None


_IMPERATIVE_COUNT = re.compile(r"^\s*(?:please\s+)?(?:(?:can|could) you\s+)?(?:count|give (?:me )?the (?:total )?number of|"
                               r"what(?:'s| is) the (?:total )?number of|state the number of|tell me the number of|"
                               # H 19:24: 'Number of X?' / 'Total number of X?' / 'Tell me how many X there are.'
                               r"(?:the\s+)?(?:total\s+)?number of|tell me how many)\s+(?:all\s+)?(?:of\s+)?(?:the\s+)?"
                               r"(?P<x>[^.?!]+?)(?:\s+there\s+are)?\s*[.?!]?\s*$", re.I)


def as_how_many(question: str) -> str:
    """H 18:54 (Stoa M-101A PNG): 'Count the filter separators.' -> 'How many filter separators?' (the readers parse the
    noun after 'how many'; 'How many filter separators are shown?' was 7 = gold, the imperative went to the agent: 1)."""
    if re.search(r"\bnumber of the [a-z]*[^s\W](?!\w)(?!\s+[a-z]+s\b)", question or "", re.I):
        return question  # 'What is the number of the room with the stair?' asks for a room NUMBER, not a count
    m = _IMPERATIVE_COUNT.match(question or "")
    if m:
        return f"How many {m.group('x')}?"
    m = _TERSE_COUNT.match(question or "")
    if m:  # X1 21:26 rp-065 'dorm room count' -> 'How many dorm rooms?'
        x = m.group("x").strip()
        return f"How many {x if re.search(r'(?i)s$', x) else x + 's'}{(' ' + m.group('rest').strip()) if m.group('rest') else ''}?"
    return question


# terse keyword questions (X1 21:26 rephrase probe): 'dorm room count', 'door count in module 02'
_TERSE_COUNT = re.compile(r"^\s*(?P<x>(?:[A-Za-z][\w'-]*\s+){0,3}?[A-Za-z][\w'-]*?)\s+count\b(?P<rest>\s+(?:in|on|of|at|for)\s+"
                          r"[^.?!]+)?\s*[.?!]?\s*$", re.I)


@functools.lru_cache(maxsize=16)
def text_lines(pdf_abs: str, mtime: float = 0.0) -> tuple:
    """((text, bbox pdf_pt), ...) of every text line on page 0 (words of one line joined by a space)."""
    import pymupdf

    with pymupdf.open(pdf_abs) as doc:
        if not len(doc):
            return ()
        blocks = doc[0].get_text("dict").get("blocks", [])
    out = []
    for b in blocks:
        for ln in b.get("lines", []):
            t = re.sub(r"\s+", " ", " ".join(s.get("text", "") for s in ln.get("spans", []))).strip()
            if t:
                out.append((t, tuple(round(x, 2) for x in ln["bbox"])))
    return tuple(out)


# printed stair notes: '21 Stg. 16.2/27.8' (DE), '16 R' / '16 RISERS' / 'UP 16R' (US), '12 marches' (FR), '18 alzate'
# (IT), '15 treden' (NL), '17 steg' (SV), '16 nousua' (FI); treads are counted separately ('15 T', '15 TREADS')
_STEPS_Q = re.compile(r"\bhow many\s+(?:\w+\s+){0,2}(steps|risers|treads|stairs)\b|\bwie viele\s+(?:\w+\s+)?(stufen|"
                      r"steigungen|tritte)\b|\bcombien de\s+(marches)\b|\bquant[ei]\s+(gradini|alzate)\b|"
                      r"\bhoeveel\s+(treden)\b|\bkuinka monta\s+(askelmaa|porrasta|nousua)\b", re.I)
_RISER_NOTE = re.compile(r"(?<![\w.,/])(\d{1,2})\s*(?:x\s*)?(?:Stg\b\.?|STG\b\.?|Stufen\b|STUFEN\b|Steigungen\b|"
                         r"STEIGUNGEN\b|STEIG\b\.?|R\b(?![-/'\.]\w)|RISERS?\b|[Rr]isers?\b|EQ\.? RISERS?\b|marches\b|"
                         r"alzate\b|gradini\b|treden\b|optreden\b|steg\b|nousua\b|askelmaa\b)")
_TREAD_NOTE = re.compile(r"(?<![\w.,/])(\d{1,2})\s*(?:T\b(?![-/'\.]\w)|TREADS?\b|[Tt]reads?\b|Auftritte\b|AUFTRITTE\b)")
_NOT_STAIR = re.compile(r"\b(?:room|rm|bed|bedroom|unit|flat|apt|apartment|zimmer|whg)\b", re.I)
_OUTSIDE_Q = re.compile(r"\b(porch|entrance|entry|front steps|outside|terrace|deck|veranda|eingang|außentreppe|"
                        r"aussentreppe|perron)\b", re.I)


def _stair_answer(lines, question: str, path: str, space: str) -> Answer | None:
    """'How many steps / risers does the staircase have?' = the printed stair note (Ziying 15:31: '21 Stg. 16.2/27.8' =
    21 risers of 16.2 cm, 27.8 cm treads). One distinct number -> 0.7; several stairs -> the asked one by wording
    (porch / entrance -> the smallest, else the largest) at 0.4 with the others named."""
    m = _STEPS_Q.search(question)
    if not m:
        return None
    word = next(g for g in m.groups() if g).lower()
    pat = _TREAD_NOTE if word in ("treads", "auftritte") else _RISER_NOTE
    hits = []
    for t, bb in lines:
        if len(t) > 60:
            continue
        if _NOT_STAIR.search(t):
            continue  # 'ROOM 2R', 'UNIT 3R': a room / unit name, not a stair note
        for mm in pat.finditer(t):
            n = int(mm.group(1))
            abbrev = len(mm.group(0).strip().split()[-1].strip(".")) <= 1 or mm.group(0).rstrip(".").endswith(("R", "T"))
            if (3 if abbrev else 2) <= n <= 40:
                hits.append((n, t, bb))
    if not hits:
        return None
    vals = sorted({n for n, _, _ in hits})
    if len(vals) == 1:
        n, conf, why = vals[0], 0.7, f"the stair note says {vals[0]}"
    else:
        n = vals[0] if _OUTSIDE_Q.search(question) else vals[-1]
        conf, why = 0.4, (f"{len(vals)} stair notes ({', '.join(map(str, vals))}): "
                          + ("the smallest for an outside / entrance stair" if n == vals[0] else
                             "the largest for the main stair"))
    used = [(t, bb) for k, t, bb in hits if k == n]
    ev = [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"stair note '{t}'")
          for t, bb in used[:6]]
    kind = "treads" if pat is _TREAD_NOTE else "risers (steps)"
    return Answer(value=n, unit="count", confidence=conf, evidence=ev, solver="label_count",
                  trace=[f"label_count: printed stair note(s): " + "; ".join(t for _, t, _ in hits[:6]) + f" -> {why}"],
                  assumptions=[f"the printed stair note gives the number of {kind} ('Stg.' / 'R' = Steigungen / risers)"]
                  + (["a flight's steps = its risers (the top step is the landing)"] if word == "steps" else []),
                  missing=[] if conf >= 0.7 else [f"which stair is meant (notes: {', '.join(map(str, vals))})"])


# component TAG families on piping / mechanical sheets (captain 16:15, Stoa's M-101A): 'FP-1'..'FP-5' = 5 fuel pumps
# '(FP)', '(FSI-…)', and dotted '(F.E.C.)' (X1 sl-329 A-101F: 5 'F.E.C.' labels; read as FEC)
_TAG_ABBR_Q = re.compile(r"\(((?:[A-Z]\.){2,5}|[A-Z]{1,5})(?:-[.…]*)?\)")
_TAG_WORDS = {"fuel pump": ("FP",), "fueling pump": ("FP",), "fuelling pump": ("FP",), "check valve": ("CV",),
              "filter separator": ("FSI", "FSR"), "pressure gauge": ("PG",), "pipe support": ("PS",),
              "insulating flange": ("IF",), "control valve": ("FSCV",), "pressure switch": ("PSW",),
              "air vent": ("MAV",), "manual air vent": ("MAV",), "flow switch": ("FS",),
              "pump": ("P", "FP")}  # H 17:26: 'How many pumps are there?' on M-101A = the 5 FP-n fueling pumps
_LEGEND = re.compile(r"([A-Z][A-Z /&-]{3,60}?)\s*\(([A-Z]{1,5})\)")
_STOPW = {"the", "a", "an", "of", "on", "in", "are", "is", "how", "many", "shown", "there", "this", "sheet", "plan",
          "piping", "tagged", "called", "out", "with", "total", "and", "different", "types", "type", "w"}


def _words(s: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in _STOPW}


def _tag_prefixes(question: str, lines) -> tuple[list[str], str]:
    # the tag family must name the COUNTED thing: 'Into how many panels do the control joints (CJ) divide the slab?'
    # counts panels, not CJ tags (stoa-like sl-017: 7 CJ tags @0.6, gold 6 panels)
    hm = _HOWMANY.search(question)
    if not hm:
        return [], ""
    phrase = question[hm.start(1):hm.end(1)]
    ab = [m.group(1).replace(".", "") for m in _TAG_ABBR_Q.finditer(question) if m.start() <= hm.end(1)]
    if ab:
        return ab, "abbreviation in the question"
    if _TAG_ABBR_Q.search(question):
        return [], ""
    question = phrase
    qw = _words(question)
    best, src = [], ""
    for t, _ in lines:  # the sheet's own legend: 'NON-SURGE CHECK VALVE (CV) W/ RATE OF FLOW CONTROLLER'
        for m in _LEGEND.finditer(t):
            lw = _words(m.group(1))
            if len(lw & qw) >= 2 and m.group(2) not in best:
                best.append(m.group(2))
                src = f"legend '{m.group(0)[:50]}'"
    if best:
        return best[:2], src
    ql = question.lower()
    for k in sorted(_TAG_WORDS, key=len, reverse=True):
        if re.search(r"\b" + k + r"(?:s|es)?\b", ql):
            return list(_TAG_WORDS[k]), f"'{k}' = tag {'/'.join(_TAG_WORDS[k])} (common piping legend)"
    acr = _acronym(question)
    if acr and len({m.group(1) for t, _ in lines for m in re.finditer(_acr_rx(acr), t)}) >= 2:
        # D1 22:52 (HAER hi76c2): 'How many slow sand filters?' -> label 'SLOW SAND FILTERS' (1) with leaders to the
        # tags SSF1 / SSF2 (= 2): the initials of a 2-4 word name, numbered, are that item's tag family
        return [acr], f"acronym of '{question.strip()[:40]}'"
    return [], ""


def _acronym(phrase: str) -> str | None:
    ws = [w for w in re.findall(r"[A-Za-z]+", phrase) if w.lower() not in _STOPW and len(w) >= 2]
    return "".join(w[0] for w in ws).upper() if 2 <= len(ws) <= 4 else None


def _acr_rx(acr: str) -> str:
    return r"(?<![A-Z0-9])" + acr + r"[- ]?(\d{1,3}[A-Z]?)\b"


# captain 23:07 / H 23:02 (expert Ziying, M-101A 'How many pumps are there?' = 7): besides the FP-1..FP-5 tag family the
# sheet draws a JOCKEY PUMP and a PUMPHOUSE DRAIN PUMP that carry only a TEXT CALLOUT (a leader, no tag). A callout
# counts when its label ENDS in the asked noun; the noun as a MODIFIER ('12" PUMP / DISCHARGE / HEADER' = a pipe, 'PUMP
# CONTROL PANEL' = a panel), pipe-run callouts ('2" … TO SUMP PUMP'), notes ('8. CONSIDER USING … PUMPS'), legend rows
# ('… PUMP (P)') and 'TYP.' callouts (one label for several items) never count; a callout next to a tag of the family
# names that tagged item and is not counted twice.
_CALLOUT_BAD = re.compile(r"^\s*(?:\d+(?:\s*[-/]\s*\d+)?\s*(?:\"|”|''|in\b|inch)|\d{1,2}[.)]\s|notes?\b)|\b(?:to|from|via|"
                          r"see|per|typ|typical|consider|provide|future|by others|nic)\b|\([A-Z]{1,5}\)", re.I)
_CALLOUT_TAIL = re.compile(r"^\s*(?:see\b|per\b|below\b|above\b|at\b|on\b|in\b|with\b|w/|under\b|over\b|beyond\b|-)",
                           re.I)
_PHRASE_DROP = re.compile(r"\b(?:the|all|any|distinct|separate|individual|different|drawn|shown|labell?ed|tagged|total|"
                          r"number|of)\b", re.I)


def _asked_noun(question: str) -> list[str]:
    """['fuel', 'pump'] for 'How many fuel pumps are there?' (folded words, last one singular)."""
    hm = _HOWMANY.search(question)
    if not hm:
        return []
    ws = [_fold(w) for w in _PHRASE_DROP.sub(" ", re.sub(r"\([^)]*\)", " ", hm.group(1))).split()]
    ws = [w for w in ws if w]
    if not ws or len(ws) > 3:
        return []
    return ws[:-1] + [_sing(ws[-1])]


def _sing(w: str) -> str:
    for suf, rep in (("ies", "y"), ("sses", "ss"), ("ches", "ch"), ("shes", "sh"), ("xes", "x"), ("s", "")):
        if w.endswith(suf) and len(w) > len(suf) + 2 and not (suf == "s" and w.endswith("ss")):
            return w[: -len(suf)] + rep
    return w


def _display(lines, abs_path: str | None, space: str) -> list[tuple]:
    """Each line's box as the sheet is DISPLAYED (a 90°-rotated PDF prints its text sideways in page space)."""
    boxes = [tuple(bb) for _, bb in lines]
    if space != "pdf_pt" or not abs_path:
        return boxes
    try:
        import pymupdf

        with pymupdf.open(abs_path) as doc:
            if not doc[0].rotation:
                return boxes
            m = doc[0].rotation_matrix
        long = [(bb[2] - bb[0], bb[3] - bb[1]) for (t, bb) in lines if len(t) >= 4]
        if not long or sum(h > w for w, h in long) <= len(long) / 2:
            return boxes  # the text already reads across in page space: that is its reading frame
        out = []
        for bb in boxes:
            r = pymupdf.Rect(bb) * m
            r.normalize()
            out.append((r.x0, r.y0, r.x1, r.y1))
        return out
    except Exception:  # noqa: BLE001
        return boxes


def callout_items(lines, question: str, abs_path: str | None, space: str, tag_boxes=()) -> tuple[list, list]:
    """(counted [(label, box in the lines' space)], rejected labels) for text callouts naming the asked thing."""
    noun = _asked_noun(question)
    if not noun or len(noun[-1]) < 3:
        return [], []
    disp = _display(lines, abs_path, space)
    short = [i for i, (t, bb) in enumerate(lines) if 1 <= len(t.split()) <= 5 and disp[i][3] > disp[i][1]]

    def stacked(i: int, below: bool) -> int | None:
        b = disp[i]
        h = b[3] - b[1]
        best = None
        for j in short:
            if j == i:
                continue
            c = disp[j]
            hc = c[3] - c[1]
            gap = (c[1] - b[3]) if below else (b[1] - c[3])
            if 0.6 < hc / h < 1.6 and -0.3 * h <= gap <= 0.8 * max(h, hc) and \
                    min(b[2], c[2]) - max(b[0], c[0]) > 0.3 * h:
                if best is None or gap < best[0]:
                    best = (gap, j)
        return None if best is None else best[1]

    tagd = [_display([("", bb)], abs_path, space)[0] for bb in tag_boxes]
    counted, rejected, seen = [], [], set()
    for i in short:
        t, bb = lines[i]
        ws = [w for w in (_fold(x) for x in t.split()) if w]
        if len(ws) < len(noun):
            continue
        if ws[-len(noun):-1] + [_sing(ws[-1])] != noun:
            continue
        if _sing(ws[-1]) != ws[-1]:
            rejected.append(t)  # 'SLOW SAND FILTERS' with leaders to SSF1 / SSF2: a plural label names the tagged items
            continue
        nxt = stacked(i, below=True)
        if nxt is not None and re.match(r"\s*\(?\s*typ", lines[nxt][0], re.I):
            rejected.append(f"{t} {lines[nxt][0]}")  # 'BLIND FLANGE' over '(TYP.)': one label for several items
            continue
        if nxt is not None and _CALLOUT_TAIL.match(lines[nxt][0]):
            nxt = None  # '40 GALLON SURGE TANK' over 'SEE NOTE 16 ON SHEET': a reference / place, not more of the name
        if nxt is not None:  # '12" PUMP' over 'DISCHARGE' (over 'HEADER'): the noun is a modifier
            rejected.append(f"{t} {lines[nxt][0]}")
            continue
        prv = stacked(i, below=False)
        label = f"{lines[prv][0]} {t}" if prv is not None and not re.search(r"[.:;]$", lines[prv][0]) else t
        if prv is not None:
            pp = stacked(prv, below=False)  # a three-line label: keep its first line for the checks
            if pp is not None:
                label = f"{lines[pp][0]} {label}"
        if len(label.split()) > 6 or _CALLOUT_BAD.search(label) or _LEGEND.search(label) or len(set(ws) - set(noun)) > 3:
            rejected.append(label)
            continue
        if re.search(r"\b[A-Z]{1,4}-\d{1,3}[A-Z]?\b", label):
            continue  # 'FUEL PUMP FP-1' names a tagged item
        d = disp[i]
        h = d[3] - d[1]
        if any(max(c[0] - d[2], d[0] - c[2], c[1] - d[3], d[1] - c[3]) < 2.5 * h for c in tagd):
            continue  # printed right next to a tag of the family: the same item
        key = (_fold(label), round(d[0] / max(h, 1)), round(d[1] / max(h, 1)))
        if key in seen:
            continue
        seen.add(key)
        box = bb if prv is None else (min(bb[0], lines[prv][1][0]), min(bb[1], lines[prv][1][1]),
                                      max(bb[2], lines[prv][1][2]), max(bb[3], lines[prv][1][3]))
        counted.append((label, box))
    return counted, rejected


def _tag_answer(lines, question: str, path: str, space: str, abs_path: str | None = None) -> Answer | None:
    """'How many fuel pumps (FP) / check valves / filter separators / pressure gauges (PG) …?' on a piping or
    mechanical sheet = the DISTINCT numbered tags of that family ('FP-1'..'FP-5' -> 5; a tag printed twice counts once),
    or, for an un-numbered family ('PG'), every printed tag. The tag family comes from an abbreviation in the question,
    the sheet's own legend ('… CHECK VALVE (CV) …') or a small table of common piping legend abbreviations."""
    if not re.search(r"\bhow many\b", question, re.I):
        return None
    pre, src = _tag_prefixes(question, lines)
    if not pre:
        return None
    nums: dict[str, dict[str, tuple]] = {p: {} for p in pre}
    bare: dict[str, list] = {p: [] for p in pre}
    acro = src.startswith("acronym")
    for t, bb in lines:
        for p in pre:
            for m in re.finditer(_acr_rx(p) if acro else r"(?<![A-Z0-9])" + p + r"-(\d{1,3}[A-Z]?)\b", t):
                if re.fullmatch(r"0+[A-Z]?", m.group(1)):
                    continue  # S0 17:42: OCR read 'PS 0' on a 6000-px copy; tag numbers start at 1
                nums[p].setdefault(m.group(1), (t, bb))
            if re.fullmatch(r"\W*" + p + r"\W*", t) or (QUAL and "." in t and re.fullmatch(r"\W*" + p + r"\W*",
                                                                                         t.replace(".", ""))):
                bare[p].append((t, bb))
    n_num = sum(len(v) for v in nums.values())
    if n_num:
        n = n_num
        used = [tb for v in nums.values() for tb in v.values()]
        how = "; ".join(f"{p}: {', '.join(sorted(v, key=lambda s: (len(s), s)))}" for p, v in nums.items() if v)
        conf = 0.7
    else:
        used = [tb for v in bare.values() for tb in v]
        n = len(used)
        how = f"{n} printed '{'/'.join(pre)}' tags (no numbers: every tag counts)"
        conf = 0.6
    calls, rej = [], []
    # only beside a tag family that IS on the sheet: without tags the printed labels are type-drawing titles and OCR'd
    # box labels (D1's HAER ship sheet dc1026: 'RECIPROCATING CARGO STRIPPER PUMP' over its description, 'STRAINER' boxes
    # stacked on 'STRIPPER PUMP') and the room-label path below reads them better
    if n and src != "abbreviation in the question":  # 'fuel pumps (FP)' asks for the family itself (captain 23:07: = 5)
        try:
            calls, rej = callout_items(lines, question, abs_path, space, tag_boxes=[bb for _, bb in used])
        except Exception:  # noqa: BLE001
            calls, rej = [], []
    if not n:
        return None
    ev = [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"tag '{t[:30]}'")
          for t, bb in used[:60]]
    assumptions = [f"Convention: each distinct tag of the {'/'.join(pre)} family = one component, a tag "
                   f"printed twice counts once (tag family from {src})"] if n else []
    trace = [f"label_count: component tags {'/'.join(pre)} ({src}): {how} -> {n}"] if n else []
    if calls:
        noun = " ".join(_asked_noun(question))
        ev += [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"callout '{t[:40]}'")
               for t, bb in calls[:30]]
        trace.append(f"label_count: + {len(calls)} untagged {noun}(s) named by a text callout: "
                     + "; ".join(t for t, _ in calls[:8]) + (f" (not counted: {'; '.join(rej[:3])})" if rej else ""))
        assumptions.append(f"Convention: an item drawn with only a text callout naming a {noun} ("
                           + ", ".join(t for t, _ in calls[:4]) + ") counts once; callouts where the word only "
                           "describes something else (a pipe header, a panel, a note) do not")
        if n:
            assumptions.append(f"the tagged {'/'.join(pre)} family alone = {n}")
        n_tags, n = n, n + len(calls)
        trace.insert(0, f"label_count: {n} {noun}s = {n_tags} tagged + {len(calls)} by callout")
        # both parts are printed text with exact boxes: keep the tag reading's confidence (vision.wanted() sends a
        # count < 0.7 to Gemini's picture count on the demo, which replaced exact readings before); callouts alone 0.6
        conf = conf if n_tags else 0.6  # (n_tags > 0 here today; kept for a callout-only family later)
    return Answer(value=n, unit="count", confidence=conf, evidence=ev, solver="label_count",
                  trace=trace, assumptions=assumptions,
                  missing=[] if n_num or calls else ["numbered tags (an un-numbered tag may repeat one component)"])


COLD_LAYER_MAX_PX = 12_500_000  # S0 16:48: a cold OCR of a 10-14k px Stoa sheet took 10+ min (3 workers at once)


def _render_source(img_abs: str):
    """(source PDF, px per pt) when the image is our own render of a PDF page (normalize.render_png names it
    '<stem>.r<dpi>.png' next to '<stem>.pdf'; a page copy is '<stem>.p<k>.pdf'), else None."""
    from pathlib import Path

    p = Path(img_abs)
    m = re.fullmatch(r"(.+)\.r(\d{2,4})\.png", p.name)
    if not m:
        return None
    pdf = p.with_name(m.group(1) + ".pdf")
    return (str(pdf), int(m.group(2)) / 72.0) if pdf.exists() else None


def _image_lines(img_abs: str):
    """((text, box image_px), ...) for an image, WITHOUT starting a slow reading of its own (S0 16:48: label_count's
    cold OCR of a rendered Stoa sheet cost 10+ min): (1) a render of a PDF page with a text layer -> that PDF's own
    lines scaled to the render's pixels; (2) the image's text layer when it is already cached (raster reads the same
    one); (3) a fresh text layer only for images up to COLD_LAYER_MAX_PX. None -> label_count abstains."""
    import os

    src = _render_source(img_abs)
    if src is not None:
        try:
            lines = text_lines(src[0], os.stat(src[0]).st_mtime)
        except Exception:  # noqa: BLE001
            lines = ()
        if len(lines) >= 3:
            k = src[1]
            try:  # G1 c6: the render shows the page AS DISPLAYED (/Rotate applied; 5 of Stoa's 9 PDFs are rotated
                # 90°), text_lines are in unrotated page space: rotate first, or every box lands off its text
                import pymupdf

                with pymupdf.open(src[0]) as doc:
                    m = doc[0].rotation_matrix
            except Exception:  # noqa: BLE001
                m = None
            out = []
            for t, bb in lines:
                if m is not None:
                    r = pymupdf.Rect(bb) * m
                    r.normalize()
                    bb = (r.x0, r.y0, r.x1, r.y1)
                out.append((t, tuple(round(c * k, 1) for c in bb)))
            return tuple(out)
    from stoa.solvers.raster import textlayer as T

    cached = False
    try:
        tag = T.file_tag(img_abs)
        cached = any(k.startswith(f"{img_abs}{tag}|") for k in list(T._mem)) or \
            T._disk_key(img_abs, 1100, "local").exists()
        if not cached:
            from stoa import ocr

            cached = ocr.cached(img_abs)
    except Exception:  # noqa: BLE001
        cached = False
    if not cached:
        try:
            from PIL import Image

            with Image.open(img_abs) as im:
                if im.width * im.height > COLD_LAYER_MAX_PX:
                    return None
        except Exception:  # noqa: BLE001
            return None
    tl = T.text_layer(img_abs)
    return tuple((re.sub(r"\s+", " ", i.text).strip(), tuple(round(float(x), 1) for x in i.box))
                 for i in tl.items if str(i.text).strip())


_IMG = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp")


_DOORS_Q = re.compile(r"\bhow many\s+(?:\w+\s+){0,2}doors?\b|\bnumber of doors\b", re.I)
_ROOM_NO = re.compile(r"^\s*(\d{2,4})\s*$")
_DOOR_TAG = re.compile(r"^\s*(\d{2,4})([A-Z])?\s*$")


def door_tags(lines) -> tuple[list, list] | None:
    """US door numbering (S0 17:13, X1 17:01: USACE A-101F 'How many doors does the filter building have?' -> 6 = tags
    101A, 101B, 102, 103, 104A, 104B): a door is tagged with its room's number (+ a letter when the room has several
    doors). Room numbers are the bare numbers printed right under a room name; door tags are the same numbers (or number
    + letter) printed ELSEWHERE. -> (distinct tags [(tag, box)], room numbers) or None when the sheet shows no such
    convention (< 2 room numbers, or no lettered tag)."""
    named = [(t, bb) for t, bb in lines if re.search(r"[A-Za-z]{3,}", t) and len(t.split()) <= 4]
    rooms, room_boxes = set(), set()
    for t, bb in lines:
        m = _ROOM_NO.match(t)
        if not m:
            continue
        h = max(1.0, bb[3] - bb[1])
        for t2, b2 in named:
            if 0 <= bb[1] - b2[3] <= 1.2 * h and min(bb[2], b2[2]) - max(bb[0], b2[0]) > 0:
                rooms.add(m.group(1))
                room_boxes.add(tuple(bb))
                break
    if len(rooms) < 2:
        return None
    tags: dict[str, tuple] = {}
    for t, bb in lines:
        m = _DOOR_TAG.match(t)
        if m and m.group(1) in rooms and tuple(bb) not in room_boxes:
            tags.setdefault(m.group(1) + (m.group(2) or ""), tuple(bb))
    if not any(k[-1].isalpha() for k in tags):
        return None
    return sorted(tags.items()), sorted(rooms)


_NUMBERED_Q = re.compile(r"\bhow many\b.*?\b(?:numbered\s+(?:rooms|spaces)|rooms?\s+(?:have|has|with|carry|carrying|bear|"
                         r"bearing|show|showing)\s+(?:a\s+|their\s+own\s+)?(?:room\s+)?numbers?|room\s+numbers)\b", re.I)
_PLAIN_ROOMS_Q = re.compile(r"^\W*how many rooms(?:\s+(?:are|is)\s+(?:there|shown|drawn))?(?:\s+(?:on|in)\s+(?:this|the)\s+"
                            r"(?:plan|floor|drawing|sheet|building))?(?:\s+(?:are|is)\s+(?:there|shown|drawn))?\W*$", re.I)

# a plain 'how many rooms' (no type, no label named): every room counts, whatever its name
_GENERIC_ROOMS_Q = re.compile(r"^\W*(?:how many|number of|count(?: the)?)\s+rooms\b(?:\s+(?:are|is|there|shown|drawn|on|in|"
                              r"this|the|plan|floor|drawing|sheet|building|level|storey|story|does|do|have|has|total|"
                              r"altogether|whole|entire|page|it))*\W*$", re.I)
BARE_ROOM = __import__("os").environ.get("STOA_G1_BARE_ROOM", "1") != "0"

_CIRCULATION = re.compile(r"\b(?:corridor|lobby|stairs?|stairway|staircase|lift|elevator|landing|walkway|circulation)\b", re.I)


def _every_room_name(pdf_abs: str, path: str, space: str):
    """A plain 'How many rooms are there?' on a sheet that names its rooms in mixed case (DfE L00): every printed room
    name counted once (stacked lines merged by E1's lineplan.labels), title-block / note text left out; corridors,
    lobbies, stairs and lifts are counted and also stated separately (whether they are 'rooms' is a convention).
    Low confidence: an unnamed room is missed and the convention is the asker's. None when nothing room-like is read."""
    from collections import Counter

    try:
        from stoa.evidence.lineplan import labels
        labs = labels(pdf_abs, 0)
    except Exception:  # noqa: BLE001
        return None

    def roomish(t: str) -> bool:
        t = t.strip()
        if not t or re.search(r"\d", t) or not t[:1].isupper() or (t.endswith(")") and "(" not in t) or \
                not re.search(r"[a-z]", t):  # mixed-case names only: 'INT' / 'FIN' / 'CLG' are abbreviations
            return False
        if re.search(r"\b[A-Z]{4,}\b", t) or len(t.split()) > 5:
            return False  # title block headings (DESCRIPTION, ORIGINATOR), notes
        return not re.search(r"\b(?:plan|level|issued|school|scale|drawing|title|project|revision|status|client|"
                             r"sheet|north|key)\b", t, re.I)

    rooms = [lb for lb in labs if roomish(lb["text"])]
    if len(rooms) < 5:
        return None
    circ = [lb for lb in rooms if _CIRCULATION.search(lb["text"])]
    n, nc = len(rooms), len(circ)
    kinds = Counter(re.sub(r"\s+", " ", lb["text"]).strip() for lb in rooms)
    ev = [Evidence(file=path, page=0, bbox=list(lb["bbox"]), bbox_space=space, exact=True, note=f"room name '{lb['text']}'")
          for lb in rooms[:120]]
    ck = Counter(re.sub(r"\s+", " ", lb["text"]).strip() for lb in circ)
    return Answer(value=n, unit="count", confidence=0.5, evidence=ev, solver="label_count",
                  trace=[f"label_count: {n} printed room names on the sheet (every name once, stacked lines merged): "
                         + "; ".join(f"{k} x{v}" if v > 1 else k for k, v in kinds.most_common(14))
                         + (" …" if len(kinds) > 14 else "")],
                  assumptions=["Convention: one printed room name = one room; a room drawn without a name is not counted",
                               (f"includes {nc} circulation space(s) ({', '.join(f'{k} x{v}' if v > 1 else k for k, v in ck.most_common(6))}); "
                                f"without them {n - nc} rooms") if nc else "no corridors / lobbies / stairs are named"],
                  missing=["which spaces count as rooms (circulation, stores, toilets) is the asker's convention"])


def room_numbers(lines) -> list[tuple]:
    """US room numbering (USACE A-101F: 'FILTER ROOM' over '101'): (number, name, name box, number box) for every bare
    2-4 digit number printed right under a short room name (the same test door_tags uses), one per number."""
    named = [(t, bb) for t, bb in lines if re.search(r"[A-Za-z]{3,}", t) and len(t.split()) <= 4]
    out: dict[str, tuple] = {}
    for t, bb in lines:
        m = _ROOM_NO.match(t)
        if not m or m.group(1) in out:
            continue
        h = max(1.0, bb[3] - bb[1])
        for t2, b2 in named:
            if 0 <= bb[1] - b2[3] <= 1.2 * h and min(bb[2], b2[2]) - max(bb[0], b2[0]) > 0:
                out[m.group(1)] = (m.group(1), t2, tuple(b2), tuple(bb))
                break
    return sorted(out.values())


def _numbered_answer(lines, question: str, path: str, space: str, plain_hits: int | None = None) -> Answer | None:
    """X1 23:20 (4) sl-327 'How many numbered rooms does the filter building have?' -> pdf_text 6 @0.55 (text rows with
    ROOM incl. the title; gold 4 = rooms 101..104). A 'numbered rooms' question counts the room numbers printed under
    room names; a plain 'How many rooms are there?' does so only when the generic label count found at most one label
    (the word 'ROOM' itself) and the sheet numbers >= 2 rooms."""
    if not QUAL or len(lines) > 4000:
        return None
    numbered = bool(_NUMBERED_Q.search(question))
    if not numbered and not (_PLAIN_ROOMS_Q.match(question) and plain_hits is not None and plain_hits <= 1):
        return None
    rows = room_numbers(lines)
    if len(rows) < 2 or len({len(r[0]) for r in rows}) > 1:
        return None
    ev = [Evidence(file=path, page=0, bbox=[min(r[2][0], r[3][0]), min(r[2][1], r[3][1]), max(r[2][2], r[3][2]),
                                            max(r[2][3], r[3][3])], bbox_space=space, exact=True,
                   note=f"room {r[0]} '{r[1]}'") for r in rows[:60]]
    conf = (0.7 if numbered else 0.55) - (0.1 if space == "image_px" else 0.0)
    return Answer(value=len(rows), unit="count", confidence=round(conf, 2), evidence=ev, solver="label_count",
                  trace=[f"label_count: {len(rows)} room numbers printed under room names: "
                         + "; ".join(f"{r[0]} {r[1]}" for r in rows[:12])],
                  assumptions=["Convention: one room number printed under a room name = one numbered room"],
                  missing=[] if numbered else ["rooms drawn without a number are not counted"])


def _door_answer(lines, question: str, path: str, space: str) -> Answer | None:
    if not _DOORS_Q.search(question) or re.search(r"\b(double|single|pairs?|leaves|leafs?|fire|exterior|interior|"
                                                  r"glazed|sliding|swing)\b", question, re.I):
        return None  # a door TYPE needs the schedule / symbols, not the tag count
    if len(lines) > 4000:
        return None  # pairwise label search; a sheet this dense is a schedule set, not one plan
    got = door_tags(lines)
    if got is None:
        return None
    tags, rooms = got
    n = len(tags)
    ev = [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"door tag {k}")
          for k, bb in tags[:60]]
    return Answer(value=n, unit="count", confidence=0.6 if space == "pdf_pt" else 0.5, evidence=ev, solver="label_count",
                  trace=[f"label_count: {n} distinct door tags (room number + letter) next to rooms "
                         f"{', '.join(rooms[:8])}: {', '.join(k for k, _ in tags[:20])}"],
                  assumptions=["Convention: one door tag = one door (US numbering: the room's number, plus a letter when "
                               "a room has several doors); a pair of leaves under one tag counts as one door",
                               "doors drawn without a tag are not counted"],
                  missing=["a door schedule to confirm the tags"])


_NOTES_Q = re.compile(r"\bhow many\s+(?:general\s+|numbered\s+|sheet\s+|key(?:ed)?\s+)?notes\b(?!\s+(?:say|mention|"
                      r"refer|list|give|call))", re.I)
_NOTES_HEAD = re.compile(r"^\s*(?:general\s+|sheet\s+|key(?:ed)?\s+)?notes?\s*:?\s*$", re.I)
_NOTE_ITEM = re.compile(r"^\s*(\d{1,2})\s*[.)](?:\s|$)")


def _notes_answer(lines, question: str, path: str, space: str) -> Answer | None:
    """D1 22:52 (HAER hi76c11): 'How many notes are listed on the USGS waterline sheet?' -> 1 = the 'NOTES:' heading
    counted as a label (gold 5 numbered notes). The notes under a NOTES heading are its numbered items '1.' .. 'N.'
    in the heading's column, below it; the count is the last number of the unbroken 1..N run."""
    heads = [(t, bb) for t, bb in lines if _NOTES_HEAD.match(t)]
    blocks = []
    for t, hb in heads:
        h = max(1.0, hb[3] - hb[1])
        items = {}
        for t2, b2 in lines:
            m = _NOTE_ITEM.match(t2)
            if m and b2[1] > hb[1] and abs(b2[0] - hb[0]) <= 3 * h and b2[1] - hb[3] <= 60 * h:
                items.setdefault(int(m.group(1)), (t2, b2))
        n = 0
        while n + 1 in items:
            n += 1
        if n >= 2:
            blocks.append((t, hb, n, [items[k] for k in range(1, n + 1)]))
    if not blocks:
        return None
    n = sum(b[2] for b in blocks)
    ev = [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"note {t.strip()[:12]}")
          for b in blocks for t, bb in b[3][:40]]
    how = " + ".join(f"'{b[0].strip()}' 1-{b[2]}" for b in blocks)
    return Answer(value=n, unit="count", confidence=0.7 if len(blocks) == 1 else 0.5, evidence=ev, solver="label_count",
                  trace=[f"label_count: numbered notes under the notes heading(s): {how} -> {n}"],
                  assumptions=["Convention: one numbered item under a NOTES heading = one note (sub-items a, b, … are "
                               "part of their note)"] + ([f"{len(blocks)} notes blocks on the sheet, added up"]
                                                          if len(blocks) > 1 else []))


STACK_FRAG = bool(int(__import__("os").environ.get("G1_STACK_FRAG", "1")))
QUAL = __import__("os").environ.get("STOA_G1_QUAL", "1") != "0"  # kill switch: qualifier words + view scope (c14)
_QUAL = re.compile(r"(?i)^(?:male|female|men'?s|women'?s|mens|womens|boys'?|girls'?|staff|pupils?'?|students?'?|"
                   r"visitors?'?|public|accessible|disabled|ada|unisex|family|employees?'?|ambulant|"
                   r"gender[- ]neutral|all[- ]gender)\s+")


def _in_scope_views(p: str, question: str, hits: list) -> tuple[list, str]:
    """The labels inside the drawing view(s) the question means (VP's views.scope_for: the plan views, never a
    reflected ceiling plan / axonometric unless asked): MFC A-103 prints 'Male Restrooms' / 'Female Restrooms' in the
    module's FLOOR PLAN and again in its RCP (4 labels, 2 rooms). Unchanged when the sheet has < 2 views, nothing to
    narrow, or no label would remain."""
    try:
        from stoa.sheet.views import scope_for

        if p.lower().endswith(".pdf"):
            sc = scope_for(p, question)
        else:  # an image: only on a text layer some reader already read (never a fresh OCR from here)
            from stoa.solvers.area_tags import _cached_layer

            layer = _cached_layer(p)
            sc = scope_for(p, question, layer=layer) if layer is not None else None
    except Exception:  # noqa: BLE001
        return hits, ""
    if sc is None or not sc.views:
        return hits, ""

    def inside(bb) -> bool:
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        return any(v.bbox and v.bbox[0] <= cx <= v.bbox[2] and v.bbox[1] <= cy <= v.bbox[3] for v in sc.views)

    keep = [h for h in hits if inside(h[1])]
    if not keep or len(keep) == len(hits):
        return hits, ""
    names = "; ".join(str(getattr(v, "title", "") or "")[:40] for v in sc.views[:3])
    return keep, (f"counted in the view(s) the question means ({names}): {len(hits) - len(keep)} label(s) in other "
                  "views (reflected ceiling plan / axonometric / other modules) left out")
_MODIFIER = re.compile(r"^(?:[^\W\d_]{3,}|and|&|of)(?:\s+(?:[^\W\d_]{3,}|and|&|of))*$")
_EXACT_Q = re.compile(r"\b(?:labell?ed|named|called|titled|marked)\b|['\"\u2018\u2019\u201c\u201d]", re.I)


def _stacked_pairs(lines) -> list:
    """(upper, lower) pairs of lines printed as ONE label on two lines: similar text height, the lower one starts within
    0.8 heights below, x-centres within half the wider line. Each upper line pairs with its nearest lower line."""
    out = []
    boxes = [(t, bb) for t, bb in lines if len(t.split()) <= 3 and bb[3] > bb[1]]
    for t, bb in boxes:
        h = bb[3] - bb[1]
        xc = (bb[0] + bb[2]) / 2
        best = None
        for t2, b2 in boxes:
            if b2 is bb:
                continue
            h2 = b2[3] - b2[1]
            gap = b2[1] - bb[3]
            if 0.6 < h2 / h < 1.6 and -0.3 * h <= gap <= 0.8 * h and \
                    abs((b2[0] + b2[2]) / 2 - xc) < 0.5 * max(bb[2] - bb[0], b2[2] - b2[0]):
                if best is None or gap < best[0]:
                    best = (gap, (t2, b2))
        if best is not None:
            out.append(((t, bb), best[1]))
    return out


@register("label_count")
class LabelCount(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith((".pdf", *_IMG))

    def solve(self, path: str, question: str) -> Answer:
        import os

        from stoa.data import abspath

        question = as_how_many(question)
        stems = asked_types(question)
        doors = bool(_DOORS_Q.search(question))
        stairs = bool(_STEPS_Q.search(question))
        tags = bool(_TAG_ABBR_Q.search(question) or any(re.search(r"\b" + k + r"(?:s|es)?\b", question.lower())
                                                         for k in _TAG_WORDS if k != "pump"))
        if not stems and not stairs and not tags and not doors:
            return self.abstain("not a 'how many <room type>' question")
        if _SCOPED.search(question) and not stairs:
            return self.abstain("scoped to an apartment / the whole building: labels carry no membership")
        p = str(abspath(path))
        img = p.lower().endswith(_IMG)
        space = "image_px" if img else "pdf_pt"
        try:
            lines = _image_lines(p) if img else text_lines(p, os.stat(p).st_mtime)
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the {'image' if img else 'PDF'} text ({type(e).__name__})")
        if lines is None:
            return self.abstain("a large image whose text has not been read yet (a cold reading costs minutes; the "
                                "image readers read it)")
        if doors:
            a = _door_answer(lines, question, path, space)
            if a is not None:
                return a
            return self.abstain("no door tags (room number + letter) on the sheet: the door symbols are counted by "
                                "the drawing readers")
        if _NOTES_Q.search(question):
            a = _notes_answer(lines, question, path, space)
            if a is not None:
                return a
        a = _numbered_answer(lines, question, path, space)
        if a is not None:
            return a
        if tags or stems:
            a = _tag_answer(lines, question, path, space, abs_path=p)
            if a is not None:
                return a
        if stairs:
            a = _stair_answer(lines, question, path, space)
            if a is not None:
                return a
            return self.abstain("no printed stair note ('21 Stg.', '16 R', '16 RISERS') on the sheet")
        if len(lines) > MAX_LINES:
            return self.abstain(f"{len(lines)} text lines: a dense sheet (legends / schedules repeat room names)")
        try:
            from stoa.router.recognize import _pdf

            if _pdf(p, os.stat(p).st_mtime)["bim_fill"] >= 2:
                return self.abstain("IfcConvert-style sheet: plan_rooms counts its room outlines")
        except Exception:  # noqa: BLE001
            pass
        hits, seen = [], set()
        fuzzy = 0
        pairs = _stacked_pairs(lines)
        if STACK_FRAG and _EXACT_Q.search(question):
            # D1 c8 (DfE 2026 GA plans; 'How many rooms labelled X'): the LAST line of a name printed on two / three lines
            # ('Sports Equipment' over 'Store', 'Entrance and' over 'Cloakroom', 'Furniture' over 'Store') is not a room
            # labelled 'Store' of its own: 'Store' 15 vs 11, 'Cloakroom' 4 vs 3, 'Toilet' 7 vs 3. Only when the line above
            # is plain name words (an apartment tag 'Whg. A' / 'App. A', a number or an area is no part of the name), and
            # not for type questions ('how many classrooms': 'Junior' over 'Classroom' is a classroom)
            for (t1, b1), (t2, b2) in pairs:
                if not _MODIFIER.match(t1.strip()) or _matches(t1, stems) or _matches(f"{t1} {t2}", stems):
                    continue
                if _matches(t2, stems):
                    seen.add((round(b2[0]), round(b2[1])))
        qual = QUAL and not _EXACT_Q.search(question)
        for t, bb in lines:
            s = _matches(t, stems)
            if not s and img:
                s = _ocr_matches(t, stems)
                fuzzy += bool(s) and (round(bb[0]), round(bb[1])) not in seen
            if not s and qual and (mq := _QUAL.match(t.strip())):
                # X1 00:00 (6) MFC A-103 'How many restrooms are there?' (gold 2): 'Male Restrooms' / 'Female Restrooms'
                # are restrooms; only the title block's 'RESTROOMS,' was counted (1)
                s = _matches(t.strip()[mq.end():], stems)
            if s and QUAL and t.rstrip().endswith(","):
                continue  # a list in a title block / note ('RESTROOMS,'), not a room's own label
            key = (round(bb[0]), round(bb[1]))
            if s and key not in seen:
                seen.add(key)
                hits.append((t, bb))
        view_note = ""
        if QUAL and len(hits) >= 2:
            hits, view_note = _in_scope_views(p, question, hits)
        if len(hits) <= 1 and _PLAIN_ROOMS_Q.match(question):
            a = _numbered_answer(lines, question, path, space, plain_hits=len(hits))
            if a is not None:
                return a
        if BARE_ROOM and len(hits) >= 2 and _GENERIC_ROOMS_Q.match(question) and \
                all(re.fullmatch(r"rooms?", _fold(t).strip()) for t, _ in hits):
            # G1 c15 (Stoa's DfE L00 PDF): 'How many rooms are there?' = 7 @0.83 from 7 lines reading exactly 'Room' =
            # the second line of 'Plant / Room', 'Group / Room', 'Fitness / Room' … (the sheet has 8 Secondary
            # Classrooms + 10 Group Rooms + 3 Plant Rooms alone). A plain room count counts every room, whatever its
            # name: bare 'Room' tails are not that count
            every = _every_room_name(p, path, space) if p.lower().endswith(".pdf") else None
            if every is not None:
                return every
            return self.abstain(f"the {len(hits)} 'Room' line(s) are the last line of longer room names (Plant Room, "
                                "Group Room …); a plain room count needs every room, not the labels with the word 'Room'")
        stacked = 0
        for (t1, b1), (t2, b2) in pairs:
            # 'Secondary' over 'Classroom' (DfE PNG: OCR reads a two-line label as two lines; stoa-like sl-035)
            k1, k2 = (round(b1[0]), round(b1[1])), (round(b2[0]), round(b2[1]))
            if k1 in seen or k2 in seen or not _matches(f"{t1} {t2}", stems):
                continue
            seen |= {k1, k2}
            hits.append((f"{t1} / {t2}", (min(b1[0], b2[0]), b1[1], max(b1[2], b2[2]), b2[3])))
            stacked += 1
        syn = {x for v in _SYN.values() for x in v}
        plural = [(t, bb) for t, bb in hits if not (set(stems) & syn) and _fold(t).endswith("s") and
                  any(_fold(t) in (st + "s", st + "es") for st in stems)]
        if plural:
            # D1 22:52 (HAER hi76c2): one label 'CHLORINATORS' with leaders to two units (gold 2) counted as 1: a PLURAL
            # equipment label names several items (rooms keep their own vocabulary: 'STAIRS', 'TOILETS' = one room)
            hits = [h for h in hits if h not in plural]
            if not hits:
                return self.abstain(f"the label '{plural[0][0][:40]}' is plural: it names several items (its leaders are "
                                    "not traced), so it is not counted as one")
        if not hits:
            return self.abstain(f"no label of the asked type ({', '.join(stems[:4])}) in the text layer "
                                f"({len(lines)} lines)")
        rel = []
        for k, v in _RELATED.items():
            if set(v) & set(stems) or k in [_fold(x) for x in stems[:1]] or stems[0] in _SYN.get(k, ()):
                rel += [t for t, _ in lines if any(_fold(t).startswith(r) for r in _RELATED[k]) and
                        not _matches(t, stems) and len(t.split()) <= 4]
        n = len(hits)
        ev = [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"label '{t}'")
              for t, bb in hits[:60]]
        assumptions = ["Convention: one printed room label of the asked type = one room (rooms drawn without a "
                       "label are not counted)"]
        if stacked:
            assumptions.append(f"{stacked} label(s) printed on two lines were read as one")
        if view_note:
            assumptions.append(view_note)
        if fuzzy:
            assumptions.append(f"{fuzzy} label(s) read with an OCR slip (e.g. M for N, 1 for I, a glued prefix) counted "
                               "as the asked type")
        if rel:
            assumptions.append(f"not counted (a related type, not the one asked): {', '.join(sorted(set(rel))[:5])}")
        # an image's labels come from OCR / a VLM reading (a label can be missed or misread): one step less sure
        conf = (0.6 if rel else 0.7) - (0.1 if img else 0.0)
        return Answer(value=n, unit="count", confidence=round(conf, 2), evidence=ev, solver=self.name,
                      trace=[f"label_count: {n} label(s) of the asked type on the sheet: "
                             + "; ".join(t for t, _ in hits[:12])],
                      assumptions=assumptions,
                      missing=[] if n else ["room labels of this type"])


# ---------------------------------------------------------------------------------------------------------------------
# stated_count (G1 cycle 5; E1 16:59, X1 16:41): a sheet that PRINTS the count of the asked room kind ('48 DORMS',
# 'SECOND FLOOR = 48 DORMS', 'TOTAL = 144 DORMS', '3 BEDROOMS', 'UNITS: 12') answers 'how many …' from that line, not
# from its polygons (plan_rooms counted 2 labelled fills on Stoa's AFCEC dorm sheet: gold 48 / 144). The row label to
# the left of the number ('SECOND FLOOR =', 'TOTAL =') picks the row the question's scope asks for.
_STATED = re.compile(r"(?<![\w.,/'\"-])(\d{1,4})\s*(x\s*|×\s*)?([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .'-]{1,30}?)\s*$")
_STATED_EQ = re.compile(r"^\s*([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .'-]{1,30}?)\s*[:=]\s*(\d{1,4})\s*$")
_FLOOR_WORDS = {
    "basement": ("basement", "cellar", "keller", "kellari"), "ground": ("ground", "erdgeschoss", "eg", "1st floor"),
    "1": ("first", "1st", "level 1", "floor 1"), "2": ("second", "2nd", "level 2", "floor 2"),
    "3": ("third", "3rd", "level 3", "floor 3"), "4": ("fourth", "4th", "level 4", "floor 4"),
    "5": ("fifth", "5th", "level 5", "floor 5"),
}
_TOTAL_ROW = re.compile(r"\b(total|totals|overall|building|all floors|gesamt|summe|yhteens[aä])\b", re.I)
_TOTAL_Q = re.compile(r"\b(whole|entire|in total|altogether|all (?:the )?(?:floors|storeys|levels)|does the building|"
                      r"building (?:have|has|contain)|in the building|insgesamt|ganze\w*|yhteens[aä])\b", re.I)
_THIS_Q = re.compile(r"\b(this|the drawn|the shown|on the plan|on this|drawn on)\b", re.I)


def _floor_of(text: str) -> str | None:
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    for k, words in _FLOOR_WORDS.items():
        if any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t) for w in words):
            if k == "ground" and "1st floor" in t and not re.search(r"\bground\b", t):
                return "1"
            return k
    return None


def _noun_matches(noun: str, stems: list[str]) -> bool:
    f = _fold(noun)
    if not f or len(noun.split()) > 3:
        return False
    sing = {f, _PLURAL.sub("", f), f[:-1] if f.endswith("s") else f, f[:-3] + "y" if f.endswith("ies") else f}
    for s in stems:
        core = re.sub(r"rooms?$", "", s) if len(s) > 6 else s
        if s in sing or (len(core) >= 3 and (core in sing or any(x.startswith(core) and len(x) - len(core) <= 6
                                                                  for x in sing))):
            return True
        if any(len(x) >= 4 and s.startswith(x) and len(s) - len(x) <= 6 for x in sing):  # 'DORMS' for 'dormitories'
            return True
        if len(core) >= 6 and any(len(x) >= 4 and core.startswith(x) and len(core) - len(x) <= 6 for x in sing):
            # X1 21:26 rp-065: 'dormitory rooms' (core 'dormitory') for the printed '48 DORMS'
            return True
    return False


def _display_lines(p: str, img: bool):
    """((text, box in the evidence space, box in DISPLAY orientation), ...): a /Rotate 90 sheet's lines are rotated back
    so one printed row ('SECOND FLOOR =' … '48 DORMS') shares a y."""
    import os

    if img:
        lines = _image_lines(p)
        return None if lines is None else tuple((t, bb, bb) for t, bb in lines)
    import pymupdf

    lines = text_lines(p, os.stat(p).st_mtime)
    with pymupdf.open(p) as doc:
        m = doc[0].rotation_matrix
    out = []
    for t, bb in lines:
        r = pymupdf.Rect(bb) * m
        r.normalize()
        out.append((t, bb, (r.x0, r.y0, r.x1, r.y1)))
    return tuple(out)


def stated_rows(lines, stems: list[str]) -> list[dict]:
    """Printed count statements of the asked kind: [{'n', 'noun', 'row' (label to its left), 'box', 'text'}]."""
    rows = []
    for t, bb, d in lines:
        m = _STATED.search(t)
        n = noun = None
        # '101 OFFICE' is a room number + name, '48 DORMS' / '2 x WC' a count: a count > 1 needs a plural or an 'x'
        if m and _noun_matches(m.group(3), stems) and (m.group(2) or int(m.group(1)) == 1 or
                                                       re.search(r"(?:s|S)\s*$", m.group(3))):
            n, noun = int(m.group(1)), m.group(3)
            left = t[:m.start()].strip(" =:")
        else:
            m2 = _STATED_EQ.match(t)
            if m2 and _noun_matches(m2.group(1), stems):
                n, noun, left = int(m2.group(2)), m2.group(1), ""
        if n is None or n == 0:
            continue
        label = " ".join([*_row_left(lines, t, d), left]).strip(" =:")
        rows.append({"n": n, "noun": noun.strip(), "row": label, "box": list(bb), "text": t})
    return rows


def _row_left(lines, t: str, d) -> list[str]:
    """The row label: short lines on the same display row, chained leftwards with gaps under ~8 text heights."""
    h = max(1.0, d[3] - d[1])
    yc = (d[1] + d[3]) / 2
    cands = sorted(((d2[0], d2[2], t2) for t2, _, d2 in lines if t2 != t and abs((d2[1] + d2[3]) / 2 - yc) < 0.5 * h
                    and d2[2] <= d[0] + 0.5 * h and len(t2.split()) <= 5), key=lambda x: -x[1])
    same, edge = [], d[0]
    for x0, x1, t2 in cands:
        if edge - x1 > 8 * h:
            break
        same.insert(0, t2)
        edge = x0
    return same


# X1 17:20 (Tapiola 1963 kitchen type sheet): 'TÄTÄ TYYPPIÄ 25 KPL' + 'PEILIKUVA KEITTIÖTÄ 25 KPL' (KPL = pieces): 'How
# many kitchens of this type are built in total, including the mirror-image ones?' = 25 + 25
_TYPE_Q = re.compile(r"\b(of this type|this type|are (?:to be )?built|built in total|mirror(?:ed)?[- ]?image|mirrored|"
                     r"peilikuv\w*|t[äa]t[äa] tyyppi\w*|dieses typs|dieser art|spiegelbildlich\w*)", re.I)
_QTY = re.compile(r"(?<![\w.,])(\d{1,4})\s*(?:KPL|kpl|Kpl|pcs|PCS|pc\.|st\.|ST\b|Stk\.?|STK|EA\b|NOS?\b\.?)")
_MIRROR = re.compile(r"PEILI|MIRROR|SPIEGEL|SPEGEL|INVERS|PEILIK", re.I)
_TYPE_ROW = re.compile(r"TYYP|TYPE|\bTYP\b|TYPPI|THIS|T[ÄA]T[ÄA]", re.I)


def type_quantities(lines, question: str) -> tuple[list[dict], str] | None:
    """Printed quantities of the drawn type ('25 KPL'), split into this type / its mirror image -> (rows used, why)."""
    if not _TYPE_Q.search(question):
        return None
    rows = []
    for t, bb, d in lines:
        m = _QTY.search(t)
        if not m:
            continue
        label = " ".join([*_row_left(lines, t, d), t[:m.start()]]).strip()
        kind = "mirror" if _MIRROR.search(label) else "type" if _TYPE_ROW.search(label) else None
        if kind:
            rows.append({"n": int(m.group(1)), "kind": kind, "row": label, "box": list(bb), "text": t})
    if not rows:
        return None
    ty = [r for r in rows if r["kind"] == "type"][:1]
    mi = [r for r in rows if r["kind"] == "mirror"][:1]
    if re.search(r"\b(including|incl\.?|with|and|plus|together)\b.{0,30}mirror|mirror.{0,30}\b(included|too|as well)\b|"
                 r"peilikuv\w*\s+mukaan", question, re.I):
        used = ty + mi
        why = "this type + its mirror image, both printed"
    elif re.search(r"mirror|peilikuv", question, re.I):
        used, why = mi, "the mirror-image quantity printed"
    else:
        used, why = ty, "the quantity printed for this type"
    return (used, why) if used else None


def _sheet_floor(lines) -> str | None:
    fl = {_floor_of(t) for t, *_ in lines if re.search(r"\b(floor|level|storey|geschoss)\s+plan\b|\bplan\b.*\b(floor|level)\b",
                                                         t, re.I)}
    fl.discard(None)
    return next(iter(fl)) if len(fl) == 1 else None


def pick_stated(rows: list[dict], question: str, lines) -> tuple[dict | None, str, float]:
    """-> (row, why, confidence) for the question's scope, or (None, why, 0)."""
    if not rows:
        return None, "no printed count of the asked kind", 0.0
    totals = [r for r in rows if _TOTAL_ROW.search(r["row"])]
    floors = {}
    for r in rows:
        f = _floor_of(r["row"])
        if f is not None and r not in totals:
            floors.setdefault(f, r)
    plain = [r for r in rows if r not in totals and _floor_of(r["row"]) is None]
    qf = _floor_of(question)
    if _TOTAL_Q.search(question) and qf is None:
        if totals:
            return totals[0], f"the printed total row '{totals[0]['row']}'", 0.85
        if len(floors) >= 2:
            s = sum(r["n"] for r in floors.values())
            return {**next(iter(floors.values())), "n": s, "sum_of": list(floors.values())}, \
                f"the sum of the printed per-floor rows ({len(floors)} floors)", 0.6
        return None, "the question asks the whole building; the sheet prints no total", 0.0
    if qf is not None:
        if qf in floors:
            return floors[qf], f"the printed row for that floor '{floors[qf]['row']}'", 0.85
        if not floors and len({r["n"] for r in plain}) == 1 and _sheet_floor(lines) == qf:
            return plain[0], "the sheet's own floor (its title) with one printed count", 0.75
        return None, "no printed row for the asked floor", 0.0
    # 'this floor' / no scope: the sheet's floor row, else every non-total row agrees, else one statement
    sf = _sheet_floor(lines)
    if sf is not None and sf in floors:
        return floors[sf], f"the sheet's own floor ({sf}) row '{floors[sf]['row']}'", 0.8
    cand = [*floors.values(), *plain]
    if cand and len({r["n"] for r in cand}) == 1:
        return cand[0], ("every printed per-floor row gives the same number" if len(cand) > 1 else
                         "one printed count on the sheet"), 0.75 if len(cand) > 1 else 0.7
    return None, f"{len(cand)} different printed counts and the question names no floor", 0.0


@register("stated_count")
class StatedCount(BaseSolver):
    """'How many dorm rooms are on the second floor?' = the sheet's printed 'SECOND FLOOR = 48 DORMS' (text layer, $0)."""
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith((".pdf", *_IMG))

    def solve(self, path: str, question: str) -> Answer:
        from stoa.data import abspath

        question = as_how_many(question)
        stems = asked_types(question)
        if (not stems and not _TYPE_Q.search(question)) or _STEPS_Q.search(question):
            return self.abstain("not a 'how many <room kind>' question")
        p = str(abspath(path))
        img = p.lower().endswith(_IMG)
        try:
            lines = _display_lines(p, img)
        except Exception as e:  # noqa: BLE001
            return self.abstain(f"could not read the text ({type(e).__name__})")
        if not lines:
            return self.abstain("no text layer read yet")
        tq = type_quantities(lines, question)
        if tq is not None:
            used, why = tq
            space = "image_px" if img else "pdf_pt"
            n = sum(r["n"] for r in used)
            ev = [Evidence(file=path, page=0, bbox=r["box"], bbox_space=space, exact=True,
                           note=f"printed quantity: {r['row']} {r['text']}".strip()) for r in used]
            return Answer(value=n, unit="count", confidence=0.5 if img else 0.6, evidence=ev, solver=self.name,
                          trace=[f"stated_count: {why}: " + " + ".join(f"'{r['row']} {r['text']}' = {r['n']}"
                                                                      for r in used) + f" = {n}"],
                          assumptions=[f"Convention: the quantity printed on the type sheet (KPL / pcs / Stk) is the "
                                       f"number built ({why})"])
        rows = stated_rows(lines, stems)
        row, why, conf = pick_stated(rows, question, lines)
        if row is None:
            return self.abstain(why)
        used = row.get("sum_of") or [row]
        space = "image_px" if img else "pdf_pt"
        ev = [Evidence(file=path, page=0, bbox=r["box"], bbox_space=space, exact=True,
                       note=f"printed: {(r['row'] + ' ') if r['row'] else ''}{r['text']}") for r in used]
        if img:
            conf -= 0.1  # OCR can misread a digit
        others = [f"{r['row'] or '(no label)'}: {r['n']}" for r in rows if r not in used][:6]
        return Answer(value=int(row["n"]), unit="count", confidence=round(conf, 2), evidence=ev, solver=self.name,
                      trace=[f"stated_count: {why}: " + "; ".join(f"'{(r['row'] + ' ') if r['row'] else ''}{r['text']}'"
                                                                 for r in used)],
                      assumptions=[f"Convention: the count printed on the sheet is taken as stated ({why}); rooms "
                                   "were not counted one by one", *([f"other printed counts of this kind: {', '.join(others)}"]
                                               if others else [])])
