"""G1 c5: printed count statements ('SECOND FLOOR = 48 DORMS'), stacked labels, tag family scope, unit / shape guards."""
from stoa.contract import Answer
from stoa.solvers.label_count import _stacked_pairs, _tag_prefixes, asked_types, pick_stated, stated_rows


def _rows(*lines):
    # (text, box, display box): one printed row per y, label left of the number
    out = []
    for i, (left, right) in enumerate(lines):
        y = 10 * i
        if left:
            out.append((left, (0, y, 40, y + 6), (0, y, 40, y + 6)))
        out.append((right, (45, y, 80, y + 6), (45, y, 80, y + 6)))
    return tuple(out)


DORMS = _rows(("FIRST FLOOR =", "48 DORMS"), ("SECOND FLOOR =", "48 DORMS"), ("THIRD FLOOR =", "48 DORMS"),
              ("TOTAL =", "144 DORMS"))


def _pick(q, lines=DORMS):
    row, why, conf = pick_stated(stated_rows(lines, asked_types(q)), q, lines)
    return None if row is None else row["n"]


def test_stated_rows_by_scope():
    assert _pick("How many dorm rooms are on the second floor?") == 48
    assert _pick("How many dorm rooms does the whole building have?") == 144
    assert _pick("How many dormitories are drawn?") == 48  # every floor row agrees
    assert _pick("How many accessible (ABA) dorm rooms are drawn on this floor?") is None
    assert _pick("How many bedrooms are there?") is None


def test_room_numbers_are_not_counts():
    lines = _rows((None, "101 OFFICE"), (None, "102 OFFICE"))
    assert stated_rows(lines, asked_types("How many offices are there?")) == []
    assert [r["n"] for r in stated_rows(_rows((None, "2 x WC")), asked_types("How many WCs are there?"))] == [2]


def test_floor_rows_differ_without_scope_abstains():
    lines = _rows(("FIRST FLOOR =", "40 DORMS"), ("SECOND FLOOR =", "48 DORMS"))
    assert _pick("How many dorm rooms are there?", lines) is None
    assert _pick("How many dorm rooms does the building have in total?", lines) == 88  # sum of floors, no total row


def test_stacked_label_pairs():
    lines = (("Secondary", (10, 10, 60, 18)), ("Classroom", (8, 20, 62, 28)), ("Store", (200, 10, 230, 18)))
    pairs = _stacked_pairs(lines)
    assert [(a[0], b[0]) for a, b in pairs] == [("Secondary", "Classroom")]


def test_tag_family_must_name_the_counted_thing():
    assert _tag_prefixes("Into how many panels do the control joints (CJ) divide the 300 mm D16 slab?", ()) == ([], "")
    assert _tag_prefixes("How many pressure gauges (PG) are tagged on the plan?", ())[0] == ["PG"]


def test_unit_guard_metric_sheet_mm():
    from stoa.pipeline import _unit_guard

    a = Answer(value="150", unit=None, confidence=0.65, solver="pdf_text")
    up = ".stoa/neutral/x1sl/c6ff0aa6e6/c6ff0aa6e6.pdf"
    out = _unit_guard("How thick is the slab reinforced with D13 bars?", a, up, {"scale": "1:50"})
    assert (out.value, out.unit) == (150.0, "mm") and out.confidence <= 0.6
    assert _unit_guard("How thick is the slab?", a, up, {"scale": '1/8" = 1\'-0"'}).unit is None
    assert _unit_guard("How many slabs are there?", a, up, {"scale": "1:50"}).unit is None


def test_pipe_run_callouts():
    from stoa.solvers.pipe_runs import _assemble, _describe

    def ln(t, x, y):
        return (t, (x, y, x + 60, y + 10), (x, y, x + 60, y + 10))

    lines = (ln('16" SS FROM', 100, 100), ln("OPERATING TANKS", 101, 111), ln('16" SUCTION HEADER', 400, 50),
             ln('6" TO TRUCK FILL STAND, SEE', 300, 300), ln("NOTE 13 ON SHEET M-001", 300, 311), ln('8"', 500, 500),
             ln("UNRELATED NOTE", 300, 330))
    calls = {c["text"]: _describe(c) for c in _assemble(lines)}
    assert calls['16" SS FROM OPERATING TANKS'] == ("from", "OPERATING TANKS")
    assert calls['16" SUCTION HEADER'] == ("name", "SUCTION HEADER")
    assert calls['6" TO TRUCK FILL STAND, SEE NOTE 13 ON SHEET M-001'] == ("to", "TRUCK FILL STAND (see note 13 on sheet m-001)")
    assert not any(t.startswith('8"') for t in calls)  # a bare size is not a callout


