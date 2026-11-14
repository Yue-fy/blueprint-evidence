"""Text-grounded answers on raster drawings without a template (historical scans, unseen plan styles).

One VLM call gets the image AND the drawing's text layer (textlayer.py: every printed line with its position, read
tile by tile at ~2x, so small title-block print is legible), and must say which text lines its answer comes from.
Those lines' boxes become the Evidence (image_px), so a reader can check the answer in seconds. Without such a line
the answer is an image-only reading at low confidence, said so in `missing`.

Numbers: a printed scale note / dimension string / area is parsed deterministically (textlayer.scales, dimensions,
areas) and given to the model as facts; it states its assumptions (e.g. "no scale printed").
"""

from __future__ import annotations

import json
import re

from stoa import llm
from stoa.contract import Answer, Evidence
from stoa.solvers.raster import apartment as A
from stoa.solvers.raster import chains as CH
from stoa.solvers.raster import scalebar as SB
from stoa.solvers.raster import textlayer as T

# a size question (length / width / height / area / distance) in the languages of the dataset
_SIZE = re.compile(r"\b(length|long|width|wide|height|high|tall|areas?|size|dimensions?|distance|span|depth|deep|"
                   r"perimeter|how big|square (?:feet|foot|meters?|metres?)|sq\.? ?ft|m²|m2|ft²|pituus|pitkä|leveys|"
                   r"korkeus|pinta-ala|länge|lang|breite|breit|höhe|hoch|fläche|longueur|largeur|hauteur|surface|"
                   r"superficie|lunghezza|larghezza|altezza|längd|lång|bredd|höjd|yta|thick|thickness|dicke?|stärke|"
                   r"paksuus|paksu|épaisseur|spessore|tjocklek|tjock)\b", re.I)

MEASURE = (', "measure": {"bbox_2d": [x1, y1, x2, y2], "extent": "width|height|area"} (ONLY if no printed text '
           'gives the size: the box of the element whose size is asked, 0-1000 relative to the image, and which '
           'extent the question asks for; the program measures it with the graphic scale bar)')

PROMPT ="""You answer a question about a scanned technical drawing (architecture / engineering).
You get the image and the list of texts read from it: [id] (x%, y%) text, where x% and y% are the position of the
text's centre as a percentage of the image width and height.
Use the texts for anything printed (names, places, numbers, dimensions, scale notes, dates, sheet numbers); use the
image for the layout, the drawing type and anything drawn but not written.
{facts}
TEXTS:
{texts}

QUESTION: {question}

Reply with JSON only: {{"answer": "<short final answer: a name, a number, a short phrase, yes/no>", "unit": "<unit of a
numeric answer (m, m2, ft, ft2, count, ...) or empty>", "lines": [<ids of the text lines the answer is read from;
[] if it comes only from the image>], "assumptions": "<what you assumed, or empty>"{measure}}}"""

SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string", "maxLength": 200},
    "unit": {"type": "string", "maxLength": 12},
    "lines": {"type": "array", "items": {"type": "integer"}, "maxItems": 8},
    "assumptions": {"type": "string", "maxLength": 300}},
    "required": ["answer", "unit", "lines", "assumptions"]}
SCHEMA_MEASURE = {**SCHEMA, "properties": {**SCHEMA["properties"], "measure": {
    "type": "object", "properties": {
        "bbox_2d": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "extent": {"type": "string", "enum": ["width", "height", "area"]}}, "required": ["bbox_2d", "extent"]}}}

_NUM = re.compile(r"^\s*-?\d+(?:[.,]\d+)?\s*$")


_SCALE_Q = re.compile(r"\b(scale|mittakaava\w*|ma(?:ß|ss)stab\w*|[ée]chelle|skala\w*|scala|escala|schaal)\b", re.I)


