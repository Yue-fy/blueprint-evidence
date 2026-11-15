"""G1 cycle 14 (01:32-): Private-mode skip reason in missing (H 01:30), 'A x B' answers to area questions (X1 23:35 (a))."""
from stoa import pipeline as P
from stoa.contract import Answer, Evidence

_PRIV = ("door/window detector gave nothing: private mode: the door detector at http://127.0.0.1:8145 is not on this "
         "server or our network (not called)")


def test_private_skip_says_why_nothing_was_located():
    guess = Answer(value=12, unit="count", confidence=0.087, solver="pipeline/vlm_lora",
                   missing=["not counted (no symbols located)"])
    trace: list = []
    out = P._private_skip(guess, {"openings": _PRIV}, trace)
    assert out.value == 12 and out.confidence == 0.087  # value / confidence never change
    assert out.missing[0].startswith("the door detector was not used: Private mode")
    assert "127.0.0.1" not in out.missing[0]  # no URL on the card
    assert trace and trace[-1].startswith("private-mode check")


def test_private_skip_leaves_located_answers_and_other_reasons_alone():
    boxed = Answer(value=34, unit="count", confidence=0.7, solver="pipeline/openings",
                   evidence=[Evidence(file="x.pdf", page=0, bbox=[0, 0, 1, 1])])
    assert P._private_skip(boxed, {"openings": _PRIV}, []).missing == []
    guess = Answer(value=12, unit="count", confidence=0.1, solver="pipeline/vlm_lora")
    assert P._private_skip(guess, {"openings": "door/window detector gave nothing: the detector service is down"},
                           []).missing == []


def _ap(q, v, conf=0.65):
    return P._area_product(q, Answer(value=v, unit=None, confidence=conf, solver="pipeline/lengths"))


def test_area_question_answered_by_two_dimensions_is_multiplied():
    o = _ap("What is the footprint of the mechanical room?", "42' - 0\" × 16' - 8\" (12.80 m × 5.08 m)")
    assert (o.value, o.unit) == (700.0, "ft2") and o.assumptions[0].startswith("area = the printed")
    assert (_ap("How big is the storage room?", "6000 x 4000 mm").value, "m2") == (24.0, "m2")
    assert _ap("What is the area of the pad?", "12'-6\" by 10'").value == 125.0


def test_dimension_door_and_three_part_answers_are_not_multiplied():
    assert _ap("What are the dimensions of the storage room?", "6.0 m x 4.0 m").value == "6.0 m x 4.0 m"
    assert _ap("How big is the door to the office?", "3'-0\" x 7'-0\"").value == "3'-0\" x 7'-0\""
    assert _ap("How long is the wall?", "12'-6\" by 10'").value == "12'-6\" by 10'"
    assert _ap("What's the area of the slab?", "30'-0\" x 20'-0\" x 6\"").value == "30'-0\" x 20'-0\" x 6\""
    assert _ap("What is the area of the pad?", "12 m").value == "12 m"


def test_how_many_answered_with_no_number_is_capped():
    q = "How many fire extinguisher cabinets (F.E.C.) are shown on the plan?"
    assert P._shape_guard(q, Answer(value="a / F.E.C.", confidence=0.65)).confidence == P.SHAPE_CAP
    assert P._shape_guard("How many rooms?", Answer(value="three", confidence=0.65)).confidence == 0.65
    assert P._shape_guard("How many pumps?", Answer(value="FP-1 to FP-5", confidence=0.65)).confidence == 0.65


def test_school_type_names_the_building():
    assert P._drawing_words("How many doors does the secondary school have?")[0] == "How many doors does the building have?"
    assert P._drawing_words("How big is the primary school's hall?")[0] == "How big is the building's hall?"
    assert P._drawing_words("What is the area of the Secondary Classroom?")[0] == \
        "What is the area of the Secondary Classroom?"


import pytest  # noqa: E402
from pathlib import Path  # noqa: E402

EFA_C2 = Path(".stoa/neutral/x1sl/d7c5a1e97b/d7c5a1e97b.pdf")  # X1's EFA colour sheet copy
MFC = Path(".stoa/neutral/x1sl/d5e6b1c4d3/d5e6b1c4d3.pdf")  # X1's MFC A-103 copy
A101F = Path(".stoa/neutral/x1sl/c6ff0aa6e6/c6ff0aa6e6.pdf")  # X1's USACE A-101F copy


