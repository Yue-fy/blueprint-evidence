"""G1 cycle 12 (23:15-): untagged items named by a text callout count with their tag family (captain 23:07, expert
Ziying on Stoa's M-101A: 'How many pumps are there?' = 7 = FP-1..FP-5 + JOCKEY PUMP + PUMPHOUSE DRAIN PUMP)."""
from pathlib import Path

import pytest


def _piping_pdf(tmp_path, rotate: int = 0) -> str:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=1224, height=792)
    put = lambda x, y, t: page.insert_text((x, y), t, fontsize=8)  # noqa: E731
    put(72, 60, "M-101A PIPING PLAN")
    for k in range(1, 4):
        put(100 + 80 * k, 300, f"FP-{k}")
    put(500, 200, "JOCKEY PUMP")
    put(600, 200, "PUMPHOUSE")  # a two-line callout
    put(600, 209, "DRAIN PUMP")
    put(700, 200, '12" PUMP')  # the noun as a modifier: a pipe header
    put(700, 209, "DISCHARGE")
    put(700, 218, "HEADER")
    put(800, 200, "SUMP PUMP")
    put(800, 209, "SEE NOTE 4")  # a reference under the name: still one pump
    put(900, 200, "BLIND FLANGE")
    put(900, 209, "(TYP.)")
    put(180, 312, "FUEL PUMP")  # printed right under FP-1: names the tagged item, not a second pump
    put(100, 700, "8. CONSIDER USING VERTICAL INLINE CENTRIFUGAL PUMPS")
    put(100, 720, '2" DRAIN LINE TO SUMP PUMP')
    if rotate:
        page.set_rotation(rotate)
    p = tmp_path / f"m101_{rotate}.pdf"
    doc.save(p)
    return str(p)


@pytest.mark.parametrize("rotate", [0, 90])
def test_pumps_count_tags_plus_untagged_callouts(tmp_path, rotate):
    from stoa.solvers import get_solver

    pdf = _piping_pdf(tmp_path, rotate)
    a = get_solver("label_count").solve(pdf, "How many pumps are there?")
    assert a.value == 6, a.trace  # FP-1..3 + jockey + pumphouse drain + sump
    notes = " ".join(e.note for e in a.evidence)
    assert "JOCKEY PUMP" in notes and "PUMPHOUSE DRAIN PUMP" in notes and "SUMP PUMP" in notes
    assert "DISCHARGE" not in notes and "CONSIDER" not in notes and "FUEL PUMP" not in notes
    assert a.confidence >= 0.7  # vision.wanted(): a structured count < 0.7 is sent to Gemini on the demo
    assert any("tagged P/FP family alone = 3" in s for s in a.assumptions)
    assert any("3 tagged + 3 by callout" in t for t in a.trace)


def test_family_abbreviation_question_keeps_the_tag_count(tmp_path):
    from stoa.solvers import get_solver

    pdf = _piping_pdf(tmp_path)
    s = get_solver("label_count")
    assert s.solve(pdf, "How many fuel pumps (FP) are there?").value == 3
    assert s.solve(pdf, "How many fuel pumps are there?").value == 3  # 'FUEL PUMP' callout = FP-1


def test_typ_and_modifier_callouts_are_rejected(tmp_path):
    from stoa.solvers.label_count import callout_items, text_lines

    pdf = _piping_pdf(tmp_path)
    got, rej = callout_items(text_lines(pdf), "How many blind flanges are there?", pdf, "pdf_pt")
    assert got == [] and any("TYP" in r for r in rej)
    got, _ = callout_items(text_lines(pdf), "How many headers?", pdf, "pdf_pt")
    assert got == []  # '12" PUMP DISCHARGE HEADER' starts with a pipe size


M101 = Path(".stoa/neutral/x1sl/acf1f07a2d/acf1f07a2d.pdf")  # Stoa's M-101A (X1 copy)
M101_PNG = Path(".stoa/neutral/x1sl/c5a6f30db7/c5a6f30db7.png")


@pytest.mark.skipif(not M101.exists(), reason="Stoa's M-101A copy not on this machine")
def test_m101a_pumps_expert_seven():
    from stoa.solvers import get_solver

    s = get_solver("label_count")
    a = s.solve(str(M101), "How many pumps are there?")
    assert a.value == 7, a.trace
    assert {"JOCKEY PUMP", "PUMPHOUSE DRAIN PUMP"} <= {e.note.split("'")[1] for e in a.evidence if "callout" in e.note}
    assert s.solve(str(M101), "How many fuel pumps (FP) are there?").value == 5
    assert s.solve(str(M101), "How many check valves are there?").value == 5  # the legend row is not a valve


@pytest.mark.skipif(not M101_PNG.exists(), reason="Stoa's M-101A PNG copy not on this machine")
def test_m101a_png_pumps_expert_seven():
    from stoa.solvers import get_solver
    from stoa.solvers.label_count import _image_lines

    if _image_lines(str(M101_PNG)) is None:
        pytest.skip("the PNG's text layer is not cached here")
    assert get_solver("label_count").solve(str(M101_PNG), "How many pumps are there?").value == 7


