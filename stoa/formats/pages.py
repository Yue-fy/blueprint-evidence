"""Multi-page PDFs: pick the page a question is about, and make a one-page copy the solvers can read (P1).

Every solver reads page 0. An uploaded drawing SET (cover sheet + plans, the 64-70 MB opening_design sets) is then
answered from its cover: on 30 dev questions re-asked on 2-page copies (cover + the Swiss plan), 0% right vs 96.7%
on the plan alone (P1 14 Nov, run 20261114-130418-pipeline-up_fmt-2c38, kind fmt:pdfmulti).

    from stoa.formats.pages import pick_page, one_page_copy
    k, why = pick_page("set.pdf", "How many doors are on the ground floor?")
    path = one_page_copy("set.pdf", k)      # set.p3.pdf next to it (cached), same page size and coordinates

pick_page: a page whose text names what the question names (sheet number like 'A-101', a quoted name, a storey or
room word that only some pages carry) wins; otherwise the page with the most drawing (vector content stream bytes +
plan text cues: m², room numbers, door/window tags). Cheap: no rendering, no model (a 70 MB set takes ~1 s).
"""

from __future__ import annotations

import os

import re
from pathlib import Path

_AREA = re.compile(r"\d+(?:[.,]\d+)?\s*(?:m²|m2|sq\.?\s*ft|ft²|sf)\b", re.I)
_ROOMNO = re.compile(r"\b\d{1,2}\.\d{2}\b|\b[A-Z]?\d{3}[A-Z]?\b")
# sheet numbers: 'A-101', 'M-101A', 'S-100F', 'A-2', 'A1.2', 'A101' (P1 17:35: the old pattern needed 2-4 digits and a
# word end, so 'M-101A' / 'S-100F' / 'A-2' never counted as a named sheet)
_SHEET = re.compile(r"\b[A-Z]{1,3}(?:[-.]\d{1,4}|\d{2,4}|\d(?=\.\d))(?:\.\d+)?[A-Z]?\b")
_WORD = re.compile(r"[^\W\d_]{4,}", re.U)
_COMMON = {"what", "which", "where", "many", "much", "total", "area", "areas", "floor", "plan", "plans", "drawing",
           "drawn", "shown", "sheet", "this", "that", "there", "with", "from", "have", "rooms", "room", "doors", "door",
           "windows", "window", "number", "count", "square", "meters", "metres", "feet", "length", "wall", "walls",
           "wie", "viele", "sind", "auf", "diesem", "dieser", "grundriss", "quelle", "combien", "dans", "quanti",
           "sono", "kuinka", "monta"}


# a sheet title line: where a set names each sheet's storey (P1 13:55: storeys named '-1', '0', '1' were found on every
# page as plain text -> 7 of 8 misses in experiments/p1/formats/pick_storeys.py)
_TITLE = re.compile(r"plan|floor|level|storey|story|geschoss|grundriss|etage|étage|verdieping|kondygnac|rzut|kerros|"
                    r"pohja|sheet|blatt|niveau|piano|planta|plano", re.I)


def _token(name: str, text: str) -> bool:
    """`name` as a whole token: '0' matches 'Floor plan: 0', not '0.9.0', '1:100' or '(-1.7)'."""
    return re.search(rf"(?<![\w.:\-]){re.escape(name)}(?![\w.:])", text, re.I) is not None


_BASEMENT_Q = re.compile(r"\b(basement|cellar|keller\w*|untergeschoss|kelder|sous-sol|piwnic\w*|kellari\w*|"
                         r"sótano|sotano|seminterrato|souterrain)\b", re.I)
_BASEMENT_NAME = re.compile(r"^\s*(kg|ug|u1|b1|ss|-1)\b|basement|cellar|keller|untergeschoss|kelder|sous-sol|piwnica|"
                            r"kellari|sótano|sotano|seminterrato|souterrain", re.I)
_PLAN_COLON = re.compile(r"(?:floor\s*plan|plan|plattegrond|grundriss|rzut\w*|pohjapiirros|plano|planta|kerros)\s*"
                         r"[:–—]\s*(\S.{0,40}?)\s*$", re.I)
_PLAN_TAIL = re.compile(r"^\s*(\S.{0,30}?)\s+(?:floor\s+)?plan\s*$", re.I)


