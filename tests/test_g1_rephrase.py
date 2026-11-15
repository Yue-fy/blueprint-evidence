"""G1 cycle 10: X1's rephrase probe (21:26 / 21:36 / 21:41) — the same question in other words reaches the same reader."""
from stoa.agent import vision as V
from stoa.contract import Answer, Evidence
from stoa.pipeline import _opening_count_english, _paren_english_area, _skirting_english, _terse_count
from stoa.router.intent import modifiers, parse
from stoa.solvers.label_count import _noun_matches, as_how_many, asked_types
from stoa.solvers.piping import _COUNT_CUE, _count_target


def test_terse_keyword_counts():
    assert _terse_count("doors in module 02") == "How many doors are in module 02?"
    assert _terse_count("dorm room count") == "How many dorm rooms are there?"
    assert _terse_count("door count module 02") == "How many doors are in module 02?"
    assert _terse_count("PG count") == "How many PG tags are there?"
    assert _terse_count("Doors of the laundry") == "How many doors are in the laundry?"
    for q in ("fitness room area?", "skirting length laundry", "doors", "How many doors?", "area of the D13 slab",
              "total area of the 4 offices in module 02", "what doors in module 02"):
        assert _terse_count(q) is None, q
    assert as_how_many("dorm room count") == "How many dorm rooms?"


def test_dormitory_matches_printed_dorms():
    assert _noun_matches("DORMS", asked_types("How many dormitory rooms are there on this level?"))
    assert not _noun_matches("BELOW TO OPEN", asked_types("How many dormitory rooms are there on this level?"))


def test_rooms_labelled_as_a_kind():
    assert asked_types("How many rooms are labelled Plant Room on this plan?") == ["plantroom"]
    assert asked_types('How many rooms are labelled "Plant Room"?') == ["plantroom"]
    assert asked_types("How many rooms are on level 2?") == ["rooms", "room"]


def test_piping_bare_tag_and_other_languages():
    assert _count_target("how many PG gauges") == ("PG", False)
    assert _count_target("Hur många tryckmätare (PG) finns på rörplanen?") == ("PG", False)
    assert _COUNT_CUE.search("Hur många tryckmätare (PG) finns på rörplanen?")
    assert _count_target("How many pressure sensors are there?") == ("PS", False)


def test_non_english_rules():
    assert _skirting_english("Hur många meter golvlist behövs på toaletten, dörröppningen borträknad?") == \
        "How many metres of skirting does the toilet need (room perimeter minus the door openings)?"
    assert _skirting_english("How much skirting in the toilet?") is None
    assert _opening_count_english("Montako ovea hallintomoduulissa (moduuli 02) on?") == "How many doors are in module 02?"
    assert _opening_count_english("Montako ovea moduulissa 02 ja 03 on?") is None
    assert _paren_english_area("Wie groß ist die Mehrzweckhalle (Multi-Purpose Hall)?") == \
        "What is the floor area of the Multi-Purpose Hall?"
    assert _paren_english_area("Wie groß ist das Zimmer (Bad)?") is None  # a Swiss label, not a translation


def test_skirting_door_exclusion_is_not_an_operator():
    assert modifiers("How much baseboard do I need for the laundry, leaving out the door?") == []
    assert modifiers("How many metres of skirting go round the toilet if we skip the doorway?") == []
    assert modifiers("How many doors are there, leaving out the cupboards?") == ["rest"]
    assert parse("What's the footprint of the generator set foundation?").task == "area"


def _laundry(conf=0.5):
    sq = [[0, 0], [100, 0], [100, 100], [0, 100]]
    return Answer(value=7.12, unit="m2", confidence=conf, solver="pipeline/plan_rooms", trace=["ours"],
                  evidence=[Evidence(file="up/x.png", page=0, bbox=[0, 0, 100, 100], bbox_space="image_px", polygon=sq,
                                     note="Laundry: 7.12 m² traced at 1:200 (approximate)")],
                  assumptions=["scale: 1:200 printed on the sheet; traced from the wall lines (approximate)",
                               "scale check: 133 door swings measure a median 0.85 m at 1:200 (a door leaf is ~0.7-1.0 m) ✓"])


