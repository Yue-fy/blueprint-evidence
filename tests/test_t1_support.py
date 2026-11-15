"""T1 trust guards (independent audit 19:51, risk #1): a cited number must BE the asked quantity.

Live failure on Stoa's S-100F PNG: 'How many square metres of the 150 mm slab with D13 bars at 300 mm centres are
shown?' -> 10000 m² @0.75 citing 'TOP OF SLAB EL. = 10000' (artifacts/AUDIT/ui-clean/s100_png/q1-answer.json).
No model, OCR or network: the reader's VLM reply and text layer are stubbed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stoa.contract import Answer, Evidence
from stoa.trust import support as S

S100_Q = "How many square metres of the 150 mm slab with D13 bars at 300 mm centres are shown?"
S100_CITED = ["150 SLAB-ON-GRADE W/", "NOTE 1 AND 2", "D13 @ 300", "TOP OF SLAB EL. = 10000"]
S100_BOXES = [[3943.2, 1663.4, 4228.8, 1687.1], [1511.4, 1687.1, 1678.2, 1709.6], [3943.2, 1691.8, 4065.3, 1714.4],
              [3943.2, 1719.1, 4236.6, 1741.6]]


def _roles(line: str) -> list[tuple[str, str]]:
    return [(m.group(1), S.role_of(line, m.start(1), m.end(1))) for m in S._NUM.finditer(line)]


@pytest.mark.parametrize("line,want", [
    ("TOP OF SLAB EL. = 10000", [("10000", "level")]),
    ("150 SLAB-ON-GRADE W/", [("150", "thickness")]),
    ("D13 @ 300", [("13", "bar"), ("300", "spacing")]),
    ("SCALE: 1 : 50", [("1", "scale"), ("50", "scale")]),
    ("FILE NUMBER: 15522", [("15522", "identifier")]),
    ("S-100F", [("100", "index")]),  # a sheet / tag mark: never a size; a tag index may be a count
    ("TOTAL COMMON SPACE = 4,479 SF", [("4,479", "area")]),
    ("KITCHEN 14,5", [("14,5", "plain")]),
    ("OH 20.5 m²", [("20.5", "area")]),
    ("6200 x 9950", [("6200", "dimension"), ("9950", "dimension")]),
    ("CLG HT 2400", [("2400", "height")]),
    ("FFL +3.200", [("3.200", "level")]),
    ("200 BLOCKWORK WALL", [("200", "thickness")]),
    ('16" SUCTION HEADER', [("16", "bar")]),
    ("TYPE 2", [("2", "index")]),
    ("ROOM NO. 12", [("12", "index")]),
    ("4 DOORS", [("4", "plain")]),
    ("H 12,5", [("12,5", "plain")]),  # Finnish 'huone' (room) code + its area: not a height
    ("DATE 12/03/2019", [("12", "date"), ("03", "date"), ("2019", "date")]),
])
def test_role_of_a_printed_number(line, want):
    assert _roles(line) == want


def test_s100_elevation_is_not_an_area():
    v = S.check_citation(S100_Q, 10000.0, S100_CITED)
    assert v is not None and not v.ok and v.role == "level" and "TOP OF SLAB EL. = 10000" in v.line


def test_controls_still_pass():
    # an elevation question is not guarded (task 'text'); the same line answers it
    assert S.check_citation("What is the top of slab elevation?", 10000, ["TOP OF SLAB EL. = 10000"]) is None
    # a printed room-area label, decimal comma, no unit
    assert S.check_citation("What is the area of the kitchen?", 14.5, ["KITCHEN 14,5"]).ok
    # a printed area table total
    assert S.check_citation("What is the total common space in square feet?", 4479,
                            ["TOTAL COMMON SPACE = 4,479 SF"]).ok
    # a thickness question may cite the thickness callout; a length question may not
    assert S.check_citation("How thick is the slab reinforced with D13 bars?", 150, ["150 SLAB-ON-GRADE W/"]).ok
    assert not S.check_citation("How long is the slab?", 150, ["150 SLAB-ON-GRADE W/"]).ok
    # a count is never a bar spacing; a plain '4 DOORS' is fine
    assert not S.check_citation("How many bars are there?", 300, ["D13 @ 300"]).ok
    assert S.check_citation("How many doors are there?", 4, ["4 DOORS"]).ok
    # the value printed twice: once as a level, once as a plain area label -> supported by the second
    assert S.check_citation("What is the area of the store?", 12.5, ["FFL 12.50", "STORE 12.5"]).ok
    # not printed literally in the cited lines -> nothing to say (other guards decide)
    assert S.check_citation(S100_Q, 61.69, S100_CITED) is None


def _audit_raw() -> Answer:
    """The deployed answer the auditor got (q1-answer.json), evidence notes as textqa writes them."""
    ev = [Evidence(file="upload/1548aa419efc/usace-S-100F-foundation-slab-plan.png", page=0, bbox=b,
                   bbox_space="image_px", exact=False, note=f"text '{t}'") for t, b in zip(S100_CITED, S100_BOXES)]
    return Answer(value=10000, unit="m2", confidence=0.75, evidence=ev, solver="pipeline/raster",
                  trace=["[raster] answer '10000' from text lines [63, 64, 65, 66] (literal match)"], missing=[])


def test_audit_raw_answer_is_not_a_supported_area():
    a = S.citation_guard(S100_Q, _audit_raw())
    assert a.confidence <= S.REJECT_CONF and any("elevation" in m for m in a.missing)
    st = S.support_state(S100_Q, a)
    assert st["state"] == "unlocated"
    f = S.final_guard(S100_Q, a)
    assert f.value is None and f.confidence == 0.0
    assert not any("TOP OF SLAB" in e.note for e in f.evidence)
    assert f.missing[0].startswith("no printed area was found") and "measure it" in f.missing[0]


def test_guard_leaves_good_citations_alone():
    ev = [Evidence(file="x.png", page=0, bbox=[1, 2, 3, 4], bbox_space="image_px", note="text 'KITCHEN 14,5'")]
    a = Answer(value=14.5, unit="m2", confidence=0.75, evidence=ev, solver="pipeline/raster")
    assert S.citation_guard("What is the area of the kitchen?", a).confidence == 0.75
    assert S.final_guard("What is the area of the kitchen?", a).value == 14.5
    # T1 c3: a number with no area unit next to it is 'approximate' (it may be a dimension or a room number: 19 wrong
    # vs 1 right in the c3 replay); value and confidence are untouched
    assert S.support_state("What is the area of the kitchen?", a)["state"] == "approximate"
    ev = [Evidence(file="x.png", page=0, bbox=[1, 2, 3, 4], bbox_space="image_px", note="text 'KITCHEN 14,5 m²'")]
    a = Answer(value=14.5, unit="m2", confidence=0.75, evidence=ev, solver="pipeline/raster")
    assert S.support_state("What is the area of the kitchen?", a)["state"] == "supported"


# --- the text reader end to end (stubbed VLM reply + text layer) ----------------------------------------------------
@pytest.fixture
def textqa_stub(tmp_path, monkeypatch):
    from PIL import Image

    from stoa.solvers.raster import chains as CH
    from stoa.solvers.raster import scalebar as SB
    from stoa.solvers.raster import textlayer as T
    from stoa.solvers.raster import textqa as Q

    img = tmp_path / "s100.png"
    Image.new("L", (5000, 3500), 255).save(img)
    monkeypatch.setattr(SB, "bars", lambda *a, **k: [])
    monkeypatch.setattr(SB, "printed_bar", lambda *a, **k: None)
    monkeypatch.setattr(SB, "user_bar", lambda *a, **k: None)
    monkeypatch.setattr(CH, "chain_bar", lambda *a, **k: None)
    monkeypatch.setattr(CH, "loose_chain_bar", lambda *a, **k: None)

    def run(question, lines, reply):
        items = [T.TextItem(text=t, box=b) for t, b in zip(lines, S100_BOXES * 3)]
        tl = T.TextLayer(path=str(img), size=(5000, 3500), items=items)
        monkeypatch.setattr(Q.llm, "chat", lambda *a, **k: SimpleNamespace(text=json.dumps(reply)))
        return Q.answer(str(img), question, layer=tl)

    return run


def test_textqa_s100_wording_does_not_return_10000_as_supported(textqa_stub):
    lines = ["SCALE: 1 : 50", *S100_CITED]
    a = textqa_stub(S100_Q, lines, {"answer": "10000", "unit": "m2", "lines": [1, 2, 3, 4]})
    assert not (a.value == 10000 and a.confidence > S.REJECT_CONF)
    assert any("elevation" in m for m in a.missing)


def test_textqa_controls(textqa_stub):
    a = textqa_stub("What is the top of slab elevation?", ["SCALE: 1 : 50", *S100_CITED],
                    {"answer": "10000", "unit": "mm", "lines": [4]})
    assert str(a.value) in ("10000", "10000.0") and a.confidence >= 0.5
    assert not any(str(m).startswith(S.TAG) for m in a.missing)
    b = textqa_stub("What is the area of the kitchen?", ["KITCHEN", "14,5 m²"],
                    {"answer": "14,5", "unit": "m2", "lines": [0, 1]})
    assert b.value == 14.5 and not any(str(m).startswith(S.TAG) for m in b.missing)


def test_mark_support_states():
    q = "How many doors are there?"
    boxes = [Evidence(file="x.png", page=0, bbox=[i, i, i + 1, i + 1], bbox_space="image_px", exact=False,
                      note=f"door {i}") for i in range(12)]
    a = S.mark_support(q, Answer(value=20, unit="count", confidence=0.4, evidence=boxes, solver="pipeline/x"))
    assert a.trace[-1].startswith("support: approximate") and "12 of 20" in a.trace[-1]
    b = S.mark_support(q, Answer(value=12, unit="count", confidence=0.4, evidence=boxes, solver="pipeline/x"))
    assert b.trace[-1].startswith("support: supported")
    c = S.mark_support(q, Answer(value=7, unit="count", confidence=0.4, solver="pipeline/prior"))
    assert c.trace[-1].startswith("support: unlocated")
    assert S.mark_support(q, Answer(value=None)).trace == []


@pytest.mark.parametrize("q,value,unit,line", [
    # sweep over 300 earlier prediction files (T1 c1): these were right and must stay unrejected
    ("What size is the suction header?", 16.0, "in", '16" SUCTION HEADER'),  # 'size' parses as area; unit decides
    ("What size is the pipe going to the truck fill stand?", 6, "in", '6" TO TRUCK FILL STAND, SEE'),
    ("On sheet M-101A, how many distinct FSR-tagged components are shown?", 2, "count", "FSR-2"),
    ("On sheet M-101A, how many distinct CV-tagged components are shown?", 5, "count", "TO DESIGNER #5)"),
    ("What is the floor area of the studio apartment?", 31.0, "M²", "31.0M"),  # OCR lost the superscript
    ("What is the riser height of the stairs?", 16.2, "cm", "16.2/27.8"),  # a pair, not a date
    ("How many square metres is the Maths 3?", 90.0, "m2", "(Optional 90m 2 )"),  # detached superscript
])
def test_sweep_controls_are_not_rejected(q, value, unit, line):
    v = S.check_citation(q, value, [line], unit=unit)
    assert v is None or v.ok, v


def test_sweep_catches_a_length_as_area():
    # D1 sf probe: 'floor area of the Chief's Office in square feet' -> 9 ft² from the dimension 9'-0" (wrong)
    v = S.check_citation("What is the floor area of the Chief's Office in square feet?", 9.0, ["9'-0\""], unit="ft2")
    assert v is not None and not v.ok and v.role == "length"


def _cited(value, unit, lines, q="What is the area of the 150 mm slab?"):
    ev = [Evidence(file="x.png", page=0, bbox=[1, 1 + 9 * i, 5, 5 + 9 * i], bbox_space="image_px", exact=False,
                   note=f"text '{t}'") for i, t in enumerate(lines)]
    return S.support_state(q, Answer(value=value, unit=unit, confidence=0.4, evidence=ev, solver="pipeline/raster"))


def test_marked_text_that_does_not_print_the_number_is_unlocated():
    # head's S-100F PNG reading (T1 c1 repro): 1500 m² @0.05 citing a file number and the slab callouts
    st = _cited(1500.0, "m2", ["FILE NUMBER:", "K", "15522", "MOTOR CONTROL", "300 SLAB-ON-GRADE W/", "ADE W/ D16"])
    assert st["state"] == "unlocated"
    # the product of two printed sides (mm -> m²) is computed, not printed: approximate
    st = _cited(61.69, "m2", ["6200", "9950"])
    assert st["state"] == "approximate" and "6200" in st["why"]


def test_one_digit_ocr_slip_is_approximate_not_unlocated():
    # head's S-100F PNG text layer: 'TOP OF SLAB EL. = 1000' while the VLM read 10000 off the image
    st = _cited(10000, None, ["TOP OF SLAB", "TOP OF SLAB EL. = 1000"], q="What is the top of slab elevation?")
    assert st["state"] == "approximate" and "check it" in st["why"]


def test_post_hoc_text_search_box_is_not_proof():
    # E1 textground attaches where the value is printed AFTER the answer: the elevation would look 'supported'
    ev = [Evidence(file="x.png", page=0, bbox=[1, 1, 5, 5], bbox_space="image_px", exact=False,
                   note="'10000' printed here (text search on the sheet; 1 place(s) print it; check it is the line the "
                        "answer means)")]
    st = S.support_state(S100_Q, Answer(value=10000, unit="m2", confidence=0.3, evidence=ev, solver="pipeline/agent"))
    assert st["state"] == "approximate" and "search" in st["why"]


def test_clean_boxes_orders_and_drops_undrawable_boxes():
    ev = [Evidence(file="x.pdf", page=0, bbox=[5, 5, 1, 1], bbox_space="pdf_pt", note="reversed"),
          Evidence(file="x.pdf", page=0, bbox=[1], bbox_space="pdf_pt", note="short"),
          Evidence(file="x.pdf", page=0, bbox=[1, 2, float("nan"), 4], bbox_space="pdf_pt", note="nan"),
          Evidence(file="x.pdf", page=0, bbox=[1, 2, 3, 4], bbox_space="pdf_pt", note="fine")]
    a = S.clean_boxes(Answer(value=1, unit="count", evidence=ev))
    assert [e.bbox for e in a.evidence] == [[1, 1, 5, 5], None, None, [1, 2, 3, 4]]
    assert [e.note for e in a.evidence] == ["reversed", "short", "nan", "fine"]


def test_own_traced_outline_is_approximate_not_a_printed_value():
    """T1 c2, Stoa's DfE sheet (upload, plan_rooms): 'Laundry: 7.03 m² traced at 1:200 (approximate)' + its outline was
    'supported — the printed value is marked' because our own note prints the number."""
    from stoa.trust.support import support_state

    a = Answer(value=7.03, unit="m2", confidence=0.55, solver="pipeline/plan_rooms",
               evidence=[Evidence(file="s.pdf", page=0, bbox=[1, 1, 50, 40], polygon=[[1, 1], [50, 1], [50, 40], [1, 40]],
                                  note="Laundry: 7.03 m² traced at 1:200 (approximate)", exact=False)])
    st = support_state("What is the floor area of the Laundry?", a)
    assert st["state"] == "approximate" and "traced" in st["why"]
    # the same outline read from the drawing's own data (exact) stays supported
    a.evidence[0].exact, a.evidence[0].note = True, "Laundry outline (IfcSpace footprint)"
    assert support_state("What is the floor area of the Laundry?", a)["state"] == "supported"
    # a printed label box (raster text layer, approximate box) still supports a printed area
    b = Answer(value=7.03, unit="m2", confidence=0.55, solver="pipeline/textqa",
               evidence=[Evidence(file="s.png", page=0, bbox=[1, 1, 50, 10], note="text: LAUNDRY 7.03 m²", exact=False)])
    assert support_state("What is the floor area of the Laundry?", b)["state"] == "supported"


def test_label_of_another_room_is_not_support():
    """X1 stoa_like_sealed: 'floor area of the dining hall?' -> 594 citing 'Sports Hall: 594 m2' (gold 290, also marked
    as 'Dining: 290 m2')."""
    from stoa.trust.support import other_room, support_state

    q = "What is the floor area of the dining hall?"
    a = Answer(value=594.0, unit="m2", confidence=0.4, solver="pipeline/area_tags",
               evidence=[Evidence(file="s.png", page=0, bbox=[1, 1, 9, 9], note="Sports Hall: 594 m2"),
                         Evidence(file="s.png", page=0, bbox=[11, 1, 19, 9], note="Dining: 290 m2")])
    st = support_state(q, a)
    assert st["state"] == "unlocated" and "Sports Hall" in st["why"]
    a.value = 290.0
    assert support_state(q, a)["state"] == "supported"
    # agreements: abbreviations, words elsewhere in the question, generic labels
    assert other_room("What is the floor area of the Multi-Purpose Hall?", "MPH: 247 m2") is None
    assert other_room("What is the area of the classroom?", "Classrm 3: 52 m2") is None
    assert other_room("How big is the living room (vardagsrum)?", "VARDAGSRUM: 24 M2") is None
    assert other_room("What is the total dormitory area of the west wing?", "DORMITORY = 6,095 SF") is None
    assert other_room("What is the area of the main hall?", "Hall: 217 m2") is None
    assert other_room("How big is the kitchen?", "Servery: 12 m2") == "the Servery"


def test_shape_check_makes_the_citation_unlocated():
    """wild_sealed: 'How long is the meeting room?' -> 196 ft2 citing '(196 SF)'; the pipeline's shape check capped it
    at 0.3 but the card still said 'supported'."""
    from stoa.trust.support import support_state

    a = Answer(value=196.0, unit="ft2", confidence=0.3, solver="pipeline/pdf_text",
               evidence=[Evidence(file="s.pdf", page=0, bbox=[1, 1, 9, 9],
                                  note="text: MEETING ROOM / 102 - (196 SF) (view 6B ADA RESTROOM)")],
               missing=["the answer does not have the shape the question asks for (the question asks for a length; "
                        "the answer is in ft2)"])
    st = support_state("How long is the meeting room?", a)
    assert st["state"] == "unlocated" and "asks for a length" in st["why"]


def test_count_from_one_mark_per_item_is_not_rejected_by_a_width_literal():
    """sched6 sl-249: 'According to the door schedule, how many doors does the support module have?' = 6 (right),
    six marked rows; the only literal '6' is a width (6' - 0")."""
    from stoa.trust.support import citation_guard, final_guard

    rows = ["7.00A; Width: 10' - 0\"", "7.01A; Width: 3' - 0\"", "7.02A; Width: 3' - 0\"", "7.03A; Width: 6' - 0\"",
            "7.04A; Width: 6' - 0\"", "7.04B; Width: 10' - 0\""]
    a = Answer(value=6, unit="count", confidence=0.3, solver="pipeline/pdf_text",
               evidence=[Evidence(file="s.pdf", page=0, bbox=[1, 1 + 10 * i, 9, 9 + 10 * i],
                                  note=f"text: table row: Door Number: {r}") for i, r in enumerate(rows)])
    q = "According to the door schedule, how many doors does the support module have?"
    assert final_guard(q, citation_guard(q, a.model_copy(deep=True))).value == 6
    # with fewer marks than the count, the literal check still runs
    b = a.model_copy(deep=True)
    b.evidence = b.evidence[3:4]
    assert final_guard(q, citation_guard(q, b)).value is None
