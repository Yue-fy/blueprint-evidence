"""G1 c7 (X1 19:33): a room's printed size 'A x B' under its name is its area (text layer only, no call)."""
import pytest


@pytest.fixture
def habs(monkeypatch):
    import stoa.solvers.room_size as R

    lines = (("BEDROOM?", (488, 723, 627, 747)), ("bathroom", (787, 736, 862, 757)),
             ("13'-5\" x 13'-5\"", (504, 750, 602, 773)), ("KITCHEN?", (496, 961, 626, 988)),
             ("13'-5\" x 13'-6\"", (506, 991, 603, 1012)), ("LIVING ROOM", (473, 1190, 646, 1217)),
             ("13'-5\" x 13'-6\"", (510, 1220, 604, 1238)), ("PORCH", (629, 1370, 719, 1398)),
             ("square rooms of equal size with a bath-", (1165, 1200, 1612, 1235)))
    monkeypatch.setattr(R, "_lines", lambda path: (lines, "image_px"))
    return R


def test_habs_room_sizes(habs):
    a = habs.answer("/tmp/upload/habs.jpg", "What is the floor area of the living room?")
    assert a.unit == "ft2" and abs(a.value - 181.13) <= 0.02 and a.confidence >= 0.6 and len(a.evidence) == 2
    b = habs.answer("/tmp/upload/habs.jpg", "How big is the bedroom in square metres?")
    assert b.unit == "m2" and abs(b.value - 16.73) <= 0.02
    assert abs(habs.answer("/tmp/upload/habs.jpg", "What is the area of the kitchen?").value - 181.13) <= 0.02


def test_no_size_no_answer(habs):
    assert habs.answer("/tmp/upload/habs.jpg", "What is the area of the porch?") is None  # no size printed by it
    assert habs.answer("/tmp/upload/habs.jpg", "What is the gross floor area of the house?") is None
    assert habs.answer("/tmp/upload/habs.jpg", "How many bedrooms are there?") is None


def test_sizes_in():
    from stoa.solvers.room_size import sizes_in

    assert sizes_in("KITCHEN 4,20 x 3,60")[0][:3] == (4.2, 3.6, "m")
    assert sizes_in("900 x 2100") == []  # a door (mm), not a room size
    a, b, u, _ = sizes_in("SHADED BOUNDRY (9’-0\"X9’-11\")")[0]
    assert u == "ft" and abs(a * b - 89.25) < 0.01


def test_number_in_question_picks_the_label(monkeypatch):
    """D1 aw29: 'the 1'-6\" slab-on-grade' must not take the '1'-9\" SLAB-ON-GRADE' label's size."""
    import stoa.solvers.room_size as R

    lines = (("1’-6\" SLAB-ON-GRADE", (840, 892, 973, 904)), ("W/#5 @ 12\" E.W.", (840, 915, 960, 927)),
             ("BOUNDRY (7’-0\"X7’-0\")", (840, 938, 978, 950)), ("1’-9\" SLAB-ON-GRADE W/#5 @", (1685, 662, 1863, 674)),
             ("SHADED BOUNDRY (9’-0\"X9’-11\")", (1686, 692, 1887, 704)))
    monkeypatch.setattr(R, "_lines", lambda path: (lines, "pdf_pt"))
    assert R.answer("/tmp/upload/s.pdf", "How large is the 1'-6\" slab-on-grade area?").value == 49.0
    assert R.answer("/tmp/upload/s.pdf", "What is the area of the 1'-9\" thick slab-on-grade?").value == 89.25


def test_agent_round_dimension_is_not_a_room_area():
    """O1 19:32 (X1 CAD crops): agent look() 7200 m² for 'English 3' vs raster's measured 54.22 @0.3 -> raster kept."""
    from stoa.contract import Answer
    from stoa.pipeline import Pipeline
    from stoa.router.intent import parse

    p = Pipeline()
    q = "What is the area of English 3?"
    agent = Answer(value=7200, unit="m2", confidence=0.35, solver="pipeline/agent", trace=["general agent first: x"])
    out = p._vs_unsure(agent, ("raster", Answer(value=54.22, unit="m2", confidence=0.3, solver="raster")), parse(q), q)
    assert out.value == 54.22 and out.solver == "pipeline/raster"
    q2 = "What is the total area of the second floor?"
    agent2 = Answer(value=7200, unit="m2", confidence=0.35, solver="pipeline/agent", trace=["general agent first: x"])
    out2 = p._vs_unsure(agent2, ("raster", Answer(value=54.22, unit="m2", confidence=0.3, solver="raster")), parse(q2), q2)
    assert out2.value != 54.22 or out2.solver != "pipeline/raster"
