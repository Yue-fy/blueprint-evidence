"""Ground an evidence-less answer in the PDF's own text layer (E1).

A VLM or agent answer on a vector PDF ("4000 PSI", "12'-8\"", "05.03.2017", "71") usually is text printed on the
sheet. We look for the value in the page words and keep the hit nearest to words of the question ("compressive",
"strength", "slabs"), so a human sees where it is printed:

    "'4000 PSI' printed here, near 'compressive' (text search; check it is the line the answer means)"

The place is the printed text's own box; whether it is THE line the answer came from is a match, so the item is
marked exact=False (drawn dashed). No model call, no value or confidence change. Skipped for long free text,
yes/no, 'unknown'-style values and numbers that only occur far from every question word.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from stoa.contract import Answer, Evidence
from stoa.data import abspath

MAX_PAGES = 40
NEAR_PT = 250.0  # a value hit counts when a question word is printed within this distance (pt)
_STOP = set("""what which where when who whom whose how many much is are was were be been the of on in at for this that
these those does do did with from by to and or a an as it its there their have has had per into than then any all each
plan sheet drawing drawings floor shown show shows labelled labeled given used total number value please quick one hey
site colleague said earlier trust answer metres meters feet square type types level levels room rooms area areas
building buildings drawn count required used labelled level""".split())
_NO_VALUE = re.compile(r"^\s*(?:yes|no|unknown|none|not (?:available|specified|shown|given)|n/?a|-)\s*$", re.I)


def _num_variants(v: float) -> list[str]:
    out = []
    if abs(v - round(v)) < 1e-9:
        n = int(round(v))
        out += [str(n), f"{n:,}", f"{n}.0", f"{n},0"]  # '78.0 m²' / '78,0 m²' print a whole number too
    else:
        for d in (2, 1, 3):
            s = f"{v:.{d}f}".rstrip("0").rstrip(".") if d != 2 else f"{v:.2f}"
            out += [s, s.replace(".", ",")]
    return list(dict.fromkeys(out))


def _value_strings(value) -> list[str]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return _num_variants(float(value))
    s = str(value).strip()
    if not s or len(s) > 40 or _NO_VALUE.match(s) or "\n" in s:
        return []
    out = [s]
    if re.search(r"\d'\s*-\s*\d", s):  # 12'-8" vs 12' - 8"
        out += [re.sub(r"'\s*-\s*", "'-", s), re.sub(r"'\s*-\s*", "' - ", s)]
    try:
        out += _num_variants(float(s.replace(",", "")))
    except ValueError:
        pass
    return list(dict.fromkeys(out))


@functools.lru_cache(maxsize=64)
def _pages(pdf: str) -> tuple:
    import pymupdf

    out = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PAGES:
                break
            out.append(tuple((w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")))
    return tuple(out)


def _keywords(question: str, values: list[str]) -> list[str]:
    toks = re.findall(r"[^\W\d_][\w-]{3,}", question.lower())
    vals = {v.lower() for v in values}
    return list(dict.fromkeys(t for t in toks if t not in _STOP and t not in vals))


ROW_KEY = bool(int(__import__("os").environ.get("E1_ROW_KEY", "1")))


def _row_keys(question: str, values: list[str]) -> list[str]:
    """Codes the question names that a schedule prints as a row key: '7.04A', 'D12', 'W-03', '1.01' (a digit plus a
    letter or a dot / dash; not the answer itself)."""
    vals = {_clean(v) for v in values}
    out = []
    for t in re.findall(r"(?<![\w.])[A-Za-z]{0,3}-?\d+(?:[.\-]\d+)*[A-Za-z]?(?![\w])", question):
        c = _clean(t)
        if c in vals or not re.search(r"\d", c) or not (re.search(r"[a-z]", c) or re.search(r"\d[.\-]\d", c)):
            continue
        out.append(c)
    return list(dict.fromkeys(out))


def _clean(w: str) -> str:
    return w.strip(".,;:()[]{}\"'").lower()


def _hits(words, values: list[str]) -> list[tuple[tuple[float, float, float, float], str]]:
    """Value occurrences on one page: single words, or runs of consecutive words for multi-word values."""
    out = []
    for v in values:
        parts = [p.lower() for p in v.split()]
        n = len(parts)
        for i in range(len(words) - n + 1):
            seq = words[i:i + n]
            if n == 1:
                ok = _clean(seq[0][4]) == parts[0].strip(".,;:()[]{}\"'").lower() or seq[0][4].lower() == parts[0]
            else:
                ok = all(_clean(seq[j][4]) == parts[j].strip(".,;:()[]{}\"'").lower() for j in range(n))
            if ok:
                out.append(((min(w[0] for w in seq), min(w[1] for w in seq), max(w[2] for w in seq),
                             max(w[3] for w in seq)), v))
    return out


def _converted(value, unit: str | None) -> list[str]:
    """The value as the sheet may print it in other units: 102.38 m² ← '1102' SF, 3.05 m ← '10' ft (whole numbers or
    one/two decimals only, so a conversion never invents precision)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return []
    u = (unit or "").lower()
    k = {"m2": 1 / 0.09290304, "m²": 1 / 0.09290304, "ft2": 0.09290304, "sqft": 0.09290304, "m": 1 / 0.3048,
         "ft": 0.3048}.get(u)
    if not k:
        return []
    x = v * k
    out = []
    for d in (0, 1, 2):
        r = round(x, d)
        if abs(r - x) <= 0.006 * max(abs(x), 1):  # the printed number rounds to this value
            out += _num_variants(r)
            break
    return out