def _r(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _fmt_scale(s: dict) -> str:
    return f"{s['text']} (1:{_r(s['ratio'])})" if s["kind"] == "imperial" else f"1:{_r(s['ratio'])}"


def _scale_answer(img_path: str, question: str, tl: T.TextLayer, solver: str) -> Answer | None:
    """'What is the scale?' answered from the parsed scale notes with their boxes, 0 LLM calls (Q bug 29: the 8B's
    answer depended on the wording, empty for 1 of 3 wordings on the same sheet). None when no note is printed."""
    if not _SCALE_Q.search(question) or _SIZE.search(question):
        return None
    sc = T.scales(tl)
    if not sc:
        return None
    pref = [x for x in sc if x["labelled"]] or sc
    # O1: an OCR note that lost a stacked fraction ('1/8"' -> '8"=1'-0"' = 1:1.5) is not a drawing scale; drop
    # ratios under 1:3 when the sheet also prints a plausible one (full / half size details keep theirs otherwise)
    if any(x["ratio"] >= 4 for x in pref):
        pref = [x for x in pref if x["ratio"] >= 3]
    ratios: list[float] = []
    for x in pref:
        if x["ratio"] not in ratios:
            ratios.append(x["ratio"])
    first = {r: next(x for x in pref if x["ratio"] == r) for r in ratios}
    assumptions, missing = [], []
    if len(ratios) == 1:
        value = _fmt_scale(first[ratios[0]])
        agree = sum(x["ratio"] == ratios[0] for x in sc)
        conf = 0.75 if (pref[0]["labelled"] or agree >= 2) else 0.6
    else:  # one scale per view (plan 1/8", detail 3/4"): name them all, the reader picks the view
        value = "; ".join(_fmt_scale(first[r]) for r in ratios[:4])
        conf = 0.5
        assumptions.append(f"the sheet prints {len(ratios)} different scales (one per view); all are listed")
        missing.append("which view of the sheet the question means")
    if any(first[r].get("unit_from_label") for r in ratios):
        conf = min(conf, 0.6)
        assumptions.append("the note prints no unit after the number; feet taken from the 'FEET' label of the "
                           "graphic scale")
    ev = [Evidence(file=img_path, page=0, bbox=[round(v, 1) for v in x["box"]], bbox_space="image_px",
                   note=f"scale note '{x['line'][:60]}' = 1:{_r(x['ratio'])}") for x in sc[:6]]
    return Answer(value=value, unit=None, confidence=conf, evidence=ev, solver=solver,
                  trace=[f"text layer: {len(tl.items)} texts; scale notes " +
                         ", ".join(f"'{x['text']}' = 1:{_r(x['ratio'])}" for x in sc[:6]) + " (read, 0 LLM calls)"],
                  assumptions=assumptions, missing=missing)


def facts(tl: T.TextLayer, bars: list | None = None) -> str:
    out = []
    sc = T.scales(tl)
    if sc:
        out.append("Scale notes found: " + "; ".join(f"'{s['line'][:50]}' = 1:{s['ratio']:g}" for s in sc[:3]) + ".")
    else:
        out.append("No scale note was found in the texts.")
    ar = T.areas(tl)
    if ar:
        out.append(f"Printed areas: {len(ar)} (e.g. " + ", ".join(a["line"][:30] for a in ar[:4]) + ").")
    for b in (bars or [])[:2]:
        out.append(f"Graphic scale bar found ({b.unit}): the program can measure a drawn element with it.")
    return "\n".join(out)


_WHERE_Q = re.compile(r"\b(where|wo|missä|mista|où|dove|var|dónde|donde|waar)\b", re.I)
_WORD = re.compile(r"[^\W\d_]{3,}", re.U)
_STOP = {"the", "this", "that", "what", "which", "who", "how", "many", "much", "is", "are", "was", "of", "on", "in",
         "drawing", "sheet", "plan", "does", "and", "for", "with", "from", "give", "answer", "like", "there", "shown"}


def select_items(items: list, question: str, W: float, H: float, k: int) -> list:
    """At most k layer lines for the QA prompt, in reading order (O1). A plain [:k] on a 300-600 line HABS layer
    (OCR reads 2x the VLM's lines) cuts the bottom and right margins, where the title block (structure, state,
    sheet 'N of M', delineator, date) lives. Rank: words shared with the question, title-block margins (outer
    12% on the right / bottom), then longer lines; short numeric fragments (member sizes, dimension ticks) last."""
    if len(items) <= k:
        return list(items)
    qw = {w.lower() for w in _WORD.findall(question)} - _STOP

    def score(i) -> float:
        t = i.text
        words = {w.lower() for w in _WORD.findall(t)}
        s = 3.0 * len(words & qw) + min(len(t), 60) / 20.0
        if i.cx > 0.88 * W or i.cy > 0.88 * H or i.cx < 0.06 * W or i.cy < 0.06 * H:
            s += 2.0
        if not words:  # only digits / symbols
            s -= 1.5
        return s

    keep = sorted(range(len(items)), key=lambda j: -score(items[j]))[:k]
    return [items[j] for j in sorted(keep)]


def answer(path: str, question: str, image_path: str | None = None, backend: str = "local",
           max_texts: int = 160, solver: str = "raster", layer: T.TextLayer | None = None) -> Answer:
    img_path = image_path or path
    tl = layer or T.text_layer(img_path)  # layer: a ready text layer (a vector PDF's own text: pdf_textqa)
    W, H = tl.size
    items = select_items(tl.items, question, W, H, max_texts)
    texts = "\n".join(f"[{k}] ({100 * i.cx / W:.0f}%, {100 * i.cy / H:.0f}%) {i.text[:100]}" for k, i in enumerate(items))
    got = _scale_answer(img_path, question, tl, solver)
    if got is not None:
        return got
    got = _level_diff_answer(img_path, question, tl, items, texts, backend, solver)
    if got is not None:
        return got
    got = _tag_count_answer(img_path, question, tl, solver)  # 'How many pressure gauges (PG) are tagged?'
    if got is not None:
        return got
    got = CH.overall_answer(img_path, question, tl, solver)  # overall dimension = a printed total its chain sums to
    if got is not None:
        return got
    bars = []
    ub = SB.user_bar()  # W5 'set scale' (solver_args px_per_m): the visitor's scale replaces any detected bar
    if ub is not None and _SIZE.search(question):
        bars = [ub]
    elif _SIZE.search(question):  # a size question: a graphic scale bar lets the PROGRAM measure (scalebar.py)
        try:
            bars = [b for b in SB.bars(img_path, backend=backend, layer=tl) if b.unit in SB.UNIT_M]
        except Exception:  # noqa: BLE001 - a bar is a bonus, never a reason to fail the question
            bars = []
        if not bars:  # no drawn bar: a printed '1:100' + the image's DPI on a standard sheet size (clean renders)
            pb = SB.printed_bar(img_path, tl)
            bars = [pb] if pb is not None else []
        if not bars:  # neither: the spacing of verified dimension chains (chains.py; >= 2 chains agreeing)
            cb = CH.chain_bar(tl)
            bars = [cb] if cb is not None else []
    cmp = A.compared_units(question, tl)
    if cmp:
        got = _compare_units(img_path, question, tl, bars, cmp, solver)
        if got is not None:
            return got
    unit_q = _AREA_Q.search(question) and A.unit_letter(question)
    if _AREA_Q.search(question) and _sum_scope(question, tl):
        got = _unit_area_answer(img_path, question, tl, bars, solver)
        if got is not None:
            return got
    if (_AREA_Q.search(question) or _BIG_Q.search(question)) and not unit_q:
        got = _rooms_sum_answer(img_path, question, tl, bars, solver)
        if got is not None:
            return got
    if bars and (_DIMS_Q.search(question) or _LEN_Q.search(question)) and not _AREA_Q.search(question) and not unit_q:
        got = _room_dims_answer(img_path, question, tl, bars, solver)
        if got is not None:
            return got
    if bars and (_AREA_Q.search(question) or _BIG_Q.search(question)) and not unit_q:
        got = _room_area_answer(img_path, question, tl, bars, solver)
        if got is not None:
            return got
    if (_AREA_Q.search(question) or _BIG_Q.search(question) or _LEN_Q.search(question)) and not unit_q:
        got = _chain_room_answer(img_path, question, tl, solver)
        if got is not None:
            return got
    # a printed-ratio scale measures labelled rooms above, but it does not go to the VLM: offered a ruler, the VLM
    # measures boxes instead of citing printed dimension strings (X1 esino_png '1104' -> an 11.44 m box @0.05)
    bars = [b for b in bars if getattr(b, "source", "") not in ("printed", "chains")]
    prompt = PROMPT.format(facts=facts(tl, bars), texts=texts or "(no text found)", question=question.strip(),
                           measure=MEASURE if bars else "")
    r = llm.chat(prompt, images=[str(T._abspath(img_path))], backend=backend, max_tokens=300,
                 json_schema=SCHEMA_MEASURE if bars else SCHEMA, cache=True)
    trace = [f"text layer: {len(tl.items)} texts in {tl.tiles} tile(s); {facts(tl).splitlines()[0]}"]
    if bars:
        trace.append(bars[0].describe() + (f" [{'; '.join(bars[0].notes)}]" if bars[0].notes else ""))
    try:
        d = json.loads(r.text)
    except ValueError:
        m = re.search(r"\{.*\}", r.text, re.S)
        d = json.loads(m.group()) if m else {}
    val = str(d.get("answer", "")).strip()
    if not val:
        return Answer(value=None, solver=solver, trace=trace + ["no answer from the text-grounded reading"],
                      missing=["the drawing's text did not answer the question"])
    cited = [items[k] for k in d.get("lines", []) if isinstance(k, int) and 0 <= k < len(items)]
    # does the answer literally appear in the cited lines (or anywhere in the text layer)?
    in_cited = any(T.match_score(val, i.text) >= 0.85 for i in cited) or \
        (len(val) > 2 and T.match_score(val, " ".join(i.text for i in cited)) >= 0.85)
    unit = str(d.get("unit", "")).strip() or None
    value: object = val
    two = two_lengths(val) if (_AREA_Q.search(question) or _BIG_Q.search(question)) and not _OPENING_Q.search(question) \
        else None
    if two is not None:
        a2, b2, u2 = two
        nums = re.findall(r"\d+(?:[.,]\d+)?", val)
        two_cited = bool(cited) and all(any(n in i.text or n.replace(",", ".") in i.text.replace(",", ".") for i in cited)
                                        for n in nums)
        v2 = round(a2 * b2, 3)
        trace.append(f"the reading '{val}' gives two sides: {a2:g} × {b2:g} {u2} = {v2:g} {u2}²"
                     + (" (both printed in the cited text)" if two_cited else ""))
        return Answer(value=v2, unit="ft2" if u2 == "ft" else "m2",
                      confidence=TWO_LEN_CONF["cited" if two_cited else "uncited"], solver=solver, trace=trace,
                      evidence=[Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in i.box], bbox_space="image_px",
                                         note=f"text '{i.text[:60]}'") for i in cited[:6]],
                      assumptions=[f"area = the two sides read on the drawing multiplied ({a2:g} {u2} × {b2:g} {u2}): "
                                   "the room / outline is taken as a rectangle"],
                      missing=["the drawn outline (a non-rectangular room or a recess changes the area)"]
                      + ([] if two_cited else ["the two sides are not both printed in the cited text"]))
    um = _NUM_UNIT.match(val)
    if um and not _NUM.match(val):  # '20,5 m²' -> 20.5 m2 (R1 08:47: such strings kept 0.75 and escaped every check)
        val, unit = um.group(1), unit or _UNIT_OF.get(um.group(2).lower().replace(" ", ""), um.group(2))
    if _NUM.match(val):
        v = float(val.replace(",", "."))
        value = int(v) if v.is_integer() and (unit in (None, "count")) else v
        # the literal check, decimal comma or point ('K 14,5' prints 14.5)
        num = re.escape(val.replace(",", "."))
        in_cited = in_cited or any(re.search(rf"(?<![\d.]){num}(?![\d])", i.text.replace(",", ".")) for i in cited)
    else:
        fm = T._FTIN.fullmatch(val.strip().rstrip('"”').strip() + '"') or T._FTIN.fullmatch(val.strip())
        if fm:  # a dimension string 187'-0" -> 187.0 ft (the printed text stays in the trace and evidence)
            value, unit = round(int(fm.group(1)) + (T._frac(fm.group(2)) / 12 if fm.group(2) else 0), 3), "ft"
    cm_note = _cm_chain(value, unit, question, cited, tl)
    if cm_note:
        value, unit = round(float(value) / 100, 3), "m"
    wh = _opening_pair(value, question, cited, tl)
    if wh is not None:
        value, partner, wh_note = wh
        cited = cited + [partner] if partner is not None and partner not in cited else cited
        in_cited = True
    lab_count = _label_count(value, question, cited)
    if lab_count:
        in_cited = True
    ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in i.box], bbox_space="image_px",
                   note=f"text '{i.text[:60]}'") for i in cited[:max(6, len(cited) if lab_count else 6)]]
    # X1 wild exam: 'How wide is the office?' -> '4 m' citing the graphic scale bar's own label '4m': a bar label is
    # the drawing's ruler, never a printed dimension of the asked element
    bar_echo = bool(bars and cited) and all(any(b.box[0] - 3 <= i.cx <= b.box[2] + 3 and b.box[1] - 3 <= i.cy <= b.box[3] + 3
                                                 for b in bars) for i in cited)
    printed = any(re.search(r"\d", i.text) for i in cited) and not bar_echo
    if bars and not printed and isinstance(d.get("measure"), dict):
        got = measure(d["measure"], bars[0], question, tl.size)
        if got is not None:
            mv, mu, mbox, note = got
            trace.append(f"the VLM's own reading was '{val}'; {note}")
            return Answer(value=mv, unit=mu, confidence=MEASURED_CONF, solver=solver, trace=trace,
                          evidence=[Evidence(file=img_path, page=0, bbox=mbox, bbox_space="image_px",
                                             note="measured element (bounding box)")] + bars[0].evidence(img_path),
                          assumptions=[f"measured on the drawing: the element's bounding box × the graphic scale bar "
                                       f"({bars[0].px_per_unit:.2f} px per {bars[0].unit}); no printed dimension "
                                       "gives this size"]
                          + (["area = box width × height (a rectangle)"] if d["measure"].get("extent") == "area"
                             else []),
                          missing=["a printed dimension or the element's exact outline (a box measurement is an "
                                   "estimate)"])
    # Confidence from the 80-question scan probe (artifacts/W3/scan_text.jsonl, run 031842-346a; HABS + Finna, judged):
    # cited lines 54/72 right (75%; HABS alone 90%), image-only 1/2 -> cited 0.75, image-only 0.3
    conf = 0.75 if cited else 0.3
    missing = [] if cited else ["the answer is not printed as text: an image-only reading"]
    if isinstance(value, (int, float)) and any(abs(float(n) - value) < 1e-9 for n in
                                               re.findall(r"\d+(?:\.\d+)?", question)) and _SIZE.search(question):
        # the 'size' is a number the question itself names (room 0.20 -> '0.20 m²'): an echo, not a reading
        conf = 0.05
        missing = [*missing, "the answer repeats a number from the question (likely the room's number, not its size)"]
    if cited and isinstance(value, (int, float)) and conf > 0.4:
        # R1 08:47, 18 by-eye CubiCasa area Qs at a flat 0.75: 9/18 right; the right ones were single named rooms read
        # literally; wrong: sums ('combined area of kitchen and living room' -> one term), superlatives ('largest
        # bedroom' -> OLOHUONE 20.5) and a room word mapped to the wrong code ('sauna' -> 'RH 6.3')
        from stoa.solvers.raster.classes import label_class, question_class, question_classes

        why = None
        qc = question_class(question)
        # the room code is printed next to its area ('K' over '14.0'): classes of the cited lines and their neighbours
        near = {label_class(j.text) for i in cited for j in tl.items
                if abs(j.cx - i.cx) < 4 * max(6.0, i.box[3] - i.box[1]) and abs(j.cy - i.cy) < 3 * max(6.0, i.box[3] - i.box[1])}
        if not in_cited:
            why = "the number is not printed in the cited text"
        elif _AGG_Q.search(question) or len(question_classes(question)) >= 2:
            why = "a sum or superlative over several rooms read off the texts (each term not checked)"
        elif qc and qc not in near and wh is None:
            why = f"the cited label{'s' if len(cited) > 1 else ''} " + ", ".join(f"'{i.text}'" for i in cited[:3]) + \
                  f" {'are' if len(cited) > 1 else 'is'} not a {qc.replace('_', ' ')} label"
        if why:
            conf = 0.3 if "label" in why else 0.4
            missing = [*missing, why]
            if "sum" in why and re.search(r"\b(apartments?|units?|flats?|dwellings?|asun\w*|wohnung\w*|l[äa]genhet\w*|"
                                          r"appartements?|appartament\w*|viviend\w*)\b", question, re.I):
                # H/Ziying x1-023: a type sheet prints ONE area for identical units drawn twice (studio 31.0 m² x 2)
                missing.append("the number of DRAWN units is not checked: a type sheet may print one area label for "
                               "several identical units")
    if (_AREA_Q.search(question) or _BIG_Q.search(question)) and not _AGG_Q.search(question) and not unit_q and \
            isinstance(value, (int, float)) and cited and conf > 0.2 and \
            any(_TOTAL_LINE.search(j.text) for i in cited for j in tl.items
                if abs(j.cy - i.cy) < 4 * max(6.0, i.box[3] - i.box[1]) and
                abs(j.cx - i.cx) < 12 * max(6.0, i.box[3] - i.box[1])):
        # D1 16:59 (Stoa-style school PNG): 'floor area of the plant room?' -> 1248 m² @0.75 from the title block's
        # 'Overall GIFA … Ground: 1248m²' (the cited 'Ground:' + '1248m²' sit under 'Overall GIFA'): a floor / building
        # total is not one room's area
        conf = min(conf, 0.15)
        missing = [*missing, "the cited area is a floor or building total (GIFA / gross / overall), not the room's own"]
    if unit and _SIZE.search(question) and str(unit).strip().lower().rstrip(".") not in _UNITS_OK:
        # P1 14:45: 'How thick is the outer wall?' -> '87.5 BRH' @0.75 ('BRH' = Brüstungshöhe, a label, not a unit)
        missing = [*missing, f"'{unit}' is not a unit: the cited number belongs to a label ({unit}), not to the asked size"]
        conf, unit = min(conf, 0.3), None
    if (_AREA_Q.search(question) or _BIG_Q.search(question) and not _OPENING_Q.search(question)) and \
            unit in ("m", "mm", "cm", "ft") and not _LEN_Q.search(question):
        # X1 wild exam: 'What is the floor area of the platea?' -> '1460 mm' @0.3 (one side's dimension string)
        conf = min(conf, 0.05)
        missing = [*missing, f"an area was asked but the reading is a length ({val} {unit}); the other side is needed"]
    if bar_echo:
        conf = min(conf, 0.1)
        missing = [*missing, "the cited text is the graphic scale bar's own label, not a dimension of the asked element"]
    if _SIZE.search(question) and not isinstance(value, (int, float)) and conf > 0.3 and re.search(r"\d", val):
        conf = 0.3  # a size question answered with something that is not one number ('38.42.46')
        missing = [*missing, f"the answer '{val[:30]}' is not a single number"]
    if cmp:  # a unit comparison the boundaries could not settle: a VLM reading of sums it never did (3 misses at 0.75)
        conf = min(conf, 0.3)
        missing = [*missing, "could not measure every compared unit from its drawn boundary; the comparison is a "
                             "reading of the drawing, not a measurement"]
    if unit_q and isinstance(value, (int, float)) and A.unit_labels(tl) and unit_q not in A.unit_labels(tl):
        conf = min(conf, 0.1)
        missing = [*missing, f"this plan labels units {', '.join(sorted(A.unit_labels(tl)))}; there is no apartment "
                             f"{unit_q}"]
    elif unit_q and isinstance(value, (int, float)):
        # a whole unit's area read off the texts: the reading usually takes one room (Q 05:16: apartment B -> room 2.10
        # alone, 16.78 of 80.83 m2 at 0.75); without the unit's drawn boundary nothing checks which rooms belong to it
        conf = min(conf, 0.2)
        missing = [*missing, f"could not find apartment {unit_q}'s drawn boundary on the image, so which rooms belong to "
                             "it is not checked (the reading may cover one room only)"]
    if bars and cited and isinstance(value, (int, float)) and unit in SB.UNIT_M and not unit_q:
        # a printed length: show the drawing's scale bar too, so a reader can check the size against it (W5 05:11)
        ev += bars[0].evidence(img_path)
    if _WHERE_Q.search(question) and cited and in_cited and isinstance(value, str):
        # Q bug 34c: 'Where is the kitchen?' -> 'K T * R' @0.75 (the label text itself). A where-answer names the
        # place: the label, its position on the sheet and its box; full confidence only if the label is of the asked
        # room class
        from stoa.solvers.raster.classes import label_class, question_class

        lab = cited[0]
        W0, H0 = tl.size
        h = "left" if lab.cx < W0 / 3 else "right" if lab.cx > 2 * W0 / 3 else "centre"
        v = "upper" if lab.cy < H0 / 3 else "lower" if lab.cy > 2 * H0 / 3 else "middle"
        where = "centre" if (h, v) == ("centre", "middle") else f"{v} {h}".replace("middle ", "").replace(" centre", "")
        value = f"in the room labelled '{lab.text}' ({where} of the sheet; box shown)"
        qc = question_class(question)
        if qc and label_class(lab.text) != qc:
            conf = min(conf, 0.3)
            missing = [*missing, f"the label '{lab.text}' is not clearly a {qc.replace('_', ' ')} label"]
    assumptions = [a for a in [str(d.get("assumptions", "")).strip()] if a and a.lower() not in ("none", "n/a")]
    if cm_note:
        assumptions = [cm_note, *assumptions]
    if wh is not None:
        assumptions = [wh_note, *assumptions]
        conf = min(conf, 0.6)
    if lab_count and conf >= 0.3:
        # X1 esino 'How many W.C.s …?' -> 3 citing 'W.C.', 'W.C.', 'W.C:' was 'unsure' (0.4: '3' is not printed), so
        # the pipeline asked the agent, whose LoRA said 2. A count that IS its cited labels is grounded label by label.
        assumptions = [lab_count, *assumptions]
        conf = LABEL_COUNT_CONF
    trace.append(f"answer '{val}' from text lines {[k for k in d.get('lines', [])][:8]}"
                 + (" (literal match)" if in_cited else ""))
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=solver, trace=trace,
                  assumptions=assumptions, missing=missing)