@pytest.mark.skipif(not EFA_C2.exists(), reason="X1's EFA colour sheet copy not on this machine")
def test_area_tags_bracketed_name_list_and_room_numbers():
    from stoa.solvers import get_solver

    s = get_solver("area_tags")
    q = "What is the total floor area of the three art rooms (Art 1, Art 2 and Art 3)?"
    assert s.solve(str(EFA_C2), q).value == 272.0  # Art 1 - Small 86 + Art 2 89 + Art 3 - 3D 97
    assert s.solve(str(EFA_C2), "What is the total floor area of Art 1, Art 2 and Art 3?").value == 272.0
    assert s.solve(str(EFA_C2), "What is the combined area of English 1 and English 2?").value == 110.0


@pytest.mark.skipif(not (MFC.exists() and A101F.exists()), reason="X1's MFC / A-101F copies not on this machine")
def test_label_count_qualifiers_view_scope_and_numbered_rooms():
    from stoa.solvers import get_solver

    s = get_solver("label_count")
    a = s.solve(str(MFC), "How many restrooms are there?")  # Male / Female Restrooms, printed in plan + RCP
    assert a.value == 2 and any("left out" in x for x in a.assumptions)
    assert s.solve(str(A101F), "How many numbered rooms does the filter building have?").value == 4


def test_gross_outline_below_the_net_room_sum_is_flagged(monkeypatch):
    summ = {"area": {"value": 1717.16, "unit": "m2"}, "rooms": {"measured": 77}, "unit": "m2"}
    monkeypatch.setattr(P, "_takeoff_of", lambda path: ({"rows": []}, summ))
    fa = Answer(value=1680.5, unit="m2", confidence=0.5, solver="pipeline/footprint")
    out = P._gross_below_net(fa, "x.png")
    assert out.value == 1680.5 and out.confidence == P.GROSS_NET_CAP
    assert "at least 1,717 m2" in out.missing[0]
    ok = Answer(value=1900.0, unit="m2", confidence=0.5, solver="pipeline/footprint")
    assert P._gross_below_net(ok, "x.png").confidence == 0.5
    monkeypatch.setattr(P, "_takeoff_of", lambda path: (None, None))
    assert P._gross_below_net(fa, "x.png").confidence == 0.5


def test_low_confidence_metric_pair_or_guess_is_not_multiplied():
    # QA 02:02: '20m by 20m' (scale-bar labels on a 1920-px screenshot) must not become 400 m²
    assert P._area_product("How big is the multi-purpose hall?",
                           Answer(value="20m by 20m", confidence=0.25, solver="pipeline/raster")).value == "20m by 20m"
    assert P._area_product("How big is the hall?",
                           Answer(value="20 m x 12 m", confidence=0.7, solver="pipeline/agent")).value == "20 m x 12 m"
    # a printed feet-inch pair read at 0.3 is still a dimension string (d1-aw29o: 3000 ft² = gold)
    assert P._area_product("What is the footprint area of the pumphouse building?",
                           Answer(value="60'-0'' x 50'-0''", confidence=0.3, solver="pipeline/raster")).value == 3000.0


def test_room_after_does_in_into_is_the_scope_of_a_count():
    from stoa.router.intent import modifiers, parse

    for q, t in (("How many doors does the kitchen have?", "door"), ("How many doors lead into the kitchen?", "door"),
                 ("How many windows are in the kitchen?", "window"), ("How many doors does the Toilets have?", "door")):
        assert parse(q).target == t and "multi" not in modifiers(q), q
    assert parse("How many kitchens are there?").target == "kitchen"
    assert parse("How many toilets are in the bathroom?").target == "wc"
    assert "multi" in modifiers("How many doors and windows are in the kitchen?")
    assert "multi" in modifiers("What is the combined area of the kitchen and the bathroom?")


def test_door_and_window_sizes_are_text_questions():
    from stoa.router.intent import parse

    assert parse("What size is door 7.01A?").task == "text"
    assert parse("What is the size of window W3?").task == "text"
    assert parse("What size is the kitchen?").task == "area"
    assert parse("What is the size of the window area on the south facade?").task == "area"