IMG_EXT = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")
A3_PT = 1190.0  # an image is treated as an A3 sheet for the 'near a question word' distances


def _image_words(path: str) -> tuple[tuple, float] | None:
    """((words,), px per pt) of a raster drawing's text layer (OCR or VLM lines, image_px boxes, each line split into
    words by character position) — ONLY a layer already read in this process by the solver that answered, so
    grounding never pays for a model call. None when no layer is in memory."""
    from stoa.solvers.raster import textlayer as T

    tag = T.file_tag(path)
    heads = {f"{p}{tag}|" for p in (str(path), str(abspath(path)))}
    tl = next((v for k, v in list(T._mem.items()) if any(k.startswith(h) for h in heads)), None)
    if tl is None or not tl.items:
        return None
    words = []
    for it in tl.items:
        text = str(it.text or "")
        x0, y0, x1, y1 = (float(v) for v in it.box)
        n = max(len(text), 1)
        for m in re.finditer(r"\d+(?:[.,]\d+)*|[^\W\d_]+|[^\w\s]", text):  # OCR glues '78.0²-76.5': split it
            words.append((x0 + (x1 - x0) * m.start() / n, y0, x0 + (x1 - x0) * m.end() / n, y1, m.group(0)))
    return (tuple(words),), max(tl.size) / A3_PT


_UNIT_WORD = {"area": re.compile(r"^(?:m2|m²|²|㎡|qm|sqm|sf|sq|ft2|ft²|m\^2)\.?$", re.I),
              "length": re.compile(r"^(?:m|mm|cm|ft|'|\")\.?$", re.I)}


def _unit_after(words, box, unit: str | None) -> bool:
    """A word right after `box` on the same line is a unit of the answer's kind (area units for m² / ft² answers)."""
    u = (unit or "").lower()
    kind = "area" if u in ("m2", "m²", "ft2", "sqft", "sf") else "length" if u in ("m", "mm", "cm", "ft") else None
    if kind is None:
        return False
    h = max(box[3] - box[1], 1e-6)
    cy = (box[1] + box[3]) / 2
    for w in words:
        if abs((w[1] + w[3]) / 2 - cy) <= h / 2 and box[2] - 0.2 * h <= w[0] <= box[2] + 1.5 * h and \
                _UNIT_WORD[kind].match(w[4].strip(",;:()")):
            return True
    return False