_UNITS_OK = {"m", "mm", "cm", "dm", "km", "m2", "m²", "sqm", "sq m", "ft", "feet", "foot", "in", "inch", "inches", "ft2",
             "ft²", "sq ft", "sqft", "sf", "yd", "m3", "m³", "count", "%", "°", "deg", "degrees", "l", "kg", "t", "kn",
             "kw", "w", "lux", "pcs", "units", "metres", "meters", "metre", "meter", "sq. ft", "sq.ft", "square feet",
             "square metres", "square meters", "cm2", "cm²", "mm2", "mm²"}
_BIG_Q = re.compile(r"\b(how big|how large|how spacious|wie gro(?:ss|ß)|quanto (?:è )?grande|kuinka suuri|hur stor)\b", re.I)
_DEC_M = re.compile(r"(?<![\d.,])\d{1,2}[.,]\d{2}(?![\d.,])")
_LEN_Q = re.compile(r"\b(length|long|width|wide|deep|depth|distance|span|perimeter|pituus|pitkä|leveys|länge|lang|"
                    r"breite|breit|longueur|largeur|lunghezza|larghezza|längd|lång|bredd)\b", re.I)


# 'How much higher is the stage floor than the platea floor?' = the difference of two printed LEVEL marks (X1 esino:
# 855.55 - 853.85 = 1.70; the free reading said 0.7). The VLM only picks which mark belongs to which thing; the
# program subtracts.
_LEVEL_DIFF_Q = re.compile(
    r"\bhow\s+much\s+(higher|lower|deeper|taller)\b|\b(?:height|level|elevation|floor[- ]level)\s+difference\b|"
    r"\bdifference\s+(?:in|of|between)\s+(?:the\s+)?(?:height|level|elevation)s?\b|\b(?:um\s+)?wie\s*viel\s+(h[öo]her|"
    r"tiefer)\b|h[öo]henunterschied|\bquanto\s+(?:è\s+)?(?:più\s+)?(alt[oa]|bass[oa])\b|dislivello|korkeusero|"
    r"\bkuinka\s+paljon\s+(korkeam\w*|matalam\w*)|\bde\s+combien\b.*\bplus\s+(haut|bas)\b|d[ée]nivel\w*|"
    r"\bhur\s+mycket\s+(h[öo]gre|l[äa]gre)\b|nivåskillnad", re.I)
