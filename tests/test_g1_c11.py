"""G1 cycle 11 (22:23-): facade counts as structured evidence, VP's printed gross rows before the printed-total gate,
width / breit rewordings as lengths, schedule row counts."""
import sys
import time
import types

from stoa.contract import Answer, Evidence


def _fake_footprint(monkeypatch, ans):
    m = types.ModuleType("stoa.sheet.footprint")
    m.is_gross_area_question = lambda q: True
    m.footprint_answer = lambda path, q, page=0: ans
    monkeypatch.setitem(sys.modules, "stoa.sheet.footprint", m)


def test_footprint_printed_row_accepted_before_printed_total_gate(monkeypatch, tmp_path):
    # VP 22:06: EFA GF 'Overall GIFA / Ground: 1248m²' = the sheet's own number (was 7.0 m² from pdf_text behind the gate)
    from stoa import pipeline as P

    a = Answer(value=1248.0, unit="m2", confidence=0.85, solver="footprint",
               evidence=[Evidence(file="x.pdf", page=0, bbox=[0, 0, 1, 1], note="printed: Ground: 1248m2")],
               trace=["printed (VP): 'Overall GIFA Ground: 1248m 2' (this sheet's storey)"])
    _fake_footprint(monkeypatch, a)
    monkeypatch.setattr(P, "_printed_total", lambda path: "GIFA 1248 m2")
    out = P.Pipeline()._footprint_first(str(tmp_path / "x.pdf"), "What is the gross internal floor area?", [], time.perf_counter())
    assert out is not None and out.value == 1248.0 and out.solver == "pipeline/footprint"
    a.confidence = 0.5  # printed, but not surely the asked floor -> next reader
    assert P.Pipeline()._footprint_first(str(tmp_path / "x.pdf"), "GIFA?", [], time.perf_counter()) is None


def test_footprint_measured_outline_still_yields_to_a_printed_total(monkeypatch, tmp_path):
    from stoa import pipeline as P

    a = Answer(value=1670.8, unit="m2", confidence=0.6, solver="footprint", trace=["footprint (VP): outline …"])
    _fake_footprint(monkeypatch, a)
    monkeypatch.setattr(P, "_printed_total", lambda path: "TOTAL = 26,562 SF")
    trace = []
    assert P.Pipeline()._footprint_first(str(tmp_path / "x.pdf"), "total floor area?", trace, time.perf_counter()) is None
    assert "prints a total" in trace[-1]
    monkeypatch.setattr(P, "_printed_total", lambda path: None)
    assert P.Pipeline()._footprint_first(str(tmp_path / "x.pdf"), "total floor area?", [], time.perf_counter()).value == 1670.8


def test_schedule_question_words():
    from stoa.pipeline import _SCHEDULE_ATTR, _SCHEDULE_Q

    for q in ("How many doors are listed in the door schedule?",
              "According to the door schedule, how many doors does module 07 have?",
              "How many doors does the door schedule list for module 05?"):
        assert _SCHEDULE_Q.search(q) and not _SCHEDULE_ATTR.search(_SCHEDULE_Q.sub(" ", q)), q
    assert _SCHEDULE_ATTR.search(_SCHEDULE_Q.sub(" ", "How many doors in the door schedule are fire rated?"))
    assert not _SCHEDULE_Q.search("How many doors are there on the plan?")


A602 = ".stoa/neutral/x1sl/d5e6b1c4d3/d5e6b1c4d3.pdf"  # X1's AFCEE A-103 + A-602 set (page 2 = A-602)


def test_schedule_row_counts_on_the_a602_page(tmp_path):
    import os
    import shutil

    import pytest

    if not os.path.exists(A602):
        pytest.skip("X1's A-602 copy not on this machine")
    import fitz

    from stoa.pipeline import Pipeline

    src = fitz.open(A602)
    one = fitz.open()
    one.insert_pdf(src, from_page=1, to_page=1)
    p = tmp_path / "a602.pdf"
    one.save(p)
    pipe = Pipeline()
    a = pipe.solve(str(p), "How many doors are listed in the door schedule?")
    # X1 sl-254 gold 95 (F8's rows here; DR's openings once its door_schedule is in the tree)
    assert a.value == 95 and a.solver in ("pipeline/schedule_rows", "pipeline/openings") and len(a.evidence) >= 95
    assert pipe.solve(str(p), "According to the door schedule, how many doors does the support module (module 07) "
                              "have?").value == 6  # sl-249 gold 6
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_area_and_length_rewordings():
    from stoa.pipeline import Pipeline, _table_wording, _terse_length
    from stoa.router.intent import parse

    for q in ("How much floor space does the laundry room take up?", "module 01 total sf",
              "Sum up all the room areas in the entry vestibule module.",
              "Paljonko asuntolatilaa (dormitory) on yhteensä kaikissa kolmessa siivessä?"):
        assert parse(q).task == "area", q
    assert parse("What are all the rooms on the ground level, including their areas and ceiling heights?").task == "text"
    assert _terse_length("pump room overall width") == "How wide is the pump room overall?"
    assert _terse_length("door width") is None and _terse_length("How wide is the hall?") is None
    assert _table_wording("What's the total common area on this floor, counting its circulation and utility rooms "
                          "too?") == "What's the total common space area on this floor, including its circulation and " \
                                     "utility rooms?"
    assert _table_wording("common space total incl circulation").endswith(" area?")
    q, _note, _c = Pipeline()._front_end("Paljonko asuntolatilaa (dormitory) on yhteensä kaikissa kolmessa siivessä?")
    assert q == "What is the total floor area of the dormitory across all wings?"


