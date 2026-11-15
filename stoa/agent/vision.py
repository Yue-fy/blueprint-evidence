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
MAX_BOXES = 200  # T1 (audit 19:51): was 12, so a 20-door answer showed 12 boxes and blamed Gemini for the other 8
WEAK = 0.3
STRUCTURED = ("ifc", "dxf", "svg_ifc", "schedule", "plan_rooms", "label_count", "stated_count", "area_tags", "pdf_vector", "line_rooms",
              "lengths",
              # G1 c7 (S0/RL d25 replay: A-101F doors openings 6 @0.70 ✓ -> Gemini 5 @0.30): DR's vector swing / tag
              # counter and the other printed-text readers are deterministic evidence too
              "openings", "room_size", "slab_area", "area_table", "pipe_runs", "piping",
              # G1 c11 (M1 22:16, preview f420f958): the facade reader's 18 west-facade windows (one exact box per window,
              # DR's openings agrees) went to Gemini ('no confident structured count') and Gemini's single reading ≈ 10 was
              # shown on top: a drawn per-window count is deterministic evidence like openings
              "facade")
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


def _native_vector(path: str) -> bool:
    try:
        from stoa.formats.normalize import native_vector_pdf
    except ImportError:  # P1's helper not on this branch
        return False
    return bool(path) and native_vector_pdf(path)


def _printed_exact(ans: Answer) -> bool:
    """An exact (printed text) evidence box whose note carries the answer's number (or the sum's terms all printed)."""
    if not isinstance(ans.value, (int, float)) or isinstance(ans.value, bool):
        return False
    v = float(ans.value)
    for e in ans.evidence or []:
        if not getattr(e, "exact", False):
            continue
        note = str(e.note or "")
        if re.search(r"\b(?:traced|measured|estimated|outline|at 1:\d)", note, re.I):
            continue  # our own measurement's note prints the number too (T1 21:25)
        for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", note):
            try:
                x = float(tok.replace(",", ""))
            except ValueError:
                continue
            if x > 0 and abs(x - v) <= 0.005 * max(abs(v), 1e-9):
                return True
    return False


# captain 00:31 (AUDIT RECHECK-0026 (1)): readers that MEASURE an outline / line at a scale (not a picture guess)
MEASURING = ("plan_rooms", "room_size", "line_rooms", "lengths", "slab_area", "pdf_vector", "dxf", "ifc", "svg_ifc",
             "pipe_runs", "footprint", "facade")
_KNOWN_SCALE = re.compile(r"\bscale: 1:\d+ printed|\bat the printed scale\b|\bscale bar\b|\bprinted (?:dimension|scale)|"
                          r"\bdimension (?:chain|string)s?\b|\bthe drawing'?s? own units\b|\bmodel units\b", re.I)
_UNKNOWN_SCALE = re.compile(r"\b(?:no|unknown|assumed|guessed|estimated) scale\b|\bscale (?:unknown|not (?:found|printed))|"
                            r"\bscale check: [^✓]*✗|\bpaper size\b", re.I)


def settled(ans: Answer, question: str = "") -> str | None:
    """Why a LOCAL answer needs no hosted second look, or None (captain 00:31, AUDIT RECHECK-0026 (1)): the hybrid default
    added a 25 s Gemini wait to answers that were already right (M-101A FP pumps 5 @0.8: 1.3 -> 28.4 s; the scanned
    kitchen re-ask 46 s). Skip it when the answer is LOCATED (boxes / outlines / a model element) AND printed (an exact
    text box carrying the value), or measured by a measuring reader at a known scale, or >= 0.8. Gemini stays for unsure,
    unlocated and unfamiliar sheets (where it earns its points)."""
    from stoa.pipeline import _located

    if os.environ.get("STOA_G1_SETTLED", "1") == "0":  # kill switch (freeze): every answer may get the hosted look
        return None
    if ans.value is None or not _located(ans) or str(ans.solver or "").endswith(("prior", "dummy")):
        return None
    if _words_only(ans.value):
        return None
    base = str(ans.solver or "").split("/")[-1]
    if ans.confidence >= 0.8:
        return f"located {base} answer @{ans.confidence:.2f}"
    if _printed_exact(ans) and ans.confidence >= 0.5:
        return f"the value is printed on the sheet at the marked spot ({base} @{ans.confidence:.2f})"
    t = task_of(question, ans) if question else ""
    if t in ("area", "length") and base in MEASURING and ans.confidence >= 0.5 and \
            isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool):
        said = " ".join(str(x) for x in [*(ans.assumptions or []), *(ans.trace or [])])
        if _KNOWN_SCALE.search(said) and not _UNKNOWN_SCALE.search(said):
            return f"measured by {base} at a known scale @{ans.confidence:.2f}"
    return None


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
    if settled(ans, question):  # captain 00:31: a located printed / measured / >= 0.8 answer waits for no hosted call
        return None
    if t in ("area", "length") and ans.confidence >= 0.5 and _printed_exact(ans):
        # captain 20:56 (2b): a local reading whose evidence IS the printed number (an exact text box carrying the value:
        # an area tag, a printed dimension, a table cell) needs no Pro call to confirm it (cost tier stays cheap)
        return None
    if t == "area" and ans.confidence < AREA_CHECK_BELOW and isinstance(ans.value, (int, float)):
        # captain 15:43: Gemini only as a CROSS-CHECK of our outline / scale on areas (it never answers an area alone)
        if os.environ.get("STOA_VECTOR_XCHECK", "0") != "1" and _located(ans) and _native_vector(path):
            # P1 21:25: not on a LOCATED reading from a NATIVE vector page (CAD / Revit export; pictures, scans and
            # traced copies keep it): 34 cached cross-checks there (6 runs) never pointed outside our outline and never
            # moved a value (6 raised a right answer to 0.6), each was a Pro call = an 'Expensive' badge and ~7 s
            return None
        return f"area cross-check of our {base} reading @{ans.confidence:.2f}"
    if t == "count" and base == "raster" and ans.confidence >= 0.55 and _located(ans) and any(
            re.match(r"\d+ circled type tags of the sheet's schedule", str(x)) for x in ans.trace or []):
        return None  # O1 23:47: one exact box per ringed type tag + the sheet's own schedule = a structured count
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


