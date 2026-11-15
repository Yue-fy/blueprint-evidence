"""T1 c5 (captain 23:04 afcec-04 fix 3; expert rule xr-40 'check a printed room-count table against the plan, and a table
'TOTAL' for all floors is not this floor's count'): a COUNT that disagrees with a count the sheet itself prints for the
same noun ('48 DORMS' in AFCEC A-2's ROOM COUNT table; ours 2 'supported' from 2 marked rooms) is shown 'approximate'.

Text comes only from what is free: a PDF's own page text (pymupdf) or the raster text layer already read in this process
(stoa.solvers.raster.textlayer._mem) — never a new OCR / VLM read. Value and confidence are never changed: one missing
line (PRINTED_COUNT_MISSING …) that support_state turns into 'approximate', and a trace line. Kill switch
STOA_T1_PRINTEDCOUNT=0."""

from __future__ import annotations

import os
import re

from stoa.contract import Answer

TAG = "printed count check (xr-40)"
PRINTED_COUNT_MISSING = "the sheet prints a different count"
# '48 DORMS', '12 UNITS', 'DORMS = 48', 'ROOMS: 16' (one table cell / label line)
_N_NOUN = re.compile(r"^\s*(?P<n>\d{1,4})\s+(?P<w>[A-Za-z][A-Za-z.'-]{2,})\s*$")
_NOUN_N = re.compile(r"^\s*(?P<w>[A-Za-z][A-Za-z.' -]{2,30}?)\s*[:=]\s*(?P<n>\d{1,4})\s*$")
_TOTAL = re.compile(r"\b(?:total|subtotal|sum|all floors|overall|building)\b", re.I)
_WHOLE = re.compile(r"\b(?:whole|entire|all (?:the )?(?:floors|levels|storeys)|building|in total|altogether|overall)\b",
                    re.I)
_GENERIC = {"rooms", "room", "spaces", "space", "units", "unit", "items", "item", "the", "and", "are", "there", "this",
            "that", "floor", "level", "plan", "sheet", "shown", "drawn", "many", "how"}


def _on() -> bool:
    return os.environ.get("STOA_T1_PRINTEDCOUNT", "1") != "0"


def _lines(path: str | None) -> tuple[list, str]:
    """(text items with .text / .box, bbox space): a PDF's page-0 text (page points) or the picture's text layer already
    in memory (its own pixels); ([], '') when neither is free."""
    p = str(path or "")
    if p.lower().endswith(".pdf") and os.path.exists(p):
        try:
            from stoa.trust.chaincheck import _items

            return list(_items(p, 0)), "pdf_pt"
        except Exception:  # noqa: BLE001
            return [], ""
    try:
        from stoa.solvers.raster import textlayer as T

        pre = f"{p}{T.file_tag(p)}|"
        for k, tl in list(T._mem.items()):
            if str(k).startswith(pre):
                return list(tl.items), "image_px"
    except Exception:  # noqa: BLE001
        return [], ""
    return [], ""


def _texts(path: str | None) -> list[str]:
    return [i.text for i in _lines(path)[0]]


def _stem(w: str) -> str:
    w = re.sub(r"[^a-z]", "", w.lower())
    return w[:4] if len(w) >= 4 else ""


def printed_counts(question: str, texts: list[str]) -> list[tuple[int, str]]:
    """[(n, line)] printed counts of the asked noun ('48 DORMS' for 'How many dormitory rooms …'), TOTAL rows left out."""
    from stoa.trust.sanity import _counted_items

    noun = _counted_items(question)
    if noun == "items":  # terse 'dorm room count': the question's own words
        noun = re.sub(r"\b(?:count|number|total|of|in|on)\b", " ", question or "", flags=re.I)
    asked = {_stem(w) for w in re.findall(r"[A-Za-z]+", noun) if w.lower() not in _GENERIC} - {""}
    if not asked:
        return []
    whole = bool(_WHOLE.search(question or ""))
    out = []
    for i, t in enumerate(texts):
        m = _N_NOUN.match(t) or _NOUN_N.match(t)
        if not m or int(m.group("n")) >= 1000:
            continue
        words = [w for w in re.findall(r"[A-Za-z]+", m.group("w")) if w.lower() not in _GENERIC]
        # a count names its items in the plural ('48 DORMS'); '300 SLAB' is a thickness
        if not words or (_N_NOUN.match(t) and not re.search(r"[^s]s$", m.group("w").strip(), re.I)):
            continue
        # EVERY asked word must be there: 'accessible (ABA) dorm rooms' is a subset of '48 DORMS'
        if not asked <= {_stem(w) for w in words}:
            continue
        prev = texts[i - 1] if i else ""
        if (_TOTAL.search(t) or re.search(r"(?:total|subtotal)\s*=?\s*$", prev, re.I)) and not whole:
            continue  # 'TOTAL = 144 DORMS': all floors, not this floor's count (xr-40)
        out.append((int(m.group("n")), t.strip()))
    return out


def check(question: str, ans: Answer, path: str | None = None) -> Answer:
    if not _on() or ans is None or isinstance(ans.value, bool) or not isinstance(ans.value, (int, float)):
        return ans
    from stoa.trust import support as S

    if S.asked_kind(question, ans.unit) != "count" or not float(ans.value).is_integer() or ans.value <= 0:
        return ans
    if any(str(t).startswith(TAG) for t in ans.trace):
        return ans
    items, space = _lines(path)
    found = printed_counts(question, [i.text for i in items])
    if not found:
        return ans
    ns = sorted({n for n, _ in found})
    v = int(ans.value)
    if v in ns:
        line = next(t for n, t in found if n == v)
        out = ans.model_copy(update={"trace": [*ans.trace, f"{TAG}: the sheet prints '{line}' = this count ✓"]})
        marks = [e for e in ans.evidence or [] if S._located(e) and not S._AUX_NOTE.search(str(e.note or ""))]
        box = next((i.box for i in items if i.text.strip() == line and getattr(i, "box", None)), None)
        if not marks and box is not None and space:
            # T1 c6: nothing marked (a vision model's number), but the sheet prints this very count: mark the printed
            # line, so the answer reads 'the sheet prints 48 DORMS' instead of 'not counted' (sanity runs this check
            # before uncounted_guard)
            from stoa.contract import Evidence

            out.evidence = [*(ans.evidence or []), Evidence(file=str(path), page=0, bbox=[round(float(x), 1) for x in box],
                                                            bbox_space=space, exact=True,
                                                            note=f"printed count '{line}' (the sheet's own count)")]
        return out
    line = found[0][1]
    miss = (f"{PRINTED_COUNT_MISSING}: '{line}'{' (and ' + str(len(found) - 1) + ' more)' if len(found) > 1 else ''} "
            f"vs {v} counted — check which one the question means (a printed count table may be for a whole floor)")
    return ans.model_copy(update={"trace": [*ans.trace, f"{TAG}: the sheet prints {ns} for this noun, the answer is {v}"],
                                  "missing": [*ans.missing, miss]})


__all__ = ["PRINTED_COUNT_MISSING", "TAG", "check", "printed_counts"]