def test_size_check_vetoed_reading_is_not_the_headline(monkeypatch):
    # QA 22:40: ours 1274 = the sheet's printed 'Overall GIFA 1274m2' (size check) vs Gemini 415 = gold
    from stoa.agent import vision as V

    g = Answer(value=415.0, unit="m2", confidence=0.5, solver="gemini", trace=["Gemini 3.1 Pro vision reading: 415 m2"])
    monkeypatch.setattr(V, "read", lambda *a, **k: g.model_copy(deep=True))
    monkeypatch.setattr(V, "printed_backing", lambda *a, **k: None)
    monkeypatch.setattr(V, "measure_at_point", lambda *a, **k: None)
    monkeypatch.setattr(V, "measure_raster_at_point", lambda *a, **k: None)
    ours = Answer(value=1274.0, unit="m2", confidence=0.1, solver="pipeline/raster",
                  trace=["intent: area", "size check: 1274 is the sheet's printed 'Overall GIFA 1274m2' (a gross / "
                                         "target total), not the asked room(s)"])
    out = V.vision_step("/tmp/up/efa_2f_scan.png", "Add up the areas of Science Lab 1 to Science Lab 5.", ours,
                        why="our readers found nothing usable")
    assert out.value == 415.0 and any("rejected" in m for m in out.missing)


def test_area_tag_qualifier_beats_generic_head_noun():
    from stoa.solvers.area_tags import _name_score, _words  # E1 22:32: dining hall -> 'Dining', not 'Sports Hall'

    exp = _words("What is the floor area of the dining hall?")
    assert _name_score(exp, {"name": "Dining"}) > _name_score(exp, {"name": "Sports Hall"})


S100F = ".stoa/neutral/x1sl/d1f20c81a3/d1f20c81a3.pdf"  # Stoa's S-100F + M-101A sheet (X1 copy)


def test_drawing_words_and_slab_wordings(tmp_path):
    import os
    import shutil

    import pytest

    from stoa.pipeline import _drawing_words
    from stoa.router.intent import parse

    assert _drawing_words("What's the footprint of the generator set foundation?")[0] == \
        "What's the footprint of the genset foundation?"
    for q in ("What is the total slab area?",  # captain 22:29: both wordings are the 3-slab aggregate (302.68)
              "What is the total slab area (all slab-on-grade zones, inside the foundation walls)?"):
        assert parse(q).task == "area" and parse(q).target == "slab"
    if not os.path.exists(S100F):
        pytest.skip("S-100F copy not on this machine")
    from stoa.pipeline import Pipeline

    p = tmp_path / "s100f.pdf"
    shutil.copy(S100F, p)
    pipe = Pipeline()
    a = pipe.solve(str(p), "What's the footprint of the generator set foundation?")
    assert abs(float(a.value) - 26.486) < 0.3  # T1 22:42 / X1 rp-016 (was 'SEE A1/S-513' / 419 m²)
    for q in ("What is the total slab area?",
              "What is the total slab area (all slab-on-grade zones, inside the foundation walls)?"):
        assert abs(float(pipe.solve(str(p), q).value) - 302.68) / 302.68 < 0.02, q


def test_ocr_tolerant_label_match():
    from stoa.solvers.label_count import _matches, _ocr_matches  # O1 22:28: HAER dc1026 OCR slips

    stems = ["strainer", "motor"]
    for t in ("STRAIMER", "DKHSTRAINER", "DKHSTRAIMER", "NOTOR"):
        assert _matches(t, stems) is None and _ocr_matches(t, stems), t
    assert _ocr_matches("ROTOR", stems) is None and _ocr_matches("MOTOR ROOM", stems) is None


def test_agent_number_in_another_unit_is_the_readers_printed_reading():
    from stoa.pipeline import Pipeline
    from stoa.router.intent import parse

    q = "What is the area of the existing GC1 lined catchment?"  # D1 22:24 hi76c1: printed 128,878 SQ FT
    p = Pipeline()
    ra = Answer(value=128878.0, unit="ft2", confidence=0.4, solver="raster", trace=["raster"])
    out = p._vs_unsure(Answer(value=128778.0, unit="m2", confidence=0.32, trace=["a"], solver="pipeline/agent"),
                       ("raster", ra), parse(q), q)
    assert out.value == 128878.0 and out.unit == "ft2" and out.solver == "pipeline/raster"
