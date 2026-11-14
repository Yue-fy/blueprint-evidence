"""G1 c6: door_detect's scope logic (no detector service needed) and the routes it joins."""
from stoa.solvers.door_detect import _owner, _scope_title

TITLES = [  # display points, as on AFCEE A-101 (titles under their drawings)
    ("MODULE 01- ENTRY VESTIBULE - FLOOR PLAN", (60.0, 1440.0, 300.0, 1453.0)),
    ("MODULE 02-ADMINISTRATION - FLOOR PLAN", (355.0, 1440.0, 622.0, 1453.0)),
    ("MODULE 02 - ADMINISTRATION - RCP", (922.0, 1440.0, 1144.0, 1453.0)),
    ("MODULE 01 - ENTRY VESTIBULE - RCP", (700.0, 1440.0, 900.0, 1453.0)),
]


def test_scope_title_picks_the_named_floor_plan():
    i, words, why = _scope_title("How many doors does the administration module (module 02) have?", TITLES)
    assert TITLES[i][0] == "MODULE 02-ADMINISTRATION - FLOOR PLAN", why
    i, _, _ = _scope_title("How many doors are there in the entry vestibule module?", TITLES)
    assert TITLES[i][0] == "MODULE 01- ENTRY VESTIBULE - FLOOR PLAN"


def test_scope_title_whole_sheet_and_unknown_scope():
    assert _scope_title("How many doors are on this plan?", TITLES)[0] is None
    assert _scope_title("How many doors are on this plan?", TITLES)[1] == set()
    i, words, why = _scope_title("How many windows does English 1 have?", TITLES)
    assert i is None and "english" in words and why.startswith("no viewport title")


def test_owner_is_the_nearest_title_below():
    assert _owner((400.0, 1200.0, 420.0, 1230.0), TITLES) == 1  # above the module 02 plan title
    assert _owner((200.0, 1300.0, 220.0, 1330.0), TITLES) == 0
    assert _owner((200.0, 1500.0, 220.0, 1530.0), TITLES) is None  # below every title


def test_routes():
    from stoa.pipeline import route
    from stoa.router.intent import parse

    up = "/tmp/some_upload/sheet.pdf"
    r = route(up, parse("How many doors does the administration module have?"))
    assert "door_detect" in r and r.index("door_detect") < r.index("plan_rooms")
    assert "door_detect" not in route(up, parse("How many rooms are on this plan?"))
    assert "door_detect" not in route("data/cubicasa5k/colorful-10052.png", parse("How many doors are there?"))


def test_vision_numeric_answer_and_derotation():
    """W6 18:16: 'S-100F' must not become the value -100; Gemini boxes on rotated pages go back to unrotated pdf_pt."""
    from stoa.agent.vision import _NUMERIC_ANSWER

    assert not _NUMERIC_ANSWER.fullmatch("S-100F") and not _NUMERIC_ANSWER.fullmatch("B-104")
    assert not _NUMERIC_ANSWER.fullmatch("1:200") and not _NUMERIC_ANSWER.fullmatch("3.44 x 1.90")
    assert _NUMERIC_ANSWER.fullmatch("1,240 SF") and _NUMERIC_ANSWER.fullmatch("12") and \
        _NUMERIC_ANSWER.fullmatch("~ 45 m²")


def test_derotation_round_trip(tmp_path):
    import pymupdf

    from stoa.agent.vision import _derotation

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    page.insert_text((50, 100), "MODULE 02 - FLOOR PLAN")
    page.set_rotation(90)
    f = tmp_path / "rot.pdf"
    doc.save(str(f))
    m = _derotation(str(f))
    assert m is not None
    with pymupdf.open(str(f)) as d:
        pg = d[0]
        r = pymupdf.Rect(50, 90, 150, 102)
        disp = r * pg.rotation_matrix
        disp.normalize()
        back = disp * m
        back.normalize()
    assert all(abs(a - b) < 1e-6 for a, b in zip(back, r))


