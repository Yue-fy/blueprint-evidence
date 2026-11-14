"""Cycle-9 fix: an unquoted room LongName that exactly one IfcSpace of the model carries is resolved as that room
('How big is the INVA-WC?' summed all 13 toilets of the storey @ 0.85; gold = the one room, 5.96 m²). Probe:
experiments/w1/probes/c9_natural.txt; expected values = the dataset-template wording "What is the area of 'X (code)'?"."""

from __future__ import annotations

import pytest

from stoa.data import abspath
from stoa.solvers.ifc.index import load_index
from stoa.solvers.ifc.solver import IfcSolver, quote_room_refs

ESPL = "data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"
SMW = "data/ifc_models/kit-smiley-west_ac-20-smiley-west-10-bldg_plan-01-kg.pdf"
need = pytest.mark.skipif(not abspath(ESPL).exists(), reason="dataset not available")


@need
@pytest.mark.parametrize("path,q,want", [
    (ESPL, "How big is the INVA-WC?", 5.96),
    (SMW, "How big is Kellerraum-05-2?", 4.582),
])
def test_unquoted_unique_longname(path, q, want):
    a = IfcSolver().solve(path, q)
    assert a.value == pytest.approx(want, rel=0.01)
    assert a.confidence >= 0.8


@need
def test_longname_quoting_is_narrow():
    idx = load_index(str(abspath("data/ifc_models/bsc-esplanades_1807-ep-ar-v18.ifc")))
    assert quote_room_refs(idx, "How big is the INVA-WC?") == "How big is the 'INVA-WC'?"
    # shared type words, storey names and already quoted questions are left alone
    for q in ("How many ESIK rooms are on this floor?", "How many doors are on 2.korrus?",
              "What is the area of 'INVA-WC (103)'?", "How many toilets on this floor?"):
        assert quote_room_refs(idx, q) == q


ESPL_IFC = "data/ifc_models/bsc-esplanades_1807-ep-ar-v18.ifc"
SMW_IFC = "data/ifc_models/kit-smiley-west_ac-20-smiley-west-10-bldg.ifc"


@need
@pytest.mark.parametrize("path,q,want", [
    (ESPL_IFC, "How many windows does the floor with the most doors have?", 55),  # 1.korrus 46 doors
    (ESPL_IFC, "How many rooms are on the floor with the fewest doors?", 55),  # 5.korrus 39 doors
    (ESPL_IFC, "How many doors does the level with the most walls have?", 42),  # tie 3/4.korrus, both 42
    (SMW_IFC, "How many windows does the floor with the most walls have?", 20),  # EG 81 walls
    (SMW_IFC, "How many doors are on the storey that has the most rooms?", 50),  # tie 1.OG / 2.OG, both 50
])
def test_nested_floor_superlative(path, q, want):
    a = IfcSolver().solve(path, q)
    assert a.value == want
    assert a.confidence >= 0.7
    assert any("nested superlative" in t for t in a.trace)


@need
def test_nested_tie_with_different_answers_is_capped():
    a = IfcSolver().solve(ESPL_IFC, "How many walls are on the floor with the most rooms?")  # 156 / 160 / 160
    assert a.confidence <= 0.5


@need
@pytest.mark.parametrize("q", ["Where is room 103?", "Where is the INVA-WC?", "Wo ist Raum 103?", "Missä on huone 103?",
                               "Kus asub ruum 103?"])
def test_bare_where_is_room(q):
    a = IfcSolver().solve(ESPL, q)
    assert str(a.value).startswith("1.korrus") and "103" in str(a.value)
    assert a.confidence >= 0.8


@need
def test_nested_same_kind_is_capped():
    # legal but what a slipped translation looks like (et 'uksi' = doors came back as 'windows')
    a = IfcSolver(options={"llm": "0"}).solve(ESPL_IFC, "How many doors does the floor with the most doors have?")
    assert a.value == 46 and a.confidence <= 0.5


@need
@pytest.mark.parametrize("q,want,not_want", [
    ("Where are the stairs?", ["1.korrus (1 stair)", "4.korrus (1 stair)"], ["5.korrus", "Vundament"]),
    ("On which floors are there columns?", ["1.korrus (37 columns)"], ["2.korrus"]),
    ("Where are the windows?", ["1.korrus (55 windows)", "Katus (2 windows)"], ["Vundament"]),
])
def test_where_element_kind(q, want, not_want):
    """c10 (post-freeze): 'Where is <element kind>?' = the storeys that have some, per-storey count rule, GUID evidence."""
    a = IfcSolver().solve(ESPL, q)
    assert isinstance(a.value, str) and a.confidence >= 0.8
    assert all(w in a.value for w in want) and not any(n in a.value for n in not_want)
    assert a.evidence and all(e.ifc_guid for e in a.evidence)


@need
def test_how_many_floors_have_kind():
    a = IfcSolver(options={"llm": "0"}).solve(ESPL, "How many floors have stairs?")  # 1.-4.korrus
    assert a.value == 4 and a.confidence >= 0.8
    b = IfcSolver(options={"llm": "0"}).solve(ESPL, "How many floors does the building have?")  # unchanged path
    assert b.value == 5