_LEVEL = re.compile(r"(?<![\d.,])([+\-±]\s?\d{1,3}[.,]\d{2,3}|\d{3,4}[.,]\d{2})(?![\d])")
_LOWER_W = re.compile(r"\b(lower|deeper|tiefer|bass[oa]|matalam\w*|bas|l[äa]gre)\b", re.I)
LEVEL_PROMPT = """You answer a question about a technical drawing (architecture). You get the image and the texts read
from it: [id] (x%, y%) text (position of the text's centre in % of the image width and height).
The question compares the LEVELS (elevations) of two things. These texts are printed level marks:
{levels}
TEXTS:
{texts}

QUESTION: {question}

Which level mark belongs to the FIRST thing the question names, and which to the SECOND? Use the room names and
positions (a level mark is printed inside or next to the space it belongs to). Reply with JSON only:
{{"first": <id of a level mark>, "second": <id of a level mark>, "first_name": "<the first thing>",
"second_name": "<the second thing>"}}"""
LEVEL_SCHEMA = {"type": "object", "properties": {
    "first": {"type": "integer"}, "second": {"type": "integer"},
    "first_name": {"type": "string", "maxLength": 80}, "second_name": {"type": "string", "maxLength": 80}},
    "required": ["first", "second", "first_name", "second_name"]}
LEVEL_CONF = 0.6


def _level_value(text: str) -> float | None:
    m = _LEVEL.search(text)
    if not m:
        return None
    t = m.group(1).replace(" ", "").replace(",", ".").replace("±", "")
    return float(t)


def _level_diff_answer(img_path, question, tl, items, texts, backend, solver) -> Answer | None:
    if not _LEVEL_DIFF_Q.search(question):
        return None
    levels = [(k, i, _level_value(i.text)) for k, i in enumerate(items) if _level_value(i.text) is not None]
    if len({v for _, _, v in levels}) < 2:
        return None
    lv = "\n".join(f"[{k}] {i.text[:60]}" for k, i, _ in levels)
    r = llm.chat(LEVEL_PROMPT.format(levels=lv, texts=texts, question=question.strip()),
                 images=[str(T._abspath(img_path))], backend=backend, max_tokens=200, json_schema=LEVEL_SCHEMA,
                 cache=True)
    try:
        d = json.loads(r.text)
    except ValueError:
        m = re.search(r"\{.*\}", r.text, re.S)
        d = json.loads(m.group()) if m else {}
    by = {k: (i, v) for k, i, v in levels}
    a, b = by.get(d.get("first")), by.get(d.get("second"))
    if a is None or b is None or a[1] == b[1]:
        return None  # the reading did not name two different marks: the general path answers
    diff = round(a[1] - b[1], 3)
    lower = bool(_LOWER_W.search(question))
    val = round(-diff if lower else diff, 3)
    fn, sn = str(d.get("first_name", "first"))[:60], str(d.get("second_name", "second"))[:60]
    trace = [f"level difference: {fn} at '{a[0].text}' vs {sn} at '{b[0].text}' -> "
             f"{a[1]:g} - {b[1]:g} = {diff:g}" + (" (asked how much LOWER: sign flipped)" if lower else "")]
    ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in i.box], bbox_space="image_px",
                  note=f"level mark '{i.text[:40]}' ({n})") for i, n in ((a[0], fn), (b[0], sn))]
    asm = [f"levels as printed on the drawing ({fn} {a[1]:g}, {sn} {b[1]:g}); the difference is computed, in the "
           "levels' own unit (metres on metric plans)"]
    miss = [] if val > 0 else [f"the difference is {val:g}: the first thing is not {'lower' if lower else 'higher'}"]
    return Answer(value=abs(val) if val > 0 else val, unit="m", confidence=LEVEL_CONF if val > 0 else 0.3,
                  evidence=ev, solver=solver, trace=trace, assumptions=asm, missing=miss)


# X1 wild ks51 (1951 German scan, no scale): rooms are sized by the dimension CHAINS along the sheet's margins
# ('3,50' above the Wohnzimmer, '3695' = 3,69^5 beside it): area = the horizontal chain segment over the room x the
# vertical one beside it. Chain texts = numbers in the outer 15% bands; the segment nearest the room's label is taken.
_CHAIN_NUM = re.compile(r"^\s*-?\s*(\d{1,2},\d{2}|\d{3,4})\s*$")
CHAIN_CONF = 0.45


def _chain_value(text: str) -> float | None:
    m = _CHAIN_NUM.match(text)
    if not m:
        return None
    t = m.group(1)
    if "," in t:
        v = float(t.replace(",", "."))
    else:
        n = int(t)
        if len(t) == 4 and 1700 <= n <= 2099:
            return None  # a year in a title block, not a chain segment
        v = n / 100 if len(t) == 3 or n / 100 <= 12 else n / 1000  # 350 cm; 1104 cm; 3695 = 3,69^5 m
    return v if 0.8 <= v <= 12 else None


def _chain_room_answer(img_path: str, question: str, tl: T.TextLayer, solver: str) -> Answer | None:
    from stoa.solvers.raster import measure as M

    W, H = tl.size
    chains = [(i, _chain_value(i.text)) for i in tl.items]
    chains = [(i, v) for i, v in chains if v is not None]
    hor = [(i, v) for i, v in chains if i.cy < 0.15 * H or i.cy > 0.85 * H]  # top / bottom chains: room widths
    ver = [(i, v) for i, v in chains if (i.cx < 0.12 * W or i.cx > 0.88 * W) and (i, v) not in hor]
    if len(hor) + len(ver) < 4 or any(re.search(r"\bm(?:²|2)\b", i.text) for i in tl.items):
        return None  # not a sheet dimensioned by margin chains (or one that prints room areas: the VLM reads those)
    lab = M.find_label(tl, question) or M.find_label(tl, question, loose=True)
    via_class = None
    if lab is None:  # 'How wide is the kitchen?' on a plan that prints 'KÜCHE': the one label of the asked room class
        from stoa.solvers.raster.classes import label_class, question_class

        qc = question_class(question)
        hits = [i for i in tl.items if qc and label_class(i.text) == qc]
        lab, via_class = (hits[0], qc) if len(hits) == 1 else (None, None)
    if lab is None:
        return None

    def nearest(cands, d, lim):
        best = min(cands, key=d, default=None)
        return best if best is not None and d(best) < lim else None

    # the chain on the room's own side first (a right-hand chain sizes the right-hand rooms); widths may fall back to
    # the opposite chain (top and bottom chains usually split the same walls), depths never do
    top = lab.cy < H / 2
    dx, dy = (lambda t: abs(t[0].cx - lab.cx)), (lambda t: abs(t[0].cy - lab.cy))
    hx = nearest([t for t in hor if (t[0].cy < H / 2) == top], dx, 0.06 * W) or \
        nearest([t for t in hor if (t[0].cy < H / 2) != top], dx, 0.06 * W)
    vy = nearest([t for t in ver if (t[0].cx < W / 2) == (lab.cx < W / 2)], dy, 0.08 * H)
    area_q = bool(_AREA_Q.search(question) or _BIG_Q.search(question))
    wid_q = bool(_WIDTH_Q.search(question))
    if area_q and (hx is None or vy is None) or not area_q and (hx if wid_q else (hx and vy)) is None:
        return None
    ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in lab.box], bbox_space="image_px",
                   note=f"room label '{lab.text}'")]
    used = [t for t in (hx, vy) if t is not None]
    ev += [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in i.box], bbox_space="image_px",
                    note=f"dimension chain '{i.text}' = {v:g} m") for i, v in used]
    def side(t):
        return "top" if t[0].cy < H / 2 else "bottom"
    def flank(t):
        return "left" if t[0].cx < W / 2 else "right"
    if area_q:
        val = round(hx[1] * vy[1], 3)
        how = (f"{hx[1]:g} m ({side(hx)} chain '{hx[0].text}', over the room) × {vy[1]:g} m ({flank(vy)} chain "
               f"'{vy[0].text}', beside it) = {val:g} m²")
        unit = "m2"
    elif wid_q:
        val, unit = hx[1], "m"
        how = f"width across the drawing = the {side(hx)} chain segment over the room: '{hx[0].text}' = {val:g} m"
    else:
        val, unit = max(hx[1], vy[1]), "m"
        how = f"longer side of {hx[1]:g} m ({side(hx)} chain) and {vy[1]:g} m ({flank(vy)} chain)"
    return Answer(value=val, unit=unit, confidence=CHAIN_CONF, evidence=ev, solver=solver,
                  trace=[f"room '{lab.text}' sized by the margin dimension chains: {how}"],
                  assumptions=["the room spans exactly the chain segment printed nearest its label (one segment per "
                               "side); a rectangle", "chain numbers: 'd,dd' = m, 3-digit = cm, '3695' = 3,69⁵ m"]
                  + ([f"the {via_class} = the only room labelled '{lab.text}'"] if via_class else []),
                  missing=["the chain segments' tick marks are not checked against the room's walls"])


# X1 Stoa-like exam (Stoa's own 10200 px piping plan): 'How many pressure gauges (PG) are tagged?' -> 12 and 'How many
# different pipe support types (PS-...) are called out?' -> 13 although the text layer holds 20 'PG' and all 23 PS-n
# codes: the VLM reads a 160-line selection of 407. A tag named in the question is counted over the WHOLE layer.
_TAG_Q = re.compile(r"\(\s*([A-Z]{1,5})(?:\s*-\s*(?:\.\.\.|…|n|#|x|\d+)?)?\s*\)")
_DISTINCT_Q = re.compile(r"\b(different|distinct|unique|types?|kinds?|verschiedene\w*|eri\w*|olika)\b", re.I)
_NOT_TAGS = {"KG", "EG", "OG", "UG", "DG", "SG", "ZG"}  # storeys named in parentheses ('auf dem Erdgeschoss (EG)')
TAG_CONF = 0.6