def _derotation(path: str):
    """PyMuPDF derotation matrix of a /Rotate'd PDF page 0 (display points -> unrotated pdf_pt), None when unrotated.
    5 of Stoa's 9 example PDFs are rotated 90°: S-100F 'S-100F' box landed at its rotation_matrix image (W6 18:16)."""
    try:
        import pymupdf

        from stoa.data import abspath

        with pymupdf.open(str(abspath(path))) as doc:
            return doc[0].derotation_matrix if len(doc) and doc[0].rotation else None
    except Exception:  # noqa: BLE001
        return None


def _words_only(v) -> bool:
    """A text answer to a numeric question: no number at all ('Not determinable'), a label that merely contains one
    ('BRH 87.50' = a sill height) or a dimension pair ('3,50 x 3,00'). Multi-part answers ('3 ; 43.82') stay."""
    if not isinstance(v, str) or ";" in v:
        return False
    t = v.strip()
    return not re.fullmatch(r"[~≈]?\s*-?\d+(?:[.,]\d+)?\s*(?:m²|m2|m³|m3|m|ft²|ft2|sq ?ft|ft)?", t)


_NUMERIC_ANSWER = re.compile(r"\s*(?:~|ca\.?|approx\.?|about)?\s*[-−]?\d[\d\s.,]*\s*[A-Za-z²³°%'\"’”/. ]{0,8}\s*")


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


def _log_failure(why: str, seconds: float) -> None:
    """Captain 20:56: failed / timed-out Gemini vision calls were in no log (gemini_calls.jsonl has the SENT requests,
    and counts toward the cap, so failures go to their own file): logs/gemini_failures.jsonl {time, agent, purpose,
    seconds, reason}."""
    try:
        from stoa.llm import GEMINI_LOG, _agent_id

        f = GEMINI_LOG.parent / "gemini_failures.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": _agent_id(),
                                 "purpose": "G1 vision reading (hybrid upload routing)", "seconds": round(seconds, 1),
                                 "reason": why}) + "\n")
    except Exception:  # noqa: BLE001 — logging never breaks an answer
        pass


