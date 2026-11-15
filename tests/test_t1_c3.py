"""T1 cycle 3: three overclaims found by replaying support_state over 400 prediction files (experiments/t1/
audit_support.py, audit_kinds.py, audit_parts.py). No model, OCR or network.

A. An area cited by a bare metric dimension ('16.850' read as 16.85 m², EFA scans; 13 / 13 wrong at 0.75) is rejected;
   a bare number without an area unit is 'approximate', never 'supported'.
B. One printed part for a question about a total of several ('DORMITORY = 6,095 SF' for '… across all three wings')
   keeps its value at confidence <= 0.4, says so first in missing, and is 'approximate'.
C. A prior guess (most common training answer) is never 'supported', whatever boxes are attached."""

from __future__ import annotations

import pytest

from stoa.contract import Answer, Evidence
from stoa.trust import sanity as Z
from stoa.trust import support as S


def _ans(value, unit, notes, solver="pipeline/raster", conf=0.75, exact=False, **kw):
    ev = [Evidence(file="x.png", page=1, bbox=[10.0 + 50 * i, 10.0, 40.0 + 50 * i, 30.0], note=n, exact=exact)
          for i, n in enumerate(notes)]
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=solver, **kw)


# --- A: bare dimensions are not areas ---------------------------------------------------------------------------------
@pytest.mark.parametrize("q,value,line", [
    ("What's the net floor area of the PE Store?", 16.85, "16.850"),
    ("What is the floor area of the Dining?", 37.8, "37.800"),
    ("How big is the main hall?", 18.0, "18,000"),
    ("How many square metres is the Food bay/practical resources?", 10.0, "10,000"),
])
def test_a_three_decimal_dimension_is_not_an_area(q, value, line):
    v = S.check_citation(q, value, [line], unit="m²")
    assert v is not None and not v.ok and v.role == "dimension"
    out = S.final_guard(q, _ans(value, "m²", [f"text '{line}'"]))
    assert out.value is None and out.missing[0].startswith("no printed area")


@pytest.mark.parametrize("q,value,unit,line", [
    ("How many square feet of dormitory does the north wing have?", 6285.0, "ft2", "6,285"),  # thousands reading
    ("What is the area of the kitchen?", 32.05, "m2", "Kitchen 32.05 m²"),
    ("What is the area of the kitchen?", 14.5, "m2", "KITCHEN 14,500 m²"),  # unit printed: an area
    ("How long is the main hall?", 16.66, "m", "16.660"),  # a dimension IS a length
])
def test_a_controls_are_not_rejected(q, value, unit, line):
    v = S.check_citation(q, value, [line], unit=unit)
    assert v is None or v.ok


def test_a_bare_number_without_area_unit_is_approximate():
    st = S.support_state("How big is the Communications?", _ans(20.3, "m2", ["text '20.3'"]))
    assert st["state"] == "approximate" and "no area unit" in st["why"]
    st = S.support_state("What is the area of the kitchen?", _ans(32.05, "m2", ["text 'Kitchen 32.05 m²'"]))
    assert st["state"] == "supported"
    st = S.support_state("What is the floor area of the studio apartment?", _ans(31.0, "m2", ["text '31.0M'"]))
    assert st["state"] == "supported"  # OCR lost the superscript '²'


# --- B: one printed part for a total ----------------------------------------------------------------------------------
AFCEC = [
    ("What is the total dormitory area on this floor across all three wings?", 6095.0,
     "DORMITORY = 6,095 SF (printed area table, read from the image's text)"),
    ("How much circulation area is there on the second floor in total?", 1240.0,
     "E CIRCULATION = 1,240 SF (printed area table, read from the image's text)"),
    ("How large is the total common space on this floor, including its circulation and utility areas?", 2911.0,
     "COMMON SPACE = 2,911 SF (printed area table, read from the image's text)"),
]


@pytest.mark.parametrize("q,value,note", AFCEC)
def test_b_one_part_for_a_total_is_capped_and_approximate(q, value, note):
    a = _ans(value, "ft2", [note], solver="pipeline/area_table", conf=0.7)
    out = Z.sanity_guard(q, a)
    assert out.value == value and out.confidence <= Z.PARTS_CAP
    assert out.missing[0].startswith(S.PARTS_MISSING)
    st = S.mark_support(q, out).support or S.support_state(q, out)
    assert st["state"] == "approximate"


