"""Areas in the hybrid upload routing (captain 15:43): our outline x our scale is the value, Gemini only locates the
room and cross-checks; word answers to numeric questions escalate. No Gemini call is made here (read() is mocked)."""

from __future__ import annotations

from stoa.agent import vision as V
from stoa.contract import Answer, Evidence

UP = ".stoa/neutral/x1/0000000000/plan.png"  # an upload path (outside data/)


def test_words_only():
    for v in ("Not determinable", "Not specified", "BRH 87.50", "3,50 x 3,00", "4.8 m² (approx)"):
        assert V._words_only(v), v
    for v in ("12.5", "12,5 m²", "~7 m", "3 ; 43.82", 12.5, None, ["a"]):
        assert not V._words_only(v), v


def test_wanted_words_and_area_crosscheck():
    w = Answer(value="Not determinable", confidence=0.3, solver="pipeline/raster")
    assert V.wanted(w, "What is the floor area of the room at the top right?", UP).startswith("no number")
    weak = Answer(value=9.84, unit="m2", confidence=0.4, solver="pipeline/plan_rooms",
                  evidence=[Evidence(file=UP, page=0, bbox=[0, 0, 10, 10], bbox_space="image_px")])
    assert V.wanted(weak, "How big is the comms room?", UP).startswith("area cross-check")
    sure = Answer(value=16.8, unit="m2", confidence=0.85, solver="pipeline/area_tags",
                  evidence=[Evidence(file=UP, page=0, bbox=[0, 0, 10, 10], bbox_space="image_px")])
    assert V.wanted(sure, "How big is the comms room?", UP) is None
    # dataset drawings keep their readers (the demo tour)
    assert V.wanted(weak, "How big is the comms room?", "data/cubicasa5k/x/F1_original.png") is None


def _gemini(value, point=None, question="q"):
    g = Answer(value=value, unit="m2", confidence=0.45, solver="pipeline/gemini_vision",
               trace=["Gemini 3.1 Pro vision reading: …"], evidence=[])
    if point:
        V._POINTS[(UP, question)] = (*point, "image_px")
    return g


def _ours(v=10.0, conf=0.4):
    sq = [[0, 0], [100, 0], [100, 100], [0, 100]]
    return Answer(value=v, unit="m2", confidence=conf, solver="pipeline/plan_rooms", trace=["ours"],
                  evidence=[Evidence(file=UP, page=0, bbox=[0, 0, 100, 100], bbox_space="image_px", polygon=sq)])


def test_crosscheck_keeps_our_value():
    a = V._area_crosscheck(UP, "q1", _ours(), _gemini(10.5, (50, 50), "q1"), "h: ")
    assert a.value == 10.0 and a.confidence == 0.6 and any("agree" in s for s in a.assumptions)
    b = V._area_crosscheck(UP, "q2", _ours(), _gemini(25.0, (50, 50), "q2"), "h: ")
    assert b.value == 10.0 and b.confidence == 0.4 and any("visual estimate" in m for m in b.missing)
    c = V._area_crosscheck(UP, "q3", _ours(), _gemini(10.2, (500, 500), "q3"), "h: ")
    assert c.value == 10.0 and c.confidence <= 0.3 and any("different room" in m for m in c.missing)


def test_never_geminis_area_alone(monkeypatch):
    q = "What is the floor area of the room at the top right?"
    monkeypatch.setattr(V, "read", lambda *a, **k: _gemini(16.2, (50, 50), q))
    monkeypatch.setattr(V, "measure_at_point", lambda *a, **k: None)
    monkeypatch.setattr(V, "measure_raster_at_point", lambda *a, **k: None)
    ans = Answer(value="Not determinable", confidence=0.3, solver="pipeline/raster", trace=["raster"])
    out = V.vision_step(UP, q, ans, why="no number for a area question")
    assert out.solver == "pipeline/gemini_estimate" and out.confidence <= V.ESTIMATE_CAP
    assert any("scale" in m for m in out.missing)
    # with a measurement of ours at Gemini's point, that is the answer
    m = Answer(value=10.4, unit="m2", confidence=0.4, solver="pipeline/gemini_locate", trace=["traced"])
    monkeypatch.setattr(V, "measure_raster_at_point", lambda *a, **k: m)
    out = V.vision_step(UP, q, Answer(value="Not determinable", confidence=0.3, solver="pipeline/raster",
                                      trace=["raster"]), why="no number for a area question")
    assert out.value == 10.4 and out.solver == "pipeline/gemini_locate"