def test_largest_room_is_never_a_roof_or_void():
    assert P._NOT_A_ROOM_NAME.search("Roof R301") and P._NOT_A_ROOM_NAME.search("Attic void")
    assert not P._NOT_A_ROOM_NAME.search("Multi-Purpose Hall") and not P._NOT_A_ROOM_NAME.search("Living Room")


def test_room_scope_is_not_an_unused_class():
    a = Answer(value=4, confidence=0.7, trace=["openings: 4 doors"])
    assert P._unused_classes("How many doors does the kitchen have?", a) == set()
    assert P._unused_classes("How many doors lead into the kitchen?", a) == set()
    assert P._unused_classes("How many toilets and sinks are there?", a) == {"wc", "sink"}


def test_per_group_breakdown_is_not_words_only():
    from stoa.agent import vision as V
    from stoa.contract import Evidence as E

    v = "Total spaces (whole model): 285\nBy storey:\n- 1.korrus: 50 (17.5%)\n- 2.korrus: 60 (21.1%)"
    a = Answer(value=v, confidence=0.85, solver="pipeline/ifc", evidence=[E(file="m.ifc", ifc_guid="g1")])
    assert not V._words_only(v)
    assert V.wanted(a, "How many rooms are on each floor?", "/tmp/stoa-uploads/x/model.ifc") is None
    assert V._words_only("Not determinable") and V._words_only("3,50 x 3,00")


def test_non_english_area_question_naming_one_room_gets_the_english_wording():
    assert P._gloss_english_area("Wie groß ist der Abstellraum in Quadratmetern?") == "What is the floor area of the store?"
    assert P._gloss_english_area("Mikä on keittiön pinta-ala?") == "What is the floor area of the kitchen?"
    assert P._gloss_english_area("Hur stor är köket?") == "What is the floor area of the kitchen?"
    assert P._gloss_english_area("Kuinka suuri on keittiön ikkuna?") is None  # a window, not the room
    assert P._gloss_english_area("Wie groß ist die Küche und der Flur?") is None  # two rooms: the translator
    assert P._gloss_english_area("Wie groß ist die große Küche?") is None  # a qualifier word
    assert P._gloss_english_area("What is the area of the kitchen?") is None


def test_apartment_type_code_is_the_flats_headline():
    from stoa.solvers import area_tags as A

    for name in ("2 H+KK+S", "3h+k+s", "1 h + kt", "2 rok", "3 r+k", "4H+K+S+VH"):
        assert A._APT_TYPE.match(name), name
    for name in ("KH", "MH", "Store 2", "H 12", "2 KK"):
        assert not A._APT_TYPE.match(name), name
    assert "mh" in A._SYN["bedroom"] and "kh" in A._SYN["bathroom"]


FI_FLAT = Path("/tmp/stoa-uploads/106fd68db466/data/cubicasa5k/asunto-2h-kk-s.png")  # F2's upload (O1 02:24)


@pytest.mark.skipif(not FI_FLAT.exists(), reason="F2's Finnish flat upload not on this machine")
@pytest.mark.skip(reason="S0 D63: the 0.9 confidence needs VP 04fd8d02 (stoa/sheet/labels.py FI clean, post-freeze, not in d34b); value 45.5 is right")
def test_finnish_flat_total_is_its_headline_not_a_sum():
    from stoa.solvers import get_solver

    s = get_solver("area_tags")
    a = s.solve(str(FI_FLAT), "What is the total floor area of the apartment?")
    if a.value is None:
        pytest.skip("the upload's text layer is not cached on this machine")
    assert a.value == 45.5 and a.confidence >= 0.9  # '2 H+KK+S 45.5 m²'; the 8 room tags sum to 45.5
    assert s.solve(str(FI_FLAT), "What is the area of the bedroom?").value == 10.0  # MH


def test_clarify_labels_never_show_trace_json_or_solver_ids():
    from stoa.agent import vision as V

    g = Answer(value=415.28, confidence=0.3, trace=['recognised: {"kind": "plan_render", "scale": null}',
                                                     "Gemini 3.1 Pro vision: area 415 m2"])
    assert V.label_of(g) == "Gemini 3.1 Pro"
    assert V.label_of(Answer(value=1, trace=['recognised: {"kind": "x vision y"}'])) == "Gemini"
    assert V._our(Answer(value=1, solver="pipeline/plan_rooms")) == "our measured outline reading"
    assert "plan_rooms" not in V._our(Answer(value=1, solver="pipeline/plan_rooms"))
