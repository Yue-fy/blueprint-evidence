"""PR: 'the bathroom' on a Swiss plan whose bathrooms carry no 'Bad' label (only a number + area) is read from the
fixtures drawn inside (bathtub / shower, else WC), says so, and stays at 0.5; labelled floors are unchanged."""

import pytest

from stoa.data import abspath

UNLABELLED = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf"  # rooms 2.04 / 2.16 / 2.18 / 2.23: no name
LABELLED = "data/swiss_dwellings/site1001_bldg2083_floor9389.pdf"  # 4 rooms labelled 'Bad'
needs_data = pytest.mark.skipif(not (abspath(UNLABELLED).exists() and abspath(LABELLED).exists()),
                                reason="dataset not available")


@pytest.fixture(scope="module")
def dxf():
    from stoa.solvers import get_solver

    return get_solver("dxf")


@needs_data
def test_unlabelled_bathroom_area_and_count(dxf):
    a = dxf.solve(UNLABELLED, "What is the area of the bathroom in apartment A?")
    assert a.value == pytest.approx(3.384, abs=0.01) and a.confidence <= 0.5
    assert any("no room is labelled as a bathroom" in s and "2.04" in s for s in a.assumptions)
    assert len(a.evidence) == 1
    a = dxf.solve(UNLABELLED, "How many bathrooms does apartment A have?")
    assert a.value == 1 and a.confidence <= 0.5
    a = dxf.solve(UNLABELLED, "How many bathrooms are on this floor?")
    assert a.value == 4
    a = dxf.solve(UNLABELLED, "What is the total area of all bathrooms on this floor?")
    assert a.value == pytest.approx(3.384 + 3.56 + 3.66 + 3.458, abs=0.05) and len(a.evidence) == 4


@needs_data
def test_labelled_bathrooms_unchanged(dxf):
    a = dxf.solve(LABELLED, "What is the area of the bathroom in apartment A?")
    assert a.value == pytest.approx(3.31, abs=0.01)
    assert not any("no room is labelled as a bathroom" in s for s in a.assumptions)
    a = dxf.solve(LABELLED, "How many bathrooms are on this floor?")
    assert a.value == 4 and not any("no room is labelled as a bathroom" in s for s in a.assumptions)


@needs_data
def test_other_concepts_untouched(dxf):
    a = dxf.solve(UNLABELLED, "How big is the kitchen of apartment A?")
    assert a.value == pytest.approx(12.05, abs=0.01)
    assert not any("no room is labelled as a bathroom" in s for s in a.assumptions)
