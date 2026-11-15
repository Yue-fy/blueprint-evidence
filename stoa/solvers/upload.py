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
        from stoa.formats.warm import wait_for_warm

        waited = wait_for_warm(path)  # the upload's pre-read still running: use its caches instead of reading twice
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
        q_read = question
        if prep.kind.startswith("page") and prep.sheet:
            from stoa.formats.normalize import without_sheet

            q_read = without_sheet(question, prep.sheet)
            if q_read != question:
                prep.notes.append(f"sheet {prep.sheet} named in the question: its page was read as '{q_read}'")
        if ans is None:
            from stoa.formats.normalize import ask_context_on

            try:
                with ask_context_on(prep):  # a right-click point / region in the solved copy's coordinates
                    ans = super().solve(prep.path, q_read)
            except Exception as e:  # noqa: BLE001 — never an error page: a plain answer that says what went wrong
                ans = Answer.abstain("upload", f"the drawing could not be read: {type(e).__name__}: {e}")
                ans.missing = [*ans.missing, "a readable copy of the drawing (PDF, PNG/JPG, IFC or DXF); this file "
                                             f"could not be read ({type(e).__name__})"]
        picture = Path(prep.path).suffix.lower() in (".png", ".jpg", ".jpeg")
        sched = _schedule_image(prep, q_read, ans) if picture else None
        if Path(prep.path).suffix.lower() == ".pdf" and _no_reader(ans) and _recognised(ans) != "bim_plan":
            # not on IFC-exported sheets: their readers abstain because the question needs the model (up_lone: 98
            # such rows, whole-model counts / volumes), which a picture of one storey does not have either
            ans = self._as_image(prep, q_read, ans)
        elif picture and (sched or (_weak(ans) and _intent(ans) in VECTOR_INTENTS)):
            ans = self._as_vector(prep, q_read, ans, tables=sched)
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
        if waited >= 0.5:
            ans.trace = [*ans.trace, f"upload: waited {waited:.1f} s for this file's pre-read (stoa.formats.warm) to "
                                     "finish, then answered from its caches"]
        ans.cost.wall_seconds = round(max(ans.cost.wall_seconds, time.perf_counter() - t0), 4)
        from stoa.trust.calibrate import calibrate  # T1 c7: shown confidence = fitted P(right); after every decision

        return calibrate(ans, path)

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
        # W5 19:33 (DAF dorm SD set): every floor sheet prints the same ROOM COUNT table, and each sheet's reading was
        # its printed 'TOTAL = 144 DORMS' row -> 144 + 144 + 144 = 432 (gold 144). Equal readings that all come from a
        # printed TOTAL row are the building's total once, not one count per floor.
        same_total = len(parts) >= 2 and len({int(p.value) for *_, p in parts}) == 1 and all(
            any("printed total row" in str(t) for t in p.trace) for *_, p in parts)
        total = int(parts[0][3].value) if same_total else int(sum(int(p.value) for *_, p in parts))
        orig = dataset_rel(src)
        first = parts[0][3]
        ans = first.model_copy(deep=True)
        ans.value, ans.unit = total, first.unit or "count"
        ans.confidence = round(min(1.0 if same_total else WHOLE_CAP, *(float(p.confidence or 0) for *_, p in parts)), 3)
        solvers = list(dict.fromkeys(str(p.solver or "") for *_, p in parts))
        ans.solver = solvers[0] if len(solvers) == 1 else "upload/sheets(" + "+".join(solvers) + ")"
        each = " + ".join(f"{p.value} on the {storey} plan (page {k + 1})" for k, storey, _, p in parts)
        how = (f"every sheet's reading is the same printed building TOTAL row, so the answer is that total once, not "
               f"their sum: {each} -> {total}") if same_total else f"summed: {each} = {total}"
        ans.evidence, ans.trace, ans.assumptions, ans.missing = [], [
            f"upload: whole-house count on a {prep.pages}-page set: counted on each of its {len(parts)} floor-plan "
            f"sheets and {how}"], [], []
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
                          + (f"read; each prints the same building total ({each}), so that total was used once"
                             if same_total else f"read and the counts added ({each})")
                          + "; site, roof, ceiling and elevation sheets were not counted")
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
            try:
                from stoa.formats.normalize import seed_text_layer

                seeded = seed_text_layer(prep.path, png, zoom)  # the PDF's own words, not a 10-min OCR (S0 16:48)
            except Exception:  # noqa: BLE001 — the image readers read the text themselves as before
                seeded = 0
            ans = super().solve(png, question)
        except Exception as e:  # noqa: BLE001 — the PDF answer stands
            ans0.trace = [*ans0.trace, f"upload: page as image failed ({type(e).__name__}: {e})"]
            return ans0
        if _no_reader(ans) and not _flagged_zero(ans):
            ans0.trace = [*ans0.trace, f"upload: page as image ({Path(png).name}) gave no reading either "
                                       f"({ans.solver}: {ans.value!r})"]
            return ans0
        why = next((t for t in ans0.trace if "abstained" in str(t)), "")
        # the chip describes the UPLOADED sheet (the PDF's recognition), not the PNG made from it ('CubiCasa-style
        # raster plan' for a CAD PDF would mislead the visitor and W5's 'Change' menu)
        rec0 = [t for t in ans0.trace[:3] if str(t).startswith("recognised:")]
        chip0 = [a for a in ans0.assumptions if str(a).startswith("recognised as:")]
        ans.trace = [*rec0, f"upload: no PDF reader answered ({why[:160]}); page read as an image at {72 * zoom:.0f} "
                            "dpi" + (f" with the PDF's own text ({seeded} lines, no OCR)" if seeded else ""),
                     *(t for t in ans.trace if not (rec0 and str(t).startswith("recognised:")))]
        if chip0:
            ans.assumptions = [*(a for a in ans.assumptions if not str(a).startswith("recognised as:")), *chip0]
        from stoa.trust.sanity import range_guard  # T1 (Ziying): CubiCasa-fitted ranges only on CubiCasa flats

        ans = range_guard(ans, kind=_recognised(ans0) or "other")  # the render itself looks like a CubiCasa plan
        ans.cost = ans.cost.add(ans0.cost)  # both readings were paid for
        if ans.confidence > 0.5:
            # the image readers' confidences were measured on CubiCasa renders and scans, not on rendered CAD sheets:
            # on X1's wild set their 0.75 answers were right on 4/5 counts but 0/7 lengths (cm dimension strings read
            # as mm, '1104.0 mm' for 11.04 m) -> in-between at most
            ans.confidence = 0.5
            ans.trace = [*ans.trace, "upload: confidence capped at 0.5 (a vector sheet read as a picture)"]
        prep.path, prep.scan_zoom = png, zoom * (prep.paper_f or 1.0)  # px per point of the UPLOADED page
        prep.kind = {"page": "page+render", "svg_pdf": "svg_pdf+render"}.get(prep.kind, "render")
        # captain 20:47/20:49 (Ziying, dfe-08): 'no PDF reader could read this sheet' next to 'a text layer (145 lines)'
        # contradicted itself; with a text layer the PDF WAS read, only no vector reader answered THIS question
        prep.notes.append(f"no vector reader answered this question from the PDF's drawing (its text layer, "
                          f"{seeded} lines, was read): the page was also read as an image at {72 * zoom:.0f} dpi"
                          if seeded else
                          f"no PDF reader could read this sheet (e.g. its text is drawn as outlines): the page was "
                          f"read as an image at {72 * zoom:.0f} dpi")
        return ans

    def _as_vector(self, prep, question: str, ans0: Answer, tables: str | None = None) -> Answer:
        """A clean PNG/JPEG export of a CAD sheet (its DPI metadata gives a paper size: A2 @120 dpi, A4 @200 dpi) where
        the image readers found nothing usable: trace it into a vector PDF with the image's OCR text as invisible text
        (stoa/formats/vectorize.py) and let the vector readers measure it (E1's lineplan: room areas / sides by label or
        position at the printed 1:N; label_count). Kept only when one of VECTOR_READERS gives a number; evidence comes
        back as image pixels (px = pt * dpi / 72)."""
        from stoa.formats import vectorize as V

        hit, how = trace_paper(prep)
        if hit is None and tables:
            # a schedule is read for its rows, not measured: any page size maps the evidence back (the same px / pt
            # factor both ways); a scan's own render dpi when there is one
            z = float(getattr(prep, "scan_zoom", 0) or 0)
            hit, how = (72.0 * z if z else 150.0, "custom"), "for its table only (a schedule is read, not measured)"
        if hit is None:  # X1 19:20: this used to return silently
            ans0.trace = [*ans0.trace, "upload: image not traced as vector lines (no paper size from its metadata or "
                                       "its pixel size, and no metric scale bar with a printed 1:N)"]
            return ans0
        dpi, sheet = hit
        try:
            # P1 00:40 (S0 d30 wild-sealed): a 14 MB scan's trace took 132 s at gate load, BEFORE the readers' 30 s, so
            # each of its 4 questions took 184-210 s (d29: 4-8 s, scan not traced) and none got a traced reading. The
            # wait for the trace is bounded too (STOA_TRACE_WAIT, 90 s: 97.7 % of the 175 traces cached tonight and 29 of
            # 30 scans took less; 60 s would cut four Stoa-like 6000 px copies at 61-77 s); a longer trace runs on in the
            # background (warm's, or this daemon thread) and is then read, for the next question.
            ikey, qkey = _intent_key(ans0), _question_key(question)

            def after_trace(o):  # the trace landed after TRACE_WAIT_S (daemon thread): read the copy now, so a re-ask
                if not o:       # finds its caches warm, and remember 'nothing' as the over-budget pass does
                    return
                k = (f"{o}@{Path(o).stat().st_mtime_ns}", ikey, qkey)
                if k not in _VEC_NONE:
                    a = Pipeline.solve(self, str(o), question)
                    if a is not None and not (_measured(a) or (tables and _table_reading(a))):
                        _VEC_NONE.setdefault(k, f"no measured reading ({a.solver}; read after a late trace)")

            got, took_t = _solve_within(lambda: V.traced_copy(prep.path, dpi), TRACE_WAIT_S, after_trace)
        except Exception as e:  # noqa: BLE001 — the image answer stands
            ans0.trace = [*ans0.trace, f"upload: image as vector lines failed ({type(e).__name__}: {e})"]
            return ans0
        if got is None:
            ans0.trace = [*ans0.trace, f"upload: the traced copy of this image ({sheet} at {dpi:.0f} dpi) is still "
                                       f"being made after {TRACE_WAIT_S:.0f} s (STOA_TRACE_WAIT); the image answer "
                                       "stands (the trace finishes in the background: a later question on this "
                                       "sheet reads it)"]
            if TRACE_NOTE not in ans0.missing:
                ans0.missing = [TRACE_NOTE, *ans0.missing]
            return ans0
        out = Path(got)
        # S0 18:42: 4 slab questions on S-100F each spent 60-167 s here for nothing -> remember 'nothing' per sheet. X1 20:38:
        # keyed by the intent alone ('area unknown') one miss blocked DIFFERENT questions of that kind on the sheet
        # (sc-035/044 after a pdf_text miss; EFA 1F skirting sc-074/080/081 after one 'length room'): now per question.
        try:  # X1 20:43: a re-traced copy (new text layer) gets a fresh try
            built = out.stat().st_mtime_ns
        except OSError:
            built = 0
        key = (f"{out}@{built}", _intent_key(ans0), _question_key(question))
        if key in _VEC_NONE:
            ans0.trace = [*ans0.trace, f"upload: image as vector lines skipped ({out.name}: {_VEC_NONE[key]} for the "
                                       f"same question asked earlier on this sheet)"]
            return ans0
        def late(a):  # the pass finished after the budget, in its daemon thread: remember only 'it gives nothing'
            if a is not None and not _measured(a):
                _VEC_NONE.setdefault(key, f"no measured reading ({a.solver}; finished after the budget)")

        try:
            ans, took = _solve_within(lambda: Pipeline.solve(self, str(out), question), VECTOR_BUDGET_S, late)
        except Exception as e:  # noqa: BLE001 — the image answer stands
            ans0.trace = [*ans0.trace, f"upload: image as vector lines failed ({type(e).__name__}: {e})"]
            return ans0
        if ans is None:
            # S0 19:18 (gate d25, load 260): NOT remembered. The first pass on a sheet also reads the traced copy cold;
            # memoising 'ran over' denied a later classrooms question its right answer (151.79 -> raster 1003.53). The
            # pass runs on in the background (warming the copy's caches) and memos only if it then gives nothing.
            ans0.trace = [*ans0.trace, f"upload: image traced as vector lines ({out.name}): no reading within "
                                       f"{VECTOR_BUDGET_S:.0f} s (STOA_VECTOR_BUDGET); the image answer stands (the "
                                       "readers finish in the background: a later question on this sheet tries again)"]
            # X1 22:22: the judge's first question on a new sheet is the one that runs over (S-100F PNG: 5 slab questions
            # got raster guesses, the same questions 2-4 s later 61.69 = gold from the warm copy). Say so on the card
            # instead of a longer wait (P1 23:12: a cold pass is 35-170 s with load; scans' late answers were 0.25).
            if TRACE_NOTE not in ans0.missing:
                ans0.missing = [TRACE_NOTE, *ans0.missing]
            return ans0
        if not (_measured(ans) or (tables and _table_reading(ans))):
            _VEC_NONE[key] = f"no measured reading ({ans.solver})"
            ans0.trace = [*ans0.trace, f"upload: image traced as vector lines ({out.name}) gave no measured reading "
                                       f"({ans.solver}: {ans.value!r}, {took:.0f} s)"]
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
        head = (f"upload: the question names the {tables} printed on this image: its rows were read as a table from the "
                f"image's text (OCR) on a traced copy ({out.name}, {how}); the image readers said "
                f"{ans0.solver}: {str(ans0.value)[:60]!r} @{ans0.confidence:.2f}") if tables else (
                f"upload: the image readers gave no usable reading ({ans0.solver}: {str(ans0.value)[:60]!r} "
                f"@{ans0.confidence:.2f}); image traced as vector lines ({sheet} at {dpi:.0f} dpi {how}, {out.name})")
        ans.trace = [*rec0, head, *(t for t in ans.trace if not (rec0 and str(t).startswith("recognised:")))]
        if chip0:
            ans.assumptions = [*(a for a in ans.assumptions if not str(a).startswith("recognised as:")), *chip0]
        ans.cost = ans.cost.add(ans0.cost)
        if ans.confidence > VECTOR_CAP:
            ans.confidence = VECTOR_CAP
            ans.trace = [*ans.trace, f"upload: confidence capped at {VECTOR_CAP} (lines traced from a picture)"]
        prep.kind = "vector" if prep.kind == "as_is" else f"{prep.kind}+vector"  # restore() adds the note
        if tables:
            prep.notes.append(f"the image prints the {tables} the question names: its rows were read from the image's "
                              "text (OCR) as a table")
            return ans
        what = "a sheet of its own size" if sheet == "custom" else f"{'an' if sheet[0] in 'AE' else 'a'} {sheet} sheet"
        prep.notes.append(f"the image is {what} at {dpi:.0f} dpi ({how}): its lines were traced into a vector drawing "
                          "(text from the image's OCR layer) and measured at the printed scale")
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
VECTOR_BUDGET_S = float(__import__("os").environ.get("STOA_VECTOR_BUDGET", "30"))  # the traced copy's readers, per question
TRACE_WAIT_S = float(__import__("os").environ.get("STOA_TRACE_WAIT", "90"))  # a question's wait for the trace itself
# F4 23:22 / W5: the web does not cache an answer whose missing[] says this (stoaApi.ts matches 'still being measured')
TRACE_NOTE = ("a measured reading from the drawing's own lines: they are still being measured (a traced copy, first "
              "question on this sheet); ask again in about a minute")