def read(path: str, question: str, base: Answer | None = None, temperature: float = 0.0) -> Answer | None:
    """One Gemini vision call on the drawing; an Answer (capped) or None (no image / call failed / no answer)."""
    from stoa import llm
    from stoa.agent.gemini import LABEL, PRICE

    from stoa.agent.private import hosted_allowed

    if not hosted_allowed("gemini"):
        if base is not None:
            base.trace = [*base.trace, "private mode: no Gemini vision reading (the drawing stays on this server)"]
        return None
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
    # QA 20:37 (demo latency): a failing call waited 150-200 s in llm.chat's retries (300 s client timeout, 4 tries)
    # while the local answer was ready in seconds -> one retry and a deadline; the local answer stands on a miss
    import concurrent.futures as cf
    import contextvars

    wait = float(os.environ.get("STOA_VISION_WAIT", 25))  # captain 20:56: <= 25 s on the demo path
    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(contextvars.copy_context().run, llm.chat, [{"role": "user", "content": msg}],
                        images=[img, *quads], system=SYS, backend="gemini", model=VISION_MODEL, max_tokens=6000,
                        temperature=temperature, json_schema=SCHEMA, purpose="G1 vision reading (hybrid upload routing)",
                        cache=True, max_pixels=MAX_PIXELS, max_side=MAX_SIDE, retries=1)
        r = fut.result(timeout=wait)
    except Exception as e:  # noqa: BLE001
        why = f"no reply within {wait:.0f} s" if isinstance(e, cf.TimeoutError) else \
            f"{type(e).__name__}: {' '.join(str(e).split())[:160]}"
        _log_failure(why, time.perf_counter() - t0)
        if base is not None:
            base.trace = [*base.trace, f"{label} vision reading failed ({why}); the local answer stands"]
        return None
    finally:
        ex.shutdown(wait=False)
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
    derot = _derotation(path) if space == "pdf_pt" else None
    pt = d.get("point_2d") or []
    if len(pt) == 2:  # where the asked room is (measure_at_point traces it), kept even if the value is dropped below
        px, py = float(pt[1]) * img.width / 1000 / k, float(pt[0]) * img.height / 1000 / k
        if derot is not None:  # W6 18:16: the render is the DISPLAYED page; pdf_pt is the unrotated page space
            q = __import__("pymupdf").Point(px, py) * derot
            px, py = q.x, q.y
        _POINTS[(str(path), question)] = (px, py, space)
    raw, unit_raw = str(d["answer"]).strip(), str(d.get("unit") or "").strip().lower()
    unit = _UNIT.get(unit_raw, unit_raw or None)
    v = _num(raw)
    value: object = raw
    if v is not None and len(re.sub(r"[\d.,\s\-]", "", raw)) <= 3 and not re.search(r"\d\s*:\s*\d", raw) and \
            _NUMERIC_ANSWER.fullmatch(raw):
        # W6 18:16: 'S-100F' (a sheet number) was the value -100; 'B-104' / grid refs alike: numeric only when the whole
        # answer is a number with at most a short unit
        # W6 17:51: a scale ratio (1:200) is not the number 1
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
    boxes = d.get("evidence") or []
    for e in boxes[:MAX_BOXES]:
        b = e.get("box_2d") or []
        if len(b) != 4:
            continue
        y0, x0, y1, x1 = (float(t) for t in b)
        px = [min(x0, x1) * W / 1000, min(y0, y1) * H / 1000, max(x0, x1) * W / 1000, max(y0, y1) * H / 1000]
        bb = [round(c / k, 2) for c in px]
        if derot is not None:
            import pymupdf

            r = pymupdf.Rect(*bb) * derot
            r.normalize()
            bb = [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]
        ev.append(Evidence(file=path, page=0, bbox=bb, bbox_space=space, exact=False,
                           note=f"{label} read: {str(e.get('label', ''))[:80]}"))
    if is_count and isinstance(value, int) and ev and len(ev) != value:
        assumptions_n = [f"{label} said {value} but drew {len(ev)} boxes"]
    else:
        assumptions_n = []
    if len(boxes) > MAX_BOXES:
        assumptions_n.append(f"showing the first {MAX_BOXES} of {len(boxes)} boxes {label} drew")
    conf = VISION_CAP if not assumptions_n else round(VISION_CAP * 0.7, 3)  # never Gemini's self-report
    how = str(d.get("how") or "").strip()
    trace = [f"{label} vision reading: {raw} {unit_raw} — {how[:300]}"]
    assumptions = [f"read by {label} from the drawing image"] + assumptions_n + \
        [str(a)[:160] for a in (d.get("assumptions") or [])[:3]]
    missing = [str(m)[:160] for m in (d.get("missing") or [])[:3]]
    if is_count and isinstance(value, int) and len(ev) < value:  # T1: partial evidence says it is partial
        missing.append(_partial(len(ev), value))
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver="pipeline/gemini_vision", trace=trace,
                  assumptions=assumptions, missing=missing, cost=cost)


def _partial(shown: int, of: int) -> str:
    return f"showing {shown} of {of} counted items: {of - shown} are not marked on the drawing"


def _count_marks(a: Answer) -> int:
    return sum(1 for e in a.evidence or [] if e.bbox or e.ifc_guid or e.polygon)


def _coverage_ok(a: Answer, task: str, question: str = "") -> bool:
    """A count may only gain confidence when every counted item is marked, or a marked printed total says the number
    (T1, audit 19:51 risk #1: 20 doors, 12 boxes, promoted to 0.8 with the mismatch warning dropped)."""
    if task != "count" or not isinstance(a.value, (int, float)) or isinstance(a.value, bool):
        return True
    if _count_marks(a) >= int(a.value):
        return True
    from stoa.trust.support import support_state

    st = support_state(question or "How many?", a)
    return bool(st) and st["state"] == "supported"


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
    label = label_of(g)
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
    label = label_of(g)
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


BACKED_CONF = float(os.environ.get("STOA_BACKED_CONF", 0.6))
_FACTOR = re.compile(r"^\s*(\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?)\s*(mm|cm|m|dimension|dim\.?)?\s*$", re.I)


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", str(s))


