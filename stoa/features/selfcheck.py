"""Drawing SELF-CHECK: find the errors in a sheet before they reach site (F3, captain's feature list Sat 19:54).

After an upload, list the places where two readings of the SAME sheet disagree beyond tolerance, each with the
readings, where they are (evidence boxes to zoom to) and how sure we are. Rule: no false alarms. A check only
reports when both readings exist and are trustworthy; anything we could not read is a 'skipped' line, never a finding.

Checks (all deterministic, CPU only, no model calls, ~1-10 s per sheet):

  area_table_sums      a printed area table's TOTAL / SUBTOTAL rows vs the sum of the rows they total
                       ('WEST WING DORMITORY 6,095 + CIRCULATION 914 = TOTAL WEST WING 7,009 SF')
  dimension_chains     a chain of dimension strings vs the overall dimension spanning the same extent
                       ('600 + 3200 + 600 + ... = 15522'); the drawn line lengths tell which number is off
  dimension_drawn      a dimension number vs the length of its own dimension line at the printed 1:N
                       (a number typed over the CAD value: 'the text says 1750, the line is 1588 mm long')
  scale_bar            the graphic scale bar vs the printed '1:N' (VP's stoa.sheet.views readings)
  room_tags            the same room number tagged twice for DIFFERENT rooms in one plan view; the same room
                       printed in two views (floor plan + RCP) with a different name or area
  room_areas           a room tag's printed area vs the area of the room's outline traced from the wall lines
                       (E1's stoa.evidence.linework.measure_room at the view's scale; only well-supported traces)
  schedule_tags        a door / window schedule's marks vs the tags on the plans of the same file
  floors_align         a SET: lifts / shafts on this level's plan vs the plans of the level above / below (other pages of
                       the PDF, files uploaded together, same-project names in a folder), laid over each other by the
                       stairs / lifts that agree; a lift drawn 1 m off on one floor is reported, stairs only ever agree
                       (their traced outline changes with flights and landings); voids over the room below are shown

    from stoa.features.selfcheck import check
    check(abs_path, page=0, file=None) -> dict  (never raises; see REPORT SHAPE below)

REPORT SHAPE
    {"file", "page", "version", "findings": [Finding], "passed": [{"check", "what"}], "checks": [{"check", "label",
     "status": "ran" | "skipped" | "failed", "compared": int, "flagged": int, "why"}], "notes": [str],
     "cost": {"wall_seconds", "llm_tokens_in": 0, "llm_tokens_out": 0, "api_calls": 0, "gpu_seconds": 0},
     "cost_tier": "cheap"}
    Finding = {"id", "check", "severity": "error" | "warning", "sure": 0..1, "title", "what",
               "readings": [{"label", "value", "unit", "source"}], "delta": {"abs", "pct", "unit"},
               "tolerance": str, "evidence": [CONTRACT Evidence dicts], "zoom": [x0, y0, x1, y1] (bbox_space)}

Tolerances (why): printed table rows and dimension strings are exact numbers, rounded to the last printed digit,
so a sum may be off by about half a unit per row; anything beyond max(rows x 1 unit, 0.2 %) is an arithmetic slip.
Drawn vs printed and measured vs printed carry the reader's own error (dims are found within 10 %; a well-supported
traced outline is within ~1-2 % of Revit's area on Stoa's AFCEE A-101), so those report only beyond 5 %.
"""

from __future__ import annotations

import functools
import math
import re
import time
from pathlib import Path

VERSION = "selfcheck-1"
FT2_M2 = 0.09290304
PT_MM_AT_1 = 25.4 / 72.0  # 1 pt on paper = 0.3528 mm

LABELS = {
    "area_table_sums": "Area table totals add up",
    "dimension_chains": "Dimension chains add up to the overall dimension",
    "dimension_drawn": "Dimension numbers match their drawn length",
    "scale_bar": "Scale bar matches the printed scale",
    "room_tags": "Room numbers are unique and consistent across views",
    "room_areas": "Printed room areas match the drawn rooms",
    "schedule_tags": "Door / window schedule marks are tagged on the plans",
    "floors_align": "Stairs and lifts line up with the floors above / below",
    "plan_vs_model": "Plan matches the 3D model (doors, windows, walls, rooms)",
}
ALL_CHECKS = tuple(LABELS)


# ----------------------------------------------------------------------------------------------------------------------
# helpers

def _ev(file: str, page: int | None, bbox, note: str, space: str = "pdf_pt", exact: bool | None = True,
        polygon=None) -> dict:
    d = {"file": file, "page": page, "bbox": [round(float(x), 2) for x in bbox] if bbox is not None else None,
         "bbox_space": space if bbox is not None else None, "ifc_guid": None, "note": note[:160], "exact": exact}
    if polygon is not None:
        d["polygon"] = [[round(float(x), 2), round(float(y), 2)] for x, y in polygon]
    return d


def _union(boxes) -> list | None:
    bs = [b for b in boxes if b is not None]
    if not bs:
        return None
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


def _pad(box, p: float) -> list | None:
    return None if box is None else [box[0] - p, box[1] - p, box[2] + p, box[3] + p]


def _fmt(v: float, unit: str) -> str:
    if unit in ("ft2", "SF"):
        return f"{v:,.0f} SF"
    if unit == "m2":
        return f"{v:,.2f} m²"
    if unit == "mm":
        return f"{v:,.0f} mm"
    return f"{v:g} {unit}".strip()


def _finding(check: str, severity: str, sure: float, title: str, what: str, readings: list, delta_abs: float,
             delta_pct: float | None, unit: str, tolerance: str, evidence: list, zoom=None) -> dict:
    return {"id": "", "check": check, "severity": severity, "sure": round(max(0.0, min(1.0, sure)), 2), "title": title,
            "what": what, "readings": readings,
            "delta": {"abs": round(delta_abs, 3), "pct": None if delta_pct is None else round(delta_pct, 2),
                      "unit": unit},
            "tolerance": tolerance, "evidence": evidence,
            "zoom": zoom or _pad(_union(e.get("bbox") for e in evidence), 12)}


class _Run:
    """Collects one check's outcome."""

    def __init__(self, check: str):
        self.check, self.compared, self.findings, self.passed, self.status, self.why = check, 0, [], [], "ran", ""

    def skip(self, why: str) -> "_Run":
        self.status, self.why = "skipped", why
        return self

    def row(self) -> dict:
        return {"check": self.check, "label": LABELS[self.check], "status": self.status, "compared": self.compared,
                "flagged": len(self.findings), "why": self.why}


# ----------------------------------------------------------------------------------------------------------------------
# 1. area table totals

_TOTAL = re.compile(r"\b(sub\s*-?\s*total|total|sum|grand total|gesamt|yhteensä|summa)\b", re.I)
_TOTAL_WORDS = {"total", "subtotal", "sub", "grand", "sum", "area", "areas", "net", "gross", "sf", "the", "of", "and",
                "all", "floor"}


