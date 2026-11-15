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
