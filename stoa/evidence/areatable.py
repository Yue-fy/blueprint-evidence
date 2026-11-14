"""Printed AREA TABLES on a sheet (E1, captain 16:15: 'read printed area tables first'; X1 stoa-like AFCEC dorm A-2).

A sheet may print its own area summary as a table: 'WEST WING DORMITORY = 6,095 SF', 'TOTAL WEST WING = 7,009 SF',
'COMMON SPACE CIRCULATION = 1,240 SF', 'TOTAL SECOND FLOOR = 26,562 SF'. Room-tag readers (area_tags) see only the
values (labels sit to the LEFT on the same row, not stacked above) and answered 'total area of the second floor' with
a sum of unlabelled tags (1,580 m² @0.6 vs the printed 26,562 SF). Here every value is paired with the label on its
row (same text direction, overlapping row band), and a question is answered from the table:

  1. a TOTAL row whose words are all in the question ('total area of the west wing' -> 'TOTAL WEST WING'), when the
     question names no area type, or the TOTAL row names it too ('total common space' -> 'TOTAL COMMON SPACE');
  2. else the rows naming the asked type ('dormitory', 'circulation'), narrowed by compass / wing words in the
     question ('the north wing') — one row is the answer, several are summed ('across all three wings').

Value in the unit asked (SF <-> m²). Evidence = each row's printed label + value box (exact).

    from stoa.evidence.areatable import answer, rows
    answer(pdf_path, question, page=0) -> Answer | None   (None = no area table / no matching row; never raises)
"""

from __future__ import annotations

import functools
import re

from stoa.contract import Answer, Evidence

SOLVER = "area_table"
FT2_M2 = 0.09290304
_VAL = re.compile(r"^(?P<v>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<u>SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|m²|m2|sqm|qm)$", re.I)
_AREA_Q = re.compile(r"\b(area|square (?:feet|foot|met\w*)|sq\.? ?(?:ft|m)|sf|m²|m2|how (?:big|large)|size|footage)\b",
                     re.I)
_SUM_Q = re.compile(r"\b(total|all|across|combined|together|sum|altogether|overall|in total)\b", re.I)
_STOP = {"what", "is", "the", "of", "area", "areas", "how", "big", "large", "in", "square", "feet", "foot", "metres",
         "meters", "metre", "meter", "sf", "ft", "sq", "m2", "m²", "this", "on", "plan", "sheet", "drawing", "a", "an",
         "which", "are", "there", "size", "total", "all", "and", "to", "for", "at", "shown", "do", "does", "have",
         "has", "much", "many", "its", "across", "combined", "together", "sum", "overall", "net", "gross", "printed",
         "according", "table", "be", "by", "with", "per", "that", "it", "subtotal", "footage", "amount", "there's",
         "whats", "what's", "sqm", "sqft", "ft2", "levels", "level", "three", "two", "four", "both", "each", "every", "s"}
_SCOPE = {"floor", "floors", "storey", "story", "level", "building", "wing", "wings", "first", "second", "third",
          "fourth", "ground", "1st", "2nd", "3rd", "4th", "upper", "lower", "basement", "whole", "entire"}
_QUAL = {"north", "south", "east", "west", "northern", "southern", "eastern", "western", "left", "right", "front",
         "rear", "main"}  # narrow table rows to one wing / side; floor ordinals are the sheet's scope (_SCOPE)
_SYN = {"dorm": "dormitory", "dorms": "dormitory", "dormitories": "dormitory", "bedroom": "dormitory",
        "circ": "circulation", "corridor": "circulation", "corridors": "circulation", "hallway": "circulation",
        "hallways": "circulation", "rooms": "room", "spaces": "space", "areas": "area", "1st": "first", "2nd": "second",
        "3rd": "third", "4th": "fourth", "stairs": "stair", "utilities": "utility", "northern": "north",
        "southern": "south", "eastern": "east", "western": "west", "storey": "floor", "story": "floor", "level": "floor"}
_INCL = re.compile(r"\b(?:including|incl\.?|inclusive of|with its|together with|plus its)\b.*$", re.I)


def _norm(w: str) -> str:
    w = w.lower().strip(".,:;()'\"")
    return _SYN.get(w, w)


