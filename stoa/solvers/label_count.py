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
    return f"How many {m.group('x')}?" if m else question


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
_TAG_ABBR_Q = re.compile(r"\(([A-Z]{1,5})(?:-[.…]*)?\)")
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
    ab = [m.group(1) for m in _TAG_ABBR_Q.finditer(question) if m.start() <= hm.end(1)]
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
    return [], ""


def _tag_answer(lines, question: str, path: str, space: str) -> Answer | None:
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
    for t, bb in lines:
        for p in pre:
            for m in re.finditer(r"(?<![A-Z0-9])" + p + r"-(\d{1,3}[A-Z]?)\b", t):
                if re.fullmatch(r"0+[A-Z]?", m.group(1)):
                    continue  # S0 17:42: OCR read 'PS 0' on a 6000-px copy; tag numbers start at 1
                nums[p].setdefault(m.group(1), (t, bb))
            if re.fullmatch(r"\W*" + p + r"\W*", t):
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
        if not n:
            return None
        how = f"{n} printed '{'/'.join(pre)}' tags (no numbers: every tag counts)"
        conf = 0.6
    ev = [Evidence(file=path, page=0, bbox=list(bb), bbox_space=space, exact=True, note=f"tag '{t[:30]}'")
          for t, bb in used[:60]]
    return Answer(value=n, unit="count", confidence=conf, evidence=ev, solver="label_count",
                  trace=[f"label_count: component tags {'/'.join(pre)} ({src}): {how} -> {n}"],
                  assumptions=[f"Convention: each distinct tag of the {'/'.join(pre)} family = one component, a tag "
                               f"printed twice counts once (tag family from {src})"],
                  missing=[] if n_num else ["numbered tags (an un-numbered tag may repeat one component)"])


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
        if tags or stems:
            a = _tag_answer(lines, question, path, space)
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
        for t, bb in lines:
            s = _matches(t, stems)
            key = (round(bb[0]), round(bb[1]))
            if s and key not in seen:
                seen.add(key)
                hits.append((t, bb))
        stacked = 0
        for (t1, b1), (t2, b2) in _stacked_pairs(lines):
            # 'Secondary' over 'Classroom' (DfE PNG: OCR reads a two-line label as two lines; stoa-like sl-035)
            k1, k2 = (round(b1[0]), round(b1[1])), (round(b2[0]), round(b2[1]))
            if k1 in seen or k2 in seen or not _matches(f"{t1} {t2}", stems):
                continue
            seen |= {k1, k2}
            hits.append((f"{t1} / {t2}", (min(b1[0], b2[0]), b1[1], max(b1[2], b2[2]), b2[3])))
            stacked += 1
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