def _tag_count_answer(img_path: str, question: str, tl: T.TextLayer, solver: str) -> Answer | None:
    from stoa.solvers.raster.classes import label_class

    if not _HOW_MANY.search(question):
        return None
    m = _TAG_Q.search(question)
    if m is None:
        return None
    ab = m.group(1)
    if len(ab) < 2:
        return None
    num = re.compile(rf"(?<![A-Z0-9]){ab}\s*-\s*(\d{{1,3}}[A-Z]?)\b")
    bare = re.compile(rf"(?<![A-Z0-9-]){ab}(?![A-Z0-9]|\s*-\s*\d|\s*[=:])")
    codes, bares = [], []
    for i in tl.items:
        if len(i.text) > 30 or "=" in i.text:  # a legend / note line defines the tag, it is not one
            continue
        codes += [(c, i) for c in num.findall(i.text)]
        bares += [i] * len(bare.findall(i.text))
    if len(codes) + len(bares) < 2:
        return None
    if (ab in _NOT_TAGS or label_class(ab) is not None) and len(codes) < 2:
        return None  # a room code (MH, WC, KH) or a storey (EG) without numbered tags: the room / label counters answer
    distinct = sorted({c for c, _ in codes}, key=lambda c: (int(re.match(r"\d+", c).group()), c))
    if _DISTINCT_Q.search(question):
        if not distinct:
            return None
        val, how = len(distinct), f"{len(distinct)} different {ab}-n codes: {', '.join(ab + '-' + c for c in distinct[:30])}"
        ev_items = [next(i for c2, i in codes if c2 == c) for c in distinct]
    else:
        repeated = len(distinct) < len(codes)  # numbered codes that repeat are TYPES (PS-8 x5); unique ones are IDs
        n_num = len(codes) if repeated else len(distinct)
        val = n_num + len(bares)
        how = (f"{len(bares)} '{ab}' tags" if bares else "") + (" + " if bares and codes else "") + \
              (f"{n_num} numbered {ab}-n tags" + (" (codes repeat: each tag is one item)" if repeated else
                                                  f" ({', '.join(ab + '-' + c for c in distinct[:12])})") if codes else "")
        ev_items = bares + [i for _, i in codes]
    ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in i.box], bbox_space="image_px",
                   note=f"tag '{i.text[:30]}'") for i in ev_items[:80]]
    return Answer(value=val, unit="count", confidence=TAG_CONF, solver=solver, evidence=ev,
                  trace=[f"tag count over the whole text layer ({len(tl.items)} texts): {how}"],
                  assumptions=[f"'{ab}' as named in the question = the drawing's tag; each printed tag is one item; legend "
                               "and note lines (with '=' or longer than 30 characters) are not counted"],
                  missing=["tags the text reading missed (small or overlapping text) are not counted"])


_TOTAL_LINE = re.compile(r"\b(GIFA|GIA|NIA|GEA|gross|overall|total|sum|subtotal|BGF|NGF|BTA|NTA|HTA|"
                         r"gesamt\w*|insgesamt|yhteens[äa]|kerrosala|bruttoala|totalt|summa)\b", re.I)
_HOW_MANY = re.compile(r"\b(how many|number of|wie viele|anzahl|combien|quanti|quante|kuinka monta|hur många|cuántos|"
                       r"cuántas|hoeveel)\b", re.I)
LABEL_COUNT_CONF = 0.7  # = G1's label_count on vector PDFs (a room may be unlabelled)


def _label_count(value, question: str, cited: list) -> str | None:
    """'How many W.C.s …?' answered N with N cited lines that are all the same label (letters only: 'W.C.' = 'W.C:')
    -> the assumption line, else None."""
    if not _HOW_MANY.search(question) or not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    if len(cited) != value or len({id(i) for i in cited}) != value:
        return None
    keys = {re.sub(r"[^a-zäöüåéèàç]", "", i.text.lower()) for i in cited}
    if len(keys) != 1 or not next(iter(keys)) or re.search(r"\d", "".join(i.text for i in cited)):
        return None
    return (f"count = the {value} labels '{cited[0].text}' printed on the sheet (one per room, boxes shown); a room "
            "drawn without its label is not counted")


_WIDTH_Q = re.compile(r"\b(wide|width|breit\w*|largh\w*|largo|larga|largeur|large|leve\w*|bredd|ancho|anchura)\b", re.I)
_HEIGHT_Q = re.compile(r"\b(tall|high|height|hoch|h[öo]he|alt[aoe]|altezza|hauteur|haut|korke\w*|h[öo]jd|h[öo]g)\b", re.I)
_DOOR_Q = re.compile(r"\b(doors?|doorways?|t[üu]r(?:en)?|porta|porte|puertas?|ov[ie]\w*|d[öo]rr\w*)\b", re.I)
_OPENING_Q = re.compile(_DOOR_Q.pattern[:-3] + r"|windows?|fenster\w*|finestr\w*|ikkun\w*|fen[êe]tres?|ventan\w*|"
                        r"f[öo]nst\w*|openings?|[öo]ffnung\w*|apertur\w*)\b", re.I)
_WH_LINE = re.compile(r"^\s*(\d+[.,]\d+)\s*[/x×]\s*(\d+[.,]\d+)\s*$")
_DEC = re.compile(r"^\s*(\d+[.,]\d+)\s*$")


def _opening_pair(value, question: str, cited: list, tl: T.TextLayer):
    """X1 wild esino: opening labels print WIDTH over HEIGHT ('0.90' over '2.10', or '0.90/2.10'), and the VLM took the
    wrong half ('How wide is the door…' -> 2.10). For a width / height question about a door / window / opening whose
    cited number is one half of such a pair: a door's height is the half >= 1.8 m (order-free); otherwise the upper
    line of a horizontal stack (or the first number of 'a/b') is the width. -> (value, partner item, note) or None."""
    if not isinstance(value, (int, float)) or not _OPENING_Q.search(question):
        return None
    wq, hq = bool(_WIDTH_Q.search(question)), bool(_HEIGHT_Q.search(question))
    if wq == hq:
        return None
    door = bool(_DOOR_Q.search(question))
    num = lambda t: float(t.replace(",", "."))  # noqa: E731
    votes = []
    for i in cited:
        m = _WH_LINE.match(i.text)
        if m:
            pair, partner, ordered = (num(m.group(1)), num(m.group(2))), None, True
        else:
            mi = _DEC.match(i.text)
            if not mi:
                continue
            size = max(i.box[2] - i.box[0], i.box[3] - i.box[1], 6.0)
            near = [(abs(j.cx - i.cx) + abs(j.cy - i.cy), j) for j in tl.items
                    if j is not i and _DEC.match(j.text) and abs(j.cx - i.cx) + abs(j.cy - i.cy) < 1.2 * size]
            if not near:
                continue
            partner = min(near, key=lambda t: t[0])[1]
            top, bot = sorted((i, partner), key=lambda t: t.cy)
            horiz = all(t.box[2] - t.box[0] > t.box[3] - t.box[1] for t in (i, partner))
            ordered = horiz and abs(top.cy - bot.cy) > abs(top.cx - bot.cx)
            pair = (num(_DEC.match(top.text).group(1)), num(_DEC.match(bot.text).group(1)))
        lo, hi = sorted(pair)
        if door and 1.8 <= hi <= 2.8 and 0.5 <= lo <= 1.6:
            votes.append((lo if wq else hi, partner, "a door's height is the half >= 1.8 m"))
        elif ordered and not door or (ordered and door and lo == hi):
            votes.append((pair[0] if wq else pair[1], partner, "the upper / first number is the width"))
    if not votes:
        return None
    v, partner, why = votes[0]
    other = partner.text if partner is not None else ""
    what = "width" if wq else "height"
    tail = f"the {what} is {v:g}" + ("" if abs(v - float(value)) < 1e-9 else f", not {value:g}")
    return (v, partner, f"opening label = width over height ({why}; '{cited[0].text}'"
            + (f" with '{other}'" if other else "") + f"): {tail}")


def _cm_chain(value, unit, question: str, cited: list, tl: T.TextLayer) -> str | None:
    """X1 wild exam (Italian 'scala 1:100' sheet): '1104' across a hall read as 1104 mm; it is 1104 cm = 11.04 m.
    European sheets that print opening sizes in metres ('2.50 / 3.00', '0.90') dimension the building with integer
    chains in CENTIMETRES; a mm sheet would print '900', never '0.90'. So: a length question answered by a printed
    3-4 digit integer (unit none/mm/cm) on a sheet with >= 3 decimal-metre values and no 'mm' -> metres = value/100."""
    if not isinstance(value, (int, float)) or unit not in (None, "", "mm", "cm", "count") or not _LEN_Q.search(question):
        return None
    if not (100 <= float(value) < 10000) or float(value) != int(value):
        return None
    if not any(re.fullmatch(r"\s*[-–]?\s*" + str(int(value)) + r"\s*", i.text) for i in cited):
        return None
    text = tl.text()
    if len(_DEC_M.findall(text)) < 3 or re.search(r"\bmm\b|millimet", text, re.I):
        return None
    return (f"the dimension string {int(value)} is read in centimetres ({int(value) / 100:g} m): the sheet prints "
            "opening sizes in metres (e.g. 0.90 / 2.10) and its integer dimension chains in cm")


_NUM_UNIT = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(m²|m2|sq\.? ?ft|ft²|ft2|m|ft|cm|mm)\.?\s*$", re.I)
# X1 (3 rows on 3 files: ks51 x1-127/128, cadss x1-151): an AREA question answered with two lengths ('12.515m x
# 4.015m', '3,50 x 3,00', "12'-6\" x 14'") came back as a string. Two sides of a room or of a building multiply to its
# area when it is a rectangle: say so, and keep the printed sides as the evidence.
_SIDE = r"(\d{1,5}(?:[.,]\d{1,3})?)\s*(mm|cm|m|ft|')?"
_TWO_LEN = re.compile(rf"^\s*{_SIDE}\s*[x×X*]\s*{_SIDE}\s*$")
_TWO_FTIN = re.compile(r"^\s*(\d{1,3})\s*['’]\s*-?\s*(\d{1,2})?\s*[\"”]?\s*[x×X*]\s*(\d{1,3})\s*['’]\s*-?\s*(\d{1,2})?\s*[\"”]?\s*$")
TWO_LEN_CONF = {"cited": 0.5, "uncited": 0.3}