def _wordset(s: str) -> set[str]:
    # H 19:23: 'm²' was tokenised to 'm' (so _STOP's 'm2' never matched and a question '… in m²?' found no row) and
    # possessives stayed one token ('west wing's' != 'wing')
    s = re.sub(r"(\w)['’]s\b", r"\1", s.replace("²", "2").replace("³", "3"))
    return {_norm(w) for w in re.findall(r"[A-Za-z0-9]+(?:'[a-z]+)?", s)} - {""}


@functools.lru_cache(maxsize=32)
def rows(pdf_abs: str, page: int = 0) -> tuple:
    """((label, value, unit 'ft2'|'m2', bbox), ...) for every printed 'LABEL = value SF' row; () when fewer than 3."""
    import pymupdf

    lines = []
    with pymupdf.open(pdf_abs) as d:
        if page >= len(d):
            return ()
        for b in d[page].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    lines.append((t, tuple(ln["bbox"]), tuple(round(x, 2) for x in ln["dir"])))
    return _pair(lines, 300.0)


_ONE = re.compile(r"^(?P<l>.*?[A-Za-z]{3,}.*?)\s*[=:]\s*(?P<v>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
                  r"(?P<u>SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|m²|m2|sqm|qm)$", re.I)


def image_rows(path: str) -> tuple:
    """The same rows from a raster sheet's ALREADY-READ text layer (VLM or OCR; stoa.solvers.raster.textlayer /
    stoa.ocr): no new model call is made here, so the hook costs nothing when the layer is not there. H 17:26 / 18:24:
    on Stoa's AFCEC A-2 PNG the raster path answered 'total circulation on the second floor' with one row (1,240 SF)
    and 'dormitory area of the west wing' with the wing TOTAL (7,009 SF); the PDF's table answers 4,470 / 6,095."""
    tl = _cached_layer(path)
    if tl is None:
        return ()
    out = _pair(_item_lines((str(it.text), it.box) for it in tl.items), left_only=True)
    box = _table_box(tl)
    why = _suspect(out, tl, box) if out else ["no rows"]
    if not why:
        return out
    # tile seams cut the layer's table rows ('WEST WING D' / ',095 SF', 'RMITORY', a neighbour tile's 'DORMITORY ='
    # without its wing; H 20:00 / QA 20:10): OCR ONLY the table's region, once (cached). A layer whose rows look damaged
    # never answers on its own: 'dormitory of the north wing' took the WEST wing's cut row 'DORMITORY' (6,095 @0.7)
    if box is None:
        return ()
    crop = _crop_rows(path, tl, box)
    if crop and not _suspect(crop, None, None, words=False):
        return crop
    return ()


_DICT: set[str] | None = None
_TABLE_WORDS = {"subtotal", "dorm", "circ", "mech", "elec", "stor", "corr", "wc", "wcs", "lobby", "vestibule", "sf",
                "gsf", "nsf", "nasf", "asf", "gross", "net", "admin", "telecom", "janitor", "jan", "toilets", "restrooms",
                "restroom", "laundry", "lounge", "stairs", "stairwell", "exterior", "interior", "mezzanine", "ii", "iii"}


def _vocab() -> set[str]:
    """Lower-case English words (/usr/share/dict/words) + table words; empty when the list is missing (then no word
    test is made)."""
    global _DICT
    if _DICT is None:
        try:
            with open("/usr/share/dict/words", encoding="utf-8", errors="ignore") as f:
                _DICT = {w.strip().lower() for w in f if w.strip().isalpha()}
        except OSError:
            _DICT = set()
        if _DICT:
            _DICT |= _TABLE_WORDS | set(_STOP) | set(_SCOPE) | set(_QUAL) | set(_SYN) | set(_SYN.values())
    return _DICT