def _words(label: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", label.lower()) if len(w) > 1}


def _table_rows(path: str, page: int) -> tuple[list, str]:
    from stoa.evidence import areatable as A

    if path.lower().endswith(".pdf"):
        rows, space = list(A.rows(path, page)), "pdf_pt"
        try:
            import pymupdf

            with pymupdf.open(path) as d:
                mat = d[page].rotation_matrix
        except Exception:  # noqa: BLE001
            mat = None
    else:
        rows, space, mat = list(_image_table_rows(path)), "image_px", None
    return _reading_order(rows, mat), space


def _image_table_rows(path: str) -> tuple:
    """areatable.image_rows from a STRICTLY cached text layer: image_rows goes through areatable._cached_layer, which
    calls text_layer() and starts a cold OCR read (120 s budget) when only the VLM layer is on disk, and it OCRs the
    table region when the rows look damaged. Seen in the uploads sweep: 123 s on a USACE S-100F scan. Here: the
    cached layer or nothing, and a table whose rows look damaged is not checked."""
    for tl in list(_image_layers(path))[:4]:
        got = _layer_table_rows(path, tl)
        if got:
            return got
    return ()


def _layer_table_rows(path: str, tl) -> tuple:
    from stoa.evidence import areatable as A

    out = A._pair(A._item_lines((str(it.text), it.box) for it in tl.items), left_only=True)
    box = A._table_box(tl)
    if out and not A._suspect(out, tl, box):
        return tuple(out)
    # rows cut by the layer's tile seams (Stoa's AFCEC PNG): areatable OCRs the table crop once and caches it; use that
    # read only when it is already cached (same crop file + OCR cache as areatable._crop_rows), never start it here
    if box is None:
        return ()
    try:
        import hashlib
        import os

        from PIL import Image

        from stoa import ocr
        from stoa.solvers.raster import textlayer as T

        with Image.open(path) as im:
            size = im.size
        fx, fy = size[0] / tl.size[0], size[1] / tl.size[1]
        b = (int(box[0] * fx), int(box[1] * fy), int(box[2] * fx), int(box[3] * fy))
        tag = hashlib.sha1(f"{T.file_tag(path)}|{Path(path).name}|{b}".encode()).hexdigest()[:16]
        f = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "artifacts" / \
            "E1" / "tablecrop" / f"{Path(path).stem[:40]}.{tag}.png"
        if not (f.exists() and ocr.cached(str(f))):
            return ()
        crop = A._crop_rows(path, tl, box)  # a cache hit now
        return tuple(crop) if crop and not A._suspect(crop, None, None, words=False) else ()
    except Exception:  # noqa: BLE001
        return ()


def _reading_order(rows: list, mat=None) -> list:
    """Rows in the order a person reads the table: by column (left edge on the DISPLAYED page, columns > 60 pt apart),
    then top to bottom. The reader returns them in content-stream order, which CAD exports do not keep (a row
    retyped later is drawn last: seeded AFCEC '941 SF' came after TOTAL SECOND FLOOR)."""
    if not rows:
        return rows

    def disp(box):
        if mat is None:
            return box[0], (box[1] + box[3]) / 2
        import pymupdf

        r = pymupdf.Rect(box) * mat
        return r.x0, (r.y0 + r.y1) / 2

    keyed = [(disp(r[3]), r) for r in rows]
    xs = sorted(k[0][0] for k in keyed)
    cols, cur = [], [xs[0]]
    for x in xs[1:]:
        if x - cur[-1] > 60:
            cols.append(cur)
            cur = [x]
        else:
            cur.append(x)
    cols.append(cur)

    def col(x):
        for i, c in enumerate(cols):
            if c[0] - 1 <= x <= c[-1] + 1:
                return i
        return len(cols)

    return [r for (k, r) in sorted(keyed, key=lambda kr: (col(kr[0][0]), kr[0][1]))]


def check_area_table(path: str, page: int, file: str) -> _Run:
    """Walk the table top to bottom with a stack: a row is pushed; a TOTAL row consumes the suffix of the stack that
    sums to it (so 'TOTAL WEST WING' eats its two wing rows, 'SUBTOTAL' then eats the four wing totals). A total that
    no suffix sums to is a finding only when its parts are unambiguous: the plain rows since the last total all carry
    the total's own qualifier words ('TOTAL WEST WING' <- 'WEST WING DORMITORY', 'WEST WING CIRCULATION')."""
    run = _Run("area_table_sums")
    try:
        rows, space = _table_rows(path, page)
    except Exception as e:  # noqa: BLE001
        return run.skip(f"could not read the area table ({type(e).__name__})")
    if len(rows) < 3:
        return run.skip("no printed area table on this sheet")
    # reading order: the reader returns rows in line order; sort by position across the table's own direction
    stack: list[dict] = []  # {"label", "value", "unit", "box", "total": bool, "parts": [...]}
    unchecked: list[str] = []
    for label, value, unit, box in rows:
        item = {"label": label, "value": float(value), "unit": unit, "box": box, "total": bool(_TOTAL.search(label))}
        if not item["total"]:
            stack.append(item)
            continue
        if not stack:
            stack.append(item)
            continue
        match = None
        for k in range(1, len(stack) + 1):
            parts = stack[-k:]
            if any(p["unit"] != item["unit"] for p in parts):
                break
            s = sum(p["value"] for p in parts)
            tol = max(len(parts) * 1.0, 0.002 * item["value"])
            if k >= 2 and abs(s - item["value"]) <= tol:
                match = k
                break
        if match is not None:
            parts = stack[-match:]
            del stack[-match:]
            run.compared += 1
            run.passed.append({"check": run.check, "what": f"{label}: " + " + ".join(_fmt(p['value'], p['unit'])
                                                                                        for p in parts)
                               + f" = {_fmt(item['value'], item['unit'])} ✓"})
            item["parts"] = parts
            stack.append(item)
            continue
        # no suffix sums to it: are its parts unambiguous? (a) the plain rows right above it, all naming the total's
        # qualifier ('TOTAL WEST WING' <- 'WEST WING ...'); (b) everything still open on the stack (a final 'TOTAL
        # SECOND FLOOR' <- SUBTOTAL + EXTERIOR STAIR) when it comes within 5 % (a typo, not another grouping: a total
        # that leaves rows out on purpose differs by much more)
        qual = _words(label) - _TOTAL_WORDS
        plain = []
        for p in reversed(stack):
            if p["total"]:
                break
            plain.insert(0, p)
        own = [p for p in plain if qual and qual <= _words(p["label"])]
        sure = 0.9
        if not (qual and len(own) >= 2 and len(own) == len(plain)):
            own = []
            whole = list(stack)
            if len(whole) >= 2 and all(p["unit"] == item["unit"] for p in whole) and item["value"]:
                sw = sum(p["value"] for p in whole)
                if abs(sw - item["value"]) <= 0.05 * item["value"] and any(p["total"] for p in whole):
                    own, sure = whole, 0.7
        if own and all(p["unit"] == item["unit"] for p in own):
            s = sum(p["value"] for p in own)
            d = item["value"] - s
            pct = 100.0 * d / item["value"] if item["value"] else None
            run.compared += 1
            sev = "error" if abs(pct or 0) >= 1.0 else "warning"
            ev = [_ev(file, page if space == "pdf_pt" else None, item["box"],
                      f"printed {label} = {_fmt(item['value'], item['unit'])}", space)]
            ev += [_ev(file, page if space == "pdf_pt" else None, p["box"],
                       f"printed {p['label']} = {_fmt(p['value'], p['unit'])}", space) for p in own]
            run.findings.append(_finding(
                run.check, sev, sure, f"{label} does not add up",
                f"The table prints {label} = {_fmt(item['value'], item['unit'])}, but its rows add up to "
                f"{_fmt(s, item['unit'])} (" + " + ".join(_fmt(p['value'], p['unit']) for p in own) + f"): "
                f"{_fmt(abs(d), item['unit'])} {'more' if d > 0 else 'less'} than the rows.",
                [{"label": f"printed {label}", "value": item["value"], "unit": item["unit"], "source": "area table"},
                 {"label": "sum of its rows", "value": s, "unit": item["unit"], "source": "area table rows"}],
                abs(d), pct, item["unit"], f"rounding: ± {max(len(own), 1)} {item['unit']} or 0.2 %", ev))
            del stack[-len(own):]
        elif plain:
            # not checked (parts unclear, or a row was not read): its plain rows belong to it all the same, so they
            # must not leak into the next total (seeded DAF A-1 with a dropped row: SUBTOTAL then 'missed' 1,403 SF)
            del stack[-len(plain):]
            unchecked.append(label)
        item["parts"] = []
        stack.append(item)
    if unchecked:
        run.why = "not checked (rows unclear): " + ", ".join(unchecked)
    if run.compared == 0:
        run.why = run.why or "table read, but no total row whose parts are clear"
    return run


# ----------------------------------------------------------------------------------------------------------------------
# 2./3. dimension strings

def _plan_ratio(path: str, page: int) -> tuple[float | None, str]:
    """The 1:N of the sheet's drawing views when they all agree (dimension strings need it), else None."""
    from stoa.sheet.views import DRAWING_KINDS, views

    try:
        vs = [v for v in views(path, page) if v.kind in DRAWING_KINDS and (v.page_ratio or v.ratio)]
    except Exception:  # noqa: BLE001
        vs = []
    rs = sorted({round(v.page_ratio or v.ratio, 3) for v in vs})
    if len(rs) == 1:
        return rs[0], f"1:{rs[0]:g} ({vs[0].scale_text or 'printed scale'})"
    if not rs:
        # no titled view carries a scale (UK school plans print one '1:200@A3' in the title block): the page's only
        # printed scale, already corrected for '@A1 on an A3 page' (E1's linework.sheet_scales)
        try:
            from stoa.evidence.linework import sheet_scales

            sc = sheet_scales(path, page)
        except Exception:  # noqa: BLE001
            sc = ()
        if len(sc) == 1:
            return sc[0], f"1:{sc[0]:g} (the sheet's only printed scale)"
        return None, "no printed scale found"
    return None, "views at different scales (" + ", ".join(f"1:{r:g}" for r in rs) + ")"


_FTIN = re.compile(r"""^(\d{1,4})['’′]\s*-?\s*(\d{1,2})(?:\s*(\d)/(\d{1,2}))?\s*["”″]$""")


def _ftin_mm(t: str) -> int | None:
    """'133'-10"' -> 40792 mm; bare feet ("40'", scale-bar labels) are not read as dimensions (precision)."""
    m = _FTIN.match(t.strip())
    if not m:
        return None
    inch = int(m.group(1)) * 12 + int(m.group(2)) + (int(m.group(3)) / int(m.group(4)) if m.group(3) else 0.0)
    return int(round(inch * 25.4))


def _ftin_txt(mm: float) -> str:
    inch = round(mm / 25.4)
    return f"{inch // 12}'-{inch % 12}\""


# a number with one of these right next to it is a quantity (an area-schedule cell '2470 SF', a percentage), not a
# dimension string: Sports Complex A-801's Finish Room Schedule 'CLINIC 2470 SF' was read as a 2470 mm dimension with
# the table's rule as its line (false alarm 'Dimension 2470 is drawn 2,231 mm long', cycle 4)
_UNIT_WORD = re.compile(r"^(?:S\.?F\.?|SQ\.?|SQ\.?\s?FT\.?|SQFT|GSF|NSF|USF|RSF|FT²|FT2|M²|M2|M\^2|SM|M|MM|CM|LF|L\.F\.|%|KG|PCS|EA)$",
                        re.I)


def _unit_next(tb, words) -> bool:
    """True when a unit word (_UNIT_WORD) sits right beside the number's box, in either text direction."""
    x0, y0, x1, y1 = tb[:4]
    h = max(1.0, min(x1 - x0, y1 - y0))  # the text height, whatever the text's direction (rotated pages)
    for w in words:
        if not _UNIT_WORD.match(str(w[4]).strip()):
            continue
        gx = max(w[0] - x1, x0 - w[2], 0.0)
        gy = max(w[1] - y1, y0 - w[3], 0.0)
        if gx <= 1.5 * h and gy <= 1.5 * h and (gx > 0 or gy > 0):
            return True
    return False


_COMMA_MM = re.compile(r"^\d{1,3},\d{3}$")  # '8,670' / '66,720' / '120,000' mm


def _dims(path: str, page: int, n: float) -> list[dict]:
    """Metric dimension strings (dimrect.dims: 3-5 digit mm) + US feet-inch ones ('133'-10"', AFCEC / USACE sheets)
    read with the same line matcher (each value converted to mm). d["txt"] = the number as printed. A number with a
    unit word beside it ('2470 SF') is a quantity, not a dimension."""
    from stoa.evidence import dimrect as R

    out = []
    hs, vs, heads, words = R._geometry(path, page)
    for val, o, lo, hi, line, tb, meas in R.dims(path, page, n):
        if _unit_next(tb, words):
            continue
        out.append({"val": int(val), "o": o, "lo": float(lo), "hi": float(hi), "line": float(line), "box": tb,
                    "meas": float(meas), "txt": str(int(val)), "imperial": False})
    from stoa.evidence import linework as L

    mm_per_pt = n * L.PT_TO_M_AT_1 * 1000
    for w in words:
        mm, imperial = _ftin_mm(w[4]), True
        if mm is None and _COMMA_MM.match(w[4]) and not _unit_next(w, words):
            # UK sheets print mm with a thousands comma ('8,670', '66,720': DfE / EFA baseline plans), which
            # dimrect.dims (bare 3-5 digit numbers) does not read
            mm, imperial = int(w[4].replace(",", "")), False
        if mm is None:
            continue
        try:
            got = R._dim_of((w[0], w[1], w[2], w[3], str(mm)), hs, vs, heads, mm_per_pt)
        except Exception:  # noqa: BLE001
            got = None
        if got is None:
            continue
        val, o, lo, hi, line, tb, meas = got
        out.append({"val": int(val), "o": o, "lo": float(lo), "hi": float(hi), "line": float(line), "box": tb,
                    "meas": float(meas), "txt": w[4].strip(), "imperial": imperial})
    return out


def _len_txt(mm: float, imperial: bool) -> str:
    return _ftin_txt(mm) if imperial else f"{mm:,.0f} mm"


def _span_box(d: dict) -> list:
    """The dimension's own extent (line from lo to hi) united with its number, page space."""
    if d["o"] == "h":
        b = [d["lo"], d["line"] - 1, d["hi"], d["line"] + 1]
    else:
        b = [d["line"] - 1, d["lo"], d["line"] + 1, d["hi"]]
    return _union([b, d["box"]])


def _some_span_fits(path: str, page: int, d: dict, mm_per_pt: float, tol: float = 0.03) -> bool:
    """True when two marks on the dimension line (perpendicular lines crossing or ending within 4 pt of it, arrow-head
    ends on it) lie either side of the number and span its value within `tol`: the printed number is then drawn
    right and the reader picked the wrong pair."""
    from stoa.evidence.dimrect import _geometry

    hs, vs, heads, _w = _geometry(path, page)
    if d["o"] == "h":  # marks = vertical segments (x, y0, y1) reaching the horizontal line at y = d["line"]
        perp = vs
        head_pos = [p for a, b, c, e in heads if b - 1.5 <= d["line"] <= e + 1.5 for p in (a, c)]
        cu = (d["box"][0] + d["box"][2]) / 2
    else:  # vertical dimension: marks = horizontal segments (y, x0, x1) reaching the line at x = d["line"]
        perp = hs
        head_pos = [p for a, b, c, e in heads if a - 1.5 <= d["line"] <= c + 1.5 for p in (b, e)]
        cu = (d["box"][1] + d["box"][3]) / 2
    want_pt = d["val"] / mm_per_pt
    lo_win, hi_win = cu - 1.6 * want_pt, cu + 1.6 * want_pt
    marks = [u for u, a, b in perp if a - 4 <= d["line"] <= b + 4 and lo_win <= u <= hi_win]
    marks += [p for p in head_pos if lo_win <= p <= hi_win]
    left = sorted({round(u, 2) for u in marks if u < cu})
    right = sorted({round(u, 2) for u in marks if u > cu})
    for a in left:
        for b in right:
            if abs((b - a) - want_pt) <= tol * want_pt:
                return True
    return False


def _line_run(path: str, page: int, d: dict) -> tuple | None:
    """The drawn extent of a dimension's own line: the contiguous run of collinear segments through its number (a gap
    for the number allowed). An overall dimension's line runs arrow tip to arrow tip, so this is its true extent even
    when the reader paired the number with marks inside it (seeded S-100F '9590' on the 9950 line: reader 588.5-1132.4
    = the arrow bases, which happen to measure 9593 mm; the line runs 577.2-1141.4 = the chain's extent)."""
    from stoa.evidence.dimrect import _geometry

    hs, vs, _heads, _w = _geometry(path, page)
    segs = hs if d["o"] == "h" else vs
    tb = d["box"]
    cu = (tb[0] + tb[2]) / 2 if d["o"] == "h" else (tb[1] + tb[3]) / 2
    tw = (tb[2] - tb[0]) if d["o"] == "h" else (tb[3] - tb[1])
    iv = sorted((a, b) for v, a, b in segs if abs(v - d["line"]) <= 0.5)
    if not iv:
        return None
    runs, cur = [], list(iv[0])
    for a, b in iv[1:]:
        if a <= cur[1] + tw + 6:
            cur[1] = max(cur[1], b)
        else:
            runs.append(cur)
            cur = [a, b]
    runs.append(cur)
    for a, b in runs:
        if a - 1 <= cu <= b + 1:
            return a, b
    return None


def _snap_to_ticks(path: str, page: int, by_line: dict, mm_per_pt: float, max_gap: float = 15.0) -> None:
    """Close the gap the reader leaves when it pairs a chain piece with an ARROW BASE instead of the tick (it takes the
    marks that best fit the printed number, so a wrong number moves its ends: seeded A-101F '4365' in the 618 | 1965 |
    4635 | 1965 | 618 chain was read 1421-1666 pt, 9 pt inside the ticks at 1412 / 1675). A joint is a tick when
    arrow heads sit on both sides of it; the neighbour whose end is an arrow base is moved onto the tick and its drawn
    length re-measured. Only gaps exactly covered by an arrow head are closed (a missing small piece stays a gap)."""
    from stoa.evidence.dimrect import _geometry

    _hs, _vs, heads, _w = _geometry(path, page)
    for (o, line), pieces in by_line.items():
        if o == "h":
            on = [(a, c) for a, b, c, e in heads if b - 1.5 <= line <= e + 1.5]
        else:
            on = [(b, e) for a, b, c, e in heads if a - 1.5 <= line <= c + 1.5]
        if not on:
            continue

        def tick(u):  # arrow heads end at u from both sides
            return any(abs(c - u) <= 1.0 for a, c in on) and any(abs(a - u) <= 1.0 for a, c in on)

        def covered(u0, u1):
            return any(abs(a - u0) <= 1.5 and abs(c - u1) <= 1.5 for a, c in on)

        for p, q in zip(pieces, pieces[1:]):
            g = q["lo"] - p["hi"]
            if not (2.0 < g <= max_gap) or not covered(p["hi"], q["lo"]):
                continue
            if tick(p["hi"]):
                q["lo"] = p["hi"]
                q["meas"] = (q["hi"] - q["lo"]) * mm_per_pt
            elif tick(q["lo"]):
                p["hi"] = q["lo"]
                p["meas"] = (p["hi"] - p["lo"]) * mm_per_pt


def _traced_copy(img: str) -> str | None:
    """The picture's traced vector copy (P1's vectorize: its lines as strokes, its OCR text as invisible text), if the
    upload / takeoff already made one next to it ('<stem>.vecN.pdf' or a downsized '<stem>.s6000.vecN.pdf'); never
    traces (cache-only, like orderlist.picture_doors)."""
    p = Path(img)
    try:
        vecs = [*p.parent.glob(p.stem + ".vec*.pdf"), *p.parent.glob(p.stem + ".s[0-9]*.vec*.pdf")]
        vecs = [v for v in vecs if v.stat().st_mtime >= p.stat().st_mtime - 1]
    except OSError:
        return None
    return str(max(vecs, key=lambda q: q.stat().st_mtime)) if vecs else None


def _dims_on_picture(path: str, file: str) -> tuple[_Run, _Run]:
    """Dimension checks on a PICTURE's traced copy: AGREEMENTS only. A number that disagrees with its traced line is
    noted, never reported: on a picture an OCR misread ('9950' read '9590') looks exactly like a drawing slip."""
    vec = _traced_copy(path)
    if vec is None:
        why = "dimension strings are read on vector PDFs (or a picture's traced copy, made by its first measured question)"
        return _Run("dimension_chains").skip(why), _Run("dimension_drawn").skip(why)
    chains, drawn = check_dimensions(vec, 0, file)
    for r in (chains, drawn):
        if r.findings:
            r.why = (f"{len(r.findings)} reading(s) disagree on the picture's traced copy: not reported (an OCR misread "
                     "looks the same as a slip; check on the PDF)")
            r.findings = []
            if not r.passed:
                r.skip(r.why)
        r.passed = [{**{k: v for k, v in x.items() if k not in ("evidence", "zoom")},  # traced-copy space
                     "what": f"{x['what']} (on the picture's traced lines)"} for x in r.passed]
    return chains, drawn


def check_dimensions(path: str, page: int, file: str) -> tuple[_Run, _Run]:
    chains, drawn = _Run("dimension_chains"), _Run("dimension_drawn")
    if not path.lower().endswith(".pdf"):
        why = "dimension strings are read on vector PDFs only"
        return chains.skip(why), drawn.skip(why)
    n, how = _plan_ratio(path, page)
    if not n:
        return chains.skip(how), drawn.skip(how)
    try:
        ds = _dims(path, page, n)
    except Exception as e:  # noqa: BLE001
        return chains.skip(f"dimension reader failed ({type(e).__name__})"), drawn.skip("dimension reader failed")
    if len(ds) < 3:
        why = f"fewer than 3 dimension strings (mm or ft-in) read at {how}"
        return chains.skip(why), drawn.skip(why)
    mm_per_pt = n * PT_MM_AT_1
    # --- chains vs overall
    by_line: dict = {}
    for d in ds:
        by_line.setdefault((d["o"], round(d["line"] * 2) / 2), []).append(d)
    for k in by_line:
        by_line[k].sort(key=lambda d: d["lo"])
    JOIN = 2.0  # pt: pieces of one chain share their tick
    _snap_to_ticks(path, page, by_line, mm_per_pt)
    from stoa.evidence.dimrect import _geometry

    heads = _geometry(path, page)[2]

    def arrow_gap(D, u0, u1):  # an arrow head on D's line spans exactly u0..u1 (the reader took its base)
        a0, a1 = min(u0, u1), max(u0, u1)
        if a1 - a0 > 15.0:
            return False
        for x0, y0, x1, y1 in heads:
            lo_, hi_, c0, c1 = (x0, x1, y0, y1) if D["o"] == "h" else (y0, y1, x0, x1)
            if c0 - 1.5 <= D["line"] <= c1 + 1.5 and abs(lo_ - a0) <= 1.5 and abs(hi_ - a1) <= 1.5:
                return True
        return False

    def find_chain(D, lo, hi):
        """(line, pieces, (start, end)) of the nearest parallel chain spanning lo..hi; an end of D read on an arrow
        base snaps to the chain's end (seeded A-101F overall '9890' read 9 pt outside the chain's first tick)."""
        best = None
        for (o, line), pieces in by_line.items():
            if o != D["o"] or abs(line - D["line"]) < 2.0 or abs(line - D["line"]) > 90.0:
                continue
            starts = [i for i, p in enumerate(pieces) if abs(p["lo"] - lo) <= JOIN or arrow_gap(D, p["lo"], lo)]
            for i in starts:
                run_ = [pieces[i]]
                j = i

                def at_end(u):
                    return abs(u - hi) <= JOIN or arrow_gap(D, u, hi)

                while not at_end(run_[-1]["hi"]) and j + 1 < len(pieces) \
                        and abs(pieces[j + 1]["lo"] - run_[-1]["hi"]) <= JOIN and run_[-1]["hi"] < hi + JOIN:
                    j += 1
                    run_.append(pieces[j])
                if len(run_) >= 2 and at_end(run_[-1]["hi"]):
                    if best is None or abs(line - D["line"]) < abs(best[0] - D["line"]):
                        best = (line, run_, (run_[0]["lo"], run_[-1]["hi"]))
        return best

    seen = set()
    for D in ds:
        ext = (D["lo"], D["hi"])
        best = find_chain(D, *ext)
        if best is None:  # the number's own line, when it holds no other number (an overall dimension)
            lr = _line_run(path, page, D)
            if lr is not None and abs(lr[0] - D["lo"]) + abs(lr[1] - D["hi"]) > JOIN:
                others = [o for o in by_line.get((D["o"], round(D["line"] * 2) / 2), []) if o is not D
                          and lr[0] < (o["lo"] + o["hi"]) / 2 < lr[1]]
                if not others:
                    best = find_chain(D, *lr)
                    ext = lr
        if best is None:
            continue
        run_, ext = best[1], best[2]  # the extent both readings dimension (the chain's ticks)
        key = (D["o"], round(ext[0]), round(ext[1]), tuple(p["val"] for p in run_))
        if key in seen:
            continue
        seen.add(key)
        s = sum(p["val"] for p in run_)
        chains.compared += 1
        imp = D.get("imperial") or any(p.get("imperial") for p in run_)
        tol = max(len(run_) * (13.0 if imp else 1.0), 0.002 * D["val"])  # rounding: 1/2 inch or 1/2 mm per number
        txt = " + ".join(p["txt"] for p in run_)
        if abs(s - D["val"]) <= tol:
            evs = [_ev(file, page, _span_box({**D, "lo": ext[0], "hi": ext[1]}), f"overall dimension {D['txt']}")]
            evs += [_ev(file, page, _span_box(p), f"chain piece {p['txt']}") for p in run_]
            chains.passed.append({"check": chains.check, "what": f"{txt} = {D['txt']}{'' if imp else ' mm'} ✓",
                                  "evidence": evs, "zoom": _pad(_union(e["bbox"] for e in evs), 20)})
            continue
        d = s - D["val"]
        # which number is off? the drawn spans: the overall line and each piece measured at 1:N
        span_mm = (ext[1] - ext[0]) * mm_per_pt
        overall_ok = abs(span_mm - D["val"]) <= max(0.005 * D["val"], 10)
        worst = max(run_, key=lambda p: abs(p["meas"] - p["val"]) / p["val"])
        blame = ""
        if overall_ok and abs(worst["meas"] - worst["val"]) > max(0.005 * worst["val"], 10):
            blame = (f" The overall line is drawn {_len_txt(span_mm, imp)} long (= {D['txt']}); the piece printed "
                     f"{worst['txt']} is drawn {_len_txt(worst['meas'], imp)}: that number is the likely slip.")
        elif not overall_ok and all(abs(p["meas"] - p["val"]) <= max(0.005 * p["val"], 10) for p in run_):
            blame = (f" Every piece matches its drawn length; the overall line is drawn {_len_txt(span_mm, imp)}: the "
                     f"overall number is the likely slip.")
        ev = [_ev(file, page, _span_box({**D, "lo": ext[0], "hi": ext[1]}), f"overall dimension {D['txt']}")]
        ev += [_ev(file, page, _span_box(p), f"chain piece {p['txt']}") for p in run_]
        chains.findings.append(_finding(
            chains.check, "error" if abs(d) >= 50 else "warning", 0.85 if blame else 0.7,
            f"Dimension chain {txt} ≠ {D['txt']}",
            f"The chain {txt} adds up to {_len_txt(s, imp)}, but the overall dimension over the same extent says "
            f"{D['txt']} ({'+' if d > 0 else '−'}{_len_txt(abs(d), imp) if imp else f'{abs(d):,} mm'}).{blame}",
            [{"label": f"overall dimension {D['txt']}", "value": D["val"], "unit": "mm", "source": "dimension string"},
             {"label": f"sum of the chain{' ' + _ftin_txt(s) if imp else ''}", "value": s, "unit": "mm",
              "source": f"{len(run_)} dimension strings"}],
            abs(d), 100.0 * d / D["val"], "mm", f"rounding: ± {len(run_)} × {'½ inch' if imp else '1 mm'} or 0.2 %", ev))
    if chains.compared == 0:
        chains.why = f"{len(ds)} dimension strings read, none chained under an overall dimension"
    # --- printed number vs drawn length (the reader keeps numbers within 10 % of their line; 5-10 % = suspicious).
    # The reader picks ONE pair of breaks; a crossing extension line it ignores can hold the true span (S-100F
    # '1750': reader 1,588 mm between an arrow base and a tick, the extension lines crossing the line give 1,732 mm),
    # so a mismatch is reported only when NO pair of lines crossing / ending at the dimension line spans the number.
    for d in ds:
        drawn.compared += 1
        err = (d["meas"] - d["val"]) / d["val"]
        if abs(err) < 0.05 or abs(d["meas"] - d["val"]) < 50:
            continue
        if _some_span_fits(path, page, d, mm_per_pt):
            continue
        drawn.findings.append(_finding(
            drawn.check, "warning", 0.55,
            f"Dimension {d['txt']} is drawn {_len_txt(d['meas'], d['imperial'])} long",
            f"The number says {d['txt']}, but its dimension line measures {_len_txt(d['meas'], d['imperial'])} at {how} "
            f"({100 * err:+.1f} %). Either the text was typed over the CAD value or the line is not to scale.",
            [{"label": f"printed dimension {d['txt']}", "value": d["val"], "unit": "mm", "source": "dimension string"},
             {"label": "drawn length at scale", "value": round(d["meas"]), "unit": "mm",
              "source": f"line length × {how}"}],
            abs(d["meas"] - d["val"]), 100 * err, "mm", "5 % (reader accuracy ~1 %)",
            [_ev(file, page, _span_box(d), f"dimension {d['txt']}, drawn {_len_txt(d['meas'], d['imperial'])}")]))
    if not drawn.findings:
        evs = [_ev(file, page, _span_box(d), f"dimension {d['txt']}, drawn {_len_txt(d['meas'], d['imperial'])}")
               for d in ds]
        drawn.passed.append({"check": drawn.check,
                             "what": f"{len(ds)} dimension numbers match their drawn length at {how} (within 5 %) ✓",
                             "evidence": evs, "zoom": _pad(_union(e["bbox"] for e in evs), 20)})
    return chains, drawn


# ----------------------------------------------------------------------------------------------------------------------
# 4. scale bar vs printed scale



def _views(path: str, page: int, layer=None):
    from stoa.sheet.views import views

    return views(path, page, layer=layer)


def check_scale_bar(path: str, page: int, file: str, layer=None, notes: list | None = None) -> _Run:
    """Every graphic scale bar vs EVERY printed scale on the page: a bar that matches one of them (within 6 %: bar
    label centres are ~2-5 % off, AW 078-24-28 p21 three '0 1' 2'' bars read 1:12.65 for 1" = 1'-0") belongs to that
    view and agrees; a bar that matches none (beyond 10 %) disagrees with the sheet's own scale notes = the finding.
    Not views.View.bar_ratio: it pairs bars to views by the nearest note, last writer wins (that page's 1:33 bar of
    the 3/8" plan was given to the 1:12 sections: a false alarm in F3's 37-sheet sweep)."""
    run = _Run("scale_bar")
    if not path.lower().endswith(".pdf"):
        # on a picture a bar measures pixels per metre, not a paper ratio (Stoa's DfE PNG: bar '1:24.48' vs printed
        # 1:200 = a false alarm in F3's PNG run, 20:58): compare it with printed 1:N + printed sheet size instead
        return _bar_vs_paper(path, file, layer, run, notes)
    try:
        from stoa.sheet.views import _parse_scales, page_reading, scale_bars

        r = page_reading(path, page, layer)
    except Exception as e:  # noqa: BLE001
        return run.skip(f"page reader failed ({type(e).__name__})")
    if r is None:
        return run.skip("no text layer")
    lines, to_src = r[0], r[5]
    space = "pdf_pt" if path.lower().endswith(".pdf") else "image_px"
    pg = page if space == "pdf_pt" else None
    bars = scale_bars(lines)
    printed = [(ln, sc) for ln in lines for sc in _parse_scales(ln.text)]
    if not bars:
        return run.skip("no graphic scale bar on this sheet")
    if not printed:
        return run.skip("a scale bar but no printed scale (1:N) to compare it with")
    ratios = sorted({sc["ratio"] for _ln, sc in printed})
    for bar in bars:
        run.compared += 1
        br = bar["ratio"]
        best = min(ratios, key=lambda q: abs(br / q - 1))
        f = br / best
        if abs(f - 1) <= 0.06:
            run.passed.append({"check": run.check, "what": f"scale bar 1:{br:g} agrees with the printed 1:{best:g} ✓"})
            continue
        if abs(f - 1) <= 0.10:
            if notes is not None:
                notes.append(f"scale bar 1:{br:g} is {100 * (f - 1):+.0f} % from the printed 1:{best:g}: within what "
                             f"reading a bar's labels can tell apart, not reported")
            continue
        bx, by = (bar["box"][0] + bar["box"][2]) / 2, (bar["box"][1] + bar["box"][3]) / 2
        near_ln, near_sc = min(printed, key=lambda p: math.hypot(p[0].cx - bx, p[0].cy - by))
        nf = br / near_sc["ratio"]
        ev = [_ev(file, pg, to_src(bar["box"]), f"scale bar measuring 1:{br:g} (labels {' '.join(bar['labels'])})",
                  space),
              _ev(file, pg, to_src(near_ln.box), f"nearest printed scale: {near_ln.text.strip()[:60]}", space)]
        printed_txt = ", ".join(f"1:{q:g}" for q in ratios[:6])
        factor = min((2.0, 2 ** 0.5, 0.5, 2 ** -0.5, 4.0, 0.25), key=lambda k: abs(nf / k - 1))
        if abs(nf / factor - 1) <= 0.06:
            what = (f"The scale bar measures 1:{br:g} on this page, {nf:.2f}× the printed 1:{near_sc['ratio']:g} next to "
                    f"it, and no scale printed on the sheet ({printed_txt}) matches it: either the sheet is printed at "
                    f"another size than it was drawn for (an A1 sheet on A3, a 22×34 sheet on 11×17) or the printed "
                    f"scale is wrong. Measure with the bar on this page.")
            sev, sure = "warning", 0.75
        else:
            what = (f"The scale bar measures 1:{br:g}, but the sheet prints {printed_txt} and none of them matches "
                    f"({100 * (nf - 1):+.0f} % from the nearest, 1:{near_sc['ratio']:g}). One of them is wrong: measure "
                    f"a known dimension to tell which.")
            sev, sure = "error", 0.65
        run.findings.append(_finding(
            run.check, sev, sure, f"Scale bar 1:{br:g} matches no printed scale", what,
            [{"label": "nearest printed scale", "value": near_sc["ratio"], "unit": "1:N",
              "source": near_ln.text.strip()[:40]},
             {"label": "scale bar", "value": br, "unit": "1:N", "source": "graphic bar measured"}],
            abs(br - near_sc["ratio"]), 100 * (nf - 1), "1:N", "10 % (bar label positions are ~2-5 % accurate)", ev))
    return run


_ISO_MM = {"A0": 1189.0, "A1": 841.0, "A2": 594.0, "A3": 420.0, "A4": 297.0}  # long side


def _bar_vs_paper(path: str, file: str, layer, run: _Run, notes: list | None) -> _Run:
    """A PICTURE of a sheet: the scale bar's pixels per metre vs the printed 1:N on the printed sheet size ('A2') over
    the image's long side. Only with ONE bar, ONE printed ratio, ONE ISO size printed alone on a line and an image
    whose aspect is an ISO sheet's (within 2 %: a scan with margins or a crop is skipped, never reported). Stoa's DfE
    PNG: bar 115.8 px/m vs 1:200 on A2 over 14,000 px = 117.8 px/m (-1.7 %)."""
    if layer is None:
        return run.skip("no text layer")
    try:
        from PIL import Image

        from stoa.sheet.views import _parse_scales, page_reading, scale_bars

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            W, H = im.size
        r = page_reading(path, 0, layer)
    except Exception as e:  # noqa: BLE001
        return run.skip(f"page reader failed ({type(e).__name__})")
    if r is None:
        return run.skip("no text layer")
    lines, to_src = r[0], r[5]
    bars = scale_bars(lines)
    ratios = sorted({sc["ratio"] for ln in lines for sc in _parse_scales(ln.text)})
    sizes = sorted({m.group(1).upper() for ln in lines if (m := re.fullmatch(r"\s*(A[0-4])\s*", ln.text or "", re.I))})
    if len(bars) != 1 or len(ratios) != 1:
        return run.skip("on a picture: needs exactly one scale bar and one printed scale (1:N) to compare")
    if len(sizes) != 1:
        return run.skip("on a picture the bar gives pixels per metre; no single printed sheet size (A0-A4) to turn "
                        "the printed 1:N into pixels")
    aspect = max(W, H) / min(W, H)
    if abs(aspect / 2 ** 0.5 - 1) > 0.02:
        return run.skip(f"the picture is not a whole {sizes[0]} sheet (aspect {aspect:.3f}, ISO 1.414): cropped or "
                        f"scanned with margins, so the sheet size gives no pixel scale")
    bar, n, size = bars[0], ratios[0], sizes[0]
    px_m_bar = 1.0 / (bar["ratio"] * PT_MM_AT_1 / 1000.0)  # the bar's ratio treats one pixel as one point
    px_m_print = max(W, H) / _ISO_MM[size] * 1000.0 / n
    f = px_m_bar / px_m_print
    run.compared += 1
    desc = f"scale bar {px_m_bar:.1f} px/m vs printed 1:{n:g} on {size} over {max(W, H):,} px = {px_m_print:.1f} px/m"
    if abs(f - 1) <= 0.06:
        run.passed.append({"check": run.check, "what": f"{desc} ({100 * (f - 1):+.1f} %) ✓"})
        return run
    if abs(f - 1) <= 0.10:
        if notes is not None:
            notes.append(f"{desc}: {100 * (f - 1):+.0f} %, within what a bar's labels and a scan's edges can tell "
                         f"apart, not reported")
        return run
    ev = [_ev(file, None, to_src(bar["box"]), f"scale bar ({' '.join(bar['labels'])}): {px_m_bar:.1f} px per metre",
              "image_px")]
    run.findings.append(_finding(
        run.check, "warning", 0.6, f"Scale bar and printed 1:{n:g} on {size} disagree ({100 * (f - 1):+.0f} %)",
        f"On this picture the scale bar measures {px_m_bar:.1f} pixels per metre, but the printed 1:{n:g} on an {size} "
        f"sheet {max(W, H):,} px long gives {px_m_print:.1f}. Both come from the same pixels, so a reprint at another "
        f"size changes neither: the printed scale, the printed sheet size or the bar is wrong. Measure with the bar "
        f"(or a known dimension) until it is fixed.",
        [{"label": f"printed 1:{n:g} on {size}", "value": round(px_m_print, 1), "unit": "px/m",
          "source": "title block + image size"},
         {"label": "scale bar", "value": round(px_m_bar, 1), "unit": "px/m", "source": "graphic bar measured"}],
        abs(px_m_bar - px_m_print), 100 * (f - 1), "px/m", "10 % (bar labels ~2-5 %, scan edges)", ev))
    return run


# ----------------------------------------------------------------------------------------------------------------------
# 5./6. room tags

def _tags(path: str, page: int, layer=None):
    from stoa.sheet.labels import room_tags

    return room_tags(path, page, layer=layer)


def _m2(t) -> float:
    return t.value * FT2_M2 if t.unit == "ft2" else t.value


def _centre(b) -> tuple:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def check_room_tags(path: str, page: int, file: str, layer=None) -> _Run:
    run = _Run("room_tags")
    try:
        tags = [t for t in _tags(path, page, layer) if t.number]
    except Exception as e:  # noqa: BLE001
        return run.skip(f"room tag reader failed ({type(e).__name__})")
    if len(tags) < 2:
        return run.skip("no numbered room tags (name / number / area stacks) on this sheet")
    space = "pdf_pt" if path.lower().endswith(".pdf") else "image_px"
    pg = page if space == "pdf_pt" else None
    by_view: dict = {}
    for t in tags:
        by_view.setdefault((t.view, t.view_kind), []).append(t)
    # (a) one number, two different rooms in the same plan view (same name + area twice = the same room tagged twice)
    for (view, kind), ts in by_view.items():
        if kind not in (None, "floor_plan"):
            continue
        groups: dict = {}
        for t in ts:
            groups.setdefault(t.number, []).append(t)
        run.compared += len(groups)
        for num, g in groups.items():
            kinds = {(_norm_name(t.name), round(t.value)) for t in g}
            names = {_norm_name(t.name) for t in g if t.name}
            if len(g) < 2 or len(kinds) < 2 or len(names) < 2:
                continue
            ev = [_ev(file, pg, t.box, f"room {num}: {t.name or '?'} {t.text}", space) for t in g]
            run.findings.append(_finding(
                run.check, "error", 0.8, f"Room number {num} is used twice",
                f"Room number {num} tags {len(g)} different rooms in {view or 'this plan'}: "
                + "; ".join(f"{t.name or 'unnamed'} ({t.text})" for t in g) + ".",
                [{"label": f"tag {i + 1}", "value": t.value, "unit": t.unit, "source": f"{t.name or '?'} {num}"}
                 for i, t in enumerate(g)], 0.0, None, t.unit, "exact (room numbers must be unique)", ev))
    # (b) the same room in two views (plan + RCP) printed differently
    plan = {}
    for (view, kind), ts in by_view.items():
        if kind == "floor_plan":
            for t in ts:
                plan.setdefault(t.number, []).append(t)
    for (view, kind), ts in by_view.items():
        if kind in (None, "floor_plan"):
            continue
        for t in ts:
            ps = plan.get(t.number)
            if not ps:
                continue
            # the plan tag of the same module (nearest plan view title words) — use every plan tag with that number
            run.compared += 1
            same = [p for p in ps if _norm_name(p.name) == _norm_name(t.name) and abs(p.value - t.value) <= 0.5]
            if same:
                continue
            p = ps[0]
            if len(ps) > 1:  # the number is used twice in plans: (a) reports that
                continue
            diff_area = abs(p.value - t.value) > 0.5 and p.unit == t.unit
            pct = 100.0 * (t.value - p.value) / p.value if p.value else None
            what = (f"Room {t.number} is '{p.name or '?'}' {p.text} in {p.view or 'the plan'} but "
                    f"'{t.name or '?'}' {t.text} in {view}.")
            run.findings.append(_finding(
                run.check, "error" if diff_area else "warning", 0.75 if diff_area else 0.6,
                f"Room {t.number} differs between views", what,
                [{"label": p.view or "floor plan", "value": p.value, "unit": p.unit, "source": p.name or "?"},
                 {"label": view or "other view", "value": t.value, "unit": t.unit, "source": t.name or "?"}],
                abs(t.value - p.value), pct, p.unit, "exact (same room, same tag)",
                [_ev(file, pg, p.box, f"{p.view}: {p.label} {p.text}", space),
                 _ev(file, pg, t.box, f"{view}: {t.label} {t.text}", space)]))
    if run.compared and not run.findings:
        run.passed.append({"check": run.check, "what": f"{len({t.number for t in tags})} room numbers: unique per "
                                                       f"plan view and the same in every view ✓"})
    return run


_SHAFT = re.compile(r"\b(elev(?:ator)?s?|lifts?|stairs?|stairwells?|shafts?|chases?|ducts?|risers?|chutes?|voids?|"
                    r"open to below|atrium|hoistway)\b", re.I)


# rooms whose fittings are drawn along the walls (the tracer may stop at a counter / bath / WC edge)
_FITTED = re.compile(r"\b(k[üu]che\w*|kitchen\w*|kitchenette|pantry|servery|keitti\w*|keittokomero|kök|köök|cuisine|"
                     r"cucina|cocina|bad|bath\w*|shower\w*|dusche|wc|w\.c\.|toilet\w*|lavator\w*|washroom|"
                     r"kph|ph|kylpy\w*|wasch\w*|laundry|utility|khh|tvätt\w*|pesu\w*|tualett\w*|vannitub\w*|"
                     r"sauna|löyly\w*)\b", re.I)


def check_room_areas(path: str, page: int, file: str, min_support: int = 8, flag_pct: float = 10.0,
                     near_pct: float = 5.0, notes: list | None = None) -> _Run:
    """Printed tag area vs the traced outline of the room around the tag. Only well-supported traces count
    (support >= min_support of the tracer's readings agree): on AFCEE A-101 the 4 supported rooms are within 0.4-0.9 %
    of their printed areas, the open-plan ones (support 3-4) are off by 60 % and are skipped. Bench 20 sheets (F3
    20:40): an elevator's trace is the CAB drawn inside the shaft (DAF dorm ELEV 72 SF -> 40) -> shafts / lifts /
    stairs are not measured; a coloured area plan's ELEC was +7 % (its colour fill +6 %: wall centre vs finish), so
    only >= 10 % is reported and 5-10 % goes to the notes as 'near tolerance'."""
    run = _Run("room_areas")
    if not path.lower().endswith(".pdf"):
        return run.skip("room outlines are traced from vector wall lines (PDF) only")
    try:
        tags = [t for t in _tags(path, page) if t.view_kind == "floor_plan"]
        vs = {v.title: v for v in _views(path, page)}
    except Exception as e:  # noqa: BLE001
        return run.skip(f"room tag reader failed ({type(e).__name__})")
    if not tags:
        uk = _room_areas_uk(path, page, file, notes)  # UK school plans: 'Hall' over 'A: 180 m2', no views / numbers
        return uk if uk is not None else run.skip("no room tags with a printed area in a floor plan view")
    from stoa.evidence.linework import measure_room

    seen, unsure, metric_off = set(), 0, []
    for t in tags:
        key = (t.view, t.number, _norm_name(t.name), t.value)
        if key in seen:
            continue
        seen.add(key)
        if _SHAFT.search(t.name or ""):
            continue
        v = vs.get(t.view)
        n = (v.page_ratio or v.ratio) if v else None
        if not n:
            continue
        try:
            m = measure_room(path, page, _centre(t.box), n, tag_box=t.box)
        except Exception:  # noqa: BLE001
            m = None
        if not m or (m.get("support") or 0) < min_support:
            unsure += 1
            continue
        printed, meas = _m2(t), float(m["area_m2"])
        run.compared += 1
        pct = 100.0 * (meas - printed) / printed
        metric = t.unit != "ft2"
        if metric and float(t.value).is_integer():  # a rounded '4 m2' is 3.5-4.5 (F5 00:30)
            lo, hi = printed - 0.5, printed + 0.5
            pct = 0.0 if lo <= meas <= hi else 100.0 * (meas - (hi if meas > hi else lo)) / printed
        if abs(pct) < near_pct:
            run.passed.append({"check": run.check, "what": f"{t.label}: printed {t.text}, traced "
                                                           f"{meas:.2f} m² ({pct:+.1f} %) ✓",
                               "evidence": [_ev(file, page, _bbox_of(m["polygon"]), f"{t.label}: printed {t.text}, "
                                                f"traced {meas:.2f} m²", exact=False, polygon=m["polygon"])]})
            continue
        if metric:
            # m² tags (UK / European plans) are compared for AGREEMENT only: on 6 clean UK secondary plans
            # (experiments/f3/uk15.txt, F3 01:0x) this path reported 20 'differences', all tracer / tag-reader artefacts
            # (open halls and circulation, changing rooms traced short, a tag whose name line was not found, small
            # stores at the rounding edge); the ft² findings keep their bench (0 false alarms on 67 clean sheets)
            metric_off.append(f"{t.label}: printed {t.text}, traced ≈ {meas:.1f} m² ({pct:+.0f} %)")
            continue
        if abs(pct) < flag_pct:
            if notes is not None:
                notes.append(f"{t.label}: printed {t.text}, the traced outline is {pct:+.1f} %: near what tracing can "
                             f"tell apart (walls measured at centre vs finish), not reported")
            continue
        if pct < 0 and _FITTED.search(f"{t.name or ''} {t.label or ''}"):
            # fittings drawn along the walls (counters, baths, WCs, machines) close the outline early: a SMALLER trace
            # is expected there (uploads sweep: Swiss 'Küche 2.02' 12.05 m² printed, traced 9.31 m² to the counter
            # edge). A LARGER trace still reports (fittings never make the outline grow).
            if notes is not None:
                notes.append(f"{t.label}: printed {t.text}, traced {meas:.2f} m² ({pct:+.1f} %): kitchens and "
                             f"bathrooms have fittings drawn along the walls that the outline may follow; not reported")
            continue
        unit = t.unit
        meas_u = meas / FT2_M2 if unit == "ft2" else meas
        run.findings.append(_finding(
            run.check, "error" if abs(pct) >= 20 else "warning", 0.75 if abs(pct) >= 20 else 0.6,
            f"{t.label}: printed {t.text}, drawn ≈ {_fmt(meas_u, unit)}",
            f"The tag prints {t.text} for {t.label}, but the room drawn around the tag measures ≈ {_fmt(meas_u, unit)} "
            f"({pct:+.1f} %) at 1:{n:g}, traced along its walls ({m.get('support')} of {m.get('n_readings')} readings "
            f"agree). The area may be out of date after a layout change.",
            [{"label": "printed on the tag", "value": t.value, "unit": unit, "source": "room tag"},
             {"label": "traced outline", "value": round(meas_u, 1), "unit": unit, "source": f"wall lines at 1:{n:g}"}],
            abs(meas_u - t.value), pct, unit, f"{flag_pct:g} % (well-traced outlines are ~1-2 % accurate; wall centre vs "
                                             f"finish adds up to ~7 % on small rooms)",
            [_ev(file, page, t.box, f"printed {t.label} {t.text}"),
             _ev(file, page, _bbox_of(m["polygon"]), f"traced outline ≈ {meas:.2f} m²", exact=False,
                 polygon=m["polygon"])]))
    if metric_off and notes is not None:
        notes.append(f"{len(metric_off)} printed room area(s) in m² differ from the outline traced around the tag by "
                     f"{near_pct:g} % or more ({'; '.join(metric_off[:4])}): not reported — on metric plans a traced "
                     "outline that merges or splits rooms looks the same as an out-of-date area.")
    rooms = [p for p in run.passed if ": printed " in p["what"]]
    if len(rooms) > 5:  # many agreeing rooms: one line (a 40-room plan should not read as 40 checks)
        named = [p for p in rooms if not p["what"].startswith("(unlabelled")] or rooms
        eg = ", ".join(p["what"].split(" (")[0].replace(": printed ", " ").replace(", traced ", " ≈ ")
                       for p in named[:3])
        evs = [e for p in rooms for e in p.get("evidence") or []]
        run.passed = [p for p in run.passed if p not in rooms] + [
            {"check": run.check, "what": f"{len(rooms)} printed room areas match the room traced around their tag "
                                         f"(within {near_pct:g} %), e.g. {eg} ✓",
             "evidence": evs, "zoom": _pad(_union(e["bbox"] for e in evs), 10)}]
    if unsure:
        run.why = f"{unsure} room(s) skipped: their outline could not be traced reliably (open plan / gaps)"
    if run.compared and not run.passed and not run.findings and run.status == "ran":
        run.skip(f"{run.compared} printed room areas traced, none within {near_pct:g} %: not reported")
    if run.compared == 0 and run.status == "ran":
        uk = _room_areas_uk(path, page, file, notes)
        if uk is not None and uk.status == "ran":
            return uk
        run.skip(run.why or "no room could be traced reliably")
    return run


# UK school plans (DfE / EFA baseline designs, the family of Stoa's DfE example): a room tag is the name over its area
# 'A: 62 m2' (2014 primaries), '6 m2' (2014 secondaries) or '(13.0 m2)' (2012 sets); no room numbers, no view titles,
# the scale in the title block ('1:200@A3'). The printed area is ROUNDED (F5 00:30): compare with the range 61.5-62.5.
_UK_AREA = re.compile(r"^\s*\(?\s*(?:A\s*:\s*)?(\d{1,4}(?:\.\d{1,2})?)\s*(?:m2|m²|sq\.?\s*m)\s*\)?\s*$", re.I)


def _uk_area_tags(path: str, page: int) -> list[dict]:
    """[{name, value, half, box}] for every 'name' over 'A: N m2' stack on an UNROTATED page (pdf_pt); half = the
    rounding of the printed number (0.5 for '62', 0.05 for '13.0')."""
    import pymupdf

    from stoa.evidence.linework import text_lines

    with pymupdf.open(path) as d:
        if page >= len(d) or d[page].rotation:
            return []
    lines = text_lines(path, page)
    out = []
    for txt, b in lines:
        m = _UK_AREA.match(txt)
        if not m:
            continue
        cx, h = (b[0] + b[2]) / 2, b[3] - b[1]
        above = [(t2, b2) for t2, b2 in lines if b2[3] <= b[1] + 0.3 * h and b[1] - b2[3] < 1.2 * h
                 and abs((b2[0] + b2[2]) / 2 - cx) < max(b2[2] - b2[0], b[2] - b[0]) and not _UK_AREA.match(t2)
                 and re.search(r"[A-Za-z]{2}", t2)]
        if not above:
            continue
        name, nb = max(above, key=lambda x: x[1][3])
        s = m.group(1)
        out.append({"name": name.strip(), "value": float(s), "text": txt.strip(),
                    "half": 0.5 if "." not in s else 0.5 * 10 ** -len(s.split(".")[1]),
                    "box": (min(b[0], nb[0]), min(b[1], nb[1]), max(b[2], nb[2]), max(b[3], nb[3]))})
    return out


def _room_areas_uk(path: str, page: int, file: str, notes: list | None, min_support: int = 10, near_pct: float = 5.0):
    """Printed UK room areas vs the room traced around each tag: AGREEMENTS ONLY, or None (not this kind of sheet).
    Bench (experiments/f3/uk15.txt, 15 clean DfE 2014 / 2012 plans, F3 01:0x): 23-49 rooms per sheet agree within the
    rounding + 5 %, but 20 rooms traced >= 20 % off at support >= 10 are tracer misses, not slips (stairs, open
    circulation, a hall / kitchen / changing room traced into its neighbour, 'Gen Office' -14 % where the fill polygon
    says 11.27 m² = the printed 11): a disagreement here is noted, never reported."""
    try:
        tags = _uk_area_tags(path, page)
    except Exception:  # noqa: BLE001 - unreadable / missing file: not this kind of sheet
        return None
    if len(tags) < 5:
        return None
    run = _Run("room_areas")
    from stoa.evidence.linework import measure_room, sheet_scales

    sc = sheet_scales(path, page)
    if len(sc) != 1:
        return run.skip(f"{len(tags)} printed room areas read, but the sheet prints "
                        f"{'no scale' if not sc else 'several scales'}: rooms not traced")
    n = sc[0]
    agree, off, untraced = [], [], 0
    for t in tags:
        if _SHAFT.search(t["name"]) or t["value"] > 5000:
            continue
        c = _centre(t["box"])
        try:
            m = measure_room(path, page, c, n, tag_box=t["box"])
        except Exception:  # noqa: BLE001
            m = None
        if not m or (m.get("support") or 0) < min_support:
            untraced += 1
            continue
        a, v, half = float(m["area_m2"]), t["value"], t["half"]
        outside = 0.0 if v - half <= a <= v + half else a - (v + half if a > v + half else v - half)
        run.compared += 1
        (agree if abs(100.0 * outside / v) < near_pct else off).append((t, a, 100.0 * outside / v, m["polygon"]))
    if not agree:
        return run.skip(f"{len(tags)} printed room areas read; {run.compared} traced, none within {near_pct:g} % of "
                        f"the printed area: not reported (the tracer may have merged or split those rooms)")
    eg = ", ".join(f"{t['name'][:24]} {t['value']:g} m² ≈ {a:.1f}" for t, a, _, _ in
                   sorted(agree, key=lambda x: -x[0]["value"])[:3])
    evs = [_ev(file, page, _bbox_of(poly), f"{t['name'][:40]}: printed {t['text']}, traced {a:.2f} m²", exact=False,
               polygon=poly) for t, a, _, poly in agree]
    run.passed.append({"check": run.check, "what": f"{len(agree)} printed room areas match the room drawn around their "
                                                   f"tag at 1:{n:g} (within the printed rounding + {near_pct:g} %), "
                                                   f"e.g. {eg} ✓", "evidence": evs,
                       "zoom": _pad(_union(e["bbox"] for e in evs), 10)})
    if off and notes is not None:
        ex = "; ".join(f"{t['name'][:20]} {t['text']} vs ≈ {a:.1f} m²" for t, a, _, _ in
                       sorted(off, key=lambda x: -abs(x[2]))[:4])
        notes.append(f"{len(off)} printed room area(s) differ from the outline traced around the tag by more than "
                     f"{near_pct:g} % ({ex}): not reported — on plans like this (open circulation, stairs, rooms with "
                     "open sides) a traced outline that merges or splits rooms looks the same as an out-of-date area.")
    run.why = "; ".join(x for x in (untraced and f"{untraced} room(s) not traced reliably",
                                     off and f"{len(off)} traced room(s) outside {near_pct:g} %: noted, never reported")
                        if x)
    return run


def _room_areas_on_picture(path: str, file: str, notes: list | None = None) -> _Run:
    """Printed room areas vs rooms traced on a PICTURE's traced copy: AGREEMENTS only, as one line. On 9 traced scans
    here (Swiss / French dwelling scans, X1's UK school scans; F3 01:2x) the per-room reading 'disagreed' 0-9 times per
    sheet, every time a tracer or OCR artefact (corridors traced short, a 'Cage d'escalier' stair, '5.48 m²' read as
    '1 m²'), while 3-28 rooms per sheet agreed within 5 %."""
    vec = _traced_copy(path)
    if vec is None:
        return _Run("room_areas").skip("room outlines are traced from vector wall lines (PDF), or a picture's traced "
                                       "copy (made by its first measured question)")
    r = check_room_areas(vec, 0, file, notes=[])
    off = len(r.findings)
    r.findings = []
    rooms = [p["what"] for p in r.passed if p["what"].endswith("✓") and ": printed " in p["what"]]
    summary = [p for p in r.passed if p not in rooms and not (": printed " in p["what"])]
    if len(rooms) > 1:
        eg = ", ".join(x.split(" (")[0].replace(": printed ", " ").replace(", traced ", " ≈ ") for x in rooms[:3])
        summary.append({"check": "room_areas", "what": f"{len(rooms)} printed room areas match the room traced around "
                                                       f"their tag (within 5 %) on the picture's traced lines, e.g. "
                                                       f"{eg} ✓"})
    else:
        summary = [{**p, "what": p["what"].replace(" ✓", " (on the picture's traced lines) ✓")}
                   for p in summary + [{"check": "room_areas", "what": x} for x in rooms]]
    r.passed = [{k: v for k, v in p.items() if k not in ("evidence", "zoom")} for p in summary]  # traced-copy space
    if off and notes is not None:
        notes.append(f"{off} printed room area(s) differ from the room traced on the picture: not reported (on a "
                     "picture an OCR misread, or a traced outline that merges or splits rooms, looks the same as a slip)")
    if not r.passed and r.status == "ran":
        r.skip(f"{r.compared} printed room areas traced on the picture, none within 5 %: not reported")
    return r


def _bbox_of(poly) -> list:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


# ----------------------------------------------------------------------------------------------------------------------
# 7. door / window schedule vs the plans' tags (whole file: a set prints the schedule on one sheet, the plans on others)

_SCHED = re.compile(r"\b(DOOR|WINDOW|OPENING)\s+SCHEDULE\b", re.I)
_HEAD = {"NO.", "NO", "MARK", "NUMBER", "TAG", "#", "NR.", "NR"}
_MARKRX = re.compile(r"^[A-Z]{0,2}-?\d{1,4}(?:\.\d{1,3})?[A-Z]{0,2}$")  # 101A, D-12, 19.01B (Revit room.door)


def _disp_words(pg) -> list:
    """(x0, y0, x1, y1, text, unrotated box) per word, boxes on the DISPLAYED page (tables are read as printed)."""
    import pymupdf

    mat = pg.rotation_matrix
    out = []
    for w in pg.get_text("words"):
        r = pymupdf.Rect(w[:4]) * mat
        out.append((r.x0, r.y0, r.x1, r.y1, w[4], tuple(w[:4])))
    return out


def _schedule_marks(pg) -> list[dict]:
    """Per DOOR / WINDOW SCHEDULE title on the page: {kind, title_box, marks: {mark: box}} from the column under its
    'NO.' / 'MARK' header (nearest header below the title, rows until a gap of 3 row heights)."""
    ws = _disp_words(pg)
    out = []
    for i, w in enumerate(ws):
        if w[4].upper() != "SCHEDULE" or i == 0:
            continue
        prev = ws[i - 1]
        kind = prev[4].upper()
        if kind not in ("DOOR", "WINDOW", "OPENING") or abs(prev[1] - w[1]) > 3:
            continue
        tx0, ty0, tx1, ty1 = prev[0], w[1], w[2], w[3]
        heads = [h for h in ws if h[4].upper() in _HEAD and ty1 < h[1] <= ty1 + 160 and tx0 - 700 <= h[0] <= tx1 + 300]
        if not heads:
            continue
        hd = min(heads, key=lambda h: (h[1], abs(h[0] - tx0)))
        rowh = max(hd[3] - hd[1], 4.0)
        cx0, cx1 = hd[0] - 3 * rowh, hd[2] + 3 * rowh
        col = sorted((x for x in ws if x[1] > hd[3] and cx0 <= (x[0] + x[2]) / 2 <= cx1), key=lambda x: x[1])
        marks, last_y = {}, hd[3]
        for x in col:
            if x[1] - last_y > 3.5 * rowh:
                break
            last_y = x[3]
            if _MARKRX.match(x[4]):
                marks.setdefault(x[4], x[5])
        if len(marks) >= 2:
            out.append({"kind": kind.lower(), "title_box": (tx0, ty0, tx1, ty1), "marks": marks,
                        "title_src": prev[5]})
    return out


@functools.lru_cache(maxsize=8)
def _file_schedules(path: str, mtime: float) -> tuple:
    """(schedules [(page, sched)], plan_tokens {page: {text: box}}) over every page of a PDF (~1 s per 100 pages)."""
    import pymupdf

    scheds, plans = [], {}
    with pymupdf.open(path) as d:
        for k, pg in enumerate(d):
            txt = pg.get_text()
            if _SCHED.search(txt):
                for sc in _schedule_marks(pg):
                    scheds.append((k, sc))
            if re.search(r"\bPLAN\b", txt, re.I):
                toks = {}
                for w in pg.get_text("words"):
                    if _MARKRX.match(w[4]):
                        toks.setdefault(w[4], tuple(w[:4]))
                plans[k] = toks
    return tuple(scheds), plans


def check_schedule_tags(path: str, page: int, file: str) -> _Run:
    run = _Run("schedule_tags")
    if not path.lower().endswith(".pdf"):
        return run.skip("schedules are read from PDF text")
    try:
        scheds, plans = _file_schedules(path, Path(path).stat().st_mtime)
    except Exception as e:  # noqa: BLE001
        return run.skip(f"could not read the file ({type(e).__name__})")
    if not scheds:
        return run.skip("no door / window schedule in this file")
    # the same table titled twice (sheet title + table title) is read twice: keep one; a schedule continued on another
    # sheet: its marks count as scheduled for the other part too (no 'tagged but not scheduled' across parts)
    uniq, seen_sets = [], set()
    for sp, sc in scheds:
        key = (sc["kind"], frozenset(sc["marks"]))
        if key not in seen_sets:
            seen_sets.add(key)
            uniq.append((sp, sc))
    scheds = uniq
    all_marks: dict = {}
    for _sp, sc in scheds:
        all_marks.setdefault(sc["kind"], set()).update(sc["marks"])
    for sp, sc in scheds:
        marks = sc["marks"]
        # plan pages: every PLAN page; on the schedule's own page only tokens outside the schedule column
        found: dict = {}
        for k, toks in plans.items():
            for m in marks:
                if m in toks and not (k == sp and toks[m] == marks[m]):
                    if k == sp and abs(toks[m][1] - marks[m][1]) < 1 and abs(toks[m][0] - marks[m][0]) < 1:
                        continue
                    found.setdefault(m, (k, toks[m]))
        run.compared += len(marks)
        share = len(found) / len(marks)
        name = f"{sc['kind']} schedule (page {sp + 1})"
        if share < 0.5:
            run.why = (f"{name}: only {len(found)} of {len(marks)} marks found as text on the plans of this file: "
                       f"tags drawn as outlines, or the plans are in another file — not compared")
            continue
        missing = [m for m in marks if m not in found]
        if not missing:
            run.passed.append({"check": run.check, "what": f"{name}: all {len(marks)} marks are tagged on the plans "
                                                           f"(" + ", ".join(sorted(marks)[:12]) + ") ✓"})
        else:
            ev = [_ev(file, sp, marks[m], f"{sc['kind']} {m}: in the schedule, tagged on no plan") for m in missing]
            run.findings.append(_finding(
                run.check, "warning", 0.6 if len(found) / len(marks) < 0.8 else 0.7,
                f"{sc['kind'].title()}{'s' if len(missing) > 1 else ''} {', '.join(missing)} not on any plan",
                f"The {name} lists {len(marks)} marks; {len(found)} are tagged on the plans of this file, but "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not: a {sc['kind']} missing from the plan, "
                f"or its tag (or the plan sheet) is missing.",
                [{"label": "marks in the schedule", "value": len(marks), "unit": sc["kind"] + "s", "source": name},
                 {"label": "of them tagged on a plan", "value": len(found), "unit": sc["kind"] + "s",
                  "source": "plan sheets' text"}],
                len(missing), 100.0 * len(missing) / len(marks), sc["kind"] + "s", "exact (every mark once)", ev))
        # the other way: a plan tag that is clearly one of these marks (same number + a letter suffix) but unscheduled
        sfx = {re.match(r"^(.*\d)([A-Z]{1,2})$", m).group(1) for m in marks if re.match(r"^.*\d[A-Z]{1,2}$", m)}
        scheduled = all_marks.get(sc["kind"], set(marks))
        extra = {}
        for k, toks in plans.items():
            if k == sp:
                continue
            for t, box in toks.items():
                mm = re.match(r"^(.*\d)([A-Z]{1,2})$", t)
                if mm and mm.group(1) in sfx and t not in scheduled:
                    extra.setdefault(t, (k, box))
        if len(extra) > max(2, 0.2 * len(marks)):  # many 'unscheduled' tags = a part of the schedule we did not read
            run.why = (run.why + "; " if run.why else "") + f"{name}: {len(extra)} plan tags not in the schedule we " \
                                                             f"read: schedule probably only partly read — not reported"
            extra = {}
        for t, (k, box) in sorted(extra.items()):
            run.findings.append(_finding(
                run.check, "warning", 0.6, f"{sc['kind'].title()} {t} is tagged but not scheduled",
                f"The plan on page {k + 1} tags {sc['kind']} {t}, but the {name} has no row {t} (it lists "
                + ", ".join(sorted(m for m in marks if m.startswith(re.match(r'^(.*\d)', t).group(1)))) + ").",
                [{"label": "tagged on the plan", "value": 1, "unit": sc["kind"] + "s", "source": f"page {k + 1}"},
                 {"label": "rows in the schedule", "value": 0, "unit": sc["kind"] + "s", "source": name}],
                1, None, sc["kind"] + "s", "exact", [_ev(file, k, box, f"tag {t} on the plan")]))
    return run


# ----------------------------------------------------------------------------------------------------------------------
# 8. floors line up: stairs, lifts and shafts on one level's plan vs the plans of the levels above / below (a SET:
#    the other pages of this PDF, the other files uploaded together, or same-project siblings in a dataset folder)

PT_M_AT_1 = PT_MM_AT_1 / 1000.0
_VC_KINDS = (("lift", re.compile(r"\b(lifts?|elevators?|elev|hoistway)\b", re.I)),
             ("stair", re.compile(r"\b(stairs?|stairwells?|staircases?)\b", re.I)),
             ("shaft", re.compile(r"\b(shafts?|risers?)\b", re.I)))
_VC_NOT = re.compile(r"\b(landing|lobby|lobbies|hall|store|storage|machine|motor|room|core|area|corridor|under|"
                     r"cupboard|lift\s*off|chair\s*lift|platform|stairlift)\b", re.I)
_VOID = re.compile(r"\b(void|open\s+to\s+below)\b", re.I)
_DERIVED = re.compile(r"\.(vec\d+|s\d+|r\d+|p\d+)\.[a-z0-9]+$", re.I)
_LEVEL_WORDS = re.compile(r"(ground|first|second|third|fourth|fifth|basement|roof|level|floor|storey|story|\bgf\b|\bff\b|"
                          r"\bsf\b|\bl\d+\b|\d+(st|nd|rd|th)\b)", re.I)
ALIGN_TOL_M, FLAG_M = 0.4, 0.7  # agree within 0.4 m (wall faces, 2 pt jitter at 1:200 = 0.14 m); report beyond 0.7 m


def _vc_kind(text: str) -> str | None:
    if _VOID.search(text):
        return "void"
    if _VC_NOT.search(text):
        return None
    for k, rx in _VC_KINDS:
        if rx.search(text):
            return k
    return None


@functools.lru_cache(maxsize=48)
def _vc_elements(path: str, mtime: float, page: int, n: float) -> tuple:
    """Traced outlines of the stairs / lifts / shafts / voids of one plan page: ({kind, name, label, bbox (pt), m
    (bbox in metres), area_m2, polygon}, ...) and every VC label's centre (traced or not) per kind."""
    from stoa.evidence import lineplan
    from stoa.evidence.linework import measure_room

    els, labs_by_kind = [], {}
    for lab in lineplan.labels(path, page):
        kind = _vc_kind(lab["text"])
        if not kind:
            continue
        labs_by_kind.setdefault(kind, []).append(tuple(lab["pt"]))
        try:
            m = measure_room(path, page, lab["pt"], n, tag_box=lab["bbox"])
        except Exception:  # noqa: BLE001
            m = None
        sup, nr = (m or {}).get("support") or 0, (m or {}).get("n_readings") or 1
        if not m or sup < 6 or sup < 0.3 * nr:
            continue
        area = float(m["area_m2"])
        if not (1.0 <= area <= (3000.0 if kind == "void" else 80.0)):
            continue  # a leaked trace (the corridor around it), or a sliver
        b = _bbox_of(m["polygon"])
        k = n * PT_M_AT_1
        els.append({"kind": kind, "name": re.sub(r"\s+", " ", lab["text"]).strip(), "label": list(lab["bbox"]),
                    "bbox": b, "m": [v * k for v in b], "area_m2": area, "polygon": m["polygon"],
                    # a weak trace (few readings agree) may confirm a match, never raise a finding
                    "weak": sup < 0.5 * nr})
    # one element per outline: a fragment inside a bigger outline of the same kind, or the same outline twice
    keep = []
    for e in sorted(els, key=lambda e: -e["area_m2"]):
        if any(o["kind"] == e["kind"] and _inside(e["bbox"], o["bbox"], 2.0) for o in keep):
            continue
        keep.append(e)
    k = n * PT_M_AT_1
    return tuple(keep), {kd: [(x * k, y * k) for x, y in v] for kd, v in labs_by_kind.items()}


def _inside(a, b, pad: float) -> bool:
    return a[0] >= b[0] - pad and a[1] >= b[1] - pad and a[2] <= b[2] + pad and a[3] <= b[3] + pad


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _shift(b, o):
    return [b[0] + o[0], b[1] + o[1], b[2] + o[0], b[3] + o[1]]


def _side_delta(a, b) -> float:
    return max(abs(a[i] - b[i]) for i in range(4))


def _align(A: list, B: list) -> tuple | None:
    """The offset (metres) that lays plan A over plan B: the one the most same-kind stair / lift / shaft pairs agree on
    (centres within ALIGN_TOL_M, sizes within 25 % / 0.5 m), at least 2 pairs. (offset, pairs) or None."""
    def same_size(a, b):
        wa, ha, wb, hb = a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]
        return abs(wa - wb) <= max(0.5, 0.25 * max(wa, wb)) and abs(ha - hb) <= max(0.5, 0.25 * max(ha, hb))

    A = [a for a in A if a["kind"] != "void"]
    B = [b for b in B if b["kind"] != "void"]
    best = None
    for a0 in A:
        for b0 in B:
            if a0["kind"] != b0["kind"] or not same_size(a0["m"], b0["m"]):
                continue
            o = (_centre(b0["m"])[0] - _centre(a0["m"])[0], _centre(b0["m"])[1] - _centre(a0["m"])[1])
            pairs, used = [], set()
            for a in A:
                cands = [(math.dist(_centre(_shift(a["m"], o)), _centre(b["m"])), j) for j, b in enumerate(B)
                         if j not in used and b["kind"] == a["kind"] and same_size(a["m"], b["m"])]
                cands = [c for c in cands if c[0] <= ALIGN_TOL_M]
                if cands:
                    j = min(cands)[1]
                    used.add(j)
                    pairs.append((a, B[j]))
            rigid = sum(1 for a, _b in pairs if a["kind"] != "stair")
            res = sum(math.dist(_centre(_shift(a["m"], o)), _centre(b["m"])) for a, b in pairs)
            score = (len(pairs), rigid, -res)
            if best is None or score > best[0]:
                best = (score, o, pairs)
    if best is None or best[0][0] < 2:
        return None
    pairs = best[2]
    # the offset from the rigid boxes (lifts, shafts) when there are any: a stair's traced outline wanders
    use = [(a, b) for a, b in pairs if a["kind"] != "stair"] or pairs
    import statistics

    dx = statistics.median(_centre(b["m"])[0] - _centre(a["m"])[0] for a, b in use)
    dy = statistics.median(_centre(b["m"])[1] - _centre(a["m"])[1] for a, b in use)
    return (dx, dy), pairs