def test_strong_outline_loses_a_little_and_shows_the_second_reading(monkeypatch):
    q = "What is the area of the Laundry?"
    assert V.strong_outline(q, _laundry())
    assert V.strong_outline("What is the area of the kitchen?", _laundry()) is None
    monkeypatch.setattr(V, "measure_at_point", lambda *a, **k: None)
    monkeypatch.setattr(V, "measure_raster_at_point", lambda *a, **k: None)
    g = Answer(value=12.5, unit="m2", confidence=0.45, solver="pipeline/gemini_vision", trace=["Gemini 3.1 Pro vision"])
    V._POINTS[("up/x.png", q)] = (500, 500, "image_px")
    a = V._area_crosscheck("up/x.png", q, _laundry(), g, "h: ")
    assert a.value == 7.12 and 0.4 <= a.confidence < 0.5  # 'best estimate', not 'our guess'
    assert a.clarify and [o["value"] for o in a.clarify["options"]] == [7.12, 12.5]
    weak = _laundry()
    weak.assumptions = weak.assumptions[:1]  # scale printed but never checked
    b = V._area_crosscheck("up/x.png", q, weak, g, "h: ")
    assert b.confidence <= 0.3


def test_footprint_first_only_without_a_printed_total(tmp_path, monkeypatch):
    import sys
    import types

    import pymupdf

    from stoa import pipeline as P

    def sheet(name, text):
        d = pymupdf.open()
        pg = d.new_page(width=842, height=595)
        for i, t in enumerate(text):
            pg.insert_text((60, 60 + 20 * i), t)
        f = str(tmp_path / name)
        d.save(f)
        return f

    plain = sheet("plain.pdf", ["LEVEL 00 GA PLAN", "SCALE 1:200 @ A1"])
    table = sheet("table.pdf", ["TOTAL =", "26,181 SF", "SECOND FLOOR PLAN"])
    inline = sheet("inline.pdf", ["GIFA 1,247 m2"])
    assert P._printed_total(plain) is None
    assert P._printed_total(table) == "TOTAL ="
    assert P._printed_total(inline) == "GIFA 1,247 m2"
    fake = types.ModuleType("stoa.sheet.footprint")
    fake.is_gross_area_question = lambda q: "total floor area" in q
    fake.footprint_answer = lambda p, q, page=0: Answer(value=1670.8, unit="m2", confidence=0.6, solver="footprint",
                                                         trace=["footprint (VP): outline"])
    monkeypatch.setitem(sys.modules, "stoa.sheet.footprint", fake)
    pipe = P.Pipeline.__new__(P.Pipeline)
    a = pipe._footprint_first(plain, "What is the total floor area on this plan?", [], 0.0)
    assert a.value == 1670.8 and a.solver == "pipeline/footprint"
    tr: list[str] = []
    assert pipe._footprint_first(table, "What is the total floor area on this plan?", tr, 0.0) is None
    assert "prints a total" in tr[0]
    assert pipe._footprint_first(plain, "What is the area of the laundry?", [], 0.0) is None


def test_agent_find_text_reads_an_image_text_layer(monkeypatch):
    import stoa.solvers.label_count as L
    from stoa.router.agent import Agent as GeneralAgent

    monkeypatch.setattr(L, "_image_lines", lambda p: (("2 1/2 IN WASHDOWN", (10, 20, 90, 30)), ("12 IN CARGO FILLING", (5, 50, 99, 60))))
    ag = GeneralAgent.__new__(GeneralAgent)
    monkeypatch.setattr(ag, "_pdf", lambda p: None, raising=False)
    ev: list = []
    out = ag._find_text("up/haer.png", "washdown", ev)
    assert "1 hit" in out and ev and ev[0].bbox_space == "image_px"


def test_no_gemini_crosscheck_on_a_printed_marked_value():
    printed = Answer(value=4479.0, unit="ft2", confidence=0.55, solver="pipeline/pdf_text",
                     evidence=[Evidence(file="up/a.png", page=0, bbox=[0, 0, 5, 5], bbox_space="image_px", exact=True,
                                        note="printed: TOTAL COMMON SPACE = 4,479 SF")])
    assert V.wanted(printed, "What is the total common area on this floor?", "up/a.png") is None
    traced = _laundry(0.55)
    traced.evidence[0].exact = True  # even if flagged exact, our own 'traced at 1:200' note is not printed text
    # captain 00:31 (RECHECK-0026 (1)): a located outline measured at the PRINTED scale (scale check ✓) gets no Pro call
    assert V.wanted(traced, "What is the area of the Laundry?", "up/x.png") is None
    traced.assumptions = ["the outline was traced from the wall lines (approximate)"]  # no known scale -> cross-check
    assert V.wanted(traced, "What is the area of the Laundry?", "up/x.png").startswith("area cross-check")