def test_scope_absent_withholds_an_unlocated_picture_guess():
    # DR 23:07 / captain 23:04: AFCEC T plan 'doors in the south wing' -> openings 'no distinct south wing', then
    # vlm_lora 12 @0.105 with no boxes was the answer
    from stoa.contract import Answer, Evidence
    from stoa.pipeline import _scope_absent

    why = {"openings": "the building has no distinct south wing: walking in from the bottom of the sheet, the footprint "
                       "never narrows into an arm (it is 92% of the building)", "door_detect": "no scale on the image"}
    guess = Answer(value=12, unit="count", confidence=0.105, solver="vlm_lora")
    trace = []
    out = _scope_absent("count", guess, why, trace)
    assert out.value is None and out.missing[0].startswith("not counted: the openings reader did not find") and "no distinct south wing" in out.missing[0]
    assert "12" in out.missing[1] and out.clarify["click"] == "room" and "withheld" in trace[-1]
    # a located reading, a sure one, another task or no scope finding: unchanged
    boxed = Answer(value=12, unit="count", confidence=0.2, solver="raster",
                   evidence=[Evidence(file="x.png", page=0, bbox=[0, 0, 1, 1], exact=True)])
    assert _scope_absent("count", boxed, why, []).value == 12
    assert _scope_absent("count", Answer(value=12, unit="count", confidence=0.5, solver="vlm_lora"), why, []).value == 12
    assert _scope_absent("area", Answer(value=12.0, unit="m2", confidence=0.1, solver="prior"), why, []).value == 12.0
    assert _scope_absent("count", guess, {"door_detect": "no scale on the image"}, []).value == 12


def test_size_vetoed_reading_is_withheld_when_nothing_replaced_it():
    # QA 22:55 r17 / S0 scan_dev sc-083: 'combined area of Science Labs 1 to 5' -> raster 1274 m2 @0.1 = 'Overall GIFA'
    from stoa.contract import Answer, Evidence
    from stoa.pipeline import _vetoed_headline

    veto = "1274 is the sheet's printed 'Overall GIFA 1274m2' (a gross / target total), not the asked room(s)"
    a = Answer(value=1274.0, unit="m2", confidence=0.1, solver="pipeline/raster", missing=[veto],
               evidence=[Evidence(file="x.png", page=0, bbox=[0, 0, 1, 1], note="printed '1274m2'")],
               trace=["raster: …", f"size check: {veto}"])
    out = _vetoed_headline(a)
    assert out.value is None and out.evidence == [] and out.missing[0].startswith("not found: the only reading, 1274 m2")
    assert veto not in out.missing[1:] and out.clarify["click"] == "room" and out.trace[-1].startswith("withheld:")
    mm = "7200 is printed on this metric sheet as a bare number, i.e. a dimension in mm (7.2 m), not an area"
    assert _vetoed_headline(a.model_copy(update={"value": 7200.0, "trace": [f"size check: {mm}"]})).value is None
    # replaced by the escalation (another number), sure, or the paper-size estimate check: kept
    assert _vetoed_headline(a.model_copy(update={"value": 415.0})).value == 415.0
    assert _vetoed_headline(a.model_copy(update={"confidence": 0.5})).value == 1274.0
    ext = "1401.45 m2 is larger than the whole sheet at its printed scale (~1,049 m²): not a reading of this drawing"
    assert _vetoed_headline(a.model_copy(update={"value": 1401.45, "confidence": 0.05,
                                                 "trace": [f"size check: {ext}"]})).value == 1401.45


def _lines_pdf(tmp_path, items) -> str:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=1224, height=792)
    for x, y, t in items:
        page.insert_text((x, y), t, fontsize=8)
    p = tmp_path / "haer.pdf"
    doc.save(p)
    return str(p)


def test_acronym_tags_plural_labels_and_notes(tmp_path):
    # D1 22:52 (HAER hi76c2 / hi76c11): 'SLOW SAND FILTERS' (1 label, leaders to SSF1 / SSF2) = 2; 'CHLORINATORS' is
    # not one chlorinator; 'NOTES:' + '1.' .. '5.' = 5 notes (was 1 = the heading)
    from stoa.solvers import get_solver

    pdf = _lines_pdf(tmp_path, [(300, 100, "SSF2"), (400, 110, "SLOW SAND FILTERS"), (300, 140, "SSF1"),
                                (400, 200, "CHLORINATORS"), (800, 60, "NOTES:")] +
                     [(800, 80 + 20 * k, f"{k}. NOTE TEXT NUMBER {k}") for k in range(1, 6)] + [(800, 300, "7. LOOSE")])
    s = get_solver("label_count")
    a = s.solve(pdf, "How many slow sand filters are there?")
    assert a.value == 2 and "SSF" in a.trace[0] and not any("callout" in e.note for e in a.evidence)
    assert s.solve(pdf, "How many chlorinators are there?").value is None
    n = s.solve(pdf, "How many notes are listed on the sheet?")
    assert n.value == 5 and len(n.evidence) == 5
    # room vocabulary keeps its plural words: 'TOILETS' is one toilet room label
    room = _lines_pdf(tmp_path, [(100, 100, "TOILETS"), (300, 100, "WC")])
    assert s.solve(room, "How many toilets are there?").value == 2