def two_lengths(val: str) -> tuple[float, float, str] | None:
    """'3,50 x 3,00' / '12.515m x 4.015m' / '350 x 300 cm' / "12'-6\" x 14'" -> (a, b, 'm' | 'ft'), else None."""
    m = _TWO_FTIN.match(val)
    if m:
        a = int(m.group(1)) + int(m.group(2) or 0) / 12
        b = int(m.group(3)) + int(m.group(4) or 0) / 12
        return (a, b, "ft") if a > 0 and b > 0 else None
    m = _TWO_LEN.match(val)
    if not m:
        return None
    a, ua, b, ub = float(m.group(1).replace(",", ".")), m.group(2), float(m.group(3).replace(",", ".")), m.group(4)
    u = (ua or ub or "").lower()
    if u in ("ft", "'"):
        return (a, b, "ft") if a > 0 and b > 0 else None
    if not u:  # bare numbers: decimals are metres; 3-digit integers centimetres, 4-digit millimetres
        u = "m" if max(a, b) < 100 else "cm" if max(a, b) < 1000 else "mm"
    f = {"m": 1.0, "cm": 0.01, "mm": 0.001}[u]
    a, b = a * f, b * f
    return (a, b, "m") if 0.3 <= min(a, b) and max(a, b) <= 300 else None


_UNIT_OF = {"m²": "m2", "m2": "m2", "sqft": "ft2", "sq.ft": "ft2", "ft²": "ft2", "ft2": "ft2", "m": "m", "ft": "ft",
            "cm": "cm", "mm": "mm"}
_AGG_Q = re.compile(r"\b(total|combined|sum|together|altogether|in all|both|all|largest|smallest|biggest|widest|"
                    r"longest|most|least|yhteensä|yhteenlaskettu|kaikkien|kaikki|suurin|pienin|gesamt|insgesamt|"
                    r"zusammen|größte|grösste|kleinste|somme|ensemble|plus grand|plus petit|totale|complessiv\w*|"
                    r"più grande|più piccol\w*|totalt|sammanlagt|störst|minst)\b", re.I)

# VLM-box measurements: Swiss PNG rooms (experiments/w3/measure_eval.py, 22 Qs) 0 within 2%, 5 within 20%, median
# error 47%: an estimate whose box a reader can check, not a measurement
MEASURED_CONF = 0.05
_AREA_Q = re.compile(r"\b(areas?|floor space|square (?:feet|foot|meters?|metres?)|sq\.? ?ft|m²|m2|ft²|pinta-ala|fläche|"
                     r"flache|surface|superficie|yta|area)\b", re.I)


_DIMS_Q = re.compile(r"\b(dimensions?|abmessungen|ma(?:ss|ß)e|mitat|dimensioni|dimensiones|mått)\b", re.I)
_LONG_Q = re.compile(r"\b(length|long|länge|lang|pituus|pitkä|longueur|long|lunghezza|lungo|längd|lång)\b", re.I)
_WIDE_Q = re.compile(r"\b(width|wide|breite|breit|leveys|leveä|largeur|large|larghezza|largo|bredd|bred)\b", re.I)
# experiments/o1/room_dims_eval.py --solid (404 Swiss PNG rooms with a scale bar, gold = the DXF room polygon's
# minimum-area rectangle; nothing tuned on it): length within 2% 271/404 (67%), width 223/404 (55%); outlines that are
# rectangles (fill >= 0.9, 258 rooms) 74% / 62%. -> P(within 2%) as confidence
DIMS_CONF = {"length": 0.6, "width": 0.5, "dims": 0.4, "not_rect": 0.3}


def _room_label(tl: T.TextLayer, question: str):
    """(label item, loose) for the room the question names, or (None, False). A loosely matched (unquoted) name with
    numbers printed next to it is skipped: its printed dimensions are the better source (the VLM reads them)."""
    from stoa.solvers.raster import measure as M

    lab = M.find_label(tl, question)
    if lab is not None:
        return lab, False
    lab = M.find_label(tl, question, loose=True)
    if lab is None:
        return None, False
    th = min(lab.box[3] - lab.box[1], lab.box[2] - lab.box[0])
    if any(re.search(r"\d", i.text) and i is not lab and abs(i.cx - lab.cx) < 8 * th and abs(i.cy - lab.cy) < 8 * th
           for i in tl.items):
        return None, False
    return lab, True


def _room_dims_answer(img_path: str, question: str, tl: T.TextLayer, bars: list, solver: str):
    """'How long / wide is room X?', 'dimensions of the Salón': the room's measured outline (measure.room_area, solid
    walls first) -> minimum-area rectangle x the scale. Evidence = the outline (polygon) + the measured side
    (polyline). None when the room, a metric scale or a closed outline is missing (the VLM path answers)."""
    from stoa.solvers.raster import measure as M

    bar = next((b for b in bars if b.px_per_m), None)
    if bar is None or _AREA_Q.search(question):
        return None
    kind = "dims" if _DIMS_Q.search(question) else "width" if _WIDE_Q.search(question) else \
        "length" if _LONG_Q.search(question) else None
    if kind is None:
        return None
    lab, loose = _room_label(tl, question)
    if lab is None:
        return None
    ids = [i for i in tl.items if M._ROOM_ID.fullmatch(i.text.strip())]
    others = [i.box for i in ids if i is not lab]
    if loose:
        others += [i.box for i in tl.items if i is not lab and M._NAME.fullmatch(T.norm(i.text))
                   and T.norm(i.text) not in M._GENERIC]
    r = M.room_area(img_path, lab.box, bar.px_per_m, **M.FILL, others=others, solid=True)
    if r is None or r["wall_frac"] < 0.05 or r["m2"] > 120:
        return None
    d = M.room_dims(r, bar.px_per_m)
    if d is None:
        return None
    rect = d["fill"] >= 0.9
    conf = DIMS_CONF[kind] if rect else DIMS_CONF["not_rect"]
    src = SB.scale_source(bar)
    if kind == "length":
        value, unit, lines = d["length_m"], "m", [("long side", d["long_side"], d["length_m"])]
    elif kind == "width":
        value, unit, lines = d["width_m"], "m", [("short side", d["short_side"], d["width_m"])]
    else:
        value, unit = f"{d['length_m']:.2f} m × {d['width_m']:.2f} m", None
        lines = [("long side", d["long_side"], d["length_m"]), ("short side", d["short_side"], d["width_m"])]
    ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in lab.box], bbox_space="image_px",
                   note=f"room label '{lab.text}'"),
          Evidence(file=img_path, page=0, bbox=r["box"], bbox_space="image_px", polygon=r.get("polygon"), exact=False,
                   note=f"room outline: {d['length_m']:.2f} m × {d['width_m']:.2f} m (rectangle fitted to it) at 1 m = "
                        f"{bar.px_per_m:.1f} px from {src}")]
    for name, pl, v in lines:
        xs, ys = [p[0] for p in pl], [p[1] for p in pl]
        ev.append(Evidence(file=img_path, page=0, bbox=[min(xs), min(ys), max(xs), max(ys)], bbox_space="image_px",
                           polyline=pl, exact=False, note=f"{v:.2f} m: the room's {name} (measured)"))
    ev += bar.evidence(img_path)
    return Answer(value=value, unit=unit, confidence=conf, solver=solver, evidence=ev,
                  trace=[f"room label '{lab.text}' at {[round(x) for x in lab.box]}", bar.describe(), *r["notes"],
                         f"min-area rectangle {d['length_m']} x {d['width_m']} m, outline fills {100 * d['fill']:.0f}% of it"],
                  assumptions=[f"measured on the drawing: the room's outline up to the walls, a rectangle fitted to it, "
                               f"at 1 m = {bar.px_per_m:.2f} px ({src}); no printed dimension was used"]
                  + ([] if rect else [f"the room is not a rectangle (its outline fills {100 * d['fill']:.0f}% of the "
                                      "fitted rectangle): the values are its bounding rectangle"]),
                  missing=["a printed dimension string: on 404 Swiss plan rooms this measurement was within 2% for 67% "
                           "of lengths and 55% of widths"])