def _sheet_tokens(path: str, space: str) -> list[tuple[str, tuple]]:
    """(text, box) of the printed tokens in the space Gemini's boxes were mapped to: PDF words (unrotated pdf_pt, the
    space read() derotates into) or the image's text layer (label_count._image_lines: cached / PDF-render lines)."""
    from stoa.data import abspath

    p = str(abspath(path))
    if space == "pdf_pt" and p.lower().endswith(".pdf"):
        try:
            import pymupdf

            with pymupdf.open(p) as doc:
                return [(w[4], tuple(w[:4])) for w in doc[0].get_text("words")]
        except Exception:  # noqa: BLE001
            return []
    try:
        from stoa.solvers.label_count import _image_lines

        out = []
        for t, bb in _image_lines(p) or ():
            out += [(w, bb) for w in str(t).split()] if len(str(t).split()) > 1 else [(t, bb)]
        return out
    except Exception:  # noqa: BLE001
        return []


def printed_backing(path: str, g: Answer) -> dict | None:
    """Captain 18:56 arbitration, areas: Gemini's area is BACKED when it is the product of two dimension numbers it
    marked (its evidence labels) AND our own text layer finds both numbers printed at the spots it marked. Fitted on the
    97 cached Gemini area readings (QA r2-r4 + X1 runs): the 12 two-factor products are S-100F slab 6200 x 9950 = 61.69 ✓
    (5x), genset 6200 x 4272 = 26.49 ✓ (3x), A-101F toilet 1900 x 2054 = 3.90 ✓ and two plain cm rooms; Gemini's
    estimates by eye ('approximately 3.8 m by 3.8 m'), wall-subtracted extents (19800-1200 x 12000-1200 = 200.88, gold
    214.5) and trapezoids are never a product of two marked numbers. A sheet-wide 'two printed numbers multiply to it'
    test is useless (180/200 random values matched): the check is LOCAL (each number inside Gemini's own box).
    -> {'a', 'b', 'unit', 'boxes': [(text, box), (text, box)], 'space'} or None."""
    if not isinstance(g.value, (int, float)) or isinstance(g.value, bool) or g.unit not in ("m2", "ft2") or g.value <= 0:
        return None
    marked = []
    for e in g.evidence or []:
        lab = str(e.note or "").split(" read: ", 1)[-1]
        m = _FACTOR.match(lab)
        if m and e.bbox:
            marked.append((float(m.group(1).replace(",", "").replace(" ", "")), m.group(1), e))
    if len(marked) < 2:
        return None
    scales = {"m2": ((1e-6, "mm"), (1e-4, "cm"), (1.0, "m")), "ft2": ((1.0, "ft"),)}[g.unit]
    pair = None
    for i in range(len(marked)):
        for j in range(i + 1, len(marked)):
            for s, u in scales:
                p = marked[i][0] * marked[j][0] * s
                if abs(p - g.value) <= 0.005 * g.value + 0.005:
                    pair = (marked[i], marked[j], u)
                    break
            if pair:
                break
        if pair:
            break
    if pair is None:
        return None
    space = pair[0][2].bbox_space
    toks = _sheet_tokens(path, space)
    if not toks:
        return None
    xs = [c for _, b in toks for c in (b[0], b[2])]
    ys = [c for _, b in toks for c in (b[1], b[3])]
    diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5 or 1.0
    found = []
    for val, raw, e in pair[:2]:
        x0, y0, x1, y1 = e.bbox
        pad = max(0.5 * ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5, 0.01 * diag)
        best = None
        for t, b in toks:
            if _digits(t) != _digits(raw):
                continue
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad:
                d = abs(cx - (x0 + x1) / 2) + abs(cy - (y0 + y1) / 2)
                if best is None or d < best[0]:
                    best = (d, t, b)
        if best is None:
            return None
        found.append((best[1], best[2]))
    return {"a": pair[0][1], "b": pair[1][1], "unit": pair[2], "boxes": found, "space": space}


def set_clarify(ans: Answer, payload: dict) -> Answer:
    """W5 19:10 shape (Answer.clarify = {ask, why, options: [{label, value, unit, evidence: [i], question}], click}).
    The field is set only once the contract has it (proposed to S0 19:2x); the trace always says it in one line."""
    try:
        if "clarify" in type(ans).model_fields:
            ans.clarify = payload
    except Exception:  # noqa: BLE001
        pass
    # W5 20:10: each option's evidence indices in the line too ('[evidence 0,1]'), until the contract field lands
    opts = " | ".join((f"{o.get('label')}: {o.get('value')} {o.get('unit') or ''}".strip()
                       + (f" [evidence {','.join(str(i) for i in o['evidence'])}]" if o.get("evidence") else ""))
                      for o in payload["options"])
    ans.trace = [*ans.trace, f"clarify: {payload['ask']} ({opts})"]
    return ans


