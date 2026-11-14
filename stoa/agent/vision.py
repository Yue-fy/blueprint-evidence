"""Gemini 3.1 Pro VISION reading of the drawing when our readers found nothing (captain 14:39, G1 cycle 3).

X1's wild exam: on real uploads the PDF readers often abstain (outlined text, no printed scale, line-only sheets) and
the cascade answered the PRIOR, while a photo of the same page was read right by the image path 50-100 % of the time.
This step shows Gemini the rendered page itself and asks for the value AND where on the drawing it read it.

When (captain 15:01 DECISION, hybrid routing for uploads; only with gemini=1 = the API/demo default, never in bulk
evals): AREAS -> our readers first, Gemini only when the answer is the prior / empty / weak and unlocated; COUNTS ->
Gemini whenever no STRUCTURED reader counted with confidence >= 0.7 (wild: Gemini direct 80 % vs ours 38 %); LENGTHS ->
Gemini when no structured reader is >= 0.6. Cached by request content (image + question), counted against the daily
caps like every Gemini call. Large pages: the whole sheet plus its 4 quadrants at higher resolution IN ONE CALL.

Confidence comes from AGREEMENT, not from Gemini's self-report: ours and Gemini's agree -> 'two independent readings
agree' (>= AGREE_CONF); a count with no estimate of ours or a different one -> a second Gemini sample at temperature
0.8 (counts only: budget) -> both samples agree -> SAMPLES_CONF, else DISAGREE_CONF; a single reading -> VISION_CAP.

What Gemini returns (JSON): answer, unit, confidence, how (the printed numbers / labels / scale it used and its
arithmetic, one or two sentences: shown in the trace), evidence boxes ([ymin, xmin, ymax, xmax] on a 0-1000 grid,
Gemini's native box format) and the assumptions it made. We map the boxes to PDF points / image pixels, cap the
confidence at VISION_CAP (a reading no tool of ours confirms), and say in the trace and assumptions that Gemini read it.
"""

from __future__ import annotations

import json
import os
import re
import time

from stoa.contract import Answer, Cost, Evidence

VISION_MODEL = os.environ.get("STOA_GEMINI_VISION", "gemini-3.1-pro-preview")
VISION_CAP = float(os.environ.get("STOA_VISION_CAP", 0.45))
AGREE_CONF, SAMPLES_CONF, DISAGREE_CONF = 0.8, 0.65, 0.3
WEAK = 0.3
STRUCTURED = ("ifc", "dxf", "svg_ifc", "schedule", "plan_rooms", "label_count", "area_tags", "pdf_vector", "line_rooms",
              "lengths")
SECOND = os.environ.get("STOA_VISION_SECOND", "1") != "0"
# areas (captain 15:43): our outline x our scale gives the value; Gemini says WHERE the room is and its own estimate is
# only a cross-check. Our area answers below this confidence get the cross-check (uploads only, gemini=1).
AREA_CHECK_BELOW = float(os.environ.get("STOA_AREA_CHECK_BELOW", 0.7))
ESTIMATE_CAP = 0.2  # Gemini's area when nothing of ours could measure the room: an unmeasured estimate
MAX_PIXELS = 6_000_000  # ~2900 x 2050: dimension strings on an A3 sheet stay legible
MAX_SIDE = 3600