def test_dims_pair_answers_become_area_or_side():
    from stoa.pipeline import _dims_guard, _length_guard

    a = _dims_guard("How big is the living room (Wohnzimmer)?", Answer(value="3,50 x 3,00", confidence=0.2), UP)
    assert (a.value, a.unit) == (10.5, "m2") and a.confidence <= 0.3
    w = _dims_guard("How wide is the kitchen?", Answer(value="3,07 m x 4,50 m", confidence=0.5), UP)
    assert (w.value, w.unit) == (3.07, "m") and w.confidence <= 0.3
    assert _dims_guard("How many doors?", Answer(value="3 x 2", confidence=0.5), UP).value == "3 x 2"
    assert _dims_guard("How big?", Answer(value="3,50 x 3,00", confidence=0.2), "data/swiss/x.png").value == "3,50 x 3,00"
    s = _length_guard("How wide is the kitchen?", Answer(value="4.8 m²", confidence=0.7, solver="pipeline/room_graph"))
    assert s.confidence <= 0.3


def test_stair_notes():
    from stoa.solvers.label_count import _stair_answer

    one = [("21 Stg. 16.2/27.8", (0, 0, 1, 1)), ("BRH 87.50", (0, 0, 1, 1))]
    assert _stair_answer(one, "How many steps does the staircase have?", "x.png", "image_px").value == 21
    two = [("UP 16R", (0, 0, 1, 1)), ("3 R", (1, 1, 2, 2)), ('15 T @ 11"', (0, 0, 1, 1)), ('16 RISERS @ 7.5"', (2, 2, 3, 3))]
    assert _stair_answer(two, "How many risers does the main staircase have?", "x", "pdf_pt").value == 16
    assert _stair_answer(two, "How many risers lead up to the front porch?", "x", "pdf_pt").value == 3
    assert _stair_answer(two, "How many treads does the stair have?", "x", "pdf_pt").value == 15
    assert _stair_answer(one, "How many doors are there?", "x", "pdf_pt") is None
    assert _stair_answer([("ROOM 2R", (0, 0, 1, 1))], "How many steps?", "x", "pdf_pt") is None


def test_label_count_routed_for_upload_images_only():
    from stoa.pipeline import route
    from stoa.router.intent import parse

    q = "How many meeting rooms are on this level?"
    up = route(".stoa/neutral/x1/640f1a9d80/640f1a9d80.png", parse(q))
    assert up[0] == "label_count" and "vlm_lora" in up
    assert "label_count" not in route("data/cubicasa5k/high_quality/1/F1_original.png", parse(q))


def test_stair_page_picks_the_noted_page(tmp_path):
    import fitz

    from stoa.pipeline import _stair_page

    d = fitz.open()
    for txt in ("COVER", "ELEVATION", 'UP 16 RISERS @ 7.5"\n3 RISERS', '16 RISERS @ 7.5"'):
        d.new_page().insert_text((72, 72), txt)
    f = tmp_path / "set.pdf"
    d.save(f)
    assert _stair_page(f, "How many risers does the main staircase have?")[0] == 3
    assert _stair_page(f, "How many risers lead up to the front porch?")[0] == 2
    assert _stair_page(f, "How many doors are there?") is None


def test_piping_tag_families():
    from stoa.solvers.label_count import _tag_answer

    L = [("FP-1", (0, 0, 1, 1)), ("FP-2", (1, 0, 2, 1)), ("FP-1", (5, 5, 6, 6)), ("FSCV-1", (2, 2, 3, 3)),
         ("CV-1", (3, 3, 4, 4)), ("CV-2", (4, 4, 5, 5)), ("PG", (6, 6, 7, 7)), ("PG", (7, 7, 8, 8)),
         ("FSI-1", (0, 1, 1, 2)), ("FSR-1", (1, 1, 2, 2)), ("NON-SURGE CHECK VALVE (CV) W/ RATE OF FLOW", (9, 9, 10, 10))]
    assert _tag_answer(L, "How many fuel pumps (FP) are shown?", "x.pdf", "pdf_pt").value == 2  # FP-1 twice = 1 pump
    assert _tag_answer(L, "How many non-surge check valves are there?", "x.pdf", "pdf_pt").value == 2  # not FSCV-1
    assert _tag_answer(L, "How many filter separators are there?", "x.pdf", "pdf_pt").value == 2
    assert _tag_answer(L, "How many pressure gauges (PG) are tagged?", "x.pdf", "pdf_pt").value == 2
    assert _tag_answer(L, "How many bedrooms are there?", "x.pdf", "pdf_pt") is None


def test_image_lines_use_the_pdf_of_a_render(tmp_path):
    import fitz
    from PIL import Image

    from stoa.solvers.label_count import _image_lines, _render_source

    d = fitz.open()
    pg = d.new_page()
    for i, t in enumerate(("FP-1", "FP-2", "21 Stg. 16.2/27.8")):
        pg.insert_text((72, 72 + 20 * i), t)
    d.save(tmp_path / "sheet.pdf")
    Image.new("RGB", (100, 100), "white").save(tmp_path / "sheet.r144.png")
    src = _render_source(str(tmp_path / "sheet.r144.png"))
    assert src and src[1] == 2.0
    lines = _image_lines(str(tmp_path / "sheet.r144.png"))
    assert [t for t, _ in lines][:2] == ["FP-1", "FP-2"] and lines[0][1][0] > 100  # pdf_pt x 2 = render px