def _backed_area(path: str, question: str, ans: Answer, g: Answer, back: dict, head: str, ours: bool) -> Answer:
    """Gemini's two-factor area whose factors our text layer found where it marked them becomes the answer (ours was
    weak or missing); our reading stays visible as the alternative / clarify option."""
    label = label_of(g)
    a, b, u = back["a"], back["b"], back["unit"]
    ev = [Evidence(file=path, page=0, bbox=[round(float(c), 2) for c in bb], bbox_space=back["space"], exact=True,
                   note=f"printed dimension {t} ({label} multiplied it; our text layer found it here)")
          for t, bb in back["boxes"]]
    ev += [e for e in g.evidence if not _FACTOR.match(str(e.note or "").split(" read: ", 1)[-1])]
    out = g.model_copy(deep=True)
    out.evidence = ev
    out.confidence = BACKED_CONF
    out.solver = "pipeline/gemini_vision"
    out.assumptions = [f"area = {a} x {b} {u}, both dimension strings printed on the drawing at the marked spots "
                       f"({label} chose the pair; our text layer confirmed both numbers)", *g.assumptions[1:]]
    c0 = float(ans.confidence or 0)
    steps = [f"{label} multiplied the printed dimensions {a} x {b} {u} = {g.value:g} {g.unit}; both numbers found by our "
             f"text layer inside the boxes it marked ✓ → confidence {BACKED_CONF}"]
    if ours:
        steps.append(f"{_our(ans)} {str(ans.value)[:30]} {ans.unit or ''} "
                     f"@{c0:.2f} had no such backing → kept as the alternative")
        out.missing = [*out.missing, f"our first reader answered {str(ans.value)[:40]} {ans.unit or ''} @{c0:.2f} "
                                     f"({ans.solver})"]
    out.trace = [*ans.trace[:3], head.rstrip(": ") + ":", *g.trace, *steps]
    out.cost = g.cost
    if ours and isinstance(ans.value, (int, float)):
        n = len(out.evidence)
        out.evidence = [*out.evidence, *(ans.evidence or [])[:6]]
        set_clarify(out, {"ask": "Two readings disagree: which area do you mean?", "why": "two readings disagree",
                          "options": [
                              {"label": f"{a} x {b} {u} (printed dimensions)", "value": g.value, "unit": g.unit,
                               "evidence": list(range(len(back["boxes"]))), "question": question},
                              {"label": _our(ans), "value": ans.value,
                               "unit": ans.unit, "evidence": list(range(n, len(out.evidence))), "question": question}],
                          "click": "room"})
    return out


_SCALE_PRINTED = re.compile(r"\b(?:printed|scale bar|bar\b)", re.I)
_SCALE_CHECKED = re.compile(r"scale check[^;]*✓|bar (?:agrees|matches|checks)|agrees with the (?:scale )?bar|"
                            r"bar[- ]agreed|door swings measure[^;]*✓", re.I)


def strong_outline(question: str, ans: Answer) -> str | None:
    """Captain 21:39 (X1's DfE scanned print, Laundry traced 7.12 m², gold 7.0, shown as 'Our guess' because Gemini's
    visual 12.5 m² pointed outside it): our reading is STRONG when (1) a closed outline is drawn (a polygon), (2) the
    outline's label is the room the question names, and (3) the scale is printed AND independently checked (bar / door
    swings). -> why (str) or None."""
    poly = [e for e in (ans.evidence or []) if getattr(e, "polygon", None) and len(e.polygon) >= 4]
    if not poly:
        return None
    lab = str(poly[0].note or "").split(":", 1)[0].strip()
    words = [w for w in re.findall(r"[a-zà-ÿ]{3,}", lab.lower()) if w not in ("room", "the", "traced", "outline")]
    if not words or not all(re.search(rf"\b{re.escape(w)}", question.lower()) for w in words):
        return None
    notes = [*ans.assumptions, *ans.trace]
    if not any(re.search(r"\bscale\b", n, re.I) and _SCALE_PRINTED.search(n) for n in notes):
        return None
    if not any(_SCALE_CHECKED.search(n) for n in notes):
        return None
    return f"closed outline labelled '{lab}', printed scale checked"