def test_b_three_trenches_one_printed_length():
    q = "What is the total length of the three trenches listed in the notes?"
    a = _ans(4275.0, "mm", ["text 'MOTOR CONTROL CENTER TRENCH LENGTH - 4275 / PUMP CONTROL PANEL TRENCH LENGTH - 1925'"],
             solver="pipeline/pdf_text", conf=0.55)
    assert Z.sanity_guard(q, a).confidence <= Z.PARTS_CAP


@pytest.mark.parametrize("q,value,notes", [
    ("What is the total floor area of the building?", 2435.0, ["text 'TOTAL FLOOR AREA = 2,435 m²'"]),  # printed total
    ("What is the combined area of the four private offices in the administration module?", 362.0,
     ["Fitness and Sports Manager Office 2.04: 120 SF", "NCOIC Office 2.05: 81 SF",
      "Operations Manager Office 2.06: 80 SF", "Sports Program Manager Office 2.07: 81 SF"]),  # a sum of 4 marks
    ("What is the total floor area of the kitchen?", 32.05, ["text 'Kitchen 32.05 m²'"]),  # 'total' alone
    ("How many square feet of dormitory does the north wing have?", 6285.0, ["NORTH WING DORMITORY = 6,285 SF"]),
])
def test_b_controls_keep_their_confidence(q, value, notes):
    a = _ans(value, "m2", notes, solver="pipeline/area_tags", conf=0.8, exact=True)
    assert Z.sanity_guard(q, a).confidence == 0.8


# --- C: a prior guess is never 'supported' ---------------------------------------------------------------------------
def test_c_prior_guess_is_unlocated_even_with_candidate_boxes():
    a = _ans(1, "count", ["bathroom region R5 — segmentation: bathroom regions (where showers and bathtubs are drawn)"],
             solver="pipeline/prior", conf=0.759)
    st = S.support_state("How many showers and bathtubs are drawn on this floor plan in total?", a)
    assert st["state"] == "unlocated" and "not read from this drawing" in st["why"]


# --- D: a sum of marked printed parts is checkable in seconds -> supported -------------------------------------------
def test_d_sum_of_marked_printed_rows_is_supported():
    q = "What is the total dormitory area on this floor across all three wings?"
    a = _ans(18472.0, "ft2", [f"{w} WING DORMITORY = {n} SF (printed area table, read from the image's text)"
                              for w, n in (("WEST", "6,095"), ("EAST", "6,092"), ("NORTH", "6,285"))],
             solver="pipeline/area_table", conf=0.7)
    out = Z.sanity_guard(q, a)
    assert out.confidence == 0.7  # three marks: not one part
    st = S.support_state(q, out)
    assert st["state"] == "supported" and "6,095 + 6,092 + 6,285" in st["why"]
    a.value = 18000.0  # does not add up -> no claim
    assert S.support_state(q, a)["state"] == "approximate"


def test_d_wrong_number_of_parts_is_capped():
    q = "What is the combined area of the four private offices in the administration module?"
    notes = [f"Office 2.0{i}: {n} SF" for i, n in enumerate((120, 81, 80, 81, 134, 702, 153, 536))]
    a = _ans(1887.0, "ft2", notes, solver="pipeline/area_tags", conf=0.7, exact=True)
    out = Z.sanity_guard(q, a)
    assert out.confidence <= Z.PARTS_CAP and "names 4" in out.missing[0]
    assert S.support_state(q, out)["state"] == "approximate"
    assert S.named_count("across all three wings") == 3 and S.named_count("How big is the kitchen?") is None


def test_e_number_from_a_door_schedule_for_a_room_is_capped():
    q = "How long is the meeting room?"
    a = _ans(6.6667, "ft", ["t3_door_schedule row: 102A"], solver="pipeline/pdf_text", conf=0.6, exact=True)
    out = Z.sanity_guard(q, a)
    assert out.confidence == Z.SHAPE_CAP and out.value == 6.6667
    assert S.support_state(q, out)["state"] == "unlocated"
    q2 = "How wide are the control room doors (102S)?"  # about doors: a schedule is the right source
    assert Z.sanity_guard(q2, _ans(0.864, "m", ["t3_door_schedule row: 102S"], solver="pipeline/pdf_text",
                                   conf=0.85, exact=True)).confidence == 0.85