def test_pipe_runs_route_only_text_questions():
    from pathlib import Path

    import pytest

    from stoa.pipeline import route
    from stoa.router.intent import parse

    up = ".stoa/neutral/x1sl/acf1f07a2d/acf1f07a2d.pdf"
    if not Path(up).is_file():
        pytest.skip("needs the stoa-like upload copy (RL 17:38: fresh clones do not have it)")
    assert route(up, parse('What does the 16" line connect to?'))[0] == "pipe_runs"
    assert "pipe_runs" not in route(up, parse("How many fuel pumps (FP) are shown?"))


def test_door_tags_us_numbering():
    from stoa.solvers.label_count import door_tags

    def b(x, y):
        return (x, y, x + 20, y + 8)

    lines = (("CONTROL ROOM", (100, 100, 180, 110)), ("101", b(120, 112)), ("TOILET", (300, 100, 340, 110)),
             ("102", b(305, 112)), ("101A", b(400, 300)), ("101B", b(500, 300)), ("102", b(340, 200)),
             ("600", b(10, 10)), ("3600", b(60, 10)))
    tags, rooms = door_tags(lines)
    assert [k for k, _ in tags] == ["101A", "101B", "102"] and rooms == ["101", "102"]
    assert door_tags((("101", b(0, 0)), ("102", b(50, 0)))) is None  # no room names, no lettered tag


def test_dims_guard_feet_inches_and_own_number():
    from stoa.pipeline import _dims_guard, _own_number

    up = ".stoa/neutral/x1sl/c6ff0aa6e6/c6ff0aa6e6.pdf"
    a = _dims_guard("How large is the 1'-6\" slab-on-grade area?", Answer(value='7’-0"X7’-0"', confidence=0.65,
                                                                        solver="x"), up)
    assert (a.value, a.unit) == (49.0, "ft2")
    assert _own_number("What is the area of the 300 mm slab-on-grade?", Answer(value="300", confidence=0.6, solver="x"))
    assert not _own_number("What is the area of the 300 mm slab?", Answer(value=214.5, unit="m2", confidence=0.6,
                                                                          solver="x"))


def test_type_quantities_kpl_with_mirror():
    from stoa.solvers.label_count import type_quantities

    lines = _rows(("TÄTÄ TYYPPIÄ", "25 KPL"), ("PEILIKUVA KEITTIÖTÄ", "25 KPL"), ("SCREWS", "120 KPL"))
    used, why = type_quantities(lines, "How many kitchens of this type are built in total, including the mirror-image ones?")
    assert sum(r["n"] for r in used) == 50
    used, _ = type_quantities(lines, "How many kitchens of this type are built?")
    assert sum(r["n"] for r in used) == 25
    assert type_quantities(lines, "How many kitchens are there?") is None


def test_printed_sides_at_traced_outline():
    from stoa.solvers.line_rooms import printed_sides

    m_per_pt = 50 * 0.0254 / 72  # 1:50
    poly = [(1455, 623), (1553, 623), (1553, 736), (1455, 736)]  # traced 1.73 x 1.99 m (A-101F toilet)
    lines = (("1900", (1497, 751, 1518, 761)), ("2054", (1580, 670, 1589, 691)), ("9800", (1533, 1351, 1554, 1360)),
             ("TOILET", (1487, 664, 1531, 676)))
    d = printed_sides(lines, poly, m_per_pt)
    assert d["x"][0] == 1.9 and d["y"][0] == 2.054
    assert printed_sides((("5000", (1497, 751, 1518, 761)),), poly, m_per_pt) == {}  # not within 25 % of a side


def test_area_larger_than_sheet_is_capped(tmp_path):
    import fitz

    from stoa.pipeline import _unit_guard

    d = fitz.open()
    d.new_page(width=2384, height=1684)  # A1 in pt
    f = tmp_path / "upload.pdf"
    d.save(f)
    big = _unit_guard("What is the total slab area?", Answer(value=102000, unit="m2", confidence=0.3, solver="x"),
                      str(f), {"scale": "1:50"})
    assert big.confidence == 0.05
    ok = _unit_guard("What is the total slab area?", Answer(value=324, unit="m2", confidence=0.3, solver="x"), str(f),
                     {"scale": "1:50"})
    assert ok.confidence == 0.3