def _set_key(name: str) -> str:
    """Sheets of one building: ISO 19650 'SEN-DFE-XX-01-D-X-0202' -> 'SEN-DFE-XX'; else the stem without its level
    words ('secondary_type_3_600_first_floor_001' -> 'secondary type 3 600 001')."""
    stem = Path(name).stem
    parts = stem.split("-")
    if len(parts) >= 6 and len(parts[3]) == 2:
        return "-".join(parts[:3]).upper()
    t = re.sub(r"[_\-.]+", " ", stem.lower())
    return re.sub(r"\s+", " ", _LEVEL_WORDS.sub(" ", t)).strip()


def _siblings(path: str, file: str) -> list[tuple[str, str]]:
    """(abs path, virtual path) of the other PDFs of the same set: uploaded together (one upload folder) or, in a
    dataset folder, same-project file names. Derived caches (*.vec6.pdf, *.s6000.png) are never sheets."""
    p = Path(path)
    out = []
    up = file.startswith("upload/")
    key = _set_key(p.name)
    try:
        names = sorted(q.name for q in p.parent.iterdir() if q.is_file())
    except OSError:
        return []
    for nm in names:
        if nm == p.name or not nm.lower().endswith(".pdf") or nm.startswith(".") or _DERIVED.search(nm):
            continue
        if not up and _set_key(nm) != key:
            continue
        vdir = file.rsplit("/", 1)[0] if "/" in file else ""
        out.append((str(p.parent / nm), f"{vdir}/{nm}" if vdir else nm))
    return out[:12]


