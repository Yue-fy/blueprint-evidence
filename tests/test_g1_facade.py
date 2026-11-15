"""Facade-scoped window / door counts (stoa.agent.facade; captain 20:47, Ziying's hand gold dfe-08 = 18)."""
from pathlib import Path

import numpy as np
import pytest

from stoa.agent import facade as F

DFE = Path(".stoa/human/drawings/d5b889d203-f4c030a4.pdf")


def test_parse_sides_and_wing():
    a = F.parse("How many windows are on the west facade of the classroom wing?")
    assert (a.side, a.kind, a.wing, a.by_compass) == ("west", "window", ["classroom"], True)
    assert F.parse("How many windows face east in the classroom wing?").side == "east"
    assert F.parse("How many north-facing windows are there?").side == "north"
    b = F.parse("How many exterior doors are on the left side of the building?")
    assert (b.side, b.kind, b.by_compass, b.wing) == ("left", "door", False, [])
    assert F.parse("How many windows are in the classroom?") is None  # no side: not a facade question
    assert F.parse("What is the area of the west wing?") is None  # not a count


def test_rotation_helpers_round_trip():
    H, W = 7, 11
    for k in range(4):
        a = np.arange(H * W).reshape(H, W)
        r = np.rot90(a, k)
        for x, y in ((0, 0), (10, 6), (3, 2)):
            xr, yr = F.rot_fwd(x, y, k, H, W)
            assert r[int(yr), int(xr)] == a[y, x]
            assert F.rot_back(xr, yr, k, H, W) == (x, y)


def test_runs_profile_windows_split_by_piers():
    px = 100.0  # px per metre
    rows, cols = 800, 80
    s = np.full((rows, cols), 255, np.uint8)
    band = slice(20, 70)
    s[0:100, band] = 0                       # pier
    for r0, r1 in ((100, 230), (250, 380)):  # two windows (1.3 m) split by a 0.2 m pier
        s[r0:r1, 20:22] = 0
        s[r0:r1, 44:46] = 0
        s[r0:r1, 68:70] = 0
    s[230:250, band] = 0
    s[380:420, band] = 0                     # pier
    # 420-510: door opening (0.9 m, empty)
    s[510:800, band] = 0
    runs, info = F._runs(s, px)
    kinds = [r.kind for r in runs]
    assert kinds.count("window") == 2 and kinds.count("door") == 1


@pytest.mark.skipif(not DFE.exists(), reason="Stoa's DfE sheet not on this machine")
def test_dfe08_west_facade_18():
    a = F.answer(str(DFE), "How many windows are on the west facade of the classroom wing?")
    assert a is not None and a.value == 18
    boxes = [e.bbox for e in a.evidence if e.exact]
    assert len(boxes) == 18 and all(130 < b[0] < 150 for b in boxes)  # all on the west wall (x ~ 139 pt)
    assert any("top of the sheet is taken as north" in x for x in a.assumptions)
    d = F.answer(str(DFE), "How many doors are on the west facade of the classroom wing?")
    assert d.value in (4, 5)  # 4 classroom doors (+ the Plant Room's louvred double door)


def test_skirting_without_a_room_asks_which_room():
    from stoa.pipeline import _skirting_which_room as f

    a = f("How much skirting should we order?")
    assert a is not None and a.value is None and any("which room" in m for m in a.missing)
    for q in ("How much skirting do we need for the laundry?", "What length of skirting is needed for all rooms?",
              "What is the skirting length in Room 1.04?",
              "For skirting in the laundry, what length should I order after excluding the doorway?"):
        assert f(q) is None, q


def test_cad_render_is_never_cubicasa(tmp_path):
    from stoa.pipeline import _cad_render

    (tmp_path / "sheet.pdf").write_bytes(b"%PDF-1.4")
    png = tmp_path / "sheet.r150.png"
    png.write_bytes(b"x")
    assert _cad_render(str(png))
    assert not _cad_render(str(tmp_path / "photo.png"))
    other = tmp_path / "plan.r150.png"  # no sibling PDF: a visitor's own PNG
    other.write_bytes(b"x")
    assert not _cad_render(str(other))