def test_printed_mm_dimension_is_not_an_area(tmp_path):
    import fitz

    from stoa.pipeline import _unit_guard

    d = fitz.open()
    pg = d.new_page(width=1191, height=842)  # A3
    pg.insert_text((100, 100), "7200")
    pg.insert_text((100, 200), "English 9")
    f = tmp_path / "sheet.pdf"
    d.save(f)
    a = _unit_guard("What is the floor area of English 9?", Answer(value=7200, unit="m2", confidence=0.32, solver="x"),
                    str(f), {"scale": "1:200"})
    assert a.confidence == 0.1
    b = _unit_guard("What is the floor area of English 9?", Answer(value=56, unit="m2", confidence=0.32, solver="x"),
                    str(f), {"scale": "1:200"})
    assert b.confidence == 0.32


def test_rooms_labelled_x():
    assert asked_types("How many rooms labelled 'Bad' are there on this floor?") == ["bad"]
    assert asked_types("How many rooms labelled Infants are there?")[:1] == ["infants"]


def test_agent_does_not_say_confirmed_with_one_source():
    from stoa.router.agent import _honest_words

    t = _honest_words(["think: the floor area of the Salón is confirmed as 16 m²"], 1)
    assert "confirmed as" not in t[0] and "unconfirmed" in t[0]
    assert _honest_words(["confirmed by two tools"], 2) == ["confirmed by two tools"]


def test_scale_question_answered_from_printed_scale(tmp_path):
    import fitz

    from stoa.pipeline import _scale_answer

    d = fitz.open()
    pg = d.new_page(width=1191, height=842)
    pg.insert_text((900, 800), "SCALE 1:200")
    f = tmp_path / "plan.pdf"
    d.save(f)
    a = _scale_answer(str(f), "What is the scale of this drawing?", {"scale": "1:200", "scale_source": "printed"})
    assert a.value == "1:200" and a.confidence >= 0.7 and a.evidence
    assert _scale_answer(str(f), "How long is the scale bar?", {"scale": "1:200", "scale_source": "printed"}) is None
    assert _scale_answer(str(f), "What is the scale of this drawing?", {"scale": "1:200", "scale_source": "bar"}) is None


def test_tableqa_value_not_printed_is_capped(tmp_path):
    import fitz

    from stoa.pipeline import _tableqa_guard

    d = fitz.open()
    pg = d.new_page(width=1191, height=842)
    pg.insert_text((100, 100), "DOOR SCHEDULE 101 3'-0\" 7'-0\" QTY 12")
    f = tmp_path / "sched.pdf"
    d.save(f)
    mk = lambda v: Answer(value=v, unit="count", confidence=0.85, solver="pipeline/pdf_text", trace=["table QA code: x"])
    assert _tableqa_guard(mk(12), str(f)).confidence == 0.85  # printed
    assert _tableqa_guard(mk(37), str(f)).confidence == 0.6  # computed, not printed


def test_room_number_is_not_an_area(tmp_path):
    import fitz

    from stoa.pipeline import _room_number

    d = fitz.open()
    pg = d.new_page(width=1191, height=842)
    pg.insert_text((100, 100), "Storage")
    pg.insert_text((102, 112), "7.04")
    f = tmp_path / "plan.pdf"
    d.save(f)
    assert _room_number(Answer(value="7.04", unit="ft2", confidence=0.65, solver="x"), str(f))
    assert not _room_number(Answer(value=1786, unit="ft2", confidence=0.65, solver="x"), str(f))


def test_line_rooms_numbered_labels_and_no_deduct_skirting():
    from stoa.solvers.line_rooms import _NO_DEDUCT, _label

    lines = (("English 2", (0, 0, 40, 8)), ("English 9", (100, 0, 140, 8)))
    lab, why = _label("What is the perimeter of the English 9?", lines)
    assert lab[0] == "English 9"
    assert _NO_DEDUCT.search("How many linear metres of skirting board does the Plant need, ignoring door openings?")
    assert not _NO_DEDUCT.search("How many linear metres of skirting board does the laundry need (minus the door)?")