@functools.lru_cache(maxsize=64)
def _sheet_kinds(path: str, mtime: float, virtual: str) -> tuple:
    from stoa.features.project import classify_file

    return tuple(classify_file(virtual, path))


def _project_overrides(path: str) -> dict:
    """The visitor's own names for the sheets of an uploaded set (T3's project file next to the uploads: 'this sheet
    is Level 01', 'leave it out'): {sheet id 'upload/<id>/<name>#<page>': user dict}. {} when there is none."""
    import json

    f = Path(path).parent / ".stoa_project.json"
    try:
        d = json.loads(f.read_text())
        return {s["id"]: s["user"] for s in d.get("sheets") or [] if s.get("user")}
    except Exception:  # noqa: BLE001
        return {}


def _set_sig(p: Path, file: str) -> str:
    """What a set-level report depends on besides the file itself: the PDFs uploaded with it and the visitor's
    level / role names for them (the router's cache key)."""
    if not file.startswith("upload/"):
        return ""
    try:
        names = sorted(q.name for q in p.parent.iterdir() if q.suffix.lower() == ".pdf")
    except OSError:
        names = []
    ov = _project_overrides(str(p))
    return "|".join(names) + "#" + repr(sorted((k, sorted(v.items())) for k, v in ov.items()))


def _effective(sheets: list, overrides: dict) -> list:
    if not overrides:
        return sheets
    try:
        from stoa.features.project import effective
    except Exception:  # noqa: BLE001
        return sheets
    out = []
    for s in sheets:
        u = overrides.get(s.get("id"))
        if u:
            e = effective({**s, "user": u})
            if e.get("include") is False:
                e = {**e, "kind": "excluded"}
            out.append(e)
        else:
            out.append(s)
    return out


