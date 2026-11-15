"""Check a plan's scale on a SECOND length: the sheet's own printed dimension strings (VP, cycle 7; expert rule xr-11).

Ziying (Stoa sheet 1, dfe-01): "The printed 1:200 only holds when the sheet is printed at A2, so the scale bar is safer
... check the scale on a second length." views already reads the printed 1:N, paper notes ('1:200 @ A1') and pairs the
graphic scale bars with their views. This module adds the second length that CAD sheets carry everywhere: dimension
strings ('6200', "133'-10\"") drawn on their own dimension lines. A number counts only when the line it sits on
measures it at the tried 1:N (stoa.evidence.dimrect; ft-in strings via F3's selfcheck reader), so reading the page at
the printed ratio and at the paper-size steps around it (x sqrt 2, x 2: an A1 sheet printed on A2 / A3, ANSI D on B)
tells which 1:N the drawing really is at on THIS page:

    dimension_scale(pdf, page=0, printed=None) -> {
        "ratio": 1:N the dimensions measure (median of the matched strings), "n": strings matched at that ratio,
        "printed": the tried printed ratio, "n_printed": strings matched at the printed ratio,
        "agrees": True | False | None (None = too few strings to say), "factor": ratio / printed,
        "boxes": up to 6 matched strings' text boxes (pdf_pt), "texts": their printed values, "note": one line } | None

Without a printed ratio it tries the usual architectural scales (1:20 ... 1:500) and returns the one the strings
measure clearly (>= 6 strings and >= 2.5x the next best), else None: an unscaled sheet's scale from its own dimensions.
Vector PDFs only; ~0.1-1 s per page (cached).
"""

from __future__ import annotations

import functools
import statistics

# page-size steps around the printed ratio: the same drawing printed smaller (A1 -> A2 = x 1.414, A1 -> A3 = x 2,
# A0 -> A3 = x 2.83) or larger (A3 -> A2 = x 0.707)
STEPS = (1.0, 1.4142, 2.0, 2.8284, 0.7071)
STANDARD = (20, 25, 35.36, 50, 70.71, 75, 100, 125, 141.42, 150, 200, 250, 282.84, 300, 400, 500)  # + A-size steps
TIGHT = 0.03      # a string agrees with a tried ratio when its own line measures it within 3 % (dimrect keeps 10 %)
MIN_STRINGS = 4   # fewer matched strings = no verdict
DOMINANCE = 2.5   # the winning ratio needs this many times the strings of the next best one (spurious pairings)


def _strings(pdf: str, page: int, n: float) -> list[tuple[float, float, tuple, str]]:
    """[(implied 1:N, value_mm, text box, printed text)] of the dimension strings whose line measures them at ~1:n."""
    out = []
    try:  # F3's reader: metric 3-5 digit mm + US feet-inch strings, quantities with a unit word ('2470 SF') left out
        from stoa.features.selfcheck import _dims

        for d in _dims(pdf, page, n):
            if d["meas"] > 0:
                out.append((n * d["val"] / d["meas"], float(d["val"]), tuple(d["box"]), str(d["txt"])))
        return out
    except Exception:  # noqa: BLE001 - F3's module missing / changed: E1's metric reader alone
        pass
    from stoa.evidence import dimrect as R

    for val, _o, _lo, _hi, _line, tb, meas in R.dims(pdf, page, n):
        if meas > 0:
            out.append((n * val / meas, float(val), tuple(tb), str(int(val))))
    return out


def _tight(rows, n: float):
    return [r for r in rows if abs(r[0] / n - 1) <= TIGHT]


@functools.lru_cache(maxsize=64)
def _numbers(pdf: str, page: int) -> int:
    """How many words on the page could be dimension strings (3-5 digit mm, ft-in): a cheap gate before the line
    geometry is read (most GA plans print none: ~0.25 s saved per page)."""
    import re

    import pymupdf

    num = re.compile(r"^\d{3,5}$")
    ftin = re.compile(r"^\d{1,4}['\u2019\u2032]\s*-?\s*\d{1,2}")  # straight / curly / prime foot marks (selfcheck._FTIN)
    with pymupdf.open(pdf) as d:
        if page >= len(d):
            return 0
        return sum(1 for w in d[page].get_text("words") if num.match(w[4]) or ftin.match(w[4]))


