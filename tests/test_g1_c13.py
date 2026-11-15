"""G1 cycle 13 (00:38-): the captain's AUDIT RECHECK-0026 items."""

from stoa.agent import vision as V
from stoa.contract import Answer, Evidence


def _boxes(n, exact=True):
    return [Evidence(file="up/m.png", page=0, bbox=[i * 10, 0, i * 10 + 5, 5], bbox_space="image_px", exact=exact,
                     note=f"FP-{i + 1}") for i in range(n)]


def test_located_confident_count_gets_no_hosted_second_look():
    # RECHECK-0026 (1): M-101A PNG 'how many fuel pumps labelled FP' = piping_img 5 @0.8 (5 marks) waited 25 s on Gemini
    a = Answer(value=5, unit="count", confidence=0.8, solver="pipeline/piping_img", evidence=_boxes(5))
    q = "On this piping drawing, how many fuel pumps labelled FP are installed?"
    assert V.settled(a, q)
    assert V.wanted(a, q, "upload/x/m.png") is None
    # unlocated or unsure answers keep the hosted look
    assert V.settled(Answer(value=5, unit="count", confidence=0.8, solver="pipeline/vlm_lora"), q) is None
    weak = Answer(value=5, unit="count", confidence=0.45, solver="pipeline/piping_img", evidence=_boxes(5))
    assert V.settled(weak, q) is None and V.wanted(weak, q, "upload/x/m.png")


def test_measured_area_at_unknown_scale_keeps_the_cross_check():
    sq = [[0, 0], [100, 0], [100, 100], [0, 100]]
    ev = [Evidence(file="up/x.png", page=0, bbox=[0, 0, 100, 100], bbox_space="image_px", polygon=sq,
                   note="Kitchen: 35.77 m² traced at 1:200 (approximate)")]
    q = "How big is the kitchen?"
    known = Answer(value=35.77, unit="m2", confidence=0.55, solver="pipeline/plan_rooms", evidence=ev,
                   assumptions=["scale: 1:200 printed on the sheet; traced from the wall lines (approximate)"])
    assert V.settled(known, q).startswith("measured by plan_rooms")
    guessed = known.model_copy(deep=True)
    guessed.assumptions = ["scale: 1:200 printed on the sheet", "no scale bar; the sheet's paper size is estimated"]
    assert V.settled(guessed, q) is None
    raster = known.model_copy(deep=True)
    raster.solver = "pipeline/raster"  # an element box x a scale bar is not a measured outline
    assert V.settled(raster, q) is None


def test_piping_image_leaves_out_ocr_doubtful_ids():
    # RECHECK-0026 (4): the audit's cold OCR read of M-101A PNG: 23 real PS types + 'PS-0' (a 15 px box; the rest ~36 px)
    from types import SimpleNamespace as W

    from stoa.solvers.piping_image import _ocr_doubtful

    items = [(f"PS-{n}", W(bbox=(100.0 * n, 1000.0, 100.0 * n + 90, 1036.0))) for n in [1, *range(3, 25)]]
    items.insert(5, ("PS-0", W(bbox=(5098.8, 1584.4, 5186.2, 1599.9))))
    kept, out = _ocr_doubtful(items)
    assert len(kept) == 23 and out and "PS-0" in out[0]
    # a series that really starts at 0 (or a short one) is kept
    zero = [(f"V-{n}", W(bbox=(0, 0, 50, 30))) for n in range(0, 6)]
    assert len(_ocr_doubtful(zero)[0]) == 6
    assert len(_ocr_doubtful(items[:3])[0]) == 3


def test_skirting_order_answers_from_a_finished_takeoff_without_hosted_calls():
    # RECHECK-0026 (2): 'How much skirting should we order?' -> empty clarify, then Gemini '≈ 1,000 m · rough estimate'
    import os

    import pytest

    from stoa.pipeline import _skirting_which_room as f
    from stoa.pipeline import _takeoff_of

    png = ".stoa/neutral/x1sl/71088f70ee/71088f70ee.s6000.png"  # X1's copy of Stoa's DfE L00 PNG
    if not os.path.exists(png) or not isinstance(_takeoff_of(png)[0], dict):
        pytest.skip("no finished takeoff of the DfE L00 PNG on this machine")
    a = f("How much skirting should we order?", png)
    assert a.solver == "pipeline/takeoff" and a.value and a.value > 500 and len(a.evidence) >= 20
    assert len(a.clarify["options"]) >= 2 and a.clarify["options"][0]["value"] == a.value
    assert f("What length of skirting is needed for all rooms?", png).solver == "pipeline/takeoff"
    for q in ("How much skirting for the north wing?", "How much skirting do we need for the laundry?"):
        assert f(q, png) is None, q


def test_clarify_and_takeoff_answers_get_no_hosted_reading():
    from stoa.agent.escalate import escalate
    from stoa.contract import Answer

    a = Answer(value=None, unit="m", confidence=0.0, solver="pipeline/clarify", trace=["ask"])
    out = escalate(None, "upload/x/a.png", "How much skirting should we order?", a, gemini=True, verify=True)
    assert out.value is None and out.trace[-1].startswith("no hosted reading")