def _level_num(storey) -> float | None:
    try:
        return float(storey)
    except (TypeError, ValueError):
        return None


def _void_over(up: list, up_name: str, low: tuple, o: tuple, run: _Run, notes: list | None) -> None:
    """The voids of the upper plan (laid onto the lower plan by offset o, metres) vs the room below them: a
    double-height hall under its void. Agreement (within 5 %) is a passed line; anything else only a note."""
    voids = [v for v in up if v["kind"] == "void"]
    groups: list[list] = []  # voids that touch (within 1 m) = one opening ('Void' printed twice over one hall)
    for v in voids:
        hit = [g for g in groups if any(_iou(_pad(v["m"], 0.5), _pad(w["m"], 0.5)) > 0 for w in g)]
        for g in hit:
            groups.remove(g)
        groups.append([v] + [w for g in hit for w in g])
    for g in groups[:4]:
        _void_group(g, up_name, low, o, run, notes)


def _void_group(voids: list, up_name: str, low: tuple, o: tuple, run: _Run, notes: list | None) -> None:
    from stoa.evidence import lineplan
    from stoa.evidence.linework import measure_room

    lp, lpage, ln, lname = low
    k = ln * PT_M_AT_1
    vm = _union([_shift(v["m"], o) for v in voids])
    tot = sum(v["area_m2"] for v in voids)
    try:
        labs = [lab for lab in lineplan.labels(lp, lpage) if not _vc_kind(lab["text"])
                and vm[0] <= lab["pt"][0] * k <= vm[2] and vm[1] <= lab["pt"][1] * k <= vm[3]]
    except Exception:  # noqa: BLE001
        labs = []
    for lab in labs[:3]:
        try:
            m = measure_room(lp, lpage, lab["pt"], ln, tag_box=lab["bbox"])
        except Exception:  # noqa: BLE001
            m = None
        if not m or (m.get("support") or 0) < 8:
            continue
        ra = float(m["area_m2"])
        name = re.sub(r"\s+", " ", lab["text"]).strip()
        pct = 100.0 * (tot - ra) / ra
        if abs(pct) <= 5.0:
            run.passed.append({"check": run.check, "what": f"{up_name}: void{'s' if len(voids) > 1 else ''} "
                                                           f"{tot:.1f} m² over {name} on {lname} ({ra:.1f} m², "
                                                           f"{pct:+.1f} %) ✓"})
            return
        if notes is not None:
            notes.append(f"{up_name}: voids {tot:.1f} m² over {name} on {lname} ({ra:.1f} m²): a void over part of "
                         f"a room, not compared")
        return