def ground(path: str, question: str, value, unit: str | None = None) -> list[Evidence]:
    """[Evidence] for the printed value nearest to the question's words, or [] when there is no convincing hit.
    With `unit`, the value converted to the sheet's other unit system is searched too (m² ↔ SF, m ↔ ft).
    Vector PDFs: the page words; raster drawings: the text layer the answering solver already read (no new call)."""
    ext = Path(path).suffix.lower()
    if ext != ".pdf" and ext not in IMG_EXT:
        return []
    values = list(dict.fromkeys([*_value_strings(value), *_converted(value, unit)]))
    if not values:
        return []
    space, f = "pdf_pt", 1.0
    try:
        if ext == ".pdf":
            pages = _pages(str(abspath(path)))
        else:
            got = _image_words(path)
            if got is None:
                return []
            (pages, f), space = got, "image_px"
    except Exception:  # noqa: BLE001
        return []
    kws = _keywords(question, values)
    try:  # a non-English question on an English sheet: add the words of its translation (memoised, 1 local call)
        from stoa.llm import lang_guess, to_english

        if lang_guess(question) != "en" and any(_hits(w, values) for w in pages):
            tr = to_english(question, force=True)
            if tr.translated and tr.ok:
                kws += [k for k in _keywords(tr.text, values) if k not in kws]
    except Exception:  # noqa: BLE001
        pass
    best = None  # (dist, page, box, value, kw)
    n_hits = 0
    keys = _row_keys(question, values) if ROW_KEY else []
    in_row = None  # (dist, page, box, value, key): a value hit on the same printed line as a row key the question names
    for pi, words in enumerate(pages):
        hits = _hits(words, values)
        n_hits += len(hits)
        if not hits:
            continue
        for key in keys:
            kb = [w for w in words if _clean(w[4]) == key]
            if not 1 <= len(kb) <= 3:
                continue  # a key printed many times is no row key
            for box, v in hits:
                for w in kb:
                    # the row runs along the text: horizontal words share a y band (key left of the value); a schedule
                    # plotted rotated (tall, narrow word boxes: MFC A-602) shares an x band, key either side
                    rot = (w[3] - w[1]) > 1.5 * (w[2] - w[0])
                    a0, a1, b0, b1 = (0, 2, 1, 3) if rot else (1, 3, 0, 2)
                    t = max(box[a1] - box[a0], 1e-6)  # the text's height across the line
                    if abs((w[a0] + w[a1]) / 2 - (box[a0] + box[a1]) / 2) > 0.6 * t:
                        continue
                    if not rot and w[2] > box[0] + 0.5 * t:
                        continue
                    dx = max(box[b0] - w[b1], w[b0] - box[b1], 0.0)
                    if in_row is None or dx < in_row[0]:
                        in_row = (dx, pi, box, v, w[4])
        kw_boxes = [(w, k) for w in words for k in kws if _clean(w[4]).startswith(k[:6])]
        for box, v in hits:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            for w, k in kw_boxes:
                d = ((cx - (w[0] + w[2]) / 2) ** 2 + (cy - (w[1] + w[3]) / 2) ** 2) ** 0.5
                if best is None or d < best[0]:
                    best = (d, pi, box, v, k)
    if in_row is not None:
        # F8 01:12: 'What is the height of door 7.04A?' -> the 7'-0" in 7.04A's schedule row, not the first 7'-0" on the
        # sheet (row 1.01A) that happened to sit nearest the word 'height' (the column header)
        _d, pi, box, v, key = in_row
        return [Evidence(file=path, page=pi if space == "pdf_pt" else None, bbox=[round(x, 2) for x in box],
                         bbox_space=space, exact=False,
                         note=f"'{v}' printed here, in the row of '{key}' (text search on the sheet; {n_hits} place(s) "
                              "print it; check it is the column the answer means)")]

    def distinctive(v: str) -> bool:  # a string that is unlikely to be printed by chance
        return bool(re.search(r"[^\W\d_]", v) and re.search(r"\d", v)) or len(v.split()) >= 2 or len(v) >= 6

    if best is not None:
        v0 = best[3]
        near = f * (NEAR_PT if len(v0) >= 4 or not v0.replace(".", "").replace(",", "").isdigit() else
                    (150.0 if len(v0) == 3 else 80.0))
        if best[0] > near or (len(v0) <= 2 and n_hits > 3):
            best = None
    if best is None:
        # a distinctive value printed at most 3 times needs no nearby question word (translated questions; a sheet
        # in another language): a value with letters, several words or 6+ characters, or a number printed with its
        # unit right after it ('78.0 m²', '78.0²-76.5' as OCR reads it)
        for pi, words in enumerate(pages):
            hits = [h for h in _hits(words, values) if distinctive(h[1]) or _unit_after(words, h[0], unit)]
            if hits and n_hits <= 3:
                box, v = hits[0]
                return [Evidence(file=path, page=pi if space == "pdf_pt" else None, bbox=[round(x, 2) for x in box],
                                 bbox_space=space, exact=False,
                                 note=f"'{v}' printed here (text search on the sheet; {n_hits} place(s) print it; "
                                      "check it is the line the answer means)")]
        return []
    d, pi, box, v, k = best
    return [Evidence(file=path, page=pi if space == "pdf_pt" else None, bbox=[round(x, 2) for x in box],
                     bbox_space=space, exact=False,
                     note=f"'{v}' printed here, near '{k}' (text search on the sheet; {n_hits} place(s) print it; "
                          "check it is the line the answer means)")]