def _room_area_answer(img_path: str, question: str, tl: T.TextLayer, bars: list, solver: str):
    """'Area of room 2.32 / «Bagno»': the label in the text layer -> a printed area next to it (read), else the room's
    flood-filled outline x the scale bar (measure.py). No VLM call. None when the room or a metric bar is missing."""
    from stoa.solvers.raster import measure as M

    lab = M.find_label(tl, question)
    bar = next((b for b in bars if b.px_per_m), None)
    loose = False
    if lab is None and bar is not None:
        lab, loose = M.find_label(tl, question, loose=True), True
    if lab is None or bar is None:
        return None
    h = lab.box[3] - lab.box[1]
    th = min(h, lab.box[2] - lab.box[0])  # text height, also for labels printed sideways
    if loose and any(re.search(r"\d", i.text) and i is not lab and abs(i.cx - lab.cx) < 8 * th
                     and abs(i.cy - lab.cy) < 8 * th for i in tl.items):
        # an unquoted name with numbers printed next to it (dimensions '12'-0" x 14'-6"', a printed area in a format
        # T.areas does not parse): the VLM reading of the print is the better source than a flood fill
        return None

    def d2(a, it):
        return ((a["box"][0] + a["box"][2]) / 2 - it.cx) ** 2 + ((a["box"][1] + a["box"][3]) / 2 - it.cy) ** 2

    # a printed area belongs to the room whose number label is NEAREST to it (Swiss renders print 'NN.NN m²' under
    # the number; a neighbour's area can be as close vertically)
    ids = [i for i in tl.items if M._ROOM_ID.fullmatch(i.text.strip())] or [lab]
    near = [a for a in T.areas(tl) if d2(a, lab) < (6 * h) ** 2 and min(ids, key=lambda i: d2(a, i)) is lab]
    lab_ev = Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in lab.box], bbox_space="image_px",
                      note=f"room label '{lab.text}'")
    if near:
        a = min(near, key=lambda a: d2(a, lab))
        return Answer(value=a["value"], unit=a["unit"], confidence=0.75, solver=solver,
                      evidence=[lab_ev, Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in a["box"]],
                                                 bbox_space="image_px", note=f"printed area '{a['line'][:40]}'")],
                      trace=[f"room label '{lab.text}' at {lab.box}; printed area next to it: '{a['line']}'"])
    others = [i.box for i in ids if i is not lab and M._ROOM_ID.fullmatch(i.text.strip())]
    if loose:  # other printed room NAMES split open regions / veto a merged solid-wall region too
        others += [i.box for i in tl.items if i is not lab and M._NAME.fullmatch(T.norm(i.text))
                   and T.norm(i.text) not in M._GENERIC]
    r = M.room_area(img_path, lab.box, bar.px_per_m, **M.FILL, others=others, solid=True)
    if r is None or r["wall_frac"] < 0.05 or r["m2"] > 120:
        # not bounded by drawn walls (thin-line historical plans leak: HABS ut0011 'KITCHEN' filled ~100 m², 0% of its
        # outline on walls; Swiss rooms: min 0.01, 5/71 below 0.2) or larger than any single room: no measurement
        return None
    unit = "ft2" if re.search(r"\b(sq\.? ?ft|square feet|ft²|feet)\b", question, re.I) else "m2"
    v = r["m2"] / 0.3048 ** 2 if unit == "ft2" else r["m2"]
    W = M.WITHIN
    return Answer(value=round(v, 2), unit=unit, confidence=M.CONF, solver=solver,
                  evidence=[lab_ev, Evidence(file=img_path, page=0, bbox=r["box"], bbox_space="image_px",
                                             polygon=r.get("polygon"), exact=False,
                                             note=f"room outline ({'solid walls' if r['notes'][0].startswith('walls-only') else 'flood fill'}): {r['m2']:.2f} m² at 1 m = "
                                                  f"{bar.px_per_m:.1f} px from {SB.scale_source(bar)}")]
                  + bar.evidence(img_path),
                  trace=[f"room label '{lab.text}' at {[round(x) for x in lab.box]}", bar.describe(), *r["notes"]],
                  assumptions=[f"the room's outline is the blank region around its label up to the drawn walls "
                               f"(convex hull, so fixtures against walls count), measured with {SB.scale_source(bar)} "
                               f"({bar.px_per_m:.2f} px per m); no area is printed for this room"],
                  missing=[f"a printed area or dimensions: on 23 held-out Swiss plan rooms this measurement was within 2% for "
                           f"{100 * W['2%']:.0f}%, within 5% for {100 * W['5%']:.0f}% and within 10% for "
                           f"{100 * W['10%']:.0f}%"])


# experiments/w3/apartment_eval.py (99 'net floor area of apartment X' Qs, 52 Swiss PNG plans: 37 dev + 15 train,
# nothing tuned on them; gold = DXF room sums; artifacts/W3/apartment_eval_hybrid.jsonl): within 2% when every room
# in the unit has a printed area 19/20; 1-2 rooms flood-filled 6/12; all rooms filled (plans without printed areas)
# 18/56 (within 5% 38/56); some room not measurable 2/8.
# + floor totals ('all numbered indoor rooms', 51 Qs) and 'all rooms labelled X' (48 Qs) on the same plans
# (artifacts/W3/area_sums_eval.jsonl), within 2% by (scope, kind): floor printed 6/6, some 5/6, filled 2/25, partial 1/14;
# labelled printed 11/17 (a room whose name was not read drops out), filled 18/31.
UNIT_CONF = {"printed": 0.85, "some": 0.5, "filled": 0.3, "partial": 0.2}
SUM_CONF = {"unit": UNIT_CONF, "floor": {"printed": 0.85, "some": 0.6, "filled": 0.1, "partial": 0.1},
            "labelled": {"printed": 0.75, "some": 0.6, "filled": 0.6, "partial": 0.15}}
# a gap in the room numbering (labels the text layer missed) makes a sum 'partial': floor 0/10, labelled 1/8, unit 2/6
# within 2% with a gap; without: labelled printed 10/12, filled 18/28 (run 20261114-084758-...-75fe)


_SUM_W = re.compile(r"\b(combined|total|together|sum|both|altogether|in all|plus|zusammen|insgesamt|gesamt\w*|"
                    r"yhteens[äa]|yhteenlaske\w*|yhdess[äa]|tillsammans|totalt|ensemble|au total|insieme|in totale|"
                    r"juntos|en total|samen)\b", re.I)
_PLURAL_W = re.compile(r"\b(two|three|four|both|all|each|the \w+s|beiden|alle|zwei|drei|molemmat|kaikki|kaksi|"
                       r"b[åa]da|alla|deux|tous|toutes|entrambi|tutti|tutte|ambos|todos)\b", re.I)
ROOMS_SUM_CONF = {"printed": 0.7, "measured": 0.4}


def _rooms_sum_answer(img_path: str, question: str, tl: T.TextLayer, bars: list, solver: str) -> Answer | None:
    """'Combined floor area of the kitchen and the living room': each named room on its own (its printed area, else
    its outline at the scale, else its dimension chains), then the sum. Room names in any language (measure.CONCEPTS +
    classes.py). None unless EVERY named room is found and sized; a room type printed several times counts only when
    the question says so (plural / 'both' / 'all')."""
    from stoa.solvers.raster import measure as M

    cs = M.question_concepts(question)
    if len(cs) < 2 or not _SUM_W.search(question) or M.RELATIONAL.search(question):
        return None
    bar = next((b for b in bars if b.px_per_m), None)
    concept_all = [i for c in cs for i in M.concept_labels(tl, question, c)]
    ids = [i for i in tl.items if M._ROOM_ID.fullmatch(i.text.strip()) and all(i is not j for j in concept_all)]
    anchors = concept_all + ids
    parts, ev, how, worst = [], [], [], "printed"
    for c in cs:
        labs = M.concept_labels(tl, question, c)
        if not labs or len(labs) > 1 and not _PLURAL_W.search(question):
            return None
        for lab in labs:
            h = max(6.0, min(lab.box[3] - lab.box[1], lab.box[2] - lab.box[0]))

            def d2(a, it):
                return ((a["box"][0] + a["box"][2]) / 2 - it.cx) ** 2 + ((a["box"][1] + a["box"][3]) / 2 - it.cy) ** 2
            near = [a for a in T.areas(tl) if d2(a, lab) < (6 * h) ** 2 and min(anchors, key=lambda i: d2(a, i)) is lab]
            lab_ev = Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in lab.box], bbox_space="image_px",
                              note=f"room label '{lab.text}' ({c.replace('_', ' ')})")
            if near:
                a = min(near, key=lambda a: d2(a, lab))
                parts.append((a["value"], a["unit"]))
                ev += [lab_ev, Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in a["box"]], bbox_space="image_px",
                                        note=f"printed area '{a['line'][:40]}'")]
                how.append(f"{lab.text} {a['value']:g} {a['unit']} (printed)")
                continue
            got = None
            if bar is not None:
                r = M.room_area(img_path, lab.box, bar.px_per_m, **M.FILL,
                                others=[i.box for i in anchors if i is not lab], solid=True)
                if r is not None and r["wall_frac"] >= 0.05 and r["m2"] <= 120:
                    got = (round(r["m2"], 2), "m2")
                    ev += [lab_ev, Evidence(file=img_path, page=0, bbox=r["box"], bbox_space="image_px",
                                            polygon=r.get("polygon"), exact=False,
                                            note=f"room outline: {r['m2']:.2f} m² at 1 m = {bar.px_per_m:.1f} px from "
                                                 f"{SB.scale_source(bar)}")]
                    how.append(f"{lab.text} {r['m2']:.2f} m2 (outline × {SB.scale_source(bar)})")
            if got is None and len([i for i in tl.items if T.norm(i.text) == T.norm(lab.text)]) == 1:
                ca = _chain_room_answer(img_path, f"How big is '{lab.text}'?", tl, solver)
                if ca is not None and ca.unit == "m2":
                    got = (ca.value, "m2")
                    ev += ca.evidence
                    how.append(f"{lab.text} {ca.value:g} m2 (dimension chains)")
            if got is None:
                return None
            parts.append(got)
            worst = "measured"
    units = {u for _, u in parts}
    if len(units) != 1:
        return None
    total = round(sum(v for v, _ in parts), 3)
    return Answer(value=total, unit=units.pop(), confidence=ROOMS_SUM_CONF[worst], solver=solver, evidence=ev[:12],
                  trace=[f"sum of the named rooms: {' + '.join(how)} = {total:g}"],
                  assumptions=[f"the question's rooms = {', '.join(how)}; their areas are added"],
                  missing=[] if worst == "printed" else ["some rooms are measured, not printed (each term is an estimate)"])


def _sum_scope(question: str, tl: T.TextLayer) -> bool:
    """A summed-area question raster can answer from room labels: one unit's rooms, rooms labelled X, or the floor's
    rooms. A question that names a room number on this plan is a single-room question (room path)."""
    from stoa.solvers.raster import measure as M

    if A.unit_letter(question):
        return True
    ids = {i.text.strip().rstrip(".") for i in tl.items}
    if any(rid in ids for rid in M._ROOM_ID.findall(question)):
        return False
    return bool(A.labelled_name(question) or A._FLOOR_SUM.search(question))