def _area_crosscheck(path: str, question: str, ans: Answer, g: Answer, head: str) -> Answer:
    """Our area stays the value (captain 15:43); Gemini checks WHICH room we measured (its point inside our outline)
    and roughly HOW BIG (its own estimate). Same room + estimate within 10 % -> 'two independent methods agree'
    (>= 0.6); Gemini points at ANOTHER room -> <= 0.3, with that room measured as the alternative."""
    label = label_of(g)
    gv = g.value if isinstance(g.value, (int, float)) and (g.unit or ans.unit) == ans.unit else None
    ours = float(ans.value)
    pt = _POINTS.get((str(path), question))
    inside = _inside(pt, ans.evidence) if pt else None
    near = gv is not None and abs(gv - ours) <= 0.1 * ours
    c0 = ans.confidence
    note = f"{head}{label} cross-check: its estimate {gv if gv is not None else '-'} {ans.unit or ''}"
    strong = strong_outline(question, ans) if not near else None
    if inside is False:
        alt = measure_at_point(path, question, g) or measure_raster_at_point(path, question, g)
        if strong:
            # captain 21:39: a strong measured outline loses a little, not its 'best estimate' standing
            ans.confidence = round(min(c0, max(0.42, c0 - 0.1)), 3)
        else:
            ans.confidence = round(min(c0, 0.3), 3)
        ans.missing = [*ans.missing, f"{label} points at a different room than the one measured"
                       + (f" (measured there: {alt.value} {alt.unit})" if alt is not None else "")
                       + (f"; its visual estimate {gv:g} {ans.unit or ''}" if gv is not None else "")]
        note += f"; it points OUTSIDE the outline we measured → confidence {c0:.2f} → {ans.confidence:.2f}" + \
            (f" (ours is a {strong}: lowered moderately)" if strong else "")
        _second_reading(ans, question, label, gv, alt)
    elif near:
        ans.confidence = round(max(c0, 0.6), 3)
        ans.assumptions = [*ans.assumptions, f"two independent methods agree ({ans.solver}: {ours:g}; {label}: {gv:g}"
                           + ("; same room" if inside else "") + ")"]
        note += f" agrees within 10 %{' and it points inside our outline' if inside else ''} ✓ → confidence " \
                f"{c0:.2f} → {ans.confidence:.2f}"
    else:
        if gv is not None:
            ans.missing = [*ans.missing, f"{label} estimated {gv:g} {ans.unit or ''} (a visual estimate, not measured)"]
            if ours > 0 and abs(gv - ours) > 0.25 * ours:
                _second_reading(ans, question, label, gv, None)
        note += (" points inside our outline (same room)" if inside else "") + "; our measurement is kept"
    ans.trace = [*ans.trace, note]
    ans.cost = ans.cost.add(g.cost) if ans.cost else g.cost
    return ans


def _second_reading(ans: Answer, question: str, label: str, gv, alt) -> None:
    """Captain 21:39: the second reading's VALUE in the answer (W5 shows both): Answer.clarify with our measured outline
    first (the value), then the other reading(s). Never replaces an existing clarify."""
    if getattr(ans, "clarify", None) or gv is None and alt is None:
        return
    n = len(ans.evidence or [])
    opts = [{"label": f"{_our(ans)} (measured outline)", "value": ans.value,
             "unit": ans.unit, "evidence": list(range(min(n, 1))), "question": question}]
    if alt is not None and isinstance(alt.value, (int, float)):
        opts.append({"label": f"the room {label} points at (measured)", "value": alt.value, "unit": alt.unit,
                     "evidence": [], "question": question})
    if gv is not None:
        opts.append({"label": f"{label}'s visual estimate (not measured)", "value": gv, "unit": ans.unit,
                     "evidence": [], "question": question})
    set_clarify(ans, {"ask": "Two readings differ: is the outlined room the one you mean?", "why": "second reading differs",
                      "options": opts, "click": "room"})


def _clip_text(t, n: int) -> str:
    t = str(t)
    return t if len(t) <= n else t[: n - 1] + "…"


