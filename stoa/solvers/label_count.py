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
              "air vent": ("MAV",), "manual air vent": ("MAV",), "flow switch": ("FS",), "pump": ("P",)}
_LEGEND = re.compile(r"([A-Z][A-Z /&-]{3,60}?)\s*\(([A-Z]{1,5})\)")
_STOPW = {"the", "a", "an", "of", "on", "in", "are", "is", "how", "many", "shown", "there", "this", "sheet", "plan",
          "piping", "tagged", "called", "out", "with", "total", "and", "different", "types", "type", "w"}


def _words(s: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in _STOPW}


def _tag_prefixes(question: str, lines) -> tuple[list[str], str]:
    ab = _TAG_ABBR_Q.findall(question)
    if ab:
        return ab, "abbreviation in the question"
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
                  assumptions=[f"each distinct tag of the {'/'.join(pre)} family is one component (tag family from "
                               f"{src})"],
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
            return tuple((t, tuple(round(c * k, 1) for c in bb)) for t, bb in lines)
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


@register("label_count")
class LabelCount(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        return str(path).lower().endswith((".pdf", *_IMG))

    def solve(self, path: str, question: str) -> Answer:
        import os

        from stoa.data import abspath

        stems = asked_types(question)
        stairs = bool(_STEPS_Q.search(question))
        tags = bool(_TAG_ABBR_Q.search(question) or any(re.search(r"\b" + k + r"(?:s|es)?\b", question.lower())
                                                         for k in _TAG_WORDS if k != "pump"))
        if not stems and not stairs and not tags:
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
        assumptions = ["each printed room label of this type is one room (rooms drawn without a label are not "
                       "counted)"]
        if rel:
            assumptions.append(f"not counted (a related type, not the one asked): {', '.join(sorted(set(rel))[:5])}")
        # an image's labels come from OCR / a VLM reading (a label can be missed or misread): one step less sure
        conf = (0.6 if rel else 0.7) - (0.1 if img else 0.0)
        return Answer(value=n, unit="count", confidence=round(conf, 2), evidence=ev, solver=self.name,
                      trace=[f"label_count: {n} label(s) of the asked type on the sheet: "
                             + "; ".join(t for t, _ in hits[:12])],
                      assumptions=assumptions,
                      missing=[] if n else ["room labels of this type"])