_VEC_NONE: dict[tuple[str, str, str], str] = {}  # (traced copy, 'area slab', question words) -> why it got nothing


def trace_paper(prep) -> tuple[tuple[float, str] | None, str]:
    """((dpi, sheet) | None, how): the paper an uploaded image is traced at (Upload._as_vector), in order: its DPI tag,
    its pixel size + printed sheet size (also a scanned PDF's own image pixels, paper.paper_of_image), a deskewed phone
    photo's corners, its metric scale bar x printed 1:N, a scanned PDF's own page size. The upload pre-read
    (stoa.formats.warm) calls it too, so the copy it traces is the one a question reads (same dpi, no re-trace)."""
    from stoa.formats import vectorize as V

    hit, how = V.paper(prep.path), "from its metadata"
    if hit is None:  # E1 17:29: Stoa's sheets carry no (or PyMuPDF's 96) dpi tag; their pixel size is the sheet
        from stoa.formats.paper import paper_of_image
        from stoa.solvers.raster import textlayer as T

        how = "from its pixel size and the sheet size it prints"
        texts: list[str] = []
        try:  # the image readers ran first, so the text layer is cached
            texts = [i.text for i in T.text_layer(prep.path).items]
            hit = paper_of_image(prep.path, texts)
            if hit is None and __import__("os").environ.get("STOA_DIMS_PAPER", "0") == "1":
                # F2 22:29 (night-F2 1823b38a): pixels that fit SEVERAL sheets and a sheet that names none (Stoa's AFCEC A-2
                # PNG: ANSI D @300 = Tabloid @600) -> the sheet its own imperial dimension strings confirm at the printed
                # 1:N (AFCEC: 4 of 17 drawn lines = Tabloid, as its PDF twin's page). OFF by default (P1 23:04): the paper
                # is right, but on that traced copy openings counts 494 doors @0.5 from 592 'arcs' (the PDF twin: 81 from
                # 87): at 1:480 a door swing is ~2 mm on paper, the size of traced text glyphs. 0 changes on the 25 AFCEC /
                # AFCEE PNG dev rows (runs -230033-upload-afcec_png-f6c8 vs -230113-...-67fc). STOA_DIMS_PAPER=1 turns it on.
                try:
                    from stoa.features.takeoff import paper_by_dims
                except ImportError:
                    paper_by_dims = None
                got = paper_by_dims(prep.path, T.text_layer(prep.path)) if paper_by_dims else None
                if got is not None:
                    hit, how = got[:2], got[2]
            if hit is None:
                # O1 20:48 (c9 patch): a deskewed phone photo has no dpi tag and no export pixel size; its paper = the
                # photographed sheet's corners + the one sheet size it prints (10 photo rows 4/10 -> 6/10, W 2 / L 1;
                # W6 DfE photo kitchen: raster leak 65.52 m2 -> lineplan 36.43, alt gold 35.61)
                from stoa.solvers.raster.photo import named_paper

                hit = named_paper(prep.path, texts)
                if hit is not None:
                    how = "from the photographed sheet's corners and the sheet size it prints"
        except Exception:  # noqa: BLE001
            hit = None
        scan_pdf = (getattr(prep, "scan_pdf", "") if getattr(prep, "scan_zoom", 0)
                    and __import__("os").environ.get("STOA_SCAN_PAPER", "1") != "0" else "")
        if hit is None:  # X1 19:20: a scan / crop that is no standard paper: its scale bar x its printed 1:N
            from stoa.formats.paper import paper_of_bar

            try:
                bar = paper_of_bar(prep.path, T.text_layer(prep.path))
            except Exception:  # noqa: BLE001
                bar = None
            if bar is not None:
                hit, how = bar[:2], bar[2]
        if hit is None and scan_pdf:
            # P1 22:40 (d29 scan_dev): a scanned PDF's render is untagged and shrunk to 6000 px; paper_of_image above
            # reads the scan's OWN pixels + its printed size (S-100F / A-101F A1, M-101A ANSI D); with no size printed,
            # after its scale bar: the scanned page's own size (MFC ANSI D, AFCEC Tabloid; paper.paper_of_scan)
            from stoa.formats.paper import paper_of_scan

            try:
                got = paper_of_scan(scan_pdf, _image_size(prep.path), texts)
            except Exception:  # noqa: BLE001
                got = None
            if got is not None:
                hit, how = got[:2], got[2]
    return hit, how