SYS = """You read one construction drawing (a floor plan, section, elevation, schedule or scan) and answer one question
about it from what is DRAWN or PRINTED on it. Rules:
- Use printed numbers first: room areas, dimension strings, level marks, labels, the title block's scale.
  European dimension strings without a decimal point are usually centimetres ('1104' = 11.04 m); an opening label
  'a / b' is width / height.
- Counts: count the drawn symbols or labels of the asked kind on this sheet only.
- Lengths / areas without a printed value: measure with the printed scale bar or scale + sheet size, and say so.
- If the drawing cannot answer it, say what is missing; still give your best estimate when one is reasonable.
- Answer in metres / square metres unless the question asks for other units.
- When the question is about one room or object, also give point_2d: a point inside it, near its centre.
Return JSON only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "the value (a number without unit, or a short text)"},
        "unit": {"type": "string", "description": "m, m2, m3, count, cm, or empty for text"},
        "confidence": {"type": "number"},
        "how": {"type": "string", "description": "1-2 sentences: what you read (numbers, labels, scale) and computed"},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"},
            "box_2d": {"type": "array", "items": {"type": "number"}}}, "required": ["label", "box_2d"]}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "missing": {"type": "array", "items": {"type": "string"}},
        "point_2d": {"type": "array", "items": {"type": "number"},
                     "description": "[y, x] on a 0-1000 grid: the centre of the ONE room / object asked about"},
    },
    "required": ["answer", "unit", "confidence", "how", "evidence"],
}

_UNIT = {"m2": "m2", "m²": "m2", "sqm": "m2", "m": "m", "cm": "m", "mm": "m", "m3": "m3", "m³": "m3", "count": "count",
         "ft": "ft", "ft2": "ft2", "sf": "ft2"}


def task_of(question: str, ans: Answer | None = None) -> str:
    from stoa.router.intent import infer_task

    t = infer_task(question)[0]
    u = str(getattr(ans, "unit", "") or "").lower()
    if t in ("text", "yesno") and u in ("count", "m", "m2", "m3"):
        t = {"count": "count", "m": "length", "m2": "area", "m3": "volume"}[u]
    return t


def wanted(ans: Answer, question: str = "", path: str = "") -> str | None:
    """Why this answer gets a vision reading (hybrid routing), or None. The count / length rules are for UPLOADS
    (files outside the dataset, captain 15:01): dataset drawings keep their tuned readers (the demo's tour answers)."""
    from stoa.pipeline import _located

    s = str(ans.solver or "")
    if ans.value is None or s.endswith("prior") or (ans.confidence < WEAK and not _located(ans)):
        return "our readers found nothing usable"
    t = task_of(question, ans) if question else ""
    if t in ("area", "length", "count", "volume") and _words_only(ans.value):
        # X1 wild-dev, Gemini on (run -151435): the image reader's 'Not determinable' / 'Not specified' / 'BRH 87.50'
        # @0.30 to 'how big is the room' was shown as the answer and never escalated (it is a value, at WEAK)
        return f"no number for a {t} question ({str(ans.value)[:30]!r})"
    if path:
        from pathlib import Path

        from stoa.data import dataset_rel

        rel = dataset_rel(path)
        if not Path(rel).is_absolute() and str(rel).startswith("data/"):
            return None  # a dataset file (not a style-guessed upload: those stay hybrid)
    base = s.split("/")[-1]
    if t == "area" and ans.confidence < AREA_CHECK_BELOW and isinstance(ans.value, (int, float)):
        # captain 15:43: Gemini only as a CROSS-CHECK of our outline / scale on areas (it never answers an area alone)
        return f"area cross-check of our {base} reading @{ans.confidence:.2f}"
    if t == "count" and not (base in STRUCTURED and ans.confidence >= 0.7):
        return f"no confident structured count ({base} @{ans.confidence:.2f})"
    if t == "length" and not (base in STRUCTURED and ans.confidence >= 0.6):
        return f"no confident length reader ({base} @{ans.confidence:.2f})"
    return None


def _page_image(path: str, tiles: bool = False):
    """(PIL overview, kind, scale to native units, [quadrant tiles]): kind 'pdf_pt' (scale = px per pt) or
    'image_px' (scale = shrink). Tiles only when asked and the full-resolution page is > 1.5 x MAX_PIXELS."""
    from PIL import Image

    from stoa import llm
    from stoa.data import abspath

    a = abspath(path)
    if a.suffix.lower() == ".pdf":
        full, ppt = llm.render_pdf_page(a, 0, dpi=200, max_pixels=40_000_000 if tiles else MAX_PIXELS)
    else:
        full, ppt = Image.open(a).convert("RGB"), 1.0
    s = min(1.0, (MAX_PIXELS / max(1, full.width * full.height)) ** 0.5, MAX_SIDE / max(full.width, full.height))
    img = full.resize((max(1, int(full.width * s)), max(1, int(full.height * s)))) if s < 1 else full
    quads = []
    # tiles cost latency (phone-photo counts 60-260 s with tiles + second sample, wild-dev run -151435) but they are
    # what makes those counts right (x1-062 4 with tiles, 5 without): kept for every large page
    if tiles and full.width * full.height > 1.5 * MAX_PIXELS:
        W, H, o = full.width, full.height, 0.08
        for (x0, y0) in ((0, 0), (0.5, 0), (0, 0.5), (0.5, 0.5)):
            box = (int(max(0, x0 - o) * W), int(max(0, y0 - o) * H), int(min(1, x0 + 0.5 + o) * W),
                   int(min(1, y0 + 0.5 + o) * H))
            t = full.crop(box)
            ts = min(1.0, (MAX_PIXELS / 2 / max(1, t.width * t.height)) ** 0.5)
            quads.append(t.resize((max(1, int(t.width * ts)), max(1, int(t.height * ts)))) if ts < 1 else t)
    k = ppt * s if a.suffix.lower() == ".pdf" else s
    return img, ("pdf_pt" if a.suffix.lower() == ".pdf" else "image_px"), k, quads


def _words_only(v) -> bool:
    """A text answer to a numeric question: no number at all ('Not determinable'), a label that merely contains one
    ('BRH 87.50' = a sill height) or a dimension pair ('3,50 x 3,00'). Multi-part answers ('3 ; 43.82') stay."""
    if not isinstance(v, str) or ";" in v:
        return False
    t = v.strip()
    return not re.fullmatch(r"[~≈]?\s*-?\d+(?:[.,]\d+)?\s*(?:m²|m2|m³|m3|m|ft²|ft2|sq ?ft|ft)?", t)


def _num(s) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(s or "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _pair_only(path: str, raw: str, how: str) -> bool:
    """Is the number `raw` printed only as one half of an 'a/b' pair ('16.2/27.8') - in Gemini's own words or in the
    PDF text layer?"""
    rx_pair = re.compile(r"(?<![\d.])" + re.escape(raw) + r"\s*/\s*\d|\d\s*/\s*" + re.escape(raw) + r"(?![\d.])")
    if rx_pair.search(how):
        return True
    if not str(path).lower().endswith(".pdf"):
        return False
    try:
        import pymupdf

        from stoa.data import abspath

        with pymupdf.open(str(abspath(path))) as doc:
            text = doc[0].get_text() if len(doc) else ""
    except Exception:  # noqa: BLE001
        return False
    alone = re.compile(r"(?<![\d./])" + re.escape(raw) + r"(?![\d/])(?!\s*/\s*\d)")
    return bool(rx_pair.search(text)) and not any(
        not re.search(r"\d\s*/\s*$", text[max(0, m.start() - 4):m.start()]) for m in alone.finditer(text))


def read(path: str, question: str, base: Answer | None = None, temperature: float = 0.0) -> Answer | None:
    """One Gemini vision call on the drawing; an Answer (capped) or None (no image / call failed / no answer)."""
    from stoa import llm
    from stoa.agent.gemini import LABEL, PRICE

    t0 = time.perf_counter()
    is_count = task_of(question, base) == "count"
    try:
        img, space, k, quads = _page_image(path, tiles=is_count)
    except Exception:  # noqa: BLE001 — no image of this file (IFC, DXF): nothing to look at
        return None
    label = LABEL.get(VISION_MODEL, VISION_MODEL)
    msg = f"Question: {question}\nThe image is {img.width} x {img.height} px."
    if is_count:
        # captain 15:01: a point/box per counted object, so a human can check the count at a glance
        msg += ("\nThis is a count: give ONE evidence box per counted object (the answer must equal the number of "
                "boxes).")
        if quads:
            msg += (f"\nImage 1 is the whole sheet; images 2-5 are its top-left, top-right, bottom-left and "
                    f"bottom-right quarters at higher resolution (overlapping slightly). Count on the whole sheet "
                    f"once (no double counts in the overlaps) and give every box in image 1's coordinates.")
    try:
        r = llm.chat([{"role": "user", "content": msg}], images=[img, *quads], system=SYS, backend="gemini",
                     model=VISION_MODEL, max_tokens=6000, temperature=temperature, json_schema=SCHEMA,
                     purpose="G1 vision reading (hybrid upload routing)", cache=True, max_pixels=MAX_PIXELS,
                     max_side=MAX_SIDE)
    except Exception as e:  # noqa: BLE001
        if base is not None:
            base.trace = [*base.trace, f"{label} vision reading failed ({type(e).__name__})"]
        return None
    try:
        d = json.loads(r.text)
    except Exception:  # noqa: BLE001
        d = llm.extract_json(r.text) or {}
    pin, pout = PRICE.get(VISION_MODEL, (2.0, 12.0))
    tin, tout = r.prompt_tokens or 0, r.completion_tokens or 0
    cost = Cost(llm_calls=1, api_calls=1, llm_tokens_in=tin, llm_tokens_out=tout, api_tokens_in=tin,
                api_tokens_out=tout, wall_seconds=round(time.perf_counter() - t0, 3))
    cost.gemini_usd = round((tin * pin + tout * pout) / 1e6, 6)
    if not isinstance(d, dict) or not str(d.get("answer", "")).strip():
        return None
    pt = d.get("point_2d") or []
    if len(pt) == 2:  # where the asked room is (measure_at_point traces it), kept even if the value is dropped below
        _POINTS[(str(path), question)] = (float(pt[1]) * img.width / 1000 / k, float(pt[0]) * img.height / 1000 / k,
                                          space)
    raw, unit_raw = str(d["answer"]).strip(), str(d.get("unit") or "").strip().lower()
    unit = _UNIT.get(unit_raw, unit_raw or None)
    v = _num(raw)
    value: object = raw
    if v is not None and len(re.sub(r"[\d.,\s\-]", "", raw)) <= 3:
        value = v / 100 if unit_raw == "cm" else (v / 1000 if unit_raw == "mm" else v)
        if unit == "count":
            value = int(round(v))
    elif unit in ("m", "m2", "m3", "count"):
        unit = None
    if unit == "m2" and isinstance(value, float) and _pair_only(path, raw, str(d.get("how") or "")):
        # X1 wild pagen: 'the room at the top right' -> 16.2 m² read off the stair label '16.2/27.8' (riser/tread)
        if base is not None:
            base.trace = [*base.trace, f"{label} vision reading dropped: '{raw}' is printed only inside an 'a/b' pair "
                                       "(riser/tread or width/height), not as a room area"]
        return None
    ev = []
    W, H = img.width, img.height
    for e in (d.get("evidence") or [])[:12]:
        b = e.get("box_2d") or []
        if len(b) != 4:
            continue
        y0, x0, y1, x1 = (float(t) for t in b)
        px = [min(x0, x1) * W / 1000, min(y0, y1) * H / 1000, max(x0, x1) * W / 1000, max(y0, y1) * H / 1000]
        bb = [round(c / k, 2) for c in px]
        ev.append(Evidence(file=path, page=0, bbox=bb, bbox_space=space, exact=False,
                           note=f"{label} read: {str(e.get('label', ''))[:80]}"))
    if is_count and isinstance(value, int) and ev and len(ev) != value:
        assumptions_n = [f"{label} said {value} but drew {len(ev)} boxes"]
    else:
        assumptions_n = []
    conf = VISION_CAP if not assumptions_n else round(VISION_CAP * 0.7, 3)  # never Gemini's self-report
    how = str(d.get("how") or "").strip()
    trace = [f"{label} vision reading: {raw} {unit_raw} — {how[:300]}"]
    assumptions = [f"read by {label} from the drawing image"] + assumptions_n + \
        [str(a)[:160] for a in (d.get("assumptions") or [])[:3]]
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver="pipeline/gemini_vision", trace=trace,
                  assumptions=assumptions, missing=[str(m)[:160] for m in (d.get("missing") or [])[:3]], cost=cost)


_POINTS: dict = {}  # (path, question) -> (x, y, space) of the room Gemini pointed at (in-process only)


def measure_at_point(path: str, question: str, g: Answer) -> Answer | None:
    """Gemini says WHERE, our geometry says HOW BIG (captain 15:01: areas -> our readers): the room Gemini pointed at,
    traced from the sheet's wall lines at the printed 1:N (E1 linework.measure_room). X1 wild: line-only CAD sheets
    with unlabelled rooms ('the room at the top right') had no reader at all."""
    pt = _POINTS.get((str(path), question))
    if not pt or pt[2] != "pdf_pt":
        return None
    try:
        from stoa.data import abspath
        from stoa.evidence import linework as L
    except ImportError:
        return None
    p = str(abspath(path))
    try:
        scales = L.sheet_scales(p, 0)
        r = L.measure_room(p, 0, (pt[0], pt[1]), float(scales[0])) if scales else None
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    n = float(scales[0])
    a = round(float(r["area_m2"]), 2)
    poly = r["polygon"]
    xs, ys = [x for x, _ in poly], [y for _, y in poly]
    gv = g.value if isinstance(g.value, (int, float)) else None
    agree = gv is not None and abs(gv - a) <= 0.1 * a
    conf = 0.6 if agree else 0.4
    ev = [Evidence(file=path, page=0, bbox=[min(xs), min(ys), max(xs), max(ys)], bbox_space="pdf_pt",
                   polygon=[[round(x, 2), round(y, 2)] for x, y in poly], exact=False,
                   note=f"room outline traced from the wall lines at 1:{n:g}: {a} m² (located by Gemini)")]
    label = g.trace[0].split(" vision")[0] if g.trace else "Gemini"
    return Answer(value=a, unit="m2", confidence=conf, evidence=ev + g.evidence[:2], solver="pipeline/gemini_locate",
                  trace=[f"{label} located the room; its outline traced from the wall lines at 1:{n:g} (printed): {a} m² "
                         f"({r.get('support')}/{r.get('n_readings')} readings agree); {label}'s own estimate "
                         f"{gv if gv is not None else '-'} {'agrees ✓' if agree else 'differs'}"],
                  assumptions=[f"scale: 1:{n:g} printed on the sheet", f"room located by {label}, measured by tracing "
                               "the drawn wall lines (approximate: to the wall faces; door openings closed)"],
                  missing=([] if agree else [f"{label} estimated {gv} m²"]) + ["a printed room area for an exact value"],
                  cost=g.cost)


def _image_bar(img_path: str):
    """The scale of an image: the visitor's (W5 'set scale') > a drawn scale bar > the printed 1:N with the image's
    DPI on a standard sheet size (O1's scalebar). None when the image has no scale we can trust."""
    from stoa.solvers.raster import scalebar as SB
    from stoa.solvers.raster import textlayer as T

    ub = SB.user_bar()
    if ub is not None:
        return ub
    try:
        tl = T.text_layer(img_path)
    except Exception:  # noqa: BLE001
        return None
    try:
        bs = [b for b in SB.bars(img_path, layer=tl) if b.unit in SB.UNIT_M and b.px_per_m]
    except Exception:  # noqa: BLE001
        bs = []
    return bs[0] if bs else SB.printed_bar(img_path, tl)


def _pdf_scale(pdf_abs: str) -> tuple[float, str] | None:
    """(N of 1:N, where it came from) for a vector PDF: the printed scale (text layer, else recognition's OCR), else
    the door swings (W5 web/server/door_scale.py: 58/60 Swiss sheets exactly the printed scale)."""
    try:
        from stoa.evidence import linework as L

        s = L.sheet_scales(pdf_abs, 0)
        if s:
            return float(s[0]), "printed on the sheet"
    except Exception:  # noqa: BLE001
        pass
    try:
        from stoa.router.recognize import recognize

        rec = recognize(pdf_abs) or {}
        m = re.fullmatch(r"1\s*:\s*(\d+(?:\.\d+)?)", str(rec.get("scale") or ""))
        if m:
            return float(m.group(1)), "printed on the sheet (read from the rendered page)"
    except Exception:  # noqa: BLE001
        pass
    try:
        import importlib.util
        from pathlib import Path

        f = Path(__file__).resolve().parents[2] / "web" / "server" / "door_scale.py"
        spec = importlib.util.spec_from_file_location("_w5_door_scale", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        d = mod.estimate(pdf_abs, 0)
        if d.get("ok") and d.get("ratio") and d.get("doors", 0) >= 5:  # 3 'doors' on a scanned US sheet said 1:400
            return float(d["ratio"]), f"estimated from {d['doors']} door swings (a door leaf ≈ 0.85 m)"
    except Exception:  # noqa: BLE001
        pass
    return None


def measure_raster_at_point(path: str, question: str, g: Answer) -> Answer | None:
    """Gemini says WHERE, our geometry says HOW BIG, on a picture (or a vector PDF no line tracer could read, rendered
    at a known scale): the blank region around Gemini's point up to the drawn walls (O1 raster measure.room_area,
    furniture-robust), times our scale. Never Gemini's own area (captain 15:43)."""
    pt = _POINTS.get((str(path), question))
    if not pt:
        return None
    try:
        from stoa.data import abspath
        from stoa.solvers.raster import measure as M
        from stoa.solvers.raster import scalebar as SB
    except ImportError:
        return None
    p = str(abspath(path))
    x, y, space = pt
    if space == "pdf_pt":
        sc = _pdf_scale(p)
        if sc is None:
            return None
        n, src = sc
        try:
            from stoa.formats.normalize import render_png

            img, zoom = render_png(p, dpi=150)
        except Exception:  # noqa: BLE001
            return None
        ppm = zoom * 72 / 0.0254 / n
        xp, yp = x * zoom, y * zoom
        scale_words = f"1:{n:g} {src}"
    else:
        img, zoom = p, 1.0
        bar = _image_bar(p)
        if bar is None or not bar.px_per_m:
            return None
        ppm, xp, yp = float(bar.px_per_m), x, y
        scale_words = SB.scale_source(bar)
    try:
        r = M.room_area(img, [xp - 3, yp - 3, xp + 3, yp + 3], ppm, **M.FILL, others=None, solid=True)
    except Exception:  # noqa: BLE001
        return None
    if r is None:
        return None
    # thin-line drawings (renders of line-only CAD sheets: X1 pagen_png, wall_frac 0.00-0.04) have closed outlines
    # without solid walls: 10.24 / 13.82 / 13.97 m² vs gold 10.49 / 13.77 / 14.15 at Gemini's rooms. Raster textqa
    # drops wall_frac < 0.05 (HABS thin-line scans leaked to ~100 m²): here a smaller size cap and less confidence
    thin = r.get("wall_frac", 0) < 0.05
    if not 0.5 <= r["m2"] <= (60 if thin else 120):
        return None
    sqft = bool(re.search(r"\b(sq\.? ?ft|square feet|ft²|ft2)\b", question, re.I))
    a = round(r["m2"] / 0.3048 ** 2 if sqft else r["m2"], 2)
    unit = "ft2" if sqft else "m2"
    gv = g.value if isinstance(g.value, (int, float)) and (g.unit or "m2") == unit else None
    agree = gv is not None and abs(gv - a) <= 0.1 * a
    # O1: this measurement is within 2 % for 6/23 held-out rooms (raster measure.CONF 0.3); a second, independent
    # estimate within 10 % lifts it a little (it says 'right room, right scale', not 'within 2 %')
    conf = round(M.CONF + (0.1 if agree else 0.0) - (0.1 if "estimated" in scale_words else 0.0)
                 - (0.1 if thin else 0.0), 3)
    k = 1 / zoom
    poly = [[round(px * k, 2), round(py * k, 2)] for px, py in (r.get("polygon") or [])]
    bx = [round(c * k, 2) for c in r["box"]]
    label = g.trace[0].split(" vision")[0] if g.trace else "Gemini"
    ev = [Evidence(file=path, page=0, bbox=bx, bbox_space=space, polygon=poly or None, exact=False,
                   note=f"room outline traced on the drawing at {scale_words}: {a} {'ft²' if sqft else 'm²'} (located by "
                        f"{label})")]
    return Answer(value=a, unit=unit, confidence=conf, evidence=ev + g.evidence[:2], solver="pipeline/gemini_locate",
                  trace=[f"{label} located the room; its outline traced up to the drawn walls at {scale_words} "
                         f"(1 m = {ppm:.1f} px): {a} {unit}; {label}'s own estimate {gv if gv is not None else '-'} "
                         f"{'agrees ✓' if agree else 'differs' if gv is not None else ''}".rstrip(), *r["notes"][:2]],
                  assumptions=[f"scale: {scale_words}", f"room located by {label}, measured by our tracer (the blank "
                               "region up to the walls; door openings closed): approximate"]
                  + (["thin-line drawing: few solid walls along the outline, it may run through a door opening"]
                     if thin else []),
                  missing=([] if agree or gv is None else [f"{label} estimated {gv} {unit}"])
                  + ["a printed room area or dimensions for an exact value"], cost=g.cost)


def _inside(pt, ev: list) -> bool | None:
    """Gemini's point inside our answer's room outline (polygon, else box) in the same space; None if not comparable."""
    x, y, space = pt
    seen = False
    for e in ev or []:
        if getattr(e, "bbox_space", None) != space:
            continue
        poly = getattr(e, "polygon", None)
        if poly and len(poly) >= 3:
            seen = True
            inside = False
            for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
                if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / ((y1 - y0) or 1e-9):
                    inside = not inside
            if inside:
                return True
    if seen:
        return False
    return None


def _area_crosscheck(path: str, question: str, ans: Answer, g: Answer, head: str) -> Answer:
    """Our area stays the value (captain 15:43); Gemini checks WHICH room we measured (its point inside our outline)
    and roughly HOW BIG (its own estimate). Same room + estimate within 10 % -> 'two independent methods agree'
    (>= 0.6); Gemini points at ANOTHER room -> <= 0.3, with that room measured as the alternative."""
    label = g.trace[0].split(" vision")[0] if g.trace else "Gemini"
    gv = g.value if isinstance(g.value, (int, float)) and (g.unit or ans.unit) == ans.unit else None
    ours = float(ans.value)
    pt = _POINTS.get((str(path), question))
    inside = _inside(pt, ans.evidence) if pt else None
    near = gv is not None and abs(gv - ours) <= 0.1 * ours
    c0 = ans.confidence
    note = f"{head}{label} cross-check: its estimate {gv if gv is not None else '-'} {ans.unit or ''}"
    if inside is False:
        alt = measure_at_point(path, question, g) or measure_raster_at_point(path, question, g)
        ans.confidence = round(min(c0, 0.3), 3)
        ans.missing = [*ans.missing, f"{label} points at a different room than the one measured"
                       + (f" (measured there: {alt.value} {alt.unit})" if alt is not None else "")]
        note += f"; it points OUTSIDE the outline we measured → confidence {c0:.2f} → {ans.confidence:.2f}"
    elif near:
        ans.confidence = round(max(c0, 0.6), 3)
        ans.assumptions = [*ans.assumptions, f"two independent methods agree ({ans.solver}: {ours:g}; {label}: {gv:g}"
                           + ("; same room" if inside else "") + ")"]
        note += f" agrees within 10 %{' and it points inside our outline' if inside else ''} ✓ → confidence " \
                f"{c0:.2f} → {ans.confidence:.2f}"
    else:
        if gv is not None:
            ans.missing = [*ans.missing, f"{label} estimated {gv:g} {ans.unit or ''} (a visual estimate, not measured)"]
        note += (" points inside our outline (same room)" if inside else "") + "; our measurement is kept"
    ans.trace = [*ans.trace, note]
    ans.cost = ans.cost.add(g.cost) if ans.cost else g.cost
    return ans


def _unmeasured(ans: Answer, g: Answer, head: str) -> Answer:
    """Nothing of ours could measure the room (no outline, or no scale): Gemini's figure is shown only as an unmeasured
    estimate at <= ESTIMATE_CAP, and the user is asked for the scale (W5's 'set the scale' tool)."""
    label = g.trace[0].split(" vision")[0] if g.trace else "Gemini"
    g.confidence = round(min(g.confidence, ESTIMATE_CAP), 3)
    g.solver = "pipeline/gemini_estimate"
    g.assumptions = [f"NOT MEASURED: {label}'s visual estimate (our tracer found no closed room outline at a known "
                     "scale)", *g.assumptions]
    g.missing = ["the drawing's scale: set it with two clicks on a length you know (a door is about 0.9 m), then the "
                 "room's outline can be measured", *g.missing]
    g.trace = [*ans.trace[:3], head.rstrip(": ") + ":", *g.trace, f"not measured by our geometry → shown as an estimate "
               f"(confidence ≤ {ESTIMATE_CAP})"]
    return g


def vision_step(path: str, question: str, ans: Answer, why: str = "") -> Answer:
    """Hybrid routing (captain 15:01): Gemini's reading vs ours; confidence from agreement (see the module doc)."""
    from stoa.agent.escalate import _same
    from stoa.pipeline import _located

    ours0 = ans.value is not None and not str(ans.solver or "").endswith("prior")
    g2_future = None
    if SECOND and not ours0 and task_of(question, ans) == "count":
        # no estimate of ours: the second sample is certain to be needed -> both samples at once (latency)
        import concurrent.futures as cf

        pool = cf.ThreadPoolExecutor(max_workers=1)
        g2_future = pool.submit(read, path, question, None, 0.8)
        pool.shutdown(wait=False)
    g = read(path, question, base=ans)
    if g is None:
        if task_of(question, ans) == "area" and (str(ans.solver or "").endswith("prior") or ans.value is None
                                                  or _words_only(ans.value)):
            stub = Answer(value=None, trace=["Gemini"])
            m = measure_at_point(path, question, stub) or measure_raster_at_point(path, question, stub)
            if m is not None:
                m.trace = [*ans.trace[:3], *ans.trace[-1:], *m.trace]
                return m
        return ans
    ours = ans.value is not None and not str(ans.solver or "").endswith("prior")
    unit = ans.unit or g.unit
    g.cost = ans.cost.add(g.cost) if ans.cost else g.cost
    head = f"hybrid routing ({why or 'unsure'}): "
    task = task_of(question, ans)
    if task == "area":
        # captain 15:43: the VALUE of an area is our outline x our scale; Gemini locates the room and cross-checks
        ours_num = ours and isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool)
        if ours_num and (why.startswith("area cross-check") or _located(ans)):
            return _area_crosscheck(path, question, ans, g, head)
        m = measure_at_point(path, question, g) or measure_raster_at_point(path, question, g)
        if m is not None:
            m.trace = [*ans.trace[:3], head.rstrip(": ") + ":", g.trace[0], *m.trace]
            if ours:
                m.missing.append(f"our first reader answered {str(ans.value)[:40]} {ans.unit or ''} @{ans.confidence:.2f} "
                                 f"({ans.solver})")
            return m
        if ours_num:
            return _area_crosscheck(path, question, ans, g, head)
        return _unmeasured(ans, g, head)
    if ours and _same(ans.value, g.value, unit):
        c = round(max(ans.confidence, AGREE_CONF), 3)
        ans.trace = [*ans.trace, head + g.trace[0] + f" — two independent readings agree (ours {ans.value}, Gemini "
                     f"{g.value}) ✓ confidence {ans.confidence:.2f} → {c:.2f}"]
        ans.assumptions = [*ans.assumptions, f"two independent methods agree ({ans.solver}: {ans.value}; "
                                             f"{g.trace[0].split(' vision')[0]}: {g.value})"]
        ans.confidence = c
        from stoa.pipeline import _located

        if not _located(ans):  # Gemini's boxes (one per counted object) become the evidence a human checks
            ans.evidence = [*(ans.evidence or []), *g.evidence]
        ans.cost = g.cost
        return ans
    g2 = None
    if g2_future is not None:
        try:
            g2 = g2_future.result(timeout=240)
        except Exception:  # noqa: BLE001
            g2 = None
    elif SECOND and task == "count":
        g2 = read(path, question, base=ans, temperature=0.8)
    if g2 is not None:
        g.cost = g.cost.add(g2.cost)
    if g2 is not None and _same(g.value, g2.value, unit):
        g.confidence = SAMPLES_CONF
        g.trace = [*g.trace, f"second {VISION_MODEL} sample at temperature 0.8: {g2.value} — samples agree "
                   f"→ confidence {SAMPLES_CONF}"]
        g.assumptions.append("two Gemini samples agree")
    elif g2 is not None:
        g.confidence = DISAGREE_CONF
        g.trace = [*g.trace, f"second {VISION_MODEL} sample at temperature 0.8: {g2.value} — samples disagree "
                   f"→ confidence {DISAGREE_CONF}"]
        g.missing.append(f"a second Gemini reading gave {g2.value}")
    if ours:
        g.confidence = round(min(g.confidence, DISAGREE_CONF if g2 is None else g.confidence), 3)
        g.missing.append(f"our reader answered {ans.value} {ans.unit or ''} @{ans.confidence:.2f} ({ans.solver})")
    g.trace = [*ans.trace[:3], head.rstrip(": ") + ":", *g.trace]
    return g


__all__ = ["read", "vision_step", "wanted", "VISION_CAP"]
