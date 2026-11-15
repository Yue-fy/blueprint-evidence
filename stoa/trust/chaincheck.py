"""Expert rule xr-36 (Ziying, s100f-04): check that the dimension strings add up to the printed overall dimension before
using them ('500 + 11000 + 500 = 12000'; '500 + 4272 + 300 + 9950 + 500 = 15522'). T1, Sat 23:00.

An answer that uses a PRINTED dimension (evidence note 'dimension 9950 mm (printed; …)', with its box on the page) is
checked on the sheet's own text: the string must sit in a run of consecutive strings on its dimension line that adds
up to a total printed on a parallel line next to it and centred on that run (a total's text sits mid-line) — exactly,
or with the edge bands the expert adds (a printed wall-sized width w at BOTH ends: 4272 + 300 + 4975 + 4975 + 2 × 500 =
15522), or be itself the total of such a run (9950 = 4975 + 4975). Outcomes, written onto the answer:
- verified: a trace line and an assumption with the arithmetic ('dimension check (xr-36): …  = 15522 printed ✓');
- contradicted: the string's own line is a chain (>= 2 more strings) with a printed total beside it, and no run that
  contains the string adds up to it -> missing line DIMCHECK_MISSING (support_state shows 'approximate');
- not checkable (no overall printed near it, a picture, no text layer): one trace line, nothing else changes.
Value and confidence are never changed. PDFs only (a picture's text layer may need a cold OCR read: not here).
Kill switch: STOA_T1_DIMCHECK=0.
"""

from __future__ import annotations

import os
import re
import statistics

from stoa.contract import Answer

TAG = "dimension check (xr-36)"
DIMCHECK_MISSING = "the printed dimensions used do not add up to the printed overall"
_USED = re.compile(r"^\s*dimension\s+(?P<t>\d[\d ,.'’′\"”″/-]*?)\s*(?:mm|cm|m|ft|in)?\s*\(printed", re.I)
MAX_RUN = 14
_cache: dict = {}


def _on() -> bool:
    return os.environ.get("STOA_T1_DIMCHECK", "1") != "0"


def _items(pdf: str, page: int):
    """The page's text lines (unrotated page points = the frame of Evidence.bbox on a PDF)."""
    key = (pdf, page, os.path.getmtime(pdf) if os.path.exists(pdf) else 0)
    if key in _cache:
        return _cache[key]
    import pymupdf

    from stoa.solvers.raster.textlayer import TextItem

    out = []
    with pymupdf.open(pdf) as doc:
        if not 0 <= page < doc.page_count:
            return []
        for b in doc[page].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = " ".join(sp["text"].strip() for sp in ln["spans"] if sp["text"].strip())
                if t:
                    out.append(TextItem(t, [float(x) for x in ln["bbox"]]))
    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = out
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _in(box, ev_box, pad: float = 2.0) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return ev_box[0] - pad <= cx <= ev_box[2] + pad and ev_box[1] - pad <= cy <= ev_box[3] + pad


def _val(d, iu: str) -> float:
    from stoa.solvers.raster import chains as CH

    return d.value if d.family == "ft" else CH._metres(d, iu)


def _fmt(d) -> str:
    return d.item.text.strip()