def test_unresolved_facade_scope_caps_a_sheet_wide_count():
    from stoa.contract import Answer
    from stoa.pipeline import _facade_unresolved

    a = Answer(value=79, unit="count", confidence=0.5, solver="pipeline/openings")
    out = _facade_unresolved("How many windows are on the west facade of the classroom wing?", a)
    assert out.confidence == 0.3 and any("not resolved" in m for m in out.missing)
    same = _facade_unresolved("How many windows are there?", a)
    assert same.confidence == 0.5


def test_non_english_opening_count_rewritten_by_rules():
    from stoa.pipeline import _opening_count_english as f

    assert f("Kuinka monta ovea piirustuksessa on?") == "How many doors are there in the drawing?"
    assert f("Kuinka monta ovea pesulassa on?") == "How many doors are in the laundry?"
    assert f("Wie viele Fenster hat die Waschküche?") == "How many windows are in the laundry?"
    assert f("Kuinka monta ovea asunnossa on?") is None  # an unknown scope word: left to the translator / readers
    assert f("How many doors are there?") is None
    assert f("Kuinka monta makuuhuonetta on?") is None


def test_dimension_guard_withholds_an_area_for_a_perimeter():
    from stoa.contract import Answer
    from stoa.pipeline import _dimension_guard as g

    q = "What is the perimeter of the Break Room (2.02) without the closet, before deducting any openings?"
    out = g(q, Answer(value=153.0, unit="SF", confidence=0.3, solver="pipeline/raster"))
    assert out.value is None and "153.0 SF" in out.missing[0]
    assert g(q, Answer(value=45.3, unit="ft", confidence=0.6)).value == 45.3
    assert g("What is the area of the Laundry?", Answer(value=7.03, unit="m2", confidence=0.6)).value == 7.03
    assert g("How many doors are there?", Answer(value=3, unit="count", confidence=0.6)).value == 3


M101 = Path(".stoa/neutral/x1sl/acf1f07a2d/acf1f07a2d.pdf")  # Stoa's M-101A piping plan (X1 copy)


@pytest.mark.skipif(not M101.exists(), reason="Stoa's M-101A copy not on this machine")
def test_piping_sums_every_named_tag_family():
    from stoa.solvers import get_solver

    s = get_solver("piping")
    a = s.solve(str(M101), "How many filter separators (FSI and FSR) are there?")
    assert a.value == 7 and "FSI 5 + FSR 2" in a.trace[-1]  # X1 21:31: gold 7 (was FSI only: 5 @0.98)
    assert s.solve(str(M101), "How many FSR are shown?").value == 2


def test_verifier_withholds_a_weak_dimension_read_as_area():
    from stoa.agent.verify import apply_verdict
    from stoa.contract import Answer

    v = {"verdict": "disagree", "reason": "7200 is a dimension label, not an area", "value_seen": "", "boxes": 0}
    out = apply_verdict(Answer(value=7200, unit="m2", confidence=0.32), v)
    assert out.value is None and out.missing[0].startswith("withheld: 7200")
    strong = apply_verdict(Answer(value=54.2, unit="m2", confidence=0.7), dict(v, boxes=1))
    assert strong.value == 54.2  # only weak readings are withheld
    un = apply_verdict(Answer(value=3, unit="count", confidence=0.4), {"verdict": "unsure", "reason": ""})
    assert "because it gave no reason" in un.trace[-1]


def test_facade_count_is_structured_evidence_gemini_does_not_replace_it():
    # M1 22:16 (preview f420f958): west-facade windows 18 (gold) escalated as 'no confident structured count (facade)'
    # and Gemini's single reading ≈ 10 was shown on top
    from stoa.agent import vision as V
    from stoa.contract import Answer, Evidence

    a = Answer(value=18, unit="count", confidence=0.85, solver="pipeline/facade",
               evidence=[Evidence(file="x.pdf", page=0, bbox=[0, 0, 1, 1], note="window 1")])
    assert V.wanted(a, "How many windows are on the west facade of the classroom wing?", "/tmp/up/SEN.pdf") is None
    assert "facade" in V.STRUCTURED


def test_facade_of_a_named_room_is_its_scope():
    from stoa.agent.facade import parse  # DR 22:12: 'north facade of the Multi-Purpose Hall' measured the whole building

    a = parse("How many windows are on the north facade of the Multi-Purpose Hall?")
    assert a.wing == ["multi-purpose", "hall"] and a.wing_text == "Multi-Purpose Hall"
    assert parse("How many windows are on the west facade of the classroom wing?").wing == ["classroom"]
    assert parse("How many doors are on the south side of the building?").wing == []
