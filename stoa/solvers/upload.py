"""`upload` solver = the pipeline behind the upload normaliser (P1): any uploaded format in, evidence on that file out.

    prepare(path, question)  DWG -> DXF, generic SVG -> PDF, HEIC -> JPEG, multi-page PDF -> the picked page
    svg_ifc first            for IfcConvert-style SVGs (route() has no .svg branch yet: G1)
    Pipeline.solve           everything else, unchanged (same kwargs as `pipeline`)
    restore(ans, prep)       evidence back on the uploaded file / page, conversion notes first in assumptions

On a file that needs no conversion it IS the pipeline (same answer, same cost). Use it from the API / batch CLI for
uploads, or let G1 fold the two calls into Pipeline.solve. Measured on results/splits/up_fmt*.jsonl
(experiments/p1/formats/).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from stoa.contract import Answer
from stoa.formats.normalize import prepare, restore
from stoa.pipeline import Pipeline
from stoa.solvers import register


@register("upload")
class Upload(Pipeline):
    name = "upload"

    def solve(self, path: str, question: str) -> Answer:
        t0 = time.perf_counter()
        prep = prepare(path, question)
        if prep.kind == "page" and whole_house_count(question):
            whole = self._whole_set(prep, question)
            if whole is not None:
                whole.cost.wall_seconds = round(max(whole.cost.wall_seconds, time.perf_counter() - t0), 4)
                return whole
        ans = None
        if Path(prep.path).suffix.lower() == ".svg":
            from stoa.solvers.svg_ifc import SvgIfc

            try:
                ans = SvgIfc().solve(prep.path, question)
            except Exception as e:  # noqa: BLE001 — a malformed SVG must not surface as an error (captain 13:56)
                ans = Answer.abstain("svg_ifc", f"svg_ifc failed: {type(e).__name__}: {e}")
            if ans.value is None:
                # a question svg_ifc does not read (perimeter, room size, title text, …): the same sheet as a vector
                # PDF goes to the PDF readers (plan_rooms reads IFC-style sheets), evidence mapped back to the SVG
                from stoa.formats.normalize import svg_to_pdf

                why = ans.trace[-1] if ans.trace else ""
                try:
                    prep.path, prep.svg_frame = svg_to_pdf(prep.path)
                    prep.kind = "svg_pdf"
                    prep.notes.append("IFC-style SVG read as a vector drawing (converted to PDF) for this question")
                    ans = None
                except Exception as e:  # noqa: BLE001
                    ans.trace = [*ans.trace, f"upload: SVG -> PDF failed ({e}); {why}"]
            else:  # svg_ifc answered: give it the recognise chip the pipeline gives its answers (G1 13:56)
                from stoa.pipeline import _recognition
                from stoa.router.recognize import chip, trace_line

                if (rec := _recognition(prep.path, None)) is not None:
                    ans.trace = [trace_line(rec), *ans.trace]
                    ans.assumptions = [*ans.assumptions, chip(rec)]
        if ans is None:
            try:
                ans = super().solve(prep.path, question)
            except Exception as e:  # noqa: BLE001 — never an error page: a plain answer that says what went wrong
                ans = Answer.abstain("upload", f"the drawing could not be read: {type(e).__name__}: {e}")
                ans.missing = [*ans.missing, "a readable copy of the drawing (PDF, PNG/JPG, IFC or DXF); this file "
                                             f"could not be read ({type(e).__name__})"]
        if Path(prep.path).suffix.lower() == ".pdf" and _no_reader(ans) and _recognised(ans) != "bim_plan":
            # not on IFC-exported sheets: their readers abstain because the question needs the model (up_lone: 98
            # such rows, whole-model counts / volumes), which a picture of one storey does not have either
            ans = self._as_image(prep, question, ans)
        elif Path(prep.path).suffix.lower() in (".png", ".jpg", ".jpeg") and _weak(ans) and \
                _intent(ans) in VECTOR_INTENTS:
            ans = self._as_vector(prep, question, ans)
        if Path(prep.path).suffix.lower() == ".dxf" and prep.missing and "dxf" in (ans.solver or "") and \
                ans.value is not None:
            blocks_only = all("drawn as blocks" in m for m in prep.missing)
            if not blocks_only:
                # the reader found none of its layers: '0 bathtubs' @0.99 meant 'no FIXTURE layer' (up_fmt4: 26 of 60
                # answers on renamed-layer DXFs were confident and wrong)
                ans.confidence = round(min(ans.confidence, 0.2), 3)
                ans.trace = [*ans.trace, "upload: confidence capped at 0.2 (the DXF has none of the reader's layers)"]
            elif any("door/window blocks (INSERT)" in str(t) for t in ans.trace):  # the reader counted them
                prep.missing = [m for m in prep.missing if "drawn as blocks" not in m]
            elif str(ans.unit or "").lower() == "count":  # doors/windows as blocks: a count may miss all of them
                ans.confidence = round(min(ans.confidence, 0.3), 3)
                ans.trace = [*ans.trace, "upload: count capped at 0.3 (doors/windows drawn as blocks)"]
        if Path(prep.path).suffix.lower() == ".dxf" and "dxf" in (ans.solver or ""):
            ans = _real_cad_zero(ans, prep)
            if not any(Path(p).with_suffix(".pdf").exists() for p in (prep.path, prep.original)):
                # a lone DXF / DWG upload: there is no PDF to map to, so 'could not map' is noise to the visitor
                ans.missing = [m for m in ans.missing if "could not map DXF to the PDF page" not in m]
        if prep.error and ans.value is None:
            ans.trace = [*ans.trace, f"upload: {prep.error}"]
        ans = restore(ans, prep)
        if prep.kind != "as_is":
            ans.trace = [f"upload: {Path(prep.original).suffix.lower()} -> {prep.kind} ({Path(prep.path).name})",
                         *ans.trace]
        ans.cost.wall_seconds = round(max(ans.cost.wall_seconds, time.perf_counter() - t0), 4)
        return ans

    def _whole_set(self, prep, question: str) -> Answer | None:
        """X1 15:46 (a real 25-page US CAD set): 'How many toilets are there in the whole house?' was answered from ONE
        sheet (the 1st floor's WC = 1; the 2nd floor has the other). A whole-house COUNT on a set with one floor-plan
        sheet per storey (pages.floor_plan_pages: >= 2, each titled with a different storey) = the count on every
        floor-plan sheet, summed; evidence stays on each sheet's page. None (-> the usual one-page answer) when any
        sheet gives no number."""
        from stoa.data import dataset_rel
        from stoa.formats.normalize import _cache_dir
        from stoa.formats.pages import floor_plan_pages, one_page_copy

        src = prep.original
        try:
            sheets = floor_plan_pages(src)
        except Exception:  # noqa: BLE001 — the one-page answer stands
            return None
        if not sheets:
            return None
        parts = []
        for k, storey in sheets:
            try:
                copy = one_page_copy(src, k, _cache_dir(Path(src)))
                part = self.solve(copy, question)  # a one-page copy: the full upload path, no second whole-set step
            except Exception:  # noqa: BLE001
                return None
            v = part.value
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or float(v) != int(v):
                return None
            parts.append((k, storey, copy, part))
        total = int(sum(int(p.value) for *_, p in parts))
        orig = dataset_rel(src)
        first = parts[0][3]
        ans = first.model_copy(deep=True)
        ans.value, ans.unit = total, first.unit or "count"
        ans.confidence = round(min(WHOLE_CAP, *(float(p.confidence or 0) for *_, p in parts)), 3)
        solvers = list(dict.fromkeys(str(p.solver or "") for *_, p in parts))
        ans.solver = solvers[0] if len(solvers) == 1 else "upload/sheets(" + "+".join(solvers) + ")"
        each = " + ".join(f"{p.value} on the {storey} plan (page {k + 1})" for k, storey, _, p in parts)
        ans.evidence, ans.trace, ans.assumptions, ans.missing = [], [
            f"upload: whole-house count on a {prep.pages}-page set: counted on each of its {len(parts)} floor-plan "
            f"sheets and summed: {each} = {total}"], [], []
        cost = None
        for k, storey, copy, p in parts:
            names = {str(copy), dataset_rel(str(copy)), str(Path(copy).resolve())}
            for ev in p.evidence:
                if ev.file in names or str(Path(ev.file).resolve()) in names:
                    ev.file, ev.page = orig, k
                ev.note = f"{storey} plan (page {k + 1}): {ev.note or ''}".rstrip(": ")
                ans.evidence.append(ev)
            ans.trace += [f"[page {k + 1} {storey}] {t}" for t in p.trace[:12]]
            for a in p.assumptions:  # 'the page was read as an image' is about ONE sheet: say which
                a = a if str(a).startswith("recognised as:") else f"page {k + 1} ({storey}): {a}"
                if a not in ans.assumptions:
                    ans.assumptions.append(a)
            ans.missing += [m for m in p.missing if m not in ans.missing]
            cost = p.cost if cost is None else cost.add(p.cost)
        ans.cost = cost
        if ans.confidence < min(float(p.confidence or 0) for *_, p in parts):
            ans.trace.append(f"upload: confidence capped at {WHOLE_CAP} (a sum over sheets)")
        prep.notes = [n for n in prep.notes if not n.startswith("multi-page PDF")]
        prep.notes.append(f"multi-page PDF ({prep.pages} pages): a whole-house count, so every floor-plan sheet was "
                          f"read and the counts added ({each}); site, roof, ceiling and elevation sheets were not counted")
        prep.kind = "sheets"  # evidence is already on the uploaded file's pages; restore() only adds the notes
        return restore(ans, prep)

    def _as_image(self, prep, question: str, ans0: Answer) -> Answer:
        """X1 14:22 (wild set): a VECTOR sheet no PDF reader can read (text drawn as outlines, no printed scale, no
        room fills) ended at the dataset prior ('No door types have fire rating information…', '1 bedroom') on 17 of
        49 wild questions. Read the page as a picture instead (raster / LoRA / VLM / agent); evidence comes back as
        points on the PDF page. Kept only when the image path gives a real answer."""
        from stoa.formats.normalize import render_png

        try:
            png, zoom = render_png(prep.path)
            ans = super().solve(png, question)
        except Exception as e:  # noqa: BLE001 — the PDF answer stands
            ans0.trace = [*ans0.trace, f"upload: page as image failed ({type(e).__name__}: {e})"]
            return ans0
        if _no_reader(ans):
            ans0.trace = [*ans0.trace, f"upload: page as image ({Path(png).name}) gave no reading either "
                                       f"({ans.solver}: {ans.value!r})"]
            return ans0
        why = next((t for t in ans0.trace if "abstained" in str(t)), "")
        # the chip describes the UPLOADED sheet (the PDF's recognition), not the PNG made from it ('CubiCasa-style
        # raster plan' for a CAD PDF would mislead the visitor and W5's 'Change' menu)
        rec0 = [t for t in ans0.trace[:3] if str(t).startswith("recognised:")]
        chip0 = [a for a in ans0.assumptions if str(a).startswith("recognised as:")]
        ans.trace = [*rec0, f"upload: no PDF reader answered ({why[:160]}); page read as an image at {72 * zoom:.0f} "
                            "dpi", *(t for t in ans.trace if not (rec0 and str(t).startswith("recognised:")))]
        if chip0:
            ans.assumptions = [*(a for a in ans.assumptions if not str(a).startswith("recognised as:")), *chip0]
        ans.cost = ans.cost.add(ans0.cost)  # both readings were paid for
        if ans.confidence > 0.5:
            # the image readers' confidences were measured on CubiCasa renders and scans, not on rendered CAD sheets:
            # on X1's wild set their 0.75 answers were right on 4/5 counts but 0/7 lengths (cm dimension strings read
            # as mm, '1104.0 mm' for 11.04 m) -> in-between at most
            ans.confidence = 0.5
            ans.trace = [*ans.trace, "upload: confidence capped at 0.5 (a vector sheet read as a picture)"]
        prep.path, prep.scan_zoom = png, zoom
        prep.kind = {"page": "page+render", "svg_pdf": "svg_pdf+render"}.get(prep.kind, "render")
        prep.notes.append(f"no PDF reader could read this sheet (e.g. its text is drawn as outlines): the page was "
                          f"read as an image at {72 * zoom:.0f} dpi")
        return ans

    def _as_vector(self, prep, question: str, ans0: Answer) -> Answer:
        """A clean PNG/JPEG export of a CAD sheet (its DPI metadata gives a paper size: A2 @120 dpi, A4 @200 dpi) where
        the image readers found nothing usable: trace it into a vector PDF with the image's OCR text as invisible text
        (stoa/formats/vectorize.py) and let the vector readers measure it (E1's lineplan: room areas / sides by label or
        position at the printed 1:N; label_count). Kept only when one of VECTOR_READERS gives a number; evidence comes
        back as image pixels (px = pt * dpi / 72)."""
        from stoa.formats import vectorize as V
        from stoa.formats.normalize import _cache_dir

        hit = V.paper(prep.path)
        if hit is None:
            return ans0
        dpi, sheet = hit
        src = Path(prep.path)
        out = _cache_dir(src) / f"{src.stem}.vec{V.VERSION}.pdf"
        try:
            if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
                V.to_vector_pdf(str(src), str(out), dpi=dpi)
            ans = super().solve(str(out), question)
        except Exception as e:  # noqa: BLE001 — the image answer stands
            ans0.trace = [*ans0.trace, f"upload: image as vector lines failed ({type(e).__name__}: {e})"]
            return ans0
        reader = str(ans.solver or "").split("/")[-1]
        if reader not in VECTOR_READERS or not isinstance(ans.value, (int, float)) or isinstance(ans.value, bool):
            ans0.trace = [*ans0.trace, f"upload: image traced as vector lines ({out.name}) gave no measured reading "
                                       f"({ans.solver}: {ans.value!r})"]
            return ans0
        from stoa.data import dataset_rel

        f = dpi / 72.0
        vec = {str(out), dataset_rel(str(out)), str(out.resolve())}
        for ev in ans.evidence:
            if ev.file in vec and ev.bbox_space == "pdf_pt":
                ev.file, ev.page, ev.bbox_space = dataset_rel(prep.path), None, "image_px"
                ev.bbox = [v * f for v in ev.bbox] if ev.bbox else ev.bbox
                for attr in ("polygon", "polyline"):
                    pts = getattr(ev, attr, None)
                    if pts:
                        setattr(ev, attr, [[x * f, y * f] for x, y in pts])
        rec0 = [t for t in ans0.trace[:3] if str(t).startswith("recognised:")]
        chip0 = [a for a in ans0.assumptions if str(a).startswith("recognised as:")]
        ans.trace = [*rec0, f"upload: the image readers gave no usable reading ({ans0.solver}: {str(ans0.value)[:60]!r} "
                            f"@{ans0.confidence:.2f}); image traced as vector lines ({sheet} at {dpi:.0f} dpi from its "
                            f"metadata, {out.name})", *(t for t in ans.trace if not (rec0 and str(t).startswith(
                                "recognised:")))]
        if chip0:
            ans.assumptions = [*(a for a in ans.assumptions if not str(a).startswith("recognised as:")), *chip0]
        ans.cost = ans.cost.add(ans0.cost)
        if ans.confidence > VECTOR_CAP:
            ans.confidence = VECTOR_CAP
            ans.trace = [*ans.trace, f"upload: confidence capped at {VECTOR_CAP} (lines traced from a picture)"]
        prep.kind = "vector" if prep.kind == "as_is" else f"{prep.kind}+vector"  # restore() adds the note
        prep.notes.append(f"the image's metadata gives {'an' if sheet[0] in 'AE' else 'a'} {sheet} sheet at {dpi:.0f} dpi: its lines were traced into a "
                          f"vector drawing (text from the image's OCR layer) and measured at the printed scale")
        return ans


WHOLE_CAP = 0.6  # a sum over sheets: each sheet's count must be right
_COUNT_Q = re.compile(r"^\W*(?:how\s+many|what\s+is\s+the\s+(?:total\s+)?number\s+of|number\s+of|count\b|wie\s*viele|"
                      r"kuinka\s+monta|montako|combien|cu[aá]nt[oa]s|quant[ie]\b|hur\s+många|hvor\s+mange|hoeveel)", re.I)
_WHOLE_Q = re.compile(
    r"\b(?:whole|entire|all\s+(?:the\s+)?(?:floors|storeys|stories|levels)|in\s+total|altogether|"
    r"(?:in|of|does|do)\s+the\s+(?:house|home|building|dwelling|villa|cottage|residence)|"
    r"the\s+(?:house|home|building|dwelling)\s+(?:have|has|contain)|ganze[nms]?|gesamte[nms]?|insgesamt|im\s+haus|"
    r"koko\s+\w+|talossa|rakennuksessa|toute\s+la\s+maison|dans\s+la\s+maison|au\s+total|toda\s+la\s+casa|"
    r"en\s+la\s+casa|en\s+total|tutta\s+la\s+casa|nella\s+casa|in\s+totale|hela\s+huset|i\s+huset|totalt)\b", re.I)
_ONE_STOREY_Q = re.compile(
    r"\b(?:ground|first|second|third|fourth|fifth|top|upper|lower|main|1st|2nd|3rd|\d+(?:st|nd|rd|th))\s+"
    r"(?:floor|storey|story|level)|\b(?:upstairs|downstairs|basement|cellar|attic|loft|mezzanine)\b|\blevel\s*-?\d|"
    r"\bfloor\s*\d|\b(?:this|that|each|every|per|one)\s+(?:floor|storey|story|level|sheet|plan|page|drawing)|"
    r"\bon\s+the\s+(?:floor|sheet|plan|page|drawing)\b|erdgeschoss|obergeschoss|untergeschoss|dachgeschoss|keller|"
    r"\b(?:EG|OG|UG|KG|DG)\b|\d\.\s*OG|kerros|étage|rez-de-chaussée|planta\s+(?:baja|alta)|piano\s+terra|"
    r"primo\s+piano|våning|\b[A-Z]{1,2}-?\d{1,3}\.\d{1,2}\b", re.I)
# drawn on EVERY storey's sheet (a stair, a lift shaft, a column, a chimney) or not a per-storey object at all
_NOT_SUMMED_Q = re.compile(
    r"\b(?:stair\w*|risers?|steps?|treads?|flights?|floors|storeys|stories|levels|sheets?|pages?|drawings?|"
    r"elevations?|sections?|details?|chimney\w*|flues?|lifts?|elevators?|shafts?|grid\s*lines?|axes|columns?|"
    r"pillars?|roofs?|ridges?|apartments?|units?|dwellings?|flats?|treppe\w*|stufen|geschoss\w*|etage\w*|"
    r"porras\w*|kerro\w*)\b", re.I)


def whole_house_count(question: str) -> bool:
    """A count over the whole building ('How many toilets are there in the whole house?', 'How many bedrooms does the
    house have?') that names no storey or sheet and counts things that live on one storey each."""
    q = question or ""
    return bool(_COUNT_Q.search(q) and _WHOLE_Q.search(q) and not _ONE_STOREY_Q.search(q)
                and not _NOT_SUMMED_Q.search(q))


VECTOR_INTENTS = ("area", "length", "count")
VECTOR_READERS = ("plan_rooms", "label_count", "line_rooms")  # readers that MEASURE / count the traced linework
VECTOR_CAP = 0.5


CAD_ZERO_MISSING = ("this drawing's layers hold nothing of the asked kind that the DXF reader can measure (room areas "
                    "need closed room outlines on a room layer; real CAD sheets often have only room-name tags): a PDF "
                    "or image of the same sheet lets the drawing readers trace the rooms")


def _real_cad_zero(ans: Answer, prep) -> Answer:
    """A ZERO from the DXF reader on a real CAD file (not the reader's own layer schema: a DWG, or a DXF whose layers
    were renamed / guessed) means 'found none', not 'there are none'. All 5 Revit DWGs in data/opening_design (P1
    15:50) answered 'total floor area' 0 m2 @0.96 (no closed room outlines, only MTEXT room tags) and one 0 doors /
    0 windows @0.99. Areas / lengths -> no value + what is missing (0 m2 is never a best estimate of a floor);
    counts keep the 0 at <= 0.2 (a sheet can truly have no windows). Swiss-schema DXFs (the dataset's) are untouched.
"""
    v = ans.value
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v != 0:
        return ans
    from stoa.data import abspath
    from stoa.solvers.vector.dxf_solver import _layers

    try:
        status = _layers(str(abspath(prep.path))).status
    except Exception:  # noqa: BLE001
        return ans
    if status == "swiss":
        return ans
    unit = str(ans.unit or "").lower()
    if unit == "count":
        ans.confidence = round(min(ans.confidence, 0.2), 3)
        ans.trace = [*ans.trace, f"upload: a 0 count from a non-standard DXF (layers: {status}) capped at 0.2"]
        ans.missing = [*ans.missing, "nothing of the asked kind was found on this DXF's layers: 0 means 'none found', "
                                     "not 'none exist'"]
        return ans
    ans.trace = [*ans.trace, f"upload: the DXF reader's 0 {ans.unit or ''} on a non-standard DXF (layers: {status}) "
                             "is 'nothing found', not a measurement -> no value"]
    ans.value, ans.confidence = None, 0.0
    ans.missing = [*ans.missing, CAD_ZERO_MISSING]
    return ans


def _weak(ans: Answer) -> bool:
    """No usable number from the image readers: nothing, a non-number ('Not determinable', 'BRH 87.50'), or a guess
    at <= 0.3."""
    v = ans.value
    return _no_reader(ans) or not isinstance(v, (int, float)) or isinstance(v, bool) or ans.confidence <= 0.3


def _intent(ans: Answer) -> str | None:
    """The pipeline's intent kind ('area', 'length', 'count', …) from its 'intent: <kind> …' trace line."""
    for t in ans.trace[:6]:
        if str(t).startswith("intent: "):
            return str(t)[8:].split()[0] if str(t)[8:].split() else None
    return None


def _recognised(ans: Answer) -> str | None:
    """G1's recognise step's drawing kind ('bim_plan', 'cad_sheet', 'other', …) from the answer's first trace line."""
    import json

    for t in ans.trace[:3]:
        if str(t).startswith("recognised:"):
            try:
                return json.loads(str(t)[len("recognised:"):]).get("kind")
            except ValueError:
                return None
    return None


def _no_reader(ans: Answer) -> bool:
    """The answer is the dataset prior (or nothing): no reader understood the drawing."""
    return ans.value is None or str(ans.solver or "").split("/")[-1] in ("prior", "dummy")