def check_one(dims, U) -> tuple[str, str]:
    """('verified' | 'contradicted' | 'unchecked', description) for the printed dimension U among the sheet's dims."""
    from stoa.solvers.raster import chains as CH

    size = statistics.median(CH._size(d) for d in dims)
    tol = max(4.0, 0.6 * size)
    span = max(max(d.item.box[2] for d in dims) - min(d.item.box[0] for d in dims),
               max(d.item.box[3] for d in dims) - min(d.item.box[1] for d in dims))
    reach = max(30 * size, 0.2 * span)  # the overall line may sit outside a band of other strings
    widths, seen_w = [], set()  # one printed string per value: the candidate edge-band widths
    for d in sorted((d for d in dims if d.family == U.family), key=lambda d: d.value):
        if d.value not in seen_w:
            seen_w.add(d.value)
            widths.append(d)
    for axis in ("row", "col"):
        key = (lambda d: d.item.cy) if axis == "row" else (lambda d: d.item.cx)
        along = (lambda d: d.item.cx) if axis == "row" else (lambda d: d.item.cy)
        line = sorted((d for d in dims if d.family == U.family and abs(key(d) - key(U)) <= tol), key=along)
        if U not in line:
            continue
        k = line.index(U)
        others = [T for T in dims if T.family == U.family and tol < abs(key(T) - key(U)) <= reach]
        # (a) U is itself the total of a run on a parallel line (9950 = 4975 + 4975)
        for L in CH._lines([d for d in others if d.family == U.family], axis, tol):
            for i in range(len(L)):
                for j in range(i + 2, min(len(L), i + MAX_RUN) + 1):
                    run = L[i:j]
                    if not _centred(along(U), along(run[0]), along(run[-1]), tol):
                        continue
                    iu = CH._int_unit(run + [U]) if U.family == "metric" else ""
                    vals, tv = [_val(d, iu) for d in run], _val(U, iu)
                    if abs(sum(vals) - tv) <= max(0.003 * tv, 0.005 if U.family == "metric" else 1 / 24):
                        return "verified", f"'{_fmt(U)}' = " + " + ".join(_fmt(d) for d in run) + " (its own chain) ✓"
        # (b) U is a segment of a run that adds up to a printed total beside its line: an exact sum anywhere first,
        # edge bands only after that (S-100F: '… + 3200 + 600 = 12000' exists, not '… + 3200 + 2 x 300 = 12000')
        banded = None
        for i in range(0, k + 1):
            for j in range(k + 1, min(len(line), i + MAX_RUN) + 1):
                run = line[i:j]
                lo, hi = along(run[0]) - tol, along(run[-1]) + tol
                for T in others:
                    if not (lo <= along(T) <= hi) or not _centred(along(T), along(run[0]), along(run[-1]), tol):
                        continue
                    iu = CH._int_unit(run + [T]) if U.family == "metric" else ""
                    vals, tv = [_val(d, iu) for d in run], _val(T, iu)
                    if tv <= max(vals):
                        continue
                    eps = max(0.003 * tv, 0.005) if U.family == "metric" else 1 / 24
                    r = tv - sum(vals)
                    segs = " + ".join(_fmt(d) for d in run)
                    if abs(r) <= eps:
                        return "verified", f"{segs} = {_fmt(T)} (the printed overall) ✓"
                    if r <= 0 or banded:
                        continue
                    # edge bands (the expert's 500 mm foundation walls): a printed wall-sized width, matched exactly
                    beps = _BAND_EPS.get(iu if U.family == "metric" else "ft", 0.005)
                    for w in widths:
                        wv = _val(w, iu)
                        if not (_BAND_MIN.get(iu or "ft", 0.05) <= wv <= _BAND_MAX.get(iu or "ft", 1.0)) or \
                                wv > 0.1 * tv:
                            continue
                        if abs(r - 2 * wv) <= beps:  # one band at each end (both of the expert's checks)
                            banded = f"{segs} + 2 × {_fmt(w)} (edge bands) = {_fmt(T)} (the printed overall) ✓"
                            break
        if banded:
            return "verified", banded
        # contradicted only in the clear case: U's whole line is a chain (>= 3 strings) and exactly ONE printed total
        # lies beside it within its extent, larger than every segment, and within 15 % of the line's sum but not equal
        if len(line) >= 3:
            lo, hi = along(line[0]) - tol, along(line[-1]) + tol
            iu = CH._int_unit(line) if U.family == "metric" else ""
            vals = [_val(d, iu) for d in line]
            near = [T for T in others if lo <= along(T) <= hi and _val(T, iu) > max(vals) and
                    abs(key(T) - key(U)) <= 8 * size]
            if len(near) == 1:
                tv = _val(near[0], iu)
                if 0.003 * tv < abs(tv - sum(vals)) <= 0.15 * tv:
                    return "contradicted", (" + ".join(_fmt(d) for d in line) + f" = {sum(vals):g}"
                                            f"{' m' if U.family == 'metric' else ' ft'} ≠ the printed overall "
                                            f"{_fmt(near[0])}")
    return "unchecked", f"'{_fmt(U)}': no printed overall found next to its dimension line"


def _centred(t: float, a: float, b: float, tol: float) -> bool:
    """A total's string is centred on its own dimension line: it must sit near the middle of the run it sums (a random
    sum elsewhere on a dense sheet rarely does; dense synthetic sheet: '600 + 1200 + 1200 + 2 x 300 = 3600' before)."""
    return abs(t - (a + b) / 2) <= 0.2 * abs(b - a) + 2 * tol


# edge-band widths (in the chain's own unit after _val: metres for metric, feet for imperial)
_BAND_EPS = {"mm": 0.0015, "cm": 0.015, "": 0.005, "ft": 1 / 96}
_BAND_MIN = {"mm": 0.05, "cm": 0.05, "": 0.05, "ft": 0.25}
_BAND_MAX = {"mm": 1.0, "cm": 1.0, "": 1.0, "ft": 3.5}


