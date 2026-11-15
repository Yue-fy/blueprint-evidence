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


MIXED = "data/swiss_dwellings/site11725_bldg18295_floor49280.pdf"  # 15 'Bad' + 3 unnamed bath/shower rooms (C: 2.21)
SEP_WC = "data/swiss_dwellings/site1005_bldg2087_floor9545.pdf"  # A: 'Bad' 2.03 (shower) + unnamed WC-only 2.02
needs_v2 = pytest.mark.skipif(not (abspath(MIXED).exists() and abspath(SEP_WC).exists()), reason="dataset not available")


@needs_v2
def test_mixed_floor_counts_unnamed_bathrooms_too(dxf):
    """v2 (PR cycle 5): 117 of 123 affected floors mix labelled and unnamed bathrooms; the floor count missed the
    unnamed ones. The labelled bathroom still wins a superlative it should win, at its old confidence."""
    a = dxf.solve(MIXED, "How many bathrooms are on this floor?")
    assert a.value == 18 and a.confidence <= 0.5 and len(a.evidence) == 18
    assert any("besides the 15 room(s) labelled" in s and "2.21" in s for s in a.assumptions)
    a = dxf.solve(MIXED, "How many bathrooms does apartment C have?")
    assert a.value == 2
    a = dxf.solve(MIXED, "What is the area of the largest bathroom in apartment C?")
    assert a.value == pytest.approx(3.96, abs=0.01) and a.confidence > 0.7  # 'Bad' 2.20 beats unnamed 2.21 (3.51)
    assert any("also compared 1 unnamed" in s for s in a.assumptions)


@needs_v2
def test_wc_only_room_next_to_a_bathroom_is_a_separate_toilet(dxf):
    a = dxf.solve(SEP_WC, "How many bathrooms does apartment A have?")
    assert a.value == 1 and not any("unnamed" in s for s in a.assumptions)  # 2.02 = a separate WC, not a 2nd bath
    a = dxf.solve(UNLABELLED, "What is the area of the smallest bathroom in apartment A?")
    assert a.value == pytest.approx(3.384, abs=0.01) and a.confidence <= 0.5


@needs_data
def test_unnamed_bath_plus_unnamed_wc_reads_the_bath_room():
    """Both unnamed (floor 50457 C: bath 3.29 m² + WC-only room): 'the bathroom' = the bath room, the WC is named."""
    from stoa.solvers import get_solver
    p = "data/swiss_dwellings/site11864_bldg18511_floor50457.pdf"
    if not abspath(p).exists():
        pytest.skip("dataset not available")
    a = get_solver("dxf").solve(p, "What is the area of the bathroom in apartment C?")
    assert a.value == pytest.approx(3.29, abs=0.03)
    assert any("not counted" in s and "separate toilet" in s for s in a.assumptions)


@needs_data
def test_scoped_concept_superlative_is_not_a_sum(dxf):
    """'the largest bedroom in apartment A' took the 'largest apartment B' branch and summed both bedrooms
    (floor 9385: largest = smallest = 31.51; the rooms are 15.9 and 15.6)."""
    big = dxf.solve(UNLABELLED, "What is the area of the largest bedroom in apartment A?")
    small = dxf.solve(UNLABELLED, "What is the area of the smallest bedroom in apartment A?")
    assert big.value == pytest.approx(15.9, abs=0.06) and small.value < big.value
    assert small.value + big.value == pytest.approx(31.51, abs=0.05)
    from stoa.solvers.vector import qparse
    assert qparse.parse("What is the area of the largest apartment B?").terms[0].kind == "area_apartment"


@needs_data
def test_german_concept_superlative_uses_the_bedroom_fallback(dxf):
    """'das grösste Schlafzimmer' on a floor labelling bedrooms 'Zimmer': the concept, not a literal 'Schlafzimmer'."""
    a = dxf.solve("data/swiss_dwellings/site333_bldg692_floor3958.pdf", "Wie gross ist das grösste Schlafzimmer der Wohnung C?")
    assert a.value == pytest.approx(18.5, abs=0.05)


def _upload_copy(tmp_path, rel):
    """The sheet ALONE under a neutral name (no DXF / PNG sibling, no data/<source>/ hint): the web upload."""
    import shutil

    d = tmp_path / "u0a1b2c3d4"
    d.mkdir()
    dst = d / "u0a1b2c3d4.pdf"
    shutil.copyfile(abspath(rel), dst)
    return str(dst)


def test_apartment_reference_regex():
    from stoa.pipeline import _APARTMENT_REF as P
    from stoa.solvers.plan_rooms import _APARTMENT_REF as R

    for rx in (P, R):
        for q in ("What is the area of the largest bedroom in apartment A?", "das grösste Schlafzimmer der Wohnung C",
                  "la plus petite chambre à coucher de l'appartement C", "Fläche Whg. B", "apartment 12"):
            assert rx.search(q), q
        for q in ("How big is the largest room?", "What is the largest room in the apartment?",
                  "How many apartments are there?", "How many units in the building?", "Which apartment has the largest kitchen?"):
            assert not rx.search(q), q


@needs_data
def test_plan_rooms_abstains_on_one_apartment(tmp_path):
    from stoa.solvers import get_solver

    p = _upload_copy(tmp_path, UNLABELLED)
    a = get_solver("plan_rooms").solve(p, "What is the area of the largest bedroom in apartment A?")
    assert a.value is None and any("apartment" in str(t) for t in (a.trace or []))


@needs_data
def test_uploaded_sheet_scoped_superlatives(tmp_path):
    """Booth floor uploaded alone: pdf_vector's scoped answers are not replaced by the sheet's largest room (plan_rooms
    'Wohnen/Essen Korridor' 25.81) or the sheet-wide takeoff ('Reduit 2.30' 31.05, a room of no apartment)."""
    from stoa.solvers import get_solver

    s = get_solver("pipeline", gemini=0, verify=0)
    p = _upload_copy(tmp_path, UNLABELLED)
    assert s.solve(p, "What is the area of the largest bedroom in apartment A?").value == pytest.approx(15.9, rel=0.02)
    assert s.solve(p, "What is the area of the largest room in apartment A?").value == pytest.approx(18.36, rel=0.02)