def attach_text_evidence(path: str, question: str, ans: Answer) -> bool:
    if ans.value is None or ans.evidence:
        return False
    ev = ground(path, question, ans.value, ans.unit)
    if not ev:
        return False
    ans.evidence = ev
    where = f"on page {ev[0].page + 1}" if ev[0].page is not None else "in the image's text layer (already read)"
    ans.trace = [*ans.trace, f"evidence (E1): the answer '{ans.value}' found printed {where} near "
                             "a question word (text search, no model call)"]
    return True


TEXT_SOLVERS = re.compile(r"(?:^|/)(?:pdf_text|agent|gemini_agent|vlm_direct|nemotron\w*|kg_qa)$")


def _box_has_value(ev: Evidence, values: list[str]) -> bool:
    try:
        pages = _pages(str(abspath(ev.file)))
    except Exception:  # noqa: BLE001
        return True  # can't tell: leave the answer alone
    pi = ev.page or 0
    if pi >= len(pages) or not ev.bbox:
        return False
    x0, y0, x1, y1 = ev.bbox
    for box, _ in _hits(pages[pi], values):
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
            return True
    return False


def ground_printed_value(path: str, question: str, ans: Answer) -> bool:
    """A number read from a sheet's TEXT (pdf_text / agent / VLM) whose evidence boxes don't contain it (e.g. a rebuilt
    table row 'U102 - 741 SF A 7/16\" U120' for 'How big is unit U120?' = 1102 SF): put the box where the value is
    printed next to the question's words first ('U120 - 1102 SF'); keep the solver's own boxes after it."""
    if ans.value is None or not ans.evidence or Path(str(path)).suffix.lower() != ".pdf" or \
            not TEXT_SOLVERS.search(ans.solver or ""):
        return False
    try:
        float(ans.value)
    except (TypeError, ValueError):
        return False
    values = list(dict.fromkeys([*_value_strings(ans.value), *_converted(ans.value, ans.unit)]))
    pdf_items = [e for e in ans.evidence if e.bbox and e.bbox_space == "pdf_pt"]
    if not values:
        return False
    if any(_box_has_value(e, values) for e in pdf_items):
        return _tighten(question, ans, values, pdf_items)
    ev = ground(str(path), question, ans.value, ans.unit)
    if not ev:
        return False
    ans.evidence = [*ev, *ans.evidence]
    ans.trace = [*ans.trace, f"evidence (E1): none of the solver's boxes contains the value; '{ans.value}' is printed on "
                             f"page {ev[0].page + 1} near a question word — that place is shown first"]
    return True


WIDE_PT = 400.0


def _tighten(question: str, ans: Answer, values: list[str], items: list[Evidence]) -> bool:
    """A solver box that contains the value but is a long band (a rebuilt table row across the sheet): put a tight box
    around the printed value and the question word next to it ('U120 … 1102') first. exact=True: the value is printed
    there, inside the row the solver itself cited."""
    for e in items:
        if not e.bbox or e.bbox[2] - e.bbox[0] < WIDE_PT:
            continue
        try:
            words = _pages(str(abspath(e.file)))[e.page or 0]
        except Exception:  # noqa: BLE001
            return False
        x0, y0, x1, y1 = e.bbox
        inside = [w for w in words if x0 - 2 <= (w[0] + w[2]) / 2 <= x1 + 2 and y0 - 2 <= (w[1] + w[3]) / 2 <= y1 + 2]
        hits = _hits(inside, values)
        kws = _keywords(question, values)
        best = None
        for box, v in hits:
            for w in inside:
                if any(_clean(w[4]).startswith(k[:6]) for k in kws):
                    d = abs((box[0] + box[2]) / 2 - (w[0] + w[2]) / 2)
                    if best is None or d < best[0]:
                        best = (d, box, v, w)
        if best is None or best[0] > NEAR_PT:
            continue
        _, box, v, w = best
        tight = [round(min(box[0], w[0]), 2), round(min(box[1], w[1]), 2), round(max(box[2], w[2]), 2),
                 round(max(box[3], w[3]), 2)]
        ans.evidence = [Evidence(file=e.file, page=e.page, bbox=tight, bbox_space="pdf_pt", exact=True,
                                 note=f"'{w[4]}' … '{v}' printed here (inside the cited row)"), *ans.evidence]
        ans.trace = [*ans.trace, f"evidence (E1): the cited row is {round(x1 - x0)} pt wide; the value '{v}' and "
                                 f"'{w[4]}' are boxed tightly first"]
        return True
    return False