def _suspect(rs: tuple, tl, box, words: bool = True) -> list[str]:
    """Why a table read from IMAGE text should not be trusted as it is (empty = fine). H 20:00: a fresh VLM read of
    Stoa's AFCEC PNG cut labels at the cell edge ('RMITORY', 'E CIRCULATION'), paired title-block text with values
    ('AIR FORCE\nCIVIL ENGINEER\nCENTER' = 26,181 SF) and dropped some labels (their values unpaired)."""
    why = []
    seen: dict = {}
    voc = _vocab() if words else set()
    for lab, v, _u, b in rs:
        if "\n" in lab:
            why.append(f"multi-line label {lab[:30]!r}")
        k = (lab, round(b[1] / 4), round(b[3] / 4))
        if k in seen:
            why.append(f"one label {lab[:30]!r} for two values")
        seen[k] = v
        toks = re.findall(r"[A-Za-z]+", lab)
        if words and toks and len(toks[0]) == 1 and toks[0].upper() not in ("A", "B", "C", "D"):
            why.append(f"label starts mid-word {lab[:30]!r}")
        if voc:
            bad = [t for t in toks if len(t) >= 3 and t.lower() not in voc and _norm(t) not in voc]
            if bad:
                why.append(f"label words not in the dictionary {bad[:3]} ({lab[:30]!r})")
    if words:
        bad = _verify(rs)[1]
        if bad:  # VP 20:01: a partial read (rows lost at tile seams) pairs cleanly but its totals do not add up
            why.append(f"{len(bad)} TOTAL rows do not add up ({bad[0][0][:24]!r} = {bad[0][1]:,.0f})")
    if tl is not None and box is not None:  # values printed in the table's column that no row took
        x0, y0, x1, y1 = box
        vals = [it for it in tl.items if _VALISH.match(str(it.text).strip()) and x0 <= it.box[0] and it.box[2] <= x1
                and y0 <= it.box[1] and it.box[3] <= y1]
        took = [b for *_x, b in rs]
        free = [it for it in vals if not any(min(it.box[3], b[3]) - max(it.box[1], b[1]) > 0.5 * (it.box[3] - it.box[1])
                                             and b[0] - 2 <= it.box[0] and it.box[2] <= b[2] + 2 for b in took)]
        if free:
            why.append(f"{len(free)} printed values in the table column without a label ({str(free[0].text)[:12]!r})")
    return why


_VALISH = re.compile(r"^[\d,.]*\d\s*(?:SF|S\.F\.|SQ\.?\s*FT\.?|FT²|FT2|m²|m2)$", re.I)
TABLE_OCR = bool(int(__import__("os").environ.get("E1_TABLE_OCR", "1")))


def _table_box(tl) -> tuple | None:
    """Image px box of a printed area table: >= 3 'N SF' / 'N m²' values right-aligned in one column (clipped reads
    like ',095 SF' count) + the labels on their rows to the left (within a quarter of the sheet), padded."""
    W, H = tl.size
    vals = [it for it in tl.items if _VALISH.match(str(it.text).strip())]
    if len(vals) < 3:
        return None
    col = max(([u for u in vals if abs(u.box[2] - v.box[2]) <= 0.015 * W] for v in vals), key=len)
    if len(col) < 3:
        return None
    x1, y0, y1 = max(u.box[2] for u in col), min(u.box[1] for u in col), max(u.box[3] for u in col)
    th = sorted(u.box[3] - u.box[1] for u in col)[len(col) // 2]
    left = [it for it in tl.items if x1 - 0.25 * W <= it.box[0] and it.box[2] <= x1 + 2 and
            any(min(it.box[3], u.box[3]) - max(it.box[1], u.box[1]) > 0.3 * th for u in col)]
    x0 = min(it.box[0] for it in left + col)
    pad = 2 * th
    return max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)