@functools.lru_cache(maxsize=256)
def _scan_one(pdf: str, page: int, n: float) -> tuple:
    try:
        return tuple(_tight(_strings(pdf, page, n), n))
    except Exception:  # noqa: BLE001
        return ()


def _inside(box, within) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for b in within:
        x0, y0, x1, y1 = b
        px, py = 0.03 * (x1 - x0), 0.03 * (y1 - y0)
        if x0 - px <= cx <= x1 + px and y0 - py <= cy <= y1 + py:
            return True
    return False


def dimension_scale(pdf: str, page: int = 0, printed: float | None = None, within=None) -> dict | None:
    """Which 1:N the page's printed dimension strings measure (see the module doc); None = not a PDF / no strings.
    `within` = page-space boxes (pdf_pt, unrotated: View.bbox): only strings inside them count (a sheet with views at
    several scales: a 1:50 detail's strings must not check a 1:25 plan)."""
    if not str(pdf).lower().endswith(".pdf"):
        return None
    try:
        if _numbers(str(pdf), int(page)) < MIN_STRINGS:
            return None
    except Exception:  # noqa: BLE001
        return None
    cands = tuple(round(printed * s, 3) for s in STEPS) if printed else tuple(float(s) for s in STANDARD)

    def scan(n):
        rows = _scan_one(str(pdf), int(page), n)
        return (n, [r for r in rows if _inside(r[2], within)] if within else list(rows))

    first = scan(cands[0])
    if printed and len(first[1]) >= MIN_STRINGS and len(first[1]) >= 0.5 * _numbers(str(pdf), int(page)):
        res = (first,)  # half of the page's numbers measure the printed ratio: no other ratio can win (saves ~0.8 s)
    else:
        res = (first,) + tuple(scan(n) for n in cands[1:])
    ranked = sorted(res, key=lambda t: -len(t[1]))
    best_n, best = ranked[0]
    second = len(ranked[1][1]) if len(ranked) > 1 else 0
    at_printed = len(res[0][1]) if printed else 0
    if not best:
        return None
    ratio = round(statistics.median(r[0] for r in best), 2)
    clear = len(best) >= MIN_STRINGS and len(best) >= DOMINANCE * max(second, 1)
    rows = sorted(best, key=lambda r: -r[1])[:6]
    out = {"ratio": ratio, "n": len(best), "printed": printed, "n_printed": at_printed, "factor": None,
           "agrees": None, "boxes": [list(r[2]) for r in rows], "texts": [r[3] for r in rows]}
    if printed:
        out["factor"] = round(ratio / printed, 3)
        if best_n == res[0][0] and len(best) >= MIN_STRINGS:
            out["agrees"] = True
            out["note"] = (f"scale checked on a second length: {len(best)} printed dimension strings measure "
                           f"1:{ratio:g} on this page = the printed scale ✓")
        elif clear:
            out["agrees"] = False
            out["note"] = (f"the printed dimension strings measure 1:{ratio:g} on this page ({len(best)} strings), not "
                           f"the printed 1:{printed:g} ({at_printed} strings): the sheet is printed at another paper "
                           f"size (x {ratio / printed:.2f})")
        else:
            out["note"] = (f"too few printed dimension strings to check the scale ({len(best)} at 1:{best_n:g}, "
                           f"{at_printed} at the printed 1:{printed:g})")
        return out
    if not clear or len(best) < 6:
        return None
    out["agrees"] = None
    out["note"] = (f"no printed scale: {len(best)} printed dimension strings measure 1:{ratio:g} on this page "
                   f"(next best {second})")
    return out