def _unit_area_answer(img_path: str, question: str, tl: T.TextLayer, bars: list, solver: str):
    """'Net floor area of apartment B': the unit's drawn boundary around its label -> the rooms inside -> their
    printed areas (+ flood fill for rooms without one), outdoor rooms dropped (apartment.py). No VLM call."""
    from stoa.solvers.raster import measure as M

    bar = next((b for b in bars if b.px_per_m), None)
    r = A.room_set(img_path, tl, question, bar.px_per_m if bar else None)
    if r is None:
        return None
    ft = bool(re.search(r"\b(sq\.? ?ft|square feet|ft²|ft2)\b", question, re.I))
    k = 1 / 0.3048 ** 2 if ft else 1.0
    lab = r["label"]
    ev = []
    if lab is not None:
        ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in lab.box], bbox_space="image_px",
                       note=f"unit label '{lab.text}'"),
              Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in r["region_box"]], bbox_space="image_px",
                       note=f"apartment {r['letter']}: the region inside its drawn boundary line")]
    parts = []
    for rm in r["rooms"]:
        name = f"{rm['id'] or ''} {rm['name']}".strip() or "room"
        if rm["area"] is not None:
            parts.append(f"{name} {rm['area']['value']:g}")
            ev.append(Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in rm["area"]["box"]],
                               bbox_space="image_px", note=f"{name}: printed '{rm['area']['text']}'"))
        else:
            parts.append(f"{name} ~{rm['measured']['m2']:g} (measured)")
            ev.append(Evidence(file=img_path, page=0, bbox=rm["measured"]["box"], bbox_space="image_px",
                               polygon=rm["measured"].get("polygon"), exact=False,
                               note=f"{name}: no printed area; flood-filled outline {rm['measured']['m2']:.2f} m²"
                                    + (f" at 1 m = {rm['measured']['px_per_m']:.1f} px" if rm["measured"].get(
                                        "px_per_m") else "")))
    n, nm = len(r["rooms"]), len(r["measured"])
    kind = "partial" if r["unmeasured"] or r["gaps"] else "printed" if nm == 0 else "some" if nm <= 2 and nm < n \
        else "filled"
    conf = SUM_CONF[r["scope"] if not r["name"] else "labelled"][kind]
    if r["scope"] == "unit":
        assumptions = [f"apartment {r['letter']} = the area enclosed by the coloured dash-dot unit boundary around the "
                       f"label '{lab.text}'; its rooms are the room labels inside it ({n})"]
    elif r["name"]:
        assumptions = [f"the rooms labelled '{r['name']}' are the {n} room(s) whose printed name reads so"]
        unnamed = [x for x in A._rooms(tl) if x["label"] is not None and not x["name"]]
        if unnamed:
            missing_names = f"{len(unnamed)} numbered room(s) have no readable name; any of them labelled " \
                            f"'{r['name']}' is missing from the sum"
            assumptions.append(missing_names)
    else:
        assumptions = [f"the floor's rooms are its {n} room labels"
                       + (" with a room number" if A._NUMBERED_Q.search(question) else "")]
    if r["dropped"]:
        assumptions.append("left out as outdoor space: " + ", ".join(f"{d['id'] or ''} {d['name']}".strip()
                                                                    for d in r["dropped"]))
    missing = []
    if nm:
        W = M.WITHIN
        assumptions.append(f"{nm} room(s) have no printed area and were measured by flood fill with the scale bar "
                           f"({bar.px_per_m:.2f} px per m)")
        if r["scope"] == "floor" and not r["name"]:
            missing.append("whole-floor sums of flood-filled rooms run ~5% low (median) and were within 2% for only "
                           "2 of 25 test floors")
        missing.append(f"printed areas for {nm} room(s): a flood-filled room is within 5% for {100 * W['5%']:.0f}% of "
                       f"held-out rooms; whole units with filled rooms were within 5% for 38 of 56 (all filled)")
    if r["gaps"]:
        missing.append(f"room number(s) {', '.join(r['gaps'][:8])} were not read (a gap in the numbering): if they "
                       "belong to the asked rooms, the sum is too small by their areas")
    if r["unmeasured"]:
        missing.append("no area for " + ", ".join(u["id"] or "?" for u in r["unmeasured"])
                       + " (neither printed nor measurable): the sum is too small by their areas")
    if bar and bar.notes:
        assumptions.append("scale: " + bar.notes[-1])
    total = round(r["value"] * k, 2)
    return Answer(value=total, unit="ft2" if ft else "m2", confidence=conf, solver=solver, evidence=ev[:14],
                  trace=([f"apartment {r['letter']}: label '{lab.text}' at {[round(x) for x in lab.box]}; region "
                          f"inside the unit boundary {[round(x) for x in r['region_box']]} ({r['region_m2']} m² inside "
                          "the line)"] if lab is not None else [f"scope: {r['scope']}"
                                                                + (f", rooms labelled '{r['name']}'" if r['name'] else "")])
                  + ["rooms: " + " + ".join(parts) + f" = {r['value']:g} m²"],
                  assumptions=assumptions, missing=missing)


def _compare_units(img_path: str, question: str, tl: T.TextLayer, bars: list, cmp: tuple, solver: str):
    """'Which apartment is the largest?' / 'Is apartment A larger than B?': each unit's net area (room_set), then the
    comparison. Confidence: the least sure unit's, lowered when the two leading units are within 5% of each other."""
    letters, how = cmp
    if not bars:  # 'larger' is not a size word for _SIZE: get the scale bar here (rooms without a printed area)
        try:
            bars = [b for b in SB.bars(img_path, layer=tl) if b.unit in SB.UNIT_M]
        except Exception:  # noqa: BLE001
            bars = []
    bar = next((b for b in bars if b.px_per_m), None)
    res = {}
    for L in letters:
        r = A.room_set(img_path, tl, f"net floor area of apartment {L} except balconies, loggias, terraces", 
                       bar.px_per_m if bar else None)
        if r is None:
            return None
        res[L] = r
    order = sorted(res, key=lambda L: res[L]["value"], reverse=(how == "max"))
    best, second = order[0], order[1]
    a, b = res[best]["value"], res[second]["value"]
    margin = abs(a - b) / max(a, b)

    def kind(r):
        n, nm = len(r["rooms"]), len(r["measured"])
        return "partial" if r["unmeasured"] or r["gaps"] else "printed" if nm == 0 else "some" if nm <= 2 and nm < n \
            else "filled"
    conf = min(UNIT_CONF[kind(r)] for r in res.values())
    # a sum is within 5% for 69/99 units: two units closer than that can swap
    conf = round(conf if margin >= 0.1 else min(conf, 0.5) if margin >= 0.05 else min(conf, 0.3), 2)
    yes_no = re.match(r"\s*(is|ist|est|è|onko|är|does|has|hat)\b", question, re.I) and len(letters) == 2
    if yes_no:  # 'Is apartment A larger than apartment B?': the first named unit vs the second
        first = letters[0]
        value = "yes" if first == best else "no"
    else:
        value = best
    ev = []
    for L in letters:
        r = res[L]
        ev += [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in r["label"].box], bbox_space="image_px",
                        note=f"unit label '{r['label'].text}': {r['value']:g} m²"),
               Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in r["region_box"]], bbox_space="image_px",
                        note=f"apartment {L}: the region inside its drawn boundary ({len(r['rooms'])} rooms)")]
    sizes = ", ".join(f"{L} {res[L]['value']:g} m²" + (f" ({len(res[L]['measured'])} room(s) flood-filled)"
                                                         if res[L]["measured"] else "") for L in order)
    return Answer(value=value, unit=None, confidence=conf, solver=solver, evidence=ev[:14],
                  trace=[f"compare units {letters} ({how}): {sizes}; margin {100 * margin:.1f}%"],
                  assumptions=[f"net floor areas from the room labels inside each unit's drawn boundary, outdoor rooms "
                               f"left out: {sizes}"],
                  missing=([f"the two leading units differ by only {100 * margin:.1f}%; a unit sum is within 5% for "
                            "69 of 99 test units"] if margin < 0.1 else []))


def measure(m: dict, bar, question: str, size: tuple[int, int]):
    """A VLM box (0-1000 relative) of the element + the scale bar -> (value, unit, box_px, trace note), or None.
    The unit follows the question (m vs ft) when it names one, else the bar's."""
    try:
        x1, y1, x2, y2 = (float(v) for v in m["bbox_2d"])
    except (KeyError, TypeError, ValueError):
        return None
    W, H = size
    box = [round(min(x1, x2) / 1000 * W, 1), round(min(y1, y2) / 1000 * H, 1), round(max(x1, x2) / 1000 * W, 1),
           round(max(y1, y2) / 1000 * H, 1)]
    wpx, hpx = box[2] - box[0], box[3] - box[1]
    if wpx < 3 or hpx < 3 or not bar.px_per_m:
        return None
    ext = m.get("extent", "width")
    q = question.lower()
    unit = "ft" if re.search(r"\b(feet|foot|ft|sq\.? ?ft|ft²)\b", q) else \
        "m" if re.search(r"\b(meters?|metres?|m|m²|m2|mètres?|metri|metros?|meter)\b", q) else bar.unit
    per = bar.px_per_m * SB.UNIT_M.get(unit, 1.0)  # px per unit of the answer
    if ext == "area":
        v, u = (wpx / per) * (hpx / per), ("m2" if unit == "m" else f"{unit}2")
    else:
        v, u = (wpx if ext == "width" else hpx) / per, unit
    return round(v, 2), u, box, f"measured {ext}: box {wpx:.0f} x {hpx:.0f} px at {per:.2f} px per {unit} = {v:.2f} {u}"