def check(ans: Answer, path: str | None = None) -> Answer:
    """Pipeline.solve (via sanity_guard): xr-36 on the printed dimensions an answer used."""
    if not _on() or ans is None or ans.value is None:
        return ans
    used = []
    for e in ans.evidence or []:
        m = _USED.match(str(e.note or ""))
        if m and e.bbox and str(e.file or path or "").lower().endswith(".pdf"):
            used.append((m.group("t").strip(), e))
    if not used or any(str(t).startswith(TAG) for t in ans.trace):
        return ans
    from stoa.solvers.raster import chains as CH

    out, bad, ok, metres = [], [], [], []
    for txt, e in used[:6]:
        pdf = str(e.file or path)
        if not os.path.isabs(pdf) and path and str(path).lower().endswith(".pdf"):
            pdf = str(path)
        try:
            items = _items(pdf, int(e.page or 0))
        except Exception:  # noqa: BLE001
            return ans
        dims = CH.dims_of(items)
        U = next((d for d in dims if _norm(d.item.text) == _norm(txt) and _in(d.item.box, e.bbox)), None)
        if U is None or len(dims) < 3:
            out.append(f"'{txt}': not found as a printed dimension at its mark")
            continue
        state, why = check_one(dims, U)
        out.append(why)
        (ok if state == "verified" else bad if state == "contradicted" else []).append(why)
        metres.append(_in_metres(U, str(e.note or "")))
    trace = [f"{TAG}: " + "; ".join(out)]
    how = _value_from(ans, metres) if ok and len(ok) == len(used[:6]) and not bad else None
    if how:  # support_state: every printed dimension used is checked, and the value is computed from them alone
        trace.append(f"{ALL_CHECKED}: {how}")
    asm = [f"the printed dimensions add up to the printed overall: {'; '.join(ok)}"] if ok and not bad else []
    miss = [f"{DIMCHECK_MISSING}: {'; '.join(bad)} — check the dimension strings on the drawing"] if bad else []
    return ans.model_copy(update={"trace": [*ans.trace, *trace], "assumptions": [*ans.assumptions, *asm],
                                  "missing": [*ans.missing, *miss]})


ALL_CHECKED = f"{TAG}: all checked"
_NOTE_UNIT = re.compile(r"^\s*dimension\s+\S.*?\s(mm|cm|m|ft|in)\s*\(printed", re.I)
_AREA_UNIT = {"m2": 1.0, "m²": 1.0, "sqm": 1.0, "ft2": 0.09290304, "ft²": 0.09290304, "sf": 0.09290304,
              "sq ft": 0.09290304}
_LEN_UNIT = {"m": 1.0, "mm": 0.001, "cm": 0.01, "ft": 0.3048, "in": 0.0254}


def _in_metres(U, note: str) -> float | None:
    """The printed dimension in metres, in the unit its evidence note names ('dimension 4272 mm (printed …')."""
    m = _NOTE_UNIT.match(note)
    if U.family == "ft":
        return U.value * 0.3048
    if U.kind == "dec":
        return U.value
    u = (m.group(1).lower() if m else "mm")
    return U.value * _LEN_UNIT.get(u, 0.001) if U.kind == "int" else U.value / 1000


def _value_from(ans: Answer, metres: list) -> str | None:
    """'area = 4.272 × 6.2 m' when the answer's value is the product (area) / the sum or one (length) of the checked
    printed dimensions, within 0.5 %; else None."""
    if any(v is None for v in metres) or not isinstance(ans.value, (int, float)) or isinstance(ans.value, bool):
        return None
    u = str(ans.unit or "").strip().lower()
    v = float(ans.value)
    if u in _AREA_UNIT and len(metres) == 2:
        a = metres[0] * metres[1]
        if abs(v * _AREA_UNIT[u] - a) <= 0.005 * a:
            return f"the area is the product of the two checked printed dimensions ({metres[0]:g} × {metres[1]:g} m)"
    if u in _LEN_UNIT and metres:
        for x, what in ((sum(metres), "sum"), *((m_, "one") for m_ in metres)):
            if abs(v * _LEN_UNIT[u] - x) <= 0.005 * x and (what == "sum" or len(metres) == 1):
                return f"the length is the checked printed dimension{'s' if len(metres) > 1 else ''} ({x:g} m)"
    return None


__all__ = ["ALL_CHECKED", "DIMCHECK_MISSING", "TAG", "check", "check_one"]
