"""G1 cycle 15: non-English whole-plan area questions and 'koko' (whole) door / window counts (F4 02:59)."""

import pytest

from stoa.pipeline import _opening_count_english, _whole_plan_english_area


@pytest.mark.parametrize("q,want", [
    ("Mikä on tämän pohjapiirustuksen kokonaispinta-ala?", "What is the total floor area of this plan?"),
    ("Mikä on kokonaispinta-ala?", "What is the total floor area of this plan?"),
    ("Mikä on pohjapiirustuksen pinta-ala?", "What is the total floor area of this plan?"),
    ("Mikä on kerroksen kokonaispinta-ala?", "What is the total floor area of this plan?"),
    ("Mikä on kerrosala?", "What is the gross floor area of this plan?"),
    ("Wie groß ist die Gesamtfläche des Grundrisses?", "What is the total floor area of this plan?"),
    ("Vad är planens totala yta?", "What is the total floor area of this plan?"),
    ("Vad är den totala golvytan på planen?", "What is the total floor area of this plan?"),
    ("Mikä on asunnon kokonaispinta-ala?", "What is the total floor area of this apartment?"),
    ("Hur stor är lägenheten?", "What is the total floor area of this apartment?"),
])
def test_whole_plan_rewrites(q, want):
    assert _whole_plan_english_area(q) == want


@pytest.mark.parametrize("q", [
    "Mikä on pinta-ala?",                                   # no total / plan / flat word
    "Mikä on keittiön pinta-ala?",                          # a room: _gloss_english_area's job
    "Wie groß ist die Wohnfläche?",                         # a net measure, not the plan's total
    "Wie gross ist die Gesamtfläche der Wohnung C?",        # a named apartment
    "Mikä on länsisiiven kokonaispinta-ala?",               # a wing
    "Wie groß ist die Gesamtfläche aller Räume im Haus?",   # other content words: the translator keeps it
    "What is the total area?",                              # English stays as asked
    "Kuinka monta ovea pohjapiirustuksessa on?",            # a count
])
def test_whole_plan_leaves_others(q):
    assert _whole_plan_english_area(q) is None


def test_whole_plan_kill_switch(monkeypatch):
    monkeypatch.setenv("STOA_G1_WHOLE_PLAN", "0")
    assert _whole_plan_english_area("Mikä on kokonaispinta-ala?") is None
    assert _opening_count_english("Kuinka monta ovea koko pohjapiirustuksessa on?") is None


def test_koko_count():
    assert _opening_count_english("Kuinka monta ovea koko pohjapiirustuksessa on?") == \
        "How many doors are there in the drawing?"
    assert _opening_count_english("Kuinka monta ikkunaa koko pohjapiirustuksessa on?") == \
        "How many windows are there in the drawing?"
    assert _opening_count_english("Hur många dörrar finns det på hela ritningen?") == \
        "How many doors are there in the drawing?"


def test_bare_total_off_by_default(monkeypatch):
    from stoa.pipeline import _bare_total_area

    monkeypatch.delenv("STOA_G1_BARE_TOTAL", raising=False)
    assert _bare_total_area("What is the total area?") is None
    monkeypatch.setenv("STOA_G1_BARE_TOTAL", "1")
    assert _bare_total_area("What is the total area?") == "What is the total floor area of this plan?"
    assert _bare_total_area("What's the total square footage?") == "What is the total floor area of this plan in square feet?"
    assert _bare_total_area("What is the total area of the west wing?") is None
    assert _bare_total_area("What is the total area of all rooms?") is None


DFE_L00 = ".stoa/human/drawings/d5b889d203-f4c030a4.pdf"


def test_generic_rooms_regex():
    import stoa.solvers.label_count as LC

    for q in ("How many rooms are there?", "How many rooms are shown on this floor plan?",
              "How many rooms does this floor have?"):
        assert LC._GENERIC_ROOMS_Q.match(q)
    for q in ("How many rooms labelled 'Store' are there on this plan?", "How many rooms on this floor are 55 m2?",
              "How many rooms does the support module have?"):
        assert not LC._GENERIC_ROOMS_Q.match(q)


def test_bare_room_tails_not_a_room_count(tmp_path, monkeypatch):
    import os
    import shutil

    import stoa.solvers.label_count as LC
    from stoa.solvers import get_solver

    if not os.path.exists(DFE_L00):
        pytest.skip("DfE L00 copy missing")
    p = tmp_path / "plan.pdf"
    shutil.copy(DFE_L00, p)
    s = get_solver("label_count")
    monkeypatch.setattr(LC, "BARE_ROOM", True)
    a = s.solve(str(p), "How many rooms are there?")  # not the 7 'Room' tails of Plant / Group / … Room
    # S0 D63: 80 on G1's base (78b27fc4 lineplan); 79 on d34b, whose E1 lineplan labels merge one more stacked name
    assert a.value in (79, 80) and a.confidence <= 0.5 and len(a.evidence) == a.value
    assert any(f"without them {a.value - 15} rooms" in x for x in a.assumptions)  # S0 D63: 65 on G1's base, 64 on d34b
    assert s.solve(str(p), "How many rooms labelled 'Plant Room' are there on this plan?").value == 3
    monkeypatch.setattr(LC, "BARE_ROOM", False)
    assert s.solve(str(p), "How many rooms are there?").value == 7