def test_cold_text_guess_is_shown_as_not_counted_yet():
    # RECHECK-0026 (3): sl-036 DfE PNG cold batch, 'How many group rooms on level 00?' -> vlm_lora 12 @0.35, no boxes
    from stoa.pipeline import _cold_guess

    g = Answer(value=12, unit="count", confidence=0.35, solver="vlm_lora")
    why = {"stated_count": "no text layer read yet",
           "label_count": "a large image whose text has not been read yet (a cold reading costs minutes; the image "
                          "readers read it)"}
    tr: list = []
    out = _cold_guess("count", g, why, tr)
    assert out.value == 12 and out.confidence <= 0.2 and out.assumptions[0].startswith("not counted yet")
    assert tr and tr[-1].startswith("cold-text check")
    # a located count, another reason, or an area: unchanged
    boxed = Answer(value=12, unit="count", confidence=0.35, solver="vlm_lora",
                   evidence=[Evidence(file="a.png", bbox=[0, 0, 1, 1], bbox_space="image_px")])
    assert _cold_guess("count", boxed, why, []).confidence == 0.35
    assert _cold_guess("count", g, {"label_count": "not a 'how many <room type>' question"}, []).confidence == 0.35
    assert _cold_guess("area", g, why, []).confidence == 0.35


def test_plural_sum_and_total_space_wordings_are_area_questions():
    # E1 / P1 00:19: picture uploads only get the traced vector pass for task 'area'
    from stoa.pipeline import _COUNT_FILLER
    from stoa.router.intent import infer_task

    for q in ("Sum of all slab areas?", "Total circulation space?", "What is the combined floor space of the ICT rooms?",
              "Add up the areas of the classrooms"):
        assert infer_task(q)[0] == "area", q
    for q in ("What is the space breakdown by level, including total spaces and floor areas?",
              "What is the overall distribution of MEP terminals in the building, including total counts, average per "
              "space, and breakdown by type?", "What are the floor areas of the rooms?"):
        assert infer_task(q)[0] != "area", q
    # H 00:18 (6): 'Kuinka monta ovea pohjapiirustuksessa on?' (= in the floor plan) is filler around the counted noun
    assert _COUNT_FILLER.sub(" ", "Kuinka monta ovea pohjapiirustuksessa on?").split() == ["ovea", "?"]


def test_whole_floor_questions_answer_from_a_finished_takeoff():
    # UX M19 / F4 23:49: DfE L00 PNG 'total floor area' 9,965 / 'rooms' 7 / 'largest room' 365.49 beside the card's takeoff
    import os

    import pytest

    from stoa.pipeline import _takeoff_floor, _takeoff_of

    png = ".stoa/neutral/x1sl/71088f70ee/71088f70ee.s6000.png"
    if not os.path.exists(png) or not isinstance(_takeoff_of(png)[0], dict):
        pytest.skip("no finished takeoff of the DfE L00 PNG on this machine")
    a = _takeoff_floor("What is the total floor area?", png, "area")
    assert a.solver == "pipeline/takeoff" and 1000 < a.value < 3000 and len(a.evidence) > 30
    assert _takeoff_floor("How many rooms are there?", png, "count").value > 50
    big = _takeoff_floor("How big is the largest room?", png, "area")
    assert "Multi-Purpose Hall" in big.evidence[0].note
    for q, t in (("What is the gross floor area of this floor?", "area"), ("How many group rooms are there?", "count"),
                 ("What is the floor area of the kitchen?", "area")):
        assert _takeoff_floor(q, png, t) is None, q


def test_key_value_table_rows_are_named_by_their_keys():
    # X1 00:00: EFA colour-sheet scans 'According to the GIFA table, how big is the second floor?' 4055 (Ground) -> 2253
    from stoa.solvers.area_tags import _row_keys

    lines = [("Overall GIFA", (332, 3130, 516, 3168)), ("Ground:", (330, 3191, 426, 3224)), ("4055m²", (461, 3195, 553, 3224)),
             ("First:", (331, 3222, 392, 3251)), ("2302m²", (461, 3223, 553, 3252)), ("Second:", (330, 3249, 426, 3280)),
             ("2253m²", (461, 3252, 553, 3281)), ("Total:", (329, 3278, 396, 3311)), ("8610m²", (461, 3281, 552, 3310))]
    tags = [{"name": "Overall GIFA" if v == 4055 else None, "number": None, "value": v, "unit": "m2", "text": f"{v}m²",
             "bbox": list(b), "tag_bbox": list(b)} for t, b in lines if t.endswith("m²") for v in [int(t[:-2])]]
    names = {t["value"]: t["name"] for t in _row_keys(tags, lines)}
    assert names == {4055: "Ground floor", 2302: "First floor", 2253: "Second floor", 8610: "Total"}
    # a lone 'Area:' label beside one room tag is no table
    one = [{"name": "Office", "number": None, "value": 12, "unit": "m2", "text": "12m²", "bbox": [100, 0, 150, 20],
            "tag_bbox": [100, 0, 150, 20]}]
    assert _row_keys(one, [("Area:", (40, 0, 95, 20))])[0]["name"] == "Office"