def _image_size(path: str) -> tuple[int, int]:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        return im.size


def _question_key(question: str) -> str:
    """The question's words, lower case, without punctuation or a trailing sheet / page reference."""
    return " ".join(re.findall(r"[a-z0-9äöåéèüß]+", (question or "").lower()))


def _intent_key(ans: Answer) -> str:
    """'area slab' from the pipeline's 'intent: area slab [drawing] (lang en)' trace line ('' when absent)."""
    for t in ans.trace[:6]:
        if str(t).startswith("intent: "):
            return " ".join(str(t)[8:].split("[")[0].split()[:2])
    return ""


def _measured(ans: Answer) -> bool:
    """One of VECTOR_READERS gave a number on the traced copy, or E1's slab_area a DIMENSION-CONFIRMED one (>= 0.5: the
    printed dimensions OCR'd and the traced slab agree; E1 19:36, Stoa's S-100F PNG '150 SLAB-ON-GRADE W/ D13 @ 300'
    22.32 raster -> 61.69 m2 = gold). Traced-only slab areas come back @0.25 and stay out (D16 194 vs 214.5)."""
    reader = str(ans.solver or "").split("/")[-1]
    number = isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool)
    if reader == "piping":
        # P1 22:50: the piping reader's tag-to-tag span ('6.5 ft' between FP-1 and FP-2 at the printed 1/4" = 1'-0") is a
        # measurement on the traced copy, right when its paper is: scanned M-101A (ANSI D from the scan's pixels) 4 / 4 =
        # gold in d29 scan_dev + the A/B, all lost to raster '12 feet' @0.3. Its tag counts / sizes stay with the image
        # pass; area_tags on traced copies was 3 right / 14 wrong (OCR'd area numbers) and stays out.
        return bool(isinstance(ans.value, str) and re.fullmatch(r"\d+(?:\.\d+)?\s*ft", ans.value.strip())
                    and any("tag-center span" in str(t) for t in ans.trace))
    if number and reader == "openings":
        # P1 23:06: door swings traced from a picture at a small printed scale are glyph-sized (1:480: a 0.9 m leaf is
        # 1.9 mm on paper, like a traced 'O' / 'C'): Stoa's AFCEC A-2 PNG traced at its right Tabloid paper gave
        # 494 doors @0.5 from 592 'arcs' (its PDF twin: 81 from 87). Coarser than 1:OPENINGS_MAX_N -> not kept.
        m = re.search(r"printed scale 1:(\d+)", " ".join(map(str, ans.trace)))
        if m and int(m.group(1)) > OPENINGS_MAX_N:
            return False
    return number and (reader in VECTOR_READERS or (reader == "slab_area" and ans.confidence >= 0.5))