# a title line that ENDS with the storey and has no 'plan' word (P1 20:15, the EFA sheets: 'Secondary Type 3 600 Ground
# Floor'); used only when the page names exactly ONE such storey (a room-count table lists every floor)
_FLOOR_TAIL = re.compile(r"(?:^|\s)((?:lower\s+|upper\s+)?(?:ground|first|second|third|fourth|fifth|sixth|seventh|eighth|"
                         r"basement|mezzanine|roof|attic|\d{1,2}(?:st|nd|rd|th))\s+(?:floor|storey|story|level)|"
                         r"level\s+-?\d{1,2})\s*$", re.I)


def sheet_storey(text: str) -> str | None:
    """The storey a sheet's title names: '… — Floor plan: 2. Level' / 'Grundriss: EG' -> '2. Level' / 'EG';
    'GROUND FLOOR PLAN' -> 'GROUND'; else a short line ending with one storey ('… 600 Ground Floor' -> 'Ground Floor')
    when it is the only storey named that way. None if no title line says it."""
    lines = text.splitlines()[:400]
    for ln in lines:
        m = _PLAN_COLON.search(ln) or _PLAN_TAIL.match(ln)
        if m:
            return m.group(1).strip()
    tails = {" ".join(m.group(1).split()).title() for ln in lines if len(ln.strip()) <= 60
             and (m := _FLOOR_TAIL.search(ln.strip()))}
    return next(iter(tails)) if len(tails) == 1 else None


def _stream_bytes(page) -> int:
    doc = page.parent
    try:
        return sum(len(doc.xref_stream(x) or b"") for x in page.get_contents())
    except Exception:  # noqa: BLE001
        return 0


_SCORES: dict[tuple, list[dict]] = {}


