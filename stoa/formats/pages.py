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


def sheet_storey(text: str) -> str | None:
    """The storey a sheet's title names: '… — Floor plan: 2. Level' / 'Grundriss: EG' -> '2. Level' / 'EG';
    'GROUND FLOOR PLAN' -> 'GROUND'. None if no title line says it."""
    for ln in text.splitlines()[:400]:
        m = _PLAN_COLON.search(ln) or _PLAN_TAIL.match(ln)
        if m:
            return m.group(1).strip()
    return None


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
        return [dict(pg) for pg in _SCORES[key]]
    out = []
    with fitz.open(path) as d:
        for i, p in enumerate(d):
            text = p.get_text("text") or ""
            out.append({"page": i, "vector_bytes": _stream_bytes(p), "areas": len(_AREA.findall(text)),
                        "room_numbers": len(_ROOMNO.findall(text)), "chars": len(text), "text": text})
    if key is not None:
        if len(_SCORES) > 64:
            _SCORES.clear()
        _SCORES[key] = out
    return [dict(pg) for pg in out]


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


def pick_page(path: str | Path, question: str = "") -> tuple[int, str]:
    """(0-based page, reason). One-page files -> (0, 'single page')."""
    pages = page_scores(path)
    if len(pages) <= 1:
        return 0, "single page"
    q = question or ""
    # 1. what the question names: a sheet number, a quoted name, or a distinctive word found on only some pages
    named = set(_SHEET.findall(q)) | set(re.findall(r"[\"'“„«‘]([^\"'”“»’]{1,60})[\"'”“»’]", q))
    inner = {m for n in named for m in re.findall(r"\(([^()]{1,20})\)", n)}  # 'Bathroom (1.5)' prints '1.5' alone
    words = {w.lower() for w in _WORD.findall(q)} - _COMMON
    big = {n: {pg["page"] for pg in pages if _token(n, pg["text"]) and _printed_big(path, pg["page"], n)}
           for n in named if _SHEET.fullmatch(n)}  # 'on sheet M-101A' -> the page whose title block prints it
    want = _HEIGHT_Q.search(q) and "elev" or "plan"  # the kind of sheet the question is about
    kinds = [_sheet_kind(pg["text"]) for pg in pages]
    has_kind = want in kinds

    def stems(w: str) -> tuple[str, ...]:  # 'bedrooms' finds 'BEDROOM', 'porches' 'PORCH', 'risers' 'RISER'
        out = [w]
        if w.endswith("es") and len(w) > 5:
            out.append(w[:-2])
        if w.endswith("s") and len(w) > 4:
            out.append(w[:-1])
        return tuple(out)

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
        score += sum(1 for w in words if any(x in low for x in stems(w)))
        if has_kind and kinds[pg["page"]] == want:  # X1 farm set: 'How long is the house?' went to the GENERAL
            score += 2                               # NOTES page ('house' x many), 'bedrooms' to the cover
        return score

    ranked = sorted(pages, key=hits, reverse=True)
    h1, h2 = hits(ranked[0]), hits(ranked[1])
    if h1 >= 2 and h1 > h2:  # one page clearly names it (ties: fall back to the drawing content)
        return ranked[0]["page"], f"page {ranked[0]['page'] + 1} names what the question asks about ({h1} matches)"
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
            return idx, f"page {idx + 1} is the sheet of storey '{labels[idx]}' ({swhy})"
    # 3. the page with the most drawing on it
    vmax = max(pg["vector_bytes"] for pg in pages) or 1
    if vmax < 20_000 and max(pg["chars"] for pg in pages) < 200:
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
            out.save(dst, garbage=3, deflate=True)
    return str(dst)