def _crop_rows(path: str, tl, box) -> tuple:
    """OCR (stoa.ocr, CPU, 0 LLM tokens) of the table crop; boxes mapped back to the layer's image px. Cached on disk
    by the image's content tag + box (a second question on the same sheet costs nothing). AFCEC A-2 PNG: 37 lines,
    every row exact, ~40 s cold."""
    if not TABLE_OCR:
        return ()
    import hashlib
    import os
    from pathlib import Path

    try:
        from PIL import Image

        from stoa import ocr
        from stoa.solvers.raster import textlayer as T

        if not ocr.available():
            return ()
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(path)
        fx, fy = im.size[0] / tl.size[0], im.size[1] / tl.size[1]
        b = (int(box[0] * fx), int(box[1] * fy), int(box[2] * fx), int(box[3] * fy))
        tag = hashlib.sha1(f"{T.file_tag(path)}|{Path(path).name}|{b}".encode()).hexdigest()[:16]
        d_ = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "artifacts" / \
            "E1" / "tablecrop"
        d_.mkdir(parents=True, exist_ok=True)
        f = d_ / f"{Path(path).stem[:40]}.{tag}.png"
        if not f.exists():
            tmp = f.with_suffix(".tmp.png")
            im.crop(b).save(tmp)
            os.replace(tmp, f)
        cached = ocr.cached(str(f))
        d = ocr.read(str(f))
        if not cached:
            from stoa.contract import record_cost

            record_cost(cpu_seconds=float(d.get("seconds", 0.0)))
    except Exception:  # noqa: BLE001 — no OCR, no table: the caller keeps its answer
        return ()
    items = []
    for it in d.get("items", []):
        if it.get("rot") or it.get("score", 1.0) < 0.6:
            continue
        x0, y0, x1, y1 = (float(v) for v in it["box"])
        items.append((str(it["text"]), [box[0] + x0 / fx, box[1] + y0 / fy, box[0] + x1 / fx, box[1] + y1 / fy]))
    return _pair(_item_lines(items), left_only=True)


def _item_lines(items) -> list:
    """(text, box) of horizontal image text -> rows()-style lines; 'LABEL = 6,095 SF' read as one line is split."""
    lines = []
    for t, b in items:
        t = str(t).strip()
        if not t:
            continue
        b = tuple(float(v) for v in b)
        m = _ONE.match(t)  # 'WEST WING DORMITORY = 6,095 SF' read as one line: split it into label + value
        if m:
            w = b[2] - b[0]
            cut = b[0] + w * len(t[:m.start("v")]) / max(1, len(t))
            lines.append((m.group("l").strip() + " =", (b[0], b[1], cut, b[3]), (1.0, 0.0)))
            lines.append((t[m.start("v"):].strip(), (cut, b[1], b[2], b[3]), (1.0, 0.0)))
            continue
        lines.append((t, b, (1.0, 0.0)))
    return lines


def _cached_layer(path: str):
    """The text layer some reader already made for this image (memory, then the disk caches); None otherwise."""
    try:
        from stoa.solvers.raster import textlayer as T
    except Exception:  # noqa: BLE001
        return None
    try:
        tag = f"{path}{T.file_tag(path)}|"
        for k, v in list(getattr(T, "_mem", {}).items()):
            if str(k).startswith(tag):
                return v
        if T._disk_key(path, 1100, "local").exists():
            return T.text_layer(path)
        from stoa import ocr

        if ocr.cached(path):
            return ocr.text_layer(path)
    except Exception:  # noqa: BLE001
        return None
    return None