def _unmeasured(ans: Answer, g: Answer, head: str) -> Answer:
    """Nothing of ours could measure the room (no outline, or no scale): Gemini's figure is shown only as an unmeasured
    estimate at <= ESTIMATE_CAP, and the user is asked for the scale (W5's 'set the scale' tool)."""
    label = label_of(g)
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
        import contextvars  # T2 21:14: private mode / cost meter context must reach the second sample's thread

        g2_future = pool.submit(contextvars.copy_context().run, read, path, question, None, 0.8)
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
        base = str(ans.solver or "").split("/")[-1]
        same = ours_num and isinstance(g.value, (int, float)) and g.unit == ans.unit and \
            abs(float(g.value) - float(ans.value)) <= 0.1 * abs(float(ans.value))
        back = None if same else printed_backing(path, g)
        if back is not None:
            # captain 18:56: a weak local reading yields to Gemini only when a second source backs it; here our own
            # text layer finds both dimension numbers Gemini multiplied, at the spots it marked (QA r2-r4: S-100F slab
            # 61.69 / genset 26.49 vs our raster @0.30). A confident structured reading of ours stays (clarify).
            c0 = float(ans.confidence or 0)
            if not ours_num or c0 <= WEAK + 1e-9 or (base not in STRUCTURED and c0 < 0.5):
                return _backed_area(path, question, ans, g, back, head, ours)
            kept = _area_crosscheck(path, question, ans, g, head)
            kept.confidence = round(min(kept.confidence, 0.5), 3)
            return set_clarify(kept, {
                "ask": "Two readings disagree: which area do you mean?", "why": "two backed readings disagree",
                "options": [{"label": _our(ans), "value": ans.value, "unit": ans.unit,
                             "evidence": list(range(len(kept.evidence))), "question": question},
                            {"label": f"{back['a']} x {back['b']} {back['unit']} (printed dimensions)",
                             "value": g.value, "unit": g.unit, "evidence": [], "question": question}],
                "click": "room"})
        veto = next((str(t)[len("size check: "):] for t in reversed(ans.trace or []) if str(t).startswith("size check: ")),
                    None)
        if ours_num and veto and isinstance(g.value, (int, float)):
            # QA 22:40 (EFA 2F scan, cold copy): 'Add up the areas of Science Lab 1 to Science Lab 5.' -> ours 1274 = the
            # sheet's printed 'Overall GIFA 1274m2' (size check -> 0.1, value KEPT) vs Gemini 415 = the gold: a reading
            # our own size check rejected is never the headline; Gemini's unmeasured estimate is, ours the alternative
            m = measure_at_point(path, question, g) or measure_raster_at_point(path, question, g)
            if m is not None:  # the room Gemini points at, measured by our geometry: better than either reading
                m.trace = [*ans.trace[:3], head.rstrip(": ") + ":", g.trace[0], *m.trace]
                m.missing.append(f"our first reader's {str(ans.value)[:40]} {ans.unit or ''} was rejected: "
                                 f"{_clip_text(veto, 90)}")
                return m
            out = _unmeasured(ans, g, head)
            out.trace = [*out.trace, f"{_our(ans)} {ans.value} {ans.unit or ''} was rejected by the size check "
                                     f"({_clip_text(veto, 90)}) → not shown as the answer"]
            out.missing = [*out.missing, f"our reader's {ans.value} {ans.unit or ''} was rejected: {_clip_text(veto, 90)}"]
            label = label_of(g)
            return set_clarify(out, {
                "ask": "Our reading failed a size check: which area is right?", "why": _clip_text(veto, 120),
                "options": [{"label": f"{label}'s visual estimate (not measured)", "value": g.value, "unit": g.unit,
                             "evidence": [], "question": question},
                            {"label": f"{_our(ans)} (rejected: {_clip_text(veto, 50)})", "value": ans.value,
                             "unit": ans.unit, "evidence": [], "question": question}],
                "click": "room"})
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
            c0 = float(ans.confidence or 0)
            out = _area_crosscheck(path, question, ans, g, head)
            if out.trace and str(out.trace[-1]).endswith("; our measurement is kept"):
                # QA 20:39 (EFA GF scan 'sports hall': agent look() 1,274 @0.32 vs Gemini 594, gold 504): an UNLOCATED
                # reading is not a measurement -> 'two readings disagree', capped, and the visitor picks
                out.trace[-1] = out.trace[-1][:-len("; our measurement is kept")] + (
                    f"; NEITHER reading is measured (ours, {base}, has no located outline): two readings disagree → "
                    f"ours shown at confidence {c0:.2f} → {min(c0, 0.3):.2f}, pick one")
                out.confidence = round(min(c0, 0.3), 3)
                if isinstance(g.value, (int, float)):
                    label = label_of(g)
                    out = set_clarify(out, {
                        "ask": "Two unmeasured readings disagree: which area is right?",
                        "why": "neither reading has a located outline",
                        "options": [{"label": f"{_our(ans)} (not located)", "value": ans.value, "unit": ans.unit,
                                     "evidence": list(range(len(out.evidence))), "question": question},
                                    {"label": f"{label}'s visual estimate", "value": g.value, "unit": g.unit,
                                     "evidence": [], "question": question}],
                        "click": "room"})
            return out
        return _unmeasured(ans, g, head)
    if ours and _same(ans.value, g.value, unit):
        c = round(max(ans.confidence, AGREE_CONF), 3)
        from stoa.pipeline import _located

        if not _located(ans):  # Gemini's boxes (one per counted object) become the evidence a human checks
            ans.evidence = [*(ans.evidence or []), *g.evidence]
        partial = not _coverage_ok(ans, task, question)
        if partial:
            # T1 (audit 19:51): 20 = 20 but only 12 of them marked -> the agreement does not prove the marks; no
            # promotion, and Gemini's own 'said 20 but drew 12 boxes' warning stays on the answer
            c = round(float(ans.confidence or 0), 3)
            ans.assumptions = [*ans.assumptions, *(a for a in g.assumptions if " but drew " in a or "showing the first" in a)]
            if not any("counted items" in str(m) for m in ans.missing):
                ans.missing = [*ans.missing, _partial(_count_marks(ans), int(ans.value))]
        ans.trace = [*ans.trace, head + g.trace[0] + f" — two independent readings agree (ours {ans.value}, Gemini "
                     f"{g.value}) ✓ confidence {ans.confidence:.2f} → {c:.2f}"
                     + (" (not raised: not every counted item is marked)" if partial else "")]
        ans.assumptions = [*ans.assumptions, f"two independent methods agree ({ans.solver}: {ans.value}; "
                                             f"{label_of(g)}: {g.value})"]
        ans.confidence = c
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
        full = _coverage_ok(g, task, question)  # T1: same-model agreement + partial marks is no reason to raise confidence
        g.confidence = SAMPLES_CONF if full else g.confidence
        g.trace = [*g.trace, f"second {VISION_MODEL} sample at temperature 0.8: {g2.value} — samples agree "
                   + (f"→ confidence {SAMPLES_CONF}" if full else "(confidence not raised: not every counted item is "
                                                                   "marked)")]
        g.assumptions.append("two Gemini samples agree")
    elif g2 is not None:
        g.confidence = DISAGREE_CONF
        g.trace = [*g.trace, f"second {VISION_MODEL} sample at temperature 0.8: {g2.value} — samples disagree "
                   f"→ confidence {DISAGREE_CONF}"]
        g.missing.append(f"a second Gemini reading gave {g2.value}")
    base = str(ans.solver or "").split("/")[-1]
    if ours and g2 is not None and not _same(g.value, g2.value, unit) and _same(ans.value, g2.value, unit):
        # QA arbiter r2-r4 (A-101F doors): label_count 6 ✓, Gemini 5 then 6 -> one Gemini sample backs ours
        return _kept(ans, g, head, f"{label_of(g)}'s two samples disagree ({g.value}, {g2.value}) and the second agrees "
                     f"with ours", max(float(ans.confidence or 0), SAMPLES_CONF if _coverage_ok(ans, task, question) else 0))
    if ours and _located(ans) and (base in STRUCTURED or task == "length") and not (
            g2 is not None and _same(g.value, g2.value, unit)):
        # QA arbiter r2-r4 (DfE laundry skirting raster 10.01 m ✓ -> Gemini 14.5): an evidence-backed reading (tags,
        # printed text, a measured outline) is never replaced by one unconfirmed vision reading; Gemini's figure is
        # kept as the alternative to check
        return _kept(ans, g, head, f"{_our(ans)} is backed by drawn / printed evidence; {label_of(g)}'s single "
                     f"visual reading {g.value} {g.unit or ''} is shown as the alternative", float(ans.confidence or 0))
    if ours:
        g.confidence = round(min(g.confidence, DISAGREE_CONF if g2 is None else g.confidence), 3)
        g.missing.append(f"our reader answered {ans.value} {ans.unit or ''} @{ans.confidence:.2f} ({ans.solver})")
    g.trace = [*ans.trace[:3], head.rstrip(": ") + ":", *g.trace]
    return g