def check_floors_align(path: str, page: int, file: str, notes: list | None = None) -> _Run:
    run = _Run("floors_align")
    if not path.lower().endswith(".pdf"):
        return run.skip("outlines are traced from vector wall lines (PDF) only")
    try:  # this page first (~0.1 s): the whole set is read only for a whole floor plan of a known level (a 49-page
        # set took 26 s under load to learn that its page 33 is not a plan)
        import pymupdf

        from stoa.sheet.sets import classify

        with pymupdf.open(path) as d:
            many = len(d) > 1
        if many and f"{file}#{page}" not in (_project_overrides(path) if file.startswith("upload/") else {}):
            s0 = classify(path, page)
            if s0.kind != "floor_plan" or s0.partial or _level_num(s0.storey) is None:
                return run.skip("not a whole floor plan of a known level")
    except Exception as e:  # noqa: BLE001
        return run.skip(f"could not classify the sheet ({type(e).__name__})")
    overrides = _project_overrides(path) if file.startswith("upload/") else {}
    try:
        me_all = _effective(list(_sheet_kinds(path, Path(path).stat().st_mtime, file)), overrides)
    except Exception as e:  # noqa: BLE001
        return run.skip(f"could not classify the sheet ({type(e).__name__})")
    me = next((s for s in me_all if s["page"] == page), None)
    if not me or me["kind"] != "floor_plan" or me.get("partial") or _level_num(me.get("storey")) is None:
        return run.skip("not a whole floor plan of a known level")
    lv = _level_num(me["storey"])
    others = [(path, file, s) for s in me_all if s["page"] != page]
    for sp, sv in _siblings(path, file):
        try:
            others += [(sp, sv, s) for s in _effective(list(_sheet_kinds(sp, Path(sp).stat().st_mtime, sv)),
                                                       overrides)]
        except Exception:  # noqa: BLE001
            continue
    plans = [(sp, sv, s) for sp, sv, s in others if s["kind"] == "floor_plan" and not s.get("partial")
             and _level_num(s.get("storey")) is not None and _level_num(s["storey"]) != lv
             and (not me.get("building") or not s.get("building") or s["building"] == me["building"])]
    below = [x for x in plans if _level_num(x[2]["storey"]) < lv]
    above = [x for x in plans if _level_num(x[2]["storey"]) > lv]
    pick = []
    if below:
        top = max(_level_num(x[2]["storey"]) for x in below)
        pick += [x for x in below if _level_num(x[2]["storey"]) == top][:1]
    if above:
        bot = min(_level_num(x[2]["storey"]) for x in above)
        pick += [x for x in above if _level_num(x[2]["storey"]) == bot][:1]
    if not pick:
        return run.skip("no plan of the level above or below in this set (upload the other floors together)")
    n_me = me.get("page_ratio") or me.get("ratio") or _plan_ratio(path, page)[0]
    if not n_me:
        return run.skip("no printed scale on this plan")
    A, labsA = _vc_elements(path, Path(path).stat().st_mtime, page, float(n_me))
    if not A:
        return run.skip("no stair, lift or shaft outline could be traced on this plan")
    my_name = me.get("level") or f"level {me['storey']}"
    why = []
    for sp, sv, s in pick:
        other_name = s.get("level") or f"level {s['storey']}"
        n_o = s.get("page_ratio") or s.get("ratio") or _plan_ratio(sp, s["page"])[0]
        if not n_o:
            why.append(f"{other_name}: no printed scale")
            continue
        B, labsB = _vc_elements(sp, Path(sp).stat().st_mtime, s["page"], float(n_o))
        al = _align(list(A), list(B))
        if al is None:
            why.append(f"{other_name}: fewer than 2 stairs / lifts traced on both plans to lay them over each other")
            continue
        o, pairs = al
        used = set()
        for a in A:
            if a["kind"] == "void":
                continue
            am = _shift(a["m"], o)
            cands = sorted(((_iou(am, b["m"]), j) for j, b in enumerate(B) if b["kind"] == a["kind"] and j not in used),
                           reverse=True)
            if cands and cands[0][0] > 0.05:
                iou, j = cands[0]
                b = B[j]
                used.add(j)
                run.compared += 1
                d = _side_delta(am, b["m"])
                if d <= ALIGN_TOL_M:
                    run.passed.append({"check": run.check, "what": f"{a['name']} ({my_name}) and {b['name']} "
                                                                   f"({other_name}) line up (sides within {d:.2f} m) ✓"})
                    continue
                if d <= FLAG_M or a["weak"] or b["weak"] or a["kind"] == "stair":
                    # a stair's traced outline changes from floor to floor (flights, landings, the roof stair next to
                    # it: DfE PRI / SEC and the 2014 600-place secondary, 10 false alarms in F3's bench) -> it only
                    # helps lay the plans over each other and can only agree
                    if notes is not None:
                        notes.append(f"{a['name']} ({my_name}) vs {b['name']} ({other_name}): traced sides {d:.2f} m "
                                     f"apart, " + ("a stair's outline differs between floors (flights, landings)"
                                                  if a["kind"] == "stair" else "near what tracing can tell apart")
                                     + ", not reported")
                    continue
                ca, cb = _centre(am), _centre(b["m"])
                mv = math.dist(ca, cb)
                wa, ha = am[2] - am[0], am[3] - am[1]
                wb, hb = b["m"][2] - b["m"][0], b["m"][3] - b["m"][1]
                rel = "above" if _level_num(s["storey"]) > lv else "below"
                what = (f"The {a['kind']} traced around '{a['name']}' on {my_name} is {wa:.1f} × {ha:.1f} m; the one "
                        f"{rel} it on {other_name} ('{b['name']}') is {wb:.1f} × {hb:.1f} m, its centre {mv:.2f} m "
                        f"away and its walls up to {d:.1f} m off, with the two plans laid over each other by the "
                        f"stairs / lifts that do line up ({len(pairs)}). A {a['kind']} shaft must stack: one of the two "
                        f"plans is drawn wrong, or the room is not the {a['kind']}.")
                run.findings.append(_finding(
                    run.check, "warning", 0.6 if a["kind"] == "stair" else 0.65,
                    f"{a['name']}: {my_name} and {other_name} do not line up ({d:.1f} m)", what,
                    [{"label": f"{my_name} outline", "value": round(wa * ha, 1), "unit": "m2", "source": a["name"]},
                     {"label": f"{other_name} outline", "value": round(wb * hb, 1), "unit": "m2", "source": b["name"]}],
                    d, None, "m", f"{FLAG_M:g} m (traced walls agree within ~{ALIGN_TOL_M:g} m when floors stack)",
                    [_ev(file, page, a["bbox"], f"{a['name']} on {my_name}", exact=False, polygon=a["polygon"]),
                     _ev(sv, s["page"], b["bbox"], f"{b['name']} on {other_name}", exact=False,
                         polygon=b["polygon"])],
                    zoom=_pad(a["bbox"], 30)))
                continue
            # nothing of that kind at this place on the other plan: never a finding (a lift may serve the lower floors
            # only and the upper plan may not extend there: DfE SEC Level 01's second lift, F3 bench 21:35)
            if notes is not None:
                notes.append(f"{a['name']} ({my_name}): no {a['kind']} traced at that place on {other_name}")
        # voids on the upper plan over the room below: shown when they agree (no finding: a void may be part of a room)
        if _level_num(s["storey"]) > lv:
            _void_over(B, other_name, (path, page, float(n_me), my_name), (-o[0], -o[1]), run, notes)
        else:
            _void_over(A, my_name, (sp, s["page"], float(n_o), other_name), o, run, notes)
    if why:
        run.why = "; ".join(why)
    if run.compared == 0:
        run.skip(run.why or "no stair, lift or shaft on both plans")
    return run


# ----------------------------------------------------------------------------------------------------------------------
# 9. plan vs 3D model: a floor plan rendered from an IFC model (IfcConvert: every drawn product carries its GlobalId in
# the SVG twin) vs the model's elements of that storey that cross the plan's cut plane (W1's stoa.kg cut compare, the
# customer view's 'Differences found'). Elements without parsed geometry are never counted (their height is unknown).
# A finding needs THREE readings to agree that something is missing: the model has the element crossing the cut, the
# SVG tags no such GlobalId, AND the PDF has no linework where the model puts it. And the sheet must confirm the cut
# height first (>= 90 % of the storey's crossing elements of the compared kinds are drawn), else the render was cut at
# another height (frigento: plan-01 draws 6 doors for 3 modelled) and gaps are notes, not findings.

_PVM_KINDS = (("doors", ("IfcDoor",)), ("windows", ("IfcWindow",)), ("walls", ("IfcWall", "IfcWallStandardCase")),
              ("columns", ("IfcColumn",)), ("rooms", ("IfcSpace",)))
_PVM_REPORT = {"doors", "windows", "columns"}  # small rigid things: a missing one is a real gap; walls / rooms pass-only
_PVM_CUT_OK = 0.9


def _pvm_model(path: str) -> tuple[str | None, str]:
    """(model path, plan path) as the ifc helpers want them: dataset-relative under the data root, else absolute
    (an upload / a copy with the .ifc next to it: '<model stem>_plan-NN-<level>.pdf' + '<model stem>.ifc')."""
    p = Path(path)
    try:
        from stoa.data import data_root

        rel = str(p.resolve().relative_to(data_root().resolve()))
    except (ValueError, OSError):
        rel = None
    if rel is not None:
        from stoa.solvers.ifc.solver import model_for

        return model_for(rel), rel
    for m in sorted(p.parent.glob("*.ifc")):
        if p.stem.startswith(m.stem + "_plan-"):
            return str(m), str(p)
    return None, str(p)


@functools.lru_cache(maxsize=16)
def _pvm_lines(path: str, mtime: float) -> tuple:
    """Bounding boxes (pdf_pt) of the page's linework, room fills left out (an IfcSpace fill covers the whole room)."""
    import pymupdf

    from stoa.evidence.ifcplan_pdf import _is_space_fill

    with pymupdf.open(path) as doc:
        if len(doc) == 0:
            return ()
        return tuple((r.x0, r.y0, r.x1, r.y1) for d in doc[0].get_drawings()
                     if not _is_space_fill(d.get("fill")) and (r := d.get("rect")) is not None)


def _pvm_drawn_at(lines: tuple, box) -> int:
    """How many pieces of linework sit mostly (>= 70 %) inside `box` (padded 2 pt): > 0 = something is drawn there."""
    x0, y0, x1, y1 = box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2
    n = 0
    for a, b, c, d in lines:
        w, h = max(c - a, 0.5), max(d - b, 0.5)
        iw = max(0.0, min(c, x1) - max(a, x0)) if c - a >= 0.5 else (1.0 if x0 <= a <= x1 else 0.0) * w
        ih = max(0.0, min(d, y1) - max(b, y0)) if d - b >= 0.5 else (1.0 if y0 <= b <= y1 else 0.0) * h
        if iw * ih >= 0.7 * w * h and w * h < 4 * max((x1 - x0) * (y1 - y0), 1.0):
            n += 1
    return n


def check_plan_vs_model(path: str, page: int, file: str, notes: list | None = None) -> _Run:
    run = _Run("plan_vs_model")
    if not path.lower().endswith(".pdf") or "_plan-" not in Path(path).stem:
        return run.skip("not a floor plan rendered from an IFC model (no linked 3D model)")
    from stoa.kg.graph import CUT_HEIGHT, _crosses, floor_offset
    from stoa.solvers.ifc.index import load_index
    from stoa.solvers.ifc.plan2d import plan_items, project_box
    from stoa.solvers.ifc.solver import plan_storey

    model, plan = _pvm_model(path)
    if not model:
        return run.skip("no IFC model linked to this sheet")
    items = plan_items(plan)
    if not items:
        return run.skip("this sheet carries no IFC element tags (no SVG twin): nothing to link to the model")
    idx = load_index(model)
    st, _why = plan_storey(plan, idx)
    if st is None:
        return run.skip("could not tell which storey of the model this plan shows")
    cut_z = (st.elevation or 0.0) + floor_offset(idx) + CUT_HEIGHT
    on = idx.in_storey(st.guid)
    rows = []
    for label, classes in _PVM_KINDS:
        mine = [e for e in on if e.cls in classes or e.eff_cls in classes]
        crossing = [e for e in mine if _crosses(e, cut_z) is True]
        above_below = sum(1 for e in mine if _crosses(e, cut_z) is False)
        tagged = {g: it for g, it in items.items() if it.cls in classes}
        if not tagged:  # the sheet tags none of this kind: no 2D data (some renders tag rooms only)
            if crossing:
                run.why = (run.why + "; " if run.why else "") + f"the sheet tags no {label} (model: {len(crossing)})"
            continue
        drawn = [e for e in crossing if e.guid in tagged]
        missing = [e for e in crossing if e.guid not in tagged]
        extra = [g for g in tagged if g not in idx.elements]
        rows.append((label, mine, crossing, above_below, drawn, missing, extra, tagged))
    if not rows:
        return run.skip(run.why or "no element of this storey to compare")
    n_cross = sum(len(r[2]) for r in rows)
    n_drawn = sum(len(r[4]) for r in rows)
    cut_ok = n_cross > 0 and n_drawn / n_cross >= _PVM_CUT_OK
    lines = _pvm_lines(path, Path(path).stat().st_mtime) if cut_ok else ()
    where = f"'{st.name}'"
    for label, mine, crossing, above_below, drawn, missing, extra, tagged in rows:
        run.compared += 1
        gone = []  # missing in the SVG AND blank in the PDF where the model puts them
        for e in missing:
            box = project_box(model, plan, e) if cut_ok else None
            if box is not None and _pvm_drawn_at(lines, box) == 0:
                gone.append((e, box))
        # this kind itself must confirm the reading: >= 90 % of its crossing elements are drawn (tagged or not), else the
        # renderer skips this kind here (frigento ground floor: 3 tagged + 3 untagged of 7 doors) and gaps are notes
        kind_ok = bool(crossing) and (len(crossing) - len(gone)) / len(crossing) >= _PVM_CUT_OK
        if label in _PVM_REPORT and cut_ok and ((gone and kind_ok) or extra):
            gone = gone if kind_ok else []
            k = len(gone) + len(extra)
            names = ", ".join(sorted({(e.name or e.guid)[:24] for e, _ in gone}))[:120]
            ev = [dict(_ev(file, 0, b, f"{label[:-1]} '{(e.name or '?')[:40]}' in the 3D model, not drawn here",
                           exact=False), ifc_guid=e.guid) for e, b in gone[:12]]
            ev += [dict(_ev(file, 0, tagged[g].bbox, f"{label[:-1]} drawn here, its GlobalId is not in the 3D model"),
                        ifc_guid=g) for g in extra[:12]]
            what = (f"On {where} the 3D model has {len(crossing)} {label} crossing the plan's cut height "
                    f"(floor + {CUT_HEIGHT:g} m); the plan draws {len(drawn) + len(extra)}.")
            if gone:
                what += f" Not drawn: {names} (nothing is drawn where the model puts them)."
            if extra:
                what += f" {len(extra)} drawn {label} are not in the model."
            run.findings.append(_finding(
                "plan_vs_model", "warning", 0.85,
                f"Plan and 3D model disagree: {k} {label[:-1] if k == 1 else label}",
                what,
                [{"label": "3D model (crossing the cut)", "value": len(crossing), "unit": label,
                  "source": Path(model).name},
                 {"label": "drawn on this plan", "value": len(drawn) + len(extra), "unit": label,
                  "source": "element tags + linework"}],
                k, None, label, "exact (GlobalId match), plus nothing drawn at the model's position",
                ev, zoom=_pad(_union(e["bbox"] for e in ev), 20)))
            continue
        untagged = len(missing) - len(gone) if cut_ok else 0
        if not missing and not extra:
            extra_txt = f"; {above_below} more sit entirely above / below the cut and are not drawn (normal)" \
                if above_below else ""
            run.passed.append({"check": "plan_vs_model", "what":
                               f"{label.capitalize()} on {where}: {len(drawn)} drawn = {len(crossing)} in the 3D model "
                               f"at the plan's cut height{extra_txt}"})
        elif cut_ok and untagged == len(missing) and not extra:
            run.passed.append({"check": "plan_vs_model", "what":
                               f"{label.capitalize()} on {where}: {len(drawn)} tagged + {untagged} drawn without their "
                               f"IFC tag = {len(crossing)} in the 3D model"})
        elif notes is not None:
            notes.append(f"Plan vs 3D model, {label} on {where}: model {len(crossing)} crossing the cut, drawn "
                         f"{len(drawn)}" + ("" if cut_ok else " (this render seems cut at another height: "
                                            f"only {n_drawn} of {n_cross} crossing elements are drawn; not reported)")
                         + (f"; {len(gone)} not drawn at all, but too many of this kind are missing on this render to "
                            "call it a drawing slip (not reported)" if cut_ok and gone and not kind_ok else ""))
    return run