def _pair(lines: list, max_gap: float | None = None, left_only: bool = False) -> tuple:
    """left_only (image text): a value's label is the text to its LEFT on the row — never the title block's 'FEBRUARY
    2024' / 'AIR FORCE CIVIL ENGINEER CENTER' to its right when the read lost the row's own label (H 20:00)."""
    if max_gap is None:  # 300 pt on a PDF; on an image ~30 text heights (the gap is in px there)
        hs = sorted(min(b[3] - b[1], b[2] - b[0]) for _, b, _ in lines) or [10.0]
        max_gap = max(300.0, 30 * hs[len(hs) // 2])
    out = []
    for t, bb, dr in lines:
        m = _VAL.match(t)
        if not m:
            continue
        horiz = abs(dr[0]) >= abs(dr[1])
        a0, a1 = (bb[1], bb[3]) if horiz else (bb[0], bb[2])  # the row band across the reading direction
        best = None
        for t2, b2, d2 in lines:
            if d2 != dr or b2 == bb or _VAL.match(t2) or not re.search(r"[A-Za-z]{3,}", t2):
                continue
            c0, c1 = (b2[1], b2[3]) if horiz else (b2[0], b2[2])
            ov = min(a1, c1) - max(a0, c0)
            if ov < 0.6 * min(a1 - a0, c1 - c0):
                continue
            if left_only and horiz and (b2[0] + b2[2]) / 2 >= bb[0]:
                continue
            gap = (b2[0] - bb[2] if b2[0] >= bb[2] else bb[0] - b2[2]) if horiz else \
                (b2[1] - bb[3] if b2[1] >= bb[3] else bb[1] - b2[3])
            if gap < -2 or gap > max_gap:
                continue
            if best is None or gap < best[0]:
                best = (gap, t2, b2)
        if best is None:
            continue
        label = best[1].rstrip(" =:").strip()
        v = float(m.group("v").replace(",", ""))
        unit = "m2" if re.match(r"m|sqm|qm", m.group("u"), re.I) else "ft2"
        box = [round(min(bb[0], best[2][0]), 1), round(min(bb[1], best[2][1]), 1),
               round(max(bb[2], best[2][2]), 1), round(max(bb[3], best[2][3]), 1)]
        out.append((label, v, unit, tuple(box), best[1].rstrip().endswith(("=", ":"))))
    # a TABLE: several 'LABEL =' rows (room tags stacked over their values never pair this way)
    if len(out) < 3 or sum(1 for r in out if r[4] or re.search(r"\btotal\b", r[0], re.I)) < 3:
        return ()
    return tuple(r[:4] for r in out)


def _verify(rs) -> tuple[list, list]:
    """(totals that add up, totals that do not) in reading order: a 'TOTAL …' row = the rows since the previous total
    (a section: 'TOTAL WEST WING' = 6,095 + 914), or the section totals since the last grand total ('SUBTOTAL' = the
    four wing totals), or the last grand total + the rows since it ('TOTAL SECOND FLOOR' = SUBTOTAL + EXTERIOR STAIR)."""
    ok: list = []
    bad: list = []
    run: list = []
    secs: list = []
    grand = None
    for r in sorted(rs, key=lambda r: ((r[3][1] + r[3][3]) / 2, r[3][0])):
        if not re.search(r"\b(sub)?total\b", r[0], re.I):
            run.append(r[1])
            continue

        def eq(x, v=r[1]):
            return x is not None and abs(x - v) <= max(1.0, 0.002 * v)

        a = sum(run) if run else None
        if eq(a):
            ok.append(r)
            secs.append(r[1])
        elif eq(sum(secs) + sum(run) if secs else None) or eq(grand + sum(run) if grand is not None else None):
            ok.append(r)
            grand, secs = r[1], []
        else:
            bad.append(r)
            secs.append(r[1])
        run = []
    return ok, bad


def _content(label: str) -> set[str]:
    return _wordset(label) - _STOP - {"total", "subtotal"}


def _frag(w: str, s: str) -> bool:
    """w is a piece of the word s cut at a cell / tile edge: 'rmitory' / 'dormitof' / 'circulat' for dormitory /
    circulation (>= 4 letters, not s itself)."""
    if len(w) < 4 or w == s or not w.isalpha():
        return False
    return s.startswith(w) or s.endswith(w) or (len(w) >= 6 and len(w) <= len(s) and s.startswith(w[:-1]))


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader never breaks the cascade
        return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    from stoa.data import abspath

    q = question
    if not _AREA_Q.search(q) or re.search(r"\b(how many|number of)\b(?!\s+(?:square|sq))", q, re.I):
        return None
    pdf = str(abspath(path))
    is_pdf = pdf.lower().endswith(".pdf")
    if not is_pdf and not re.search(r"\.(png|jpe?g|tiff?|webp|bmp)$", pdf, re.I):
        return None
    rs = rows(pdf, page) if is_pdf else image_rows(path)
    if not rs:
        return None
    qw = _wordset(_INCL.sub(" ", q)) - _STOP
    subject = qw - _SCOPE - _QUAL - {"room", "space"} if not ({"common"} & qw) else qw - _SCOPE - _QUAL
    quals = qw & _QUAL
    totals = [r for r in rs if re.search(r"\btotal\b", r[0], re.I) and not re.search(r"\bsubtotal\b", r[0], re.I)]
    pick: list = []
    how = ""
    qual_cap = None
    cands = [r for r in totals if _content(r[0]) and _content(r[0]) <= qw and subject <= _content(r[0])]
    if cands:
        best = max(len(_content(r[0])) for r in cands)
        top = [r for r in cands if len(_content(r[0])) == best]
        if len(top) == 1:
            pick, how = top, f"the printed table row '{top[0][0]}'"
    if not pick and not subject and re.search(r"\b(this|the|whole|entire)\s+(floor|level|storey|story|sheet|plan)\b",
                                              q, re.I):
        # 'the total area of this floor': the one floor-wide TOTAL row ('TOTAL FIRST FLOOR' on the A-1 sibling)
        fl = [r for r in totals if _content(r[0]) & {"floor"} and
              _content(r[0]) <= {"floor", "first", "second", "third", "fourth", "ground", "upper", "lower", "basement"}]
        if len(fl) == 1:
            pick, how = fl, f"the printed table row '{fl[0][0]}' (this sheet's floor)"
    if not pick and subject:
        hits = [r for r in rs if subject <= _content(r[0]) and not re.search(r"\b(sub)?total\b", r[0], re.I)]
        if quals:
            hits = [r for r in hits if quals & _content(r[0]) or not (_QUAL & _content(r[0]))]
            if any(quals & _content(r[0]) for r in hits):
                hits = [r for r in hits if quals & _content(r[0])]
            elif hits:
                # QA 20:10 / H 20:00: 'dormitory of the NORTH wing' took a row 'DORMITORY' that names no wing. On a table
                # that names wings elsewhere ('TOTAL WEST WING') that row lost its wing in the read: abstain. On a table
                # that names no wing at all the row may still be the one: answer, but say so and stay low.
                if any(_QUAL & _content(r[0]) for r in rs):
                    return None
                qual_cap = (0.3, f"the table names no {' / '.join(sorted(quals))} part; its only matching row is "
                                 f"taken for it")
        if not is_pdf and any(_frag(w, sw) for r in rs if r not in hits for w in _content(r[0]) for sw in subject):
            return None  # a cut label of the asked type ('RMITORY' for dormitory): the rows found are not all of them
        if len(hits) == 1:
            pick, how = hits, f"the printed table row '{hits[0][0]}'"
        elif len(hits) > 1 and (_SUM_Q.search(q) or len(hits) <= 6):
            if not _SUM_Q.search(q) and not re.search(r"\b(wings|floors|levels|spaces|areas|rooms)\b", q, re.I):
                return None  # 'the circulation area' with four circulation rows and no 'total': ambiguous
            pick = hits
            how = f"sum of {len(hits)} printed table rows naming {', '.join(sorted(subject))}"
    if not pick:
        return None
    units = {r[2] for r in pick}
    if len(units) != 1:
        return None
    src_unit = units.pop()
    total = sum(r[1] for r in pick)
    want_m2 = bool(re.search(r"\b(square met\w*|m²|m2|sq\.? ?m|sqm|quadrat\w*)\b", q, re.I))
    want_ft2 = bool(re.search(r"\b(square f\w*|sq\.? ?ft|sf|ft²|ft2)\b", q, re.I))
    if src_unit == "ft2" and want_m2:
        val, unit = round(total * FT2_M2, 2), "m2"
    elif src_unit == "m2" and want_ft2:
        val, unit = round(total / FT2_M2, 1), "ft2"
    else:
        val, unit = (round(total, 2), src_unit)
    terms = " + ".join(f"{r[1]:,.0f}" if r[1] == int(r[1]) else f"{r[1]:g}" for r in pick)
    su = "SF" if src_unit == "ft2" else "m²"
    ev = [Evidence(file=path, page=page, bbox=list(r[3]), bbox_space="pdf_pt" if is_pdf else "image_px", exact=is_pdf,
                   note=f"{r[0][:48]} = {r[1]:,.0f} {su} (printed area table" + ("" if is_pdf else ", read from the "
                   "image's text") + ")") for r in pick]
    conf = (0.85 if len(pick) == 1 else 0.75) if is_pdf else (0.7 if len(pick) == 1 else 0.6)  # image: values as read
    assumptions = [f"{how}: {terms} {su}" + (f" = {total:,.0f} {su}" if len(pick) > 1 else ""),
                   "areas as printed in the sheet's own area table (not measured)"]
    ok, bad = _verify(rs)
    check = ""
    if ok and not bad:
        check = f"table check: its {len(ok)} TOTAL rows add up to the rows above them ✓"
        if not is_pdf:
            conf += 0.1  # every total of the read table adds up: the values were read right
    elif bad:
        check = (f"table check: {len(bad)} of {len(ok) + len(bad)} TOTAL rows do not add up as read "
                 f"({bad[0][0][:24]} = {bad[0][1]:,.0f})")
        if not is_pdf:  # X1 20:38 (AFCEC 300-dpi scan): a row lost in the read makes a SUM short (12,380 of 18,472)
            conf = min(conf, 0.3 if len(pick) > 1 or _SUM_Q.search(q) else 0.5)
            assumptions.append("some of the table's totals do not add up as read: a row may be missing or misread")
    if qual_cap is not None:
        conf = min(conf, qual_cap[0])
        assumptions.append(qual_cap[1])
    conf = round(conf, 2)
    if unit != src_unit:
        assumptions.append(f"converted: {total:,.0f} {su} x {FT2_M2 if unit == 'm2' else 1 / FT2_M2:.8g} = {val} "
                           f"{'m²' if unit == 'm2' else 'SF'}")
    return Answer(value=val, unit=unit, confidence=conf, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=[], trace=[f"area_table: {len(rs)} printed 'LABEL = value' rows on the sheet"
                                     + ("" if is_pdf else " (from the image's text layer)") + f"; {how}"]
                  + ([check] if check else []))


__all__ = ["answer", "rows"]


def prefer_printed_table(path: str, question: str, ans: Answer) -> Answer:
    """Pipeline hook (E1 block, captain 16:15 'printed area tables first'): on a PDF whose own area table answers the
    question, the table's value is the answer (exact, the rows boxed); an answer that already agrees (±0.5 %) is kept
    with a cross-check line. Fires only on sheets with >= 3 'LABEL = value SF/m²' rows (rows())."""
    try:
        b = answer(path, question)
    except Exception:  # noqa: BLE001
        return ans
    if b is None:
        return ans
    if b.confidence < 0.5 and ans.value is not None and (ans.confidence or 0.0) >= b.confidence:
        ans.trace = [*ans.trace, f"area_table (not used, {b.confidence}): {b.value:g} {b.unit}; {b.assumptions[-1]}"]
        return ans
    try:
        v = float(ans.value) if ans.value is not None else None
    except (TypeError, ValueError):
        v = None
    if v is not None and str(ans.unit or "").lower() in ("m2", "m²", "ft2") and \
            abs(v * (FT2_M2 if str(ans.unit).lower() == "ft2" else 1) - b.value * (FT2_M2 if b.unit == "ft2" else 1)) \
            <= 0.005 * b.value * (FT2_M2 if b.unit == "ft2" else 1):
        ans.trace = [*ans.trace, f"cross-check: the sheet's printed area table says {b.value:g} {b.unit} ✓"]
        if b.confidence > (ans.confidence or 0.0):  # two readers agree: the table's confidence and its row boxes
            ans.trace.append(f"confidence {ans.confidence} -> {b.confidence} (agrees with the printed table)")
            ans.confidence = b.confidence
            ans.evidence = [*b.evidence, *(ans.evidence or [])]
            ans.assumptions = [*(ans.assumptions or []), *b.assumptions[:1]]
        return ans
    b.solver = f"pipeline/{SOLVER}" if str(ans.solver or "").startswith("pipeline/") else SOLVER
    b.cost = b.cost.add(ans.cost) if ans.cost is not None and b.cost is not None else (ans.cost or b.cost)
    b.trace = [*(t for t in ans.trace[:2] if str(t).startswith("recognised:")),
               f"E1 area_table: the sheet prints this area in its own table; replaces {ans.solver}: "
               f"{str(ans.value)[:40]!r} @{ans.confidence}", *b.trace]
    b.recognition = getattr(ans, "recognition", None)
    return b