def test_type_tag_door_count_regex():
    from stoa.pipeline import _TYPE_TAG_Q

    assert _TYPE_TAG_Q.search("How many type A doors are on the second floor plan?")
    assert _TYPE_TAG_Q.search("How many doors are tagged on the second floor plan in total?")
    assert not _TYPE_TAG_Q.search("How many doors are there?")


def test_count_question_answered_with_words_is_no_answer():
    from stoa.pipeline import _wordy_quantity

    q = "How many bedrooms does the largest apartment have?"
    assert _wordy_quantity(q, Answer(value="apartment A", unit=None, confidence=0.009, solver="agent"))
    assert not _wordy_quantity(q, Answer(value="2", unit="count", confidence=0.5, solver="agent"))
    assert not _wordy_quantity("Which apartment is the largest?", Answer(value="apartment A", confidence=0.4, solver="agent"))


def test_living_room_is_not_the_dwellings_living_area():
    # X1 01:05 / 01:16: USDA farmhouse 'Living area 960' (the house) vs 'LIVING ROOM 15'-2" x 11'-6"' (174.42 ft2)
    from stoa.pipeline import _living_mixup

    row = Answer(value=960.0, unit="ft2", confidence=0.65, solver="pdf_text",
                 evidence=[Evidence(file="a.pdf", page=0, bbox=[0, 0, 1, 1], bbox_space="pdf_pt",
                                    note="text: Square Feet / 960 / Living area / Carport and storage 288")])
    room = Answer(value=174.42, unit="ft2", confidence=0.7, solver="room_size",
                  evidence=[Evidence(file="a.pdf", page=0, bbox=[0, 0, 1, 1], bbox_space="pdf_pt",
                                     note="LIVING ROOM 15 ft 2 in x 11 ft 6 in")])
    assert _living_mixup("What is the floor area of the living room?", row)
    assert not _living_mixup("What is the living area of this house in square feet?", row)
    assert _living_mixup("What is the living area of this house in square feet?", room)
    assert not _living_mixup("What is the floor area of the living room?", room)


def test_answer_shape_guards_multi_dimension_and_door_size():
    # X1 23:35 (b) / (c), kinds from the wild sealed US Revit sheets
    from stoa.pipeline import _DOOR_SIZE, _length_guard

    inch = chr(34)
    multi = Answer(value=f"11 3/8{inch} 2'-6{inch} 6 5/8{inch}", unit=None, confidence=0.65, solver="pipeline/pdf_text")
    assert _length_guard("How long is the countertop?", multi).confidence <= 0.3
    one = Answer(value=f"2'-6{inch}", unit=None, confidence=0.65, solver="pipeline/pdf_text")
    assert _length_guard("How wide is the door?", one).confidence == 0.65
    pair = Answer(value="12.80 m x 5.08 m", unit=None, confidence=0.65, solver="pipeline/lengths")
    assert _length_guard("How long is the room?", pair).confidence == 0.65  # an 'A x B' extent is left to the readers
    assert _DOOR_SIZE.search(f"2'-10{inch} wide x 6'-8{inch} MIN high")
    assert not _DOOR_SIZE.search(f"12'-0{inch} x 14'-6{inch}")


def test_bracketed_area_tag_is_a_tag():
    # F5 23:45: DfE 2012 baseline sheets print the room area in brackets under its name: '(55.0 m2)'
    from stoa.solvers.area_tags import _TAG

    for t in ("(55.0 m2)", "[69.0 m²]", "55.0 m2", "Area: 12 m2", "2,911 SF"):
        assert _TAG.match(t), t
    for t in ("GF Area 1645m2", "(55.0 m2) x 2"):
        assert not _TAG.match(t), t


def test_one_labelled_room_in_a_drawn_box(monkeypatch):
    # UX M23: a box over the Multi-Purpose Hall ('Multi-Purpose' / 'Hall' on two lines) + 'Area of the room in here'
    from stoa.agent import click as C

    fake = [("Multi-Purpose", (2959, 2776, 3045, 2790)), ("Hall", (2991, 2792, 3014, 2805)), ("Store", (100, 100, 140, 112))]
    monkeypatch.setattr(C, "lines", lambda path, page=0: fake)
    box = [(2475, 2365), (3460, 2365), (3460, 3179), (2475, 3179)]
    assert C._one_room_label("x.png", box, "Area of the room in here") == "Multi-Purpose Hall"
    assert C._one_room_label("x.png", box, "Total area of the rooms in here") is None  # plural: the tags / box answer
    big = [(0, 0), (4000, 0), (4000, 4000), (0, 4000)]
    assert C._one_room_label("x.png", big, "Area of the room in here") is None  # two names