OPENINGS_MAX_N = 300  # a 0.9 m door leaf >= 3 mm on paper (text is ~2.5 mm)
TABLE_READERS = ("pdf_text", "schedule", "area_table")


def _table_reading(ans: Answer) -> bool:
    """A number from a schedule / table reader (for a question that names a schedule printed on a picture)."""
    reader = str(ans.solver or "").split("/")[-1]
    return reader in TABLE_READERS and isinstance(ans.value, (int, float)) and not isinstance(ans.value, bool)


def _schedule_image(prep, question: str, ans: Answer) -> str | None:
    """'DOOR SCHEDULE' when the question names a schedule that this picture prints as a title (X1 21:22: a scanned
    A-602 door schedule was counted as door SYMBOLS by vlm_lora: 0 doors) and no table reader answered yet; the
    image's text layer is read only for such questions (it is cached by the image pass / the upload's pre-read)."""
    from stoa.formats.pages import _SCHED_Q, schedule_title

    if not _SCHED_Q.search(question or "") or _table_reading(ans):
        return None
    try:
        from stoa.solvers.raster import textlayer as T

        text = "\n".join(str(i.text) for i in T.text_layer(prep.path).items)
    except Exception:  # noqa: BLE001
        return None
    return schedule_title(question, text)


def _solve_within(fn, budget: float, late=None):
    """(fn(), seconds) or (None, budget) when fn takes longer: it runs on in a daemon thread (Python threads cannot be
    stopped), its answer discarded for this question; late(answer) is then called in that thread when it finishes.
    The cost meter's context goes with it."""
    import contextvars
    import threading

    box: dict = {}
    ctx = contextvars.copy_context()
    lock = threading.Lock()

    def run():
        try:
            box["ans"] = ctx.run(fn)
        except BaseException as e:  # noqa: BLE001 — re-raised in the caller
            box["err"] = e
        with lock:
            gone = box.get("gone")
        if gone and late is not None:
            try:
                late(box.get("ans"))
            except Exception:  # noqa: BLE001 — a background bookkeeping step
                pass

    t0 = time.perf_counter()
    th = threading.Thread(target=run, daemon=True, name="upload-vector")
    th.start()
    th.join(budget)
    with lock:
        if th.is_alive():
            box["gone"] = True
            return None, budget
    if "err" in box:
        raise box["err"]
    return box.get("ans"), time.perf_counter() - t0
# readers that MEASURE / count the traced linework; 'openings' (DR 19:59, d25 sl-086 Stoa's DfE PNG: its door count on the
# traced copy was dropped, 91 vs truth ~101, vlm_lora 70 @0.22 answered; per-room door counts = the vector PDF's on 14/17)
VECTOR_READERS = ("plan_rooms", "label_count", "line_rooms", "openings")
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


def _flagged_zero(ans: Answer) -> bool:
    """T1 21:07: T1's sanity guard turned an implausible 0 (windows on a facade) into 'none found' (value None + the
    reason + where we looked): still a reading, preferred over the PDF pass's dataset prior. False before T1's module."""
    try:
        from stoa.trust.sanity import flagged_zero

        return bool(flagged_zero(ans))
    except Exception:  # noqa: BLE001
        return False


def _no_reader(ans: Answer) -> bool:
    """The answer is the dataset prior (or nothing): no reader understood the drawing."""
    return ans.value is None or str(ans.solver or "").split("/")[-1] in ("prior", "dummy")