def label_of(g: Answer) -> str:
    """The hosted model's name for a customer-facing label ('Gemini 3.1 Pro'), never a raw trace line: UX 02:34 (F8's stack,
    Enlisted dormitory example) — g.trace[0] was the 'recognised: {"kind": …}' JSON line and the clarify BUTTON showed it."""
    for t in g.trace or []:
        t = str(t)
        if " vision" in t and not t.lstrip().startswith(("recognised", "{", "[")):
            name = t.split(" vision")[0].strip()
            if name and len(name) <= 40 and not re.search(r"[{}\[\]\"=:]", name):
                return name
    return "Gemini"


_READER_WORDS = {"plan_rooms": "measured outline", "line_rooms": "measured outline", "room_size": "measured room",
                 "area_tags": "printed area tag", "area_table": "printed area table", "pdf_text": "printed text",
                 "pdf_vector": "measured drawing", "raster": "reading of the picture", "lengths": "measured length",
                 "slab_area": "measured slab", "footprint": "measured building outline", "takeoff": "measured rooms",
                 "openings": "door finder", "label_count": "printed labels", "stated_count": "printed count",
                 "schedule": "schedule", "dxf": "CAD drawing", "ifc": "building model", "vlm_lora": "picture model",
                 "vlm_direct": "picture model", "agent": "general agent", "prior": "typical value", "facade": "facade count"}


def _our(ans: Answer) -> str:
    """'our <words> reading' for a customer-facing label (UX 02:35: never a solver id like 'plan_rooms')."""
    base = str(ans.solver or "").split("/")[-1]
    return f"our {_READER_WORDS.get(base, 'own')} reading"


def _kept(ans: Answer, g: Answer, head: str, why: str, conf: float) -> Answer:
    """Our reading stays the answer; Gemini's reading is the stated alternative (captain 18:56 arbitration)."""
    ans.trace = [*ans.trace, head + (g.trace[0] if g.trace else "") + f" — {why} → ours kept"]
    ans.missing = [*ans.missing, f"{label_of(g)} read {g.value} {g.unit or ''} (alternative; check the marked spots)"]
    ans.confidence = round(min(max(conf, 0.0), 0.8), 3)
    ans.cost = g.cost
    return ans


__all__ = ["read", "vision_step", "wanted", "VISION_CAP"]