# --- F: one printed side is not a perimeter; scale bars print no values; labels with a parenthesis -------------------
def test_f_one_printed_dimension_is_not_the_skirting():
    q = "How many metres of skirting do we need for English 1?"
    out = Z.sanity_guard(q, _ans(7.2, "m", ["text '7.200'"]))
    assert out.confidence <= Z.PARTS_CAP and "one printed dimension" in out.missing[0]
    assert S.support_state(q, out)["state"] == "approximate"
    ok = _ans(28.71, "m", ["text 'English 1 perimeter 28.71 m'"])
    assert Z.sanity_guard(q, ok).confidence == 0.75


def test_f_scale_bar_note_is_not_a_printed_value():
    q = "How many linear metres of skirting board does the laundry need?"
    a = _ans(10.0, "m", ["graphic scale bar 0 10 20 m: 29.60 px per m", "searched area: the laundry"], conf=0.4)
    assert S._printed_support(q, a) is None


def test_f_spacing_is_not_a_thickness_in_support_either():
    q = "How thick is the slab reinforced with D13 bars?"
    a = _ans(300.0, "mm", ["text 'D13 @ 300'"])
    assert S._printed_support(q, a) is None and S.final_guard(q, a).value is None


def test_f_label_with_parenthesis_names_another_room():
    q = "What is the total area of the west wing? (sheet A-2)"
    a = _ans(381.0, "ft2", ["EXTERIOR STAIR (1/2 SF) = 381 SF (printed area table)"], solver="pipeline/area_table",
             conf=0.85)
    assert S.support_state(q, a)["state"] == "unlocated"


def test_g_marks_with_another_label_do_not_support_the_count():
    q = "How many rooms are labelled Plant Room on this plan?"
    out = Z.sanity_guard(q, _ans(7, "count", ["label 'Room'"] * 7, solver="pipeline/label_count", conf=0.7))
    assert out.confidence == Z.SHAPE_CAP and "'Room', not 'Plant Room'" in out.missing[0]
    assert S.support_state(q, out)["state"] == "unlocated"
    q2 = "How many rooms labelled 'Science Laboratory' are there on this plan?"
    ok = _ans(7, "count", ["label 'Science / Laboratory'"] * 7, solver="pipeline/label_count", conf=0.7)
    assert Z.sanity_guard(q2, ok).confidence == 0.7


# --- H: a list of printed dimensions is not one length (S0 21:57) -----------------------------------------------------
@pytest.mark.parametrize("q,v", [
    ("How deep are the private offices, from the corridor wall to the outside wall?", "11 3/8\" 2'-6\" 6 5/8\""),
    ("What is the length of the north wall?", "12'-0\" 3'-6\""),
    ("What is the area of the store?", "12'-0\" 10'-6\""),
])
def test_h_dimension_list_is_withheld(q, v):
    out = Z.sanity_guard(q, _ans(v, None, ["text 'x'"], solver="pipeline/pdf_text", conf=0.65))
    assert out.value is None and out.missing[0].startswith(Z.DIMLIST_MISSING)


@pytest.mark.parametrize("q,v", [
    ("What are the dimensions of the bedroom?", "14'-0\" x 12'-0\""),  # dimensions asked
    ("How wide is the corridor?", "5'-0\""),  # one dimension
    ("What size are the parking stalls shown on the site plan?", "9'x18'"),
    ("How long is the meeting room?", "2'-10\" x 6'-8\""),  # joined pair: the shape cap's business, not withheld
    ("Could you provide a comprehensive analysis of the pipe lengths?", "Total 7898.0 m, 2075 m hydronic"),
])
def test_h_controls_keep_their_value(q, v):
    assert Z.sanity_guard(q, _ans(v, None, ["text 'x'"], solver="pipeline/pdf_text", conf=0.65)).value == v


def test_i_our_candidate_notes_are_not_a_printed_total():
    q = "How many bedrooms (makuuhuone, usually marked MH) are on this floor plan?"
    a = _ans(3, "count", ["bedroom region R5, printed label 'MH 3 / MH 2' — candidate (segmentation finds 2)"],
             solver="pipeline/vlm_lora", conf=0.915)
    st = S.support_state(q, a)
    assert st["state"] == "approximate" and "showing 1 of 3" in st["why"]
    printed = _ans(48, "count", ["printed: SECOND FLOOR 48 DORMS"], solver="pipeline/stated_count", conf=0.85, exact=True)
    assert S.support_state("How many dorm rooms are on the second floor?", printed)["state"] == "supported"