def page_scores(path: str | Path) -> list[dict]:
    """Per page: vector bytes, area labels, room numbers, chars, text. Remembered per file (path, mtime, size) for the
    process: a 156-page set takes 3-7 s to read and every question on it asks again (P1 17:40)."""
    import fitz  # pymupdf

    try:
        st = Path(path).stat()
        key = (str(Path(path).resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and key in _SCORES:
        return _with_scan_texts(path, [dict(pg) for pg in _SCORES[key]])
    out = []
    with fitz.open(path) as d:
        from stoa.formats.normalize import ocr_layer_scan

        for i, p in enumerate(d):
            text = p.get_text("text") or ""
            vb = _stream_bytes(p)
            if text.strip() and ocr_layer_scan(p):
                text, vb = "", 0  # X1 c13: a scanner's OCR layer: a scanned page (its picture is read, not the layer)
            out.append({"page": i, "vector_bytes": vb, "areas": len(_AREA.findall(text)),
                        "room_numbers": len(_ROOMNO.findall(text)), "chars": len(text), "own_chars": len(text),
                        "text": text})
    if key is not None:
        if len(_SCORES) > 64:
            _SCORES.clear()
        _SCORES[key] = out
    return _with_scan_texts(path, [dict(pg) for pg in out])


# ------------------------------------------------------------------------------------- scanned sets (P1 19:40)
# Captain 18:50: 'a building can have 8 floor plans' as a SCANNED set (every page one image, no text layer): page
# picking then only had 'the page with the most ink' (P1's 3-page scanned EFA set: 9/24 questions on the right page,
# every one to the ground floor). Each page's image is read once (the raster text layer of the page's scan PNG = the
# very file a question on that page reads, so the reading is shared) and its text cached beside the set, so the
# naming / storey rules below work on scans as on CAD sets. Read by warm at upload, or by the first question.
SCAN_TEXT_PAGES = int(os.environ.get("STOA_SCAN_TEXT_PAGES", "12"))  # read at most this many scanned pages per set
# a QUESTION reads them itself only on small sets (warm did not run, or ran out of time): each page costs one text
# layer (~1-3 min cold on a busy CPU), and the picked page's reading is needed anyway
SCAN_TEXT_ASK_PAGES = int(os.environ.get("STOA_SCAN_TEXT_ASK_PAGES", "4"))
SCAN_TEXT_VERSION = 1


def scanned_set(pages: list[dict]) -> bool:
    """Every page one picture: (almost) no vector content and no text layer of its own."""
    return len(pages) > 1 and max(pg["vector_bytes"] for pg in pages) < 20_000 and \
        max(pg.get("own_chars", pg["chars"]) for pg in pages) < 200


def _scan_text_file(path: str | Path) -> Path:
    from stoa.formats.normalize import _cache_dir

    src = Path(path)
    return Path(_cache_dir(src)) / f"{src.stem}.pagetext.json"


def _scan_key(path: str | Path) -> list:
    st = Path(path).stat()
    return [SCAN_TEXT_VERSION, st.st_size, st.st_mtime_ns]


def cached_scan_texts(path: str | Path) -> dict[int, str]:
    """{page: text} read earlier for this scanned set ({} when none, or the file changed since)."""
    import json

    try:
        d = json.loads(_scan_text_file(path).read_text())
        if d.get("key") != _scan_key(path):
            return {}
        return {int(k): v for k, v in d.get("texts", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def read_scan_texts(path: str | Path, limit: int | None = None) -> dict[int, str]:
    """Read every scanned page's text (<= limit pages, default SCAN_TEXT_PAGES; none when the set is bigger) and cache
    it. A page that cannot be read gets ''. Cost: one raster text layer per page (OCR for scans), billed by it."""
    import json

    from stoa.formats.normalize import _cache_dir, scan_png, tmp_path

    got = cached_scan_texts(path)
    pages = page_scores(path)
    if not scanned_set(pages) or len(pages) > (SCAN_TEXT_PAGES if limit is None else limit):
        return got
    if len(got) == len(pages):
        return got
    from stoa.solvers.raster import textlayer as T

    texts = dict(got)
    f = _scan_text_file(path)
    for k in range(len(pages)):
        if k in texts:
            continue
        try:
            copy = one_page_copy(path, k, _cache_dir(Path(path)))
            scan = scan_png(copy)
            texts[k] = "\n".join(it.text for it in T.text_layer(scan[0]).items) if scan else ""
        except Exception:  # noqa: BLE001 — that page keeps 'no text'
            texts[k] = ""
        try:  # after EVERY page: a warm stopped by its caller's timeout keeps what it read, the next reader goes on
            tmp = tmp_path(f)
            tmp.write_text(json.dumps({"key": _scan_key(path), "texts": {str(k): v for k, v in texts.items()}}))
            os.replace(tmp, f)
        except OSError:
            pass
    return texts


def classify_scan_page(path: str | Path, page: int):
    """T3's project loop on a SCANNED set (captain 18:50: '8 floor plans as a scan'): stoa.sheet.sets.classify reads a PDF
    page's own text, which a scan does not have, so every page came out 'level not read'. This classifies the page from
    what the upload warm-up already read (its scan PNG's cached text layer, else the page text in <stem>.pagetext.json):
    a stoa.sheet.sets.Sheet with file / page of the ORIGINAL set, or None when nothing was read yet. Never renders or
    reads anything (no 20-60 s cold read): call it after the warm (or again when it lands)."""
    from stoa.formats.normalize import _cache_dir
    from stoa.sheet import sets as S

    src = Path(path)
    cdir = Path(_cache_dir(src))
    png = cdir / f"{src.stem}.p{int(page) + 1}.scan.png"
    if not png.exists() and int(page) == 0:
        png = cdir / f"{src.stem}.scan.png"  # a one-page scan: normalize.scan_png's own copy
    sh = None
    if png.exists():
        try:
            from stoa.solvers.raster.sheet import cached_layer

            layer = cached_layer(str(png))
            if layer is not None:
                sh = S.classify(str(png), 0, layer=layer)
        except Exception:  # noqa: BLE001 — fall back to the page text below
            sh = None
    text = cached_scan_texts(path).get(int(page)) or ""
    if sh is None:
        if not text:
            return None
        sh = S.Sheet(file=str(path), page=int(page), confidence=0.4)
        kind = _sheet_kind(text)
        sh.kind = {"plan": "floor_plan", "elev": "elevation"}.get(kind, kind) if kind else "unknown"
    sh.file, sh.page = str(path), int(page)
    if sh.storey is None and text:
        name = sheet_storey(text)
        if name:
            k, nm = S._storey_of(name if re.search(r"\b(?:floor|level|storey|story|plan)\b", name, re.I)
                                 else f"{name} floor")
            if k is not None:
                sh.storey, sh.storey_name = k, nm or name
    return sh


def _with_scan_texts(path: str | Path, pages: list[dict]) -> list[dict]:
    """page_scores rows with a scanned page's cached text in place of its (empty) own text."""
    if not scanned_set(pages):
        return pages
    texts = cached_scan_texts(path)
    for pg in pages:
        t = texts.get(pg["page"])
        if t and pg["own_chars"] < 20:
            pg.update(text=t, chars=len(t), areas=len(_AREA.findall(t)), room_numbers=len(_ROOMNO.findall(t)),
                      scan_text=True)
    return pages


_QTY_Q = re.compile(r"\b(?:how\s+(?:many|much|big|large|long|wide|tall|high|deep)|area|size|length|width|depth|"
                    r"perimeter|distance|count|number\s+of|total|sum|m²|m2|sq\.?\s*(?:m|ft)|square|metres?|meters?|"
                    r"feet|ft)\b", re.I)
_HEIGHT_Q = re.compile(r"\b(?:how\s+(?:tall|high)|height|ridge|eaves|hoch|höhe|korkeus|hauteur|altezza)\b", re.I)
_PLAN_TITLE = re.compile(r"\b(?:floor|level|storey|story|ground|first|second|third|basement|attic|\d+(?:st|nd|rd|th))\b"
                         r"[^\n]{0,24}\bplan\b|\bgrundriss|\bpohjapiirustus|\bplan d['’]étage|\bplanta\b", re.I)
_NOT_PLAN = re.compile(r"ceiling|electrical|lighting|plumbing|mechanical|framing|foundation|roof|site|landscape|detail|"
                       r"demolition|structural", re.I)
_ELEV_TITLE = re.compile(r"\b(?:elevation|section|ansicht|schnitt|julkisivu|leikkaus|coupe|façade|facade|sezione)\b",
                         re.I)


_INDEX_LINE = re.compile(r"(?mi)^\s*[A-Z]{1,2}-?\d{1,3}(?:\.\d{1,2})?\s+[^\n]{0,40}\b(?:plan|elevation|section|notes|"
                         r"details?|schedule|information|cover)\b")


_INDEX_WORDS = re.compile(r"sheet\s+index|drawing\s+(?:list|index)|list\s+of\s+drawings|\bcover\b|blattverzeichnis|"
                          r"planverzeichnis|piirustusluettelo|liste\s+des\s+plans", re.I)


def _sheet_kind(text: str) -> str | None:
    """'plan' when a short title line of the page names a floor plan (not a ceiling / electrical / site / roof /
    foundation / detail plan), 'elev' for elevations and sections, else None. Title = a line of <= 60 characters."""
    if len(_INDEX_LINE.findall(text)) >= 4 or _INDEX_WORDS.search(text):  # a cover / sheet index lists every
        return None                                                        # sheet's title ('A1.2 1ST FLOOR PLAN')
    plan = elev = False
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or len(ln) > 60:
            continue
        if _PLAN_TITLE.search(ln) and not _NOT_PLAN.search(ln):
            plan = True
        elif _ELEV_TITLE.search(ln) and not re.search(r"detail|see\b", ln, re.I):
            elev = True
    return "plan" if plan else "elev" if elev else None


def floor_plan_pages(path: str | Path) -> list[tuple[int, str]]:
    """[(0-based page, storey title)] of a drawing SET's floor-plan sheets, one per storey ('1ST', '2ND'), when there are
    at least two and every one names a different storey; else []. Two sheets of the same storey (a furniture plan
    next to the floor plan) or a floor plan without a storey title -> [] (a sum over them could count a room twice)."""
    plan = [(pg["page"], sheet_storey(pg["text"]) or "") for pg in page_scores(path) if _sheet_kind(pg["text"]) == "plan"]
    names = [re.sub(r"\W+", " ", s).strip().upper() for _, s in plan]
    if len(plan) < 2 or not all(names) or len(set(names)) != len(names):
        return []
    return plan


def _printed_big(path: str | Path, page: int, name: str, ratio: float = 2.0) -> bool:
    """The sheet number `name` printed at least `ratio` x the page's median text size: the sheet's OWN number in its
    title block (USACE AW 078-24-28: 'M-101A' 28.5 pt on its sheet vs 11.5 pt in the set's sheet index; S-100F 27.9 pt;
    AFCEC 'A-2' 14.6 pt vs 8.4 pt on the cover), not a reference ('SEE SHEET M-504') or an index row."""
    import statistics

    import pymupdf

    try:
        with pymupdf.open(path) as d:
            spans = [sp for b in d[page].get_text("dict").get("blocks", []) for ln in b.get("lines", [])
                     for sp in ln.get("spans", []) if sp.get("text", "").strip()]
    except Exception:  # noqa: BLE001
        return False
    if not spans:
        return False
    med = statistics.median(sp["size"] for sp in spans)
    return any(sp["size"] >= ratio * med and len(sp["text"].strip()) <= len(name) + 12 and _token(name, sp["text"])
               for sp in spans)


def sheet_id(path: str | Path, page: int, ratio: float = 2.0) -> str | None:
    """The page's own sheet number ('M-101A'): the largest span that is a sheet number printed >= `ratio` x the page's
    median text size (title block); None when the page prints none."""
    import statistics

    import pymupdf

    try:
        with pymupdf.open(path) as d:
            spans = [sp for b in d[page].get_text("dict").get("blocks", []) for ln in b.get("lines", [])
                     for sp in ln.get("spans", []) if sp.get("text", "").strip()]
    except Exception:  # noqa: BLE001
        return None
    if not spans:
        return None
    med = statistics.median(sp["size"] for sp in spans)
    ids = [sp for sp in spans if sp["size"] >= ratio * med and _SHEET.fullmatch(sp["text"].strip())]
    return max(ids, key=lambda sp: sp["size"])["text"].strip() if ids else None


def related(path: str | Path, question: str, exclude: int = -1, n: int = 3) -> list[tuple[int, str | None]]:
    """Up to n other pages whose text carries the question's RARE words (each word weighted by log(pages / pages that
    have it): 'suction' on 5 of 156 pages counts, 'check' on 155 does not), with their own sheet numbers. For the note on
    a big set when the question names no sheet (P1 17:58: Stoa's questions on their 156-page source set are ambiguous;
    the right sheet is in this list of 3 for 9 of 17, so the visitor can re-ask naming it)."""
    import math

    pages = page_scores(path)
    N = len(pages)
    if N < 2:
        return []
    low = [pg["text"].lower() for pg in pages]
    words = {w.lower() for w in _WORD.findall(question or "")} - _COMMON
    rel = [0.0] * N
    for w in words:
        stems = [w] + ([w[:-2]] if w.endswith("es") and len(w) > 5 else []) + \
                ([w[:-1]] if w.endswith("s") and len(w) > 4 else [])
        has = [any(x in t for x in stems) for t in low]
        df = sum(has)
        if df:
            idf = math.log(N / df)
            for i in range(N):
                if has[i]:
                    rel[i] += idf
    import statistics

    vmed = statistics.median(pg["vector_bytes"] for pg in pages)
    out = []
    for i in sorted(range(N), key=lambda i: -rel[i]):
        if len(out) >= n or rel[i] <= 0:
            break
        if i == exclude or pages[i]["vector_bytes"] < vmed:  # notes / index pages carry every word but little drawing
            continue
        sid = sheet_id(path, i)
        if sid and re.match(r"G\d|G-", sid):  # US CAD standard 'G' sheets: general (cover, index, notes)
            continue
        out.append((i, sid))
    return out


_PHRASE_STOP = _COMMON | {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is", "are", "be", "how",
                          "big", "large", "wide", "long", "high", "tall", "does", "do", "did", "need", "we", "you", "i",
                          "me", "give", "tell", "show", "net", "gross", "size", "metres", "meters", "square", "m2", "m",
                          "what's", "whats", "s", "it", "its", "if", "not", "all", "each", "every", "per", "by", "go",
                          "around", "skirting", "baseboard", "perimeter", "linear", "would", "run", "openings",
                          "deducted", "subtract", "doors", "please", "level", "storey", "story"}


def _phrases(q: str) -> set[str]:
    """Name-like word runs of the question ('Science Lab 3', 'PE Store', 'Learning Resource Centre'): 2-4 words, content
    words at both ends, at least one capitalised (X1 / G1 18:35-18:56: 'Science Lab 3' printed on one page of a 3-page set
    lost to 'the most drawing' because 'science' and 'lab' alone are on every page or none)."""
    toks = re.findall(r"[^\W_][\w\-]*(?:\.\d+)?", q or "", re.U)
    out = set()
    for n in (4, 3, 2):
        for i in range(len(toks) - n + 1):
            g = toks[i:i + n]
            if g[0].lower() in _PHRASE_STOP or g[-1].lower() in _PHRASE_STOP:
                continue
            if any(t.lower() in ("of", "the", "is", "are", "and", "or") for t in g[1:-1]):
                continue
            if not any(t[0].isupper() for t in g if t[0].isalpha()):
                continue
            out.add(" ".join(g))
    return out


def _phrase_in(phrase: str, text: str) -> bool:
    """The phrase as whole tokens with any whitespace between ('Lab 3' is not 'Lab 30')."""
    rx = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.search(rf"(?<![\w.]){rx}(?![\w.])", text, re.I) is not None


_SCHED_Q = re.compile(r"\b(?:(door|window|room|finish(?:es)?|hardware|fixture|equipment|lighting|luminaire|panel|"
                      r"plumbing|opening|area|wall|partition|frame|glazing|louver|louvre)\s+)?schedules?\b", re.I)
_SEE = re.compile(r"\b(?:see|refer|ref\.?|per|as\s+(?:per|shown))\b", re.I)


def schedule_title(question: str, text: str) -> str | None:
    """'DOOR SCHEDULE' when the question names a schedule ('the door schedule', 'according to the schedule') and this
    page's text has a title line that starts with that schedule's name (an optional sheet number first: 'A-602 DOOR
    SCHEDULE'); cover / index pages and 'SEE DOOR SCHEDULE' notes do not count. Else None."""
    m = _SCHED_Q.search(question or "")
    if not m or len(_INDEX_LINE.findall(text or "")) >= 4 or _INDEX_WORDS.search(text or ""):
        return None
    noun = m.group(1)
    head = re.compile(r"^\W*(?:[A-Z]{1,3}-?\d{1,4}(?:\.\d+)?[A-Z]?\s+)?" + (re.escape(noun.rstrip("s")) + r"s?\s+"
                      if noun else r"(?:[A-Za-z]+\s+){0,2}") + r"schedules?\b", re.I)
    if any(head.search(ln) and not _SEE.search(ln) for ln in (text or "").splitlines() if 0 < len(ln.strip()) <= 60):
        return (f"{noun} schedule" if noun else "schedule").upper()
    return None


def _schedule_pages(pages: list[dict], q: str) -> tuple[set[int], str] | None:
    """({pages}, 'DOOR SCHEDULE'): the pages whose title is the schedule the question names, when some but not all do."""
    hit = {pg["page"]: t for pg in pages if (t := schedule_title(q, pg["text"]))}
    if not hit or len(hit) == len(pages):
        return None
    return set(hit), hit[min(hit)]


def pick_page(path: str | Path, question: str = "") -> tuple[int, str]:
    """(0-based page, reason). One-page files -> (0, 'single page')."""
    pages = page_scores(path)
    if len(pages) <= 1:
        return 0, "single page"
    q = question or ""
    read_note = ""
    if q.strip() and scanned_set(pages) and len(cached_scan_texts(path)) < len(pages) and \
            len(pages) <= SCAN_TEXT_ASK_PAGES and os.environ.get("STOA_SCAN_TEXT", "1") != "0":
        read_scan_texts(path)  # the first question on a scanned set that warm did not (fully) read
        pages = page_scores(path)
    if any(pg.get("scan_text") for pg in pages):
        read_note = " (scanned pages: text read from their images)"
    # 0. X1 21:22: 'according to the door schedule …' -> the page whose title is that schedule (A-602 DOOR SCHEDULE),
    #    not the plan page that prints the module names more often
    sched = _schedule_pages(pages, q)
    if sched and len(sched[0]) == 1:
        k = next(iter(sched[0]))
        return k, f"page {k + 1} prints the {sched[1]} the question names{read_note}"
    # 1. what the question names: a sheet number, a quoted name, or a distinctive word found on only some pages
    named = set(_SHEET.findall(q)) | set(re.findall(r"[\"'“„«‘]([^\"'”“»’]{1,60})[\"'”“»’]", q))
    inner = {m for n in named for m in re.findall(r"\(([^()]{1,20})\)", n)}  # 'Bathroom (1.5)' prints '1.5' alone
    words = {w.lower() for w in _WORD.findall(q)} - _COMMON
    # P1 19:55 (scanned set): 'the three MFL classrooms' -> 'MFL' (2-4 capitals) is a name too short for _WORD
    acronyms = {a for a in re.findall(r"\b[A-Z]{2,4}\b", q) if a.lower() not in _COMMON} - named
    big = {n: {pg["page"] for pg in pages if _token(n, pg["text"]) and _printed_big(path, pg["page"], n)}
           for n in named if _SHEET.fullmatch(n)}  # 'on sheet M-101A' -> the page whose title block prints it
    phrase_pages = {ph: {pg["page"] for pg in pages if _phrase_in(ph, pg["text"])} for ph in _phrases(q) - named}
    phrase_pages = {ph: ps for ph, ps in phrase_pages.items() if len(ps) == 1}  # printed on exactly ONE page
    want = _HEIGHT_Q.search(q) and "elev" or "plan"  # the kind of sheet the question is about
    kinds = [_sheet_kind(pg["text"]) for pg in pages]
    has_kind = want in kinds

    def stems(w: str) -> tuple[str, ...]:  # 'bedrooms' finds 'BEDROOM', 'porches' 'PORCH', 'risers' 'RISER'
        out = [w]
        if w.endswith("es") and len(w) > 5:
            out.append(w[:-2])
        if w.endswith("s") and len(w) > 3 and not w.endswith("ss"):  # 'labs' -> 'lab' (not 'glass' -> 'glas')
            out.append(w[:-1])
        return tuple(out)

    def found(w: str, pg) -> bool:
        return any(x in pg["text"].lower() for x in stems(w))

    # a question word printed on exactly ONE page counts twice there (P1's scanned set: 'maths classrooms' -> the
    # only page printing 'Maths 1'..'Maths 4'; one match alone was below the 2 this rule needs)
    lone = {w for w in words if sum(1 for pg in pages if found(w, pg)) == 1}
    lone |= {a for a in acronyms if sum(1 for pg in pages if _token(a, pg["text"])) == 1}

    def hits(pg) -> int:
        low = pg["text"].lower()
        titles = [ln for ln in pg["text"].splitlines() if _TITLE.search(ln)]
        score = 0
        for n in named:
            if pg["page"] in big.get(n, ()):  # its own sheet number, printed big in the title block
                score += 8
            if any(_token(n, ln) for ln in titles):  # the sheet's title names it ('Floor plan: 0', '… 2. Level')
                score += 6
            elif len(n) >= 4 and n.lower() in low:
                score += 3
            elif _token(n, pg["text"]):
                score += 1
        score += sum(2 for m in inner if _token(m, pg["text"]))
        score += 6 if any(pg["page"] in ps for ps in phrase_pages.values()) else 0  # a name printed only here
        score += sum(1 for w in words if any(x in low for x in stems(w)))
        score += sum(1 for a in acronyms if _token(a, pg["text"]))
        score += sum(1 for w in lone if (found(w, pg) if w in words else _token(w, pg["text"])))
        if sched and pg["page"] in sched[0]:  # the schedule the question names, continued on several pages
            score += 8
        if has_kind and kinds[pg["page"]] == want:  # X1 farm set: 'How long is the house?' went to the GENERAL
            score += 2                               # NOTES page ('house' x many), 'bedrooms' to the cover
        return score

    ranked = sorted(pages, key=hits, reverse=True)
    h1, h2 = hits(ranked[0]), hits(ranked[1])
    top_pg = ranked[0]
    drawn = [pg for pg in pages if pg["vector_bytes"] >= 50_000 or pg["areas"] >= 10]
    if h1 < 6 and _QTY_Q.search(q) and drawn and top_pg not in drawn and top_pg["vector_bytes"] < 20_000 and \
            not top_pg["areas"] and not top_pg.get("scan_text"):
        # S0 21:16 (d27, up_fmt2 pdf3_last): a COVER page ('APARTMENT BUILDING - TENDER DRAWINGS / 1 Cover 2 Legend 3
        # Floor plan') won a quantity question with 2 weak word matches over the plan page (203 kB, 48 area labels)
        # whose labels are French. Weak matches on a page with no drawing never beat a page that carries one.
        h1 = 0
    if h1 >= 2 and h1 > h2:  # one page clearly names it (ties: fall back to the drawing content)
        return ranked[0]["page"], (f"page {ranked[0]['page'] + 1} names what the question asks about ({h1} matches)"
                                   f"{read_note}")
    # 2. an everyday storey mention ('on the ground floor', 'level 2', 'im 1. OG') against each page's title storey
    labels = [sheet_storey(pg["text"]) for pg in pages]
    if len({x for x in labels if x}) >= 2 and not (want == "elev" and has_kind):  # 'roof ridge' is not a storey
        try:
            from stoa.solvers.ifc.parse import storey_mention  # W1's storey matcher (ordinals, ground-floor words)

            idx, swhy = storey_mention(q, [x or "" for x in labels])
        except Exception:  # noqa: BLE001
            idx = None
        if idx is None and _BASEMENT_Q.search(q):  # storey_mention knows ground-floor names, not basement ones
            hit = [i for i, x in enumerate(labels) if x and _BASEMENT_NAME.search(x)]
            if len(hit) == 1:
                idx, swhy = hit[0], f"'basement' -> storey '{labels[hit[0]]}'"
        if idx is not None:
            return idx, f"page {idx + 1} is the sheet of storey '{labels[idx]}' ({swhy}){read_note}"
    # 3. the page with the most drawing on it
    vmax = max(pg["vector_bytes"] for pg in pages) or 1
    if scanned_set(pages):
        # a SCANNED set (every page one image, no text layer): the plan is the page with the most ink
        ink = _ink(path)
        if ink and max(ink) > 0:
            cand = [i for i in range(len(ink)) if not has_kind or kinds[i] == want] or list(range(len(ink)))
            k = max(cand, key=lambda i: ink[i])
            return k, (f"page {k + 1} of {len(pages)} carries the most drawing (scanned set: {100 * ink[k]:.1f}% of "
                       "the page inked, the most of any page)")

    def score(pg):
        return pg["vector_bytes"] / vmax + 0.02 * min(pg["areas"], 50) + 0.005 * min(pg["room_numbers"], 100)

    pool = [pg for pg in pages if kinds[pg["page"]] == want] if has_kind else pages
    top = max(pool or pages, key=score)
    return top["page"], (f"page {top['page'] + 1} of {len(pages)} carries the most drawing "
                         f"({top['vector_bytes'] // 1000} kB of vector content, {top['areas']} area labels)")


def _ink(path: str | Path, dpi: int = 18) -> list[float]:
    """Fraction of dark pixels per page on a tiny grey render (a 37-page set takes well under a second)."""
    import pymupdf

    out = []
    with pymupdf.open(path) as d:
        for p in d:
            pix = p.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            data = pix.samples
            out.append(sum(1 for v in data if v < 200) / max(len(data), 1))
    return out


def one_page_copy(path: str | Path, page: int, out_dir: str | Path | None = None) -> str:
    """A PDF holding only `page` (same size and coordinates), next to the original unless out_dir is given."""
    import fitz

    src = Path(path)
    dst = Path(out_dir or src.parent) / f"{src.stem}.p{page + 1}.pdf"
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        with fitz.open(src) as d:
            out = fitz.open()
            out.insert_pdf(d, from_page=page, to_page=page)
            from stoa.formats.normalize import tmp_path

            tmp = tmp_path(dst)  # X1 19:24: atomic, a parallel question never opens a half-written copy
            out.save(tmp, garbage=3, deflate=True)
            os.replace(tmp, dst)
    side = dst.with_name(dst.name + ".json")  # DR 21:57: '<copy>.json' {original, page} -> openings finds the set's
    if not side.exists():                     # DOOR SCHEDULE page even when the copy lives in a cache dir
        try:
            import json

            from stoa.formats.normalize import tmp_path

            tmp = tmp_path(side)
            tmp.write_text(json.dumps({"original": str(src.resolve()), "page": int(page)}))
            os.replace(tmp, side)
        except OSError:
            pass
    return str(dst)


def copy_origin(copy: str | Path) -> tuple[str, int] | None:
    """(original set, 0-based page) of a one_page_copy, from its '<copy>.json' sidecar; None for any other file."""
    import json

    try:
        d = json.loads(Path(str(copy) + ".json").read_text())
        return str(d["original"]), int(d["page"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