# ----------------------------------------------------------------------------------------------------------------------
# entry point

def _image_layers(path: str):
    """Every text layer of the picture that something already read (an upload render's PDF words, memory, the OCR
    cache, the VLM layer on disk), in that order; never starts a read. Not areatable._cached_layer /
    raster.sheet.cached_layer: both end in text_layer(), which runs a cold OCR read (up to 120 s on the OCR server) when
    only the VLM layer is on disk (F3 bench 21:40 on an older S-100F scan upload). Several can exist: the API process
    that answered a question keeps ITS layer in memory, which may differ from the one on disk (scanned AFCEC PDF upload:
    the in-memory layer's table rows looked damaged, the disk layer's read cleanly)."""
    try:
        import json as _json

        from stoa import ocr
        from stoa.solvers.raster import textlayer as T
    except Exception:  # noqa: BLE001
        return
    seen = set()

    def new(tl):
        if tl is None or id(tl) in seen:
            return False
        seen.add(id(tl))
        return True

    for step in ("render", "mem", "ocr", "disk"):
        try:
            if step == "render":
                cands = [T.pdf_render_layer(path)]
            elif step == "mem":
                tag = f"{path}{T.file_tag(path)}|"
                cands = [v for k, v in list(getattr(T, "_mem", {}).items()) if str(k).startswith(tag)]
            elif step == "ocr":
                cands = [ocr.text_layer(path)] if ocr.cached(path) and T.ocr_suits(path) else []  # a disk load
            else:
                f = T._disk_key(path, 1100, "local")
                cands = []
                if f.exists():
                    d = _json.loads(f.read_text())
                    d["items"] = [T.TextItem(**i) for i in d["items"]]
                    d["size"] = tuple(d["size"])
                    cands = [T.TextLayer(**d)]
        except Exception:  # noqa: BLE001
            continue
        for tl in cands:
            if new(tl):
                yield tl


def _image_layer(path: str):
    """The picture's first already-read text layer (see _image_layers), or None; never starts a read."""
    try:
        return next(iter(_image_layers(path)), None)
    except Exception:  # noqa: BLE001
        return None


def _page_is_scan(pdf: str, page: int) -> bool:
    """normalize.scan_png's cheap test on any page: no rotation, (almost) no text, a small content stream, one image over
    >= 50 % of the page. Lets a VECTOR set skip the one-page copy a scan needs."""
    import pymupdf

    from stoa.formats.pages import _stream_bytes

    with pymupdf.open(pdf) as d:
        if page >= len(d):
            return False
        pg = d[page]
        imgs = pg.get_images(full=True)
        if not imgs or pg.rotation or _stream_bytes(pg) > 20_000 or len((pg.get_text() or "").strip()) >= 20:
            return False
        area = max(pg.rect.get_area(), 1e-9)
        return any((r & pg.rect).get_area() / area >= 0.5 for im in imgs for r in pg.get_image_rects(im[0]))


def _scan_of(pdf: str, page: int = 0) -> tuple[str, float] | None:
    """(rendered PNG, px per pt) when that page of the PDF is a scan; the SAME files the upload readers read and OCR
    (page 0: normalize.scan_png; page k: pages.one_page_copy into normalize._cache_dir, then scan_png, as
    normalize.prepare does), so their text layer is there after the first question. None for a drawn page / on error."""
    try:
        from stoa.formats.normalize import _cache_dir, scan_png

        if page == 0:
            return scan_png(pdf)
        if not _page_is_scan(pdf, page):
            return None
        from stoa.formats.pages import one_page_copy

        return scan_png(one_page_copy(pdf, page, _cache_dir(Path(pdf))))
    except Exception:  # noqa: BLE001 — a failed scan check leaves the PDF to the PDF checks
        return None


def _photo_of(img: str) -> tuple[str, list] | None:
    """(deskewed copy, 3x3 deskewed px -> photo px) when O1's photo step already rectified this phone photo of a print
    (its '<stem>.photo.json' sidecar in normalize._cache_dir, valid for this file): the upload readers OCR THAT copy,
    sometimes never the raw photo (3 of 12 photo uploads here). Cache-only: never deskews in a request."""
    try:
        import json as _json

        from stoa.formats.normalize import _cache_dir
        from stoa.solvers.raster import photo as PH

        if not PH.enabled():
            return None
        src = Path(img)
        side = Path(_cache_dir(src)) / f"{src.stem}.photo{'' if PH.FLATTEN else '-nf'}.json"
        got = _json.loads(side.read_text())
        key = {"v": PH.VERSION, "flat": PH.FLATTEN, "mtime": src.stat().st_mtime, "size": src.stat().st_size}
        d = got.get("d") if got.get("key") == key else None
        if not d or not Path(d["path"]).exists():
            return None
        return str(d["path"]), d["to_photo"]
    except Exception:  # noqa: BLE001 — no / stale sidecar: not a photo we know
        return None


def _to_photo(findings: list, passed: list, H) -> None:
    """Evidence on the deskewed copy (image_px) -> the raw photo's pixels through the homography (bbox = the box of the
    four mapped corners), so the viewer (which shows the photo) outlines the right spot."""
    import numpy as np

    M = np.asarray(H, dtype=float)

    def pt(x, y):
        v = M @ np.array([x, y, 1.0])
        return float(v[0] / v[2]), float(v[1] / v[2])

    def box(b):
        if not b:
            return b
        ps = [pt(b[0], b[1]), pt(b[2], b[1]), pt(b[2], b[3]), pt(b[0], b[3])]
        return [round(min(p[0] for p in ps), 1), round(min(p[1] for p in ps), 1), round(max(p[0] for p in ps), 1),
                round(max(p[1] for p in ps), 1)]

    for item in [*findings, *passed]:
        moved = False
        for e in item.get("evidence") or []:
            if e.get("bbox_space") == "image_px":
                e["bbox"] = box(e.get("bbox"))
                if e.get("polygon"):
                    e["polygon"] = [[round(c, 1) for c in pt(x, y)] for x, y in e["polygon"]]
                moved = True
        if moved and item.get("zoom"):
            item["zoom"] = box(item["zoom"])


def _shrunk_of(img: str) -> tuple[str, float] | None:
    """(the downsized copy the upload readers read, its factor) of a BIG picture: '<stem>.s6000.png' made by
    normalize.shrunk_copy (pictures over 6000 px are read at 6000 px), only when its sidecar is valid (same source size,
    SHRINK_VERSION, newer than the picture). Cache-only: never shrinks in a request. X1's scans of Stoa-like sheets at
    6623 x 4678 / 6800 x 4400 px (S-100F, A-101F, AFCEE A-101, M-101A) had their text layer ONLY on that copy, so the
    self-check said 'no text layer yet' after the visitor's questions had read it (F3 01:2x)."""
    import json

    p = Path(img)
    if re.search(r"\.s\d+$", p.stem) or p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return None
    try:
        from PIL import Image

        from stoa.formats import normalize as N

        dst = N._cache_dir(p) / f"{p.stem}.s{N.IMAGE_MAX_SIDE}{'.jpg' if p.suffix.lower() != '.png' else '.png'}"
        meta = dst.with_name(dst.name + ".json")
        if not (dst.exists() and meta.exists() and dst.stat().st_mtime >= p.stat().st_mtime):
            return None
        m = json.loads(meta.read_text())
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(p) as im:
            size = list(im.size)
        if m.get("src_size") != size or m.get("v", 1) != N.SHRINK_VERSION:
            return None
        f = float(m["factor"])
    except Exception:  # noqa: BLE001
        return None
    return (str(dst), f) if 0.0 < f < 1.0 else None


def _unshrink(findings: list, passed: list, factor: float) -> None:
    """Evidence on the downsized copy (image_px) -> the uploaded picture's pixels."""
    def box(b):
        return [round(float(v) / factor, 1) for v in b] if b else b

    for item in [*findings, *passed]:
        moved = False
        for e in item.get("evidence") or []:
            if e.get("bbox_space") == "image_px":
                e["bbox"] = box(e.get("bbox"))
                if e.get("polygon"):
                    e["polygon"] = [[round(x / factor, 1), round(y / factor, 1)] for x, y in e["polygon"]]
                moved = True
        if moved and item.get("zoom"):
            item["zoom"] = box(item["zoom"])


def _px_to_pt(findings: list, passed: list, file: str, zoom: float, page: int = 0) -> None:
    """Evidence on a scanned PDF page's rendered picture (image_px) -> that page's points, so the viewer (which shows
    the PDF) outlines the right spot (the same map as takeoff.px_to_pt; a one-page copy keeps size and coordinates)."""
    def box(b):
        return [round(float(v) / zoom, 2) for v in b] if b else b

    for item in [*findings, *passed]:
        moved = False
        for e in item.get("evidence") or []:
            if e.get("bbox_space") == "image_px":
                e["file"], e["page"], e["bbox_space"], e["bbox"] = file, page, "pdf_pt", box(e.get("bbox"))
                if e.get("polygon"):
                    e["polygon"] = [[round(x / zoom, 2), round(y / zoom, 2)] for x, y in e["polygon"]]
                moved = True
        if moved and item.get("zoom"):
            item["zoom"] = box(item["zoom"])


def check(path: str, page: int = 0, file: str | None = None, checks=None) -> dict:
    """Run every check on one page of a sheet. `path` = absolute path on disk; `file` = the path evidence should
    carry (dataset-relative or the upload's virtual path; default = path). Never raises."""
    t0 = time.perf_counter()
    path = str(path)
    file = file or path
    page = int(page or 0)
    want = set(checks or ALL_CHECKS)
    runs: list[_Run] = []
    notes: list[str] = []
    is_pdf = path.lower().endswith(".pdf")
    scan_zoom, rep_page = None, page
    if is_pdf:  # a scanned print (the page is one picture, no text, no lines): check the picture
        scan = _scan_of(path, page)
        if scan:
            (path, scan_zoom), page = scan, 0  # the picture is one page; the report keeps the PDF's page
            is_pdf = False
            notes.append("Scanned PDF (the page is one picture): checked like a picture, places shown on the page.")
    layer, photo_h, shrink = None, None, None
    if not is_pdf:
        layer = _image_layer(path)
        sh = _shrunk_of(path) if layer is None and not scan_zoom else None
        if sh and _image_layer(sh[0]) is not None:  # a big picture read at 6000 px: check the copy that was read
            (path, shrink), layer = sh, _image_layer(sh[0])
            notes.append(f"Large picture: checked on the {round(100 * shrink)} % copy the readers read, places shown "
                         "on the picture.")
        ph = _photo_of(path) if layer is None and not scan_zoom else None
        if ph and _image_layer(ph[0]) is not None:  # a phone photo read only through its deskewed copy
            (path, photo_h), layer = ph, _image_layer(ph[0])
            notes.append("Phone photo of a print: checked on its straightened copy, places shown on the photo.")
        if layer is None:
            notes.append("This picture has no text layer yet (ask any question about it first, which reads it): "
                         "only checks that need no text ran.")

    def safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            r = _Run(fn.__name__.replace("check_", ""))
            if r.check not in LABELS:
                r.check = "room_tags"
            r.status, r.why = "failed", f"{type(e).__name__}: {e}"[:200]
            return r

    if "area_table_sums" in want:
        runs.append(safe(check_area_table, path, page, file))
    if want & {"dimension_chains", "dimension_drawn"}:
        got = safe(check_dimensions, path, page, file) if is_pdf else safe(_dims_on_picture, path, file)
        runs += [r for r in (got if isinstance(got, tuple) else (got,)) if r.check in want]
    if "scale_bar" in want:
        runs.append(safe(check_scale_bar, path, page, file, layer, notes=notes) if (is_pdf or layer is not None)
                    else _Run("scale_bar").skip("no text layer"))
    if "room_tags" in want:
        rt = safe(check_room_tags, path, page, file, layer) if (is_pdf or layer is not None) \
            else _Run("room_tags").skip("no text layer")
        if not is_pdf and rt.findings:
            # on a picture the tags are OCR text: X1's scanned AFCEE A-101 (experiments/f3/scans32.txt, F3 01:1x) gave 4
            # false alarms, 'Room number 2.06 is used twice: Operations Maniager Office / Operations Manager Office'
            # (one tag read twice, spelt two ways) and 'Room 1.00 103 SF vs 108 SF in the RCP' (the vector sheet agrees):
            # agreements only, like the picture's dimensions and room areas
            notes.append(f"{len(rt.findings)} room tag reading(s) disagree on this picture ("
                         + "; ".join(f["title"] for f in rt.findings[:3])
                         + "): not reported, an OCR misread looks the same as a slip; check on the PDF")
            rt.findings = []
            if not rt.passed:
                rt.skip("room tags read by OCR disagree: not reported (an OCR misread looks the same as a slip)")
        runs.append(rt)
    if "room_areas" in want:
        runs.append(safe(check_room_areas, path, page, file, notes=notes) if is_pdf
                    else safe(_room_areas_on_picture, path, file, notes=notes))
    if "schedule_tags" in want:
        runs.append(safe(check_schedule_tags, path, page, file))
    if "floors_align" in want:
        runs.append(safe(check_floors_align, path, page, file, notes=notes))
    if "plan_vs_model" in want:
        runs.append(safe(check_plan_vs_model, path, page, file, notes=notes) if is_pdf
                    else _Run("plan_vs_model").skip("not a floor plan rendered from an IFC model"))
    for r in runs:  # a check that ran but had nothing to compare did not check anything (QA 21:35: AFCEC PNG headline)
        if r.status == "ran" and not r.compared and not r.findings and not r.passed:
            r.skip(r.why or "nothing on this sheet to compare")
    findings = [f for r in runs for f in r.findings]
    if shrink is not None:
        _unshrink(findings, [p for r in runs for p in r.passed], shrink)
    if photo_h is not None:
        _to_photo(findings, [p for r in runs for p in r.passed], photo_h)
    if scan_zoom:
        _px_to_pt(findings, [p for r in runs for p in r.passed], file, scan_zoom, rep_page)
    findings.sort(key=lambda f: (f["severity"] != "error", -f["sure"]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"
    wall = round(time.perf_counter() - t0, 3)
    return {"file": file, "page": rep_page, "version": VERSION, "findings": findings,
            "passed": [p for r in runs for p in r.passed], "checks": [r.row() for r in runs], "notes": notes,
            "cost": {"wall_seconds": wall, "llm_tokens_in": 0, "llm_tokens_out": 0, "api_calls": 0, "gpu_seconds": 0},
            "cost_tier": "cheap"}


def summary_line(rep: dict) -> str:
    """One line for a toast / the answer card: '2 issues found · 14 checks agree'."""
    nf, np_ = len(rep.get("findings") or []), len(rep.get("passed") or [])
    if nf:
        return f"{nf} issue{'s' if nf != 1 else ''} found · {np_} cross-check{'s' if np_ != 1 else ''} agree"
    ran = [c for c in rep.get("checks") or [] if c["status"] == "ran"]
    if not ran:
        return "Nothing to cross-check on this sheet (no tables, dimension chains, scale bars or room tags read)"
    # say how many checks ran (UX M10, as the panel does): 'No inconsistencies' over 1 of 9 checks overclaims
    return (f"{len(ran)} check{'s' if len(ran) != 1 else ''} ran, no inconsistencies · {np_} cross-check"
            f"{'s' if np_ != 1 else ''} agree")


if __name__ == "__main__":  # python -m stoa.features.selfcheck <abs path> [page] [--file virtual/path]
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("page", nargs="?", type=int, default=0)
    ap.add_argument("--file", default=None)
    ap.add_argument("--checks", default=None, help="comma list")
    a = ap.parse_args()
    rep = check(str(Path(a.path).resolve()), a.page, a.file, a.checks.split(",") if a.checks else None)
    print(json.dumps(rep, ensure_ascii=False))


# ----------------------------------------------------------------------------------------------------------------------
# HTTP: GET /feat/selfcheck/run?path=...&page=0  (mounted by stoa.features.include_feature_routers when STOA_FEATURES
# names 'selfcheck'). `path` = dataset-relative ('data/...'), an upload's virtual path ('upload/<id>/<name>', resolved
# in STOA_UPLOADS like web/mock/stoaApi.ts) or an absolute path under one of those roots / STOA_SELFCHECK_ROOTS.

def _roots() -> list[Path]:
    import os
    import tempfile

    out = []
    try:
        from stoa.data import data_root

        out.append(data_root().resolve())
    except Exception:  # noqa: BLE001
        pass
    out.append(Path(os.environ.get("STOA_UPLOADS") or Path(tempfile.gettempdir()) / "stoa-uploads").resolve())
    out += [Path(p).resolve() for p in os.environ.get("STOA_SELFCHECK_ROOTS", "").split(":") if p]
    return out


# ------------------------------------------------------------------------------------- a print vs its original: revision
# UX C-10 (01:17) + the brief ('find the errors before they reach site'): a print / scan on site that is an OLDER issue
# of the drawing than the original uploaded with it is a classic way an error reaches site. T3's project links the print
# to its original (same drawing number / look); here both title blocks are read: the CURRENT revision (the value in the
# box under / beside a 'REVISION' / 'REV' label that is not a history-table header) and the revision HISTORY rows (a
# revision code with a date on its line). Only a difference that the original's own history confirms (it lists the
# print's code as an earlier issue, or the print lists the original's) is called out; any other difference is a 'check'
# line (an OCR misread of 'C01' looks the same as a different issue).
_REV_LABEL = re.compile(r"^(?:REVISION|REV\.?|REV\.?\s*NO\.?|REVISION\s*NO\.?)$", re.I)
_REV_HEADER_NEXT = re.compile(r"^(?:DATE|DESCRIPTION|DESC\.?|BY|CHK\.?|CHECKED|APP\.?|APPROVED|AMENDMENTS?|DETAILS?|"
                              r"NOTES?|STATUS|ISSUE|PURPOSE|DRAWN|DRN)$", re.I)
_REV_VAL = re.compile(r"^(?:[A-Z]{1,2}\d{1,3}|\d{1,3}|[A-Z])$")
_REV_DATE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}[./-][A-Z]{3}[./-]\d{2,4})$", re.I)