def test_piping_reader_before_schedule_when_a_sheet_is_named(tmp_path):
    # stoa-like sl-180: 'On sheet M-101A, how many distinct FSCV-tagged components are shown?' -> schedule's LLM table
    # op 2 @0.5 was accepted before Erin's piping reader (7 @0.98)
    from stoa.pipeline import route
    from stoa.router.intent import parse

    pdf = _piping_pdf(tmp_path)
    order = route(pdf, parse("On sheet M-101A, how many distinct FSCV-tagged components are shown?"))
    assert "piping" in order and "schedule" in order and order.index("piping") < order.index("schedule")


def test_area_tags_sum_only_tags_named_like_the_subject():
    # X1 23:16 (EFA colour sheet 2012 secondary type 2 FF): English 1..9 + ICT Classroom + English Staff Work Room = 578
    from stoa.solvers.area_tags import pure_subset

    def t(name):
        return {"name": name, "number": None, "value": 55.0, "unit": "m2", "text": "55 m2"}

    pool = [t(f"English {k}") for k in range(1, 4)] + [t("ICT Classroom"), t("English Staff Work Room"), t("Eng. Store"),
                                                         t("Art 1 - Small"), t("Art 3 - 3D"), t("Art Store"), t("MFL 1"),
                                                         t("MFL Store"), t("MFL SWR"), t("ICT Classroom")]
    names = lambda ts: [x["name"] for x in ts]  # noqa: E731
    assert names(pure_subset(["english", "classrooms"], pool)) == ["English 1", "English 2", "English 3"]
    assert names(pure_subset(["ict", "classrooms"], pool)) == ["ICT Classroom", "ICT Classroom"]
    assert names(pure_subset(["art", "rooms"], pool)) == ["Art 1 - Small", "Art 3 - 3D"]
    assert names(pure_subset(["mfl", "rooms"], pool)) == ["MFL 1"]
    assert pure_subset(["offices"], pool) is None  # a generic head alone: nothing to test, the old subset stays


EFA_C2 = Path(".stoa/neutral/x1sl/d7c5a1e97b/d7c5a1e97b.pdf")  # X1's EFA colour sheet copy


@pytest.mark.skipif(not EFA_C2.exists(), reason="X1's EFA colour sheet copy not on this machine")
def test_efa_colour_sheet_sums_and_size_count():
    from stoa.solvers import get_solver

    s = get_solver("area_tags")
    assert s.solve(str(EFA_C2), "What is the total floor area of the English classrooms?").value == 495.0
    assert s.solve(str(EFA_C2), "What is the combined area of all the MFL rooms on this floor?").value == 330.0
    assert s.solve(str(EFA_C2), "What is the total area of the art rooms?").value == 272.0
    assert s.solve(str(EFA_C2), "How many rooms on this floor are 55 m2?").value == 17
    ict = s.solve(str(EFA_C2), "What is the total area of the ICT classrooms?")
    # never the whole-sheet sum for a one-tag subset: either the gold 2 x 62 = 124 (VP e036c9c3 labels.dedupe keeps both
    # 'ICT Classroom 62 m2' tags) or a low-confidence guess
    assert ict.value != 2071.0
    assert ict.value == 124.0 or ict.confidence <= 0.5


def test_area_tags_sum_needs_the_subjects_qualifiers(tmp_path):
    # D1 23:22 (HAER HI-76 C2): 'total roof catchment area of the two rainshed buildings' = the two LINED GROUND catchment
    # tags (179,878 @0.70; gold 86,157): no tag carries 'roof' -> abstain; 'the lined ground catchments' still sums them
    import pymupdf

    from stoa.solvers import get_solver

    doc = pymupdf.open()
    page = doc.new_page(width=1224, height=792)
    for y, t in ((100, "LINED GROUND CATCHMENT - GC1"), (300, "INSTALL LINED GROUND CATCHMENT - GC2")):
        page.insert_text((100, y), t, fontsize=8)
    page.insert_text((100, y + 10), "51,000 SQ FT", fontsize=8)
    page.insert_text((100, 110), "128,878 SQ FT", fontsize=8)
    page.insert_text((600, 100), "OFFICE", fontsize=8)
    page.insert_text((600, 110), "1,200 SQ FT", fontsize=8)
    p = tmp_path / "c2.pdf"
    doc.save(p)
    s = get_solver("area_tags")
    roof = s.solve(str(p), "What is the total roof catchment area of the two rainshed buildings?")
    assert roof.value is None
    lined = s.solve(str(p), "What is the total area of the lined ground catchments?")
    assert lined.value is None or abs(lined.value - 179878) < 1, lined.trace