def test_room_range_parts():
    """X1 18:15 (1): numbered room ranges are summed room by room; nothing else fires (3 of 5,755 split questions)."""
    from stoa.pipeline import _room_range_parts as R

    assert R("What is the combined area of Science Labs 1 to 5?") == ("Science Labs", 1, 5, "area")
    assert R("Total floor area of the maths classrooms (Maths 1-5)?") == ("Maths", 1, 5, "area")
    assert R("What is the total floor area of English 1 to English 5?") == ("English", 1, 5, "area")
    assert R("What is the area of Science Lab 3?") is None
    assert R("What is the floor area of levels 1 to 3?") is None
    assert R("How many linear metres of skirting do Classrooms 1-3 need?") is None


def test_gross_question_words():
    from stoa.pipeline import _GROSS_Q

    assert not _GROSS_Q.search("What is the total floor area of all the rooms labelled Infants?")
    assert not _GROSS_Q.search("How big is the Learning Resource Centre?")
    assert _GROSS_Q.search("What is the gross floor area?") and _GROSS_Q.search("How large is the total common space on this floor?")


def test_counted_rooms_need_matching_labels(tmp_path):
    """X1 sl-121: 'the three MFL classrooms' = labels 'MFL 1..3' only when exactly three such labels are printed."""
    import pymupdf

    from stoa.pipeline import _counted_rooms

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    for i, t in enumerate(["MFL 1", "MFL 2", "MFL 3", "MFL SWR", "English 1"]):
        page.insert_text((50, 50 + 30 * i), t)
    f = tmp_path / "plan.pdf"
    doc.save(str(f))
    assert _counted_rooms(str(f), "How much floor area do the three MFL classrooms have together?") == ("MFL", 1, 3, "area")
    assert _counted_rooms(str(f), "What is the area of the four MFL classrooms?") is None
    assert _counted_rooms(str(f), "How many MFL classrooms are there?") is None


def test_printed_unit_wins_over_agent_unit():
    """stoa-like sl-100: raster '702 SF' (printed) vs the agent's 702 'm2' (same number): the printed reading is kept."""
    from stoa.contract import Answer
    from stoa.pipeline import Pipeline
    from stoa.router.intent import parse

    p = Pipeline.__new__(Pipeline)
    q = parse("How big is the staff workstation area?")
    ra = Answer(value="702 SF", unit="SF", confidence=0.3, trace=["raster"], solver="raster")
    o = p._vs_unsure(Answer(value=702.0, unit="m2", confidence=0.318, trace=["a"], solver="pipeline/agent"), ("raster", ra), q)
    assert o.value == "702 SF" and o.solver == "pipeline/raster"
    o2 = p._vs_unsure(Answer(value=650.0, unit="m2", confidence=0.318, trace=["a"], solver="pipeline/agent"), ("raster", ra), q)
    assert o2.value == 650.0


def test_asked_unit_conversion():
    """H 18:54: the unit the question asks for wins (same kind only); counts and unasked units are untouched."""
    from stoa.contract import Answer
    from stoa.pipeline import _asked_unit, _to_asked_unit

    assert _asked_unit("How wide is it in cm?") == "cm"  # the older one-argument helper must stay (it was shadowed)
    o = _to_asked_unit("How many square metres is the west wing in total?", Answer(value=7009.0, unit="SF", confidence=.5))
    assert (o.value, o.unit) == (651.16, "m2") and "converted" in o.assumptions[-1]
    o = _to_asked_unit("What is the total length of the foundation in metres?", Answer(value=27000.0, unit="mm", confidence=.5))
    assert (o.value, o.unit) == (27.0, "m")
    assert _to_asked_unit("What is the area of the hall?", Answer(value=7009.0, unit="SF", confidence=.5)).unit == "SF"
    assert _to_asked_unit("How many doors in metres?", Answer(value=3, unit="count", confidence=.5)).value == 3