def _rev_norm(t: str) -> str:
    """'c0l' / 'CO1' (OCR) -> 'C01'; '02' stays; anything else upper-cased and stripped."""
    t = str(t).strip().strip(".:;,()[]").upper()
    m = re.fullmatch(r"([A-Z])([0-9OIL]{1,3})", t) or re.fullmatch(r"([A-Z]{2})([0-9OIL]{1,3})", t)  # 'CO1': C + O1
    if m and any(ch.isdigit() for ch in m.group(2)):
        t = m.group(1) + m.group(2).translate(str.maketrans("OIL", "011"))
    return t


def _rev_tokens(items) -> list[tuple[str, float, float, float, float]]:
    """(text, x0, y0, x1, y1) per WORD; an OCR line ('REVISION DATE') is split with boxes in proportion to its chars."""
    out = []
    for text, x0, y0, x1, y1 in items:
        parts = str(text).split()
        if len(parts) <= 1:
            out.append((str(text).strip(), x0, y0, x1, y1))
            continue
        n, pos = max(1, len(str(text))), 0
        for w in parts:
            i = str(text).find(w, pos)
            pos = i + len(w)
            out.append((w, x0 + (x1 - x0) * i / n, y0, x0 + (x1 - x0) * pos / n, y1))
    return [t for t in out if t[0]]


def _revision_from_words(items) -> dict | None:
    """The current revision + the history rows from one sheet's words ((text, x0, y0, x1, y1), any one space)."""
    ws = _rev_tokens(items)
    labels = [w for w in ws if _REV_LABEL.match(w[0])]
    if not labels:
        return None
    cur, heads = [], []
    for lab in labels:
        t, x0, y0, x1, y1 = lab
        h = max(1e-6, y1 - y0)
        cy = (y0 + y1) / 2
        same_line = [w for w in ws if w is not lab and abs((w[2] + w[4]) / 2 - cy) < 0.6 * h and 0 <= w[1] - x1 <= 6 * h]
        if any(_REV_HEADER_NEXT.match(w[0]) for w in same_line):
            heads.append(lab)
            continue
        cands = []
        for w in ws:
            if w is lab or _REV_LABEL.match(w[0]) or _REV_DATE.match(w[0]):
                continue
            v = _rev_norm(w[0])
            if not _REV_VAL.match(v):
                continue
            wcy = (w[2] + w[4]) / 2
            below = w[2] >= cy and w[2] - y1 <= 2.5 * h and min(w[3], x1 + h) - max(w[1], x0 - h) > 0
            right = abs(wcy - cy) < 0.6 * h and 0 <= w[1] - x1 <= 3 * h
            if below or right:
                cands.append((abs(wcy - cy) + abs(w[1] - x0), v, w, lab))
        if cands:
            cur.append(min(cands, key=lambda c: c[0]))
    hist = []
    for w in ws:
        v = _rev_norm(w[0])
        if not _REV_VAL.match(v) or _REV_LABEL.match(w[0]) or len(v) < 2 and not heads:
            continue
        h = max(1e-6, w[4] - w[2])
        cy = (w[2] + w[4]) / 2
        dated = [d for d in ws if _REV_DATE.match(d[0]) and abs((d[2] + d[4]) / 2 - cy) < 0.6 * h and 0 <= d[1] - w[3] <= 12 * h]
        if not dated:
            continue
        in_col = any(min(w[3], hd[3] + h) - max(w[1], hd[1] - h) > 0 and abs(cy - (hd[2] + hd[4]) / 2) <= 25 * h for hd in heads)
        if heads and not in_col:
            continue
        hist.append({"rev": v, "date": dated[0][0], "box": [round(w[1], 2), round(w[2], 2), round(w[3], 2), round(w[4], 2)]})
    vals = {c[1] for c in cur}
    if len(vals) != 1:  # none, or two boxes that disagree: no current revision
        return {"rev": None, "box": None, "label_box": None, "history": hist} if hist else None
    best = min(cur, key=lambda c: c[0])
    _, v, w, lab = best
    return {"rev": v, "box": [round(x, 2) for x in w[1:5]], "label_box": [round(x, 2) for x in lab[1:5]], "history": hist}


def sheet_revision(path: str, page: int = 0) -> dict | None:
    """The sheet's current revision code + revision history, with boxes in the space the viewer shows: a drawn PDF's
    words (pdf_pt; unrotated pages only), a scanned PDF page's / a picture's EXISTING text layer (cache-only, never a
    new OCR read; a big picture's 6000 px copy mapped back). None when nothing is readable."""
    path = str(path)
    try:
        if path.lower().endswith(".pdf"):
            scan = _scan_of(path, page)
            if not scan:
                import pymupdf

                with pymupdf.open(path) as d:
                    pg = d[int(page)]
                    if pg.rotation:
                        return None
                    r = _revision_from_words([(w[4], w[0], w[1], w[2], w[3]) for w in pg.get_text("words")])
                if r:
                    r.update(space="pdf_pt", read="pdf text")
                return r
            png, zoom = scan
            r = sheet_revision(png, 0)
            if r:
                for k in ("box", "label_box"):
                    if r.get(k):
                        r[k] = [round(v / zoom, 2) for v in r[k]]
                for hh in r["history"]:
                    hh["box"] = [round(v / zoom, 2) for v in hh["box"]]
                r["space"] = "pdf_pt"
            return r
        q, factor = path, 1.0
        lay = _image_layer(q)
        if lay is None and (sh := _shrunk_of(q)) and _image_layer(sh[0]) is not None:
            (q, factor), lay = sh, _image_layer(sh[0])
        best = None
        for lay in [x for x in _image_layers(q)][:3]:  # the OCR layer may miss a box the VLM layer read, or the reverse
            r = _revision_from_words([(i.text, *i.box) for i in lay.items])
            if r and (best is None or (r.get("rev") and not best.get("rev")) or len(r["history"]) > len(best["history"]) and bool(r.get("rev")) >= bool(best.get("rev"))):
                if best and best.get("rev") and r.get("rev") and best["rev"] != r["rev"]:
                    best["layers_disagree"] = True
                    continue
                best = {**r, "layers_disagree": bool(best and best.get("layers_disagree"))}
        if best:
            if factor != 1.0:
                for k in ("box", "label_box"):
                    if best.get(k):
                        best[k] = [round(v / factor, 1) for v in best[k]]
                for hh in best["history"]:
                    hh["box"] = [round(v / factor, 1) for v in hh["box"]]
            best.update(space="image_px", read="OCR")
        return best
    except Exception:  # noqa: BLE001
        return None


def compare_revision(copy_path: str, copy_page: int, orig_path: str, orig_page: int,
                     copy_file: str | None = None, orig_file: str | None = None) -> dict:
    """A print / copy vs its original: {status: same | older | newer | differs | unread, line, severity, evidence}.
    older = the original's history lists the print's revision as an earlier issue (the print is out of date); newer =
    the print's history lists the original's (the uploaded original is the older one); differs = the codes differ and
    no history says which came first (a misread is possible: 'check')."""
    a, b = sheet_revision(copy_path, copy_page), sheet_revision(orig_path, orig_page)
    ra, rb = (a or {}).get("rev"), (b or {}).get("rev")
    cf, of = copy_file or copy_path, orig_file or orig_path

    def ev(r, f, p, note):
        return {"file": f, "page": p, "bbox": r["box"], "bbox_space": r.get("space", "pdf_pt"), "note": note}

    out = {"status": "unread", "copy_rev": ra, "orig_rev": rb, "severity": None, "line": "", "evidence": []}
    if not ra or not rb:
        if ra or rb:
            out["line"] = (f"Revision: {'this print' if ra else 'the original'} reads {ra or rb}; "
                           f"{'the original' if ra else 'this print'}'s revision box could not be read")
        return out
    out["evidence"] = [ev(a, cf, copy_page, f"revision {ra} on this print ({a.get('read')})"),
                       ev(b, of, orig_page, f"revision {rb} on the original ({b.get('read')})")]
    if ra == rb:
        out.update(status="same", line=f"Same revision as the original: {ra} on both ✓")
        return out
    older = any(h["rev"] == ra for h in b.get("history") or [])
    newer = any(h["rev"] == rb for h in a.get("history") or [])
    if older and not newer:
        d = next(h["date"] for h in b["history"] if h["rev"] == ra)
        out.update(status="older", severity="error",
                   line=(f"Out of date: this print is revision {ra}, the original is {rb} (its revision table lists {ra} "
                         f"of {d} as an earlier issue). Build from {rb}; check what changed before using this print."))
    elif newer and not older:
        d = next(h["date"] for h in a["history"] if h["rev"] == rb)
        out.update(status="newer", severity="error",
                   line=(f"This print is NEWER than the uploaded original: print revision {ra}, original {rb} (the "
                         f"print's revision table lists {rb} of {d} as an earlier issue). Check which one is current."))
    else:
        out.update(status="differs", severity="check",
                   line=(f"Revision codes read differently: this print {ra} ({a.get('read')}), the original {rb}: check "
                         "both title blocks (neither revision table says which came first; a misread looks the same)."))
    return out


_UPLOAD_PATH = re.compile(r"^upload/([0-9a-f]{12})/(?:([a-z0-9_]+)/)?([^/\\]+)$")


def resolve(path: str) -> Path | None:
    """The file on disk for a request path, or None when it is outside every allowed root / missing."""
    if not path or ".." in Path(path).parts:
        return None
    roots = _roots()
    if path.startswith("upload/"):
        up = roots[1 if len(roots) > 1 else 0]
        # a picture whose drawing style the upload guessed is 'upload/<id>/<style>/<name>' in the browser but stored at
        # <uploads>/<id>/data/<style>/<name> (web/mock/stoaApi.ts uploadAbs; F8 22:42)
        m = _UPLOAD_PATH.match(path)
        if m and m.group(2):
            cand = up / m.group(1) / "data" / m.group(2) / m.group(3)
        else:
            cand = up / path[len("upload/"):]
    elif path.startswith("/"):
        cand = Path(path)
    else:
        cand = roots[0] / path if roots else Path(path)
    try:
        cand = cand.resolve()
    except OSError:
        return None
    if not cand.is_file() or not any(r == cand or r in cand.parents for r in roots):
        return None
    return cand


@functools.lru_cache(maxsize=64)
def _cached(abs_path: str, mtime: float, page: int, file: str, layer_sig: int = 0, set_sig: str = "") -> dict:
    return check(abs_path, page, file)


@functools.lru_cache(maxsize=64)
def _cached_copy(cp: str, cm: float, cpage: int, cfile: str, op: str, om: float, opage: int, ofile: str,
                 csig=0, osig=0) -> dict:
    return compare_revision(cp, cpage, op, opage, cfile, ofile)


def _layer_sig(p: Path, page: int = 0):
    """A picture's report depends on its text layer, which the upload warm-up reads AFTER the first (background) check,
    and on its traced copy, which the first measured question makes: key the cache on both, or a scan keeps 'no text
    layer yet' for the whole session. Drawn PDFs: 0; a scanned PDF: its rendered picture's (check() reads that)."""
    q = str(p)
    if p.suffix.lower() == ".pdf":
        scan = _scan_of(q, page)
        if not scan:
            return 0
        q = scan[0]
    lay = _image_layer(q)
    if lay is None and (sh := _shrunk_of(q)) and _image_layer(sh[0]) is not None:  # check() reads the 6000 px copy
        q, lay = sh[0], _image_layer(sh[0])
    if lay is None and (ph := _photo_of(q)):  # check() reads the straightened copy then (its layer, its traced copy)
        q, lay = ph[0], _image_layer(ph[0])
    try:
        n = -1 if lay is None else len(lay)
    except TypeError:
        n = 1
    vec = _traced_copy(q)
    try:
        return (n, round(Path(vec).stat().st_mtime, 3) if vec else 0.0)
    except OSError:
        return (n, 0.0)


try:
    from fastapi import APIRouter, HTTPException, Query

    router = APIRouter(prefix="/feat/selfcheck", tags=["selfcheck"])

    @router.get("/run")
    def run_endpoint(path: str = Query(...), page: int = 0) -> dict:
        p = resolve(path)
        if p is None:
            raise HTTPException(404, "no such file (dataset-relative, upload/<id>/<name>, or an allowed root)")
        rep = dict(_cached(str(p), p.stat().st_mtime, int(page), path, _layer_sig(p, int(page)), _set_sig(p, path)))
        rep["summary"] = summary_line(rep)
        return rep

    @router.get("/copy")
    def copy_endpoint(path: str = Query(...), page: int = 0, orig: str = Query(...), opage: int = 0) -> dict:
        """A print / copy (T3's project knows it) vs its original: the revision codes of both title blocks."""
        p, o = resolve(path), resolve(orig)
        if p is None or o is None:
            raise HTTPException(404, "no such file (dataset-relative, upload/<id>/<name>, or an allowed root)")
        return _cached_copy(str(p), p.stat().st_mtime, int(page), path, str(o), o.stat().st_mtime, int(opage), orig,
                            _layer_sig(p, int(page)), _layer_sig(o, int(opage)))

    @router.get("/checks")
    def checks_endpoint() -> dict:
        return {"version": VERSION, "checks": [{"check": k, "label": v} for k, v in LABELS.items()]}
except Exception:  # noqa: BLE001  (no fastapi: the module still works as a library / CLI)
    router = None
